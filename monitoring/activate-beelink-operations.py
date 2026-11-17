#!/usr/bin/env python3
"""Enable mount-bound boot and run the existing mirror jobs sequentially."""

import argparse
import fcntl
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess


ROOT = Path(__file__).resolve().parent
UNIT_DIR = Path("/etc/systemd/system")
NAME = "beelink-operations-check"
HELPERS = ("control-beelink-staging.py", "start-beelink-apps.py",
           "start-beelink-frontend.py", "start-beelink-artifacts.py",
           "start-beelink-dns.py")
JOBS = ("apache", "gitlab", "owncloud", "mxserver", "matomo", "monitor")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def definitions():
    result = {
        "daten.mount.d/beelink-encryption.conf": """[Unit]
Requires=systemd-cryptsetup@beelink\\x2dt7.service
After=systemd-cryptsetup@beelink\\x2dt7.service
""",
        NAME + ".service": """[Unit]
Description=Verify beelink mirror jobs sequentially after cutover
After=beelink-staging.service

[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/python3 /usr/local/libexec/activate-beelink-operations.py --run-checks
TimeoutStartSec=24h
""",
    }
    for port in (80, 443):
        result[f"beelink-public-web-{port}.socket.d/boot-order.conf"] = """[Unit]
DefaultDependencies=no
After=sysinit.target
Before=shutdown.target
Conflicts=shutdown.target
"""
    return result


def encryption_guard():
    entries = [line.split() for line in Path("/etc/crypttab").read_text().splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
    matches = [entry for entry in entries if entry[0] == "beelink-t7"]
    if len(matches) != 1 or len(matches[0]) < 4:
        raise RuntimeError("Expected one T7 crypttab entry")
    entry = matches[0]
    if entry[1] != "UUID=2e7135d2-30fe-4880-93e7-cbd39d21430e":
        raise RuntimeError("Unexpected encrypted T7 device")
    key = Path(entry[2])
    if (not key.is_absolute() or key.is_symlink() or not key.is_file()
            or key.stat().st_uid != 0 or key.stat().st_mode & 0o077):
        raise RuntimeError("Expected a private root-owned T7 boot key")


def fix_web_boot():
    jobs = load("operations_jobs", "beelink-jobs.py")
    jobs.guard()
    for relative, text in definitions().items():
        if not relative.endswith(".socket.d/boot-order.conf"):
            continue
        path = UNIT_DIR / relative
        if path.is_symlink() or path.exists() and path.read_text() != text:
            raise RuntimeError("Unexpected socket boot override: " + relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o644)
    run("systemctl", "daemon-reload")
    run("systemd-analyze", "verify", "beelink-public-web-80.socket",
        "beelink-public-web-443.socket", env={
            **os.environ, "SYSTEMD_UNIT_PATH":
            "/etc/systemd/system:/run/systemd/generator.early:"
            "/run/systemd/generator:/run/systemd/generator.late:"})
    run("systemctl", "start", "beelink-public-web-80.socket", "beelink-public-web-443.socket")
    failures = jobs.local_health()
    if failures:
        raise RuntimeError("\n".join(failures))
    print("Web sockets restored; late-boot ordering installed; live health checks passed.",
          flush=True)


def prepare():
    jobs = load("operations_jobs", "beelink-jobs.py")
    jobs.preflight()
    encryption_guard()
    control = load("operations_control", "control-beelink-staging.py")
    with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Refresh the installed controller, including the corrected frontend.
        # This installs/validates only; it does not restart live applications.
        control.install()
    for relative, text in definitions().items():
        path = UNIT_DIR / relative
        if path.is_symlink() or path.exists() and path.read_text() != text:
            raise RuntimeError("Existing operations configuration differs: " + relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o644)
    target = Path("/usr/local/libexec/activate-beelink-operations.py")
    shutil.copyfile(Path(__file__), target)
    target.chmod(0o700)
    run("systemctl", "daemon-reload")
    run("systemd-analyze", "verify", "beelink-staging.service",
        "beelink-public-web-80.socket", "beelink-public-web-443.socket",
        NAME + ".service", env={
            **os.environ, "SYSTEMD_UNIT_PATH":
            "/etc/systemd/system:/run/systemd/generator.early:"
            "/run/systemd/generator:/run/systemd/generator.late:"})
    run("systemctl", "enable", "beelink-staging.service")
    run("systemctl", "add-wants", "multi-user.target",
        "beelink-public-web-80.socket", "beelink-public-web-443.socket")
    run("systemctl", "start", "--no-block", NAME + ".service")
    print("Boot dependencies enabled; sequential mirror verification started.", flush=True)
    print("No reboot performed. Follow: sudo journalctl -fu " + NAME + ".service", flush=True)


def checks():
    failures = []
    for job in JOBS:
        print("Checking actual scheduled job: " + job, flush=True)
        result = subprocess.run(["systemctl", "start", "beelink-job-" + job + ".service"])
        if result.returncode:
            failures.append(job)
            print("FAILED: " + job + "; continuing independent checks.", flush=True)
    run("python3", "/usr/local/bin/beelink-job-status", "--report")
    if failures:
        raise RuntimeError("Job checks failed: " + ", ".join(failures))
    print("All five mirror jobs and monitoring completed successfully. "
          "Actual machine reboot remains untested.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare-and-check", action="store_true")
    group.add_argument("--run-checks", action="store_true")
    group.add_argument("--fix-web-boot", action="store_true")
    args = parser.parse_args()
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    os.umask(0o077)
    if args.fix_web_boot:
        fix_web_boot()
    elif args.prepare_and_check:
        prepare()
    else:
        checks()


if __name__ == "__main__":
    main()
