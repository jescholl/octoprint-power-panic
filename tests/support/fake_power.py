"""Test-support plugin: a power plugin for the simulated printer.

Offers PSU Control's ``get_psu_state`` helper, reporting the fake printer's mains
(``power_off`` absent in the control directory, ``FAKE_PRINTER_DIR``). It stands for
any power plugin that follows that convention.
"""

import os

__plugin_name__ = "Fake power (test support)"
__plugin_pythoncompat__ = ">=3.7,<4"


def get_psu_state():
    return not os.path.exists(os.path.join(os.environ["FAKE_PRINTER_DIR"], "power_off"))


__plugin_helpers__ = {"get_psu_state": get_psu_state}
