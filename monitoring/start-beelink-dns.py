#!/usr/bin/env python3
"""Verify split DNS and authenticated DNS-over-TLS without publishing DNS ports."""

import argparse
import fcntl
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import socket


SPEC = importlib.util.spec_from_file_location(
    "beelink_apps", Path(__file__).with_name("start-beelink-apps.py"))
apps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apps)
WORK = apps.RUNTIME / "dns"
COMPOSE = ["docker", "compose", "-p", "beelink-dns", "-f", str(WORK / "compose.json")]
IPV4 = "192.168.178.142"
IPV6 = "fdf4:be15:98e0:0:7a55:36ff:fe0e:d07e"
DOMAINS = ("www.mxtracks.info", "dev.mxtracks.info", "git.mxtracks.info",
           "homeassistant.mxtracks.info")
MIRRORS = ("www16.mxtracks.info", "dev16.mxtracks.info")
DOT_HOST = "nextcloud.mxtracks.info"
CERT_NAME = "nextcloud.mxtracks.info-0002"
PROBE = r"""
import ipaddress
import json
import secrets
import socket
import ssl
import struct
import sys

ip, ipv4, ipv6, dot_host, primary, mirrors = sys.argv[1:]
primary, mirrors = json.loads(primary), json.loads(mirrors)

def exact(stream, count):
    data = b''
    while len(data) < count:
        block = stream.recv(count - len(data))
        if not block:
            raise RuntimeError('Truncated DNS-over-TLS response')
        data += block
    return data

def skip_name(data, offset):
    for _ in range(128):
        size = data[offset]
        if size & 0xc0 == 0xc0:
            return offset + 2
        if size & 0xc0:
            raise RuntimeError('Invalid DNS label')
        offset += 1
        if size == 0:
            return offset
        offset += size
    raise RuntimeError('Oversized DNS name')

def query(host, kind, transport):
    identifier = secrets.randbelow(65536)
    labels = b''.join(bytes([len(label)]) + label.encode('ascii') for label in host.split('.')) + b'\0'
    packet = struct.pack('!HHHHHH', identifier, 0x100, 1, 0, 0, 0) + labels + struct.pack('!HH', kind, 1)
    if transport == 'udp':
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
            stream.settimeout(10)
            stream.connect((ip, 53))
            stream.send(packet)
            data = stream.recv(65535)
    else:
        with socket.create_connection((ip, 853 if transport == 'tls' else 53), timeout=10) as raw:
            stream = ssl.create_default_context().wrap_socket(raw, server_hostname=dot_host) \
                if transport == 'tls' else raw
            with stream:
                stream.sendall(struct.pack('!H', len(packet)) + packet)
                data = exact(stream, struct.unpack('!H', exact(stream, 2))[0])
    ident, flags, questions, answers, _, _ = struct.unpack_from('!HHHHHH', data)
    if ident != identifier or not flags & 0x8000 or flags & 0x0200 or flags & 15:
        raise RuntimeError('DNS response failed validation: ' + host)
    offset = 12
    for _ in range(questions):
        offset = skip_name(data, offset) + 4
    result = []
    for _ in range(answers):
        offset = skip_name(data, offset)
        record_type, record_class, _, size = struct.unpack_from('!HHIH', data, offset)
        offset += 10
        value = data[offset:offset+size]
        offset += size
        if record_type == kind and record_class == 1 and kind in (1, 28):
            result.append(str(ipaddress.ip_address(value)))
    return result

for transport in ('udp', 'tcp', 'tls'):
    for host in primary:
        if query(host, 1, transport) != [ipv4]:
            raise RuntimeError('Wrong primary IPv4: ' + host)
        if query(host, 28, transport) != [str(ipaddress.ip_address(ipv6))]:
            raise RuntimeError('Wrong primary IPv6: ' + host)
    for host in mirrors:
        if query(host, 1, transport) != ['192.168.178.70'] or query(host, 28, transport):
            raise RuntimeError('Wrong mirror DNS mapping: ' + host)
    if not query('example.org', 1, transport):
        raise RuntimeError('Upstream DNS forwarding failed')
    print('Split DNS IPv4/IPv6, mirror records and upstream checked over ' + transport)
"""


def corefile():
    ipaddress.IPv4Address(IPV4)
    ipaddress.IPv6Address(IPV6)
    hosts = "".join(f"        {IPV4} {domain}\n        {IPV6} {domain}\n" for domain in DOMAINS)
    hosts += "".join(f"        192.168.178.70 {domain}\n" for domain in MIRRORS)
    block = """    errors
    template IN AAAA www16.mxtracks.info dev16.mxtracks.info {
        rcode NOERROR
    }
    hosts {
""" + hosts + """        ttl 60
        no_reverse
        fallthrough
    }
    forward . 192.168.178.1
"""
    return (".:53 {\n" + block + "}\n" + "tls://.:853 {\n"
            f"    tls /etc/letsencrypt/live/{CERT_NAME}/fullchain.pem "
            f"/etc/letsencrypt/live/{CERT_NAME}/privkey.pem\n" + block + "}\n")


def compose_config(image):
    return {
        "name": "beelink-dns",
        "services": {"dns": {
            "image": image, "user": "0:0", "restart": "no", "read_only": True,
            "networks": ["resolver"],
            "command": ["-conf", "/Corefile"],
            "volumes": [
                apps.mount(WORK / "Corefile", "/Corefile", True),
                apps.mount(apps.RUNTIME / "host-configuration/etc/letsencrypt",
                           "/etc/letsencrypt", True),
            ],
        }},
        "networks": {"resolver": {"driver": "bridge"}},
    }


def prepare(image):
    config = compose_config(image)
    if (WORK / "compose.json").exists():
        if (json.loads((WORK / "compose.json").read_text()) != config
                or (WORK / "Corefile").read_text() != corefile()):
            raise RuntimeError("Existing DNS test configuration differs")
        return
    if (WORK / "Corefile").exists():
        raise RuntimeError("Unmanaged DNS Corefile exists")
    (WORK / "Corefile").write_text(corefile())
    apps.write_json(WORK / "compose.json", config)


def verify():
    cert = apps.RUNTIME / "host-configuration/etc/letsencrypt/live" / CERT_NAME / "fullchain.pem"
    apps.run("openssl", "x509", "-in", str(cert), "-noout", "-checkend", "604800")
    apps.run("openssl", "x509", "-in", str(cert), "-noout", "-checkhost", DOT_HOST)
    apps.run(*COMPOSE, "up", "-d", "--pull", "never")
    container = apps.capture(*COMPOSE, "ps", "--quiet", "dns")
    ip = apps.capture("docker", "inspect", "--format",
                      '{{(index .NetworkSettings.Networks "beelink-dns_resolver").IPAddress}}',
                      container)
    ipaddress.IPv4Address(ip)
    apps.run("docker", "run", "--rm", "--network", "beelink-dns_resolver",
             "--read-only", "--entrypoint", "python",
             "local/github-artifacts:server-relocation", "-c", PROBE, ip, IPV4, IPV6,
             DOT_HOST, json.dumps(DOMAINS), json.dumps(MIRRORS))
    apps.write_json(WORK / "dns-report.json", {
        "domains": list(DOMAINS), "ipv4": IPV4, "ipv6": IPV6,
        "mirror_domains": list(MIRRORS), "dot_hostname": DOT_HOST,
        "checks": "UDP/TCP53 and verified TLS853, A/AAAA and upstream forwarding",
        "published_ports": [], "production_ready": False,
    })
    print("DNS/DoT test complete. LAN DNS, router and retired Nextcloud application unchanged.",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-isolated", action="store_true", required=True)
    parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    os.listdir("/daten")
    if apps.capture("findmnt", "-rn", "-M", "/daten", "-t", "ext4", "-o", "UUID") != \
            "c0c42461-ae4c-4edb-a993-bba94a05ed6c":
        raise RuntimeError("Expected encrypted T7 is not mounted")
    if (apps.RUNTIME.is_symlink() or apps.RUNTIME.stat().st_uid != 0
            or apps.RUNTIME.stat().st_mode & 0o077
            or not (apps.RUNTIME / "staging-restart-report.json").is_file()):
        raise RuntimeError("Complete private cold restart verification first")
    addresses = json.loads(apps.capture("ip", "-j", "-6", "addr", "show"))
    if not any(item["local"] == IPV6 for interface in addresses for item in interface["addr_info"]):
        raise RuntimeError("Expected beelink IPv6 address is not present")
    if WORK.is_symlink():
        raise RuntimeError("DNS work directory must not be a symlink")
    WORK.mkdir(mode=0o700, exist_ok=True)
    with (WORK / "start.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        image = apps.capture("docker", "image", "inspect", "coredns/coredns:1.11.3",
                             "--format", "{{.Id}}")
        prepare(image)
        try:
            verify()
        except BaseException:
            print("ERROR: DNS checks failed; stopping only unexposed DNS test service.", flush=True)
            apps.run(*COMPOSE, "stop")
            raise


if __name__ == "__main__":
    main()
