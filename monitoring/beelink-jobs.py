#!/usr/bin/env python3
"""Run mount-guarded beelink backups, mirror jobs and availability checks."""

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parent
RUNTIME = Path("/daten/beelink-runtime")
STATE = Path("/var/lib/beelink-jobs")
INSTALLED = Path("/usr/local/libexec/beelink-jobs")
JOBS = {
    "postgresql": ("19:15:00", "PostgreSQL backups"),
    "apache": ("03:05:00", "Apache mirror sync"),
    "gitlab": ("03:15:00", "GitLab standby backup"),
    "owncloud": ("04:00:00", "ownCloud mirror sync"),
    "mxserver": ("04:30:00", "MXServer mirror sync"),
    "matomo": ("04:45:00", "Matomo mirror sync"),
    "monitor": ("*:00:00", "Systems health monitor"),
}
SSH_OPTIONS = [
    "-i", "/home/hannes/.ssh/id_rsa", "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
    "-o", "HostKeyAlias=mac2016", "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
    "-o", "UserKnownHostsFile=/home/hannes/.ssh/known_hosts",
]
MIRROR = "hannes@192.168.178.70"
PG = ("beelink-databases-mxserver-1", "beelink-databases-owncloud-1",
      "camera-postgres-db-1")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def remote(script, **kwargs):
    return run("ssh", *SSH_OPTIONS, MIRROR, "/bin/bash -euo pipefail -c " + shlex.quote(
        "export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin; " + script), **kwargs)


def guard():
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    os.listdir("/daten")
    if capture("findmnt", "-rn", "-M", "/daten", "-t", "ext4", "-o", "UUID") != \
            "c0c42461-ae4c-4edb-a993-bba94a05ed6c":
        raise RuntimeError("Expected encrypted T7 is not mounted")
    if RUNTIME.is_symlink() or RUNTIME.stat().st_uid != 0 or RUNTIME.stat().st_mode & 0o077:
        raise RuntimeError("Expected private root-owned runtime")
    run("systemctl", "is-active", "--quiet", "beelink-production.service")


def environment():
    return {**os.environ, "BEELINK_PRODUCTION": "1", "HOME": "/root",
            "LC_ALL": "C", "LANG": "C",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "POSTGRES_BACKUP_ROOT": "/daten/beelink-backups/postgresql",
            "POSTGRES_OWNCLOUD_CONTAINER": PG[1], "POSTGRES_OWNCLOUD_USER": "beelink_restore",
            "MATOMO_SOURCE_DIR": str(RUNTIME / "matomo/www/piwik")}


def local_health():
    failures = []
    if not os.access(STATE, os.R_OK | os.X_OK):
        failures.append("Job status directory is missing or unreadable: " + str(STATE))
    try:
        if capture("findmnt", "-rn", "-M", "/daten", "-t", "ext4", "-o", "UUID") != \
                "c0c42461-ae4c-4edb-a993-bba94a05ed6c":
            failures.append("Expected encrypted T7 is not mounted")
    except Exception as error:
        failures.append(str(error))
    for container in (
            *PG, "beelink-databases-native-mysql-1", "beelink-databases-apache-mysql-1",
            "beelink-apps-owncloud-app-1", "beelink-apps-redis-1",
            "beelink-apps-gitlab-1", "beelink-wordpress-apache-1",
            "beelink-java-web-glassfish-1", "beelink-java-web-wildfly-1",
            "beelink-matomo-matomo-1", "beelink-artifacts-artifacts-1",
            "beelink-dns-dns-1", "beelink-frontend-frontend-1", "homeassistant"):
        try:
            status = json.loads(capture("docker", "inspect", container))[0]["State"]
            if not status["Running"] or status.get("Health", {}).get("Status", "healthy") != "healthy":
                failures.append("Unhealthy container: " + container)
        except subprocess.CalledProcessError:
            failures.append("Missing container: " + container)
    if os.statvfs("/daten").f_bavail * os.statvfs("/daten").f_frsize < 50 * 1024**3:
        failures.append("Less than 50 GiB free on /daten")
    for domain, path, allowed in (
            ("www.mxtracks.info", "/wordpress/", {"200"}),
            ("dev.mxtracks.info", "/wordpress/", {"200"}),
            ("www.mxtracks.info", "/owncloud/status.php", {"200"}),
            ("www.mxtracks.info", "/backend/MXServer/rest/version", {"401"}),
            ("git.mxtracks.info", "/users/sign_in", {"200"}),
            ("homeassistant.mxtracks.info", "/", {"200"})):
        try:
            code = capture("curl", "--noproxy", "*", "--silent", "--show-error",
                           "--max-time", "35", "--resolve", f"{domain}:443:127.0.0.1",
                           "--output", "/dev/null", "--write-out", "%{http_code}",
                           f"https://{domain}{path}")
            if code not in allowed:
                failures.append(f"{domain}{path}: HTTP {code}")
        except subprocess.CalledProcessError:
            failures.append("TLS/HTTP failed: " + domain + path)
    now = dt.datetime.now().astimezone()
    for name in JOBS:
        if name == "monitor":
            continue
        path = STATE / (name + ".json")
        if not path.exists():
            installed = STATE / "installed-at"
            if installed.exists() and now.timestamp() - float(installed.read_text()) > 30 * 3600:
                failures.append("No completed run recorded: " + name)
            continue
        report = json.loads(path.read_text())
        if report["result"] != "SUCCESS":
            failures.append(name + ": " + report["result"])
        elif now.timestamp() - dt.datetime.fromisoformat(report["finished"]).timestamp() > 30 * 3600:
            failures.append("Last successful run over 30 hours ago: " + name)
    for unit in ("beelink-production.service", "beelink-certificates.timer", "beelink-public-web-80.socket",
                 "beelink-public-web-443.socket"):
        if subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode:
            failures.append("Inactive unit: " + unit)
    return failures


def preflight():
    guard()
    for command in ("rsync", "ssh", "docker", "mail"):
        run("/usr/bin/which", command)
    for container, database in ((PG[0], "MXServer"), (PG[1], "owncloud_stage")):
        run("docker", "exec", container, "pg_isready", "-U", "beelink_restore", "-d", database)
    for name in ("camDoor", "camHinten", "camPi3"):
        run("docker", "exec", PG[2], "pg_isready", "-U", "postgres", "-d", name)
    for path in (
            "rootfs/daten/owncloud/data/.ocdata", "apps/owncloud/volume/config/config.php",
            "java-web/glassfish/glassfish/domains/domain1/config/keyfile",
            "matomo/www/piwik/config/config.ini.php",
            "wordpress/www/dev.mxtracks/wordpress/wp-config.php",
            "wordpress/www/mxdocs/wordpress/wp-config.php"):
        if not (RUNTIME / path).is_file():
            raise RuntimeError("Missing production input: " + path)
    remote("""
test "$(hostname -s)" = mac2016
/sbin/mount | awk '$3 == "/Volumes/SamsungT5_1T" { ok=1 } END { exit !ok }'
test -f /Volumes/SamsungT5_1T/ownCloudBackup/owncloud/.owncloud-mirror-managed
test -f /Volumes/SamsungT5_1T/MXServerMirror/.mxserver-mirror-managed
test -x /usr/local/bin/rsync
for c in apache-apache-1 apache-db-1 apache-owncloud-db-1 apache-mxserver-db-1 gitlab16; do
    test "$(docker inspect -f '{{.State.Running}}' "$c")" = true
done
""")
    source_version = capture("docker", "exec", "beelink-apps-gitlab-1", "cat",
                             "/opt/gitlab/embedded/service/gitlab-rails/VERSION")
    mirror_version = remote(
        "docker exec gitlab16 cat /opt/gitlab/embedded/service/gitlab-rails/VERSION",
        stdout=subprocess.PIPE, text=True).stdout.strip()
    if source_version != mirror_version:
        raise RuntimeError("GitLab source/mirror versions differ")
    key = Path("/home/hannes/.ssh/gitlab-mac-backup")
    if not key.is_file():
        raise RuntimeError("Restricted GitLab backup key missing")
    print("Source data, DB readiness, mirror storage, SSH and GitLab versions checked.", flush=True)


def gitlab():
    container = "beelink-apps-gitlab-1"
    pending = STATE / "gitlab-pending-id"
    version = capture("docker", "exec", container, "cat",
                      "/opt/gitlab/embedded/service/gitlab-rails/VERSION")
    import re
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError("Unexpected GitLab version")
    backup_id = pending.read_text().strip() if pending.exists() else (
        "git16dr_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + version)
    if not re.fullmatch(r"git16dr_\d{8}T\d{6}Z_" + re.escape(version), backup_id):
        raise RuntimeError("Invalid pending GitLab backup ID")
    pending.write_text(backup_id)
    filename = backup_id + "_gitlab_backup.tar"
    exists = subprocess.run(["docker", "exec", container, "test", "-s",
                             "/var/opt/gitlab/backups/" + filename]).returncode == 0
    if not exists:
        run("docker", "exec", container, "gitlab-backup", "create", "BACKUP=" + backup_id)
    run("docker", "exec", container, "tar", "-tf", "/var/opt/gitlab/backups/" + filename,
        stdout=subprocess.DEVNULL)
    command = ["ssh", *SSH_OPTIONS, MIRROR, "backup " + backup_id]
    command[command.index("/home/hannes/.ssh/id_rsa")] = "/home/hannes/.ssh/gitlab-mac-backup"
    sender = subprocess.Popen(
        ["docker", "exec", container, "tar", "-cf", "-", "-C", "/var/opt/gitlab/backups",
         filename, "-C", "/etc/gitlab", "gitlab.rb", "gitlab-secrets.json"],
        stdout=subprocess.PIPE)
    try:
        run(*command, stdin=sender.stdout)
    finally:
        sender.stdout.close()
        if sender.wait() != 0:
            raise RuntimeError("GitLab snapshot stream failed; pending backup retained")
    run("docker", "exec", container, "rm", "-f", "/var/opt/gitlab/backups/" + filename)
    pending.unlink()
    print("GitLab standby backup completed: " + backup_id, flush=True)


def wordpress():
    for subdir, schema, domain in (
            ("dev.mxtracks", "wordpress", "dev16.mxtracks.info"),
            ("mxdocs", "wordpress_mxtracks", "www16.mxtracks.info")):
        source = RUNTIME / "wordpress/www" / subdir
        target = "/Users/hannes/server/apache/www/" + subdir
        # Preserve the mirror-only database credentials and HTTPS configuration.
        remote(f"test -f {target}/wordpress/wp-config.php")
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = f"/Users/hannes/.local/state/beelink-wordpress/{stamp}-{schema}"
        remote(f"umask 077; mkdir -p {backup}; "
               "docker exec apache-db-1 sh -c " + shlex.quote(
                   'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -uroot '
                   f'--single-transaction --no-tablespaces {schema}') +
               f" > {backup}/database.sql; "
               f"cp {target}/wordpress/wp-config.php {backup}/wp-config.php")
        with tempfile.TemporaryFile() as dump:
            run("docker", "exec", "beelink-databases-native-mysql-1", "mysqldump",
                "--defaults-extra-file=/run/restore-client.cnf",
                "--single-transaction", "--no-tablespaces", "--hex-blob", schema, stdout=dump)
            remote(f"printf '%s\\n' '<?php $upgrading = time();' > {target}/wordpress/.maintenance")
            run("rsync", "-rltp", "--delete", "--partial", "--rsync-path=/usr/local/bin/rsync",
                "--exclude=wp-config.php", "--exclude=/piwik/", "--exclude=.maintenance",
                "--exclude=.git/", "--exclude=*/cache/", "--exclude=*/tmp/",
                "-e", shlex.join(["ssh", *SSH_OPTIONS]),
                str(source) + "/", f"{MIRROR}:{target}/")
            dump.seek(0)
            remote("docker exec -i apache-db-1 sh -c " + shlex.quote(
                'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -e '
                f'"DROP DATABASE {schema}; CREATE DATABASE {schema} '
                'CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" && '
                f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot {schema}'), stdin=dump)
        original_domain = "dev.mxtracks.info" if schema == "wordpress" else "www.mxtracks.info"
        remote("docker exec apache-apache-1 wp --allow-root --skip-plugins --skip-themes "
               f"--path=/var/www/html/{subdir}/wordpress search-replace "
               f"https://{original_domain} https://{domain}:8443 "
               "--all-tables --skip-columns=guid; "
               "docker exec apache-apache-1 wp --allow-root --skip-plugins --skip-themes "
               f"--path=/var/www/html/{subdir}/wordpress search-replace "
               f"http://{original_domain} https://{domain}:8443 "
               "--all-tables --skip-columns=guid")
        remote("docker exec apache-apache-1 wp --allow-root --skip-plugins --skip-themes "
               f"--path=/var/www/html/{subdir}/wordpress option update home "
               f"https://{domain}:8443/wordpress; "
               "docker exec apache-apache-1 wp --allow-root --skip-plugins --skip-themes "
               f"--path=/var/www/html/{subdir}/wordpress option update siteurl "
               f"https://{domain}:8443/wordpress; "
               "docker exec apache-apache-1 wp --allow-root --skip-plugins --skip-themes "
               f"--path=/var/www/html/{subdir}/wordpress core update-db; "
               f"rm {target}/wordpress/.maintenance; "
               f"curl --noproxy '*' -fsS --max-time 40 --resolve {domain}:8443:127.0.0.1 "
               f"https://{domain}:8443/wordpress/ >/dev/null")
        print("Verified WordPress mirror: " + domain, flush=True)


def execute(job):
    if job == "postgresql":
        run("bash", str(ROOT / "postgresql-backup.sh"), env=environment())
    elif job == "owncloud":
        run("bash", str(ROOT / "owncloud/sync-latitude.sh"), env=environment())
    elif job == "mxserver":
        run("bash", str(ROOT / "mxserver/sync-latitude.sh"), env=environment())
    elif job == "matomo":
        run("python3", str(ROOT / "sync-matomo.py"), "--apply", env=environment())
    elif job == "gitlab":
        gitlab()
    elif job == "apache":
        wordpress()
    elif job == "monitor":
        failures = local_health()
        try:
            remote("docker info >/dev/null; "
                   "for c in apache-apache-1 apache-db-1 apache-owncloud-1 "
                   "apache-owncloud-db-1 apache-mxserver-1 apache-mxserver-db-1 gitlab16; do "
                   'test "$(docker inspect -f \'{{.State.Running}}\' "$c")" = true; done')
        except subprocess.CalledProcessError:
            failures.append("mac2016 SSH/Docker/standby application unavailable")
        if failures:
            raise RuntimeError("\n".join(failures))
        print("beelink and mac2016 services, TLS and job freshness checked.", flush=True)


def notified_run(job):
    STATE.mkdir(mode=0o755, parents=True, exist_ok=True)
    STATE.chmod(0o755)
    started = dt.datetime.now().astimezone().isoformat()
    status_path = STATE / (job + ".json")
    previous = json.loads(status_path.read_text()) if status_path.exists() else {}
    success, error = False, None
    with tempfile.TemporaryFile(mode="w+") as output:
        try:
            with (STATE / (job + ".lock")).open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                command = ["python3", str(Path(__file__).resolve()), "--execute", job]
                result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
                success = result.returncode == 0
                if not success:
                    error = "Job exited with status " + str(result.returncode)
        except Exception as failure:
            error = str(failure)
        finished = dt.datetime.now().astimezone().isoformat()
        report = dict(job=job, result="SUCCESS" if success else "FAILED",
                      started=started, finished=finished, error=error)
        output.seek(0)
        text = output.read()
        print(text, end="", flush=True)
        temporary = status_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.chmod(0o644)
        temporary.replace(status_path)
        # Monitoring retains change-only alerts; scheduled backup/sync jobs mail every run.
        if job != "monitor" or previous.get("result") != report["result"]:
            run("/usr/bin/mail", "-s", f"[beelink] {report['result']}: {JOBS[job][1]}",
                "hannes.software@gmx.at",
                input=json.dumps(report, indent=2) + "\n\n" + text[-20000:], text=True)
        if not success:
            raise RuntimeError(error)


def units():
    result = {}
    for job, (schedule, title) in JOBS.items():
        name = "beelink-job-" + job
        result[name + ".service"] = f"""[Unit]
Description={title} from beelink
After=network-online.target docker.service

[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/python3 {INSTALLED}/beelink-jobs.py --run {job}
TimeoutStartSec=8h
"""
        result[name + ".timer"] = f"""[Unit]
Description=Schedule {title} from beelink

[Timer]
OnCalendar=*-*-* {schedule}
Persistent=false
Unit={name}.service

[Install]
WantedBy=timers.target
"""
    return result


def install():
    preflight()
    definitions = units()
    for name, text in definitions.items():
        path = Path("/etc/systemd/system") / name
        if path.is_symlink() or path.exists() and path.read_text() != text:
            raise RuntimeError("Existing job unit differs: " + name)
    INSTALLED.mkdir(mode=0o700, parents=True, exist_ok=True)
    import shutil
    for source, target in (
            (ROOT / "beelink-jobs.py", INSTALLED / "beelink-jobs.py"),
            (ROOT / "postgresql-backup.sh", INSTALLED / "postgresql-backup.sh"),
            (ROOT / "sync-matomo.py", INSTALLED / "sync-matomo.py"),
            (ROOT.parent / "owncloud/sync-latitude.sh", INSTALLED / "owncloud/sync-latitude.sh"),
            (ROOT.parent / "mxserver/sync-latitude.sh", INSTALLED / "mxserver/sync-latitude.sh")):
        target.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o700)
    status_helper = Path("/usr/local/bin/beelink-job-status")
    shutil.copyfile(ROOT / "beelink-jobs.py", status_helper)
    status_helper.chmod(0o755)
    STATE.mkdir(mode=0o755, parents=True, exist_ok=True)
    STATE.chmod(0o755)
    if not (STATE / "installed-at").exists():
        (STATE / "installed-at").write_text(str(dt.datetime.now().timestamp()))
        (STATE / "installed-at").chmod(0o644)
    for name, text in definitions.items():
        path = Path("/etc/systemd/system") / name
        path.write_text(text)
        path.chmod(0o644)
    run("systemd-analyze", "verify", *[
        "/etc/systemd/system/" + name for name in definitions])
    run("systemctl", "daemon-reload")
    # First perform a real database-only backup and availability check.
    # No mirror snapshots/restores are repeated during installation.
    run("systemctl", "start", "beelink-job-postgresql.service", "beelink-job-monitor.service")
    remote("""
test -f /Users/hannes/git/dockerApache/monitoring/systems-health-check-mac.sh
bash -n /Users/hannes/git/dockerApache/monitoring/systems-health-check-mac.sh
mkdir -p /Users/hannes/.local/state/beelink-jobs
if test ! -f /Users/hannes/.local/state/beelink-jobs/mac-monitor-before-cutover.sh; then
    cp /Users/hannes/bin/systems-health-check-mac.sh \
        /Users/hannes/.local/state/beelink-jobs/mac-monitor-before-cutover.sh
fi
install -m 0755 /Users/hannes/git/dockerApache/monitoring/systems-health-check-mac.sh \
    /Users/hannes/bin/systems-health-check-mac.sh
HOME=/Users/hannes PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
    /Users/hannes/bin/systems-health-check-mac.sh
""")
    run("systemctl", "enable", "--now", *[
        name for name in definitions if name.endswith(".timer")])
    run("systemctl", "list-timers", "beelink-job-*", "--no-pager")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true")
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--run", choices=JOBS)
    action.add_argument("--execute", choices=JOBS)
    action.add_argument("--health-json", action="store_true")
    action.add_argument("--report", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.report:
        if not os.access(STATE, os.R_OK | os.X_OK):
            raise RuntimeError("Job status directory is missing or unreadable: " + str(STATE))
        for job, (schedule, title) in JOBS.items():
            path = STATE / (job + ".json")
            report = json.loads(path.read_text()) if path.exists() else {}
            print(f"{title} ({schedule}): {report.get('result', 'PENDING FIRST RUN')} "
                  f"{report.get('finished', '')}")
        return
    if args.health_json:
        failures = local_health()
        print(json.dumps({"host": socket.gethostname(), "failures": failures}))
        return
    if args.run:
        # Notification wrapper also runs when the mounted production disk is missing.
        if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
            raise RuntimeError("Run as root directly on beelink")
        notified_run(args.run)
        return
    guard()
    if args.install:
        install()
    elif args.preflight:
        preflight()
    else:
        with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            guard()
            execute(args.execute)


if __name__ == "__main__":
    main()
