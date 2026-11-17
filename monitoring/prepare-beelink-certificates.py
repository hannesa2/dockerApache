#!/usr/bin/env python3
"""Prepare private Certbot state on beelink without enabling renewal or ports."""

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import subprocess
import tarfile


SPEC = importlib.util.spec_from_file_location(
    "certificate_control", Path(__file__).with_name("control-beelink-staging.py"))
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)
DESTINATION = control.RUNTIME / "certificates"
LINEAGES = (
    "www.mxtracks.info", "dev.mxtracks.info-0002", "git.mxtracks.info-0001",
    "homeassistant.mxtracks.info", "nextcloud.mxtracks.info-0002",
)
ACCOUNT_LINKS = {
    "acme-v02.api.letsencrypt.org": "acme-v01.api.letsencrypt.org",
    "acme-staging-v02.api.letsencrypt.org": "acme-staging.api.letsencrypt.org",
}


def validate_member(member):
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or path.parts[:2] != ("etc", "letsencrypt"):
        raise RuntimeError("Unsafe certificate archive path")
    relative = path.parts[2:]
    allowed = not relative or relative[0] == "accounts"
    if relative and relative[0] in ("live", "archive") and len(relative) >= 2:
        allowed = relative[1] in LINEAGES
    if relative and relative[0] == "renewal" and len(relative) == 2:
        allowed = relative[1] in {name + ".conf" for name in LINEAGES}
    if not allowed or not (member.isfile() or member.isdir() or member.issym()):
        raise RuntimeError("Unexpected certificate archive member: " + member.name)
    if member.issym():
        target = PurePosixPath(member.linkname)
        if (len(relative) == 3 and relative[0] == "accounts"
                and relative[2] == "directory" and relative[1] in ACCOUNT_LINKS):
            server = ACCOUNT_LINKS[relative[1]]
            if member.linkname != f"/etc/letsencrypt/accounts/{server}/directory":
                raise RuntimeError("Unsafe ACME account archive symlink")
            return
        if (len(relative) != 3 or relative[0] != "live" or target.is_absolute()
                or target.parts[:3] != ("..", "..", "archive")
                or len(target.parts) != 5 or target.parts[3] != relative[1]
                or target.parts[4] in (".", "..")):
            raise RuntimeError("Unsafe certificate archive symlink")


def validated_members(archive):
    members = archive.getmembers()
    names = [member.name.rstrip("/") for member in members]
    if len(set(names)) != len(names):
        raise RuntimeError("Duplicate certificate archive paths")
    for member in members:
        validate_member(member)
    links = {PurePosixPath(m.name) for m in members if m.issym()}
    if any(parent in links for m in members for parent in PurePosixPath(m.name).parents):
        raise RuntimeError("Certificate archive traverses a symlink")
    for member in members:
        parts = PurePosixPath(member.name).parts
        if member.issym() and parts[2] == "accounts":
            server = ACCOUNT_LINKS[parts[3]]
            target = f"etc/letsencrypt/accounts/{server}/directory"
            if target not in names or not archive.getmember(target).isdir():
                raise RuntimeError("ACME account symlink target is missing or not a directory")
            member.linkname = f"../{server}/directory"
    return members


def prepare(source):
    if source.is_symlink() or not source.resolve().is_relative_to(
            "/daten/latitude-migration/cutover-20261117"):
        raise RuntimeError("Expected the private transferred certificate directory")
    if not (source / "transfer-complete").is_file():
        raise RuntimeError("Certificate transfer is incomplete")
    expected = {}
    for line in (source / "SHA256SUMS").read_text().splitlines():
        checksum, name = line.split(maxsplit=1)
        if name not in ("letsencrypt.tar.gz", "certificate-inventory.txt") or name in expected:
            raise RuntimeError("Unexpected certificate checksum manifest")
        expected[name] = checksum
    if set(expected) != {"letsencrypt.tar.gz", "certificate-inventory.txt"}:
        raise RuntimeError("Incomplete certificate checksum manifest")
    for name, checksum in expected.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != checksum:
            raise RuntimeError("Certificate checksum mismatch: " + name)
    if DESTINATION.exists():
        raise RuntimeError("Certificate destination already exists; inspect before retrying")
    with tarfile.open(source / "letsencrypt.tar.gz") as archive:
        members = validated_members(archive)
        DESTINATION.mkdir(mode=0o700)
        archive.extractall(DESTINATION, members=members)
    config = DESTINATION / "etc/letsencrypt"
    for name in LINEAGES:
        renewal = config / "renewal" / (name + ".conf")
        text = renewal.read_text()
        # Certbot stores absolute source paths in each lineage configuration.
        text = text.replace("/etc/letsencrypt/", str(config) + "/")
        if any(line.lstrip().startswith(
                ("pre_hook =", "post_hook =", "renew_hook =", "deploy_hook ="))
               for line in text.splitlines()):
            raise RuntimeError("Source-specific renewal hook requires explicit migration")
        renewal.write_text(text)
        renewal.chmod(0o600)
        for filename in ("fullchain.pem", "privkey.pem"):
            path = config / "live" / name / filename
            if not path.resolve().is_relative_to(config / "archive") or not path.is_file():
                raise RuntimeError("Invalid transferred certificate link")
        control.run("openssl", "x509", "-in", str(config / "live" / name / "fullchain.pem"),
                    "-checkend", "604800", "-noout")
    if shutil.which("certbot") is None:
        raise RuntimeError("Install certbot first with: sudo apt-get install --no-install-recommends certbot")
    for name in ("work", "logs"):
        (DESTINATION / name).mkdir(mode=0o700)
    control.run("certbot", "certificates", "--config-dir", str(config),
                "--work-dir", str(DESTINATION / "work"),
                "--logs-dir", str(DESTINATION / "logs"))
    (DESTINATION / "prepared.json").write_text(json.dumps({
        "source": str(source), "lineages": list(LINEAGES), "renewal_enabled": False,
        "public_ports_enabled": False,
    }, indent=2) + "\n")
    print("Private Certbot state prepared. No renewal, timer, live certificate "
          "mount, public port or source change.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    if shutil.which("certbot") is None:
        raise RuntimeError("Install certbot first with: sudo apt-get install --no-install-recommends certbot")
    with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        control.preflight()
        prepare(args.source)


if __name__ == "__main__":
    main()
