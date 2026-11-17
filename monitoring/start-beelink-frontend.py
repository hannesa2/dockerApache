#!/usr/bin/env python3
"""Check original TLS names and migrated proxy routes in internal-only Docker."""

import argparse
import fcntl
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket


SPEC = importlib.util.spec_from_file_location(
    "beelink_apps", Path(__file__).with_name("start-beelink-apps.py"))
apps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apps)
WORK = apps.RUNTIME / "frontend"
CERTIFICATES = apps.RUNTIME / "host-configuration/etc/letsencrypt"
COMPOSE = ["docker", "compose", "-p", "beelink-frontend-test",
           "-f", str(WORK / "compose.json")]
HOMEASSISTANT_CONFIG = Path("/home/hannes/homeassistant/config/configuration.yaml")
SITES = {
    "www.mxtracks.info": ("www.mxtracks.info", "beelink-wordpress-test-apache-1"),
    "dev.mxtracks.info": ("dev.mxtracks.info-0002", "beelink-wordpress-test-apache-1"),
    "git.mxtracks.info": ("git.mxtracks.info-0001", "beelink-app-test-gitlab-1"),
    "homeassistant.mxtracks.info": ("homeassistant.mxtracks.info", None),
}
PENDING = (
    "/github", "/surveil", "/nexus", "/temperatur", "/pi-backend",
    "/pi3-live", "/door-live", "/easy-live", "/fly-live",
    "/easy-gyro", "/easy-gyroAll", "/easy-gyroComp",
)
OWNCLOUD_PROXY = r"""
$path = '/mnt/data/config/config.php';
$CONFIG = [];
require $path;
if (($CONFIG['dbhost'] ?? '') !== 'beelink-staging-owncloud-1'
    || ($CONFIG['dbname'] ?? '') !== 'owncloud_stage') {
    throw new Exception('Expected isolated ownCloud configuration');
}
$CONFIG['trusted_domains'] = ['localhost', '127.0.0.1', 'beelink', 'www.mxtracks.info'];
$CONFIG['trusted_proxies'] = [$argv[1]];
$CONFIG['overwritehost'] = 'www.mxtracks.info';
$CONFIG['overwriteprotocol'] = 'https';
$CONFIG['overwritewebroot'] = '/owncloud';
$CONFIG['overwrite.cli.url'] = 'https://www.mxtracks.info/owncloud';
$CONFIG['htaccess.RewriteBase'] = '/';
$temporary = $path . '.proxy.tmp';
if (file_put_contents($temporary, "<?php\n\$CONFIG = " . var_export($CONFIG, true) . ";\n") === false
    || !chmod($temporary, 0600) || !rename($temporary, $path)) {
    throw new Exception('Could not write private ownCloud proxy configuration');
}
"""
WEBSOCKET_CHECK = r"""
import base64
import hashlib
import json
import os
import socket
import ssl
import sys

host = "homeassistant.mxtracks.info"
key = base64.b64encode(os.urandom(16)).decode()
request = (
    f"GET /api/websocket HTTP/1.1\r\nHost: {host}\r\n"
    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
)
with socket.create_connection((sys.argv[1], 443), timeout=15) as tcp:
    with ssl.create_default_context().wrap_socket(tcp, server_hostname=host) as stream:
        stream.sendall(request.encode())
        reader = stream.makefile("rb")
        if reader.readline().rstrip() != b"HTTP/1.1 101 Switching Protocols":
            raise RuntimeError("Expected WebSocket protocol upgrade")
        headers = {}
        while True:
            line = reader.readline()
            if line == b"\r\n":
                break
            if not line:
                raise RuntimeError("Incomplete WebSocket response")
            name, value = line.decode().split(":", 1)
            headers[name.lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise RuntimeError("Invalid WebSocket handshake")
        frame = reader.read(2)
        if len(frame) != 2 or frame[0] != 0x81 or frame[1] & 0x80:
            raise RuntimeError("Expected an unmasked complete text frame")
        length = frame[1]
        if length == 126:
            length = int.from_bytes(reader.read(2), "big")
        elif length == 127:
            length = int.from_bytes(reader.read(8), "big")
        if length > 65536:
            raise RuntimeError("Unexpected WebSocket frame length")
        if json.loads(reader.read(length)).get("type") != "auth_required":
            raise RuntimeError("Expected Home Assistant authentication challenge")
print("Home Assistant verified TLS/WebSocket upgrade and auth_required: OK")
"""


def proxy(path, target):
    return (f"    ProxyPass {path} {target} connectiontimeout=5 timeout=120 retry=0\n"
            f"    ProxyPassReverse {path} {target}\n")


def blocked(path):
    return f"""    <LocationMatch "^{path}(?:/|$)">
        Redirect 503 /
    </LocationMatch>
"""


def vhosts(homeassistant_gateway=None, legacy_copy_sites=False, artifacts=False):
    if homeassistant_gateway is not None:
        ipaddress.IPv4Address(homeassistant_gateway)
    text = """ServerName beelink-frontend-test
SSLProtocol -all +TLSv1.2 +TLSv1.3
SSLSessionCache shmcb:/var/run/apache2/ssl_scache(512000)
ProxyRequests Off
<VirtualHost *:80>
    ServerName localhost
    <Location />
        Require all denied
    </Location>
</VirtualHost>
"""
    sites = dict(SITES)
    if legacy_copy_sites:
        sites = dict(list(sites.items())[:-1] + [
            ("wwwcopy.mxtracks.info", ("wwwcopy.mxtracks.info", "beelink-java-web-apache-1")),
            ("devcopy.mxtracks.info", ("devcopy.mxtracks.info", "beelink-java-web-apache-1")),
        ] + list(sites.items())[-1:])
    for domain, (certificate, backend) in sites.items():
        text += f"""<VirtualHost *:80>
    ServerName {domain}
    Redirect permanent / https://{domain}/
</VirtualHost>
<VirtualHost *:443>
    ServerName {domain}
    SSLEngine on
    SSLCertificateFile /etc/letsencrypt/live/{certificate}/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/{certificate}/privkey.pem
    ProxyPreserveHost On
    AllowEncodedSlashes NoDecode
    RequestHeader set X-Forwarded-Proto "https"
    RequestHeader set X-Forwarded-Port "443"
    RequestHeader unset X-Forwarded-Host
    ErrorDocument 503 "Backend not migrated or deliberately isolated; production cutover pending."
    ErrorLog /var/log/apache2/{domain}-error.log
    CustomLog /var/log/apache2/{domain}-access.log combined
"""
        if not legacy_copy_sites:
            text += f"""    <If "%{{HTTP_HOST}} !~ m#(?i)^{re.escape(domain)}(?::443)?$#">
        Require all denied
    </If>
"""
        if domain == "www.mxtracks.info":
            for path in PENDING:
                if artifacts and path == "/github":
                    continue
                text += blocked(path)
            if artifacts:
                text += """    RedirectMatch 301 ^/github$ /github/
    <LocationMatch "^/github(?:/|$)">
        RequestHeader set X-Forwarded-Prefix "/github"
        Header edit Location "^/$" "/github/"
    </LocationMatch>
"""
                text += proxy("/github/", "http://beelink-artifacts-test-artifacts-1:5000/")
            text += "    ProxyPassMatch ^/owncloud$ !\n"
            text += "    RedirectMatch 301 ^/owncloud$ /owncloud/\n"
            text += proxy("/owncloud/", "http://beelink-app-test-owncloud-app-1:8080/")
            text += proxy("/backend/", "http://beelink-java-web-glassfish-1:8081/")
            text += proxy("/piwik/", "http://beelink-matomo-test-matomo-1/piwik/")
        if domain == "dev.mxtracks.info":
            text += blocked("/pi")
        if domain in ("www.mxtracks.info", "dev.mxtracks.info"):
            text += ("    ProxyPassMatch ^/wordpress$ !\n"
                     "    RedirectMatch 301 ^/wordpress$ /wordpress/\n")
        if backend is None:
            if homeassistant_gateway is None:
                text += '    Redirect 503 /\n'
            else:
                text += (f"    ProxyPass / http://{homeassistant_gateway}:8123/ "
                         "upgrade=websocket connectiontimeout=5 timeout=120 retry=0\n"
                         f"    ProxyPassReverse / http://{homeassistant_gateway}:8123/\n")
        else:
            text += proxy("/", f"http://{backend}/")
        text += "</VirtualHost>\n"
    return text


def fix_path_redirects():
    settings = homeassistant_settings()
    expected = vhosts(settings["gateway"] if settings else None,
                      artifacts=(apps.RUNTIME / "artifacts/prepared").is_file())
    previous = expected.replace("    ProxyPassMatch ^/owncloud$ !\n", "").replace(
        "    ProxyPassMatch ^/wordpress$ !\n"
        "    RedirectMatch 301 ^/wordpress$ /wordpress/\n", "")
    path = WORK / "vhosts/frontend.conf"
    original = path.read_text()
    if path.is_symlink() or original not in (previous, expected):
        raise RuntimeError("Unexpected frontend configuration; no changes made")
    if original != expected:
        backup = WORK / "frontend-before-path-redirects.conf"
        if backup.exists():
            if backup.read_text() != original:
                raise RuntimeError("Existing redirect backup differs; inspect before retrying")
        else:
            shutil.copy2(path, backup)
        temporary = path.with_suffix(".conf.tmp")
        temporary.write_text(expected)
        temporary.replace(path)
    try:
        apps.run(*COMPOSE, "exec", "-T", "frontend", "apache2ctl", "configtest")
        apps.run(*COMPOSE, "exec", "-T", "frontend", "apache2ctl", "graceful")
        for domain, route in (("www.mxtracks.info", "owncloud"),
                              ("www.mxtracks.info", "wordpress"),
                              ("dev.mxtracks.info", "wordpress")):
            headers = apps.capture(
                *COMPOSE, "exec", "-T", "frontend", "curl", "--silent", "--show-error",
                "--head", "--max-time", "15", "--resolve", f"{domain}:443:127.0.0.1",
                f"https://{domain}/{route}").replace("\r", "")
            if (not re.match(r"HTTP/\S+ 301\b", headers)
                    or f"\nLocation: https://{domain}/{route}/\n" not in headers):
                raise RuntimeError(f"Redirect check failed: {domain}/{route}")
    except BaseException:
        path.write_text(original)
        print("ERROR: Redirect update failed; restoring and reloading original frontend.",
              flush=True)
        apps.run(*COMPOSE, "exec", "-T", "frontend", "apache2ctl", "configtest")
        apps.run(*COMPOSE, "exec", "-T", "frontend", "apache2ctl", "graceful")
        raise
    print("HTTPS path redirects checked. Frontend reloaded without container restarts.",
          flush=True)


def compose_config(image, frontend_ip=None):
    config = {
        "name": "beelink-frontend-test",
        "services": {"frontend": {
            "image": image, "restart": "no", "networks": ["isolated"],
            "volumes": [
                apps.mount(WORK / "vhosts", "/etc/apache2/sites-enabled", True),
                apps.mount(CERTIFICATES, "/etc/letsencrypt", True),
                apps.mount(WORK / "logs", "/var/log/apache2"),
            ],
        }},
        "networks": {"isolated": {"external": True, "name": "beelink-staging_isolated"}},
    }
    if frontend_ip is not None:
        ipaddress.IPv4Address(frontend_ip)
        config["services"]["frontend"]["networks"] = {"isolated": {"ipv4_address": frontend_ip}}
    return config


def check_certificates():
    for domain, (name, _) in SITES.items():
        directory = CERTIFICATES / "live" / name
        cert, key = directory / "fullchain.pem", directory / "privkey.pem"
        for path in (cert, key):
            if not path.resolve().is_relative_to(CERTIFICATES.resolve()) or not path.is_file():
                raise RuntimeError("Missing or escaping private TLS material: " + domain)
        apps.run("openssl", "x509", "-in", str(cert), "-noout", "-checkend", "604800")
        apps.run("openssl", "x509", "-in", str(cert), "-noout", "-checkhost", domain)
        certificate_key = apps.capture("openssl", "x509", "-in", str(cert), "-pubkey", "-noout")
        private_key = apps.capture("openssl", "pkey", "-in", str(key), "-pubout")
        if certificate_key != private_key:
            raise RuntimeError("TLS certificate and key differ: " + domain)


def homeassistant_settings():
    path = WORK / "homeassistant-proxy.json"
    if not path.exists():
        return None
    settings = json.loads(path.read_text())
    for key in ("frontend_ip", "gateway"):
        ipaddress.IPv4Address(settings[key])
    return settings


def prepare(image, connect_homeassistant=False):
    settings = homeassistant_settings()
    artifacts = (apps.RUNTIME / "artifacts/prepared").is_file()
    previous_config = compose_config(image)
    previous_vhosts = vhosts()
    if connect_homeassistant and settings is None:
        existing = json.loads((WORK / "compose.json").read_text())
        if existing != previous_config or (WORK / "vhosts/frontend.conf").read_text() != previous_vhosts:
            raise RuntimeError("Complete the unchanged initial frontend staging first")
        container = apps.capture(*COMPOSE, "ps", "--quiet", "frontend")
        if not container:
            raise RuntimeError("Start the existing test frontend before connecting Home Assistant")
        ip = apps.capture("docker", "inspect", "--format",
                          '{{(index .NetworkSettings.Networks "beelink-staging_isolated").IPAddress}}',
                          container)
        network = json.loads(apps.capture(
            "docker", "network", "inspect", "beelink-staging_isolated"))[0]
        if len(network["IPAM"]["Config"]) != 1:
            raise RuntimeError("Expected one isolated network subnet")
        subnet = network["IPAM"]["Config"][0]
        gateway = subnet["Gateway"]
        if ipaddress.IPv4Address(ip) not in ipaddress.ip_network(subnet["Subnet"]):
            raise RuntimeError("Frontend IP is outside the isolated network")
        ipaddress.IPv4Address(gateway)
        settings = {"frontend_ip": ip, "gateway": gateway}
        shutil.copy2(WORK / "compose.json", WORK / "compose-before-homeassistant.json")
        shutil.copy2(WORK / "vhosts/frontend.conf", WORK / "frontend-before-homeassistant.conf")
        apps.write_json(WORK / "homeassistant-proxy.json", settings)
    config = compose_config(image, settings["frontend_ip"] if settings else None)
    text = vhosts(settings["gateway"] if settings else None, artifacts=artifacts)
    if (WORK / "compose.json").exists():
        existing = json.loads((WORK / "compose.json").read_text())
        existing_vhosts = (WORK / "vhosts/frontend.conf").read_text()
        if existing == config and existing_vhosts == text:
            return
        if artifacts and existing == config and existing_vhosts == vhosts(
                settings["gateway"] if settings else None):
            shutil.copy2(WORK / "vhosts/frontend.conf", WORK / "frontend-before-artifacts.conf")
            (WORK / "vhosts/frontend.conf").write_text(text)
            print("Connected isolated read-only GitHub artifact service.", flush=True)
            return
        if existing == config and existing_vhosts == vhosts(
                settings["gateway"] if settings else None, legacy_copy_sites=True):
            shutil.copy2(WORK / "vhosts/frontend.conf", WORK / "frontend-before-copy-retirement.conf")
            (WORK / "vhosts/frontend.conf").write_text(text)
            print("Removed retired copy-domain vhosts; original sites retained.", flush=True)
            return
        if not (connect_homeassistant and existing == previous_config
                and existing_vhosts == previous_vhosts):
            raise RuntimeError("Existing frontend configuration differs; inspect before retrying")
        apps.write_json(WORK / "compose.json", config)
        (WORK / "vhosts/frontend.conf").write_text(text)
        return
    for name in ("vhosts", "logs"):
        if (WORK / name).exists():
            raise RuntimeError("Unmanaged frontend directory: " + name)
        (WORK / name).mkdir()
    (WORK / "vhosts/frontend.conf").write_text(text)
    apps.write_json(WORK / "compose.json", config)


def patch_homeassistant_config(text, frontend_ip):
    ipaddress.IPv4Address(frontend_ip)
    blocks = list(re.finditer(r"(?ms)^http:[ \t]*\n.*?(?=^\S|\Z)", text))
    if len(blocks) != 1:
        raise RuntimeError("Expected one inline Home Assistant http block")
    block = blocks[0]
    original = block.group()
    if not re.search(r"(?m)^  use_x_forwarded_for: true[ \t]*$", original):
        raise RuntimeError("Inspect Home Assistant forwarded-header configuration first")
    lists = list(re.finditer(
        r"(?m)^  trusted_proxies:[ \t]*\n(?:    - [^\n]+\n?)+", original))
    if len(lists) != 1:
        raise RuntimeError("Expected one inline Home Assistant trusted proxy list")
    trusted = lists[0]
    entries = re.findall(r"(?m)^    - ([^\n]+)", trusted.group())
    if any(entry not in ("127.0.0.1", "::1", frontend_ip) for entry in entries):
        raise RuntimeError("Unexpected existing Home Assistant proxy trust; inspect before changing")
    if frontend_ip in entries:
        return text
    updated = (original[:trusted.end()] + ("" if trusted.group().endswith("\n") else "\n")
               + "    - " + frontend_ip + "\n" + original[trusted.end():])
    return text[:block.start()] + updated + text[block.end():]


def configure_homeassistant(settings, allow_restart):
    mounts = json.loads(apps.capture("docker", "inspect", "--format", "{{json .Mounts}}",
                                    "homeassistant"))
    if not any(m["Destination"] == "/config"
               and Path(m["Source"]) == HOMEASSISTANT_CONFIG.parent for m in mounts):
        raise RuntimeError("Unexpected existing Home Assistant config mount")
    if HOMEASSISTANT_CONFIG.is_symlink():
        raise RuntimeError("Home Assistant configuration must not be a symlink")
    original = HOMEASSISTANT_CONFIG.read_text()
    updated = patch_homeassistant_config(original, settings["frontend_ip"])
    if updated == original:
        return
    if not allow_restart:
        raise RuntimeError("Home Assistant proxy trust needs --connect-homeassistant and a brief restart")
    backup = WORK / "homeassistant-before-proxy.yaml"
    if not backup.exists():
        shutil.copy2(HOMEASSISTANT_CONFIG, backup)
        backup.chmod(0o600)
    HOMEASSISTANT_CONFIG.write_text(updated)
    try:
        apps.run("docker", "exec", "homeassistant", "python3", "-m", "homeassistant",
                 "--script", "check_config", "--config", "/config")
    except BaseException:
        HOMEASSISTANT_CONFIG.write_text(original)
        print("ERROR: Home Assistant validation failed; original config restored, no restart.",
              flush=True)
        raise
    print("Validated Home Assistant proxy trust; briefly restarting the existing instance.",
          flush=True)
    apps.run("docker", "restart", "homeassistant")


def matomo_proxy_config(text):
    database = re.search(r"(?ms)^\[database\]\s*\n.*?(?=^\[|\Z)", text)
    if not database or not re.search(
            r'(?m)^host\s*=\s*"beelink-staging-native-mysql-1"\s*$', database.group()):
        raise RuntimeError("Expected isolated Matomo configuration")
    if len(re.findall(r"(?m)^\[General\]\s*$", text)) != 1:
        raise RuntimeError("Expected one Matomo General section")
    text = re.sub(r"(?m)^\s*trusted_hosts\[\]\s*=.*\n?", "", text)
    return re.sub(
        r"(?m)^\[General\]\s*$",
        '[General]\ntrusted_hosts[] = "localhost"\ntrusted_hosts[] = "127.0.0.1"\n'
        'trusted_hosts[] = "www.mxtracks.info"', text, count=1)


def configure_matomo():
    path = apps.RUNTIME / "matomo/www/piwik/config/config.ini.php"
    text = path.read_text()
    updated = matomo_proxy_config(text)
    backup = WORK / "matomo-before-proxy.ini.php"
    if not backup.exists():
        backup.write_text(text)
        backup.chmod(0o600)
    path.write_text(updated)


def response(domain, route, https=True):
    port, scheme = (443, "https") if https else (80, "http")
    result = apps.capture(
        *COMPOSE, "exec", "-T", "frontend", "curl", "--silent", "--show-error",
        "--max-time", "40", "--resolve", f"{domain}:{port}:127.0.0.1",
        "--write-out", "\n%{http_code}", f"{scheme}://{domain}{route}")
    return result.rsplit("\n", 1)


def verify(connect_homeassistant=False):
    apps.run(*COMPOSE, "up", "-d")
    apps.run(*COMPOSE, "exec", "-T", "frontend", "apache2ctl", "configtest")
    apps.run(*COMPOSE, "exec", "-T", "frontend", "apache2ctl", "graceful")
    apps.run("docker", "compose", "-p", "beelink-java-web", "-f",
             str(apps.RUNTIME / "java-web/compose.json"), "stop", "apache")
    (apps.RUNTIME / "java-web/copy-sites-retired").touch()
    container = apps.capture(*COMPOSE, "ps", "--quiet", "frontend")
    ip = apps.capture("docker", "inspect", "--format",
                      '{{(index .NetworkSettings.Networks "beelink-staging_isolated").IPAddress}}',
                      container)
    ipaddress.ip_address(ip)
    settings = homeassistant_settings()
    if settings:
        if ip != settings["frontend_ip"]:
            raise RuntimeError("Pinned frontend IP differs; refusing Home Assistant trust change")
        configure_homeassistant(settings, connect_homeassistant)
        apps.COMPOSE = COMPOSE
        apps.wait_http("frontend", ["curl", "--fail", "--silent", "--max-time", "5",
                                   "-H", "X-Forwarded-For: 192.0.2.1",
                                   f"http://{settings['gateway']}:8123/"], attempts=60)
    source = apps.APPS / "owncloud/volume/config/config.php"
    backup = WORK / "owncloud-before-proxy.php"
    if not backup.exists():
        backup.write_bytes(source.read_bytes())
        backup.chmod(0o600)
    apps.run("docker", "exec", "--user", "www-data", "beelink-app-test-owncloud-app-1",
             "php", "-r", OWNCLOUD_PROXY, "--", ip)
    configure_matomo()
    checks = [
        ("www.mxtracks.info", "/wordpress/", 200, "wp-content/"),
        ("dev.mxtracks.info", "/wordpress/", 200, "wp-content/"),
        ("www.mxtracks.info", "/backend/MXServer/", 200, None),
        ("www.mxtracks.info", "/backend/MXServer/rest/version", 401, None),
        ("www.mxtracks.info", "/piwik/index.php", 200, "Matomo"),
        ("www.mxtracks.info", "/owncloud/status.php", 200, '"installed":true'),
        ("www.mxtracks.info", "/owncloud/index.php/login", 200, "ownCloud"),
        ("git.mxtracks.info", "/users/sign_in", 200, "GitLab"),
        ("homeassistant.mxtracks.info", "/", 200 if settings else 503,
         "home-assistant" if settings else "cutover pending"),
    ]
    report = []
    for domain, route, expected, marker in checks:
        body, status = response(domain, route)
        if status != str(expected) or "<?php" in body or (marker and marker not in body):
            raise RuntimeError(f"TLS proxy check failed: {domain}{route}, HTTP {status}")
        report.append({"domain": domain, "path": route, "status": int(status)})
        print(f"TLS proxy checked: {domain}{route} -> {status}", flush=True)
    for domain in SITES:
        if response(domain, "/", https=False)[1] != "301":
            raise RuntimeError("Missing HTTPS redirect: " + domain)
    for domain in ("wwwcopy.mxtracks.info", "devcopy.mxtracks.info"):
        status = apps.capture(
            *COMPOSE, "exec", "-T", "frontend", "curl", "--silent", "--show-error",
            "--max-time", "10", "--resolve", "www.mxtracks.info:443:127.0.0.1",
            "-H", "Host: " + domain, "--output", "/dev/null", "--write-out", "%{http_code}",
            "https://www.mxtracks.info/")
        if status != "403":
            raise RuntimeError("Retired copy hostname still serves content: " + domain)
    for route in PENDING:
        if route == "/github" and (apps.RUNTIME / "artifacts/prepared").is_file():
            continue
        if response("www.mxtracks.info", route)[1] != "503":
            raise RuntimeError("Unmigrated route is not explicitly blocked: " + route)
    if settings:
        apps.run("docker", "exec", "homeassistant", "python3", "-c", WEBSOCKET_CHECK,
                 settings["frontend_ip"])
    apps.write_json(WORK / "frontend-report.json", {
        "checks": report, "tls": "Original certificates, hostname and chain verification enabled",
        "pending_routes": [route for route in PENDING if route != "/github"
                           or not (apps.RUNTIME / "artifacts/prepared").is_file()]
                          + ["dev:/pi"] + ([] if settings else ["homeassistant:/"]),
        "homeassistant": "TLS HTTP and WebSocket authentication challenge checked" if settings else
                         "Not yet connected",
        "owncloud": "HTTPS subpath configured; status checked, authenticated WebDAV still pending",
        "mxserver": "Anonymous HTTP and expected authentication challenge; authenticated API pending",
        "production_ready": False,
    })
    print("Isolated TLS/proxy checks complete. No host ports, source jobs or router settings changed.",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start-isolated", action="store_true")
    action.add_argument("--fix-path-redirects", action="store_true",
                        help="Fix exact ownCloud/WordPress paths with a graceful reload only")
    parser.add_argument("--connect-homeassistant", action="store_true",
                        help="Pin frontend IP, validate proxy trust and briefly restart Home Assistant")
    args = parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    os.listdir("/daten")
    if apps.capture("findmnt", "-rn", "-M", "/daten", "-t", "ext4", "-o", "UUID") != \
            "c0c42461-ae4c-4edb-a993-bba94a05ed6c":
        raise RuntimeError("Expected encrypted T7 is not mounted")
    if (apps.RUNTIME.is_symlink() or apps.RUNTIME.stat().st_uid != 0
            or apps.RUNTIME.stat().st_mode & 0o077
            or not (apps.RUNTIME / "wordpress/wordpress-report.json").is_file()):
        raise RuntimeError("Complete original WordPress staging first")
    if not json.loads(apps.capture(
            "docker", "network", "inspect", "beelink-staging_isolated"))[0]["Internal"]:
        raise RuntimeError("Frontend must remain on the internal test network")
    if WORK.is_symlink():
        raise RuntimeError("Frontend directory must not be a symlink")
    WORK.mkdir(mode=0o700, exist_ok=True)
    with (WORK / "start.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.fix_path_redirects:
            fix_path_redirects()
            return
        check_certificates()
        image = apps.capture("docker", "image", "inspect", "beelink-source-apache:20261116",
                             "--format", "{{.Id}}")
        prepare(image, args.connect_homeassistant)
        try:
            verify(args.connect_homeassistant)
        except BaseException:
            print("ERROR: Frontend checks failed; stopping only the test frontend.", flush=True)
            apps.run(*COMPOSE, "stop")
            raise


if __name__ == "__main__":
    main()
