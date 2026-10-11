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

    # -- settings --------------------------------------------------------

    def get_settings_defaults(self):
        return {
            "auto_resume": False,
            "auto_max_outage_minutes": 10,
            "max_age_hours": 24,
            "hold_heat_minutes": 60,
            "reconnect_minutes": 30,
            # Not in the settings UI; tests shorten it.
            "reconnect_interval_seconds": 30,
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
            reconnect_window=s.get_float(["reconnect_minutes"]) * 60,
            reconnect_interval=s.get_float(["reconnect_interval_seconds"]),
        )

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
        self._printer.select_file(target, False, printAfterSelect=True, tags=tags)

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
        if not self._journal.printing:
            return
        tags = tags or set()
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
        # Cheap pre-filter: this hook sees every line the printer sends.
        if line.strip() in ("INT4", "start") or self._controller.state == "reading":
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
