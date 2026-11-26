#!/usr/bin/env python3
"""Schedule guarded online/final ownCloud snapshots and verified retention."""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess


ROOT = Path(__file__).resolve().parent
INSTALL = Path("/usr/local/libexec/owncloud-backintime")
STATE = Path("/var/lib/owncloud-backintime")
HISTORY = Path("/datenBackup/owncloud-root/backintime/beelink/root/1")
STAGE = Path("/daten/beelink-backups/backintime-owncloud")
SOURCES = (
    Path("/daten/beelink-runtime/rootfs/daten/owncloud/data"),
    Path("/daten/beelink-runtime/apps/owncloud/volume/apps"),
    Path("/daten/beelink-runtime/apps/owncloud/volume/config"),
    STAGE,
)
SID = re.compile(r"^\d{8}-\d{6}-\d{3}$")
RESERVE = 100 * 1024**3
PINNED = "20261126-063812-580"
RECIPIENT = "hannes.software@gmx.at"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def active(unit):
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def mounts():
    for path, uuid in (
        ("/daten", "c0c42461-ae4c-4edb-a993-bba94a05ed6c"),
        ("/datenBackup", "fe91ecb5-c1a4-4e6c-9edf-f3c9c75cffe1"),
    ):
        os.listdir(path)
        if capture("findmnt", "-rn", "-M", path, "-t", "ext4", "-o", "UUID") != uuid:
            raise RuntimeError("Unexpected or missing encrypted filesystem: " + path)
    destination = HISTORY.parents[3]
    if destination.is_symlink() or destination.stat().st_uid != 0 or \
            destination.stat().st_mode & 0o077:
        raise RuntimeError("Backup destination is not root-private")


def snapshots():
    return sorted(path.name for path in HISTORY.iterdir()
                  if SID.fullmatch(path.name) and not path.is_symlink()
                  and path.is_dir() and not (path / "failed").exists())


def dump_hash(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def verify(sid, current=False):
    if not SID.fullmatch(sid):
        raise RuntimeError("Invalid snapshot ID")
    snapshot = HISTORY / sid
    if snapshot.is_symlink() or not snapshot.is_dir() or (snapshot / "failed").exists():
        raise RuntimeError("Invalid/failed snapshot: " + sid)
    backup = snapshot / "backup"
    for source in SOURCES:
        if not (backup / source.relative_to("/")).is_dir():
            raise RuntimeError("Snapshot missing source: " + str(source))
    for relative in (
        SOURCES[0] / ".ocdata", SOURCES[2] / "config.php",
        STAGE / "containers.json", STAGE / "images.json",
    ):
        if not (backup / relative.relative_to("/")).is_file():
            raise RuntimeError("Snapshot missing required file: " + str(relative))
    saved = backup / STAGE.relative_to("/")
    expected = (saved / "owncloud.dump.sha256").read_text().split()[0]
    if dump_hash(saved / "owncloud.dump") != expected:
        raise RuntimeError("Snapshot database checksum mismatch")
    if current and (
        (saved / "captured-at").read_bytes() != (STAGE / "captured-at").read_bytes()
        or dump_hash(STAGE / "owncloud.dump") != expected
    ):
        raise RuntimeError("Snapshot does not contain this run's database capture")
    return {"snapshot": sid, "captured": (saved / "captured-at").read_text().strip(),
            "verified": dt.datetime.now().astimezone().isoformat(),
            "dump_sha256": expected}


def capacity(baseline):
    """Conservative full-file delta estimate, not the existing hardlinked tree."""
    transferred = 0
    for source in SOURCES:
        previous = HISTORY / baseline / "backup" / source.relative_to("/")
        output = capture("rsync", "-an", "--stats", "--out-format=",
                         str(source) + "/", str(previous) + "/")
        match = re.search(r"Total transferred file size:\s*([\d,]+) bytes", output)
        if not match:
            raise RuntimeError("Cannot parse rsync space estimate")
        transferred += int(match[1].replace(",", ""))
    free = shutil.disk_usage(HISTORY).free
    needed = RESERVE + transferred * 3 // 2 + 1024**3
    print(f"Space: {free / 1024**3:.1f} GiB free; "
          f"{transferred / 1024**3:.1f} GiB estimated changed files; "
          "100 GiB safety reserve.", flush=True)
    if free < needed:
        raise RuntimeError("Insufficient free space; no maintenance or automatic emergency deletion")
    return transferred


def retained(ids):
    """Union of newest snapshots on 7 dates, 4 ISO weeks and 3 months."""
    keep = set()
    for limit, period in (
        (7, lambda day: day.date()),
        (4, lambda day: day.isocalendar()[:2]),
        (3, lambda day: (day.year, day.month)),
    ):
        seen = set()
        for sid in sorted(ids, reverse=True):
            day = dt.datetime.strptime(sid[:15], "%Y%m%d-%H%M%S")
            bucket = period(day)
            if bucket not in seen and len(seen) < limit:
                seen.add(bucket)
                keep.add(sid)
    return keep


def prune_locked():
    # Never remove a snapshot potentially being read by a restore rehearsal.
    if active("owncloud-full-restore-test.service"):
        print("Retention deferred: full restore rehearsal is active.", flush=True)
        return
    records = STATE / "verified"
    ids = [path.stem for path in records.glob("*.json")
           if SID.fullmatch(path.stem) and (HISTORY / path.stem).is_dir()]
    keep = retained(ids) | {PINNED}
    if not ids:
        raise RuntimeError("No verified consistent snapshots; refusing retention")
    newest = max(ids)
    verify(newest)
    obsolete = set(ids) - keep
    preliminary = STATE / "preliminary"
    obsolete.update(path.stem for path in preliminary.glob("*.json")
                    if SID.fullmatch(path.stem) and path.stem < newest
                    and path.stem not in keep)
    config = HISTORY / newest / "config"
    if not config.is_file():
        raise RuntimeError("Saved profile missing; retention refused")
    for sid in sorted(obsolete):
        path = HISTORY / sid
        if path.is_symlink():
            raise RuntimeError("Refusing symlink during retention")
        if path.exists():
            print("Removing managed obsolete snapshot: " + sid, flush=True)
            run("backintime", "--config", str(config), "--profile", "1",
                "remove", "--skip-confirmation", sid)
            if path.exists():
                raise RuntimeError("Back In Time did not remove snapshot: " + sid)
        for directory in (records, preliminary):
            (directory / (sid + ".json")).unlink(missing_ok=True)


def prune():
    with Path("/run/lock/owncloud-backintime-online.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prune_locked()


def run_phase(name, script, argument, seconds):
    run("systemd-run", "--wait", "--collect", "--unit=owncloud-backintime-" + name,
        "--property=RuntimeMaxSec=" + str(seconds), "--property=TimeoutStopSec=180s",
        "--property=KillMode=control-group", "--property=Nice=15",
        "--property=IOSchedulingClass=idle", str(INSTALL / script), argument)


def cycle():
    mounts()
    if active("owncloud-full-restore-test.service"):
        raise RuntimeError("Backup deferred while full restore rehearsal reads backup HDD")
    # No offline work after 02:40, before the existing 03:05+ night jobs.
    now = dt.datetime.now().astimezone()
    deadline = now.replace(hour=2, minute=40, second=0, microsecond=0)
    if now.hour >= 23:
        deadline += dt.timedelta(days=1)
    if not (now.hour >= 23 or now < deadline):
        raise RuntimeError("Outside backup window 23:00-02:40; ownCloud remains online")
    before = snapshots()
    if not before:
        raise RuntimeError("No existing baseline; refusing unattended initial full backup")
    # Delete only already-obsolete managed snapshots, never to force an oversized delta.
    prune()
    baseline = snapshots()[-1]
    capacity(baseline)
    now = dt.datetime.now().astimezone()
    seconds = min(3 * 3600, int((deadline - now).total_seconds()) - 60)
    if seconds < 60:
        raise RuntimeError("Insufficient time left for online preliminary copy")
    try:
        run_phase("preliminary-auto", "backintime-online-beelink.sh", "--scheduled", seconds)
    finally:
        # Record new completed online snapshots even if BIT reported a nonzero exit.
        # They must never be treated as consistent.
        for sid in set(snapshots()) - set(before):
            write_json(STATE / "preliminary" / (sid + ".json"), {"snapshot": sid})
    preliminary = snapshots()[-1]
    if preliminary in before:
        raise RuntimeError("Online phase did not create a new usable snapshot")
    if dt.datetime.now().astimezone() >= deadline:
        raise RuntimeError("Online copy exceeded window; no maintenance enabled")
    if capacity(preliminary) > 1024**3:
        raise RuntimeError("More than 1 GiB changed after online phase; offline pass deferred")
    if dt.datetime.now().astimezone() >= deadline:
        raise RuntimeError("Capacity check exceeded window; no maintenance enabled")
    # The final script acquires lifecycle locks non-blockingly before maintenance.
    run_phase("final-auto", "backintime-beelink.sh", "--backup", 15 * 60)
    if dt.datetime.now().astimezone() >= deadline + dt.timedelta(minutes=18):
        raise RuntimeError("Final phase exceeded its permitted runtime")
    final = snapshots()[-1]
    if final == preliminary:
        raise RuntimeError("Final phase did not create a new snapshot")
    record = verify(final, current=True)
    write_json(STATE / "verified" / (final + ".json"), record)
    write_json(STATE / "last-success.json", record)
    prune()


def alert(status, message):
    previous_path = STATE / "alert.json"
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    if previous.get("status") != status or previous.get("message") != message:
        run("/usr/bin/mail", "-s", "[beelink] ownCloud Back In Time: " + status,
            RECIPIENT, input=message + "\n", text=True)
        write_json(previous_path, {"status": status, "message": message})


def monitor():
    problems = []
    try:
        mounts()
        free = shutil.disk_usage(HISTORY).free
        if free < RESERVE:
            problems.append("Less than 100 GiB free on the backup disk")
        latest = json.loads((STATE / "last-success.json").read_text())
        saved = dt.datetime.fromisoformat(latest["captured"])
        if (dt.datetime.now().astimezone() - saved).total_seconds() > 30 * 3600:
            problems.append("Last consistent backup is older than 30 hours")
        sid = latest["snapshot"]
        if not SID.fullmatch(sid) or not (HISTORY / sid).is_dir():
            problems.append("Last verified snapshot is missing")
        report_path = STATE / "run.json"
        if report_path.exists():
            report = json.loads(report_path.read_text())
            if report["result"] != "SUCCESS":
                problems.append(report["error"])
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        problems.append(str(error))
    alert("FAILED" if problems else "OK", "\n".join(problems) or "Consistent backup is current; disk reserve OK.")
    if problems:
        raise RuntimeError("; ".join(problems))


def units():
    command = "/usr/bin/python3 " + str(INSTALL / Path(__file__).name)
    result = {}
    for suffix, calendar, option in (
        ("", "*-*-* 23:00:00", "--run"),
        ("-monitor", "*-*-* *:30:00", "--monitor"),
    ):
        name = "owncloud-backintime" + (suffix or "-cycle")
        result[name + ".service"] = f"""[Unit]
Description=Guarded ownCloud Back In Time{suffix}
After=docker.service

[Service]
Type=oneshot
ExecStart={command} {option}
Environment=HOME=/root LC_ALL=C LANG=C
UMask=0077
Nice=15
IOSchedulingClass=idle
CPUWeight=10
IOWeight=10
TimeoutStartSec={"120s" if suffix else "0"}
TimeoutStopSec=180s
"""
        result[name + ".timer"] = f"""[Unit]
Description=Schedule ownCloud Back In Time{suffix}

[Timer]
OnCalendar={calendar}
Persistent=false
AccuracySec=30s

[Install]
WantedBy=timers.target
"""
    return result


def install():
    mounts()
    for command in ("backintime", "docker", "rsync", "mail", "systemd-run",
                    "systemd-analyze", "curl"):
        if not shutil.which(command):
            raise RuntimeError("Missing command: " + command)
    INSTALL.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in (Path(__file__).name, "backintime-beelink.sh", "backintime-online-beelink.sh"):
        source = ROOT / name
        if source.resolve() != (INSTALL / name).resolve():
            run("install", "-o", "root", "-g", "root", "-m", "0700",
                str(source), str(INSTALL / name))
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    for name in ("verified", "preliminary"):
        (STATE / name).mkdir(mode=0o700, exist_ok=True)
    if not (STATE / "last-success.json").exists():
        # This is the explicitly verified consistent baseline, not an arbitrary GUI backup.
        record = verify(PINNED)
        write_json(STATE / "verified" / (PINNED + ".json"), record)
        write_json(STATE / "last-success.json", record)
    latest = json.loads((STATE / "last-success.json").read_text())["snapshot"]
    verify(latest)
    capacity(latest)
    # Do not silently remove an unrelated user's cron profile.
    for user in ("hannes", "root"):
        cron = subprocess.run(["crontab", "-u", user, "-l"], capture_output=True, text=True)
        if cron.returncode not in (0, 1):
            raise RuntimeError("Cannot inspect crontab: " + user + ": " + cron.stderr)
        if cron.returncode == 0:
            lines = cron.stdout.splitlines(keepends=True)
            matching = [line for line in lines if not line.lstrip().startswith("#")
                        and re.search(r"\bbackintime\s+backup\s+--background\b", line)]
            if matching:
                (STATE / (user + "-crontab-before.txt")).write_text(cron.stdout)
                run("crontab", "-u", user, "-", input="".join(
                    "# Disabled: replaced by guarded owncloud-backintime-cycle.timer\n# " + line
                    if line in matching else line for line in lines), text=True)
    for name, content in units().items():
        path = Path("/etc/systemd/system") / name
        path.write_text(content)
        path.chmod(0o644)
    run("systemd-analyze", "verify", *(
        str(Path("/etc/systemd/system") / name) for name in units()))
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "owncloud-backintime-cycle.timer",
        "owncloud-backintime-monitor.timer")
    run("systemctl", "start", "owncloud-backintime-monitor.service")
    run("systemctl", "list-timers", "owncloud-backintime*", "--no-pager")
    print("Installed. First scheduled run at 23:00; no backup started by installation.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    for option in ("install", "run", "monitor"):
        group.add_argument("--" + option, action="store_true")
    args = parser.parse_args()
    if os.getuid() != 0 or socket.gethostname().split(".")[0] != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    os.umask(0o077)
    os.environ.update(HOME="/root", LC_ALL="C", LANG="C",
                      PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    if args.install:
        install()
    elif args.monitor:
        monitor()
    else:
        with (STATE / "cycle.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                cycle()
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
                write_json(STATE / "run.json", {"result": "FAILED", "error": str(error)})
                alert("FAILED", str(error))
                raise
            write_json(STATE / "run.json", {"result": "SUCCESS"})
            monitor()


if __name__ == "__main__":
    main()
