import pytest

from octoprint_power_panic import controller as C
from octoprint_power_panic.journal import Journal

PRUSA = "Prusa-Firmware 3.14.1 based on Marlin"


class Rig:
    """A controller with a real journal, a fake file system and a fake clock."""

    def __init__(self, tmp_path, **settings):
        self.now = 1000.0
        self.files = {"part.gcode": (5000, 77)}
        self.settings = C.Settings(**settings)
        self.journal = Journal(str(tmp_path / "journal.json"), clock=lambda: self.now)
        self.c = self.new_controller()

    def new_controller(self):
        return C.Controller(self.journal, lambda: self.settings, self.files.get)

    def actions(self):
        return self.c.take_actions()

    def sent(self, actions):
        return [cmd for a in actions if a[0] == "send" for cmd in a[1]]

    def notices(self, actions):
        return [a[1] for a in actions if a[0] == "notify"]

    def advance(self, seconds):
        self.now += seconds
        self.c.tick(self.now)

    def print_some(self, lines=40, tools=()):
        self.c.on_connected(self.now)
        self.c.on_print_started("part.gcode", "local", self.now)
        for n in range(1, lines + 1):
            self.journal.record_line(n, n * 100)
        for n, cmd in tools:
            self.journal.record_tool(n, cmd)
        self.journal.flush()
        self.actions()

    def lose_host(self, outage=60):
        """Host and printer lose power together; OctoPrint restarts."""
        self.now += outage
        self.journal = Journal(self.journal._path, clock=lambda: self.now)
        self.c = self.new_controller()
        self.c.startup(self.now)

    def connect_prusa(self, paused=True):
        self.c.on_connected(self.now)
        self.c.on_firmware_info(PRUSA)
        if paused:
            self.c.on_action("paused", self.now)
        self.c.tick(self.now)

    def answer_probe(self, auto=True):
        self.c.on_action("uvlo_auto_recovery_ready" if auto else "uvlo_recovery_ready", self.now)

    def answer_line(self, line):
        self.c.on_received(
            "0f91  " + " ".join(f"{b:02x}" for b in line.to_bytes(4, "little")), self.now
        )


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path, auto_resume=True)


# -- arming ------------------------------------------------------------------


def test_finished_print_leaves_nothing(rig):
    rig.print_some()
    rig.c.on_print_done()
    assert not rig.journal.active


def test_user_cancel_is_not_recovered(rig):
    rig.print_some()
    rig.c.on_print_cancelled(rig.now)
    assert not rig.journal.active


def test_printer_fault_is_never_recovered(rig):
    rig.print_some()
    rig.c.on_error("firmware")
    rig.c.on_print_failed(rig.now)
    assert not rig.journal.active
    assert "not_recoverable" in rig.notices(rig.actions())


def test_lost_connection_is_recovered(rig):
    rig.print_some()
    rig.c.on_error("timeout")
    rig.c.on_print_failed(rig.now)
    assert rig.journal.snapshot()["interruption"]["kind"] == "timeout"
    assert rig.c.state == C.DETECTING


def test_serial_failure_without_error_event_is_recovered(rig):
    rig.print_some()
    rig.c.on_print_failed(rig.now)
    assert rig.journal.snapshot()["interruption"]["kind"] == "connection_lost"


def test_printer_reset_mid_print_keeps_recovery(rig):
    # Printer-only blip: it comes back within OctoPrint's timeout and sends "start";
    # OctoPrint cancels the job. That must not wipe the recovery.
    rig.print_some()
    rig.c.on_received("start", rig.now)
    rig.c.on_print_cancelled(rig.now)
    assert rig.journal.snapshot()["interruption"]["kind"] == "printer_reset"


def test_power_panic_line_is_recorded(rig):
    rig.print_some()
    rig.c.on_received("INT4", rig.now)
    rig.c.on_error("timeout")
    rig.c.on_print_failed(rig.now)
    assert rig.journal.snapshot()["interruption"]["kind"] == "power_panic"


def test_host_loss_is_recovered_on_restart(rig):
    rig.print_some()
    rig.lose_host()
    assert rig.c.state == C.WAITING
    assert rig.journal.snapshot()["interruption"]["kind"] == "process_lost"


def test_sd_prints_are_not_tracked(rig):
    rig.c.on_print_started("PART.GCO", "sdcard", rig.now)
    assert not rig.journal.active


# -- Prusa firmware path ---------------------------------------------------------


def test_automatic_firmware_resume(rig):
    rig.print_some()
    rig.lose_host(outage=120)
    rig.connect_prusa()
    assert rig.c.hold
    assert rig.sent(rig.actions()) == ['M79 S"OP"']
    rig.answer_probe(auto=True)
    assert rig.sent(rig.actions()) == ["D3 Ax0f91 C4"]

    rig.answer_line(25)
    actions = rig.actions()
    assert rig.sent(actions) == ["M115", "M110 N25", "M602"]
    assert rig.c.state == C.RESUMING
    assert rig.c.hold

    rig.c.on_action("resumed", rig.now)
    assert ("start_job", "part.gcode", 2500) in rig.actions()
    assert not rig.c.hold

    rig.c.on_print_started("part.gcode", "local", rig.now)
    assert rig.c.state == C.IDLE
    assert rig.journal.printing  # the resumed print is protected too
    assert "resumed" in rig.notices(rig.actions())


def test_waits_for_settle_before_probing(rig):
    rig.print_some()
    rig.lose_host()
    rig.connect_prusa(paused=False)
    assert rig.sent(rig.actions()) == []
    rig.advance(rig.settings.settle_timeout)
    assert rig.sent(rig.actions()) == ['M79 S"OP"']


def test_g_commands_held_while_firmware_may_hold_recovery(rig):
    rig.print_some()
    rig.lose_host()
    rig.c.on_connected(rig.now)
    assert rig.c.should_block("G28", set())
    assert rig.c.should_block("g1 x5", None)
    assert not rig.c.should_block("M105", set())
    assert not rig.c.should_block("G28", {C.PLUGIN_TAG})


def test_manual_when_auto_resume_off(tmp_path):
    rig = Rig(tmp_path, auto_resume=False)
    rig.print_some()
    rig.lose_host()
    rig.connect_prusa()
    rig.actions()
    rig.answer_probe()
    rig.actions()
    rig.answer_line(25)
    actions = rig.actions()
    assert rig.c.state == C.PENDING
    assert rig.sent(actions) == []
    assert "Automatic resume is turned off." in rig.c.recovery["reasons"]
    assert rig.c.hold  # nothing may disturb the firmware while a person decides

    rig.c.resume(rig.now)
    assert rig.sent(rig.actions()) == ["M115", "M110 N25", "M602"]


@pytest.mark.parametrize(
    "setup, reason",
    [
        (dict(auto=False), "bed cooled"),
        (dict(outage=3600), "longer than"),
        (dict(tools=[(20, "T2")]), "tool change"),
        (dict(line=45), "may be printed twice"),
    ],
)
def test_each_guard_forces_manual(rig, setup, reason):
    rig.print_some(tools=setup.get("tools", ()))
    rig.lose_host(outage=setup.get("outage", 60))
    rig.connect_prusa()
    rig.answer_probe(auto=setup.get("auto", True))
    rig.answer_line(setup.get("line", 25))
    rig.actions()
    assert rig.c.state == C.PENDING
    assert any(reason in r for r in rig.c.recovery["reasons"]), rig.c.recovery["reasons"]


def test_changed_file_cannot_be_resumed(rig):
    rig.print_some()
    rig.lose_host()
    rig.files["part.gcode"] = (5001, 78)
    rig.connect_prusa()
    rig.answer_probe()
    rig.answer_line(25)
    rig.actions()
    assert rig.c.state == C.FAILED
    assert rig.c.hold
    assert "changed" in rig.c.recovery["error"]


def test_keepalive_while_pending_then_stops(tmp_path):
    rig = Rig(tmp_path, auto_resume=False, hold_heat=60, keepalive_interval=20)
    rig.print_some()
    rig.lose_host()
    rig.connect_prusa()
    rig.answer_probe()
    rig.answer_line(25)
    rig.actions()
    probes = 0
    for _ in range(12):
        rig.advance(10)
        probes += rig.sent(rig.actions()).count('M79 S"OP"')
    assert probes == 3  # at 10, 30, 50 s; none after the 60 s hold


def test_printer_lcd_resume_and_stop(tmp_path):
    rig = Rig(tmp_path, auto_resume=False)
    rig.print_some()
    rig.lose_host()
    rig.connect_prusa()
    rig.answer_probe()
    rig.answer_line(25)
    rig.actions()
    rig.c.on_action("resume", rig.now)
    assert rig.sent(rig.actions()) == ["M115", "M110 N25", "M602"]

    rig2 = Rig(tmp_path / "2", auto_resume=False)
    (tmp_path / "2").mkdir()
    rig2.print_some()
    rig2.lose_host()
    rig2.connect_prusa()
    rig2.answer_probe()
    rig2.answer_line(25)
    rig2.c.on_action("cancel", rig2.now)
    assert not rig2.journal.active
    assert rig2.c.state == C.IDLE


def test_discard_stops_firmware_recovery(tmp_path):
    rig = Rig(tmp_path, auto_resume=False)
    rig.print_some()
    rig.lose_host()
    rig.connect_prusa()
    rig.answer_probe()
    rig.answer_line(25)
    rig.actions()
    assert rig.c.discard(rig.now)
    assert rig.sent(rig.actions()) == ["M603"]
    assert not rig.journal.active
    assert not rig.c.hold


def test_another_print_is_blocked_while_firmware_recovery_pending(tmp_path):
    rig = Rig(tmp_path, auto_resume=False)
    rig.print_some()
    rig.lose_host()
    rig.connect_prusa()
    rig.answer_probe()
    rig.answer_line(25)
    rig.actions()
    rig.c.on_print_started("other.gcode", "local", rig.now)
    actions = rig.actions()
    assert ("cancel_print",) in actions
    assert "blocked" in rig.notices(actions)
    assert rig.journal.snapshot()["job"]["path"] == "part.gcode"


def test_job_start_is_retried_then_nozzle_lifted_and_cooled(rig):
    rig.print_some()
    rig.lose_host()
    rig.connect_prusa()
    rig.answer_probe()
    rig.answer_line(25)
    rig.c.on_action("resumed", rig.now)
    assert ("start_job", "part.gcode", 2500) in rig.actions()

    # First timeout: the printer is in position after M602, so just try again.
    rig.advance(rig.settings.start_timeout + 1)
    assert rig.actions() == [("start_job", "part.gcode", 2500)]
    assert rig.c.state == C.STARTING

    # Second timeout: give up, but get the hot nozzle off the part first.
    rig.advance(rig.settings.start_timeout + 1)
    actions = rig.actions()
    assert rig.sent(actions) == ["G91", "G1 E-2 F2400", "G1 Z10 F600", "G90", "M104 S0"]
    assert rig.c.state == C.FAILED
    assert not rig.c.hold


def test_resume_timeout_fails(rig):
    rig.print_some()
    rig.lose_host()
    rig.connect_prusa()
    rig.answer_probe()
    rig.answer_line(25)
    rig.actions()
    rig.advance(rig.settings.resume_timeout + 1)
    assert rig.c.state == C.FAILED


def test_unreadable_line_number_fails_after_retries(rig):
    rig.print_some()
    rig.lose_host()
    rig.connect_prusa()
    rig.answer_probe()
    reads = rig.sent(rig.actions()).count("D3 Ax0f91 C4")
    for _ in range(rig.settings.probe_attempts):
        rig.advance(rig.settings.probe_timeout)
        reads += rig.sent(rig.actions()).count("D3 Ax0f91 C4")
    assert reads == rig.settings.probe_attempts
    assert rig.c.state == C.FAILED


# -- no firmware recovery: generic fallback ---------------------------------------


def test_prusa_without_pending_recovery_offers_fallback(rig):
    rig.print_some()
    rig.lose_host()
    rig.connect_prusa()
    for _ in range(rig.settings.probe_attempts):
        rig.advance(rig.settings.probe_timeout)
    assert rig.c.state == C.PENDING
    assert rig.c.recovery["mode"] == C.FALLBACK
    assert rig.c.recovery["pos"] == 4000
    assert not rig.c.hold


def test_non_prusa_offers_fallback_without_probing(rig):
    rig.print_some()
    rig.lose_host()
    rig.c.on_connected(rig.now)
    rig.c.on_firmware_info("Marlin 2.1.2")
    rig.c.tick(rig.now)
    assert rig.sent(rig.actions()) == []
    assert rig.c.state == C.PENDING
    assert rig.c.recovery["mode"] == C.FALLBACK
    assert not rig.c.hold


def test_fallback_is_never_automatic_and_starts_recovery_file(rig):
    rig.print_some()
    rig.lose_host()
    rig.c.on_connected(rig.now)
    rig.c.on_firmware_info("Marlin 2.1.2")
    rig.c.tick(rig.now)
    rig.actions()
    assert rig.c.state == C.PENDING
    rig.c.resume(rig.now)
    (action,) = [a for a in rig.actions() if a[0] == "start_fallback"]
    assert action[1:4] == ("part.gcode", 4000, "recovered_part.gcode")
    rig.c.on_print_started("recovered_part.gcode", "local", rig.now)
    assert rig.c.state == C.IDLE


def test_unidentified_firmware_is_still_probed(tmp_path):
    # Real MK3S, 2026-10-14: after the connect reset replayed the recovery, M115
    # was never answered. The plugin must still find the firmware's recovery.
    rig = Rig(tmp_path, auto_resume=False)
    rig.print_some()
    rig.lose_host()
    rig.c.on_connected(rig.now)
    rig.c.on_action("paused", rig.now)
    rig.c.tick(rig.now)
    assert rig.sent(rig.actions()) == ["M115", 'M79 S"OP"']
    assert rig.c.hold
    rig.answer_probe()
    rig.answer_line(25)
    rig.actions()
    assert rig.c.state == C.PENDING
    assert rig.c.recovery["mode"] == C.FIRMWARE
    # The recovery reply alone proves Prusa host recovery, so discard stops it.
    rig.c.discard(rig.now)
    assert rig.sent(rig.actions()) == ["M603"]


def test_unidentified_firmware_without_recovery_falls_back(rig):
    rig.print_some()
    rig.lose_host()
    rig.c.on_connected(rig.now)
    rig.advance(rig.settings.settle_timeout)
    for _ in range(rig.settings.probe_attempts):
        rig.advance(rig.settings.probe_timeout)
    assert rig.c.state == C.PENDING
    assert rig.c.recovery["mode"] == C.FALLBACK
    assert not rig.c.hold


def test_rejected_lines_during_detection_are_resynced_and_reprobed(rig):
    # Real MK3S, 2026-10-14: the boot recovery's "M110 N<saved>" made the printer
    # reject OctoPrint's numbered M115 and the plugin's M79 ("Resend: <saved+1>").
    rig.print_some()
    rig.lose_host()
    rig.c.on_connected(rig.now)
    rig.c.on_action("paused", rig.now)
    rig.c.tick(rig.now)
    assert rig.sent(rig.actions()) == ["M115", 'M79 S"OP"']

    assert rig.c.on_resend_request(rig.now)
    assert rig.sent(rig.actions()) == ["M110 N0"]
    assert rig.c.on_resend_request(rig.now + 0.1)  # same burst: no second resync
    assert rig.sent(rig.actions()) == []

    rig.c.tick(rig.now)
    assert rig.sent(rig.actions()) == ["M115", 'M79 S"OP"']
    assert rig.c.hold


def test_resend_requests_while_printing_and_resuming(rig):
    rig.print_some()
    assert not rig.c.on_resend_request(rig.now)  # a normal print's resends are OctoPrint's
    rig.lose_host()
    rig.connect_prusa()
    rig.answer_probe()
    rig.answer_line(25)
    rig.actions()
    assert rig.c.state == C.RESUMING
    # M602 answers "Resend: <line+1>": neutralised (OctoPrint may have numbered the
    # M602 itself and would resend it), but nothing is sent and the state holds.
    assert rig.c.on_resend_request(rig.now)
    assert rig.sent(rig.actions()) == []
    assert rig.c.state == C.RESUMING


def test_fan_is_restored_before_the_job_restarts(rig):
    # Real MK3S, 2026-10-14: the resumed layers' cooling couldn't be confirmed, and
    # the part failed. Re-send the file's last fan command before streaming resumes.
    rig.c.on_connected(rig.now)
    rig.c.on_print_started("part.gcode", "local", rig.now)
    for n in range(1, 41):
        rig.journal.record_line(n, n * 100)
    rig.journal.update_state(fan="M106 S125")
    rig.journal.flush()
    rig.actions()
    rig.lose_host()
    rig.connect_prusa()
    rig.answer_probe()
    rig.answer_line(25)
    rig.actions()
    rig.c.on_action("resumed", rig.now)
    assert rig.actions() == [("send", ["M106 S125"]), ("start_job", "part.gcode", 2500)]


def test_resends_neutralised_until_the_job_resets_line_numbers(rig):
    rig.print_some()
    rig.lose_host()
    rig.connect_prusa()
    rig.answer_probe()
    rig.answer_line(25)
    rig.c.on_action("resumed", rig.now)
    rig.actions()
    assert rig.c.state == C.STARTING
    # A numbered command rejected after M602, before the job's own M110 N0.
    assert rig.c.on_resend_request(rig.now)
    rig.c.on_job_numbering_reset()
    # From the job's M110 N0 on, resends are real and belong to OctoPrint.
    assert not rig.c.on_resend_request(rig.now)
    assert rig.sent(rig.actions()) == []


def test_resync_reidentifies_a_known_printer(rig):
    # Printer-only blip: the firmware was identified before it rebooted.
    rig.print_some()
    rig.c.on_firmware_info(PRUSA)
    rig.c.on_received("start", rig.now)
    rig.c.on_print_cancelled(rig.now)
    rig.c.on_action("paused", rig.now)
    rig.c.tick(rig.now)
    assert rig.sent(rig.actions()) == ['M79 S"OP"']
    rig.c.on_resend_request(rig.now)
    rig.c.tick(rig.now)
    assert rig.sent(rig.actions()) == ["M110 N0", "M115", 'M79 S"OP"']


def test_firmware_info_before_connected_event_is_kept(rig):
    # OctoPrint autoconnects before the plugin starts, so the firmware report and
    # the printer's "paused" can arrive before on_connected.
    rig.print_some()
    rig.c.on_disconnected(rig.now)
    rig.lose_host()
    rig.c.on_firmware_info(PRUSA)
    rig.c.on_action("paused", rig.now)
    rig.c.on_connected(rig.now)
    rig.c.tick(rig.now)
    assert rig.sent(rig.actions()) == ['M79 S"OP"']


# -- connection handling -----------------------------------------------------------


def connect_times(rig, seconds, step=10):
    """Advance in steps; return the offsets (from now) at which a connect was asked."""
    start, times = rig.now, []
    for _ in range(int(seconds / step)):
        rig.advance(step)
        times += [rig.now - start] * sum(1 for a in rig.actions() if a == ("connect",))
    return times


def waiting_rig(tmp_path, **settings):
    rig = Rig(tmp_path, **settings)
    rig.print_some()
    rig.c.on_disconnected(rig.now)
    rig.lose_host()
    assert rig.c.state == C.WAITING
    return rig


def test_reconnects_back_off_to_the_maximum_and_keep_going_when_power_is_unknown(tmp_path):
    rig = waiting_rig(tmp_path, reconnect_interval=30, reconnect_max_interval=240, max_age=10**6)
    times = connect_times(rig, 2000)
    # Waits of 30, 60, 120, 240, then 240 for good: never gives up on its own.
    assert times[:6] == [30, 90, 210, 450, 690, 930]
    assert [b - a for a, b in zip(times[3:], times[4:], strict=False)] == [240] * (len(times) - 4)


def test_backoff_settles_at_one_attempt_an_hour_by_default(tmp_path):
    rig = waiting_rig(tmp_path, max_age=10**7)
    times = connect_times(rig, 6 * 3600, step=30)
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert gaps[:7] == [60, 120, 240, 480, 960, 1920, 3600]
    assert set(gaps[7:]) == {3600}


def test_no_reconnects_while_the_printer_is_reported_off(tmp_path):
    rig = waiting_rig(tmp_path)
    rig.c.on_power_state(False, rig.now)
    assert connect_times(rig, 3600) == []
    assert rig.c.state == C.WAITING


def test_power_coming_back_reconnects_soon_with_the_backoff_reset(tmp_path):
    rig = waiting_rig(tmp_path, max_age=10**6)
    rig.c.on_power_state(None, rig.now)
    connect_times(rig, 1000)  # several unknown-power attempts: the wait is long now
    rig.c.on_power_state(False, rig.now)
    assert connect_times(rig, 7200) == []
    rig.c.on_power_state(True, rig.now)
    assert connect_times(rig, 100) == [30, 90]


def test_attempts_with_power_on_also_back_off(tmp_path):
    rig = waiting_rig(tmp_path, max_age=10**6)
    rig.c.on_power_state(True, rig.now)
    assert connect_times(rig, 500)[:4] == [30, 90, 210, 450]


def test_a_connect_that_drops_again_does_not_restart_the_backoff(tmp_path):
    # Every connect resets a Prusa printer: a link that keeps dropping must not be
    # hammered every 30 s.
    rig = waiting_rig(tmp_path, max_age=10**6)
    assert connect_times(rig, 100) == [30, 90]
    rig.c.on_connected(rig.now)
    rig.c.on_disconnected(rig.now)
    assert rig.c.state == C.WAITING
    assert connect_times(rig, 200) == [120]


def test_discard_while_waiting_stops_reconnecting(tmp_path):
    rig = waiting_rig(tmp_path)
    assert rig.c.discard(rig.now)
    assert connect_times(rig, 3600) == []


def test_old_interruption_expires_while_waiting(tmp_path):
    rig = waiting_rig(tmp_path, max_age=3600)
    rig.c.on_power_state(False, rig.now)  # never connects, so only the wait can expire it
    rig.advance(3601)
    assert rig.c.state == C.IDLE
    assert not rig.journal.active
    assert "expired" in rig.notices(rig.actions())
    assert connect_times(rig, 3600) == []


def test_finished_print_whose_done_event_was_lost_is_not_recovered(rig):
    # 2026-11-08: a plugin blocked OctoPrint's event thread, PrintDone never arrived,
    # and after a restart the finished print was offered for recovery.
    rig.print_some(lines=50)  # 50 * 100 = 5000 = the file's size
    rig.lose_host()
    assert rig.c.state == C.IDLE
    assert not rig.journal.active
    assert connect_times(rig, 600) == []


def test_finished_print_is_not_armed_when_the_link_drops_at_the_end(rig):
    rig.print_some(lines=50)
    rig.c.on_error("timeout")
    rig.c.on_print_failed(rig.now)
    assert rig.c.state == C.IDLE
    assert not rig.journal.active


def test_print_one_line_short_of_the_end_is_still_recovered(rig):
    rig.print_some(lines=49)
    rig.lose_host()
    assert rig.c.state == C.WAITING


def test_disconnect_during_detection_returns_to_waiting(rig):
    rig.print_some()
    rig.lose_host()
    rig.connect_prusa()
    rig.c.on_disconnected(rig.now)
    assert rig.c.state == C.WAITING
    assert not rig.c.hold


def test_old_interruption_expires(rig):
    rig.print_some()
    rig.lose_host(outage=rig.settings.max_age + 1)
    rig.c.on_connected(rig.now)
    assert not rig.journal.active
    assert rig.c.state == C.IDLE
    assert "expired" in rig.notices(rig.actions())


def test_printer_only_outage_while_connected_is_detected(rig):
    rig.print_some()
    rig.c.on_received("start", rig.now)
    rig.c.on_print_cancelled(rig.now)
    assert rig.c.state == C.DETECTING
    rig.c.on_firmware_info(PRUSA)
    rig.c.on_action("paused", rig.now)
    rig.c.tick(rig.now)
    assert rig.sent(rig.actions()) == ['M79 S"OP"']
