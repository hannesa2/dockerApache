#!/usr/bin/env python3
"""Start the mac2016 nightly keep-awake session after its scheduled wake."""

import argparse
import datetime as dt
import fcntl
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time


HOME = Path.home()
LABEL = "info.mxtracks.nightly-keep-awake"
PLIST = HOME / "Library/LaunchAgents" / (LABEL + ".plist")
INSTALLED = HOME / "bin/mac2016-night-wake.py"
LOG = HOME / "Library/Logs/mac2016-night-wake.log"
SCRIPT = """
tell application "Amphetamine"
    start new session with options {duration:0, interval:0, displaySleepAllowed:true}
    enable closed display mode
    if not (session is active) then error "Amphetamine session is not active"
    if not (closed display mode enabled) then error "Closed display mode is not enabled"
end tell
"""
SNAPSHOTS = HOME / "server/homeassistant/standby-snapshots"


def wake():
    subprocess.run(["/usr/bin/osascript", "-e", SCRIPT], check=True, timeout=90)
    print("Amphetamine session started with closed display mode.",
          flush=True)


def sleep():
    if socket.gethostname().split(".")[0].lower() != "mac2016":
        raise RuntimeError("Run as the mac2016 user")
    check_standby()
    with LOG.open("a") as log:
        subprocess.Popen(
            ["/usr/bin/python3", str(INSTALLED), "--sleep-now"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
    print("Sleep scheduled after 10 seconds.", flush=True)


def sleep_now():
    if socket.gethostname().split(".")[0].lower() != "mac2016":
        raise RuntimeError("Run as the mac2016 user")
    time.sleep(10)
    check_standby()
    subprocess.run(
        ["/usr/bin/osascript", "-e",
         'tell application "Amphetamine" to end session'],
        check=True, timeout=90)
    try:
        subprocess.run(["/usr/bin/pmset", "sleepnow"], check=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        wake()
        raise


def check_standby():
    if socket.gethostname().split(".")[0].lower() != "mac2016":
        raise RuntimeError("Run as the mac2016 user")
    now = dt.datetime.now().astimezone()
    earliest = now.replace(hour=3, minute=50, second=0, microsecond=0)
    lock_path = SNAPSHOTS / ".lock"
    if SNAPSHOTS.is_symlink() or lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError("Home Assistant standby snapshot lock is missing or unsafe")
    with lock_path.open("r") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Home Assistant standby snapshot is still running") from error
        for path in SNAPSHOTS.iterdir():
            if not re.fullmatch(r"snapshot-\d{8}T\d{6}Z", path.name) \
                    or path.is_symlink() or not path.is_dir() \
                    or not (path / ".beelink-standby-snapshot").is_file():
                continue
            timestamp = dt.datetime.strptime(path.name, "snapshot-%Y%m%dT%H%M%SZ")
            timestamp = timestamp.replace(tzinfo=dt.timezone.utc).astimezone()
            if earliest <= timestamp <= now:
                print("Home Assistant standby snapshot completed today.", flush=True)
                return
    raise RuntimeError("No completed Home Assistant standby snapshot since 03:50 today")


def install():
    if socket.gethostname().split(".")[0].lower() != "mac2016":
        raise RuntimeError("Install as the mac2016 user")
    if PLIST.exists() or PLIST.is_symlink() or INSTALLED.exists() or INSTALLED.is_symlink():
        raise RuntimeError("Existing wake agent or script; inspect before replacing")
    INSTALLED.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    definition = {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", str(INSTALLED), "--wake"],
        "StartCalendarInterval": {"Hour": 2, "Minute": 44},
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {
            "HOME": str(HOME),
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }
    installed = False
    plist_created = False
    try:
        with open(__file__, "rb") as source, INSTALLED.open("xb") as target:
            installed = True
            shutil.copyfileobj(source, target)
        INSTALLED.chmod(0o700)
        with PLIST.open("xb") as file:
            plist_created = True
            plistlib.dump(definition, file)
        PLIST.chmod(0o600)
        subprocess.run(["launchctl", "bootstrap", "gui/" + str(os.getuid()), str(PLIST)],
                       check=True)
    except (OSError, subprocess.SubprocessError):
        if plist_created:
            PLIST.unlink(missing_ok=True)
        if installed:
            INSTALLED.unlink(missing_ok=True)
        raise
    print("Nightly keep-awake agent installed for 02:44; no automatic sleep is installed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true")
    group.add_argument("--wake", action="store_true")
    group.add_argument("--sleep", action="store_true")
    group.add_argument("--sleep-now", action="store_true")
    group.add_argument("--check-standby", action="store_true")
    args = parser.parse_args()
    try:
        if args.install:
            install()
        elif args.wake:
            wake()
        elif args.sleep:
            sleep()
        elif args.check_standby:
            check_standby()
        else:
            sleep_now()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("ERROR: mac2016 nightly keep-awake failed:", error, file=sys.stderr, flush=True)
        if args.wake or args.sleep or args.sleep_now:
            try:
                subprocess.run(
                    ["/usr/bin/mail", "-s", "[mac2016] nightly keep-awake failed",
                     "hannes.software@gmx.at"], input=str(error), text=True, check=True,
                    timeout=60)
            except (OSError, subprocess.SubprocessError) as mail_error:
                print("ERROR: failure notification failed:", mail_error,
                      file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    main()
