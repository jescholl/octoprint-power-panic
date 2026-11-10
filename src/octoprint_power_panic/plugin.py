"""OctoPrint adapter: wires hooks and events into the controller and runs its actions."""

import os
import tempfile
import threading
import time

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions
from octoprint.events import Events, eventManager
from octoprint.filemanager.util import DiskFileWrapper

from . import fallback
from .controller import PLUGIN_TAG, Controller, Settings
from .journal import Journal
from .tracker import StreamTracker

OCTOPRINT_MAJOR = int(octoprint.__version__.split(".")[0])
TICK = 0.2
# How often the power plugin is asked; its helper may do I/O.
POWER_POLL = 5.0
# PSU Control's public helper. Power plugins that plug into PSU Control (Tasmota,
# TP-Link, Tuya, ...) report through it too, and any other plugin can offer the same.
DEFAULT_POWER_HELPER = "get_psu_state"

CUSTOM_EVENTS = [
    "recovery_pending",
    "recovery_resuming",
    "recovery_resumed",
    "recovery_discarded",
    "recovery_failed",
    "recovery_blocked",
    "recovery_not_recoverable",
    "recovery_expired",
]


class PowerPanicPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.SimpleApiPlugin,
):
    def initialize(self):
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._running = True
        self._tracker = StreamTracker()
        self._journal = Journal(os.path.join(self.get_plugin_data_folder(), "journal.json"))
        self._controller = Controller(
            self._journal, self._controller_settings, self._file_identity, logger=self._logger
        )
        self._warned_line_counter = False
        self._power_source = None  # last logged (plugin, helper), or "none"
        self._next_power_poll = 0.0

    # -- settings --------------------------------------------------------

    def get_settings_defaults(self):
        return {
            "auto_resume": False,
            "auto_max_outage_minutes": 10,
            "max_age_hours": 24,
            "hold_heat_minutes": 60,
            # Waits between reconnect attempts double up to this maximum.
            "reconnect_max_interval_minutes": 60,
            # Not in the settings UI; tests shorten it.
            "reconnect_interval_seconds": 30,
            # "<plugin identifier>:<helper>" returning True/False for printer power.
            # Empty: any enabled plugin offering a get_psu_state helper (PSU Control).
            "power_state_helper": "",
            "fallback_z_lift": 5,
            "fallback_home_command": "G28 X Y",
            "fallback_xy_feedrate": 3000,
            "fallback_extra_gcode": "",
        }

    def _controller_settings(self):
        s = self._settings
        return Settings(
            auto_resume=s.get_boolean(["auto_resume"]),
            auto_max_outage=s.get_float(["auto_max_outage_minutes"]) * 60,
            max_age=s.get_float(["max_age_hours"]) * 3600,
            hold_heat=s.get_float(["hold_heat_minutes"]) * 60,
            reconnect_interval=s.get_float(["reconnect_interval_seconds"]),
            reconnect_max_interval=s.get_float(["reconnect_max_interval_minutes"]) * 60,
        )

    # -- printer power ---------------------------------------------------

    def _power_helper(self):
        """The (plugin, helper name, callable) to ask for printer power, or None."""
        configured = (self._settings.get(["power_state_helper"]) or "").strip()
        if configured:
            plugin, _, name = configured.partition(":")
            candidates = [(plugin.strip(), name.strip() or DEFAULT_POWER_HELPER)]
        else:
            # PSU Control first if present, then any other plugin offering the helper.
            plugins = sorted(
                self._plugin_manager.enabled_plugins, key=lambda k: (k != "psucontrol", k)
            )
            candidates = [(p, DEFAULT_POWER_HELPER) for p in plugins if p != self._identifier]
        for plugin, name in candidates:
            helpers = self._plugin_manager.get_helpers(plugin, name)
            if helpers and callable(helpers.get(name)):
                return plugin, name, helpers[name]
        return None

    def _read_power(self):
        """True or False as the power plugin reports it; None if nothing can tell."""
        found = self._power_helper()
        source = found[:2] if found else "none"
        if source != self._power_source:
            self._power_source = source
            if found:
                self._logger.info("Reading printer power from %s.%s", *source)
            else:
                self._logger.info("No power plugin found; reconnects back off on their own")
        if not found:
            return None
        try:
            value = found[2]()
        except Exception:
            self._logger.exception("Reading printer power from %s.%s failed", *source)
            return None
        # PSU Control's GPIO sensing reports 0/1 rather than a bool.
        if isinstance(value, (bool, int)) and value in (0, 1):
            return bool(value)
        return None

    def get_template_configs(self):
        return [
            {"type": "settings", "custom_bindings": False},
            {
                "type": "sidebar",
                "name": "Power Panic",
                "custom_bindings": True,
                "icon": "bolt",
                # Only shown while there is something to recover.
                "data_bind": "visible: visible",
            },
        ]

    def is_template_autoescaped(self):
        return True

    def get_assets(self):
        return {"js": ["js/power_panic.js"]}

    # -- lifecycle -------------------------------------------------------

    def on_after_startup(self):
        with self._lock:
            self._controller.startup(time.time())
            if self._printer.is_operational():
                self._controller.on_connected(time.time())
        threading.Thread(target=self._work, name="power-panic", daemon=True).start()

    def on_shutdown(self):
        self._running = False
        self._wake.set()

    def _work(self):
        while self._running:
            self._wake.wait(TICK)
            self._wake.clear()
            try:
                # Only reconnect decisions use it, so only ask while waiting for the printer.
                waiting = self._controller.state == "waiting_for_printer"
                if waiting and time.monotonic() >= self._next_power_poll:
                    self._next_power_poll = time.monotonic() + POWER_POLL
                    power = self._read_power()  # outside the lock: it calls another plugin
                    with self._lock:
                        self._controller.on_power_state(power, time.time())
                with self._lock:
                    self._controller.tick(time.time())
                    actions = self._controller.take_actions()
                self._journal.flush()
                for action in actions:
                    self._run(action)
            except Exception:
                self._logger.exception("Power panic worker error")

    def _run(self, action):
        kind, args = action[0], action[1:]
        tags = {PLUGIN_TAG, "source:plugin"}
        if kind == "send":
            self._logger.info("Sending %s", ", ".join(args[0]))
            self._printer.commands(args[0], tags=tags)
        elif kind == "start_job":
            path, pos = args
            self._wait_until_ready()
            self._logger.info("Resuming %s from byte %d", path, pos)
            self._printer.select_file(path, False, printAfterSelect=True, pos=pos, tags=tags)
        elif kind == "start_fallback":
            self._start_fallback(*args, tags=tags)
        elif kind == "cancel_print":
            self._printer.cancel_print(tags=tags)
        elif kind == "connect":
            self._connect()
        elif kind == "notify":
            self._publish(*args)

    def _connect(self):
        self._logger.info("Reconnecting to the printer to check for an interrupted print")
        if OCTOPRINT_MAJOR >= 2:
            self._printer.connect()
        else:
            port = self._settings.global_get(["serial", "port"])
            baudrate = self._settings.global_get_int(["serial", "baudrate"])
            self._printer.connect(port=port, baudrate=baudrate)

    def _start_fallback(self, path, pos, target, state, tags):
        s = self._settings
        header = fallback.build_header(
            state,
            source_name=path,
            pos=pos,
            z_lift=s.get_float(["fallback_z_lift"]),
            home_command=s.get(["fallback_home_command"]),
            xy_feedrate=s.get_float(["fallback_xy_feedrate"]),
            extra_gcode=s.get(["fallback_extra_gcode"]),
        )
        source = self._file_manager.path_on_disk("local", path)
        fd, tmp = tempfile.mkstemp(suffix=".gcode", dir=self.get_plugin_data_folder())
        os.close(fd)
        fallback.write_recovery_file(source, pos, header, tmp)
        self._file_manager.add_file(
            "local", target, DiskFileWrapper(os.path.basename(target), tmp), allow_overwrite=True
        )
        self._logger.info("Starting generic recovery file %s", target)
        self._wait_until_ready()
        self._printer.select_file(target, False, printAfterSelect=True, tags=tags)

    def _wait_until_ready(self, timeout=30):
        """Wait for OctoPrint to be operational and idle before starting a job.

        OctoPrint silently ignores a start while it is still busy, e.g. finishing
        the cancel of the job a printer reset interrupted.
        """
        end = time.monotonic() + timeout
        while not self._printer.is_ready() and time.monotonic() < end:
            time.sleep(0.2)

    def _publish(self, kind, payload):
        status = self._controller.status()
        self._plugin_manager.send_plugin_message(
            self._identifier, {"type": kind, "payload": payload, "status": status}
        )
        eventManager().fire(f"plugin_{self._identifier}_recovery_{kind}", payload)
        if kind in ("failed", "blocked", "not_recoverable"):
            self._logger.warning("%s: %s", kind, payload)
        else:
            self._logger.info("%s: %s", kind, payload)

    def _file_identity(self, path):
        try:
            st = os.stat(self._file_manager.path_on_disk("local", path))
        except Exception:
            return None
        return st.st_size, st.st_mtime_ns

    # -- events ----------------------------------------------------------

    def on_event(self, event, payload):
        now = time.time()
        payload = payload or {}
        with self._lock:
            c = self._controller
            if event == Events.CONNECTED:
                c.on_connected(now)
            elif event == Events.DISCONNECTED:
                c.on_disconnected(now)
            elif event == Events.PRINT_STARTED:
                self._tracker.reset()
                c.on_print_started(payload.get("path"), payload.get("origin"), now)
            elif event == Events.PRINT_DONE:
                c.on_print_done()
            elif event == Events.PRINT_CANCELLED:
                c.on_print_cancelled(now)
            elif event == Events.PRINT_FAILED:
                c.on_print_failed(now)
            elif event == Events.ERROR:
                c.on_error(payload.get("reason"))
            elif event == Events.PRINTER_RESET:
                c.on_printer_reset(now)
            else:
                return
        self._wake.set()

    # -- comm hooks ------------------------------------------------------

    def hook_gcode_queuing(
        self, comm, phase, cmd, cmd_type, gcode, subcode=None, tags=None, *args, **kwargs
    ):
        with self._lock:
            block = self._controller.should_block(cmd, tags)
        if block:
            self._logger.warning("Holding back %r: a power-panic recovery is pending", cmd)
            return (None,)
        return None

    def hook_gcode_sent(
        self, comm, phase, cmd, cmd_type, gcode, subcode=None, tags=None, *args, **kwargs
    ):
        tags = tags or set()
        if gcode == "M110" and "trigger:comm.start_print" in tags:
            # The resumed job's own line-number reset: from here on, resend requests
            # are real and OctoPrint must handle them.
            with self._lock:
                self._controller.on_job_numbering_reset()
        if not self._journal.printing:
            return
        if gcode == "M110":
            # A renumbering mid-print invalidates the line history, except when it
            # is OctoPrint resyncing with a printer that just reset: that history is
            # exactly what the recovery needs.
            with self._lock:
                reset = self._controller.printer_reset_seen
            if not reset:
                self._journal.reset_numbering()
            return

        changes = self._tracker.feed(cmd)
        tool = changes.pop("tool_command", None)
        if changes:
            self._journal.update_state(**changes)

        if "source:file" not in tags:
            return
        pos = next((int(t[8:]) for t in tags if t.startswith("filepos:")), None)
        # OctoPrint has numbered and sent this line; its counter already points at
        # the next one. Private attribute, covered by the integration tests on every
        # supported OctoPrint version.
        counter = getattr(comm, "_current_line", None)
        if pos is None or counter is None:
            if not self._warned_line_counter:
                self._warned_line_counter = True
                self._logger.error("Cannot read OctoPrint's line counter; firmware resume disabled")
            return
        line = counter - 1
        if tool:
            self._journal.record_tool(line, tool)
        self._journal.record_line(line, pos)

    def hook_gcode_received(self, comm, line, *args, **kwargs):
        stripped = line.strip()
        if stripped.startswith("Resend:"):
            with self._lock:
                neutralise = self._controller.on_resend_request(time.time())
            current = getattr(comm, "_current_line", None)
            if neutralise and current is not None:
                self._wake.set()
                # OctoPrint ignores a resend of exactly the line it is about to send,
                # instead of stalling on one it has no history for.
                return f"Resend: {current}\n"
            return line
        # Cheap pre-filter: this hook sees every line the printer sends.
        if stripped in ("INT4", "start") or self._controller.state == "reading":
            with self._lock:
                self._controller.on_received(line, time.time())
            self._wake.set()
        return line

    def hook_action(self, comm, line, action, name=None, params=None, *args, **kwargs):
        with self._lock:
            self._controller.on_action(name or action, time.time())
        self._wake.set()

    def hook_firmware_info(self, comm, firmware_name, firmware_data, *args, **kwargs):
        with self._lock:
            self._controller.on_firmware_info(firmware_name)
        self._wake.set()

    # -- API ---------------------------------------------------------------

    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return {"resume": [], "discard": []}

    def on_api_get(self, request):
        if not Permissions.STATUS.can():
            flask.abort(403)
        with self._lock:
            return flask.jsonify(self._controller.status())

    def on_api_command(self, command, data):
        if not Permissions.PRINT.can():
            flask.abort(403)
        with self._lock:
            if command == "resume":
                ok = self._controller.resume(time.time())
            else:
                ok = self._controller.discard(time.time())
            status = self._controller.status()
        self._wake.set()
        if not ok:
            return flask.make_response(flask.jsonify(status), 409)
        return flask.jsonify(status)

    # -- helpers for other plugins -------------------------------------------

    def print_recovery_pending(self):
        """True while an interrupted print may still be resumed (any state but idle).

        For plugins about to do something that would disturb it, e.g. restart OctoPrint:
        the reconnect after a restart resets the printer, which a pending Prusa recovery
        doesn't survive cleanly. A print that is simply running is not "pending"; ask
        OctoPrint's printer state for that.
        """
        with self._lock:
            return self._controller.state != "idle"

    # -- software update ---------------------------------------------------

    def get_update_information(self):
        return {
            "power_panic": {
                "displayName": "Power Panic",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "jescholl",
                "repo": "octoprint-power-panic",
                "current": self._plugin_version,
                "pip": "https://github.com/jescholl/octoprint-power-panic/archive/{target_version}.zip",
            }
        }


def register_custom_events(*args, **kwargs):
    return CUSTOM_EVENTS


__plugin_implementation__ = _impl = PowerPanicPlugin()
__plugin_hooks__ = {
    "octoprint.comm.protocol.gcode.queuing": _impl.hook_gcode_queuing,
    "octoprint.comm.protocol.gcode.sent": _impl.hook_gcode_sent,
    "octoprint.comm.protocol.gcode.received": _impl.hook_gcode_received,
    "octoprint.comm.protocol.action": _impl.hook_action,
    "octoprint.comm.protocol.firmware.info": _impl.hook_firmware_info,
    "octoprint.events.register_custom_events": register_custom_events,
    "octoprint.plugin.softwareupdate.check_config": _impl.get_update_information,
}
__plugin_helpers__ = {"print_recovery_pending": _impl.print_recovery_pending}
