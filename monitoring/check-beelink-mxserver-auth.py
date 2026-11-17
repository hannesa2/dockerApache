#!/usr/bin/env python3
"""Verify MXServer's file-realm API with a disposable user in isolated staging."""

import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


SPEC = importlib.util.spec_from_file_location(
    "beelink_control", Path(__file__).with_name("control-beelink-staging.py"))
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)
CONTAINER = "beelink-java-web-glassfish-1"
WORK = control.RUNTIME / "java-web"
KEYFILE = WORK / "glassfish/glassfish/domains/domain1/config/keyfile"
KEYFILE_IN_CONTAINER = "/opt/glassfish-4.0/glassfish/domains/domain1/config/keyfile"
CLASS_PATH = "/opt/glassfish-4.0/glassfish/modules/*"
REMOTE_SOURCE = "/tmp/FileRealmProbe.java"
REMOTE_CLASS = "/tmp/FileRealmProbe"
DOMAIN = "www.mxtracks.info"
ENDPOINT = "/backend/MXServer/rest/version"
PENDING = WORK / "mxserver-auth-pending.json"
REPORT = WORK / "mxserver-auth-report.json"
JAVA_SOURCE = r"""
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import org.glassfish.security.common.FileRealmHelper;

public final class FileRealmProbe {
    public static void main(String[] args) throws Exception {
        FileRealmHelper realm = new FileRealmHelper(args[1]);
        switch (args[0]) {
            case "replace":
                if (!realm.getUserNames().contains(args[2])) {
                    throw new IllegalStateException("Expected mapped staging user is missing");
                }
                char[] password = new BufferedReader(
                    new InputStreamReader(System.in, StandardCharsets.UTF_8)).readLine().toCharArray();
                realm.removeUser(args[2]);
                realm.addUser(args[2], password, new String[0]);
                realm.persist();
                Arrays.fill(password, '\0');
                break;
            case "verify":
                char[] candidate = new BufferedReader(
                    new InputStreamReader(System.in, StandardCharsets.UTF_8)).readLine().toCharArray();
                String[] groups = realm.authenticate(args[2], candidate);
                Arrays.fill(candidate, '\0');
                if (groups == null) {
                    throw new IllegalStateException("Staging file-realm authentication failed");
                }
                System.out.println("Staging file-realm credentials verified");
                break;
            default:
                throw new IllegalArgumentException("Unsupported realm action");
        }
    }
}
"""


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def inspect_target():
    control.preflight()
    if capture("systemctl", "is-active", "beelink-staging.service") != "active":
        raise RuntimeError("Managed isolated staging service is not active")
    if not KEYFILE.is_file() or KEYFILE.is_symlink():
        raise RuntimeError("Expected private isolated GlassFish file-realm keyfile")
    if not KEYFILE.resolve().is_relative_to(control.RUNTIME.resolve()):
        raise RuntimeError("GlassFish file-realm keyfile escaped the private runtime")
    descriptor = WORK / "glassfish/glassfish/domains/domain1/applications/" \
        "MXServer-1.9.5/WEB-INF/glassfish-web.xml"
    if not descriptor.is_file() or descriptor.is_symlink():
        raise RuntimeError("Expected deployed MXServer security-role descriptor")
    root = ET.parse(descriptor).getroot()
    user_roles = [mapping for mapping in root.iter()
                  if mapping.tag.rsplit("}", 1)[-1] == "security-role-mapping"
                  and any(child.tag.rsplit("}", 1)[-1] == "role-name"
                          and (child.text or "").strip() == "UserRole"
                          for child in mapping)]
    principals = [child.text.strip() for mapping in user_roles for child in mapping
                  if child.tag.rsplit("}", 1)[-1] == "principal-name" and child.text]
    if principals != ["user"]:
        raise RuntimeError("Unexpected MXServer UserRole principal mapping; refusing auth test")
    web = ET.parse(WORK / "glassfish/glassfish/domains/domain1/applications/"
                   "MXServer-1.9.5/WEB-INF/web.xml").getroot()
    protected_rest = any(
        any(node.tag.rsplit("}", 1)[-1] == "url-pattern"
            and (node.text or "").strip() == "/rest/*" for node in constraint.iter())
        and any(node.tag.rsplit("}", 1)[-1] == "role-name"
                and (node.text or "").strip() in {"UserRole", "AdminRole"}
                for node in constraint.iter())
        for constraint in web.iter()
        if constraint.tag.rsplit("}", 1)[-1] == "security-constraint")
    if not protected_rest:
        raise RuntimeError("MXServer REST authentication rules differ from expected")
    details = json.loads(capture("docker", "inspect", CONTAINER))[0]
    if (details["HostConfig"]["RestartPolicy"]["Name"] != "no"
            or details["HostConfig"].get("PortBindings")
            or set(details["NetworkSettings"]["Networks"]) != {"beelink-staging_isolated"}
            or not details["State"]["Running"]):
        raise RuntimeError("MXServer must remain in the isolated non-restarting test container")
    frontend = json.loads(capture(
        "docker", "inspect", "beelink-frontend-test-frontend-1"))[0]
    return frontend["NetworkSettings"]["Networks"]["beelink-staging_isolated"]["IPAddress"]


def api_request(ip, username=None, secret=None):
    connection = http.client.HTTPSConnection(DOMAIN, timeout=30)
    sock = socket.create_connection((ip, 443), timeout=15)
    connection.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=DOMAIN)
    try:
        headers = {}
        if username is not None:
            token = base64.b64encode((username + ":" + secret).encode()).decode("ascii")
            headers["Authorization"] = "Basic " + token
        connection.request("GET", ENDPOINT, headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, len(body)
    finally:
        connection.close()


def realm_action(action, username, secret=None):
    try:
        with tempfile.TemporaryDirectory(prefix="beelink-auth-", dir="/run") as temp:
            source = Path(temp) / "FileRealmProbe.java"
            source.write_text(JAVA_SOURCE)
            source.chmod(0o600)
            run("docker", "cp", str(source), CONTAINER + ":" + REMOTE_SOURCE,
                stdout=subprocess.DEVNULL)
            run("docker", "exec", CONTAINER, "javac", "-cp", CLASS_PATH,
                "-d", "/tmp", REMOTE_SOURCE, stdout=subprocess.DEVNULL)
            command = ["docker", "exec", "-i", CONTAINER, "java", "-cp",
                       "/tmp:" + CLASS_PATH, "FileRealmProbe", action,
                       KEYFILE_IN_CONTAINER, username]
            result = subprocess.run(command, input=(secret + "\n") if secret else "",
                                    text=True, capture_output=True, check=False)
            if result.stdout:
                print(result.stdout, end="", flush=True)
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr, flush=True)
            result.check_returncode()
    finally:
        subprocess.run(["docker", "exec", CONTAINER, "rm", "-f",
                        REMOTE_SOURCE, REMOTE_CLASS + ".class"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_ready(attempts=60):
    for _ in range(attempts):
        details = json.loads(capture("docker", "inspect", CONTAINER))[0]
        if not details["State"]["Running"]:
            raise RuntimeError("Isolated GlassFish stopped during the API authentication check")
        result = subprocess.run(
            ["docker", "exec", CONTAINER, "curl", "--fail", "--silent", "--max-time", "10",
             "http://127.0.0.1:8081/MXServer/"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return
        time.sleep(5)
    raise RuntimeError("Isolated GlassFish did not become ready after its controlled restart")


def verify(ip):
    username = "user"
    password = secrets.token_hex(32)
    protected = {name: control.container_identity(name)
                 for name in ("homeassistant", "camera-postgres-db-1")}
    original = KEYFILE.read_bytes()
    if api_request(ip)[0] != 401:
        raise RuntimeError("Expected anonymous MXServer API request to return HTTP 401")
    if PENDING.exists():
        raise RuntimeError("Existing MXServer authentication test state; inspect before retrying")
    backup = WORK / "mxserver-keyfile-before-auth-test"
    metadata = KEYFILE.stat()
    backup.write_bytes(original)
    backup.chmod(0o600)
    marker = {"username": username, "backup": backup.name}
    PENDING.write_text(json.dumps(marker) + "\n")
    PENDING.chmod(0o600)
    authenticated_status = None
    operation_error = None
    modified = None
    try:
        realm_action("replace", username, password)
        if KEYFILE.read_bytes() == original:
            raise RuntimeError("Temporary staging password was not persisted")
        modified = KEYFILE.read_bytes()
        marker["modified_sha256"] = hashlib.sha256(modified).hexdigest()
        PENDING.write_text(json.dumps(marker) + "\n")
        PENDING.chmod(0o600)
        realm_action("verify", username, password)
        run("docker", "restart", "--time", "60", CONTAINER, stdout=subprocess.DEVNULL)
        wait_ready()
        authenticated_status, response_bytes = api_request(ip, username, password)
        if authenticated_status != 200 or response_bytes == 0:
            raise RuntimeError(
                "Authenticated MXServer API request failed with HTTP " + str(authenticated_status))
    except BaseException as error:
        operation_error = error
    finally:
        try:
            current = KEYFILE.read_bytes()
            if current != original:
                if modified is None or current != modified:
                    raise RuntimeError(
                        "File-realm changed unexpectedly; recovery files retained")
                temporary = KEYFILE.with_suffix(".keyfile-restore")
                shutil.copy2(backup, temporary)
                os.chmod(temporary, metadata.st_mode & 0o777)
                os.chown(temporary, metadata.st_uid, metadata.st_gid)
                os.replace(temporary, KEYFILE)
                if KEYFILE.read_bytes() != original:
                    raise RuntimeError("Original GlassFish keyfile was not restored")
            if modified is not None:
                run("docker", "restart", "--time", "60", CONTAINER, stdout=subprocess.DEVNULL)
                wait_ready()
                anonymous_status, _ = api_request(ip)
                removed_user_status, _ = api_request(ip, username, password)
                if anonymous_status != 401 or removed_user_status != 401:
                    raise RuntimeError("Restored file realm did not reject test credentials")
                if any(control.container_identity(name) != identity
                       for name, identity in protected.items()):
                    raise RuntimeError("Home Assistant or camera database changed during MXServer test")
        except BaseException as cleanup_error:
            raise RuntimeError(
                "MXServer test cleanup failed; inspect private marker and backup: " + str(PENDING)
            ) from cleanup_error
        backup.unlink()
        PENDING.unlink()
    if operation_error is not None:
        raise operation_error
    report = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "anonymous_status": 401, "disposable_authenticated_status": authenticated_status,
              "removed_test_user_status": 401, "temporary_user_removed": True,
              "original_keyfile_restored": True, "protected_services_unchanged": True,
              "production_ready": False}
    temporary = REPORT.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(REPORT)
    print("Authenticated MXServer REST API and file-realm role verified; "
          "temporary user removed and original keyfile restored.", flush=True)


def interrupted(signum, frame):
    raise SystemExit("MXServer API test interrupted by signal " + str(signum))


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
        verify(inspect_target())


if __name__ == "__main__":
    main()
