#!/usr/bin/env python3
"""Control beelink production stacks with a fail-closed T7 mount guard."""

import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time


UUID = "c0c42461-ae4c-4edb-a993-bba94a05ed6c"
RUNTIME = Path("/daten/beelink-runtime")
LIBEXEC = Path("/usr/local/libexec/beelink-staging")
UNIT = Path("/etc/systemd/system/beelink-production.service")
DATABASE_SOURCE = Path("/daten/beelink-staging/compose.json")
STACKS = (
    ("beelink-databases", RUNTIME / "database-compose.json",
     ("mxserver", "cameras", "owncloud", "apache-mysql", "native-mysql")),
    ("beelink-apps", RUNTIME / "apps/compose.json", ("redis", "owncloud-app", "gitlab")),
    ("beelink-java-web", RUNTIME / "java-web/compose.json", ("glassfish", "wildfly")),
    ("beelink-wordpress", RUNTIME / "wordpress/compose.json", ("apache",)),
    ("beelink-matomo", RUNTIME / "matomo/compose.json", ("matomo",)),
    ("beelink-artifacts", RUNTIME / "artifacts/compose.json", ("artifacts",)),
    ("beelink-dns", RUNTIME / "dns/compose.json", ("dns",)),
    ("beelink-frontend", RUNTIME / "frontend/compose.json", ("frontend",)),
)
UNIT_TEXT = """[Unit]
Description=Beelink production applications and databases
Requires=docker.service
After=docker.service daten.mount
BindsTo=daten.mount
RequiresMountsFor=/daten

[Service]
Type=oneshot
RemainAfterExit=yes
UMask=0077
ExecStart=/usr/bin/python3 /usr/local/libexec/beelink-staging/control-beelink-staging.py --start
ExecStop=/usr/bin/python3 /usr/local/libexec/beelink-staging/control-beelink-staging.py --stop
TimeoutStartSec=25min
TimeoutStopSec=20min

[Install]
WantedBy=multi-user.target
"""


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def check_mount():
    # Access triggers the existing automount, but autofs alone is not sufficient.
    os.listdir("/daten")
    if capture("findmnt", "-rn", "-M", "/daten", "-t", "ext4", "-o", "UUID") != UUID:
        raise RuntimeError("Expected encrypted T7 ext4 filesystem is not mounted; refusing startup")


def normalized_bindings(bindings):
    return {key: sorted((str(ipaddress.ip_address(binding["HostIp"])), binding["HostPort"])
                        for binding in values)
            for key, values in bindings.items() if values}


def dns_bindings():
    return {
        f"{port}/{protocol}": [
            {"HostIp": host_ip, "HostPort": str(port)}
            for host_ip in ("192.168.178.142", "fdf4:be15:98e0:0:7a55:36ff:fe0e:d07e")]
        for port, protocol in ((53, "udp"), (53, "tcp"), (853, "tcp"))
    }


def validate_config(project, config, services):
    dns = project == "beelink-dns"
    network_name = "resolver" if dns else "isolated"
    if config.get("name") != project:
        raise RuntimeError("Unexpected Compose project: " + project)
    if not set(services).issubset(config["services"]):
        raise RuntimeError("Missing staging services: " + project)
    for name in services:
        service = config["services"][name]
        expected_networks = {network_name}
        if (project == "beelink-apps" and name == "gitlab") or project == "beelink-wordpress":
            expected_networks.add("egress")
        if service.get("restart") != "no" or service.get("privileged") \
                or service.get("network_mode"):
            raise RuntimeError("Staging isolation/restart policy differs: " + project + "/" + name)
        networks = service.get("networks")
        if not isinstance(networks, (list, dict)) or set(networks) != expected_networks:
            raise RuntimeError("Unexpected staging service network")
        ports = service.get("ports", [])
        if project == "beelink-apps" and name == "gitlab":
            expected_ports = [{"target": 22, "published": 40022,
                               "host_ip": "192.168.178.142", "protocol": "tcp"}]
        elif dns:
            expected_ports = [
                {"target": port, "published": port, "host_ip": host_ip,
                 "protocol": protocol}
                for host_ip in ("192.168.178.142",
                                "fdf4:be15:98e0:0:7a55:36ff:fe0e:d07e")
                for port, protocol in ((53, "udp"), (53, "tcp"), (853, "tcp"))
            ]
        else:
            expected_ports = []
        if ports != expected_ports:
            raise RuntimeError("Unexpected published ports: " + project + "/" + name)
        for mount in service.get("volumes", []):
            source = Path(mount["source"])
            if (mount.get("type") != "bind" or not source.is_relative_to("/daten")
                    or not mount.get("bind", {}).get("create_host_path") is False
                    or not source.exists() or not source.resolve().is_relative_to("/daten")):
                raise RuntimeError("Missing or unsafe staging bind mount")
        if project == "beelink-artifacts":
            volumes = service.get("volumes", [])
            mount_modes = {mount["target"]: mount.get("read_only", False)
                           for mount in volumes}
            if (not service.get("read_only")
                    or len(volumes) != len(mount_modes)
                    or mount_modes != {"/app/files": False, "/app/parameter.yml": True}
                    or service.get("tmpfs") != ["/tmp"]):
                raise RuntimeError("Only artifact files may be writable in the artifact service")
    network = config.get("networks", {}).get(network_name, {})
    if dns:
        if network != {"driver": "bridge"}:
            raise RuntimeError("Unexpected dedicated DNS network definition")
        if not config["services"]["dns"].get("read_only") or any(
                not mount.get("read_only") for mount in config["services"]["dns"]["volumes"]):
            raise RuntimeError("DNS filesystem and configuration must remain read-only")
    elif project in ("beelink-apps", "beelink-wordpress"):
        if config.get("networks", {}).get("egress") != {"driver": "bridge"}:
            raise RuntimeError("Unexpected application egress network definition")
        if network.get("name") != "beelink-databases_isolated" or not network.get("external"):
            raise RuntimeError("Unexpected staging network reference")
    elif project == "beelink-databases":
        if network.get("internal") is not True:
            raise RuntimeError("Database network must be internal")
    elif network.get("name") != "beelink-databases_isolated" or not network.get("external"):
        raise RuntimeError("Unexpected staging network reference")


def preflight(allow_stopped_recreation=False):
    check_mount()
    if (RUNTIME.is_symlink() or RUNTIME.stat().st_uid != 0
            or RUNTIME.stat().st_mode & 0o077):
        raise RuntimeError("Expected private root-owned runtime")
    for report in ("frontend/frontend-report.json", "wordpress/wordpress-report.json",
                   "artifacts/artifacts-report.json", "dns/dns-report.json",
                   "java-web/copy-sites-retired"):
        if not (RUNTIME / report).is_file():
            raise RuntimeError("Complete existing staging checks first: " + report)
    for project, path, services in STACKS:
        if path.is_symlink() or path.stat().st_uid != 0:
            raise RuntimeError("Expected root-owned Compose configuration")
        validate_config(project, json.loads(path.read_text()), services)
    network = json.loads(capture("docker", "network", "inspect", "beelink-databases_isolated"))[0]
    if not network["Internal"]:
        raise RuntimeError("Existing staging network is not internal")
    if allow_stopped_recreation:
        stopped = True
        for project, _, services in STACKS:
            for service in services:
                name = project + "-" + service + "-1"
                if capture("docker", "inspect", "--format", "{{.State.Running}}", name) != "false":
                    stopped = False
        if stopped:
            print("Validated desired Compose configuration; stopped containers will be recreated.",
                  flush=True)
            return
    for project, _, services in STACKS:
        for service in services:
            name = project + "-" + service + "-1"
            details = json.loads(capture("docker", "inspect", name))[0]
            expected_networks = {
                "beelink-dns_resolver" if project == "beelink-dns"
                else "beelink-databases_isolated"}
            if (project == "beelink-apps" and service == "gitlab") \
                    or project == "beelink-wordpress":
                expected_networks.add(project + "_egress")
            expected_bindings = {}
            if project == "beelink-apps" and service == "gitlab":
                expected_bindings = {
                    "22/tcp": [{"HostIp": "192.168.178.142", "HostPort": "40022"}]}
            elif project == "beelink-dns":
                expected_bindings = dns_bindings()
            if (details["HostConfig"]["RestartPolicy"]["Name"] != "no"
                    or normalized_bindings(details["HostConfig"].get("PortBindings") or {})
                    != normalized_bindings(expected_bindings)
                    or set(details["NetworkSettings"]["Networks"]) != expected_networks):
                raise RuntimeError("Existing container bypasses staging restrictions: " + name)
            if project == "beelink-artifacts":
                mounts = {mount["Destination"]: mount["RW"] for mount in details["Mounts"]}
                if (not details["HostConfig"]["ReadonlyRootfs"]
                        or mounts != {"/app/files": True, "/app/parameter.yml": False}
                        or set(details["HostConfig"].get("Tmpfs") or {}) != {"/tmp"}):
                    raise RuntimeError("Artifact container has an unexpected writable path")
    for project, service in (("beelink-apps", "gitlab"),
                             ("beelink-wordpress", "apache")):
        network = json.loads(capture("docker", "network", "inspect",
                                     project + "_egress"))[0]
        if (network["Driver"] != "bridge" or network.get("Internal")
                or [item["Name"] for item in network.get("Containers", {}).values()]
                != [project + "-" + service + "-1"]):
            raise RuntimeError("Egress network must contain only its approved application")
    dns_network = json.loads(capture("docker", "network", "inspect",
                                     "beelink-dns_resolver"))[0]
    if dns_network["Driver"] != "bridge" or dns_network.get("Internal") or any(
            item["Name"] != "beelink-dns-dns-1"
            for item in dns_network.get("Containers", {}).values()):
        raise RuntimeError("DNS network must contain only the dedicated DNS test service")
    print("T7 UUID, private paths, bind mounts, internal network and restart policies checked.",
          flush=True)


def wait_databases(attempts=180):
    commands = [
        ("beelink-databases-" + service + "-1",
         ("pg_isready", "-U", "beelink_restore", "-d", "postgres"))
        for service in ("mxserver", "cameras", "owncloud")
    ] + [
        ("beelink-databases-" + service + "-1",
         ("mysql", "--defaults-file=/run/restore-client.cnf", "--protocol=socket",
          "--batch", "--skip-column-names", "-e", "SELECT 1"))
        for service in ("apache-mysql", "native-mysql")
    ]
    for _ in range(attempts):
        pending = []
        for name, command in commands:
            if capture("docker", "inspect", "--format", "{{.State.Running}}", name) != "true":
                raise RuntimeError("Database container stopped during startup: " + name)
            result = subprocess.run(["docker", "exec", name, *command],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                pending.append((name, command))
        if not pending:
            return
        commands = pending
        time.sleep(5)
    raise RuntimeError("Database readiness timed out: " + ", ".join(name for name, _ in commands))


def stop():
    # No /daten access: also usable when the mount disappeared.
    for project, _, services in reversed(STACKS):
        ids = capture("docker", "ps", "--all", "--quiet",
                      "--filter", "label=com.docker.compose.project=" + project).splitlines()
        for container in ids:
            details = json.loads(capture("docker", "inspect", container))[0]
            labels = details["Config"].get("Labels") or {}
            service = labels.get("com.docker.compose.service")
            if service not in services or labels.get("com.docker.compose.oneoff", "").lower() == "true":
                continue
            if details["State"]["Running"]:
                run("docker", "stop", "--time", "60", container)
    print("Only managed production containers stopped; Home Assistant and existing camera DB retained.",
          flush=True)


def wait_applications(attempts=180):
    deadline = time.monotonic() + 600
    probes = [
        ("beelink-apps-gitlab-1", ("curl", "--fail", "--silent", "--max-time", "10",
                                     "http://127.0.0.1/-/readiness")),
        ("beelink-apps-owncloud-app-1", ("curl", "--fail", "--silent", "--max-time", "10",
                                           "http://127.0.0.1:8080/status.php")),
        ("beelink-java-web-glassfish-1", ("curl", "--fail", "--silent", "--max-time", "10",
                                        "http://127.0.0.1:8081/MXServer/")),
        ("beelink-java-web-wildfly-1", ("curl", "--fail", "--silent", "--max-time", "10",
                                      "http://127.0.0.1:8080/")),
    ]
    for attempt in range(attempts):
        if time.monotonic() >= deadline:
            break
        pending = []
        for name, command in probes:
            if capture("docker", "inspect", "--format", "{{.State.Running}}", name) != "true":
                raise RuntimeError("Application stopped during startup: " + name)
            result = subprocess.run(["docker", "exec", name, *command],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                pending.append((name, command))
        if not pending:
            return
        if attempt % 12 == 0:
            print("Waiting for application startup: " + ", ".join(name for name, _ in pending),
                  flush=True)
        probes = pending
        time.sleep(5)
    raise RuntimeError("Application readiness timed out: " + ", ".join(name for name, _ in probes))


def start():
    preflight(allow_stopped_recreation=True)
    try:
        for index, (project, path, services) in enumerate(STACKS):
            check_mount()
            run("docker", "compose", "-p", project, "-f", str(path),
                "up", "-d", "--pull", "never", "--no-build", *services)
            if index == 0:
                wait_databases()
        preflight()
        wait_applications()
        helper = Path(__file__).with_name("start-beelink-artifacts.py")
        # Existing prepared artifacts are read-only; this also repeats frontend
        # TLS and Home Assistant WebSocket checks, without a Home Assistant restart.
        run("/usr/bin/python3", str(helper), "--start-isolated")
        run("/usr/bin/python3", str(Path(__file__).with_name("start-beelink-dns.py")),
            "--start-isolated")
        print("Managed production stacks started and TLS/download checks passed. Boot enablement unchanged.",
              flush=True)
    except BaseException:
        print("ERROR: Production startup failed; stopping only managed production stacks.", flush=True)
        stop()
        raise


def install(prepared=False):
    install_database_snapshot()
    preflight(allow_stopped_recreation=prepared)
    sources = ("control-beelink-staging.py", "start-beelink-apps.py",
               "start-beelink-frontend.py", "start-beelink-artifacts.py",
               "start-beelink-dns.py", "prepare-beelink-gitlab-runner.py")
    if LIBEXEC.is_symlink() or UNIT.is_symlink():
        raise RuntimeError("Managed installation paths must not be symlinks")
    if UNIT.exists() and UNIT.read_text() != UNIT_TEXT:
        raise RuntimeError("Existing systemd unit differs; inspect before replacing")
    LIBEXEC.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name in sources:
        source = Path(__file__).with_name(name)
        target = LIBEXEC / name
        if target.is_symlink():
            raise RuntimeError("Managed helper must not be a symlink")
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        os.chown(target, 0, 0)
        target.chmod(0o644)
    candidate = LIBEXEC / "beelink-production.service"
    candidate.write_text(UNIT_TEXT)
    run("systemd-analyze", "verify", str(candidate), env={
        **os.environ,
        "SYSTEMD_UNIT_PATH": "/run/systemd/generator.early:/run/systemd/generator:"
                             "/run/systemd/generator.late:",
    })
    shutil.copyfile(candidate, UNIT)
    UNIT.chmod(0o644)
    run("systemctl", "daemon-reload")
    print("Root-owned controller and mount-bound unit installed. Automatic boot start NOT enabled.",
          flush=True)


def install_database_snapshot():
    check_mount()
    if (RUNTIME.is_symlink() or RUNTIME.stat().st_uid != 0
            or RUNTIME.stat().st_mode & 0o077):
        raise RuntimeError("Expected private root-owned runtime")
    destination = RUNTIME / "database-compose.json"
    source = DATABASE_SOURCE
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("Expected existing database Compose definition")
    config = json.loads(source.read_text())
    validate_config(STACKS[0][0], config, STACKS[0][2])
    if destination.exists() or destination.is_symlink():
        if (destination.is_symlink() or destination.stat().st_uid != 0
                or json.loads(destination.read_text()) != config):
            raise RuntimeError("Existing root database snapshot differs; inspect before retrying")
        return
    with destination.open("x") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
    destination.chmod(0o600)
    print("Validated database Compose snapshotted privately for root-controlled startup.",
          flush=True)


def container_identity(name):
    details = json.loads(capture("docker", "inspect", name))[0]
    return {"id": details["Id"], "started": details["State"]["StartedAt"],
            "running": details["State"]["Running"]}


def restart_test():
    if capture("systemctl", "is-active", "beelink-production.service") != "active":
        raise RuntimeError("Start the existing managed staging unit before testing its restart")
    protected = {name: container_identity(name)
                 for name in ("homeassistant", "camera-postgres-db-1")}
    before = {project + "-" + service + "-1": container_identity(project + "-" + service + "-1")
              for project, _, services in STACKS for service in services}
    if not all(value["running"] for value in [*protected.values(), *before.values()]):
        raise RuntimeError("All managed and protected services must be running before restart test")
    print("Restarting only managed migration stacks; no reboot or T7 unmount.", flush=True)
    run("systemctl", "restart", "beelink-production.service")
    for name, previous in protected.items():
        if container_identity(name) != previous:
            raise RuntimeError("Protected existing service changed during restart: " + name)
    for name, previous in before.items():
        current = container_identity(name)
        if not current["running"] or current["started"] == previous["started"]:
            raise RuntimeError("Managed service did not restart successfully: " + name)
    if container_identity("beelink-java-web-apache-1")["running"]:
        raise RuntimeError("Retired copy Apache was reactivated")
    report = {"managed_restarted": sorted(before), "protected_unchanged": protected,
              "tls_checks": "Managed start completed full TLS/WebSocket/artifact checks",
              "reboot_tested": False, "disk_absent_tested": False}
    path = RUNTIME / "staging-restart-report.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(path)
    print("Managed cold restart verified; Home Assistant and existing camera DB unchanged.",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    for option in ("check", "start", "stop", "install-prepared",
                   "install-and-start", "install-and-restart-test"):
        group.add_argument("--" + option, action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.check:
            preflight()
        elif args.stop:
            stop()
        elif args.start:
            start()
        else:
            install(prepared=args.install_prepared)
    # Release the installer lock before systemd starts the controller.
    if args.install_and_start:
        run("systemctl", "start", "beelink-production.service")
    elif args.install_and_restart_test:
        # A separate lock serializes tests without blocking ExecStop/ExecStart.
        with Path("/run/lock/beelink-staging-restart-test.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            restart_test()


if __name__ == "__main__":
    main()
