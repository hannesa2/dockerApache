#!/usr/bin/env python3
"""Prepare the registered Latitude runner for the Beelink host shell executor."""

import argparse
import fcntl
import importlib.util
import os
from pathlib import Path
import pwd
import re
import socket
import subprocess


SPEC = importlib.util.spec_from_file_location(
    "beelink_control", Path(__file__).with_name("control-beelink-staging.py"))
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)
RUNTIME = control.RUNTIME
SOURCE = RUNTIME / "host-configuration/srv/gitlab-runner/config/config.toml"
TARGET = Path("/etc/gitlab-runner/config.toml")
DROPIN_DIR = Path("/etc/systemd/system/gitlab-runner.service.d")
DROPIN = DROPIN_DIR / "beelink-shell-executor.conf"


def run(*args):
    subprocess.run(args, check=True)


def shell_config(text):
    if len(re.findall(r"(?m)^\s*\[\[runners\]\]\s*(?:#.*)?$", text)) != 1:
        raise RuntimeError("Expected exactly one registered Latitude runner")
    block = re.split(r"(?m)^\s*\[\[runners\]\]\s*(?:#.*)?$", text, maxsplit=1)[1]
    nested_table = re.search(r"(?m)^\s*\[", block)
    settings = block[:nested_table.start()] if nested_table else block
    values = {}
    for key in ("url", "token", "executor"):
        matches = re.findall(
            rf"""(?m)^[ \t]*{key}[ \t]*=[ \t]*("([^"\n]*)"|'([^'\n]*)')[ \t]*(?:#.*)?$""",
            settings)
        if len(matches) != 1:
            raise RuntimeError("Expected exactly one runner " + key + " setting")
        values[key] = matches[0][1] or matches[0][2]
    if values["url"].rstrip("/") != "https://git.mxtracks.info":
        raise RuntimeError("Latitude runner is registered to an unexpected GitLab URL")
    if not values["token"]:
        raise RuntimeError("Latitude runner authentication token is missing")
    if values["executor"] not in ("docker", "shell"):
        raise RuntimeError("Unexpected existing GitLab runner executor")
    matches = list(re.finditer(
        r"""(?m)^([ \t]*executor[ \t]*=[ \t]*)(?:"[^"\n]*"|'[^'\n]*')[ \t]*(?:#.*)?$""",
        settings))
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one runner executor setting")
    match = matches[0]
    updated = settings[:match.start()] + match.group(1) + '"shell"' + settings[match.end():]
    result = text[:len(text) - len(block)] + updated + block[len(settings):]
    if re.search(r"""(?m)^[ \t]*executor[ \t]*=[ \t]*"shell"[ \t]*(?:#.*)?$""",
                 result) is None:
        raise RuntimeError("Could not prepare the host shell executor")
    return result


def dropin_text(account):
    home = Path(account.pw_dir)
    if (not home.is_absolute() or not home.is_dir() or home.is_symlink()
            or not home.is_relative_to("/home")):
        raise RuntimeError("The gitlab-runner account needs a private home under /home")
    return f"[Service]\nUser=gitlab-runner\nWorkingDirectory={home}\n"


def prepare(activate):
    control.check_mount()
    if (RUNTIME.is_symlink() or RUNTIME.stat().st_uid != 0
            or RUNTIME.stat().st_mode & 0o077):
        raise RuntimeError("Expected private root-owned runtime")
    if SOURCE.is_symlink() or not SOURCE.is_file():
        raise RuntimeError("Private Latitude runner configuration is missing")
    if TARGET.parent.is_symlink() or not TARGET.parent.is_dir():
        raise RuntimeError("Install the GitLab Runner package before preparing its configuration")
    try:
        account = pwd.getpwnam("gitlab-runner")
    except KeyError as error:
        raise RuntimeError("The gitlab-runner service account is missing") from error
    unit = subprocess.run(["systemctl", "show", "gitlab-runner.service", "-p", "LoadState",
                           "--value"], check=True, text=True, capture_output=True).stdout.strip()
    if unit != "loaded":
        raise RuntimeError("GitLab Runner systemd service is not installed")
    override = dropin_text(account)
    desired = shell_config(SOURCE.read_text())
    if TARGET.is_symlink():
        raise RuntimeError("Refusing a symlinked GitLab Runner configuration")
    if TARGET.exists():
        if not TARGET.is_file() or TARGET.read_text() != desired:
            raise RuntimeError("Existing Beelink runner configuration differs; inspect manually")
    else:
        temporary = TARGET.with_name("config.toml.prepare")
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError("Existing runner configuration temporary file; inspect manually")
        temporary.write_text(desired)
        temporary.chmod(0o600)
        os.chown(temporary, account.pw_uid, account.pw_gid)
        temporary.replace(TARGET)
    TARGET.chmod(0o600)
    os.chown(TARGET, account.pw_uid, account.pw_gid)
    if activate:
        if DROPIN_DIR.is_symlink() or (DROPIN.exists() and
                                      (DROPIN.is_symlink() or DROPIN.read_text() != override)):
            raise RuntimeError("Existing GitLab Runner systemd override differs")
        DROPIN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        if not DROPIN.exists():
            DROPIN.write_text(override)
        if DROPIN_DIR.stat().st_uid != 0 or DROPIN_DIR.stat().st_mode & 0o022:
            raise RuntimeError("GitLab Runner systemd override directory is writable by other users")
        os.chown(DROPIN, 0, 0)
        DROPIN.chmod(0o644)
        run("systemctl", "daemon-reload")
        run("runuser", "-u", "gitlab-runner", "--", "test", "-r", str(TARGET))
        run("gitlab-runner", "verify", "--config", str(TARGET))
        run("systemctl", "enable", "gitlab-runner.service")
        if subprocess.run(["systemctl", "is-active", "--quiet", "gitlab-runner.service"]).returncode:
            run("systemctl", "start", "gitlab-runner.service")
        else:
            run("systemctl", "restart", "gitlab-runner.service")
    print("Latitude GitLab runner prepared for the Beelink shell executor; token not displayed.",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    with Path("/run/lock/beelink-gitlab-runner.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prepare(args.activate)


if __name__ == "__main__":
    main()
