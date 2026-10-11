"""When a recovery must NOT be offered."""

import time

from conftest import make_gcode


def settle(rig):
    time.sleep(2)
    return rig.status()["state"]


def test_completed_print_leaves_nothing(make_rig):
    rig = make_rig(auto_resume=True)
    rig.wait_operational()
    rig.upload_and_print("small.gcode", make_gcode(40))
    assert rig.wait_for(lambda: rig.job()["state"] == "Operational", 60)
    assert settle(rig) == "idle"
    rig.kill()
    rig.start()
    rig.wait_operational()
    assert settle(rig) == "idle"


def test_cancelled_print_is_not_recovered(make_rig):
    rig = make_rig(auto_resume=True)
    rig.wait_operational()
    rig.upload_and_print("part.gcode", make_gcode(600))
    rig.wait_printed(100)
    rig.api("POST", "/api/job", json={"command": "cancel"})
    assert rig.wait_for(lambda: rig.job()["state"] == "Operational", 30)
    assert settle(rig) == "idle"


def test_printer_fault_is_never_recovered(make_rig):
    # Upstream PowerFailure restarted the print 58 ms after reconnecting from a
    # thermal runaway. This plugin must not.
    rig = make_rig(auto_resume=True, reconnect_interval_seconds=3)
    rig.wait_operational()
    rig.upload_and_print("part.gcode", make_gcode(600))
    rig.wait_printed(100)
    (rig.control / "fault").touch()
    assert rig.wait_for(lambda: rig.printer_state() not in ("Printing", "Operational"), 30)
    done = len(rig.executed_body())
    rig.api("POST", "/api/connection", json={"command": "connect", "port": "FAKEPRINTER"})
    rig.wait_operational()
    assert settle(rig) == "idle"
    time.sleep(3)
    assert len(rig.executed_body()) == done
