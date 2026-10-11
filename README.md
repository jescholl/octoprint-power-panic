# OctoPrint Power Panic

Resumes an OctoPrint print after a power loss: the host, the printer, or both.

On a **Prusa MK3/MK3S with firmware 3.14 or newer** it works with the firmware's own
power-panic recovery, which restores the exact position, Z height, mesh bed levelling
and print settings saved at the moment the power failed. On other printers it falls
back to a generic recovery that a person starts by hand.

> **Status: only the Prusa path is meant for real use, and it is still being tested.**
> Everything has been tested against a simulated printer (see *Testing*), not yet
> through a real power cut on real hardware.
>
> **Non-Prusa recovery is theoretical.** It has only ever run against a simulated
> generic printer. Nobody has verified it on a real non-Prusa machine, and it relies
> on assumptions that do not hold for every printer (see *Generic recovery*). Treat
> it as a starting point that you must check on your own printer, not as something
> that is known to work.

## Why the firmware matters

Since 3.14, Prusa's firmware handles a power cut during a USB print itself. It saves
the number of the last G-code line it completed, the position, mesh and settings;
when power returns it restores the print, then parks and waits for the host. But it
needs the host to take part, and OctoPrint does not: so after a power cut the printer
just shows *Print paused* with only *Stop* available. Worse, an OctoPrint plugin that
ignores this state and sends its own homing G-code erases the firmware's recovery
(any `G` line does) and prints in mid-air, 20 mm above the part.

This plugin speaks the firmware's protocol (`M79`, `D3`, `M110`, `M602`), maps the
firmware's line number back to the right place in the file, and streams the rest.

## What happens after a power cut

1. While printing, the plugin keeps a journal of which file position each G-code line
   came from. It is written several times a second, atomically, so a power cut never
   leaves it half-written.
2. When OctoPrint is back and the printer connects, the plugin asks the firmware
   whether it has a recovery pending, and reads the saved line number. Until then it
   holds back every `G` command, from anywhere, so nothing disturbs the firmware's state.
3. It then either resumes automatically, or shows a **Power Panic** panel in the
   sidebar with *Resume* and *Discard*. *Resume print* on the printer's own screen
   works too.

Automatic resume is **off by default**. When turned on, it still only happens if every
check passes:

- the firmware reports a pending recovery and that the bed stayed warm,
- the outage was shorter than the configured limit (10 minutes by default),
- no tool change (MMU) may have been interrupted,
- the file is unchanged since it was printing, and
- the journal covers the exact line the printer stopped at.

Anything else waits for a person.

A recovery is never offered after the printer reported a fault (thermal runaway,
*Printer halted*), after a person cancelled the print, or after a print finished.

## Generic recovery

Used for non-Prusa printers, Prusa firmware older than 3.14, and Prusa printers whose
firmware has nothing pending (for example the host crashed but the printer kept power,
so reconnecting reset it). It writes `recovered_<file>`: a header that reheats, lifts
Z, homes X/Y, moves back and restores the extrusion mode, fan and settings seen in
the stream, followed by the rest of the original file. It is **never started
automatically**.

Its limits:

- It resumes after the last line *sent*, not the last line the printer finished, so
  the moves the printer had buffered (up to ~16 on Marlin) are skipped.
- It assumes Z did not move while the printer was off.
- Mesh bed levelling cannot be redone over a part.
- It homes X and Y without Z (`G28 X Y` by default, configurable). That is fine on
  cartesian and CoreXY printers and wrong on deltas.

## Settings

| Setting | Default | |
|---|---|---|
| Resume automatically | off | Prusa firmware path only, and only when every check above passes |
| Longest outage to resume automatically | 10 min | |
| Keep the printer ready for | 60 min | While waiting for a person, keeps the printer's *Resume* menu available and keeps [PSU Control](https://github.com/jescholl/OctoPrint-PSUControl) from idling it off |
| Forget interrupted prints after | 24 h | |
| Retry connecting for | 30 min | OctoPrint itself only tries once at startup |
| Generic recovery: Z lift, homing command, travel feedrate, extra G-code | 5 mm, `G28 X Y`, 3000 | |

The plugin also fires events (`plugin_power_panic_recovery_pending`, `_resumed`,
`_failed`, ...) for notification plugins to pick up.

## Installing

In OctoPrint's Plugin Manager, *Get More* → *from URL*:

    https://github.com/jescholl/octoprint-power-panic/archive/refs/tags/v0.1.0.zip

Requires OctoPrint 1.11 or newer and Python 3.10 or newer. Tested on OctoPrint 1.11.8
and 2.0.0rc6.

Check that OctoPrint's *Before print job starts* script does nothing that assumes a
fresh start (like homing), because it also runs when a print resumes.

## Testing

`tests/unit` covers the logic. `tests/integration` runs a real OctoPrint against a
simulated printer (`tests/support/fake_printer.py`) that models Prusa firmware 3.14.1's
power-panic behaviour as read from its source: the 16-move planner, `INT4`, the
boot-time recovery, `M79`/`D3`/`M602`/`M603`, and a `G` line clearing the recovery.
The tests cut its power and kill OctoPrint mid-print, then check that every line of
the file was printed exactly once. The simulation is only as good as that reading of
the firmware, which is why it is not a substitute for testing on a real printer.

    python -m pip install octoprint -r tests/requirements.txt -e .
    task test        # or: python -m pytest tests
    task lint        # needs pre-commit, installed separately (e.g. pipx install pre-commit):
                     # its dependencies conflict with OctoPrint's in one virtualenv

## License

MIT.
