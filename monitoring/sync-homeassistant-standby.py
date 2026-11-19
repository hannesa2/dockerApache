#!/usr/bin/env python3
"""Keep a private Home Assistant recovery snapshot on mac2016."""

import argparse
import datetime
import fcntl
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile


HOME = Path.home()
ROOT = HOME / "server/homeassistant"
SNAPSHOTS = ROOT / "standby-snapshots"
LABEL = "info.mxtracks.homeassistant-standby"
PLIST = HOME / "Library/LaunchAgents" / (LABEL + ".plist")
INSTALLED = HOME / "bin/sync-homeassistant-standby.py"
LOG = HOME / "Library/Logs/homeassistant-standby.log"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "beelink"]
REMOTE_DB = """import os, sqlite3, sys, tempfile
fd, path = tempfile.mkstemp(suffix=".db")
os.close(fd)
try:
    source = sqlite3.connect("file:/config/home-assistant_v2.db?mode=ro", uri=True)
    target = sqlite3.connect(path)
    try:
        source.backup(target)
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite snapshot integrity check failed")
    finally:
        target.close()
        source.close()
    with open(path, "rb") as data:
        while chunk := data.read(1048576):
            sys.stdout.buffer.write(chunk)
finally:
    os.unlink(path)
"""


def command(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def extract_config(archive, target):
    with tarfile.open(archive, "r:") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
                raise RuntimeError("Unsafe member in Home Assistant snapshot: " + member.name)
            destination = target.joinpath(*path.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = tar.extractfile(member)
                if source is None:
                    raise RuntimeError("Cannot read snapshot file: " + member.name)
                with source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(0o600)


def sync():
    if socket.gethostname().split(".")[0].lower() != "mac2016":
        raise RuntimeError("Run as the mac2016 user")
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    SNAPSHOTS.mkdir(mode=0o700, exist_ok=True)
    if SNAPSHOTS.is_symlink() or not SNAPSHOTS.is_dir():
        raise RuntimeError("Unsafe snapshot directory")
    SNAPSHOTS.chmod(0o700)
    with (SNAPSHOTS / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix=".pending-", dir=SNAPSHOTS) as work:
            work = Path(work)
            config = work / "config"
            config.mkdir(mode=0o700)
            archive = work / "config.tar"
            with archive.open("wb") as output:
                command(*SSH, "docker", "exec", "homeassistant", "tar", "-C", "/config",
                        "--exclude=./home-assistant_v2.db*",
                        "--exclude=./home-assistant.log*", "-cf", "-", ".",
                        stdout=output)
            extract_config(archive, config)
            archive.unlink()
            database = config / "home-assistant_v2.db"
            with database.open("wb") as output:
                command(*SSH, "docker", "exec", "homeassistant", "python3", "-c",
                        shlex.quote(REMOTE_DB), stdout=output)
            database.chmod(0o600)
            if not (config / "configuration.yaml").is_file() or not (config / ".storage").is_dir():
                raise RuntimeError("Home Assistant configuration snapshot incomplete")
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Received SQLite database failed integrity check")
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            final = SNAPSHOTS / ("snapshot-" + timestamp)
            if final.exists():
                raise RuntimeError("Snapshot timestamp already exists")
            (config / ".beelink-standby-snapshot").write_text("managed snapshot\n")
            config.rename(final)
            previous = sorted(p for p in SNAPSHOTS.iterdir()
                              if re.fullmatch(r"snapshot-\d{8}T\d{6}Z", p.name)
                              and p.is_dir() and not p.is_symlink()
                              and (p / ".beelink-standby-snapshot").is_file())
            for old in previous[:-7]:
                shutil.rmtree(old)
            print("Home Assistant recovery snapshot saved:", final, flush=True)


def install():
    if socket.gethostname().split(".")[0].lower() != "mac2016":
        raise RuntimeError("Install as the mac2016 user")
    if PLIST.exists() or PLIST.is_symlink() or INSTALLED.exists() or INSTALLED.is_symlink():
        raise RuntimeError("Existing scheduled job or installed script; inspect before replacing")
    sync()
    INSTALLED.parent.mkdir(mode=0o700, exist_ok=True)
    shutil.copyfile(__file__, INSTALLED)
    INSTALLED.chmod(0o700)
    plist = {
        "Label": LABEL, "ProgramArguments": [sys.executable, str(INSTALLED), "--sync"],
        "StartCalendarInterval": {"Hour": 3, "Minute": 50},
        "StandardOutPath": str(LOG), "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {"HOME": str(HOME), "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }
    with PLIST.open("xb") as file:
        plistlib.dump(plist, file)
    PLIST.chmod(0o600)
    try:
        command("launchctl", "bootstrap", "gui/" + str(os.getuid()), str(PLIST))
    except subprocess.CalledProcessError:
        PLIST.unlink()
        INSTALLED.unlink()
        raise
    print("Daily Home Assistant snapshot scheduled for 03:50 on mac2016.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true")
    group.add_argument("--sync", action="store_true")
    args = parser.parse_args()
    try:
        install() if args.install else sync()
    except (OSError, RuntimeError, subprocess.SubprocessError, sqlite3.Error, tarfile.TarError) as error:
        print("ERROR: Home Assistant standby snapshot failed:", error, file=sys.stderr, flush=True)
        if args.sync:
            mail = subprocess.run(["/usr/bin/mail", "-s", "[mac2016] Home Assistant standby snapshot failed",
                                   "hannes.software@gmx.at"], input=str(error), text=True, check=False)
            if mail.returncode:
                print("ERROR: failure notification could not be sent", file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    os.umask(0o077)
    main()
