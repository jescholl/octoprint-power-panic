"""Follows the G-code stream to remember what a generic recovery has to restore."""

import re

from .fallback import RESTORED_COMMANDS

_WORD_RE = re.compile(r"([A-Z])\s*(-?\d*\.?\d+)")
_TOOL_RE = re.compile(r"^T(\d+)\b")


class StreamTracker:
    def __init__(self):
        self.state = {}
        self._relative_xyz = False
        self._relative_e = False

    def reset(self):
        self.__init__()

    def feed(self, command):
        """Update from one sent command; returns the changed state keys (possibly empty)."""
        cmd = command.split(";", 1)[0].strip().upper()
        if not cmd:
            return {}
        code = cmd.split(None, 1)[0]
        words = dict(_WORD_RE.findall(cmd[len(code) :]))
        changes = {}

        if code in ("G0", "G1"):
            for axis in ("X", "Y", "Z"):
                if axis in words:
                    value = float(words[axis])
                    if self._relative_xyz:
                        value += self.state.get(axis.lower()) or 0.0
                    changes[axis.lower()] = value
            if "E" in words:
                value = float(words["E"])
                if self._relative_e:
                    value += self.state.get("e") or 0.0
                changes["e"] = value
            if "F" in words:
                changes["feedrate"] = float(words["F"])
        elif code == "G92":
            for axis in ("X", "Y", "Z", "E"):
                if axis in words:
                    changes[axis.lower()] = float(words[axis])
        elif code == "G28":
            # Homed axes are at an unknown machine-specific origin until the next move.
            homed = [a for a in ("X", "Y", "Z") if a in cmd[3:]] or ["X", "Y", "Z"]
            for axis in homed:
                changes[axis.lower()] = None
        elif code == "G90":
            self._relative_xyz = False
            self._relative_e = False
        elif code == "G91":
            self._relative_xyz = True
            self._relative_e = True
        elif code == "M82":
            self._relative_e = False
            changes["e_mode"] = "M82"
        elif code == "M83":
            self._relative_e = True
            changes["e_mode"] = "M83"
        elif code in ("M104", "M109"):
            target = words.get("S", words.get("R"))
            if target is not None:
                changes["tool"] = float(target)
        elif code in ("M140", "M190"):
            target = words.get("S", words.get("R"))
            if target is not None:
                changes["bed"] = float(target)
        elif code in ("M106", "M107"):
            changes["fan"] = cmd
        elif code in RESTORED_COMMANDS:
            extras = dict(self.state.get("extras") or {})
            extras[code] = cmd
            changes["extras"] = extras
        elif _TOOL_RE.match(code):
            changes["tool_command"] = code

        self.state.update({k: v for k, v in changes.items() if k != "tool_command"})
        return changes


def is_tool_change(command):
    return bool(_TOOL_RE.match(command.strip().upper()))
