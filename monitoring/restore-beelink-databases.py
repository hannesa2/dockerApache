#!/usr/bin/env python3
"""Restore the seed into isolated databases, never into running production."""

import argparse
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tarfile
import time


SEED = Path("/daten/latitude-migration")
DESTINATION = Path("/daten/beelink-staging")
T7_UUID = "c0c42461-ae4c-4edb-a993-bba94a05ed6c"
PROJECT = "beelink-staging"
DATABASES = {
    "mxserver": ("mxserver.dump", "mxserver-roles.sql", ["MXServer"]),
    "cameras": ("", "camera-roles.sql", ["camDoor", "camHinten", "camPi3"]),
    "owncloud": ("owncloud.dump", "owncloud-roles.sql", ["owncloud_stage"]),
}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def private_json(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def compose_config(root, pg_password, mysql_passwords, mysql_images):
    services = {}
    for service in DATABASES:
        services[service] = {
            "image": "camera-postgres-db:latest" if service != "owncloud" else "postgres:16-alpine",
            "restart": "no",
            "environment": {
                "POSTGRES_USER": "beelink_restore",
                "POSTGRES_PASSWORD": pg_password,
                "POSTGRES_DB": "postgres",
                "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256",
                "TZ": "Europe/Berlin",
            },
            "volumes": [
                {"type": "bind", "source": str(root / service / "data"),
                 "target": "/var/lib/postgresql/data",
                 "bind": {"create_host_path": False}},
            ],
            "networks": ["isolated"],
        }
    for service, image in mysql_images.items():
        services[service] = {
            "image": image,
            "restart": "no",
            "environment": {"MYSQL_ROOT_PASSWORD": mysql_passwords[service]},
            "command": ["--event-scheduler=OFF"],
            "volumes": [
                {"type": "bind", "source": str(root / service / "data"),
                 "target": "/var/lib/mysql", "bind": {"create_host_path": False}},
                {"type": "bind", "source": str(root / service / "client.cnf"),
                 "target": "/run/restore-client.cnf", "read_only": True,
                 "bind": {"create_host_path": False}},
            ],
            "networks": ["isolated"],
        }
    return {
        "name": PROJECT,
        "services": services,
        "networks": {"isolated": {"internal": True}},
    }


def verify_checksums(directory):
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        expected, filename = line.split(maxsplit=1)
        filename = filename.removeprefix("*").removeprefix("./")
        if "/" in filename or len(expected) != 64:
            raise RuntimeError("Unexpected database checksum entry")
        digest = hashlib.sha256()
        with (directory / filename).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise RuntimeError("Database checksum mismatch: " + filename)


def mysql_schemas(archive):
    names = []
    with gzip.open(archive, "rt") as stream:
        for line in stream:
            if line.startswith("-- Current Database: `") and line.rstrip().endswith("`"):
                names.append(line.rstrip()[len("-- Current Database: `"):-1])
    if "mysql" not in names or len(names) < 2:
        raise RuntimeError("Expected complete MySQL dump with system and application schemas")
    return sorted(names)


def mysql_tables(archive):
    tables = {}
    schema = None
    with gzip.open(archive, "rt") as stream:
        for line in stream:
            if line.startswith("-- Current Database: `") and line.rstrip().endswith("`"):
                schema = line.rstrip()[len("-- Current Database: `"):-1]
                tables[schema] = set()
            elif schema and line.startswith("CREATE TABLE "):
                match = re.match(r"CREATE TABLE (?:IF NOT EXISTS )?`((?:``|[^`])+)`", line)
                if not match:
                    raise RuntimeError("Unexpected MySQL table definition")
                tables[schema].add(match[1].replace("``", "`"))
    return tables


def mysql_version(archive):
    with gzip.open(archive, "rt") as stream:
        for line in stream:
            if line.startswith("-- Server version"):
                match = re.search(r"\b(\d+\.\d+\.\d+)\b", line)
                if match:
                    return match[1]
                break
    raise RuntimeError("The dump does not identify its source MySQL version")


def postgres_table_count(listing):
    return len(re.findall(r"^\d+; \d+ \d+ TABLE (?!DATA )", listing, re.MULTILINE))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore-isolated", action="store_true", required=True)
    parser.add_argument("--apache-mysql-image",
                        help="Loaded source image ID when docker save/load changes its manifest ID")
    args = parser.parse_args()
    os.umask(0o077)
    if socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run directly on beelink")
    os.listdir("/daten")  # Trigger the optional automount before checking its actual filesystem.
    if output("findmnt", "-rn", "-M", "/daten", "-t", "ext4", "-o", "UUID") != T7_UUID:
        raise RuntimeError("The expected encrypted T7 is not mounted")
    if (SEED / ".latitude-seed").read_text().strip() != "latitude-to-beelink-v1":
        raise RuntimeError("Unexpected seed marker")
    if not (SEED / "seed-complete").is_file():
        raise RuntimeError("Complete the initial seed first")
    if DESTINATION.is_symlink():
        raise RuntimeError("The staging root must not be a symlink")
    DESTINATION.mkdir(mode=0o700, exist_ok=True)
    if DESTINATION.stat().st_uid != os.getuid() or DESTINATION.stat().st_mode & 0o077:
        raise RuntimeError("The staging root must be private and owned by the invoking user")
    with (DESTINATION / "restore.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        restore(args.apache_mysql_image)


def restore(apache_mysql_image=None):
    config_path = DESTINATION / "compose.json"
    if config_path.exists() and json.loads(config_path.read_text()).get("name") != PROJECT:
        raise RuntimeError("Database project has changed; initial restore must not be rerun "
                           "against the production data directories")
    snapshots = SEED / "metadata/databases"
    verify_checksums(snapshots)
    state_path = DESTINATION / "database-state.json"
    if not config_path.exists():
        if state_path.exists() or any((DESTINATION / name).exists()
                                      for name in (*DATABASES, "apache-mysql", "native-mysql")):
            raise RuntimeError("Unmanaged database directories already exist; inspect before restoring")
        # Pin the source Docker MySQL image by its immutable ID. Load it from
        # Retrieve the original image only if the exact image is not already present.
        inventory = json.loads((SEED / "metadata/docker-inspect.json").read_text())
        apache = next(c for c in inventory if c["Name"] == "/apache-db-1")
        source_mysql = apache_mysql_image or apache["Image"]
        run("docker", "image", "inspect", source_mysql, stdout=subprocess.DEVNULL)
        for image in ("postgres:16-alpine", "mysql:8.0.46"):
            run("docker", "pull", image)
        mysql_images = {"apache-mysql": source_mysql, "native-mysql": "mysql:8.0.46"}
        for service, image in mysql_images.items():
            version = mysql_version(snapshots / (service + ".sql.gz"))
            binary_version = output("docker", "run", "--rm", "--network", "none",
                                    image, "mysqld", "--version")
            if not re.search(r"\b" + re.escape(version) + r"\b", binary_version):
                raise RuntimeError("MySQL image does not match the dump version: " + service)
        state = {
            "pg_password": secrets.token_hex(32),
            "mysql_passwords": {name: secrets.token_hex(32) for name in mysql_images},
            "completed": [],
        }
        config = compose_config(DESTINATION, state["pg_password"],
                                state["mysql_passwords"], mysql_images)
        for name in config["services"]:
            (DESTINATION / name / "data").mkdir(parents=True, mode=0o700)
        apache_env = dict(value.split("=", 1) for value in apache["Config"]["Env"])
        password = apache_env["MYSQL_ROOT_PASSWORD"]
        (DESTINATION / "apache-mysql/client.cnf").write_text(
            "[client]\nuser=root\npassword=" + json.dumps(password) + "\n")
        with tarfile.open(SEED / "metadata/host-configuration.tar.gz") as archive:
            stream = archive.extractfile("etc/mysql/debian.cnf")
            if stream is None:
                raise RuntimeError("Native MySQL client configuration is missing")
            (DESTINATION / "native-mysql/client.cnf").write_bytes(stream.read())
        private_json(state_path, state)
        private_json(config_path, config)
    else:
        state = json.loads(state_path.read_text())
        config = json.loads(config_path.read_text())
    compose = ["docker", "compose", "-p", PROJECT, "-f", str(config_path)]

    def dc(*args, **kwargs):
        return run(*compose, *args, **kwargs)

    def pg(service, sql, database="postgres"):
        return subprocess.check_output(
            [*compose, "exec", "-T", "--user", "postgres", service,
             "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "beelink_restore",
             "-d", database, "-Atc", sql], text=True).strip()

    report = {}
    for service in config["services"]:
        marker = DESTINATION / service / "restore-started"
        if marker.exists() and service not in state["completed"]:
            raise RuntimeError("An incomplete restore needs inspection before retrying: " + service)
        dc("up", "-d", "--no-deps", service)
        for _ in range(120):
            if service in DATABASES:
                probe = ["--user", "postgres", service, "pg_isready", "-U", "beelink_restore"]
            elif service in state["completed"]:
                probe = [service, "mysql", "--defaults-file=/run/restore-client.cnf",
                         "--protocol=socket", "-e", "SELECT 1"]
            else:
                probe = [service, "sh", "-c",
                         'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; '
                         'exec mysql --user=root --protocol=tcp -h 127.0.0.1 -e "SELECT 1"']
            result = subprocess.run([*compose, "exec", "-T", *probe],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                break
            time.sleep(2)
        else:
            raise RuntimeError("Database did not become ready: " + service)
        if service not in state["completed"]:
            marker.touch(exist_ok=False)
            if service in DATABASES:
                dump, roles, names = DATABASES[service]
                with (snapshots / roles).open("rb") as stream:
                    dc("exec", "-T", "--user", "postgres", service, "psql", "-X",
                       "-v", "ON_ERROR_STOP=1", "-U", "beelink_restore", "-d", "postgres",
                       stdin=stream, stdout=subprocess.DEVNULL)
                for name in names:
                    with (snapshots / (dump or name + ".dump")).open("rb") as stream:
                        dc("exec", "-T", "--user", "postgres", service, "pg_restore",
                           "--exit-on-error", "--create", "--no-tablespaces",
                           "-U", "beelink_restore", "-d", "postgres",
                           stdin=stream, stdout=subprocess.DEVNULL)
            else:
                archive = snapshots / (service + ".sql.gz")
                # The native dump stays on 8.0.46; never import its mysql system
                # schema into the separate 8.4 Apache database.
                with gzip.open(archive, "rb") as stream:
                    process = subprocess.Popen(
                        [*compose, "exec", "-T", service, "sh", "-c",
                         'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysql --user=root --binary-mode'],
                        stdin=subprocess.PIPE)
                    try:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            process.stdin.write(block)
                        process.stdin.write(b"\nFLUSH PRIVILEGES;\n")
                    finally:
                        process.stdin.close()
                        status = process.wait()
                    if status:
                        raise RuntimeError("MySQL restore failed: " + service)
            state["completed"].append(service)
            temporary = state_path.with_suffix(".tmp")
            with temporary.open("w") as stream:
                json.dump(state, stream)
            temporary.replace(state_path)
        if service in DATABASES:
            dump, _, names = DATABASES[service]
            report[service] = {}
            for name in names:
                with (snapshots / (dump or name + ".dump")).open("rb") as stream:
                    listing = subprocess.check_output(
                        [*compose, "exec", "-T", "--user", "postgres", service,
                         "pg_restore", "--list"], stdin=stream, text=True)
                expected = postgres_table_count(listing)
                actual = int(pg(
                    service, "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') "
                    "AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'", name))
                if actual != expected:
                    raise RuntimeError("Restored PostgreSQL table count differs: " + name)
                report[service][name] = actual
        else:
            archive = snapshots / (service + ".sql.gz")
            expected = mysql_tables(archive)
            actual = subprocess.check_output(
                [*compose, "exec", "-T", service, "mysql",
                 "--defaults-file=/run/restore-client.cnf", "--protocol=socket", "-NB",
                 "-e", "SELECT TABLE_SCHEMA,TABLE_NAME FROM information_schema.TABLES"], text=True)
            tables = {}
            for line in actual.splitlines():
                name, table = line.split("\t")
                tables.setdefault(name, set()).add(table)
            for name in mysql_schemas(archive):
                actual_tables = tables.get(name, set())
                # MySQL's dump omits engine-maintained system tables such as
                # innodb_index_stats; those are created by initialization.
                if (name == "mysql" and not expected[name] <= actual_tables or
                        name != "mysql" and expected[name] != actual_tables):
                    raise RuntimeError("Restored MySQL tables differ: " + service + "/" + name)
            report[service] = {name: len(tables.get(name, set())) for name in expected}
        print("Restored and checked isolated database:", service, flush=True)
    temporary = DESTINATION / "database-report.tmp"
    with temporary.open("w") as stream:
        json.dump(report, stream, indent=2)
    temporary.replace(DESTINATION / "database-report.json")
    print("Isolated database restore complete. No production service, host port or schedule was changed.")


if __name__ == "__main__":
    main()
