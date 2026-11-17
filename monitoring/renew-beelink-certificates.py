#!/usr/bin/env python3
"""Renew the five migrated beelink certificates and deploy to Apache/CoreDNS."""

import argparse
import configparser
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time


RUNTIME = Path("/daten/beelink-runtime")
STATE = RUNTIME / "certificates"
CONFIG = STATE / "etc/letsencrypt"
SERVED = RUNTIME / "host-configuration/etc/letsencrypt"
UUID = "c0c42461-ae4c-4edb-a993-bba94a05ed6c"
FRONTEND = "beelink-frontend-frontend-1"
DNS = "beelink-dns-dns-1"
LINEAGES = {
    "www.mxtracks.info": "www.mxtracks.info",
    "dev.mxtracks.info-0002": "dev.mxtracks.info",
    "git.mxtracks.info-0001": "git.mxtracks.info",
    "homeassistant.mxtracks.info": "homeassistant.mxtracks.info",
    "nextcloud.mxtracks.info-0002": "nextcloud.mxtracks.info",
}
MIRRORS = ("dev16.mxtracks.info", "www16.mxtracks.info", "git16.mxtracks.info")
FILES = ("cert.pem", "chain.pem", "fullchain.pem", "privkey.pem")
PAUSED = Path("/run/beelink-certificate-http-paused")
INSTALLED = Path("/usr/local/libexec/renew-beelink-certificates.py")
UNITS = Path("/etc/systemd/system")
NAME = "beelink-certificates"
LOCK = Path("/run/lock/beelink-staging-control.lock")


def log(message):
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + message, flush=True)


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def active(unit):
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def guard():
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    os.listdir("/daten")
    if capture("findmnt", "-rn", "-M", "/daten", "-t", "ext4", "-o", "UUID") != UUID:
        raise RuntimeError("Expected encrypted T7 is not mounted")
    if RUNTIME.is_symlink() or RUNTIME.stat().st_uid != 0 or RUNTIME.stat().st_mode & 0o077:
        raise RuntimeError("Expected private root-owned runtime")
    if not active("beelink-production.service"):
        raise RuntimeError("Managed application stacks are not active")


def validate():
    if not (STATE / "prepared.json").is_file():
        raise RuntimeError("Import the private Certbot state first")
    for path in (STATE, CONFIG, SERVED):
        if path.is_symlink() or not path.resolve().is_relative_to(RUNTIME.resolve()):
            raise RuntimeError("Unexpected certificate directory")
    renewals = CONFIG / "renewal"
    names = {p.stem for p in renewals.glob("*.conf")}
    if names not in (set(LINEAGES), set(LINEAGES) | set(MIRRORS)):
        raise RuntimeError("Expected production lineages with either all or no mirror lineages")
    for name in names:
        parser = configparser.ConfigParser(interpolation=None)
        # Certbot's top-level path keys precede its [renewalparams] section.
        parser.read_string("[lineage]\n" + (renewals / (name + ".conf")).read_text())
        params = parser["renewalparams"]
        if (params.get("authenticator") != "standalone"
                or params.get("server") != "https://acme-v02.api.letsencrypt.org/directory"):
            raise RuntimeError("Unexpected ACME server/authenticator: " + name)
        for section in parser.values():
            if any(key.endswith("_hook") or key == "installer" for key in section):
                raise RuntimeError("Inspect unexpected Certbot hook/installer: " + name)
        for key in ("cert", "privkey", "chain", "fullchain", "archive_dir"):
            path = Path(parser["lineage"][key])
            if not path.resolve().is_relative_to(CONFIG.resolve()) or not path.exists():
                raise RuntimeError("Escaping/missing Certbot lineage path: " + name)
    if (CONFIG / "cli.ini").exists() or any(
            p.is_file() for p in (CONFIG / "renewal-hooks").rglob("*")):
        raise RuntimeError("Unexpected shared Certbot configuration/hooks")
    for container in (FRONTEND, DNS):
        details = json.loads(capture("docker", "inspect", container))[0]
        mounts = [m for m in details["Mounts"] if m["Destination"] == "/etc/letsencrypt"]
        if (not details["State"]["Running"] or len(mounts) != 1
                or mounts[0]["Source"] != str(SERVED) or mounts[0]["RW"]):
            raise RuntimeError("Unexpected running certificate consumer: " + container)
    timer = subprocess.run(["systemctl", "is-enabled", "certbot.timer"],
                           text=True, stdout=subprocess.PIPE, check=False)
    if timer.stdout.strip() != "masked":
        raise RuntimeError("Keep the distro Certbot timer masked; use only the private timer")


def restore_http():
    if PAUSED.exists():
        guard()
        run("systemctl", "start", "beelink-public-web-80.socket")
        PAUSED.unlink()
        log("HTTP forwarding restored; HTTPS was not stopped.")


def renew(dry_run=False):
    if not active("beelink-public-web-80.socket"):
        raise RuntimeError("Expected public HTTP socket to be active")
    if PAUSED.exists():
        raise RuntimeError("Previous HTTP pause marker exists; run --restore-http first")
    command = [
        "certbot", "renew", "--non-interactive", "--no-random-sleep-on-renew",
        "--no-directory-hooks", "--config", "/dev/null",
        "--config-dir", str(CONFIG), "--work-dir", str(STATE / "work"),
        "--logs-dir", str(STATE / "logs"),
    ]
    if dry_run:
        command.append("--dry-run")
    PAUSED.touch(mode=0o600)
    try:
        run("systemctl", "stop", "beelink-public-web-80.socket",
            "beelink-public-web-80.service")
        log("HTTP temporarily reserved for standalone HTTP-01; HTTPS stays online.")
        run(*command)
    finally:
        restore_http()


def material():
    result = {}
    for name, domain in LINEAGES.items():
        directory = CONFIG / "live" / name
        for filename in FILES:
            path = directory / filename
            if not path.is_file() or not path.resolve().is_relative_to((CONFIG / "archive").resolve()):
                raise RuntimeError("Missing or escaping certificate: " + name)
        cert, key = directory / "cert.pem", directory / "privkey.pem"
        run("openssl", "x509", "-in", str(cert), "-noout", "-checkend", "604800")
        run("openssl", "x509", "-in", str(cert), "-noout", "-checkhost", domain)
        if capture("openssl", "x509", "-in", str(cert), "-pubkey", "-noout") != capture(
                "openssl", "pkey", "-in", str(key), "-pubout"):
            raise RuntimeError("Certificate/private key mismatch: " + name)
        der = subprocess.check_output(["openssl", "x509", "-in", str(cert), "-outform", "DER"])
        result[name] = hashlib.sha256(der).hexdigest()
    return result


def reload_consumers():
    run("docker", "exec", FRONTEND, "apache2ctl", "configtest")
    run("docker", "exec", FRONTEND, "apache2ctl", "graceful")
    # CoreDNS reads its TLS files at startup; Apache only needs a graceful reload.
    run("docker", "restart", DNS)


def verify_served(expected):
    details = json.loads(capture("docker", "inspect", DNS))[0]
    networks = details["NetworkSettings"]["Networks"]
    if set(networks) != {"beelink-dns_resolver"}:
        raise RuntimeError("Unexpected CoreDNS network")
    dns_ip = networks["beelink-dns_resolver"]["IPAddress"]
    for name, domain in LINEAGES.items():
        address = (dns_ip, 853) if domain == "nextcloud.mxtracks.info" else ("127.0.0.1", 443)
        for attempt in range(20):
            try:
                with socket.create_connection(address, timeout=5) as raw:
                    with ssl.create_default_context().wrap_socket(raw, server_hostname=domain) as tls:
                        digest = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
                if digest != expected[name]:
                    raise RuntimeError("Served certificate fingerprint differs: " + domain)
                break
            except (OSError, RuntimeError):
                if attempt == 19:
                    raise
                time.sleep(1)
        log("Verified served certificate: " + domain)


def deploy():
    expected = material()
    links = {}
    changed = False
    try:
        for name, digest in expected.items():
            source = CONFIG / "live" / name
            destination = SERVED / "archive" / ("beelink-" + digest)
            if not destination.exists():
                destination.mkdir(mode=0o700)
                for filename in FILES:
                    shutil.copyfile(source / filename, destination / filename)
                    (destination / filename).chmod(0o600)
            for filename in FILES:
                if (destination / filename).read_bytes() != (source / filename).read_bytes():
                    raise RuntimeError("Existing certificate generation differs: " + name)
                link = SERVED / "live" / name / filename
                if (not link.is_symlink()
                        or not link.resolve().is_relative_to((SERVED / "archive").resolve())):
                    raise RuntimeError("Unexpected served certificate path: " + str(link))
                links[link] = os.readlink(link)
                target = os.path.relpath(destination / filename, link.parent)
                if links[link] == target:
                    continue
                changed = True
                temporary = link.with_name(filename + ".beelink-new")
                temporary.symlink_to(target)
                temporary.replace(link)
        if changed:
            reload_consumers()
        verify_served(expected)
    except BaseException:
        log("ERROR: Certificate deployment failed; restoring previous served links.")
        for link, target in links.items():
            temporary = link.with_name(link.name + ".beelink-rollback")
            temporary.symlink_to(target)
            temporary.replace(link)
        if changed:
            reload_consumers()
        raise


def import_mirrors(source):
    source = source.resolve()
    if not source.is_relative_to("/daten/latitude-migration/cutover-20261117"):
        raise RuntimeError("Expected private mirror certificate transfer directory")
    if not (source / "transfer-complete").is_file():
        raise RuntimeError("Mirror transfer is incomplete")
    expected = {}
    for line in (source / "SHA256SUMS").read_text().splitlines():
        checksum, name = line.split(maxsplit=1)
        if name not in ("letsencrypt.tar.gz", "certificate-inventory.txt") or name in expected:
            raise RuntimeError("Unexpected mirror checksum manifest")
        expected[name] = checksum
    if set(expected) != {"letsencrypt.tar.gz", "certificate-inventory.txt"}:
        raise RuntimeError("Incomplete mirror checksum manifest")
    for name, checksum in expected.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != checksum:
            raise RuntimeError("Mirror transfer checksum mismatch: " + name)
    if any((CONFIG / "renewal" / (name + ".conf")).exists() for name in MIRRORS):
        raise RuntimeError("Mirror lineages already exist; inspect before importing again")
    spec = importlib.util.spec_from_file_location(
        "certificate_import", Path(__file__).with_name("prepare-beelink-certificates.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.LINEAGES = MIRRORS
    with tempfile.TemporaryDirectory(prefix="mirror-import-", dir=STATE) as temporary:
        with tarfile.open(source / "letsencrypt.tar.gz") as archive:
            members = module.validated_members(archive)
            archive.extractall(temporary, members=members)
        extracted = Path(temporary) / "etc/letsencrypt"
        for name in MIRRORS:
            renewal = extracted / "renewal" / (name + ".conf")
            text = renewal.read_text().replace("/etc/letsencrypt/", str(CONFIG) + "/")
            if any(line.lstrip().startswith(
                    ("pre_hook =", "post_hook =", "renew_hook =", "deploy_hook ="))
                   for line in text.splitlines()):
                raise RuntimeError("Unexpected source-specific mirror renewal hook")
            renewal.write_text(text)
            renewal.chmod(0o600)
            for kind in ("archive", "live"):
                if (CONFIG / kind / name).exists():
                    raise RuntimeError("Mirror certificate destination exists: " + name)
            run("openssl", "x509", "-in", str(extracted / "live" / name / "fullchain.pem"),
                "-noout", "-checkhost", name, "-checkend", "604800")
        for name in MIRRORS:
            for kind in ("archive", "live"):
                shutil.copytree(extracted / kind / name, CONFIG / kind / name, symlinks=True)
            shutil.copyfile(extracted / "renewal" / (name + ".conf"),
                            CONFIG / "renewal" / (name + ".conf"))
    log("Imported three mirror lineages; renewal/deployment test still required.")


def deploy_mirrors():
    if not (CONFIG / "renewal" / (MIRRORS[0] + ".conf")).exists():
        return
    for name in MIRRORS:
        directory = CONFIG / "live" / name
        cert, key = directory / "fullchain.pem", directory / "privkey.pem"
        for path in (cert, key):
            if not path.resolve().is_relative_to((CONFIG / "archive").resolve()):
                raise RuntimeError("Escaping mirror certificate path")
        run("openssl", "x509", "-in", str(cert), "-noout", "-checkhost", name,
            "-checkend", "604800")
        if capture("openssl", "x509", "-in", str(cert), "-pubkey", "-noout") != capture(
                "openssl", "pkey", "-in", str(key), "-pubout"):
            raise RuntimeError("Mirror certificate/key mismatch")
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as stream:
                stream.dereference = True
                for filename in ("fullchain.pem", "privkey.pem"):
                    stream.add(directory / filename, arcname=filename, recursive=False)
            archive.seek(0)
            run("ssh", "-i", "/home/hannes/.ssh/mac2016-cert-deploy",
                "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
                "-o", "HostKeyAlias=mac2016",
                "-o", "UserKnownHostsFile=/home/hannes/.ssh/known_hosts",
                "hannes@192.168.178.70", name, stdin=archive, timeout=120)
        expected = subprocess.check_output(
            ["openssl", "x509", "-in", str(cert), "-outform", "DER"])
        for attempt in range(20):
            try:
                with socket.create_connection(("192.168.178.70", 443), timeout=5) as raw:
                    with ssl.create_default_context().wrap_socket(raw, server_hostname=name) as tls:
                        if tls.getpeercert(binary_form=True) != expected:
                            raise RuntimeError("Mirror served certificate differs: " + name)
                break
            except (OSError, RuntimeError):
                if attempt == 19:
                    raise
                time.sleep(1)
        log("Deployed and verified mirror certificate: " + name)


def notify(success, error=None):
    result = "SUCCESS" if success else "FAILED"
    body = (f"Host: beelink\nResult: {result}\n"
            f"Finished: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
            "Job: TLS certificate renewal and deployment\n"
            "Production: www, dev, git, homeassistant, nextcloud (DoT)\n"
            f"Mirrors imported: {(CONFIG / 'renewal' / (MIRRORS[0] + '.conf')).is_file()}\n"
            f"Details: {error or 'Certbot completed; served certificates verified.'}\n"
            "Log: journalctl -u beelink-certificates.service\n")
    subprocess.run(
        ["/usr/bin/mail", "-s", f"[beelink] {result}: certificate renewal",
         "hannes.software@gmx.at"], input=body, text=True, check=True, timeout=60)
    log("Status email submitted to hannes.software@gmx.at: " + result)


def units():
    return {
        NAME + ".service": f"""[Unit]
Description=Renew private beelink TLS certificates
After=network-online.target docker.service beelink-production.service daten.mount
BindsTo=daten.mount
ConditionPathIsMountPoint=/daten

[Service]
Type=oneshot
UMask=0077
ExecCondition=/usr/bin/systemctl is-active --quiet beelink-production.service
ExecStart=/usr/bin/python3 {INSTALLED} --renew
ExecStopPost=/usr/bin/python3 {INSTALLED} --restore-http
TimeoutStartSec=30min
TimeoutStopSec=60
""",
        NAME + ".timer": f"""[Unit]
Description=Check beelink TLS renewal twice daily

[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=1h
Persistent=true
Unit={NAME}.service

[Install]
WantedBy=timers.target
""",
    }


def install():
    for name, text in units().items():
        path = UNITS / name
        if path.is_symlink() or (path.exists() and path.read_text() != text):
            raise RuntimeError("Existing renewal unit differs: " + name)
    INSTALLED.parent.mkdir(parents=True, exist_ok=True)
    if Path(__file__).resolve() != INSTALLED.resolve():
        shutil.copyfile(Path(__file__), INSTALLED)
    INSTALLED.chmod(0o700)
    for name, text in units().items():
        (UNITS / name).write_text(text)
        (UNITS / name).chmod(0o644)
    run("systemd-analyze", "verify", str(UNITS / (NAME + ".service")),
        str(UNITS / (NAME + ".timer")),
        env={**os.environ, "SYSTEMD_UNIT_PATH":
             "/etc/systemd/system:/run/systemd/generator.early:"
             "/run/systemd/generator:/run/systemd/generator.late:"})
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", NAME + ".timer")
    if not active(NAME + ".timer"):
        raise RuntimeError("Renewal timer failed to start")


def interrupted(signum, frame):
    raise SystemExit("Certificate operation interrupted: " + str(signum))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--test-and-enable", action="store_true")
    action.add_argument("--renew", action="store_true")
    action.add_argument("--restore-http", action="store_true")
    action.add_argument("--import-mirrors", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.restore_http:
        guard()
        restore_http()
        return
    signal.signal(signal.SIGTERM, interrupted)
    try:
        guard()
        with LOCK.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            validate()
            if args.import_mirrors:
                import_mirrors(args.import_mirrors)
                validate()
            renew(dry_run=args.test_and_enable or bool(args.import_mirrors))
            deploy()
            deploy_mirrors()
            if args.test_and_enable or args.import_mirrors:
                install()
            report = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                      "lineages": list(LINEAGES),
                      "mirror_lineages": list(MIRRORS) if (
                          CONFIG / "renewal" / (MIRRORS[0] + ".conf")).exists() else [],
                      "dry_run": args.test_and_enable or bool(args.import_mirrors),
                      "source_timer_managed": False}
            (STATE / "renewal-report.json").write_text(json.dumps(report, indent=2) + "\n")
            log("Certificate renewal/deployment complete; source ownership unchanged.")
    except BaseException as error:
        try:
            notify(False, str(error))
        except Exception as mail_error:
            log("ERROR: Failure notification could not be submitted: " + str(mail_error))
        raise
    notify(True)


if __name__ == "__main__":
    main()
