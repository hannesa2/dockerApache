#!/usr/bin/env python3
"""Fence known migrated Latitude jobs while preserving OS and unrelated jobs."""

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import socket
import subprocess


MARKER = "# Disabled after beelink cutover: "
COMMANDS = (
    "/daten/sicherung.postgresql/postgresql-backup.sh",
    "/home/hannes/git/dockerApache/apache/sync-to-mirror.sh",
    "/home/hannes/git/dockerApache/nextcloud/sync-to-mirror.sh",
    "/home/hannes/bin/systems-health-check.sh",
    "/home/hannes/bin/gitlab-mac-backup.sh",
    "/usr/local/sbin/owncloud-mirror-sync",
    "/usr/local/sbin/mxserver-mirror-sync",
    "/home/hannes/git/dockerApache/monitoring/sync-matomo.py",
)
CRON_FILES = ("matomo-mirror-sync", "mxserver-mirror-sync", "owncloud-mirror-sync",
              "nextcloud-mirror-sync")


def disabled(text):
    lines = []
    count = 0
    for line in text.splitlines(keepends=True):
        if (line.strip() and not line.lstrip().startswith("#")
                and any(command in line for command in COMMANDS)):
            lines.append(MARKER + line)
            count += 1
        else:
            lines.append(line)
    return "".join(lines), count


def crontab(user):
    result = subprocess.run(["crontab", "-u", user, "-l"], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 1 and "no crontab for" in result.stderr:
        return ""
    raise RuntimeError("Cannot inspect " + user + " crontab: " + result.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disable", required=True, action="store_true")
    parser.parse_args()
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "latitude":
        raise RuntimeError("Run as root directly on Latitude")
    os.umask(0o077)
    backup = Path("/var/backups/latitude-cutover-jobs") / datetime.datetime.now().strftime(
        "%Y%m%dT%H%M%S%f")
    backup.mkdir(parents=True, mode=0o700)
    changes = {}
    remaining = {}
    for user in ("hannes", "root"):
        original = crontab(user)
        (backup / (user + ".crontab")).write_text(original)
        updated, count = disabled(original)
        if count:
            subprocess.run(["crontab", "-u", user, "-"], input=updated, text=True, check=True)
        if crontab(user) != updated:
            raise RuntimeError("Crontab verification failed: " + user)
        changes[user + ".crontab"] = count
        remaining[user] = [
            line for line in updated.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
            and not re.match(r"^\s*[A-Za-z_][A-Za-z_0-9]*\s*=", line)]
    for name in CRON_FILES:
        path = Path("/etc/cron.d") / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Unexpected cron path: " + str(path))
        original = path.read_text()
        (backup / ("cron.d-" + name)).write_text(original)
        updated, count = disabled(original)
        if updated != original:
            temporary = path.with_name(path.name + ".cutover-tmp")
            temporary.write_text(updated)
            temporary.chmod(path.stat().st_mode & 0o777)
            temporary.replace(path)
        if disabled(path.read_text())[1]:
            raise RuntimeError("Active migration command remains: " + str(path))
        changes[str(path)] = count
    # Preserve private full inventories for any additional root-only jobs.
    for name, command in (
            ("timers.txt", ["systemctl", "list-timers", "--all", "--no-pager"]),
            ("processes.txt", ["ps", "-eo", "pid,user,args"])):
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, check=True)
        (backup / name).write_text(result.stdout)
    subprocess.run(["systemctl", "disable", "--now", "snap.certbot.renew.timer"], check=True)
    (backup / "report.json").write_text(json.dumps({
        "disabled_entries": changes, "remaining_crontab_jobs": remaining,
        "beelink_jobs_installed": False,
    }, indent=2) + "\n")
    print("Known migrated Latitude schedules disabled. Private rollback inventory: " + str(backup))
    for name, count in changes.items():
        print(f"  {name}: {count} entries disabled")
    for user, entries in remaining.items():
        print(f"Remaining {user} crontab jobs (review separately): {len(entries)}")
        for line in entries:
            print("  " + line)
    print("OS maintenance and unrelated jobs preserved. No beelink jobs started.")


if __name__ == "__main__":
    main()
