#!/usr/bin/env python3
"""Read public job timestamps and calculate expected runs from enabled cron entries."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

STATE = Path(os.environ.get(
    "SYNC_JOB_STATUS_DIR", "/home/hannes/.local/state/sync-job-notify/status"
))
JOBS = {
    "PostgreSQL backups": "sicherung.postgresql",
    "GitLab standby backup": "gitlab-mac-backup",
    "Apache mirror sync": "apache/sync-to-mirror.sh",
    "MXServer mirror sync": "mxserver-mirror-sync",
    "Nextcloud mirror sync": "nextcloud/sync-to-mirror.sh",
    "ownCloud mirror sync": "owncloud-mirror-sync",
}


def state_path(name):
    return STATE / (hashlib.sha256(name.encode()).hexdigest() + ".json")


def read_state(name):
    try:
        return json.loads(state_path(name).read_text())
    except FileNotFoundError:
        return {}


def record(name, result, started, finished):
    STATE.mkdir(parents=True, exist_ok=True)
    old = read_state(name)
    data = dict(job=name, result=result, started=started, finished=finished,
                last_success=finished if result == "SUCCESS" else old.get("last_success"))
    fd, temporary = tempfile.mkstemp(dir=STATE)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, state_path(name))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def matches(field, value, low, high, sunday=False):
    values = set()
    for part in field.split(","):
        base, _, step = part.partition("/")
        stride = int(step) if step else 1
        if stride < 1:
            raise ValueError("Invalid cron step")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            start, end = map(int, base.split("-"))
        else:
            start = int(base)
            end = high if step else start
        if not low <= start <= end <= high:
            raise ValueError("Unsupported cron field")
        values.update(range(start, end + 1, stride))
    if sunday and 7 in values:
        values.add(0)
    return value in values


def next_run(fields, now):
    candidate = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        minute, hour, day, month, weekday = fields
        dom = matches(day, candidate.day, 1, 31)
        dow = matches(weekday, (candidate.weekday() + 1) % 7, 0, 7, True)
        day_ok = dom and dow if day.startswith("*") or weekday.startswith("*") else dom or dow
        if (matches(month, candidate.month, 1, 12) and day_ok
                and matches(hour, candidate.hour, 0, 23)
                and matches(minute, candidate.minute, 0, 59)):
            return candidate.strftime("%Y-%m-%d %H:%M %z")
        candidate += dt.timedelta(minutes=1)
    return "no matching run within one year"


def report():
    cron = subprocess.run(["crontab", "-l"], text=True, capture_output=True)
    if cron.returncode and "no crontab" not in cron.stderr.lower():
        raise RuntimeError(cron.stderr.strip())
    sources = [(cron.stdout, False)]
    sources += [(path.read_text(), True) for path in sorted(Path("/etc/cron.d").glob("*"))
                if path.is_file() and not path.name.startswith(".")]
    schedules = {name: [] for name in JOBS}
    now = dt.datetime.now().astimezone()
    for text, system in sources:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" in line.split()[0]:
                continue
            parts = line.split(None, 6 if system else 5)
            if len(parts) < (7 if system else 6):
                continue
            command = parts[-1]
            for name, token in JOBS.items():
                if token in command:
                    try:
                        schedules[name].append(next_run(parts[:5], now))
                    except ValueError:
                        schedules[name].append("unknown (unsupported cron schedule)")
    print("Sync jobs on Latitude (host timezone; expected times require host/cron availability):")
    for name in JOBS:
        state = read_state(name)
        print(f"{name}\n"
              f"  Last start: {state.get('started') or 'unknown (not yet recorded)'}; "
              f"finished: {state.get('finished') or 'unknown'}\n"
              f"  Result: {state.get('result', 'unknown')}; "
              f"last success: {state.get('last_success') or 'unknown'}\n"
              f"  Next expected: {', '.join(schedules[name]) or 'not scheduled in inspected user crontab /etc/cron.d'}")
    print("Unrecorded history is not inferred from log modification times.")


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "--record":
        record(*sys.argv[2:])
    elif len(sys.argv) == 1:
        report()
    else:
        raise SystemExit("Usage: sync-job-report.py [--record name result started finished]")
