"""The recovery state machine.

Pure logic: no OctoPrint imports, no threads, no I/O of its own. The plugin feeds it
events and hook calls, and executes the actions it leaves in ``take_actions()``.
Every method takes ``now`` so tests can drive time directly.

Safety rules it enforces:

- A recovery is armed only when a print was *lost* (host died, printer lost power,
  or the connection dropped), never after the printer reported a fault or a person
  cancelled.
- While Prusa firmware may be holding its own recovery, no G command reaches the
  printer (one would silently erase that recovery).
- Automatic resume needs every guard to pass; otherwise a person decides.
"""

from dataclasses import dataclass

from . import prusa
from .fallback import recovery_name
from .journal import INTERRUPTED, JournalError, lookup_line, tool_change_near

IDLE = "idle"
WAITING = "waiting_for_printer"
DETECTING = "detecting"
PROBING = "probing"
READING = "reading"
PENDING = "pending"
RESUMING = "resuming"
STARTING = "starting"
FAILED = "failed"

FIRMWARE = "firmware"
FALLBACK = "fallback"

PLUGIN_TAG = "plugin:power_panic"

# Error event reasons that mean the printer reported a fault itself. Anything that
# ends a print this way is never recovered.
FAULT_REASONS = {"firmware"}
LOST_REASONS = {"timeout", "connection"}


@dataclass
class Settings:
    auto_resume: bool = False
    auto_max_outage: float = 10 * 60
    max_age: float = 24 * 3600
    hold_heat: float = 60 * 60
    reconnect_window: float = 30 * 60
    reconnect_interval: float = 30
    settle_timeout: float = 25
    probe_timeout: float = 5
    probe_attempts: int = 3
    resume_timeout: float = 20 * 60
    start_timeout: float = 60
    keepalive_interval: float = 20
    toolchange_window: int = 16


class Controller:
    def __init__(self, journal, settings, file_identity, logger=None):
        self.journal = journal
        self._settings = settings
        self._file_identity = file_identity
        self._log = logger

        self.state = IDLE
        self.hold = False
        self.recovery = None
        self.connected = False

        self._actions = []
        self._firmware = None
        self._paused_seen = False
        self._last_error = None
        self._hint = None
        self._expecting_start = None
        self._deadline = None
        self._attempts = 0
        self._phase_started = None
        self._ready_kind = None
        self._waiting_since = None
        self._next_connect = None
        self._pending_since = None
        self._next_keepalive = None
        self._last_resync = None
        self._job_numbering_reset = False
        self._start_attempts = 0

    # -- plumbing --------------------------------------------------------

    @property
    def settings(self):
        return self._settings() if callable(self._settings) else self._settings

    def take_actions(self):
        actions, self._actions = self._actions, []
        return actions

    def _do(self, *action):
        self._actions.append(action)

    def _send(self, *commands):
        self._do("send", list(commands))

    def _notify(self, kind, **payload):
        self._do("notify", kind, payload)

    def _info(self, message, *args):
        if self._log:
            self._log.info(message, *args)

    def status(self):
        return {"state": self.state, "hold": self.hold, "recovery": self.recovery}

    @property
    def printer_reset_seen(self):
        """The printer reported a power panic or reset during the current print."""
        return self._hint is not None

    # -- startup and connection -----------------------------------------

    def startup(self, now):
        try:
            snap = self.journal.load()
        except JournalError as e:
            self._notify("failed", error=f"The power-panic journal could not be read: {e}")
            return
        if snap and snap["status"] == INTERRUPTED:
            self._info("Found an interrupted print: %s", snap["job"]["path"])
            self._enter_waiting(now)

    def on_connected(self, now):
        # Firmware info and //action:paused can arrive before this event (or before
        # the plugin started, when OctoPrint autoconnects first). They are cleared
        # on disconnect, not here, so they are never lost to that race.
        self.connected = True
        snap = self.journal.snapshot()
        if snap and snap["status"] == INTERRUPTED:
            self._begin_detection(now)

    def on_disconnected(self, now):
        self.connected = False
        self._firmware = None
        self._paused_seen = False
        if self.state in (DETECTING, PROBING, READING, PENDING, RESUMING, STARTING, FAILED):
            self.hold = False
            self._expecting_start = None
            self._enter_waiting(now)

    def on_firmware_info(self, name):
        version = prusa.firmware_version(name)
        self._firmware = version if version else "other"

    def on_printer_reset(self, now):
        snap = self.journal.snapshot()
        if self.connected and snap and snap["status"] == INTERRUPTED and self.state != DETECTING:
            self._begin_detection(now)

    def _enter_waiting(self, now):
        self.state = WAITING
        self.recovery = None
        self._waiting_since = now
        self._next_connect = now + self.settings.reconnect_interval

    def _begin_detection(self, now):
        snap = self.journal.snapshot()
        age = now - snap["updated"]
        if age > self.settings.max_age:
            self._info("Discarding interrupted print older than the maximum age (%ds)", age)
            self.journal.clear()
            self._reset_idle()
            self._notify("expired", path=snap["job"]["path"], age=age)
            return
        self.state = DETECTING
        self.recovery = None
        # Until the firmware is identified, assume it might be holding a recovery.
        self.hold = True
        self._phase_started = now
        self._ready_kind = None

    def _reset_idle(self):
        self.state = IDLE
        self.hold = False
        self.recovery = None
        self._expecting_start = None

    # -- print lifecycle --------------------------------------------------

    def on_print_started(self, path, origin, now):
        if self._expecting_start is not None and path == self._expecting_start:
            self._expecting_start = None
            self._reset_idle()
            self._start_journal(path, origin)
            self._notify("resumed", path=path)
            return

        if self.hold:
            # Something started a different print while the firmware may be holding
            # a recovery. The queuing gate keeps its G commands away from the
            # printer; cancel it so the person resolves the recovery first.
            self._do("cancel_print")
            self._notify(
                "blocked",
                reason="A power-panic recovery is pending. Resume or discard it before "
                "starting another print.",
            )
            return

        if self.journal.active and not self.journal.printing:
            self._info("New print started; dropping the pending recovery")
            self._reset_idle()
        self._start_journal(path, origin)

    def _start_journal(self, path, origin):
        self._last_error = None
        self._hint = None
        if origin != "local":
            # Printing from the printer's own SD card: its firmware handles power loss.
            self.journal.clear()
            return
        identity = self._file_identity(path)
        if identity is None:
            self.journal.clear()
            return
        self.journal.start(path, *identity)

    def on_print_done(self):
        if self.journal.printing:
            self.journal.clear()

    def on_print_cancelled(self, now):
        if not self.journal.printing:
            return
        if self._hint:
            # The printer reset (or reported a power panic) mid-print and OctoPrint
            # cancelled the job because the printer lost its state.
            self._interrupt(self._hint, now)
        else:
            self.journal.clear()

    def on_error(self, reason):
        self._last_error = reason

    def on_print_failed(self, now):
        if not self.journal.printing:
            return
        if self._last_error in FAULT_REASONS and not self._hint:
            self.journal.clear()
            self._notify(
                "not_recoverable",
                reason="The printer reported a fault, so the print will not be recovered.",
            )
            return
        if self._hint:
            kind = self._hint
        elif self._last_error in LOST_REASONS or self._last_error is None:
            kind = self._last_error or "connection_lost"
        else:
            kind = "protocol_error"
        self._interrupt(kind, now)

    def _interrupt(self, kind, now):
        self.journal.interrupt(kind)
        self._info("Print interrupted (%s); recovery armed", kind)
        if self.connected:
            self._begin_detection(now)
        else:
            self._enter_waiting(now)

    # -- serial traffic ------------------------------------------------

    def on_received(self, line, now):
        text = line.strip()
        if self.journal.printing:
            if text == prusa.POWER_PANIC_LINE:
                self._hint = "power_panic"
            elif text == prusa.RESET_LINE:
                self._hint = self._hint or "printer_reset"
        if self.state == READING:
            number = prusa.parse_line_number(text)
            if number is not None:
                self._evaluate_firmware(number, now)

    def on_action(self, name, now):
        if name == prusa.ACTION_PAUSED:
            self._paused_seen = True
        elif name in (prusa.ACTION_RECOVERY_READY, prusa.ACTION_AUTO_RECOVERY_READY):
            if self.state == PROBING:
                self._ready_kind = "auto" if name == prusa.ACTION_AUTO_RECOVERY_READY else "manual"
                self.state = READING
                self._attempts = 1
                self._deadline = now + self.settings.probe_timeout
                self._send(prusa.READ_LINE_COMMAND)
        elif name == prusa.ACTION_RESUMED:
            if self.state == RESUMING:
                self.hold = False
                self.state = STARTING
                self._job_numbering_reset = False
                self._start_attempts = 1
                self._expecting_start = self.recovery["path"]
                self._deadline = now + self.settings.start_timeout
                fan = ((self.journal.snapshot() or {}).get("state") or {}).get("fan")
                if fan:
                    # The firmware restores the fan from its own saved copy, but
                    # nothing confirms that; re-send the last fan command the file
                    # gave so the resumed layers are cooled as sliced.
                    self._send(fan)
                self._do("start_job", self.recovery["path"], self.recovery["pos"])
        elif name == prusa.ACTION_RESUME:
            if self.state == PENDING and self.recovery["mode"] == FIRMWARE:
                self.resume(now, by="printer")
        elif name == prusa.ACTION_CANCEL:
            if self.state in (DETECTING, PROBING, READING, PENDING, FAILED):
                # "Stop print" on the printer: its firmware has already given up.
                self.journal.clear()
                self._reset_idle()
                self._notify("discarded", by="printer")

    def on_resend_request(self, now):
        """The printer asked the host to resend a line. Returns True to neutralise it.

        The firmware's boot recovery runs ``M110 N<saved line>``, so after it the
        printer rejects every numbered line OctoPrint sends (OctoPrint numbers
        everything until the firmware identifies itself), including the plugin's
        M115 and M79. OctoPrint can't satisfy those resends and stalls. While a
        recovery is being detected or awaits a decision, the plugin answers with
        ``M110 N0`` -- accepted at any line number, and it touches neither the saved
        line in EEPROM nor the copy M602 restores from -- then probes again.
        """
        if self.journal.printing:
            return False
        if self.state == RESUMING or (self.state == STARTING and not self._job_numbering_reset):
            # M602 always ends with "Resend: <line+1>", and a numbered command sent
            # after it (before the job starts) is rejected too. OctoPrint would resend
            # the M602 or stall; the job's own M110 N0 resyncs everything, so until
            # that has gone out these requests are moot. (Real-printer blip gap.)
            return True
        if self.state not in (DETECTING, PROBING, READING, PENDING, FAILED):
            return False
        if self._last_resync is not None and now - self._last_resync < 1:
            return True
        self._last_resync = now
        self._info("Printer rejected a numbered line; resyncing line numbers with M110 N0")
        self._send("M110 N0")
        # The printer rebooted under us: have it identify itself again, which is also
        # what makes OctoPrint stop numbering idle commands.
        self._firmware = None
        if self.state in (PROBING, READING):
            # The probe or the read was among the rejected lines: start over.
            self.state = DETECTING
            self._paused_seen = True
            self._phase_started = now
        return True

    def on_job_numbering_reset(self):
        """The resumed job's own M110 N0 went out; resends are OctoPrint's again."""
        if self.state == STARTING:
            self._job_numbering_reset = True

    def should_block(self, command, tags):
        """True if a queued command must not reach the printer right now."""
        if not self.hold or PLUGIN_TAG in (tags or ()):
            return False
        return command.strip().upper().startswith("G")

    # -- time ------------------------------------------------------------

    def tick(self, now):
        s = self.settings
        if self.state == WAITING:
            if (
                not self.connected
                and now - self._waiting_since <= s.reconnect_window
                and now >= self._next_connect
            ):
                self._next_connect = now + s.reconnect_interval
                self._do("connect")

        elif self.state == DETECTING:
            fw = self._firmware
            if fw == "other" or (isinstance(fw, tuple) and not prusa.supports_host_recovery(fw)):
                self.hold = False
                self._offer_fallback(now, "the printer's firmware has no host power-panic support")
            elif self._paused_seen or now - self._phase_started >= s.settle_timeout:
                # Probe even when the firmware hasn't identified itself: after a power
                # panic the connect reset replays the recovery and a real MK3S left
                # OctoPrint's M115 unanswered. M79 is harmless on other firmware.
                self.state = PROBING
                self._attempts = 1
                self._deadline = now + s.probe_timeout
                if fw is None:
                    self._send("M115", prusa.PROBE_COMMAND)
                else:
                    self._send(prusa.PROBE_COMMAND)

        elif self.state == PROBING and now >= self._deadline:
            if self._attempts < s.probe_attempts:
                self._attempts += 1
                self._deadline = now + s.probe_timeout
                self._send(prusa.PROBE_COMMAND)
            else:
                self.hold = False
                self._offer_fallback(now, "the printer has no recovery of its own pending")

        elif self.state == READING and now >= self._deadline:
            if self._attempts < s.probe_attempts:
                self._attempts += 1
                self._deadline = now + s.probe_timeout
                self._send(prusa.READ_LINE_COMMAND)
            else:
                self._fail("Could not read the saved line number from the printer.", firmware=True)

        elif self.state == PENDING:
            if (
                self.recovery["mode"] == FIRMWARE
                and now - self._pending_since <= s.hold_heat
                and now >= self._next_keepalive
            ):
                # Keeps the printer's own "Resume print" menu item available, and
                # resets PSU Control's idle timer so the bed stays hot meanwhile.
                self._next_keepalive = now + s.keepalive_interval
                self._send(prusa.PROBE_COMMAND)

        elif self.state == RESUMING and now >= self._deadline:
            self._fail("The printer did not confirm the resume.", firmware=True)

        elif self.state == STARTING and now >= self._deadline:
            if self._start_attempts < 2 and self.recovery["mode"] == FIRMWARE:
                # The printer is homed, levelled, heated and in position after M602;
                # streaming the file is all that is left, so try that again first.
                self._start_attempts += 1
                self._deadline = now + s.start_timeout
                self._info("Resumed job did not start; trying again")
                self._do("start_job", self.recovery["path"], self.recovery["pos"])
            else:
                self._expecting_start = None
                if self.recovery["mode"] == FIRMWARE:
                    # M602 left a hot nozzle resting on the part at print height.
                    # Retract, lift clear and cool it so residual heat can't melt the
                    # part; the bed stays on so the part stays stuck down.
                    self._send("G91", "G1 E-2 F2400", "G1 Z10 F600", "G90", "M104 S0")
                self._fail(
                    "The resumed print did not start. The nozzle was lifted clear of the "
                    "part and its heater turned off.",
                    firmware=False,
                )

    # -- decisions -----------------------------------------------------

    def _check_file(self, snap):
        job = snap["job"]
        identity = self._file_identity(job["path"])
        if identity is None:
            return f"The file {job['path']} no longer exists."
        if tuple(identity) != (job["size"], job["mtime_ns"]):
            return f"The file {job['path']} has changed since it was printing."
        return None

    def _evaluate_firmware(self, line, now):
        s = self.settings
        snap = self.journal.snapshot()
        problem = self._check_file(snap)
        if problem:
            self._fail(problem, firmware=True)
            return
        try:
            found = lookup_line(snap, line)
        except JournalError as e:
            self._fail(f"Cannot find where to resume: {e}.", firmware=True)
            return

        outage = now - snap["updated"]
        reasons = []
        if not s.auto_resume:
            reasons.append("Automatic resume is turned off.")
        if self._ready_kind != "auto":
            reasons.append("The printer reports the bed cooled down while it was off.")
        if outage > s.auto_max_outage:
            reasons.append(
                f"The printer was off for {outage / 60:.0f} min, longer than the "
                f"{s.auto_max_outage / 60:.0f} min allowed for automatic resume."
            )
        if found.beyond:
            reasons.append(
                f"The last {found.beyond} lines the printer completed were not yet "
                "journaled and may be printed twice."
            )
        if tool_change_near(snap, line, s.toolchange_window):
            reasons.append("A tool change may have been in progress.")

        self.recovery = {
            "mode": FIRMWARE,
            "path": snap["job"]["path"],
            "line": line,
            "pos": found.pos,
            "size": snap["job"]["size"],
            "outage": outage,
            "interruption": snap.get("interruption"),
            "reasons": reasons,
        }
        if reasons:
            self._pending(now)
        else:
            self._info("All automatic-resume guards passed; resuming")
            self._resume_firmware(now)

    def _offer_fallback(self, now, why):
        snap = self.journal.snapshot()
        problem = self._check_file(snap)
        lines = snap.get("lines") or []
        if problem or not lines:
            self._fail(problem or "Nothing was printed from the file yet.", firmware=False)
            return
        self.recovery = {
            "mode": FALLBACK,
            "path": snap["job"]["path"],
            "line": None,
            "pos": lines[-1][1],
            "size": snap["job"]["size"],
            "outage": now - snap["updated"],
            "interruption": snap.get("interruption"),
            "reasons": [
                f"Using the generic recovery because {why}.",
                "Generic recovery is always started by a person.",
            ],
        }
        self._pending(now)

    def _pending(self, now):
        self.state = PENDING
        self._pending_since = now
        self._next_keepalive = now
        self._notify("pending", **self.recovery)

    def _fail(self, error, firmware):
        self.state = FAILED
        snap = self.journal.snapshot()
        self.recovery = {
            "mode": FIRMWARE if firmware else FALLBACK,
            "path": snap["job"]["path"] if snap else None,
            "error": error,
        }
        # With firmware recovery possibly pending, keep G commands held back until a
        # person discards it.
        self.hold = firmware
        self._notify("failed", error=error, path=self.recovery["path"])

    # -- people --------------------------------------------------------

    def resume(self, now, by="user"):
        if self.state != PENDING:
            return False
        self._info("Resuming recovery (requested by %s)", by)
        if self.recovery["mode"] == FIRMWARE:
            self._resume_firmware(now)
        else:
            snap = self.journal.snapshot()
            target = recovery_name(self.recovery["path"])
            self.state = STARTING
            self._expecting_start = target
            self._deadline = now + self.settings.start_timeout
            self._do(
                "start_fallback", self.recovery["path"], self.recovery["pos"], target, snap["state"]
            )
        return True

    def _resume_firmware(self, now):
        # M110 first: the firmware answers M602 with "Resend: <line+1>". With
        # OctoPrint's counter already at line+1 that request is a known no-op;
        # otherwise OctoPrint treats it as a protocol error and disconnects.
        # M115 first: once the firmware has identified itself OctoPrint stops
        # numbering idle commands, so the M602 goes out unnumbered and can't be
        # resent after the firmware rewinds its line counter.
        self.state = RESUMING
        self._deadline = now + self.settings.resume_timeout
        self._send("M115", f"M110 N{self.recovery['line']}", prusa.RESUME_COMMAND)
        self._notify("resuming", **self.recovery)

    def discard(self, now, by="user"):
        if not self.journal.active and self.state == IDLE:
            return False
        # A recovery-ready reply proves Prusa host recovery even if M115 never answered.
        prusa_host_recovery = self._ready_kind is not None or (
            isinstance(self._firmware, tuple) and prusa.supports_host_recovery(self._firmware)
        )
        firmware_pending = self.state in (PROBING, READING) or (
            (self.recovery or {}).get("mode") == FIRMWARE and self.state in (PENDING, FAILED)
        )
        if prusa_host_recovery and firmware_pending and self.connected:
            # Same as "Stop print" on the printer's screen.
            self._send(prusa.STOP_COMMAND)
        self.journal.clear()
        self._reset_idle()
        self._notify("discarded", by=by)
        return True
