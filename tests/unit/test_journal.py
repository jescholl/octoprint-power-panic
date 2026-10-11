import json
import os

import pytest

from octoprint_power_panic.journal import (
    INTERRUPTED,
    PRINTING,
    Journal,
    JournalError,
    lookup_line,
    tool_change_near,
)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "data" / "journal.json")


def test_start_writes_immediately(path, clock):
    j = Journal(path, clock)
    j.start("a/b.gcode", 123, 456)
    data = json.load(open(path))
    assert data["status"] == PRINTING
    assert data["job"] == {"path": "a/b.gcode", "size": 123, "mtime_ns": 456, "started": 1000.0}


def test_flush_persists_lines_and_updated_time(path, clock):
    j = Journal(path, clock)
    j.start("x.gcode", 1, 2)
    j.record_line(5, 100)
    j.record_line(6, 120)
    clock.t = 1005
    j.flush()
    data = json.load(open(path))
    assert data["lines"] == [[5, 100], [6, 120]]
    assert data["updated"] == 1005


def test_flush_without_changes_does_not_touch_updated(path, clock):
    j = Journal(path, clock)
    j.start("x.gcode", 1, 2)
    clock.t = 2000
    j.flush()
    assert json.load(open(path))["updated"] == 1000


def test_no_temp_files_left_behind(path, clock):
    j = Journal(path, clock)
    j.start("x.gcode", 1, 2)
    for i in range(20):
        j.record_line(i, i * 10)
        j.flush()
    assert os.listdir(os.path.dirname(path)) == ["journal.json"]


def test_load_marks_a_print_in_progress_as_lost(path, clock):
    j = Journal(path, clock)
    j.start("x.gcode", 1, 2)
    j.record_line(1, 10)
    j.flush()

    clock.t = 5000
    reloaded = Journal(path, clock)
    snap = reloaded.load()
    assert snap["status"] == INTERRUPTED
    assert snap["interruption"] == {"kind": "process_lost", "at": 5000}
    # Outage length is measured from the last moment the print was known running.
    assert snap["updated"] == 1000
    assert snap["lines"] == [[1, 10]]
    assert json.load(open(path))["status"] == INTERRUPTED


def test_load_missing_file(path, clock):
    assert Journal(path, clock).load() is None


def test_load_corrupt_file_raises(path, clock):
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as f:
        f.write("")
    with pytest.raises(JournalError):
        Journal(path, clock).load()


def test_interrupt_is_idempotent(path, clock):
    j = Journal(path, clock)
    j.start("x.gcode", 1, 2)
    j.interrupt("timeout")
    clock.t = 3000
    j.interrupt("power_panic")
    assert j.snapshot()["interruption"] == {"kind": "timeout", "at": 1000}


def test_recording_stops_once_interrupted(path, clock):
    j = Journal(path, clock)
    j.start("x.gcode", 1, 2)
    j.record_line(1, 10)
    j.interrupt("timeout")
    j.record_line(2, 20)
    assert j.snapshot()["lines"] == [[1, 10]]


def test_clear_removes_file(path, clock):
    j = Journal(path, clock)
    j.start("x.gcode", 1, 2)
    j.clear()
    assert not os.path.exists(path)
    assert j.snapshot() is None
    j.clear()  # idempotent


def test_ring_is_bounded(path, clock):
    j = Journal(path, clock)
    j.start("x.gcode", 1, 2)
    for i in range(2000):
        j.record_line(i, i)
    lines = j.snapshot()["lines"]
    assert len(lines) == 512
    assert lines[-1] == [1999, 1999]


def test_reset_numbering_drops_history(path, clock):
    j = Journal(path, clock)
    j.start("x.gcode", 1, 2)
    j.record_line(10, 100)
    j.record_tool(10, "T1")
    j.reset_numbering()
    assert j.snapshot()["lines"] == []
    assert j.snapshot()["tools"] == []


def snap(lines, tools=()):
    return {"lines": [list(x) for x in lines], "tools": [list(x) for x in tools]}


def test_lookup_exact_line():
    s = snap([(10, 100), (11, 130), (12, 150)])
    assert lookup_line(s, 11).pos == 130
    assert lookup_line(s, 11).beyond == 0


def test_lookup_line_between_file_lines():
    # Line 12 was a non-file command (e.g. M105); resume after file line 11.
    s = snap([(10, 100), (11, 130), (13, 150)])
    assert lookup_line(s, 12).pos == 130


def test_lookup_beyond_journal_reports_gap():
    s = snap([(10, 100), (11, 130)])
    found = lookup_line(s, 14)
    assert found.pos == 130
    assert found.beyond == 3


def test_lookup_older_than_history_fails():
    with pytest.raises(JournalError):
        lookup_line(snap([(10, 100)]), 9)


def test_lookup_empty_fails():
    with pytest.raises(JournalError):
        lookup_line(snap([]), 9)


def test_tool_change_near():
    s = snap([], tools=[(50, "T1")])
    assert tool_change_near(s, 60, 16)
    assert tool_change_near(s, 40, 16)  # sent after the last completed line
    assert not tool_change_near(s, 66, 16)
    assert not tool_change_near(snap([]), 60, 16)
