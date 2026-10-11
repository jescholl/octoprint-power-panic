import os

from octoprint_power_panic.fallback import build_header, recovery_name, write_recovery_file
from octoprint_power_panic.tracker import StreamTracker


def feed(tracker, *commands):
    for c in commands:
        tracker.feed(c)
    return tracker.state


def test_tracks_absolute_moves_and_settings():
    t = StreamTracker()
    state = feed(
        t,
        "M140 S90",
        "M104 S230",
        "G90",
        "M83",
        "G1 Z0.2 F720",
        "G1 X10.5 Y20 E1.2 F1500",
        "M106 S255",
        "M900 K0.04",
        "M221 S95",
    )
    assert state["bed"] == 90
    assert state["tool"] == 230
    assert (state["x"], state["y"], state["z"]) == (10.5, 20, 0.2)
    assert state["e_mode"] == "M83"
    assert state["feedrate"] == 1500
    assert state["fan"] == "M106 S255"
    assert state["extras"] == {"M900": "M900 K0.04", "M221": "M221 S95"}


def test_relative_moves_accumulate():
    t = StreamTracker()
    state = feed(t, "G90", "G1 Z1", "G91", "G1 Z0.5", "G90")
    assert state["z"] == 1.5


def test_homing_forgets_position_and_relative_after_home():
    t = StreamTracker()
    state = feed(t, "G1 X5 Y5 Z5", "G28 X Y")
    assert state["x"] is None and state["y"] is None and state["z"] == 5
    state = feed(t, "G91", "G1 X2")
    assert state["x"] == 2


def test_tool_change_reported_but_not_stored():
    t = StreamTracker()
    assert t.feed("T3")["tool_command"] == "T3"
    assert "tool_command" not in t.state


def test_comments_ignored():
    t = StreamTracker()
    assert t.feed("; G1 X5") == {}
    assert t.feed("G1 X5 ; move") == {"x": 5.0}


def test_header_order_lifts_before_homing_and_heats_before_moving_back():
    state = {"bed": 60, "tool": 215, "z": 12.4, "x": 50, "y": 60, "e_mode": "M82", "e": 345.6}
    header = build_header(
        state,
        source_name="part.gcode",
        pos=1234,
        z_lift=5,
        home_command="G28 X Y",
        xy_feedrate=3000,
        extra_gcode="M117 resumed",
    ).splitlines()
    assert header.index(
        "G92 Z12.4 ; assume Z did not move while the printer was off"
    ) < header.index("G1 Z5 F600 ; lift clear of the part before homing X/Y")
    assert header.index("G1 Z5 F600 ; lift clear of the part before homing X/Y") < header.index(
        "G28 X Y"
    )
    assert header.index("M109 S215") < header.index("G1 X50 Y60 F3000")
    assert header.index("G1 X50 Y60 F3000") < header.index("G1 Z12.4 F600")
    assert "G92 E345.6" in header
    assert "M117 resumed" in header


def test_header_relative_extrusion_does_not_set_e():
    header = build_header(
        {"z": 1, "e_mode": "M83", "e": 99},
        source_name="p",
        pos=0,
        z_lift=5,
        home_command="G28 X Y",
        xy_feedrate=3000,
        extra_gcode="",
    )
    assert "M83" in header
    assert "G92 E" not in header


def test_write_recovery_file(tmp_path):
    src = tmp_path / "in.gcode"
    src.write_bytes(b"G1 X1\nG1 X2\nG1 X3\n")
    dst = tmp_path / "out.gcode"
    write_recovery_file(str(src), 6, "M117 hi\n", str(dst))
    assert dst.read_bytes() == b"M117 hi\nG1 X2\nG1 X3\n"
    assert sorted(os.listdir(tmp_path)) == ["in.gcode", "out.gcode"]


def test_recovery_name():
    assert recovery_name("a/b.gcode") == "a/recovered_b.gcode"
    assert recovery_name("b.gcode") == "recovered_b.gcode"
    assert recovery_name("recovered_b.gcode") == "recovered_b.gcode"
