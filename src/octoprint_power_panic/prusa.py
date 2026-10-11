"""Prusa firmware's host-print power-panic protocol (MK3/MK3S, firmware 3.14.0+).

Everything here is from the firmware source (prusa3d/Prusa-Firmware v3.14.1):

- On mains loss during a USB/host print the firmware saves the number of the last
  G-code line it *completed* to EEPROM (``EEPROM_FILE_POSITION``, 0x0F91) along with
  position, mesh and temperatures, and prints ``INT4`` if the host is listening.
- On the next boot it restores the print on its own, then pauses (hotend off,
  Z lifted 20 mm) and waits for the host.
- ``M79`` makes it answer ``//action:uvlo_recovery_ready`` (or
  ``uvlo_auto_recovery_ready`` if the bed is still near temperature) while that
  recovery is pending.
- ``D3 Ax0f91 C4`` dumps the saved line number.
- ``M602`` reheats, moves back, asks the host to resend the next line and prints
  ``//action:resumed``.
- Any serial line starting with ``G`` while not printing from SD silently clears
  the pending recovery (cmdqueue.cpp), so nothing may send a G command first.
"""

import re

MIN_VERSION = (3, 14, 0)

PROBE_COMMAND = 'M79 S"OP"'
RESUME_COMMAND = "M602"
STOP_COMMAND = "M603"

EEPROM_FILE_POSITION = 0x0F91
READ_LINE_COMMAND = "D3 Ax0f91 C4"

ACTION_RECOVERY_READY = "uvlo_recovery_ready"
ACTION_AUTO_RECOVERY_READY = "uvlo_auto_recovery_ready"
ACTION_PAUSED = "paused"
ACTION_RESUMED = "resumed"
ACTION_RESUME = "resume"  # the LCD's "Resume print" asking the host to resume
ACTION_CANCEL = "cancel"  # the LCD's "Stop print"

POWER_PANIC_LINE = "INT4"
RESET_LINE = "start"

_FIRMWARE_RE = re.compile(r"Prusa-Firmware\s+(\d+)\.(\d+)\.(\d+)")
_D3_RE = re.compile(r"^\s*([0-9a-fA-F]{4,6})\s+((?:[0-9a-fA-F]{2}\s*){4})$")


def firmware_version(name):
    """``(major, minor, patch)`` for Prusa firmware, else None."""
    match = _FIRMWARE_RE.search(name or "")
    if not match:
        return None
    return tuple(int(x) for x in match.groups())


def supports_host_recovery(version):
    return version is not None and version >= MIN_VERSION


def parse_line_number(line):
    """The saved line number from a ``D3 Ax0f91 C4`` reply line, else None.

    The firmware prints the address then the bytes in hex, e.g. ``0f91  2a 31 00 00``;
    the value is a little-endian uint32.
    """
    match = _D3_RE.match(line.strip())
    if not match or int(match.group(1), 16) != EEPROM_FILE_POSITION:
        return None
    data = bytes(int(b, 16) for b in match.group(2).split())
    return int.from_bytes(data, "little")
