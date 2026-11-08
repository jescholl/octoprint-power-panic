"""Reconnecting after a full outage: power-plugin aware, with backoff otherwise."""

from conftest import make_gcode
from test_firmware_recovery import (
    LINES,
    assert_firmware_recovery_untouched,
    assert_printed_exactly_once,
)


def outage_with_printer_still_off(rig):
    """Host and printer lose mains mid-print; only the host comes back."""
    rig.upload_and_print("part.gcode", make_gcode(LINES))
    rig.wait_printed(150)
    rig.power_off()
    rig.kill()
    rig.start()  # OctoPrint's one autoconnect finds nothing
    rig.wait_state("waiting_for_printer", 30)


def test_no_reconnects_while_a_power_plugin_reports_off_then_one_when_on(make_rig):
    rig = make_rig(auto_resume=True, reconnect_interval_seconds=2, power_plugin=True)
    rig.wait_operational()
    outage_with_printer_still_off(rig)

    assert rig.wait_for(
        lambda: "Reading printer power from fake_power.get_psu_state" in rig.log_text(), 15
    ), rig.diagnostics()
    rig.wait_for(lambda: False, 15)  # without the power plugin this is ~4 attempts
    assert rig.reconnect_times() == [], rig.diagnostics()

    rig.power_on()
    assert rig.wait_for(lambda: len(rig.executed_body()) >= LINES, 120), rig.diagnostics()
    assert_printed_exactly_once(rig)
    assert_firmware_recovery_untouched(rig)
    assert len(rig.reconnect_times()) == 1


def test_without_a_power_plugin_reconnects_back_off(make_rig):
    rig = make_rig(auto_resume=True, reconnect_interval_seconds=4)
    rig.wait_operational()
    outage_with_printer_still_off(rig)
    assert rig.wait_for(lambda: len(rig.reconnect_times()) >= 3, 60), rig.diagnostics()
    assert "No power plugin found" in rig.log_text()

    times = rig.reconnect_times()
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert 7 <= gaps[0] <= 10 and 15 <= gaps[1] <= 18, gaps  # 8 s, then 16 s

    rig.power_on()
    assert rig.wait_for(lambda: len(rig.executed_body()) >= LINES, 120), rig.diagnostics()
    assert_printed_exactly_once(rig)
