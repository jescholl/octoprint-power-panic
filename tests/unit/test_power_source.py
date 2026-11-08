"""Where the plugin reads printer power from, with stub OctoPrint objects."""

import logging

import pytest

from octoprint_power_panic.plugin import PowerPanicPlugin


class StubSettings:
    def __init__(self, helper=""):
        self.helper = helper

    def get(self, path):
        assert path == ["power_state_helper"]
        return self.helper


class StubPluginManager:
    def __init__(self, plugins):
        # {identifier: {helper name: callable}}
        self.plugins = plugins

    @property
    def enabled_plugins(self):
        return dict.fromkeys(self.plugins)

    def get_helpers(self, name, *helpers):
        if name not in self.plugins:
            return None
        return {k: v for k, v in self.plugins[name].items() if k in helpers}


def make(plugins, helper=""):
    p = PowerPanicPlugin.__new__(PowerPanicPlugin)
    p._identifier = "power_panic"
    p._logger = logging.getLogger("test")
    p._settings = StubSettings(helper)
    p._plugin_manager = StubPluginManager(plugins)
    p._power_source = None
    return p


def test_reads_psu_control():
    p = make({"psucontrol": {"get_psu_state": lambda: False}})
    assert p._read_power() is False


def test_prefers_psu_control_over_other_plugins_offering_the_helper():
    p = make(
        {
            "aaa_power": {"get_psu_state": lambda: True},
            "psucontrol": {"get_psu_state": lambda: False},
        }
    )
    assert p._read_power() is False


def test_any_plugin_offering_the_helper_works():
    p = make({"tasmota_direct": {"get_psu_state": lambda: True}, "other": {"unrelated": lambda: 1}})
    assert p._read_power() is True


def test_a_configured_helper_is_used_instead():
    p = make(
        {"psucontrol": {"get_psu_state": lambda: True}, "smartplug": {"is_on": lambda: False}},
        helper="smartplug:is_on",
    )
    assert p._read_power() is False


def test_a_configured_plugin_without_a_helper_name_uses_get_psu_state():
    p = make({"smartplug": {"get_psu_state": lambda: True}}, helper="smartplug")
    assert p._read_power() is True


def test_a_configured_helper_that_does_not_exist_is_unknown():
    p = make({"psucontrol": {"get_psu_state": lambda: True}}, helper="smartplug:is_on")
    assert p._read_power() is None


def test_no_power_plugin_is_unknown():
    assert make({"other": {}})._read_power() is None


@pytest.mark.parametrize(
    "value, expected", [(1, True), (0, False), ("on", None), (None, None), (2, None)]
)
def test_only_booleans_or_0_1_count(value, expected):
    # PSU Control's GPIO sensing reports 0/1.
    p = make({"psucontrol": {"get_psu_state": lambda: value}})
    assert p._read_power() is expected


def test_a_failing_helper_is_unknown():
    def broken():
        raise RuntimeError("Home Assistant unreachable")

    assert make({"psucontrol": {"get_psu_state": broken}})._read_power() is None
