#!/usr/bin/env python3
"""Synchronize beelink artifact files to the mac2016 mirror."""

import argparse
import datetime
import fcntl
import os
from pathlib import Path, PurePosixPath
import shutil
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path.home() / "server/githubArtifactsPy"
SOURCE = "beelink-artifacts-artifacts-1"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "beelink"]
REMOTE_CHECK = """set -eu
test "$(hostname -s)" = beelink
test "$(findmnt -rn -M /daten -t ext4 -o UUID)" = c0c42461-ae4c-4edb-a993-bba94a05ed6c
test "$(docker inspect -f '{{.State.Running}}' beelink-artifacts-artifacts-1)" = true
test "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/files"}}{{.Source}}{{end}}{{end}}' beelink-artifacts-artifacts-1)" = /daten/beelink-runtime/artifacts/files
docker exec beelink-artifacts-artifacts-1 test -f /app/parameter.yml
"""


def extract(archive, destination):
    count = 0
    with tarfile.open(archive, "r:") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
                raise RuntimeError("Unsafe artifact archive member: " + member.name)
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise RuntimeError("Could not read artifact: " + member.name)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                os.utime(target, (member.mtime, member.mtime))
                count += 1
    if not count:
        raise RuntimeError("Source artifact archive is empty; refusing mirror update")
    return count


def sync(dry_run=False):
    if socket.gethostname().split(".")[0].lower() != "mac2016":
        raise RuntimeError("Run as the mac2016 user")
    target = ROOT / "files"
    if (ROOT.is_symlink() or target.is_symlink() or not target.is_dir()
            or not (ROOT / "parameter.yml").is_file()):
        raise RuntimeError("Expected existing artifact mirror and configuration")
    backups = ROOT / "sync-backups"
    if backups.is_symlink():
        raise RuntimeError("Unsafe backup directory")
    backups.mkdir(mode=0o700, exist_ok=True)
    backups.chmod(0o700)
    with (ROOT / ".beelink-sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        subprocess.run([*SSH, "sh", "-c", shlex.quote(REMOTE_CHECK)], check=True)
        with tempfile.TemporaryDirectory(prefix=".beelink-sync-", dir=ROOT) as temp:
            work = Path(temp)
            archive = work / "artifacts.tar"
            snapshot = work / "files"
            snapshot.mkdir()
            with archive.open("wb") as output:
                subprocess.run([*SSH, "docker", "exec", SOURCE, "tar",
                                "-C", "/app/files", "-cf", "-", "."],
                               stdout=output, check=True)
            count = extract(archive, snapshot)
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            options = ["--dry-run"] if dry_run else []
            backup = backups / timestamp
            if not dry_run:
                backup.mkdir(mode=0o700)
                options += ["--backup", "--backup-dir=" + str(backup)]
            subprocess.run(["/usr/local/bin/rsync", "-rt", "--checksum", "--delete",
                            "--delay-updates", "--human-readable", "--stats", *options,
                            str(snapshot) + "/", str(target) + "/"], check=True)
            print(f"{'Compared' if dry_run else 'Mirrored'} {count} artifacts from beelink"
                  + ("" if dry_run else f"; replaced/deleted files: {backup}"), flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        sync(args.dry_run)
    except (OSError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as error:
        print("ERROR: artifact mirror sync failed:", error, file=sys.stderr, flush=True)
        if not args.dry_run:
            notification = subprocess.run(
                ["/usr/bin/mail", "-s", "[mac2016] Artifact mirror sync failed",
                 "hannes.software@gmx.at"], input=str(error), text=True, check=False)
            if notification.returncode:
                print("ERROR: failure notification could not be sent", file=sys.stderr, flush=True)
        raise
