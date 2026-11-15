"""Stands in for `python -m vacation_memories` in the editor's tests: records its arguments, then behaves as FAKE_JOB says.

`sleep` waits for a signal and exits like the CLI does (143 on SIGTERM); `exit:<code>` prints an error line and exits.
"""

import json
import os
import signal
import sys
import time

arguments = sys.argv[1:]
workdir = arguments[arguments.index("--workdir") + 1]
with open(os.path.join(workdir, "argv.json"), "w") as handle:
    json.dump(arguments, handle)

mode = os.environ.get("FAKE_JOB", "ok")
if mode == "sleep":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    open(os.path.join(workdir, "ready"), "w").close()  # the handler is installed: a signal now is an orderly stop
    time.sleep(120)
elif mode.startswith("exit:"):
    print("error: the reason of the failure", file=sys.stderr)
    sys.exit(int(mode.split(":")[1]))
