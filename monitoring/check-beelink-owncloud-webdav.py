#!/usr/bin/env python3
"""Exercise authenticated WebDAV only in the isolated beelink ownCloud copy."""

import argparse
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess


SPEC = importlib.util.spec_from_file_location(
    "beelink_control", Path(__file__).with_name("control-beelink-staging.py"))
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)
OWNCLOUD = "beelink-app-test-owncloud-app-1"
CLIENT = "beelink-artifacts-test-artifacts-1"
OCC = ["docker", "exec", "--user", "www-data", OWNCLOUD, "occ"]
PENDING = control.RUNTIME / "owncloud-webdav-pending.json"
REPORT = control.RUNTIME / "owncloud-webdav-report.json"
PROBE = r"""
import base64
import hashlib
import http.client
import json
import socket
import ssl
import sys
import xml.etree.ElementTree as ET

settings = json.load(sys.stdin)
host = "www.mxtracks.info"
base = "/owncloud/remote.php/dav/files/" + settings["username"] + "/"
folder = base + "beelink-webdav-check/"
file = folder + "probe.bin"
payload = b"beelink isolated WebDAV check\n" + bytes(range(256)) * 4
authorization = "Basic " + base64.b64encode(
    (settings["username"] + ":" + settings["password"]).encode()).decode()

def request(method, path, expected, body=None, headers=None, authenticated=True):
    connection = http.client.HTTPSConnection(host, timeout=30)
    sock = socket.create_connection((settings["ip"], 443), timeout=15)
    connection.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    try:
        outgoing = dict(headers or {})
        if authenticated:
            outgoing["Authorization"] = authorization
        connection.request(method, path, body=body, headers=outgoing)
        response = connection.getresponse()
        data = response.read()
        if response.status != expected:
            raise RuntimeError("WebDAV %s expected %d, received %d" %
                               (method, expected, response.status))
        return data
    finally:
        connection.close()

request("PROPFIND", base, 401, headers={"Depth": "0"}, authenticated=False)
request("MKCOL", folder, 201)
request("PUT", file, 201, body=payload, headers={"Content-Type": "application/octet-stream"})
download = request("GET", file, 200)
if download != payload:
    raise RuntimeError("WebDAV binary download differs from uploaded bytes")
listing = ET.fromstring(request("PROPFIND", folder, 207, headers={"Depth": "1"}))
entries = listing.findall("{DAV:}response")
found = False
for entry in entries:
    if entry.findtext("{DAV:}href") != file:
        continue
    for propstat in entry.findall("{DAV:}propstat"):
        if propstat.findtext("{DAV:}status") == "HTTP/1.1 200 OK" and \
                propstat.findtext("{DAV:}prop/{DAV:}getcontentlength") == str(len(payload)):
            found = True
if not found:
    raise RuntimeError("WebDAV listing did not contain the uploaded file and its size")
request("DELETE", folder, 204)
request("GET", file, 404)
print(json.dumps({"anonymous_status": 401, "upload_bytes": len(payload),
                  "download_sha256": hashlib.sha256(download).hexdigest(),
                  "propfind_status": 207, "deleted_file_status": 404}))
"""


def users(username):
    return json.loads(control.capture(*OCC, "user:list", "--output=json", username))


def check_target():
    control.preflight()
    for key, expected in (("dbhost", "beelink-staging-owncloud-1"),
                          ("dbname", "owncloud_stage"),
                          ("datadirectory", "/mnt/data/files")):
        if control.capture(*OCC, "config:system:get", key) != expected:
            raise RuntimeError("Unexpected isolated ownCloud configuration: " + key)
    status = json.loads(control.capture(*OCC, "status", "--output=json"))
    if not status.get("installed") or status.get("maintenance") or status.get("needsDbUpgrade"):
        raise RuntimeError("ownCloud must be installed, upgraded and outside maintenance mode")
    if PENDING.exists():
        raise RuntimeError("Inspect unfinished test account recorded in " + str(PENDING))
    frontend = json.loads(control.capture(
        "docker", "inspect", "beelink-frontend-test-frontend-1"))[0]
    return frontend["NetworkSettings"]["Networks"]["beelink-staging_isolated"]["IPAddress"]


def verify(ip):
    username = "beelink-dav-" + secrets.token_hex(12)
    password = secrets.token_urlsafe(32)
    if username in users(username):
        raise RuntimeError("Generated WebDAV test account already exists; refusing to alter it")
    directory = "/mnt/data/files/" + username
    control.run("docker", "exec", "--user", "www-data", OWNCLOUD, "test", "!", "-e", directory)
    PENDING.write_text(json.dumps({"username": username}) + "\n")
    result = None
    try:
        # Send the generated password through stdin, not command arguments or a file.
        control.run("docker", "exec", "-i", "--user", "www-data", OWNCLOUD,
                    "sh", "-c", 'IFS= read -r OC_PASS; export OC_PASS; exec occ '
                    'user:add --no-interaction --password-from-env "$1"', "sh", username,
                    input=password + "\n", text=True, stdout=subprocess.DEVNULL)
        probe = subprocess.run(
            ["docker", "exec", "-i", CLIENT, "python", "-c", PROBE],
            input=json.dumps({"username": username, "password": password, "ip": ip}),
            text=True, capture_output=True, check=False)
        if probe.stderr:
            print(probe.stderr, end="", flush=True)
        probe.check_returncode()
        result = json.loads(probe.stdout)
    finally:
        print("Removing only the disposable ownCloud WebDAV test account.", flush=True)
        if username in users(username):
            control.run(*OCC, "user:delete", "--no-interaction", username,
                        stdout=subprocess.DEVNULL)
        if username in users(username):
            raise RuntimeError("WebDAV test account cleanup failed; see " + str(PENDING))
        control.run("docker", "exec", "--user", "www-data", OWNCLOUD, "test", "!", "-e", directory)
        PENDING.unlink()
    result.update({"checked_at": datetime.now(timezone.utc).isoformat(),
                   "temporary_account_removed": True, "production_ready": False})
    temporary = REPORT.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    temporary.replace(REPORT)
    print("Verified HTTPS WebDAV authentication, binary upload/download, listing and deletion; "
          "temporary account and files removed.", flush=True)


def interrupted(signum, frame):
    raise SystemExit("WebDAV test interrupted by signal " + str(signum))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-isolated", action="store_true", required=True)
    parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    signal.signal(signal.SIGTERM, interrupted)
    with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify(check_target())


if __name__ == "__main__":
    main()
