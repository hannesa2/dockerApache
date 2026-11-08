#!/usr/bin/env python3
"""Recreate the stopped Latitude GitLab with bounded Docker logs."""

import copy
import datetime
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True)


class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def main():
    os.umask(0o077)
    if len(sys.argv) != 2 or sys.argv[1] != "--apply":
        raise SystemExit("Usage: python3 monitoring/recreate-gitlab-logrotation.py --apply")
    original = json.loads(docker("inspect", "gitlab"))[0]
    if original["State"]["Running"]:
        raise SystemExit("GitLab must be stopped before recreation.")
    if original["HostConfig"]["NetworkMode"] != "bridge":
        raise SystemExit("Unexpected network: review deployment before recreation.")
    expected = {
        "/etc/gitlab": "/srv/gitlab/config",
        "/var/log/gitlab": "/srv/gitlab/logs",
        "/var/opt/gitlab": "/srv/gitlab/data",
    }
    mounts = {m["Destination"]: m["Source"] for m in original["Mounts"]}
    if mounts != expected or any(m["Type"] != "bind" for m in original["Mounts"]):
        raise SystemExit("Unexpected GitLab storage: refusing recreation.")
    if original["HostConfig"]["AutoRemove"]:
        raise SystemExit("Unexpected AutoRemove setting.")
    log_config = {"Type": "json-file", "Config": {"max-size": "50m", "max-file": "3"}}
    if original["HostConfig"]["LogConfig"] == log_config:
        raise SystemExit("Log rotation already configured; do not recreate again.")
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path.home() / ".local/state/gitlab-logrotation" / timestamp
    backup_dir.mkdir(parents=True, mode=0o700)
    (backup_dir / "container-inspect.json").write_text(json.dumps(original, indent=2))
    payload = copy.deepcopy(original["Config"])
    payload["Image"] = original["Image"]
    payload["HostConfig"] = copy.deepcopy(original["HostConfig"])
    payload["HostConfig"]["LogConfig"] = log_config
    version = docker("version", "--format", "{{.Server.APIVersion}}").strip()
    backup_name = "gitlab-pre-logrotation-" + timestamp
    renamed = False
    created = False
    try:
        docker("update", "--restart=no", "gitlab")
        docker("rename", "gitlab", backup_name)
        renamed = True
        connection = DockerConnection("localhost", timeout=120)
        try:
            connection.request(
                "POST", "/v" + version + "/containers/create?name=gitlab",
                body=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            if response.status != 201:
                raise RuntimeError("Docker create failed: " + str(result.get("message")))
            created = True
        finally:
            connection.close()
        for warning in result.get("Warnings") or []:
            print("Docker warning:", warning, file=sys.stderr)
        replacement = json.loads(docker("inspect", "gitlab"))[0]
        for key in ("Env", "Cmd", "Entrypoint", "Hostname", "User", "WorkingDir", "Healthcheck"):
            if replacement["Config"].get(key) != original["Config"].get(key):
                raise RuntimeError("Recreated container differs at Config." + key)
        for key in ("Binds", "PortBindings", "RestartPolicy", "ShmSize", "NetworkMode"):
            if replacement["HostConfig"][key] != original["HostConfig"][key]:
                raise RuntimeError("Recreated container differs at HostConfig." + key)
        if replacement["Image"] != original["Image"]:
            raise RuntimeError("GitLab image changed unexpectedly.")
        if replacement["HostConfig"]["LogConfig"] != log_config:
            raise RuntimeError("Log bounds were not applied.")
    except Exception:
        if created:
            docker("rm", "gitlab")
        if renamed:
            docker("rename", backup_name, "gitlab")
        policy = original["HostConfig"]["RestartPolicy"]
        restart = policy["Name"]
        if restart == "on-failure" and policy.get("MaximumRetryCount"):
            restart += ":" + str(policy["MaximumRetryCount"])
        docker("update", "--restart=" + restart, "gitlab")
        raise
    # Never automatically start the rollback container on shared database files.
    docker("start", "gitlab")
    print("GitLab started with its original immutable image and 3 x 50 MB Docker logs.")
    print("Stopped rollback container:", backup_name)
    print("Protected original configuration:", backup_dir)
    print("Never start the rollback container alongside production: they share database files.")


if __name__ == "__main__":
    main()
