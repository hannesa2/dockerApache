#!/usr/bin/env python3
"""Restore final application snapshots into existing isolated beelink databases."""

import argparse
from datetime import datetime, timezone
import fcntl
import gzip
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


control = load("database_reconcile_control", "control-beelink-staging.py")
restore = load("database_reconcile_restore", "restore-beelink-databases.py")
wordpress = load("database_reconcile_wordpress", "start-beelink-wordpress.py")
matomo = load("database_reconcile_matomo", "start-beelink-matomo.py")
apps = load("database_reconcile_apps", "start-beelink-apps.py")
WORK = control.RUNTIME / "database-reconciliation"
PENDING = WORK / "pending.json"
REPORT = WORK / "report.json"
SCHEMAS = {
    "native-mysql": {
        "phpmyadmin", "piwik_db", "wordpress", "wordpress_arbeitsmedizin_praevention",
        "wordpress_homebike", "wordpress_mxtracks",
    },
    "apache-mysql": {"wordpress", "wordpress_mxtracks"},
}
SYSTEM_SCHEMAS = {"mysql", "sys", "information_schema", "performance_schema"}
APP_CONTAINERS = (
    "beelink-app-test-owncloud-app-1", "beelink-java-web-glassfish-1",
    "beelink-java-web-wildfly-1", "beelink-wordpress-test-apache-1",
    "beelink-matomo-test-matomo-1",
)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def application_sql(archive, allowed):
    """Keep mysqldump session setup/footer, excluding complete system sections."""
    include = True
    with gzip.open(archive, "rb") as stream:
        for line in stream:
            if line.startswith(b"-- Current Database: `"):
                match = re.fullmatch(rb"-- Current Database: `([^`]+)`\r?\n?", line)
                if match is None:
                    raise RuntimeError("Unexpected MySQL database section")
                schema = match[1].decode("ascii")
                if schema not in allowed | SYSTEM_SCHEMAS:
                    raise RuntimeError("Unexpected MySQL schema: " + schema)
                include = schema in allowed
            elif line.startswith(b"-- Dump completed on "):
                include = True
            if include:
                yield line


def mysql_command(service, binary="mysql"):
    return ["docker", "exec", "-i", "beelink-staging-" + service + "-1", binary,
            "--defaults-file=/run/restore-client.cnf", "--protocol=socket"]


def restore_mysql(service, archive):
    names = set(restore.mysql_schemas(archive)) - SYSTEM_SCHEMAS
    if names != SCHEMAS[service]:
        raise RuntimeError("Unexpected application schema inventory: " + service)
    expected_tables = restore.mysql_tables(archive)
    run(*mysql_command(service), input="".join(
        f"DROP DATABASE IF EXISTS `{name}`;\n" for name in sorted(names)), text=True)
    process = subprocess.Popen([*mysql_command(service), "--binary-mode"], stdin=subprocess.PIPE)
    try:
        for block in application_sql(archive, names):
            process.stdin.write(block)
    except BaseException:
        process.stdin.close()
        process.wait()
        raise
    process.stdin.close()
    if process.wait():
        raise RuntimeError("MySQL application restore failed: " + service)
    result = subprocess.check_output(
        [*mysql_command(service), "-NB", "-e",
         "SELECT TABLE_SCHEMA,TABLE_NAME FROM information_schema.TABLES"], text=True)
    actual = {}
    for line in result.splitlines():
        schema, table = line.split("\t")
        actual.setdefault(schema, set()).add(table)
    for name in names:
        if actual.get(name, set()) != expected_tables[name]:
            raise RuntimeError("Restored MySQL tables differ: " + service + "/" + name)
    return {name: len(expected_tables[name]) for name in sorted(names)}


def restore_postgres(service, database, archive):
    container = "beelink-staging-" + service + "-1"
    command = ["docker", "exec", "-i", "--user", "postgres", container]
    run(*command, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "beelink_restore",
        "-d", "postgres", "-c",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname='{database}' AND pid <> pg_backend_pid();",
        stdout=subprocess.DEVNULL)
    with archive.open("rb") as stream:
        run(*command, "pg_restore", "--exit-on-error", "--clean", "--if-exists",
            "--create", "--no-tablespaces", "-U", "beelink_restore", "-d", "postgres",
            stdin=stream, stdout=subprocess.DEVNULL)
    with archive.open("rb") as stream:
        listing = subprocess.check_output([*command, "pg_restore", "--list"],
                                          stdin=stream, text=True)
    expected = restore.postgres_table_count(listing)
    count = int(subprocess.check_output(
        [*command, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "beelink_restore",
         "-d", database, "-Atc",
         "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
         "WHERE c.relkind IN ('r','p') AND n.nspname !~ '^pg_' "
         "AND n.nspname <> 'information_schema'"], text=True))
    if count != expected:
        raise RuntimeError("Restored PostgreSQL table count differs: " + database)
    return count


def reconcile(snapshots):
    if PENDING.exists() or REPORT.exists():
        raise RuntimeError("A reconciliation already started; inspect its report/pending marker")
    if snapshots.is_symlink() or not snapshots.resolve().is_relative_to(
            "/daten/latitude-migration/cutover-20261117"):
        raise RuntimeError("Expected the private final snapshot directory")
    if not (snapshots.parent / "collection-complete").is_file():
        raise RuntimeError("Snapshot collection is incomplete")
    entries = (snapshots / "SHA256SUMS").read_text().splitlines()
    filenames = [line.split(maxsplit=1)[1].removeprefix("./") for line in entries]
    if set(filenames) != {"mxserver.dump", "owncloud.dump",
                           "native-mysql.sql.gz", "apache-mysql.sql.gz"} or len(entries) != 4:
        raise RuntimeError("Expected exactly four final database snapshots")
    restore.verify_checksums(snapshots)
    for service in SCHEMAS:
        archive = snapshots / (service + ".sql.gz")
        if set(restore.mysql_schemas(archive)) - SYSTEM_SCHEMAS != SCHEMAS[service]:
            raise RuntimeError("Unexpected source MySQL schemas: " + service)
    if wordpress.core_version(wordpress.WORK / "www/mxdocs/wordpress") != wordpress.WWW_VERSION:
        raise RuntimeError("Preserve the upgraded WordPress core before restoring its database")
    protected = {name: control.container_identity(name)
                 for name in ("homeassistant", "camera-postgres-db-1",
                              "beelink-app-test-gitlab-1")}
    if not all(item["running"] for item in protected.values()):
        raise RuntimeError("Protected services must be running")
    record = {"snapshots": str(snapshots), "checksums": entries,
              "started": datetime.now(timezone.utc).isoformat()}
    PENDING.write_text(json.dumps(record, indent=2) + "\n")
    succeeded = False
    try:
        run("docker", "stop", "--time", "60", *APP_CONTAINERS)
        for service in SCHEMAS:
            record[service] = restore_mysql(service, snapshots / (service + ".sql.gz"))
        for service, database in (("mxserver", "MXServer"), ("owncloud", "owncloud_stage")):
            record[service] = restore_postgres(service, database, snapshots / (service + ".dump"))
        # Schema-scoped staging grants are retained because mysql.* was not imported.
        wordpress.verify(upgrade_www=True)
        matomo.verify()
        apps.start_owncloud()
        run("docker", "start", "beelink-java-web-glassfish-1",
            "beelink-java-web-wildfly-1")
        control.wait_applications()
        control.preflight()
        for name, previous in protected.items():
            if control.container_identity(name) != previous:
                raise RuntimeError("Protected container changed: " + name)
        record.update({"completed": datetime.now(timezone.utc).isoformat(),
                       "protected_unchanged": protected, "isolated": True,
                       "files_reconciled": False, "camera_databases_reconciled": False})
        REPORT.write_text(json.dumps(record, indent=2) + "\n")
        PENDING.unlink()
        succeeded = True
        print("Final application databases restored and checked. "
              "Files/cameras still pending; no GitLab, routing or renewal changes.", flush=True)
    finally:
        if not succeeded:
            print("ERROR: Database reconciliation incomplete; stopping affected apps. "
                  "Inspect pending marker and journal before retrying.", flush=True)
            run("docker", "stop", "--time", "60", *APP_CONTAINERS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        control.preflight()
        if control.capture("systemctl", "is-active", "beelink-staging.service") != "active":
            raise RuntimeError("Managed isolated staging must be active")
        WORK.mkdir(mode=0o700, exist_ok=True)
        reconcile(args.snapshots)


if __name__ == "__main__":
    main()
