"""Live-test helper: plant a journal that looks like OctoPrint died mid-print.

Run it on the OctoPrint host (inside the container, if OctoPrint runs in one), then
restart OctoPrint. On startup the plugin finds a journal still marked "printing" and
treats it as a lost process, exactly as after a real host power cut. Only the plugin's
data file is written; nothing is sent to the printer.

    python3 plant_journal.py FILE.gcode midway     # resume point half way through the file
    python3 plant_journal.py FILE.gcode finished   # last line journaled = end of file

FILE.gcode is a path relative to OctoPrint's uploads folder. Use --basedir if
OctoPrint's base directory is not ~/.octoprint.

What to expect: "finished" is dropped at startup without reconnecting. "midway"
reconnects (subject to the power checks and backoff) and offers a recovery at that
position. Only do this with the printer idle and its bed clear, and discard the
recovery from the UI afterwards.
"""

import argparse
import json
import os
import sys
import time

try:
    from octoprint_power_panic.journal import VERSION
except ImportError:  # run with a bare python3, outside OctoPrint's environment
    VERSION = 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("gcode", help="file path relative to OctoPrint's uploads folder")
    parser.add_argument("where", choices=["midway", "finished"])
    parser.add_argument("--basedir", default=os.path.expanduser("~/.octoprint"))
    parser.add_argument(
        "--force", action="store_true", help="overwrite a journal still marked printing"
    )
    args = parser.parse_args()

    src = os.path.join(args.basedir, "uploads", args.gcode)
    data_dir = os.path.join(args.basedir, "data", "power_panic")
    target = os.path.join(data_dir, "journal.json")

    if os.path.exists(target) and not args.force:
        with open(target) as f:
            status = json.load(f).get("status")
        if status in ("printing", "interrupted"):
            sys.exit(
                f"{target} is {status!r}: a real print may need it. Use --force to replace it."
            )

    st = os.stat(src)
    last = st.st_size if args.where == "finished" else st.st_size // 2
    now = time.time()
    journal = {
        "version": VERSION,
        "status": "printing",
        "job": {
            "path": args.gcode,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "started": now - 600,
        },
        "updated": now,
        "interruption": None,
        "state": {},
        "lines": [[100, last - 200], [101, last - 100], [102, last]],
        "tools": [],
    }
    os.makedirs(data_dir, exist_ok=True)
    tmp = os.path.join(data_dir, ".journal-test")
    with open(tmp, "w") as f:
        json.dump(journal, f)
    os.replace(tmp, target)
    print(f"planted {args.where} journal for {args.gcode}: last position {last} of {st.st_size}")


if __name__ == "__main__":
    main()
