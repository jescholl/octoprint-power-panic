"""Test-support plugin: a serial port backed by a simulated printer.

Two firmwares, chosen by ``config.json`` in the control directory
(``FAKE_PRINTER_DIR``):

``prusa`` models Prusa-Firmware 3.14.1's host-print power panic, from its source:

- ``ok`` is sent once a move is *planned*; a 16-entry planner drains in the
  background, so the printer runs behind what OctoPrint has sent.
- On mains loss it saves the last *completed* line number (not the last received)
  to "EEPROM", prints ``INT4`` and goes silent.
- On the next boot it restores the print by itself, prints ``//action:paused`` and
  waits. Any ``G`` line from the host in that state clears the saved recovery.
- ``M79`` answers ``//action:uvlo_[auto_]recovery_ready``; ``D3 Ax0f91 C4`` dumps
  the saved line; ``M602`` reheats, then prints ``Resend: <line+1>``, ``ok``,
  ``//action:resumed`` and the command's own ``ok``; ``M603`` stops.

``marlin`` is a generic printer with no power-panic support.

Tests steer it through files in the control directory: ``power_off`` present means
mains is off; ``fault`` and ``lcd_resume`` are one-shot triggers. Everything the
printer executes goes to ``executed.jsonl``, notable protocol events to
``events.jsonl``, and "EEPROM" lives in ``eeprom.json``.
This is a model built from reading the firmware source, not the firmware itself.
"""

import json
import os
import queue
import re
import threading
import time
from collections import deque

PORT = "FAKEPRINTER"
PLANNER_SIZE = 16

__plugin_name__ = "Fake printer (test support)"
__plugin_pythoncompat__ = ">=3.7,<4"

_LINE_RE = re.compile(r"^N(\d+)\s+(.*)\*(\d+)$")


def _checksum(text):
    cs = 0
    for c in text.encode():
        cs ^= c
    return cs


class Control:
    def __init__(self, directory):
        self.dir = directory

    def path(self, name):
        return os.path.join(self.dir, name)

    def config(self):
        with open(self.path("config.json")) as f:
            return json.load(f)

    @property
    def powered(self):
        return not os.path.exists(self.path("power_off"))

    def eeprom(self):
        try:
            with open(self.path("eeprom.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return {"pending": False, "line": 0}

    def save_eeprom(self, data):
        tmp = self.path("eeprom.json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path("eeprom.json"))

    def log(self, name, **record):
        record["t"] = time.time()
        with open(self.path(name), "a") as f:
            f.write(json.dumps(record) + "\n")


class FakePrinter:
    def __init__(self, control, baudrate, read_timeout):
        self.control = control
        self.cfg = control.config()
        self.prusa = self.cfg.get("firmware", "prusa") == "prusa"
        self.port = PORT
        self.baudrate = baudrate
        self.timeout = read_timeout

        self._out = queue.Queue()
        self._in = queue.Queue()
        self._planner = deque()
        self._planner_lock = threading.Condition()
        self._alive = True
        self._powered = False
        self._paused = False
        self._recovering = False
        self._recover_after_first_line = False
        self._last_n = 0
        self._completed = 0
        self._targets = {"T": 0.0, "B": 0.0}

        threading.Thread(target=self._power_watch, daemon=True).start()
        threading.Thread(target=self._process, daemon=True).start()
        threading.Thread(target=self._execute, daemon=True).start()

    # -- serial interface ------------------------------------------------

    def write(self, data):
        if self._powered:
            for line in data.decode("utf-8", "replace").splitlines():
                if line.strip():
                    self._in.put(line.strip())
        return len(data)

    def readline(self):
        try:
            return (self._out.get(timeout=self.timeout) + "\n").encode()
        except queue.Empty:
            return b""

    def close(self):
        self._alive = False

    def cancel_read(self):
        pass

    def cancel_write(self):
        pass

    def _say(self, *lines):
        for line in lines:
            self._out.put(line)

    # -- power -----------------------------------------------------------

    def _power_watch(self):
        while self._alive:
            powered = self.control.powered
            if powered and not self._powered:
                self._boot()
            elif not powered and self._powered:
                self._power_lost()
            if self._powered and self._take("fault"):
                # A firmware-detected fault, as on thermal runaway.
                with self._planner_lock:
                    self._planner.clear()
                self._say("Error:Thermal Runaway, system stopped! Heater_ID: 0")
                self._say("Error:Printer halted. kill() called!")
            if self._powered and self._take("lcd_resume"):
                # "Resume print" on the printer's screen during a host-print recovery.
                self._say("//action:resume")
            time.sleep(0.02)

    def _take(self, name):
        try:
            os.remove(self.control.path(name))
            return True
        except FileNotFoundError:
            return False

    def _boot(self):
        # The MCU is resetting: serial input is ignored until the bootloader is done.
        time.sleep(self.cfg.get("boot_time", 0.3))
        self._paused = False
        self._last_n = 0
        self._completed = 0
        with self._planner_lock:
            self._planner.clear()
        while not self._in.empty():
            self._in.get_nowait()
        eeprom = self.control.eeprom()
        recovery = bool(self.prusa and eeprom.get("pending"))
        self._recovering = False
        # Seen on the real printer: OctoPrint's connect handshake is answered at once;
        # right after that the recovery takes over the command queue, and host lines
        # sent from then on wait behind it. _process flips _recovering after the
        # first line so the switch can't race OctoPrint's next command.
        self._recover_after_first_line = recovery
        self._powered = True
        self._say("start")
        if recovery:
            while not self._recovering and self._powered:
                time.sleep(0.01)
            # Firmware restores Z, mesh, XY and temperatures from EEPROM, then parks.
            time.sleep(self.cfg.get("recovery_time", 1.0))
            self._targets = {"T": 0.0, "B": eeprom.get("bed", 0.0)}
            # restore_print_from_eeprom enqueues "M110 N<saved line>" (power_panic.cpp
            # :514), so from here on the firmware expects the host's next numbered line
            # to be saved+1 and rejects anything else. On a real MK3S+ (2026-10-14) this
            # is what swallowed OctoPrint's numbered M115 and the plugin's M79.
            self._last_n = eeprom["line"]
            self._paused = True
            self._recovering = False
            self.control.log("events.jsonl", event="boot_recovery", line=eeprom["line"])
            self._say("//action:paused")

    def _power_lost(self):
        self._powered = False
        with self._planner_lock:
            self._planner.clear()
            completed = self._completed
        # Whatever was still queued for the host is lost with the power...
        while not self._out.empty():
            self._out.get_nowait()
        if self.prusa and self.cfg.get("uvlo", True):
            self.control.save_eeprom(
                {"pending": True, "line": completed, "bed": self._targets["B"]}
            )
            self.control.log("events.jsonl", event="power_panic", line=completed)
            # ...but the firmware gets INT4 out on capacitor energy.
            self._say("INT4")

    # -- command handling --------------------------------------------------

    def _process(self):
        while self._alive:
            try:
                raw = self._in.get(timeout=0.1)
            except queue.Empty:
                continue
            if not self._powered:
                continue
            # The connect handshake (M110) is answered at once; the first command after
            # it (OctoPrint's M115) lands behind the boot recovery's own queued
            # commands, as on the real printer, and so sees the recovery's line number.
            if self._recover_after_first_line and "M110" not in raw:
                self._recover_after_first_line = False
                self._recovering = True
            while self._recovering and self._powered:
                time.sleep(0.02)
            try:
                self._handle(raw)
            except Exception as e:  # pragma: no cover - surfaced in octoprint.log
                self._say(f"Error:fake printer crashed on {raw!r}: {e}", "ok")

    def _handle(self, raw):
        n = None
        cmd = raw
        m = _LINE_RE.match(raw)
        if m:
            n, cmd, cs = int(m.group(1)), m.group(2), int(m.group(3))
            if _checksum(raw[: raw.rindex("*")]) != cs:
                self._say(f"Error:checksum mismatch, Last Line: {self._last_n}")
                self._say(f"Resend: {self._last_n + 1}", "ok")
                return
            if not cmd.startswith("M110") and n != self._last_n + 1:
                self._say(f"Error:Line Number is not Last Line Number+1, Last Line: {self._last_n}")
                self._say(f"Resend: {self._last_n + 1}", "ok")
                return
            self._last_n = n
        cmd = cmd.split(";", 1)[0].strip()
        code = cmd.split(" ", 1)[0].upper()

        if code.startswith("G") and self.prusa and self._paused:
            # cmdqueue.cpp: any G from the host clears the pending recovery.
            eeprom = self.control.eeprom()
            if eeprom.get("pending"):
                eeprom["pending"] = False
                self.control.save_eeprom(eeprom)
                self.control.log("events.jsonl", event="recovery_cleared_by_g", cmd=cmd)
            self.control.log("events.jsonl", event="g_while_paused", cmd=cmd)

        if code in ("G0", "G1", "G2", "G3", "G4", "G28", "G92") or re.match(r"^T\d+$", code):
            with self._planner_lock:
                while len(self._planner) >= PLANNER_SIZE and self._powered:
                    self._planner_lock.wait(0.05)
                if not self._powered:
                    return
                self._planner.append((n, cmd))
                self._planner_lock.notify_all()
            self._say("ok")
        elif code == "M110":
            value = re.search(r"N(\d+)", cmd[4:])
            self._last_n = int(value.group(1)) if value else 0
            self._say("ok")
        elif code == "M115":
            if self.prusa:
                self._say(
                    "FIRMWARE_NAME:Prusa-Firmware 3.14.1 based on Marlin "
                    "FIRMWARE_URL:https://github.com/prusa3d/Prusa-Firmware PROTOCOL_VERSION:1.0 "
                    "MACHINE_TYPE:Prusa i3 MK3S EXTRUDER_COUNT:1 "
                    "UUID:00000000-0000-0000-0000-000000000000"
                )
            else:
                self._say(
                    "FIRMWARE_NAME:Marlin 2.1.2.4 (Github) PROTOCOL_VERSION:1.0 "
                    "MACHINE_TYPE:Fake EXTRUDER_COUNT:1"
                )
            self._say("ok")
        elif code == "M105":
            self._say(self._temps("ok "))
        elif code in ("M104", "M109", "M140", "M190"):
            value = re.search(r"[SR](-?\d+\.?\d*)", cmd[4:])
            if value:
                self._targets["T" if code in ("M104", "M109") else "B"] = float(value.group(1))
            self._say("ok")
        elif code == "M79" and self.prusa:
            eeprom = self.control.eeprom()
            if eeprom.get("pending") and self._paused:
                auto = self.cfg.get("auto_recovery", True)
                self._say(
                    "//action:uvlo_auto_recovery_ready" if auto else "//action:uvlo_recovery_ready"
                )
            self._say("ok")
        elif code == "D3" and self.prusa:
            line = self.control.eeprom().get("line", 0)
            data = " ".join(f"{b:02x}" for b in int(line).to_bytes(4, "little"))
            self._say("D3 - Read/Write EEPROM", f"0f91  {data}", "ok")
        elif code == "M602" and self.prusa and self._paused:
            eeprom = self.control.eeprom()
            time.sleep(self.cfg.get("reheat_time", 0.5))
            self._targets["T"] = 215.0
            self._last_n = eeprom["line"]
            eeprom["pending"] = False
            self.control.save_eeprom(eeprom)
            self._paused = False
            self.control.log("events.jsonl", event="m602", line=eeprom["line"])
            self._say(f"Resend: {eeprom['line'] + 1}", "ok", "//action:resumed", "ok")
        elif code == "M603" and self.prusa:
            eeprom = self.control.eeprom()
            eeprom["pending"] = False
            self.control.save_eeprom(eeprom)
            self._paused = False
            self.control.log("events.jsonl", event="m603")
            self._say("ok")
        elif code in ("M79", "D3", "M602", "M603"):
            self._say(f'echo:Unknown command: "{cmd}"', "ok")
        else:
            self._say("ok")

    def _temps(self, prefix=""):
        t, b = self._targets["T"], self._targets["B"]
        return f"{prefix}T:{t:.1f} /{t:.1f} B:{b:.1f} /{b:.1f}"

    def _execute(self):
        line_time = self.cfg.get("line_time", 0.02)
        while self._alive:
            with self._planner_lock:
                while self._alive and (not self._planner or not self._powered):
                    self._planner_lock.wait(0.05)
                if not self._alive:
                    return
                n, cmd = self._planner[0]
            time.sleep(line_time)
            with self._planner_lock:
                if not self._powered or not self._planner or self._planner[0] != (n, cmd):
                    continue  # power was cut mid-move: the move never completed
                self._planner.popleft()
                if n is not None:
                    self._completed = n
                self.control.log("executed.jsonl", n=n, cmd=cmd)
                self._planner_lock.notify_all()


def _factory(comm_instance, port, baudrate, read_timeout, *args, **kwargs):
    if port != PORT:
        return None
    control = Control(os.environ["FAKE_PRINTER_DIR"])
    control.log("events.jsonl", event="connect")
    return FakePrinter(control, baudrate, read_timeout)


def _port_names(*args, **kwargs):
    return [PORT]


__plugin_hooks__ = {
    "octoprint.comm.transport.serial.factory": _factory,
    "octoprint.comm.transport.serial.additional_port_names": _port_names,
}
