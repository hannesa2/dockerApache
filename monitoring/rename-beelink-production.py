#!/usr/bin/env python3
"""Migrate existing beelink production names without moving or restoring data."""

import argparse
import contextlib
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time


ROOT = Path(__file__).resolve().parent
RUNTIME = Path("/daten/beelink-runtime")
UNITS = Path("/etc/systemd/system")
LIBEXEC = Path("/usr/local/libexec/beelink-staging")
OLD_UNIT = "beelink-staging.service"
NEW_UNIT = "beelink-production.service"
OLD_NETWORK = "beelink-staging_isolated"
NEW_NETWORK = "beelink-databases_isolated"
PROJECTS = {
    "beelink-staging": "beelink-databases",
    "beelink-app-test": "beelink-apps",
    "beelink-wordpress-test": "beelink-wordpress",
    "beelink-matomo-test": "beelink-matomo",
    "beelink-artifacts-test": "beelink-artifacts",
    "beelink-dns-test": "beelink-dns",
    "beelink-frontend-test": "beelink-frontend",
}
PROTECTED = ("homeassistant", "camera-postgres-db-1")
MARKER = RUNTIME / "production-names.json"
BACKUPS = Path("/var/backups/beelink-production-names")


def renamed(text):
    text = text.replace(OLD_UNIT, NEW_UNIT).replace(OLD_NETWORK, NEW_NETWORK)
    text = text.replace("beelink-dns-test_dns_test", "beelink-dns_resolver")
    text = re.sub(r"beelink-staging-(?!control[.]lock|restart-test[.]lock)",
                  "beelink-databases-", text)
    text = re.sub(r"(?<![\w/.-])beelink-staging(?![\w/.-])", "beelink-databases", text)
    for old, new in PROJECTS.items():
        if old != "beelink-staging":
            text = text.replace(old, new)
    return text.replace('"dns_test"', '"resolver"').replace("'dns_test'", "'resolver'").replace(
        "Description=Isolated beelink migration stacks (not production)",
        "Description=Beelink production applications and databases")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def inspect(kind, name):
    return json.loads(capture("docker", *([kind] if kind else []), "inspect", name))[0]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def active(unit):
    return capture("systemctl", "show", unit, "-p", "ActiveState", "--value") == "active"


def identity(name):
    info = inspect("", name)
    return {"id": info["Id"], "started": info["State"]["StartedAt"],
            "running": info["State"]["Running"]}


def bind_mounts(info):
    return sorted((mount["Source"], mount["Destination"], mount["RW"])
                  for mount in info["Mounts"] if mount["Type"] == "bind")


def compose(stack, *args):
    project, path, services = stack
    run("docker", "compose", "-p", project, "-f", str(path), *args, *services)


def managed(stacks):
    return [project + "-" + service + "-1"
            for project, _, services in stacks for service in services]


def database_config(text, network):
    config = json.loads(renamed(text))
    ipam = network["IPAM"]["Config"][0]
    config["networks"]["isolated"]["ipam"] = {"config": [
        {"subnet": ipam["Subnet"], "gateway": ipam["Gateway"]}]}
    return json.dumps(config, indent=2) + "\n"


def file_plan(stacks, network):
    database_files = {RUNTIME / "database-compose.json",
                      Path("/daten/beelink-staging/compose.json")}
    if len({json.dumps(json.loads(path.read_text()), sort_keys=True)
            for path in database_files}) != 1:
        raise RuntimeError("Source and root-controlled database Compose snapshots differ")
    paths = {path for _, path, _ in stacks}
    paths.add(Path("/daten/beelink-staging/compose.json"))
    paths.update(LIBEXEC.glob("*.py"))
    paths.update(Path("/usr/local/libexec/beelink-jobs").rglob("*.py"))
    paths.update(Path("/usr/local/libexec/beelink-jobs").rglob("*.sh"))
    paths.update(UNITS.glob("beelink-*.service"))
    paths.update(UNITS.glob("beelink-*.socket"))
    paths.update((
        Path("/usr/local/bin/beelink-job-status"),
        Path("/usr/local/libexec/renew-beelink-certificates.py"),
        Path("/usr/local/libexec/activate-beelink-operations.py"),
        RUNTIME / "apps/owncloud/volume/config/config.php",
        RUNTIME / "java-web/glassfish/glassfish/domains/domain1/config/domain.xml",
        RUNTIME / "matomo/www/piwik/config/config.ini.php",
    ))
    paths.update((RUNTIME / "wordpress/www").glob("*/wordpress/wp-config.php"))
    paths.update((RUNTIME / "frontend/vhosts").glob("*.conf"))
    plan = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Expected regular configuration: " + str(path))
        original = path.read_text()
        updated = database_config(original, network) if path in database_files else renamed(original)
        if updated != original:
            plan[path] = updated
    for path in (UNITS / OLD_UNIT, UNITS / "beelink-certificates.service",
                 Path("/usr/local/bin/beelink-job-status"),
                 RUNTIME / "database-compose.json",
                 RUNTIME / "apps/owncloud/volume/config/config.php",
                 RUNTIME / "java-web/glassfish/glassfish/domains/domain1/config/domain.xml"):
        if path not in plan:
            raise RuntimeError("Required old naming reference missing: " + str(path))
    return plan


def save(backup, plan):
    metadata = {}
    for index, path in enumerate(plan):
        stat = path.stat()
        filename = str(index)
        shutil.copyfile(path, backup / filename)
        (backup / filename).chmod(0o600)
        metadata[str(path)] = dict(file=filename, uid=stat.st_uid, gid=stat.st_gid,
                                   mode=stat.st_mode & 0o777)
    (backup / "files.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def restore_files(backup, metadata):
    for filename, attributes in metadata.items():
        path = Path(filename)
        shutil.copyfile(backup / attributes["file"], path)
        os.chown(path, attributes["uid"], attributes["gid"])
        path.chmod(attributes["mode"])


def remove_containers(names):
    for name in names:
        result = subprocess.run(["docker", "container", "inspect", name],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            # Distinguish an absent container from an unavailable Docker daemon.
            run("docker", "info", stdout=subprocess.DEVNULL)
            continue
        run("docker", "stop", "--time", "60", name, stdout=subprocess.DEVNULL)
        run("docker", "rm", name, stdout=subprocess.DEVNULL)


def remove_network(name):
    listing = capture("docker", "network", "ls", "--format", "{{.Name}}").splitlines()
    if name not in listing:
        return
    network = inspect("network", name)
    if network.get("Containers"):
        raise RuntimeError("Network still has endpoints: " + name)
    run("docker", "network", "rm", name)


def create_network(network, name):
    validate_network(network)
    ipam = network["IPAM"]["Config"][0]
    command = ["docker", "network", "create", "--internal", "--driver", "bridge",
               "--subnet", ipam["Subnet"], "--gateway", ipam["Gateway"]]
    for key, value in (network.get("Labels") or {}).items():
        command.extend(("--label", key + "=" + value))
    run(*command, name)


def validate_network(network):
    if (network["Driver"] != "bridge" or not network["Internal"]
            or network.get("EnableIPv6") or network.get("Options")
            or len(network["IPAM"]["Config"]) != 1):
        raise RuntimeError("Unexpected internal production network configuration")
    ipam = network["IPAM"]["Config"][0]
    if ipam != {"Subnet": "172.19.0.0/16", "Gateway": "172.19.0.1"}:
        raise RuntimeError("Expected the existing pinned production subnet and gateway")


def wait_health(paused_timers=()):
    if any(not unit.startswith("beelink-") or not unit.endswith(".timer")
           for unit in paused_timers):
        raise ValueError("Only explicitly paused beelink timers may be excluded")
    expected = {"Inactive unit: " + unit for unit in paused_timers}
    for _ in range(24):
        result = json.loads(capture("python3", "/usr/local/bin/beelink-job-status",
                                    "--health-json"))
        failures = [failure for failure in result["failures"] if failure not in expected]
        if not failures:
            return
        print("Waiting for health: " + "; ".join(failures), flush=True)
        time.sleep(10)
    raise RuntimeError("Production health checks did not settle")


def apply():
    old = load(LIBEXEC / "control-beelink-staging.py", "old_production_control")
    old.preflight()
    if not active(OLD_UNIT) or MARKER.exists() or (UNITS / NEW_UNIT).exists():
        raise RuntimeError("Expected the existing, not-yet-renamed production service")
    stacks = old.STACKS
    new_stacks = tuple((PROJECTS.get(project, project), path, services)
                       for project, path, services in stacks)
    names = managed(stacks)
    new_names = managed(new_stacks)
    existing = capture("docker", "ps", "-a", "--format", "{{.Names}}").splitlines()
    if set(existing) & (set(new_names) - set(names)):
        raise RuntimeError("Target production container name already exists")
    networks = capture("docker", "network", "ls", "--format", "{{.Name}}").splitlines()
    if NEW_NETWORK in networks or "beelink-dns_resolver" in networks:
        raise RuntimeError("Target network already exists")
    network = inspect("network", OLD_NETWORK)
    validate_network(network)
    retired = "beelink-java-web-apache-1"
    if set(item["Name"] for item in network["Containers"].values()) - set(names) - {retired}:
        raise RuntimeError("Foreign containers use the production network")
    retired_info = inspect("", retired)
    if retired_info["State"]["Running"]:
        raise RuntimeError("Retired Apache must remain stopped")
    protected = {name: identity(name) for name in PROTECTED}
    mounts = {renamed(name): bind_mounts(inspect("", name)) for name in names}
    plan = file_plan(stacks, network)
    for _, path, _ in new_stacks:
        candidate = json.loads(plan.get(path, path.read_text()))
        original = json.loads(path.read_text())
        for service, definition in original["services"].items():
            if definition.get("volumes") != candidate["services"][service].get("volumes"):
                raise RuntimeError("Renaming would change bind mounts: " + str(path))
    timers = [line for line in capture(
        "systemctl", "list-units", "--type=timer", "--state=active",
        "--plain", "--no-legend", "beelink-*").splitlines()]
    timers = [line.split()[0] for line in timers]
    for unit in ["beelink-certificates.service", "beelink-operations-check.service"] + [
            f"beelink-job-{job}.service"
            for job in ("apache", "gitlab", "owncloud", "mxserver", "matomo",
                        "postgresql", "monitor")]:
        state = capture("systemctl", "show", unit, "-p", "ActiveState", "--value")
        if state not in {"inactive", "failed"}:
            raise RuntimeError("Wait for the running operation: " + unit)
    backup = BACKUPS / dt.datetime.now().strftime(
        "%Y%m%dT%H%M%S%f")
    backup.mkdir(mode=0o700, parents=True)
    metadata = save(backup, plan)
    (backup / "network.json").write_text(json.dumps(network, indent=2))
    print("Private rollback inventory: " + str(backup), flush=True)
    sockets = ["beelink-public-web-80.socket", "beelink-public-web-443.socket"]
    retired_disconnected = False
    stopped = False
    recovered = True
    try:
        if timers:
            run("systemctl", "stop", *timers)
        with lifecycle_lock():
            for unit in ["beelink-certificates.service", "beelink-operations-check.service"] + [
                    f"beelink-job-{job}.service" for job in (
                        "apache", "gitlab", "owncloud", "mxserver", "matomo",
                        "postgresql", "monitor")]:
                state = capture("systemctl", "show", unit, "-p", "ActiveState", "--value")
                if state not in {"inactive", "failed"}:
                    raise RuntimeError("Operation started before timers stopped: " + unit)
        # ExecStop/ExecStart acquire the same lifecycle lock themselves.
        stopped = True
        recovered = False
        run("systemctl", "stop", *sockets,
            "beelink-public-web-80.service", "beelink-public-web-443.service", OLD_UNIT)
        with lifecycle_lock():
            remove_containers(names)
            if OLD_NETWORK in inspect("", retired)["NetworkSettings"]["Networks"]:
                run("docker", "network", "disconnect", "--force", OLD_NETWORK, retired)
                retired_disconnected = True
            remove_network(OLD_NETWORK)
            for path, text in plan.items():
                path.write_text(text)
            shutil.copyfile(UNITS / OLD_UNIT, UNITS / NEW_UNIT)
            (UNITS / NEW_UNIT).chmod(0o644)
            run("systemctl", "disable", OLD_UNIT)
            (UNITS / OLD_UNIT).unlink()
            for stack in new_stacks:
                compose(stack, "up", "-d", "--pull", "never", "--no-build")
                if stack == new_stacks[0]:
                    fresh = load(LIBEXEC / "control-beelink-staging.py", "renamed_control")
                    fresh.wait_databases()
        run("systemctl", "daemon-reload")
        run("systemd-analyze", "verify", NEW_UNIT, *sockets,
            env={**os.environ, "SYSTEMD_UNIT_PATH":
                 "/etc/systemd/system:/run/systemd/generator.early:"
                 "/run/systemd/generator:/run/systemd/generator.late:"})
        run("systemctl", "enable", NEW_UNIT)
        run("systemctl", "start", NEW_UNIT, *sockets)
        wait_health(paused_timers=timers)
        for name, expected in mounts.items():
            if bind_mounts(inspect("", name)) != expected:
                raise RuntimeError("Bind mounts changed: " + name)
        for name, expected in protected.items():
            if identity(name) != expected:
                raise RuntimeError("Protected service changed: " + name)
        remove_network("beelink-dns-test_dns_test")
        recovered = True
    except BaseException:
        print("ERROR: Naming migration failed; restoring previous names/configuration.",
              flush=True)
        if stopped:
            # Avoid running the new ExecStop against partially restored files.
            run("systemctl", "stop", *(
                [NEW_UNIT] if (UNITS / NEW_UNIT).exists() else []), *sockets,
                "beelink-public-web-80.service", "beelink-public-web-443.service")
            if (UNITS / NEW_UNIT).exists():
                run("systemctl", "disable", NEW_UNIT)
            with lifecycle_lock():
                remove_containers(new_names)
                remove_network(NEW_NETWORK)
                remove_network("beelink-dns_resolver")
                restore_files(backup, metadata)
                if (UNITS / NEW_UNIT).exists():
                    (UNITS / NEW_UNIT).unlink()
                if OLD_NETWORK not in capture(
                        "docker", "network", "ls", "--format", "{{.Name}}").splitlines():
                    create_network(network, OLD_NETWORK)
                for stack in stacks:
                    compose(stack, "up", "-d", "--pull", "never", "--no-build")
                    if stack == stacks[0]:
                        old.wait_databases()
                if retired_disconnected:
                    run("docker", "network", "connect", OLD_NETWORK, retired)
            run("systemctl", "daemon-reload")
            run("systemctl", "enable", OLD_UNIT)
            run("systemctl", "start", OLD_UNIT, *sockets)
            wait_health(paused_timers=timers)
            recovered = True
        raise
    finally:
        if timers and recovered:
            run("systemctl", "start", *timers)
            for timer in timers:
                run("systemctl", "is-active", "--quiet", timer)
            wait_health()
        elif timers:
            print("ERROR: Recovery incomplete; scheduled jobs remain stopped. "
                  "Use private rollback inventory: " + str(backup), flush=True)
    MARKER.write_text(json.dumps({"completed": dt.datetime.now().isoformat(),
                                  "rollback": str(backup),
                                  "projects": PROJECTS}, indent=2) + "\n")
    print("Production names installed; live health and unchanged data mounts verified. "
          "Home Assistant and camera DB untouched. No backup/restore jobs repeated.", flush=True)


@contextlib.contextmanager
def lifecycle_lock():
    # Keep the existing lock identity across the transition, including old callers.
    with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", required=True, action="store_true")
    parser.parse_args()
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    os.umask(0o077)
    with Path("/run/lock/beelink-production-rename.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        apply()


if __name__ == "__main__":
    main()
