"""Generic recovery for printers without firmware power-panic support.

Used for non-Prusa printers, Prusa firmware older than 3.14, and Prusa printers
whose firmware has no recovery pending (the host died but the printer kept power,
so reconnecting reset it). It writes a new G-code file: a header that reheats,
re-establishes position and restores the settings seen in the stream, followed by
the rest of the original file. It is only ever started by a person.

Known limits, also stated in the README: the resume point is the last line *sent*,
not the last line executed, so a few moves the printer had buffered are skipped;
Z is assumed not to have moved while the printer was off; mesh bed levelling
cannot be redone over a part; the X/Y homing command must suit the machine
(cartesian and CoreXY printers can home X/Y without Z).
"""

import os
import shutil

# Settings seen in the stream that are restored verbatim before resuming.
RESTORED_COMMANDS = ("M900", "M204", "M201", "M203", "M205", "M220", "M221")


def build_header(state, *, source_name, pos, z_lift, home_command, xy_feedrate, extra_gcode):
    bed = _num(state.get("bed"))
    tool = _num(state.get("tool"))
    z = _num(state.get("z"))
    x = state.get("x")
    y = state.get("y")

    lines = [
        f"; power_panic: fallback recovery of {source_name} from byte {pos}",
        f"M140 S{bed}",
        f"M104 S{tool}",
        "G21",
        "G90",
        f"G92 Z{z} ; assume Z did not move while the printer was off",
        "G91",
        f"G1 Z{_num(z_lift)} F600 ; lift clear of the part before homing X/Y",
        "G90",
        home_command,
        f"M190 S{bed}",
        f"M109 S{tool}",
    ]
    if x is not None and y is not None:
        lines.append(f"G1 X{_num(x)} Y{_num(y)} F{_num(xy_feedrate)}")
    lines.append(f"G1 Z{z} F600")

    e_mode = state.get("e_mode")
    if e_mode in ("M82", "M83"):
        lines.append(e_mode)
        if e_mode == "M82" and state.get("e") is not None:
            lines.append(f"G92 E{_num(state['e'])}")

    extras = state.get("extras") or {}
    lines.extend(extras[code] for code in RESTORED_COMMANDS if code in extras)
    if state.get("fan"):
        lines.append(state["fan"])
    if state.get("feedrate") is not None:
        lines.append(f"G1 F{_num(state['feedrate'])}")
    if extra_gcode and extra_gcode.strip():
        lines.append(extra_gcode.strip())
    lines.append("; power_panic: end of recovery header")
    return "\n".join(lines) + "\n"


def write_recovery_file(source, pos, header, destination):
    """Header followed by ``source`` from byte ``pos``; streamed, never fully in memory."""
    tmp = destination + ".partial"
    with open(source, "rb") as src, open(tmp, "wb") as dst:
        dst.write(header.encode("utf-8"))
        src.seek(pos)
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp, destination)


def recovery_name(path):
    directory, name = os.path.split(path)
    if not name.startswith("recovered_"):
        name = "recovered_" + name
    return os.path.join(directory, name) if directory else name


def _num(value):
    if value is None:
        return 0
    value = float(value)
    return int(value) if value.is_integer() else round(value, 5)
