#!/usr/bin/env python3
"""Publish authenticated Java administration and application ports on the LAN."""

import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess


LAN = "192.168.178.142"
SUBNET = "192.168.178.0/24"
INTERFACE = "enp1s0"
UUID = "c0c42461-ae4c-4edb-a993-bba94a05ed6c"
HELPER = Path("/usr/local/libexec/publish-beelink-java-lan.py")
UNITS = Path("/etc/systemd/system")
FIREWALL_UNIT = "beelink-java-lan-firewall.service"
PORTS = {
    8080: ("beelink-java-web-wildfly-1", "WildFly applications"),
    9990: ("beelink-java-web-wildfly-1", "WildFly administration"),
    8081: ("beelink-java-web-glassfish-1", "GlassFish applications"),
    4848: ("beelink-java-web-glassfish-1", "GlassFish HTTPS administration"),
}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def guard():
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    if capture("findmnt", "-rn", "-M", "/daten", "-t", "ext4", "-o", "UUID") != UUID:
        raise RuntimeError("Expected encrypted T7 filesystem")
    run("systemctl", "is-active", "--quiet", "beelink-production.service")


def target(port):
    info = json.loads(capture("docker", "inspect", PORTS[port][0]))[0]
    if not info["State"]["Running"]:
        raise RuntimeError("Java container is not running")
    address = info["NetworkSettings"]["Networks"]["beelink-databases_isolated"]["IPAddress"]
    if ipaddress.ip_address(address) not in ipaddress.ip_network("172.19.0.0/16"):
        raise RuntimeError("Unexpected Java container network address")
    return address


def definitions(port):
    name = f"beelink-java-lan-{port}"
    dependencies = f"""Requires=beelink-production.service {FIREWALL_UNIT}
After=beelink-production.service daten.mount {FIREWALL_UNIT}
BindsTo=beelink-production.service daten.mount
PartOf=beelink-production.service
"""
    return {
        name + ".socket": f"""[Unit]
Description=LAN only: {PORTS[port][1]}
{dependencies}DefaultDependencies=no
After=sysinit.target
Before=shutdown.target
Conflicts=shutdown.target

[Socket]
ListenStream={LAN}:{port}
BindToDevice={INTERFACE}
NoDelay=true

[Install]
WantedBy=multi-user.target
""",
        name + ".service": f"""[Unit]
Description=LAN forwarding: {PORTS[port][1]}
{dependencies}Requires={name}.socket
After={name}.socket

[Service]
ExecStart=/usr/bin/python3 {HELPER} --forward {port}
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
""",
    }


def probe(addresses):
    for port in PORTS:
        address = addresses[port]
        scheme = "https" if port == 4848 else "http"
        path = "/management/domain" if port == 4848 else "/management" if port == 9990 else "/"
        command = ["curl", "--noproxy", "*", "--silent", "--show-error", "--max-time", "15",
                   "--output", "/dev/null", "--write-out", "%{http_code}"]
        if scheme == "https":
            # GlassFish uses its existing self-signed admin certificate.
            command.append("--insecure")
        status = capture(*command, f"{scheme}://{address}:{port}{path}")
        allowed = {"401"} if port in (4848, 9990) else {"200", "301", "302", "404"}
        if status not in allowed:
            raise RuntimeError(f"Unexpected Java endpoint response on {port}: {status}")
        print(f"{PORTS[port][1]}: HTTP {status}", flush=True)


def firewall_rules():
    ports = ", ".join(str(port) for port in PORTS)
    scope = f"ip daddr {LAN} tcp dport {{ {ports} }}"
    # Replace only our own table, atomically; never flush the host ruleset.
    return f"""add table inet beelink_java_lan
delete table inet beelink_java_lan
table inet beelink_java_lan {{
    chain input {{
        type filter hook input priority -10; policy accept;
        {scope} iifname "lo" accept
        {scope} iifname "{INTERFACE}" ip saddr {SUBNET} accept
        {scope} counter drop
    }}
}}
"""


def firewall_definition():
    return f"""[Unit]
Description=Restrict Java LAN ports to the trusted local subnet
Requires=beelink-production.service
After=beelink-production.service daten.mount
BindsTo=beelink-production.service daten.mount
PartOf=beelink-production.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 {HELPER} --firewall
UMask=0077
"""


def firewall():
    guard()
    rules = firewall_rules()
    run("/usr/sbin/nft", "--check", "--file", "-", input=rules, text=True)
    run("/usr/sbin/nft", "--file", "-", input=rules, text=True)


def install():
    guard()
    addresses = {port: target(port) for port in PORTS}
    definitions_all = {name: text for port in PORTS for name, text in definitions(port).items()}
    definitions_all[FIREWALL_UNIT] = firewall_definition()
    if any((UNITS / name).exists() or (UNITS / name).is_symlink() for name in definitions_all):
        raise RuntimeError("LAN units already exist; inspect before reinstalling")
    if HELPER.is_symlink() or HELPER.exists():
        raise RuntimeError("LAN helper already exists; inspect before reinstalling")
    for port in PORTS:
        with socket.socket() as listener:
            listener.bind((LAN, port))
    probe(addresses)
    run("/usr/sbin/nft", "--check", "--file", "-", input=firewall_rules(), text=True)
    shutil.copyfile(Path(__file__), HELPER)
    HELPER.chmod(0o700)
    sockets = [f"beelink-java-lan-{port}.socket" for port in PORTS]
    succeeded = False
    try:
        for name, text in definitions_all.items():
            path = UNITS / name
            path.write_text(text)
            path.chmod(0o644)
        run("systemctl", "daemon-reload")
        run("systemd-analyze", "verify", *definitions_all,
            env={**os.environ, "SYSTEMD_UNIT_PATH":
                 "/etc/systemd/system:/run/systemd/generator.early:"
                 "/run/systemd/generator:/run/systemd/generator.late:"})
        run("systemctl", "enable", "--now", *sockets)
        probe({port: LAN for port in PORTS})
        succeeded = True
    finally:
        if not succeeded:
            print("ERROR: LAN publication failed; disabling LAN sockets. "
                  "Production web ports remain unchanged.", flush=True)
            run("systemctl", "disable", "--now", *sockets)
            run("systemctl", "stop", *[
                f"beelink-java-lan-{port}.service" for port in PORTS])
    print("Java LAN ports enabled. Admin credentials unchanged; no router changes.", flush=True)


def forward(port):
    guard()
    run("systemctl", "is-active", "--quiet", FIREWALL_UNIT)
    address = target(port)
    # Resolve Docker identity as root, then drop privileges before TCP forwarding.
    account = pwd.getpwnam("nobody")
    os.setgroups([])
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)
    os.execv("/usr/lib/systemd/systemd-socket-proxyd",
             ["/usr/lib/systemd/systemd-socket-proxyd", f"{address}:{port}"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true")
    group.add_argument("--firewall", action="store_true")
    group.add_argument("--forward", type=int, choices=PORTS)
    args = parser.parse_args()
    os.umask(0o077)
    os.environ["LC_ALL"] = "C"
    if args.install:
        with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            install()
    elif args.firewall:
        firewall()
    else:
        forward(args.forward)


if __name__ == "__main__":
    main()
