"""Generic recovery on a printer without firmware power panic (simulated Marlin).

Only checks what can be checked virtually: the recovery is offered, never automatic,
and the file it prints is the header followed by the rest of the original file.
Whether that header is right for a real non-Prusa machine is untested.
"""

from conftest import make_gcode

LINES = 600


def test_marlin_outage_offers_manual_generic_recovery(make_rig):
    rig = make_rig(printer={"firmware": "marlin"}, auto_resume=True, fallback_xy_feedrate=900)
    rig.wait_operational()
    rig.upload_and_print("part.gcode", make_gcode(LINES))
    rig.wait_printed(150)
    rig.power_off()
    rig.kill()
    rig.power_on()
    rig.start()

    rig.wait_state("pending", 60)
    recovery = rig.status()["recovery"]
    assert recovery["mode"] == "fallback"
    before = rig.executed_body()

    assert rig.command("resume").status_code == 200
    assert rig.wait_for(lambda: rig.job()["state"].startswith("Printing"), 30), rig.diagnostics()
    assert rig.job()["job"]["file"]["path"] == "recovered_part.gcode"
    assert rig.wait_for(lambda: LINES - 1 in rig.executed_body(), 120), rig.diagnostics()

    after = rig.executed_body()[len(before) :]
    assert after == list(range(after[0], LINES))
    # Resumes from the last line *sent*; the lines the printer had buffered are
    # skipped. That gap is the documented cost of the generic recovery.
    assert 0 <= after[0] - (before[-1] + 1) <= 20
