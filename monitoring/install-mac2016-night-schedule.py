#!/usr/bin/env python3
"""Install the guarded mac2016 nightly sleep check and revised beelink timers."""

import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parent
UNIT_DIR = Path("/etc/systemd/system")
INSTALLED_JOBS = Path("/usr/local/libexec/beelink-jobs")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load installer dependency: " + filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jobs = load("night_schedule_jobs", "beelink-jobs.py")
renewal = load("night_schedule_renewal", "renew-beelink-certificates.py")
backup = load("night_schedule_backup", "backup-beelink-config.py")

OLD_CERTIFICATE_TIMER = """[Unit]
Description=Check beelink TLS renewal twice daily

[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=1h
Persistent=true
Unit=beelink-certificates.service

[Install]
WantedBy=timers.target
"""
OLD_BACKUP_TIMER = """[Unit]
Description=Back up beelink configuration weekly

[Timer]
OnCalendar=Sun *-*-* 02:15:00
Persistent=true
Unit=beelink-config-backup.service

[Install]
WantedBy=timers.target
"""


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def atomic_write(path, text):
    if path.is_symlink():
        raise RuntimeError("Refusing to replace symlink: " + str(path))
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(text)
    temporary.chmod(0o644)
    temporary.replace(path)


def install_copy(source, destination, mode=0o700):
    if destination.is_symlink():
        raise RuntimeError("Refusing to replace symlink: " + str(destination))
    if destination.exists() and (not destination.is_file() or destination.stat().st_uid != 0):
        raise RuntimeError("Installed file must be a root-owned regular file: " + str(destination))
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as file:
        temporary = Path(file.name)
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod(mode)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_private_directory(path):
    if path.is_symlink():
        raise RuntimeError("Installed helper directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != 0 or path.stat().st_mode & 0o077:
        raise RuntimeError("Installed helper directory must be root-private: " + str(path))


def install():
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    jobs.guard()
    for unit in ("beelink-certificates.timer", "beelink-config-backup.timer"):
        run("systemctl", "is-active", "--quiet", unit)
    definitions = {}
    definitions.update(renewal.units())
    definitions.update(backup.units())
    definitions.update(jobs.units())
    paths = {
        "beelink-certificates.service": definitions["beelink-certificates.service"],
        "beelink-config-backup.service": definitions["beelink-config-backup.service"],
        "beelink-config-backup-failure.service":
            definitions["beelink-config-backup-failure.service"],
    }
    for name, expected in paths.items():
        path = UNIT_DIR / name
        if path.is_symlink() or not path.is_file() or path.read_text() != expected:
            raise RuntimeError("Existing service unit differs; inspect before updating: " + name)

    timer_specs = (
        ("beelink-certificates.timer", definitions["beelink-certificates.timer"],
         OLD_CERTIFICATE_TIMER),
        ("beelink-config-backup.timer", definitions["beelink-config-backup.timer"],
         OLD_BACKUP_TIMER),
    )
    for name, expected, old in timer_specs:
        path = UNIT_DIR / name
        if path.is_symlink() or not path.is_file() or path.read_text() not in (old, expected):
            raise RuntimeError("Existing timer differs; inspect before updating: " + name)
    for name in ("beelink-job-mac2016-sleep.service", "beelink-job-mac2016-sleep.timer"):
        path = UNIT_DIR / name
        if path.is_symlink() or path.exists() and path.read_text() != definitions[name]:
            raise RuntimeError("Existing sleep unit differs; inspect before replacing: " + name)

    ensure_private_directory(INSTALLED_JOBS)
    ensure_private_directory(backup.INSTALLED)
    install_copy(ROOT / "beelink-jobs.py", INSTALLED_JOBS / "beelink-jobs.py")
    install_copy(ROOT / "beelink-jobs.py", Path("/usr/local/bin/beelink-job-status"), 0o755)
    install_copy(ROOT / "renew-beelink-certificates.py",
                 renewal.INSTALLED)
    install_copy(ROOT / "backup-beelink-config.py",
                 backup.INSTALLED / "backup-beelink-config.py")
    for name, expected, _ in timer_specs:
        atomic_write(UNIT_DIR / name, expected)
    for name in ("beelink-job-mac2016-sleep.service", "beelink-job-mac2016-sleep.timer"):
        atomic_write(UNIT_DIR / name, definitions[name])

    run("systemd-analyze", "verify", *[
        str(UNIT_DIR / name) for name in (
            "beelink-certificates.service", "beelink-certificates.timer",
            "beelink-config-backup.service", "beelink-config-backup-failure.service",
            "beelink-config-backup.timer", "beelink-job-mac2016-sleep.service",
            "beelink-job-mac2016-sleep.timer")], env={
                **os.environ,
                "SYSTEMD_UNIT_PATH": "/etc/systemd/system:/run/systemd/generator.early:"
                                     "/run/systemd/generator:/run/systemd/generator.late:",
            })
    run("systemctl", "daemon-reload")
    run("systemctl", "restart", "beelink-certificates.timer",
        "beelink-config-backup.timer")
    run("systemctl", "enable", "--now", "beelink-job-mac2016-sleep.timer")
    run("systemctl", "list-timers", "beelink-certificates.timer",
        "beelink-config-backup.timer", "beelink-job-mac2016-sleep.timer", "--no-pager")
    print("Night schedule installed. Existing services were not restarted.")


if __name__ == "__main__":
    install()
