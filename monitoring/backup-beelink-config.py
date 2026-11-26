#!/usr/bin/env python3
"""Encrypt and restore-verify Beelink host/application configuration with Restic."""

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import tempfile
from datetime import datetime, timezone


SPEC = importlib.util.spec_from_file_location(
    "beelink_control", Path(__file__).with_name("control-beelink-staging.py"))
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)
RUNTIME = control.RUNTIME
ENV_FILE = Path("/etc/restic/beelink.env")
STATE = Path("/var/lib/beelink-config-backup")
RESTORE_PREFIX = "beelink-config-restore-"
INSTALLED = Path("/usr/local/libexec/beelink-config-backup")
UNIT_DIR = Path("/etc/systemd/system")
NAME = "beelink-config-backup"
MAIL_HELPERS = (
    Path(__file__).with_name("send-health-mail.py"),
    INSTALLED / "send-health-mail.py",
    Path("/usr/local/libexec/beelink-jobs/send-health-mail.py"),
)


def parse_environment(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE") \
                or key in values:
            raise RuntimeError("Unexpected Restic environment file setting")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not value or "\0" in value or "\n" in value or "\r" in value:
            raise RuntimeError("Empty or invalid Restic environment value")
        values[key] = value
    if set(values) != {"RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE"}:
        raise RuntimeError("Restic repository and password file settings are required")
    return values


def read_environment(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != 0 \
            or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError("Restic environment file must be a private root-owned regular file")
    return parse_environment(path.read_text())


def validate_repository(environment):
    repository = environment["RESTIC_REPOSITORY"]
    path_value = repository.removeprefix("local:")
    is_local = repository.startswith("local:") or Path(repository).is_absolute()
    if not is_local and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", repository):
        raise RuntimeError("Restic repository must be an absolute local path or remote URL")
    if is_local:
        path = Path(path_value)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise RuntimeError("Existing external local Restic repository is required")
        if path.resolve().is_relative_to(Path("/daten").resolve()) \
                or path.stat().st_dev == os.stat("/daten").st_dev:
            raise RuntimeError("Restic repository must be on a different filesystem from /daten")
    password_file = Path(environment["RESTIC_PASSWORD_FILE"])
    if (password_file.is_symlink() or not password_file.is_file()
            or password_file.stat().st_uid != 0
            or stat.S_IMODE(password_file.stat().st_mode) & 0o077):
        raise RuntimeError("Restic password file must be a private root-owned regular file")
    if is_local and password_file.resolve().is_relative_to(path.resolve()):
        raise RuntimeError("Restic password file must be stored outside the repository")
    if password_file.resolve().is_relative_to(Path("/daten").resolve()):
        raise RuntimeError("Restic password file must be stored outside /daten")


def inputs():
    paths = [
        Path("/etc"),
        Path("/usr/local/libexec/beelink-staging"),
        Path("/usr/local/libexec/beelink-jobs"),
        RUNTIME / "host-configuration",
        RUNTIME / "database-compose.json",
        RUNTIME / "apps/compose.json",
        RUNTIME / "apps/gitlab/config",
        RUNTIME / "apps/owncloud/volume/config",
        RUNTIME / "wordpress/compose.json",
        RUNTIME / "wordpress/vhosts",
        RUNTIME / "wordpress/www/dev.mxtracks/wordpress/wp-config.php",
        RUNTIME / "wordpress/www/mxdocs/wordpress/wp-config.php",
        RUNTIME / "matomo/compose.json",
        RUNTIME / "matomo/www/piwik/config/config.ini.php",
        RUNTIME / "artifacts/compose.json",
        RUNTIME / "dns/compose.json",
        RUNTIME / "dns/Corefile",
        RUNTIME / "frontend/compose.json",
    ]
    for path in paths:
        if path.is_symlink() or not path.exists():
            raise RuntimeError("Missing or symlinked configuration backup input: " + str(path))
    return paths


def manifest(paths):
    result = {}

    def add(path):
        if path.is_symlink():
            result[str(path)] = ("symlink", os.readlink(path))
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            result[str(path)] = ("file", digest.hexdigest())
        elif path.is_dir():
            for root, directories, files in os.walk(path, followlinks=False):
                root_path = Path(root)
                for name in tuple(directories):
                    child = root_path / name
                    if child.is_symlink():
                        add(child)
                        directories.remove(name)
                    elif not any(child.iterdir()):
                        result[str(child)] = ("directory", "")
                for name in files:
                    add(root_path / name)
        else:
            raise RuntimeError("Unsupported configuration entry: " + str(path))

    for value in paths:
        add(value)
    return result


def verify_restored(original, target):
    restored = {}
    for source, expected in original.items():
        relative = Path(source).relative_to("/")
        path = target / relative
        if expected[0] == "file":
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            restored[source] = ("file", digest.hexdigest())
        elif expected[0] == "symlink":
            restored[source] = ("symlink", os.readlink(path))
        else:
            restored[source] = ("directory", "")
    if restored != original:
        raise RuntimeError("Restic restore verification differs from source configuration")


def run_restic(environment, *args, **kwargs):
    env = {**os.environ, **environment}
    env.setdefault("HOME", "/root")
    return subprocess.run(["restic", *args], env=env, check=True, **kwargs)


def backup(environment):
    validate_repository(environment)
    selected = inputs()
    before = manifest(selected)
    tag = "beelink-config-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_restic(environment, "backup", "--tag", "dockerApache-beelink-config",
                "--tag", tag, *map(str, selected))
    snapshots = json.loads(run_restic(
        environment, "snapshots", "--json", "--tag", tag,
        stdout=subprocess.PIPE, text=True).stdout)
    if len(snapshots) != 1 or not re.fullmatch(r"[a-f0-9]{8,64}", snapshots[0]["id"]):
        raise RuntimeError("Restic did not create exactly one tagged configuration snapshot")
    temporary = Path(tempfile.mkdtemp(prefix=RESTORE_PREFIX, dir="/run"))
    temporary.chmod(0o700)
    try:
        run_restic(environment, "restore", snapshots[0]["id"], "--target", str(temporary))
        verify_restored(before, temporary)
    finally:
        if temporary.parent == Path("/run") and temporary.name.startswith(RESTORE_PREFIX) \
                and not temporary.is_symlink():
            shutil.rmtree(temporary)
    if STATE.is_symlink():
        raise RuntimeError("Restic backup state directory must remain root-private")
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.stat().st_uid != 0 or STATE.stat().st_mode & 0o077:
        raise RuntimeError("Restic backup state directory must remain root-private")
    report = {"snapshot": snapshots[0]["id"], "tag": tag,
              "verified_entries": len(before),
              "verified_at": datetime.now(timezone.utc).isoformat()}
    temporary_report = STATE / "latest.json.tmp"
    if temporary_report.is_symlink() or temporary_report.exists():
        raise RuntimeError("Existing Restic report temporary file; inspect before retrying")
    temporary_report.write_text(json.dumps(report, indent=2) + "\n")
    temporary_report.chmod(0o600)
    temporary_report.replace(STATE / "latest.json")
    print(f"Encrypted Restic snapshot {snapshots[0]['id']} created and restored; "
          f"{len(before)} configuration entries verified.", flush=True)


def units():
    return {
        NAME + ".service": f"""[Unit]
Description=Back up and verify encrypted beelink configuration
After=network-online.target docker.service daten.mount
OnFailure={NAME}-failure.service

[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/flock -w 14400 /run/lock/beelink-staging-control.lock /usr/bin/python3 {INSTALLED}/backup-beelink-config.py --backup-and-verify
TimeoutStartSec=6h
""",
        NAME + "-failure.service": f"""[Unit]
Description=Notify on beelink configuration backup failure

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {INSTALLED}/backup-beelink-config.py --notify-failure
""",
        NAME + ".timer": f"""[Unit]
Description=Back up beelink configuration on Sundays at 02:55

[Timer]
OnCalendar=Sun *-*-* 02:55:00
Persistent=true
Unit={NAME}.service

[Install]
WantedBy=timers.target
""",
    }


def install_weekly(environment):
    validate_repository(environment)
    report_path = STATE / "latest.json"
    if report_path.is_symlink() or not report_path.is_file():
        raise RuntimeError("Run and verify a configuration backup before installing the timer")
    report = json.loads(report_path.read_text())
    if not re.fullmatch(r"[a-f0-9]{8,64}", report.get("snapshot", "")) \
            or not isinstance(report.get("verified_entries"), int) \
            or report["verified_entries"] < 1:
        raise RuntimeError("Invalid verified configuration backup report")
    definitions = units()
    for name, text in definitions.items():
        path = UNIT_DIR / name
        if path.is_symlink() or (path.exists() and path.read_text() != text):
            raise RuntimeError("Existing configuration backup unit differs: " + name)
    if INSTALLED.is_symlink():
        raise RuntimeError("Unsafe configuration backup helper directory")
    INSTALLED.mkdir(mode=0o700, parents=True, exist_ok=True)
    if INSTALLED.stat().st_uid != 0 or INSTALLED.stat().st_mode & 0o077:
        raise RuntimeError("Configuration backup helper directory must be root-private")
    for name in ("backup-beelink-config.py", "control-beelink-staging.py",
                 "send-health-mail.py"):
        source = Path(__file__).with_name(name)
        target = INSTALLED / name
        if target.is_symlink():
            raise RuntimeError("Unsafe configuration backup helper: " + name)
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        os.chown(target, 0, 0)
        target.chmod(0o700)
    for name, text in definitions.items():
        path = UNIT_DIR / name
        path.write_text(text)
        path.chmod(0o644)
    subprocess.run(["systemd-analyze", "verify", *map(str, (
        UNIT_DIR / name for name in definitions))], check=True, env={
            **os.environ, "SYSTEMD_UNIT_PATH":
            "/etc/systemd/system:/run/systemd/generator.early:"
            "/run/systemd/generator:/run/systemd/generator.late:"})
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", NAME + ".timer"], check=True)
    subprocess.run(["systemctl", "is-active", "--quiet", NAME + ".timer"], check=True)
    print("Weekly configuration backup timer enabled; first scheduled run not yet verified.",
          flush=True)


def notify_failure():
    for helper in MAIL_HELPERS:
        if helper.is_file():
            subprocess.run(
                ["python3", str(helper),
                 "[beelink] FAILED: encrypted configuration backup",
                 "hannes.software@gmx.at"],
                input="Configuration backup failed. Inspect: journalctl -u "
                      "beelink-config-backup.service --no-pager\n",
                text=True, check=True, timeout=60)
            return
    raise RuntimeError("Mail helper is unavailable: send-health-mail.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--backup-and-verify", action="store_true")
    actions.add_argument("--install-weekly", action="store_true")
    actions.add_argument("--notify-failure", action="store_true")
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    args = parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    if args.notify_failure:
        notify_failure()
        return
    control.check_mount()
    if RUNTIME.is_symlink() or RUNTIME.stat().st_uid != 0 or RUNTIME.stat().st_mode & 0o077:
        raise RuntimeError("Expected private root-owned runtime")
    with Path("/run/lock/beelink-config-backup.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        subprocess.run(["which", "restic"], check=True, stdout=subprocess.DEVNULL)
        environment = read_environment(args.env_file)
        if args.install_weekly:
            install_weekly(environment)
        else:
            backup(environment)


if __name__ == "__main__":
    main()
