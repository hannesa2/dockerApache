#!/usr/bin/env python3
"""Publish verified beelink HTTP/HTTPS via mount-bound TCP socket proxies."""

import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import socket


SPEC = importlib.util.spec_from_file_location(
    "publish_control", Path(__file__).with_name("control-beelink-staging.py"))
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)
UNIT_DIR = Path("/etc/systemd/system")
DOMAINS = ("www.mxtracks.info", "dev.mxtracks.info", "git.mxtracks.info",
           "homeassistant.mxtracks.info")


def units(port, target):
    name = f"beelink-public-web-{port}"
    dependencies = """Requires=beelink-production.service
After=beelink-production.service daten.mount
BindsTo=beelink-production.service daten.mount
PartOf=beelink-production.service
"""
    return {
        name + ".socket": f"""[Unit]
Description=Beelink public web TCP {port}
{dependencies}
[Socket]
ListenStream=0.0.0.0:{port}
ListenStream=[::]:{port}
BindIPv6Only=ipv6-only
NoDelay=true
""",
        name + ".service": f"""[Unit]
Description=Beelink public web TCP {port} forwarding
{dependencies}
Requires={name}.socket
After={name}.socket

[Service]
ExecStartPre=/bin/sh -c 'test "$(/usr/bin/findmnt -rn -M /daten -t ext4 -o UUID)" = c0c42461-ae4c-4edb-a993-bba94a05ed6c'
ExecStart=/usr/lib/systemd/systemd-socket-proxyd {target}:{port}
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
""",
    }


def probe(address):
    for domain in DOMAINS:
        target = f"[{address}]" if ":" in address else address
        redirect = control.capture(
            "curl", "--noproxy", "*", "--silent", "--show-error", "--max-time", "30",
            "--resolve", f"{domain}:80:{target}", "--output", "/dev/null",
            "--write-out", "%{http_code}", f"http://{domain}/")
        if redirect not in {"301", "302", "308"}:
            raise RuntimeError(f"HTTP redirect check failed for {domain}: {redirect}")
        code = control.capture(
            "curl", "--noproxy", "*", "--silent", "--show-error", "--max-time", "30",
            "--resolve", f"{domain}:443:{target}", "--output", "/dev/null",
            "--write-out", "%{http_code}", f"https://{domain}/")
        # GitLab and ownCloud-backed sites may redirect to their login paths.
        if code not in {"200", "301", "302", "303"}:
            raise RuntimeError(f"Verified TLS HTTP check failed for {domain}: {code}")
    print("Verified HTTPS hostnames through " + address, flush=True)


def publish():
    control.preflight()
    if control.capture("systemctl", "is-active", "beelink-production.service") != "active":
        raise RuntimeError("Managed stacks must be active")
    for report in ("files-reconciliation-report.json", "certificates/prepared.json"):
        if not (control.RUNTIME / report).is_file():
            raise RuntimeError("Required reconciliation/preparation missing: " + report)
    settings = json.loads((control.RUNTIME / "frontend/homeassistant-proxy.json").read_text())
    target = settings["frontend_ip"]
    actual = control.capture("docker", "inspect", "--format",
                             '{{(index .NetworkSettings.Networks "beelink-databases_isolated").IPAddress}}',
                             "beelink-frontend-frontend-1")
    if target != actual:
        raise RuntimeError("Pinned frontend address changed")
    socket.inet_pton(socket.AF_INET, target)
    for port in (80, 443):
        for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
            with socket.socket(family, socket.SOCK_STREAM) as listener:
                if family == socket.AF_INET6:
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                listener.bind((address, port))
    definitions = {}
    for port in (80, 443):
        definitions.update(units(port, target))
    if any((UNIT_DIR / name).exists() or (UNIT_DIR / name).is_symlink()
           for name in definitions):
        raise RuntimeError("Public web units already exist; inspect before retrying")
    for name, text in definitions.items():
        (UNIT_DIR / name).write_text(text)
        (UNIT_DIR / name).chmod(0o644)
    sockets = [f"beelink-public-web-{port}.socket" for port in (80, 443)]
    succeeded = False
    try:
        control.run("systemd-analyze", "verify", *[str(UNIT_DIR / name) for name in definitions],
                    env={**os.environ, "SYSTEMD_UNIT_PATH":
                         "/etc/systemd/system:/run/systemd/generator.early:"
                         "/run/systemd/generator:/run/systemd/generator.late:"})
        control.run("systemctl", "daemon-reload")
        control.run("systemctl", "start", *sockets)
        probe("192.168.178.142")
        probe("::1")
        (control.RUNTIME / "public-web-report.json").write_text(json.dumps({
            "frontend_ip": target, "ports": [80, 443], "lan_ipv4": "192.168.178.142",
            "ipv6_loopback_checked": True, "boot_enabled": False,
            "router_changed": False, "renewal_handover_complete": False,
        }, indent=2) + "\n")
        succeeded = True
        print("LAN HTTP/HTTPS published and TLS checked. Router unchanged; "
              "public reachability, renewal and boot activation remain pending.", flush=True)
    finally:
        if not succeeded:
            print("ERROR: Public web checks failed; closing both public sockets.", flush=True)
            control.run("systemctl", "stop", *sockets,
                        "beelink-public-web-80.service", "beelink-public-web-443.service")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish-lan", required=True, action="store_true")
    parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        publish()


if __name__ == "__main__":
    main()
