#!/usr/bin/env python3
"""Prepare authenticated GitHub artifact uploads and downloads using the proven image."""

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket


SPEC = importlib.util.spec_from_file_location(
    "beelink_frontend", Path(__file__).with_name("start-beelink-frontend.py"))
frontend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(frontend)
apps = frontend.apps
WORK = apps.RUNTIME / "artifacts"
IMAGE = "local/github-artifacts:production-features"
COMPOSE = ["docker", "compose", "-p", "beelink-artifacts", "-f", str(WORK / "compose.json")]
CONFIGURE = r"""
from pathlib import Path
import os
import yaml

config = yaml.safe_load(Path('/source/parameter.yml').read_text())
if config['file']['directory'] != '/daten/githubArtifactsPy' or config['server']['sub_url'] != '/github':
    raise RuntimeError('Expected existing GitHub artifact configuration')
for key in ('username', 'password'):
    if not isinstance(config['authentication'][key], str) or not config['authentication'][key]:
        raise RuntimeError('Missing original artifact credentials')
config['file']['directory'] = '/app/files'
path = Path('/target/parameter.yml')
path.write_text(yaml.safe_dump(config))
path.chmod(0o600)
os.chown(path, 10001, 10001)
"""
CHECK_AUTH = r"""
from pathlib import Path
import base64
import urllib.request
import yaml

config = yaml.safe_load(Path('/app/parameter.yml').read_text())
credentials = config['authentication']
value = base64.b64encode((credentials['username'] + ':' + credentials['password']).encode()).decode()
request = urllib.request.Request('http://127.0.0.1:5000/hostname/',
    headers={'Authorization': 'Basic ' + value})
with urllib.request.urlopen(request, timeout=10) as response:
    if response.status != 200 or 'beelink-artifacts' not in response.read().decode():
        raise RuntimeError('Artifact application authentication failed')
print('Original artifact credentials authenticate; no writes performed')
"""
CHECK_UPLOAD = r"""
import base64
import secrets
import urllib.error
import urllib.parse
import urllib.request
import yaml

with open('/app/parameter.yml') as stream:
    credentials = yaml.safe_load(stream)['authentication']
token = base64.b64encode(
    (credentials['username'] + ':' + credentials['password']).encode()).decode()
name = 'beelink-probe-' + secrets.token_hex(8) + '.txt'
payload = b'authenticated artifact upload probe'
boundary = '----beelink-' + secrets.token_hex(12)
body = (
    f'--{boundary}\r\nContent-Disposition: form-data; name="job"\r\n\r\nprobe\r\n'
    f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
    'Content-Type: text/plain\r\n\r\n'
).encode() + payload + f'\r\n--{boundary}--\r\n'.encode()
auth = {'Authorization': 'Basic ' + token}
url = 'http://127.0.0.1:5000/'
try:
    request = urllib.request.Request(
        url, data=body, headers={**auth, 'Content-Type': 'multipart/form-data; boundary=' + boundary})
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.read() != payload or not response.geturl().endswith('/uploads/probe_' + name):
            raise RuntimeError('Authenticated upload/download probe failed')
    protected = urllib.request.Request(url, data=body,
                                       headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    try:
        urllib.request.urlopen(protected, timeout=15)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise RuntimeError('Unauthenticated upload was not rejected')
    else:
        raise RuntimeError('Unauthenticated upload was accepted')
    delete = urllib.request.Request(
        'http://127.0.0.1:5000/delete',
        data=urllib.parse.urlencode({'name': 'probe_' + name}).encode(),
        headers={**auth, 'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(delete, timeout=15):
        pass
    try:
        urllib.request.urlopen('http://127.0.0.1:5000/uploads/probe_' + name, timeout=15)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise RuntimeError('Uploaded probe cleanup failed')
    else:
        raise RuntimeError('Uploaded probe remains after cleanup')
finally:
    cleanup = urllib.request.Request(
        'http://127.0.0.1:5000/delete',
        data=urllib.parse.urlencode({'name': 'probe_' + name}).encode(),
        headers={**auth, 'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        urllib.request.urlopen(cleanup, timeout=15).close()
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
print('Authenticated upload, unauthenticated rejection, download and cleanup checked')
"""


def compose_config(image):
    return {
        "name": "beelink-artifacts",
        "services": {"artifacts": {
            "image": image, "hostname": "beelink-artifacts",
            "user": "10001:10001", "restart": "no", "networks": ["isolated"],
            "read_only": True, "tmpfs": ["/tmp"],
            "volumes": [
                apps.mount(WORK / "files", "/app/files"),
                apps.mount(WORK / "parameter.yml", "/app/parameter.yml", True),
            ],
        }},
        "networks": {"isolated": {"external": True, "name": "beelink-databases_isolated"}},
    }


def prepare(image):
    config = compose_config(image)
    if (WORK / "compose.json").exists():
        existing = json.loads((WORK / "compose.json").read_text())
        previous = json.loads(json.dumps(config))
        previous["services"]["artifacts"]["image"] = existing["services"]["artifacts"]["image"]
        previous["services"]["artifacts"]["volumes"][0]["read_only"] = True
        if existing not in (config, previous) or not (WORK / "prepared").is_file():
            raise RuntimeError("Existing artifact staging differs or is incomplete")
        if existing != config:
            ids = apps.capture(*COMPOSE, "ps", "--all", "--quiet").splitlines()
            if any(apps.capture("docker", "inspect", "--format", "{{.State.Running}}", item)
                   != "false" for item in ids):
                raise RuntimeError("Stop artifacts before enabling uploads")
            for path in (WORK / "files", WORK / "parameter.yml"):
                if path.is_symlink() or not path.exists():
                    raise RuntimeError("Missing or unsafe artifact input: " + str(path))
            apps.write_json(WORK / "compose.json", config)
        return
    if (WORK / "files").exists() or (WORK / "parameter.yml").exists():
        raise RuntimeError("Unmanaged artifact working files exist")
    source = apps.SEED / "github-artifacts-extra/files"
    if source.is_symlink() or not source.is_dir() or not any(source.iterdir()):
        raise RuntimeError("Missing separately seeded original artifact files")
    if any(path.is_symlink() or not path.is_file() for path in source.iterdir()):
        raise RuntimeError("Expected only regular artifact files")
    configuration = apps.ROOTFS / "home/hannes/git/githubArtifactsPy/parameter.yml"
    if not configuration.is_file() or configuration.is_symlink():
        raise RuntimeError("Missing original artifact credentials in private seed")
    apps.run("rsync", "-rt", str(source) + "/", str(WORK / "files") + "/")
    for path in (WORK / "files").iterdir():
        os.chown(path, 10001, 10001)
        path.chmod(0o640)
    os.chown(WORK / "files", 10001, 10001)
    (WORK / "files").chmod(0o750)
    apps.run("docker", "run", "--rm", "--network", "none", "--user", "0:0",
             "--entrypoint", "python",
             "--mount", f"type=bind,source={configuration.parent},target=/source,readonly",
             "--mount", f"type=bind,source={WORK},target=/target", image, "-c", CONFIGURE)
    apps.write_json(WORK / "compose.json", config)
    (WORK / "prepared").touch()


def verify():
    apps.run(*COMPOSE, "up", "-d")
    apps.COMPOSE = COMPOSE
    apps.wait_http("artifacts", ["python", "-c",
                               "from urllib.request import urlopen; urlopen('http://127.0.0.1:5000/',timeout=5)"])
    apps.run(*COMPOSE, "exec", "-T", "artifacts", "python", "-c", CHECK_AUTH)
    apps.run(*COMPOSE, "exec", "-T", "artifacts", "python", "-c", CHECK_UPLOAD)
    apps.run("python3", str(Path(__file__).with_name("start-beelink-frontend.py")),
             "--start-isolated")
    body, status = frontend.response("www.mxtracks.info", "/github/")
    if status != "200" or '/github/version' not in body or '/github/update' not in body:
        raise RuntimeError("Artifact prefix-aware HTML check failed")
    version, status = frontend.response("www.mxtracks.info", "/github/version")
    if status != "200" or not json.loads(version).get("version"):
        raise RuntimeError("Artifact version endpoint failed through TLS proxy")
    if frontend.response("www.mxtracks.info", "/github/hostname/")[1] != "401":
        raise RuntimeError("Artifact authentication challenge missing")
    path = next(iter(sorted((WORK / "files").iterdir())))
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    probe = r"""
import hashlib
import http.client
import ssl
import sys
import urllib.parse
import urllib.request

ip, filename, checksum = sys.argv[1:]
class Connection(http.client.HTTPSConnection):
    def connect(self):
        import socket
        sock = socket.create_connection((ip, 443), timeout=15)
        self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
class Handler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(Connection, request)
url = 'https://www.mxtracks.info/github/uploads/' + urllib.parse.quote(filename, safe='')
with urllib.request.build_opener(Handler()).open(url, timeout=20) as response:
    if response.status != 200 or hashlib.sha256(response.read()).hexdigest() != checksum:
        raise RuntimeError('Artifact TLS download hash mismatch')
print('Original artifact downloaded through verified TLS; SHA256 matches')
"""
    settings = frontend.homeassistant_settings()
    if not settings:
        raise RuntimeError("Expected pinned frontend IP from completed Home Assistant integration")
    apps.run(*COMPOSE, "exec", "-T", "artifacts", "python", "-c", probe,
             settings["frontend_ip"], path.name, checksum)
    apps.write_json(WORK / "artifacts-report.json", {
        "files": len(list((WORK / "files").iterdir())),
        "authentication": "Authenticated upload checked; unauthenticated upload returns 401",
        "tls": "Prefix HTML, version, original file SHA256 download checked",
        "writable_data_only": True, "production_ready": False,
    })
    print("Artifact Docker/TLS checks complete. Authenticated upload verified; sync jobs remain disabled.",
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
            or not (frontend.WORK / "frontend-report.json").is_file()
            or not frontend.homeassistant_settings()):
        raise RuntimeError("Complete private frontend/Home Assistant staging first")
    if not json.loads(apps.capture(
            "docker", "network", "inspect", "beelink-databases_isolated"))[0]["Internal"]:
        raise RuntimeError("Artifact staging must remain internal-only")
    if shutil.disk_usage(apps.RUNTIME).free < 5 * 1024**3:
        raise RuntimeError("At least 5 GiB free required")
    if WORK.is_symlink():
        raise RuntimeError("Artifact work directory must not be a symlink")
    WORK.mkdir(mode=0o700, exist_ok=True)
    with (WORK / "start.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        image = apps.capture("docker", "image", "inspect", IMAGE, "--format", "{{.Id}}")
        prepare(image)
        try:
            verify()
        except BaseException:
            print("ERROR: Artifact staging failed; stopping only artifact test service.", flush=True)
            apps.run(*COMPOSE, "stop")
            raise


if __name__ == "__main__":
    main()
