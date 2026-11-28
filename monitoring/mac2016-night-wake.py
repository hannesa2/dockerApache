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
SLEEP_STATE = HOME / ".local/state/mac2016-night-sleep.json"
WATCH_LABEL = "info.mxtracks.post-jobs-sleep"
SCRIPT = """
tell application "Amphetamine"
    start new session with options {duration:0, interval:0, displaySleepAllowed:true}
    enable closed display mode
    if not (session is active) then error "Amphetamine session is not active"
    if not (closed display mode enabled) then error "Closed display mode is not enabled"
end tell
"""
SNAPSHOTS = HOME / "server/homeassistant/standby-snapshots"
MAIL_HELPERS = (
    Path(__file__).with_name("send-health-mail.py"),
    HOME / "bin/send-health-mail.py",
)


def wake():
    SLEEP_STATE.unlink(missing_ok=True)
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
    if not idle_background_wake():
         print("Keeping mac2016 awake: user activity or wake state is not safely idle.", flush=True)
         return
    try:
        SLEEP_STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        SLEEP_STATE.write_text(dt.datetime.now().astimezone().isoformat())
        SLEEP_STATE.chmod(0o600)
        subprocess.run(["/usr/bin/pmset", "sleepnow"], check=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        SLEEP_STATE.unlink(missing_ok=True)
        wake()
        raise


def idle_background_wake():
    output = subprocess.check_output(
        ["/usr/sbin/ioreg", "-c", "IOHIDSystem"], text=True, timeout=15)
    idle = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', output)
    if idle is None or int(idle[1]) < 30 * 60 * 10**9:
        return False
    assertions = subprocess.check_output(
        ["/usr/bin/pmset", "-g", "assertions"], text=True, timeout=15)
    for key in ("UserIsActive", "PreventSystemSleep", "PreventUserIdleSystemSleep"):
        match = re.search(r"^\s*" + key + r"\s+(\d+)\s*$", assertions, re.MULTILINE)
        if match is None or match[1] != "0":
            return False
    return True


def sleep_watchdog():
    if socket.gethostname().split(".")[0].lower() != "mac2016":
        raise RuntimeError("Run as the mac2016 user")
    if not SLEEP_STATE.exists():
        return
    requested = dt.datetime.fromisoformat(SLEEP_STATE.read_text())
    now = dt.datetime.now().astimezone()
    if requested.date() != now.date() or now.time() < dt.time(4, 50):
        return
    # A full/user wake cancels automatic resleep for the remainder of this session.
    output = subprocess.check_output(
        ["/usr/bin/pmset", "-g", "log"], text=True, timeout=30)
    events = []
    for line in output.splitlines():
        if not re.search(r"\b(?:Wake|DarkWake)\s+from |\bDarkWake to FullWake\b", line):
            continue
        stamp = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4})", line)
        if stamp:
            when = dt.datetime.strptime(stamp[1], "%Y-%m-%d %H:%M:%S %z")
            if when > requested:
                events.append(line)
    if not events:
        return
    if any(not re.search(r"\bDarkWake\s+from ", line) for line in events):
        SLEEP_STATE.unlink()
        print("Automatic resleep cancelled after a full/user wake.", flush=True)
        return
    if not idle_background_wake():
        print("Automatic resleep deferred: user activity or sleep assertion.", flush=True)
        return
    check_standby()
    print("Returning idle mac2016 to sleep after background wake.", flush=True)
    subprocess.run(["/usr/bin/pmset", "sleepnow"], check=True, timeout=60)


def install_sleep_watchdog():
    if socket.gethostname().split(".")[0].lower() != "mac2016":
        raise RuntimeError("Install as the mac2016 user")
    path = HOME / "Library/LaunchAgents" / (WATCH_LABEL + ".plist")
    if path.exists() or path.is_symlink():
        raise RuntimeError("Existing sleep watchdog; inspect before replacing")
    definition = {
        "Label": WATCH_LABEL,
        "ProgramArguments": ["/usr/bin/python3", str(INSTALLED), "--sleep-watchdog"],
        "StartInterval": 300,
        "StandardOutPath": str(LOG), "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {"HOME": str(HOME),
                                 "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }
    with path.open("xb") as stream:
        plistlib.dump(definition, stream)
    path.chmod(0o600)
    try:
        subprocess.run(["launchctl", "bootstrap", "gui/" + str(os.getuid()), str(path)],
                       check=True)
    except (OSError, subprocess.SubprocessError):
        path.unlink()
        raise
    print("Installed idle-only post-jobs sleep watchdog; no sleep requested now.")


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
        helper = Path(__file__).with_name("send-health-mail.py")
        if helper.is_file():
            shutil.copyfile(helper, HOME / "bin/send-health-mail.py")
            (HOME / "bin/send-health-mail.py").chmod(0o700)
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
    group.add_argument("--sleep-watchdog", action="store_true")
    group.add_argument("--install-sleep-watchdog", action="store_true")
    args = parser.parse_args()
    try:
        if args.install_sleep_watchdog:
            install_sleep_watchdog()
        elif args.sleep_watchdog:
            sleep_watchdog()
        elif args.install:
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
            for helper in MAIL_HELPERS:
                if helper.is_file():
                    try:
                        subprocess.run(
                            ["python3", str(helper), "[mac2016] nightly keep-awake failed",
                             "hannes.software@gmx.at"], input=str(error),
                            text=True, check=True, timeout=60)
                    except (OSError, subprocess.SubprocessError) as mail_error:
                        print("ERROR: failure notification failed:", mail_error,
                              file=sys.stderr, flush=True)
                    break
            else:
                print("ERROR: failure notification helper missing", file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    main()
