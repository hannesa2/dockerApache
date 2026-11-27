#!/usr/bin/env python3
"""Compare beelink's installed helpers with their repository sources."""
import hashlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = Path("/usr/local/libexec")
STATUS_HELPER = Path("/usr/local/bin/beelink-job-status")

GROUPS = {
    "beelink-jobs": {
        "beelink-jobs.py": "monitoring/beelink-jobs.py",
        "send-health-mail.py": "monitoring/send-health-mail.py",
        "postgresql-backup.sh": "monitoring/postgresql-backup.sh",
        "sync-matomo.py": "monitoring/sync-matomo.py",
        "owncloud/sync-beelink.sh": "owncloud/sync-beelink.sh",
        "mxserver/sync-beelink.sh": "mxserver/sync-beelink.sh",
    },
    "beelink-staging": {
        name: "monitoring/" + name for name in (
            "control-beelink-staging.py", "start-beelink-apps.py",
            "start-beelink-frontend.py", "start-beelink-artifacts.py",
            "start-beelink-dns.py",
        )
    },
    "beelink-config-backup": {
        name: "monitoring/" + name for name in (
            "backup-beelink-config.py", "control-beelink-staging.py",
        )
    },
    "owncloud-backintime": {
        **{name: "owncloud/" + name for name in (
            "schedule-backintime-beelink.py", "backintime-beelink.sh",
            "backintime-online-beelink.sh", "backup-dev-wordpress-beelink.sh",
        )},
    },
}
OPTIONAL = {
    "beelink-config-backup/send-health-mail.py": "monitoring/send-health-mail.py",
    "owncloud-backintime/send-health-mail.py": "monitoring/send-health-mail.py",
}
STANDALONE = {
    "activate-beelink-operations.py": "monitoring/activate-beelink-operations.py",
    "beelink-backup-disk-idle.py": "monitoring/beelink-backup-disk-idle.py",
    "publish-beelink-java-lan.py": "monitoring/publish-beelink-java-lan.py",
    "renew-beelink-certificates.py": "monitoring/renew-beelink-certificates.py",
    "send-health-mail.py": "monitoring/send-health-mail.py",
}
# The retired migration helper is intentionally pinned, not updated from Git.
PINNED = {LIBEXEC / "beelink-staging/prepare-beelink-gitlab-runner.py"}


def hostname():
    return subprocess.check_output(["hostname", "-s"], text=True).strip().lower()


def check():
    if hostname() != "beelink":
        return 0
    result = subprocess.run(
        ["sudo", "-n", "/usr/bin/find", str(LIBEXEC), "(",
         "-type", "d", "-o", "-type", "l", "-o", "(", "-type", "f",
         "(", "-name", "*.py", "-o", "-name", "*.sh", ")", ")",
         ")", "-printf", "%y\t%p\\0"], capture_output=True, check=True,
    )
    discovered = {}
    for entry in result.stdout.split(b"\0"):
        if entry:
            kind, path = entry.split(b"\t", 1)
            discovered[Path(path.decode())] = kind
    expected = {}
    warnings = False
    for group, entries in GROUPS.items():
        directory = LIBEXEC / group
        if directory not in discovered:
            print(f"WARNING: Installed helper directory missing: {directory}", file=sys.stderr)
            warnings = True
        elif discovered[directory] == b"l":
            print(f"WARNING: Installed helper directory is a symlink: {directory}", file=sys.stderr)
            warnings = True
        elif discovered.get(directory) == b"d":
            expected.update({directory / name: ROOT / source for name, source in entries.items()})
    for name, source in STANDALONE.items():
        path = LIBEXEC / name
        if path in discovered:
            expected[path] = ROOT / source
    for name, source in OPTIONAL.items():
        path = LIBEXEC / name
        if path in discovered:
            expected[path] = ROOT / source
    if LIBEXEC / "beelink-jobs" in discovered:
        expected[STATUS_HELPER] = ROOT / "monitoring/beelink-jobs.py"

    for path, kind in sorted(discovered.items()):
        if path in PINNED:
            continue
        if (kind != b"d" and path.suffix in (".py", ".sh") and path not in expected
                and (any(LIBEXEC / group in path.parents for group in GROUPS)
                     or path.parent == LIBEXEC and "beelink" in path.name)):
            print(f"WARNING: Installed helper has no repository mapping: {path}", file=sys.stderr)
            warnings = True
    for installed, source in expected.items():
        if not source.is_file():
            print(f"WARNING: Repository source missing: {source} (installed: {installed})",
                  file=sys.stderr)
            warnings = True
            continue
        if discovered.get(installed) == b"l":
            print(f"WARNING: Installed helper is a symlink: {installed}", file=sys.stderr)
            warnings = True
            continue
        comparison = subprocess.run(["sudo", "-n", "/usr/bin/sha256sum", "--", str(installed)],
                                    capture_output=True, text=True)
        if comparison.returncode:
            print(f"WARNING: Cannot check installed helper {installed}: "
                  f"{comparison.stderr.strip()}", file=sys.stderr)
            warnings = True
            continue
        installed_hash = comparison.stdout.split(maxsplit=1)[0]
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if installed_hash != source_hash:
            print(f"WARNING: Installed helper differs: {installed} <- {source}",
                  file=sys.stderr)
            warnings = True
    if warnings:
        print("WARNING: Review differences and use the appropriate installer; "
              "Git does not deploy installed helpers.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(check())
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError, IndexError) as error:
        print(f"ERROR: Installed helper check failed: {error}", file=sys.stderr)
        print("HINT: On beelink, run 'sudo -v' in this terminal and retry.",
              file=sys.stderr)
        sys.exit(2)
