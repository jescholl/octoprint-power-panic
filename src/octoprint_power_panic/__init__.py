"""OctoPrint Power Panic.

The entry point is this package (not ``.plugin``): OctoPrint finds templates/ and
static/ next to the entry point only for a package ``__init__``. The metadata below
must stay literal assignments here, because OctoPrint reads it by parsing this
file's source; without ``__plugin_pythoncompat__`` it assumes Python 2 only.
"""

__plugin_name__ = "Power Panic"
__plugin_pythoncompat__ = ">=3.10,<4"

from .plugin import __plugin_hooks__, __plugin_implementation__  # noqa: E402, F401
