"""Crash-safe record of the print in progress.

The journal is the only state that has to survive the host losing power, so every
write goes to a temporary file that is fsynced and then atomically renamed over the
previous one. A power cut at any moment leaves either the old or the new journal on
disk, never a truncated one.

While printing it keeps a ring of (line number, file position after that line)
pairs for the lines most recently sent from the file. Prusa firmware saves the
number of the last line it *completed*; the ring turns that back into the byte
offset to resume from.
"""

import copy
import json
import os
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass

VERSION = 1
RING_SIZE = 512
TOOL_HISTORY = 16

PRINTING = "printing"
INTERRUPTED = "interrupted"


class JournalError(Exception):
    pass


@dataclass(frozen=True)
class LineLookup:
    """Where to resume after the firmware's last completed line ``line``."""

    pos: int
    # How many lines past the newest journaled line the firmware claims to have
    # completed. Non-zero means the journal flush lagged the printer and those
    # lines may be printed twice, so the resume must not be automatic.
    beyond: int


class Journal:
    def __init__(self, path, clock=time.time):
        self._path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._data = None
        self._ring = deque(maxlen=RING_SIZE)
        self._tools = deque(maxlen=TOOL_HISTORY)
        self._dirty = False

    # -- lifecycle -------------------------------------------------------

    def load(self):
        """Read the journal left by the previous run.

        A journal still marked ``printing`` means OctoPrint (or the whole host)
        died mid-print without seeing the print end, so it becomes interrupted.
        Raises JournalError if a journal exists but cannot be read.
        """
        with self._lock:
            try:
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                return None
            except (OSError, ValueError) as e:
                raise JournalError(f"unreadable journal {self._path}: {e}") from e

            if not isinstance(data, dict) or data.get("version") != VERSION:
                raise JournalError(f"unsupported journal format in {self._path}")

            self._data = data
            self._ring = deque((tuple(x) for x in data.pop("lines", [])), maxlen=RING_SIZE)
            self._tools = deque((tuple(x) for x in data.pop("tools", [])), maxlen=TOOL_HISTORY)
            if data["status"] == PRINTING:
                self._interrupt("process_lost")
            return self.snapshot()

    def start(self, path, size, mtime_ns):
        with self._lock:
            now = self._clock()
            self._data = {
                "version": VERSION,
                "status": PRINTING,
                "job": {"path": path, "size": size, "mtime_ns": mtime_ns, "started": now},
                "updated": now,
                "interruption": None,
                "state": {},
            }
            self._ring.clear()
            self._tools.clear()
            self._write()

    def clear(self):
        with self._lock:
            self._data = None
            self._ring.clear()
            self._tools.clear()
            self._dirty = False
            try:
                os.remove(self._path)
            except FileNotFoundError:
                return
            _fsync_dir(os.path.dirname(self._path))

    # -- recording while printing ---------------------------------------

    @property
    def active(self):
        with self._lock:
            return self._data is not None

    @property
    def printing(self):
        with self._lock:
            return self._data is not None and self._data["status"] == PRINTING

    def record_line(self, line, pos):
        with self._lock:
            if self.printing:
                self._ring.append((line, pos))
                self._dirty = True

    def record_tool(self, line, command):
        with self._lock:
            if self.printing:
                self._tools.append((line, command))
                self._dirty = True

    def reset_numbering(self):
        """OctoPrint renumbered its lines mid-print; older pairs no longer match."""
        with self._lock:
            self._ring.clear()
            self._tools.clear()
            self._dirty = True

    def update_state(self, **values):
        with self._lock:
            if self.printing:
                self._data["state"].update(values)
                self._dirty = True

    def flush(self):
        """Persist pending changes. Called several times a second while printing."""
        with self._lock:
            if self._dirty and self.printing:
                self._data["updated"] = self._clock()
                self._write()

    def interrupt(self, kind):
        with self._lock:
            if self._data is not None:
                self._interrupt(kind)

    def _interrupt(self, kind):
        # "updated" is deliberately left alone: it is the last moment the print was
        # known to be running, which is what outage length is measured from.
        if self._data["status"] == INTERRUPTED:
            return
        self._data["status"] = INTERRUPTED
        self._data["interruption"] = {"kind": kind, "at": self._clock()}
        self._write()

    # -- reading ---------------------------------------------------------

    def snapshot(self):
        with self._lock:
            if self._data is None:
                return None
            data = copy.deepcopy(self._data)
            data["lines"] = [list(x) for x in self._ring]
            data["tools"] = [list(x) for x in self._tools]
            return data

    def _write(self):
        data = self.snapshot()
        directory = os.path.dirname(self._path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".journal-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        _fsync_dir(directory)
        self._dirty = False


def lookup_line(snapshot, line):
    """Map the firmware's last completed line number to the file offset to resume at."""
    lines = snapshot.get("lines") or []
    if not lines:
        raise JournalError("the journal has no line history")
    if line < lines[0][0]:
        raise JournalError(
            f"line {line} is older than the journal's history (starts at {lines[0][0]})"
        )

    pos = None
    for number, after in lines:
        if number > line:
            break
        pos = after
    newest = lines[-1][0]
    return LineLookup(pos=pos, beyond=max(0, line - newest))


def tool_change_near(snapshot, line, window):
    """True if a tool change may have been in progress when ``line`` completed.

    A T command is slow and sits in the firmware's command buffer while the MMU
    works, so one sent shortly before or any time after the last completed line
    may have been interrupted part way through.
    """
    return any(number > line - window for number, _ in snapshot.get("tools") or [])


def _fsync_dir(directory):
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
