"""The print_recovery_pending helper other plugins ask before disturbing OctoPrint."""

import threading

import pytest

from octoprint_power_panic import controller as C
from octoprint_power_panic import plugin as plugin_module
from octoprint_power_panic.plugin import PowerPanicPlugin


class StubController:
    def __init__(self, state):
        self.state = state


def make(state):
    p = PowerPanicPlugin.__new__(PowerPanicPlugin)
    p._lock = threading.RLock()
    p._controller = StubController(state)
    return p


def test_is_exported_as_a_plugin_helper():
    helper = plugin_module.__plugin_helpers__["print_recovery_pending"]
    assert helper == plugin_module.__plugin_implementation__.print_recovery_pending


def test_idle_is_not_pending():
    # Includes a print that is simply running: OctoPrint's printer state covers that.
    assert make(C.IDLE).print_recovery_pending() is False


@pytest.mark.parametrize(
    "state",
    [C.WAITING, C.DETECTING, C.PROBING, C.READING, C.PENDING, C.RESUMING, C.STARTING, C.FAILED],
)
def test_every_recovery_state_is_pending(state):
    assert make(state).print_recovery_pending() is True
