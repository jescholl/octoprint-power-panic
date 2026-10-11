"""End-to-end rig: a real OctoPrint, this plugin, and a simulated printer.

The printer is ``tests/support/fake_printer.py``, installed as a single-file plugin
that supplies the serial port. Tests cut its power through a control directory and
kill OctoPrint with SIGKILL to model the host losing power at the same moment.
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

import octoprint
import pytest
import requests
import yaml

API_KEY = "powerpanictestkey0123456789abcdef"
PORT = "FAKEPRINTER"
HERE = os.path.dirname(os.path.abspath(__file__))
SUPPORT = os.path.join(os.path.dirname(HERE), "support", "fake_printer.py")
OCTOPRINT_MAJOR = int(octoprint.__version__.split(".")[0])

DISABLED_PLUGINS = [
    "announcements",
    "errortracking",
    "health_check",
    "pluginmanager",
    "softwareupdate",
    "tracking",
    "discovery",
    "achievements",
    "virtual_printer",
]

# Fast comm timeouts so a silent printer is declared dead in seconds, not minutes.
SERIAL = {
    "timeout": {"communication": 1.0, "communicationBusy": 1.0, "connection": 3.0},
    "maxCommunicationTimeouts": {"idle": 2, "printing": 2, "long": 2},
}


def body_line(i):
    # F is unique per line, so every executed line identifies itself.
    return f"G1 X{10 + i % 150} Y{10 + (i // 150) % 150} E0.02 F{1000 + i}"


def make_gcode(lines, tool_changes=()):
    out = ["G90", "M83", "M140 S60", "M104 S215", "G28", "G1 Z0.2 F600"]
    for i in range(lines):
        if i in tool_changes:
            out.append(f"T{tool_changes[i]}")
        out.append(body_line(i))
    out.append("M84")
    return ("\n".join(out) + "\n").encode()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Rig:
    def __init__(self, tmp_path, plugin_settings, printer):
        self.basedir = tmp_path / "octoprint"
        self.basedir.mkdir()
        self.control = tmp_path / "printer"
        self.control.mkdir()
        (self.control / "config.json").write_text(json.dumps(printer))
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = None
        self.runs = 0
        self._write_config(plugin_settings)

    def _write_config(self, plugin_settings):
        cfg = {
            "api": {"key": API_KEY},
            "server": {
                "host": "127.0.0.1",
                "port": self.port,
                "firstRun": False,
                "onlineCheck": {"enabled": False},
                "pluginBlacklist": {"enabled": False},
            },
            "feature": {"sdSupport": False},
            "webcam": {"webcamEnabled": False, "timelapseEnabled": False},
            "plugins": {
                "_disabled": DISABLED_PLUGINS,
                "power_panic": plugin_settings,
                "serial_connector": SERIAL,
            },
        }
        if OCTOPRINT_MAJOR >= 2:
            cfg["printerConnection"] = {
                "autoconnect": True,
                "preferred": {
                    "connector": "serial",
                    "parameters": {"port": PORT, "baudrate": 115200},
                },
            }
        else:
            cfg["serial"] = dict(SERIAL, port=PORT, baudrate=115200, autoconnect=True)
        with open(self.basedir / "config.yaml", "w") as f:
            yaml.safe_dump(cfg, f)

    # -- process ---------------------------------------------------------

    def start(self):
        plugins_dir = self.basedir / "plugins"
        plugins_dir.mkdir(exist_ok=True)
        shutil.copy(SUPPORT, plugins_dir / "fake_printer.py")
        self.runs += 1
        env = dict(os.environ, PYTHONUNBUFFERED="1", FAKE_PRINTER_DIR=str(self.control))
        self.stdout = open(self.basedir / f"stdout-{self.runs}.log", "w")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "octoprint",
                "--basedir",
                str(self.basedir),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            stdout=self.stdout,
            stderr=subprocess.STDOUT,
            env=env,
            # `python -m` puts the cwd on sys.path; never let a checkout shadow the
            # installed plugin under test.
            cwd=str(self.basedir),
        )
        assert self.wait_for(self._answers, 90), "OctoPrint never came up:\n" + self.diagnostics()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        if getattr(self, "stdout", None):
            self.stdout.close()

    def kill(self):
        """The host loses power: no shutdown handlers, no final events."""
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(10)
        self.stdout.close()

    # -- printer power ---------------------------------------------------

    def power_off(self):
        (self.control / "power_off").touch()
        assert self.wait_for(lambda: self.event_seen("power_panic") or not self.prusa, 5)

    def power_on(self):
        os.remove(self.control / "power_off")

    @property
    def prusa(self):
        return (
            json.loads((self.control / "config.json").read_text()).get("firmware", "prusa")
            == "prusa"
        )

    def _records(self, name):
        try:
            with open(self.control / name) as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def events(self, name=None):
        return [e for e in self._records("events.jsonl") if name is None or e["event"] == name]

    def event_seen(self, name):
        return bool(self.events(name))

    def executed_body(self):
        """Indices of the generated body lines the printer completed, in order."""
        out = []
        for rec in self._records("executed.jsonl"):
            m = re.search(r"\bE0\.02 F(\d+)", rec["cmd"])
            if m:
                out.append(int(m.group(1)) - 1000)
        return out

    # -- API ---------------------------------------------------------------

    def api(self, method, path, **kw):
        headers = kw.pop("headers", {})
        headers["X-Api-Key"] = API_KEY
        return requests.request(method, self.base + path, headers=headers, timeout=60, **kw)

    def _answers(self):
        try:
            return self.api("GET", "/api/plugin/power_panic").status_code == 200
        except requests.RequestException:
            return False

    def status(self):
        return self.api("GET", "/api/plugin/power_panic").json()

    def command(self, command):
        return self.api("POST", "/api/plugin/power_panic", json={"command": command})

    def printer_state(self):
        return self.api("GET", "/api/connection").json()["current"]["state"]

    def job(self):
        return self.api("GET", "/api/job").json()

    def upload_and_print(self, name, data):
        r = self.api(
            "POST", "/api/files/local", files={"file": (name, data)}, data={"print": "true"}
        )
        assert r.status_code == 201, r.text
        assert self.wait_for(lambda: self.job()["state"].startswith("Printing"), 30), (
            self.diagnostics()
        )

    def wait_for(self, predicate, timeout=60, interval=0.25):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if predicate():
                    return True
            except (requests.RequestException, ValueError, KeyError):
                pass
            time.sleep(interval)
        return False

    def wait_operational(self, timeout=60):
        assert self.wait_for(lambda: self.printer_state() == "Operational", timeout), (
            self.diagnostics()
        )

    def wait_state(self, state, timeout=60):
        assert self.wait_for(lambda: self.status()["state"] == state, timeout), (
            f"plugin state {self._safe_status()}, wanted {state}\n" + self.diagnostics()
        )

    def wait_printed(self, count, timeout=60):
        assert self.wait_for(lambda: len(self.executed_body()) >= count, timeout), (
            self.diagnostics()
        )

    def _safe_status(self):
        try:
            return self.status()
        except Exception as e:
            return repr(e)

    def log_text(self):
        try:
            return (self.basedir / "logs" / "octoprint.log").read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def diagnostics(self):
        lines = [x for x in self.log_text().splitlines() if "power_panic" in x or "Error" in x]
        return (
            "---- octoprint.log (plugin/error lines) ----\n"
            + "\n".join(lines[-60:])
            + f"\n---- printer events ----\n{self.events()}"
        )


@pytest.fixture
def make_rig(tmp_path, request):
    rigs = []

    def factory(printer=None, **plugin_settings):
        printer = dict({"firmware": "prusa", "line_time": 0.02}, **(printer or {}))
        rig = Rig(tmp_path, plugin_settings, printer)
        rigs.append(rig)
        rig.start()
        return rig

    yield factory
    for rig in rigs:
        rig.stop()
    keep = os.environ.get("POWER_PANIC_TEST_LOGS")
    if keep and rigs:
        os.makedirs(keep, exist_ok=True)
        src = rigs[-1].basedir / "logs" / "octoprint.log"
        if src.exists():
            shutil.copy(src, os.path.join(keep, request.node.name + ".log"))
