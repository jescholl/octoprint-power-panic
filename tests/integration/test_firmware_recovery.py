"""Prusa firmware path, against the simulated MK3S (see tests/support/fake_printer.py)."""

import pytest
from conftest import OCTOPRINT_MAJOR, make_gcode

LINES = 600


def full_outage(rig, data=None, after=150):
    """Mains drops for the host and the printer together, mid-print, then returns."""
    rig.upload_and_print("part.gcode", data or make_gcode(LINES))
    rig.wait_printed(after)
    rig.power_off()  # the printer saves its recovery to EEPROM on the way down
    rig.kill()  # the host dies at the same instant: no shutdown, no final events
    rig.power_on()
    rig.start()  # host boots, OctoPrint autoconnects


def assert_printed_exactly_once(rig, lines=LINES):
    done = rig.executed_body()
    missing = sorted(set(range(lines)) - set(done))
    repeated = sorted({i for i in done if done.count(i) > 1})
    assert done == list(range(lines)), f"missing {missing[:20]}, repeated {repeated[:20]}"


def assert_firmware_recovery_untouched(rig):
    # Nothing but M602 may end the firmware's recovery, and no move may run while
    # the printer is parked in its paused-recovery state.
    assert not rig.events("recovery_cleared_by_g"), rig.events("recovery_cleared_by_g")
    assert not rig.events("g_while_paused"), rig.events("g_while_paused")


def test_full_outage_resumes_automatically(make_rig):
    rig = make_rig(auto_resume=True)
    rig.wait_operational()
    full_outage(rig)

    assert rig.wait_for(lambda: len(rig.executed_body()) >= LINES, 120), rig.diagnostics()
    assert rig.wait_for(lambda: rig.job()["state"] == "Operational", 30), rig.diagnostics()
    assert_printed_exactly_once(rig)
    assert_firmware_recovery_untouched(rig)
    assert len(rig.events("m602")) == 1
    assert rig.status()["state"] == "idle"


def wait_pending(rig):
    rig.wait_state("pending", 60)
    return rig.status()["recovery"]


def test_manual_resume_holds_everything_until_a_person_decides(make_rig):
    rig = make_rig(auto_resume=False)
    rig.wait_operational()
    full_outage(rig)

    recovery = wait_pending(rig)
    assert recovery["mode"] == "firmware"
    assert "Automatic resume is turned off." in recovery["reasons"]

    # A jog from the UI would clear the firmware's recovery; it must be held back.
    r = rig.api("POST", "/api/printer/printhead", json={"command": "jog", "x": 10})
    assert r.status_code == 204
    executed_before = len(rig.executed_body())

    r = rig.command("resume")
    assert r.status_code == 200, r.text
    assert rig.wait_for(lambda: len(rig.executed_body()) >= LINES, 120), rig.diagnostics()
    assert executed_before < LINES
    assert_printed_exactly_once(rig)
    assert_firmware_recovery_untouched(rig)


def test_resume_from_the_printer_screen(make_rig):
    rig = make_rig(auto_resume=False)
    rig.wait_operational()
    full_outage(rig)
    wait_pending(rig)
    (rig.control / "lcd_resume").touch()
    assert rig.wait_for(lambda: len(rig.executed_body()) >= LINES, 120), rig.diagnostics()
    assert_printed_exactly_once(rig)


def test_discard_stops_the_printer_recovery(make_rig):
    rig = make_rig(auto_resume=False)
    rig.wait_operational()
    full_outage(rig)
    wait_pending(rig)

    r = rig.command("discard")
    assert r.status_code == 200, r.text
    assert rig.wait_for(lambda: rig.event_seen("m603"), 10), rig.diagnostics()
    assert rig.status()["state"] == "idle"

    # Nothing is held back any more.
    rig.api("POST", "/api/printer/command", json={"commands": ["G1 X5 F1234"]})
    assert rig.wait_for(lambda: rig.events("g_while_paused") == [], 2)
    assert not rig.event_seen("m602")


def test_another_print_is_refused_while_recovery_pending(make_rig):
    rig = make_rig(auto_resume=False)
    rig.wait_operational()
    full_outage(rig)
    wait_pending(rig)

    r = rig.api(
        "POST",
        "/api/files/local",
        files={"file": ("other.gcode", make_gcode(50))},
        data={"print": "true"},
    )
    assert r.status_code == 201
    assert rig.wait_for(lambda: rig.job()["state"] == "Operational", 30)
    assert rig.status()["state"] == "pending"
    assert rig.status()["recovery"]["path"] == "part.gcode"
    assert_firmware_recovery_untouched(rig)


def test_printer_only_outage_with_octoprint_up(make_rig):
    rig = make_rig(auto_resume=True, reconnect_interval_seconds=3)
    rig.wait_operational()
    rig.upload_and_print("part.gcode", make_gcode(LINES))
    rig.wait_printed(150)
    rig.power_off()
    # OctoPrint declares the silent printer dead and disconnects.
    assert rig.wait_for(lambda: rig.printer_state() not in ("Printing", "Operational"), 30)
    rig.power_on()
    # The plugin reconnects by itself; OctoPrint's autoconnect never retries.
    assert rig.wait_for(lambda: len(rig.executed_body()) >= LINES, 120), rig.diagnostics()
    assert_printed_exactly_once(rig)
    assert_firmware_recovery_untouched(rig)


@pytest.mark.xfail(
    OCTOPRINT_MAJOR < 2,
    strict=True,
    reason="Known gap on OctoPrint 1.x since the simulator models the boot recovery's "
    "M110 N<saved line>: with OctoPrint still connected through the printer's reboot, a "
    "stale 'Resend' arrives between M602 and the job's own M110 N0 and OctoPrint 1.x acts "
    "on it, so the resumed job never starts (2.0 passes). The full-outage path is "
    "unaffected. To be fixed.",
)
def test_printer_blip_while_connected(make_rig):
    # Mains returns before OctoPrint times out: the printer reboots, sends "start",
    # and OctoPrint cancels the job. That cancel must not wipe the recovery.
    rig = make_rig(auto_resume=True)
    rig.wait_operational()
    rig.upload_and_print("part.gcode", make_gcode(LINES))
    rig.wait_printed(150)
    rig.power_off()
    rig.power_on()
    assert rig.wait_for(lambda: len(rig.executed_body()) >= LINES, 120), rig.diagnostics()
    assert_printed_exactly_once(rig)
    assert_firmware_recovery_untouched(rig)
