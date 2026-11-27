#!/usr/bin/env python3
"""Run the private-config check before selected Git commands."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
args = sys.argv[1:]
after_pull = bool(args and args[0] == "--after-pull")
if after_pull:
    args = args[1:]
options = []
i = 0
with_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
while i < len(args):
    arg = args[i]
    if arg in with_value:
        if i + 1 >= len(args):
            break
        options.extend(args[i:i + 2])
        i += 2
    elif arg.startswith("-"):
        options.append(arg)
        i += 1
    else:
        break

if i < len(args) and args[i] in (("pull",) if after_pull else ("status", "commit", "pull")):
    result = subprocess.run(["git", *options, "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True)
    if result.returncode == 0 and Path(result.stdout.strip()).resolve() == ROOT:
        if not after_pull:
            check = subprocess.run(["python3", str(ROOT / "private/check-private.py"), "check"])
            if check.returncode == 2:
                print("WARNING: Private configuration status could not be checked.", file=sys.stderr)
        check = subprocess.run(["python3", str(ROOT / "private/check-installed.py")])
        if check.returncode == 2:
            print("WARNING: Installed helper status could not be checked.", file=sys.stderr)
