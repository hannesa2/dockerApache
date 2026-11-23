#!/usr/bin/env python3
"""Back up and update beelink GitLab, its Docker runner and mac2016 standby."""

import argparse
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time


RUNTIME = Path("/daten/beelink-runtime")
APPS = RUNTIME / "apps/compose.json"
RUNNER = Path("/etc/beelink-docker-runner/compose.json")
BACKUPS = Path("/daten/beelink-backups/gitlab-updates")
PENDING = BACKUPS / "update-in-progress.json"
CONTAINER = "beelink-apps-gitlab-1"
MIRROR_COMPOSE = Path("/Users/hannes/git/dockerApache/gitlab/docker-compose.yml")
MIRROR_LOCK = Path("/Users/hannes/.local/state/gitlab-mac-sync/lock")
MIRROR_BACKUP_ROOT = "/gitlab-backup-archives/.sync"
VERSION_FILE = "/opt/gitlab/embedded/service/gitlab-rails/VERSION"
LOG = None


def log(message):
    line = datetime.now(timezone.utc).isoformat() + " " + message
    print(line, flush=True)
    if LOG:
        LOG.write(line + "\n")
        LOG.flush()


def command(*args, timeout=3600, input=None, stdout=None):
    log("+ " + shlex.join(str(arg) for arg in args))
    try:
        result = subprocess.run(
            [str(arg) for arg in args], input=input, text=True, timeout=timeout,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired as error:
        for output in (error.stdout, error.stderr):
            if output:
                log(output.decode(errors="replace") if isinstance(output, bytes) else output)
        log("ERROR: Timed out. A docker exec or SSH operation may still be running "
            "on the server; inspect it before recovery.")
        raise
    if result.stderr:
        log(result.stderr.rstrip())
    if result.returncode:
        if result.stdout:
            log(result.stdout.rstrip())
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]}")
    return result.stdout.strip() if result.stdout else ""


def version(value):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError("Expected an exact version such as 19.5.4, not latest: " + value)
    return tuple(int(part) for part in value.split("."))


def validate_path(current, targets, reviewed):
    previous = version(current)
    for target in targets:
        following = version(target)
        if following <= previous:
            raise RuntimeError("Only strictly increasing versions are allowed; no downgrades")
        if following[:2] != previous[:2] and not reviewed:
            raise RuntimeError("Minor/major upgrades require --upgrade-path-reviewed; "
                               "check https://docs.gitlab.com/update/upgrade_paths/")
        previous = following


def validate_runners(current, targets, runners):
    if len(targets) != len(runners):
        raise RuntimeError("Supply one runner version per GitLab upgrade stop")
    previous = version(current)
    for target, runner in zip(targets, runners):
        following = version(runner)
        if following[:2] != version(target)[:2] or following < previous:
            raise RuntimeError("Runner versions must not decrease and must match "
                               "the GitLab major/minor version at each stop")
        previous = following


def image(target, runner=False):
    version(target)
    return ("gitlab/gitlab-runner:v" + target if runner
            else "gitlab/gitlab-ce:" + target + "-ce.0")


def write_json(path, value):
    if path.is_symlink() or path.with_name(path.name + ".update-tmp").exists():
        raise RuntimeError("Unsafe configuration or existing temporary file: " + str(path))
    temporary = path.with_name(path.name + ".update-tmp")
    with temporary.open("x") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def dc(path, *args):
    return command("docker", "compose", "-f", path, *args)


def mirror_dc(files, *args):
    options = [item for path in files for item in ("-f", path)]
    return command("docker", "compose", *options, *args)


def execute(container, *args, timeout=3600, stdout=None):
    output = command("docker", "exec", container, *args, timeout=timeout, stdout=stdout)
    if stdout is None and output:
        log(output)
    return output


def installed(container):
    result = execute(container, "cat", VERSION_FILE)
    version(result)
    return result


def check_gitlab(container, expected, wait=False):
    deadline = time.monotonic() + 1200
    while True:
        probe = subprocess.run(
            ["docker", "exec", container, "curl", "--fail", "--silent",
             "--show-error", "--max-time", "10", "http://127.0.0.1/-/readiness"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if probe.returncode == 0:
            break
        if not wait or time.monotonic() >= deadline:
            raise RuntimeError("GitLab readiness failed: " + container + "\n" + probe.stderr)
        time.sleep(10)
    if installed(container) != expected:
        raise RuntimeError("Running GitLab version differs from expected " + expected)
    execute(container, "gitlab-rake", "db:abort_if_pending_migrations")
    execute(container, "gitlab-rake", "gitlab:background_migrations:wait")
    execute(container, "gitlab-rake", "gitlab:check", "SANITIZE=true")
    execute(container, "gitlab-rake", "gitlab:doctor:secrets")


def wait_runner(expected, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        if command("systemctl", "is-active", "beelink-docker-runner.service") != "active":
            raise RuntimeError("Runner service stopped while waiting for its container")
        container = dc(RUNNER, "ps", "--status", "running", "-q", "runner")
        if container:
            actual = command("docker", "inspect", "--format", "{{.Config.Image}}", container)
            if actual == image(expected, runner=True):
                return
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out waiting for runner container " + image(expected, runner=True))
        log("Waiting for runner container " + image(expected, runner=True))
        time.sleep(2)


def backup(container, directory, backup_root="/var/opt/gitlab/backups"):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    backup_id = "preupdate_" + directory.parent.name + "_" + installed(container)
    execute(container, "gitlab-rails", "runner",
            "abort('Unexpected backup path') unless "
            f"Gitlab.config.backup.path.to_s.delete_suffix('/') == '{backup_root}'")
    archive = backup_root + "/" + backup_id + "_gitlab_backup.tar"
    execute(container, "test", "!", "-e", archive)
    execute(container, "gitlab-backup", "create", "BACKUP=" + backup_id)
    execute(container, "gitlab-rake", "gitlab:backup:verify", "BACKUP=" + backup_id)
    for name, args in (
            ("gitlab-backup.tar", ("cat", archive)),
            ("gitlab-configuration.tar", ("tar", "-cf", "-", "-C", "/etc", "gitlab"))):
        with (directory / name).open("xb") as stream:
            execute(container, *args, stdout=stream)
            stream.flush()
            os.fsync(stream.fileno())
    (directory / "backup-id").write_text(backup_id + "\n")
    execute(container, "rm", "--", archive)
    log("Backup retained at " + str(directory))


def load_jobs():
    spec = importlib.util.spec_from_file_location(
        "beelink_update_jobs", Path(__file__).with_name("beelink-jobs.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def remote(jobs, action, target, run_id):
    # Send the worker source over the existing authenticated SSH connection.
    # No installation, sudo or new credentials are needed on the standby.
    output = command("ssh", *jobs.SSH_OPTIONS, jobs.MIRROR,
                     "export PATH=/Applications/OrbStack.app/Contents/MacOS/xbin:"
                     "/usr/local/bin:/usr/bin:/bin; python3 - --mirror-worker " +
                     shlex.join([action, target, run_id]),
                     input=Path(__file__).read_text(), timeout=14400)
    if output:
        log(output)


def mirror_files(details):
    override = MIRROR_COMPOSE.with_name("docker-compose.override.yml")
    labels = details["Config"].get("Labels") or {}
    configured = labels.get("com.docker.compose.project.config_files")
    allowed = ([MIRROR_COMPOSE], [MIRROR_COMPOSE, override])
    for files in allowed:
        if configured == ",".join(str(path) for path in files):
            for path in files:
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError("Standby Compose file missing or unsafe: " + str(path))
            return files
    raise RuntimeError("Unexpected standby Compose files")


def mirror_config(files):
    config = json.loads(mirror_dc(files, "config", "--format", "json"))
    omnibus = config["services"]["gitlab"]["environment"]["GITLAB_OMNIBUS_CONFIG"]
    paths = re.findall(
        r"(?m)^\s*gitlab_rails\['backup_path'\]\s*=\s*['\"]([^'\"]+)['\"]\s*$",
        omnibus)
    if paths == [MIRROR_BACKUP_ROOT]:
        return config, False
    old = "gitlab_rails['backup_path'] = '/gitlab-backup-archives'"
    if (paths == ["/gitlab-backup-archives"] and len(files) == 2
            and files[1].read_text().count(old) == 1):
        return config, True
    raise RuntimeError("Unsupported standby GitLab backup path or override format")


def normalize_mirror_override(path):
    old = "gitlab_rails['backup_path'] = '/gitlab-backup-archives'"
    new = "gitlab_rails['backup_path'] = '/gitlab-backup-archives/.sync'"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Standby override is missing or unsafe")
    text = path.read_text()
    if text.count(old) != 1:
        raise RuntimeError("Standby override changed; refusing to alter backup path")
    temporary = path.with_name(path.name + ".update-tmp")
    with temporary.open("x") as stream:
        os.chmod(temporary, path.stat().st_mode & 0o777)
        stream.write(text.replace(old, new))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def mirror_check():
    if socket.gethostname().split(".")[0].lower() != "mac2016":
        raise RuntimeError("Standby worker must run on mac2016")
    details = json.loads(command("docker", "inspect", "gitlab16"))[0]
    files = mirror_files(details)
    config, _ = mirror_config(files)
    current = installed("gitlab16")
    if (config["services"]["gitlab"]["image"] != image(current)
            or details["Config"]["Image"] != image(current)
            or not details["State"]["Running"]):
        raise RuntimeError("Standby must be running the same pinned CE version as its Compose file")
    mounts = {mount["Destination"]: mount["Source"] for mount in details["Mounts"]
              if mount["Type"] == "bind"}
    root = Path("/Users/hannes/server/gitlab")
    if any(mounts.get(destination) != str(source) for destination, source in (
            ("/etc/gitlab", root / "config"), ("/var/opt/gitlab", root / "data"),
            ("/gitlab-backup-archives", root / "backups"))):
        raise RuntimeError("Unexpected standby bind mounts")
    if shutil.disk_usage(root).free < 50 * 1024**3:
        raise RuntimeError("Standby requires at least 50 GiB free")
    execute("gitlab16", "gitlab-rails", "runner",
            "abort('Unexpected standby backup path') unless "
            f"Gitlab.config.backup.path.to_s.delete_suffix('/') == '{MIRROR_BACKUP_ROOT}'")
    return current


def mirror_worker(action, target, run_id):
    version(target)
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", run_id):
        raise RuntimeError("Invalid update identifier")
    owner = MIRROR_LOCK / "updater"
    if action == "acquire":
        MIRROR_LOCK.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        MIRROR_LOCK.mkdir(mode=0o700)
        owner.write_text(run_id)
        if (MIRROR_LOCK.parent / "restore-in-progress").exists():
            raise RuntimeError("Standby restore is incomplete; update lock retained")
        return
    if action == "plan":
        if MIRROR_LOCK.exists() or (MIRROR_LOCK.parent / "restore-in-progress").exists():
            raise RuntimeError("Standby restore/update is running or needs recovery")
        if mirror_check() != target:
            raise RuntimeError("Production and standby versions differ")
        check_gitlab("gitlab16", target)
        return
    if not owner.is_file() or owner.read_text() != run_id:
        raise RuntimeError("Standby update lock is not owned by this update")
    if action == "release":
        owner.unlink()
        MIRROR_LOCK.rmdir()
    elif action == "pull":
        mirror_check()
        command("docker", "pull", image(target))
    elif action == "save-config":
        mirror_check()
        files = mirror_files(json.loads(command("docker", "inspect", "gitlab16"))[0])
        directory = Path("/Users/hannes/server/gitlab/backups/updates") / run_id / target
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        with (directory / "gitlab-configuration.tar").open("xb") as stream:
            execute("gitlab16", "tar", "-cf", "-", "-C", "/etc", "gitlab", stdout=stream)
            stream.flush()
            os.fsync(stream.fileno())
        for path in files:
            shutil.copy2(path, directory / path.name)
        log("Standby configuration retained at " + str(directory) +
            "; standby data will be replaced by the final production sync")
    elif action == "update":
        mirror_check()
        files = mirror_files(json.loads(command("docker", "inspect", "gitlab16"))[0])
        config, normalize = mirror_config(files)
        text = MIRROR_COMPOSE.read_text()
        text, count = re.subn(
            r"(?m)^([ \t]*image:[ \t]*)gitlab/gitlab-ce:[0-9]+\.[0-9]+\.[0-9]+-ce\.0[ \t]*$",
            lambda match: match[1] + image(target), text)
        if count != 1:
            raise RuntimeError("Expected exactly one pinned GitLab CE image in standby Compose")
        temporary = MIRROR_COMPOSE.with_name("docker-compose.yml.update-tmp")
        with temporary.open("x") as stream:
            stream.write(text + ("" if text.endswith("\n") else "\n"))
        temporary.replace(MIRROR_COMPOSE)
        if normalize:
            normalize_mirror_override(files[1])
            log("Standby override backup path normalized to " + MIRROR_BACKUP_ROOT)
        updated, still_needs_normalization = mirror_config(files)
        expected = config["services"]["gitlab"]["environment"]["GITLAB_OMNIBUS_CONFIG"]
        if normalize:
            expected = expected.replace(
                "gitlab_rails['backup_path'] = '/gitlab-backup-archives'",
                "gitlab_rails['backup_path'] = '/gitlab-backup-archives/.sync'")
        if (updated["services"]["gitlab"]["image"] != image(target)
                or still_needs_normalization
                or updated["services"]["gitlab"]["environment"]["GITLAB_OMNIBUS_CONFIG"] != expected):
            raise RuntimeError("Standby Compose image or GitLab configuration changed unexpectedly")
        mirror_dc(files, "up", "-d", "--no-deps", "--pull", "never", "gitlab")
        check_gitlab("gitlab16", target, wait=True)
    else:
        raise RuntimeError("Unknown standby action")


def preflight(jobs, args):
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    jobs.guard()
    if PENDING.exists():
        raise RuntimeError("Unfinished update: inspect " + str(PENDING) +
                           "; do not retry or downgrade blindly")
    for path in (APPS, RUNNER):
        if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o077:
            raise RuntimeError("Expected private root-owned Compose file: " + str(path))
    if shutil.disk_usage(RUNTIME).free < 50 * 1024**3:
        raise RuntimeError("Production requires at least 50 GiB free")
    command("systemctl", "is-active", "--quiet", "beelink-docker-runner.service")
    if not jobs.STATE.is_dir() or not Path("/home/hannes/.ssh/gitlab-mac-backup").is_file():
        raise RuntimeError("Install the normal GitLab mirror job and its restricted SSH key first")
    if (jobs.STATE / "gitlab-pending-id").exists():
        raise RuntimeError("Complete the pending GitLab mirror backup before updating")
    current = installed(CONTAINER)
    if execute(CONTAINER, "dpkg-query", "--show", "--showformat=${Package}",
               "gitlab-ce") != "gitlab-ce":
        raise RuntimeError("Only GitLab CE is supported")
    validate_path(current, args.versions, args.upgrade_path_reviewed)
    config = json.loads(dc(APPS, "config", "--format", "json"))
    details = json.loads(command("docker", "inspect", CONTAINER))[0]
    if not details["State"]["Running"] or command(
            "docker", "image", "inspect", config["services"]["gitlab"]["image"],
            "--format", "{{.Id}}") != details["Image"]:
        raise RuntimeError("Production Compose image differs from the running container")
    runner = json.loads(RUNNER.read_text())["services"]["runner"]["image"]
    match = re.fullmatch(r"gitlab/gitlab-runner:v([0-9]+\.[0-9]+\.[0-9]+)", runner)
    if not match:
        raise RuntimeError("Expected a pinned official Docker runner image")
    validate_runners(match[1], args.versions, args.runner_versions)
    dc(RUNNER, "exec", "-T", "runner", "gitlab-runner", "verify")
    check_gitlab(CONTAINER, current)
    remote(jobs, "plan", current, "00000000T000000Z")
    return current


def require_paused_ci():
    execute(CONTAINER, "gitlab-rails", "runner", """
abort('Pause ALL runners in GitLab before updating') if Ci::Runner.active.exists?
abort('Wait for running CI jobs to finish') if Ci::Build.where(status: %w[running preparing]).exists?
puts 'All runners paused; no running CI jobs'
""")


def update(jobs, args, current):
    global LOG
    require_paused_ci()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = BACKUPS / run_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    with (directory / "update.log").open("x") as logfile:
        LOG = logfile
        try:
            write_json(PENDING, {"run_id": run_id, "from": current,
                                 "targets": args.versions, "runners": args.runner_versions,
                                 "backups": str(directory)})
            remote(jobs, "acquire", current, run_id)
            # Keep both the pending marker and the standby lock on any failure.
            command("systemctl", "stop", "beelink-docker-runner.service")
            for target, runner_target in zip(args.versions, args.runner_versions):
                jobs.guard()
                log("Preparing upgrade " + current + " -> " + target)
                command("docker", "pull", image(target))
                command("docker", "pull", image(runner_target, runner=True))
                remote(jobs, "pull", target, run_id)
                step = directory / target
                backup(CONTAINER, step)
                shutil.copy2(APPS, step / "apps-compose.json")
                shutil.copy2(RUNNER, step / "runner-compose.json")
                remote(jobs, "save-config", target, run_id)
                apps = json.loads(APPS.read_text())
                apps["services"]["gitlab"]["image"] = image(target)
                write_json(APPS, apps)
                dc(APPS, "up", "-d", "--no-deps", "--pull", "never", "gitlab")
                check_gitlab(CONTAINER, target, wait=True)
                remote(jobs, "update", target, run_id)
                runner = json.loads(RUNNER.read_text())
                runner["services"]["runner"]["image"] = image(runner_target, runner=True)
                write_json(RUNNER, runner)
                current = target
            command("systemctl", "start", "beelink-docker-runner.service")
            wait_runner(args.runner_versions[-1])
            dc(RUNNER, "exec", "-T", "runner", "gitlab-runner", "verify")
            remote(jobs, "release", current, run_id)
            # The normal receiver must own the mirror lock during this restore.
            jobs.gitlab()
            PENDING.rename(directory / "completed.json")
            log("Update and standby sync completed. Runners remain paused in GitLab; "
                "run a CI smoke test after unpausing them.")
        except BaseException:
            log("ERROR: Update incomplete. No automatic rollback. Inspect " + str(PENDING) +
                " and " + str(directory) + "; standby lock may remain. "
                "Keep CI paused until recovery is complete.")
            raise
        finally:
            LOG = None


def main():
    os.umask(0o077)
    os.environ.update(LC_ALL="C", LANG="C")
    if len(sys.argv) == 5 and sys.argv[1] == "--mirror-worker":
        mirror_worker(*sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--versions", nargs="+", required=True,
                        help="Exact target versions, including every required upgrade stop")
    parser.add_argument("--runner-versions", nargs="+",
                        help="One runner version per stop (default: same as GitLab); "
                        "use when Runner patch releases differ")
    parser.add_argument("--upgrade-path-reviewed", action="store_true",
                        help="Confirm that the listed minor/major stops follow GitLab's upgrade path")
    parser.add_argument("--apply", action="store_true",
                        help="Perform the update; default only checks and displays the plan")
    args = parser.parse_args()
    args.runner_versions = args.runner_versions or args.versions
    for target in args.versions:
        version(target)
    jobs = load_jobs()
    with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = preflight(jobs, args)
        log("GitLab + Runner + mac2016: " + " -> ".join([current, *args.versions]))
        if args.apply:
            update(jobs, args, current)
        else:
            log("Check only: no images pulled or configuration changed. "
                "Pause all GitLab runners, then repeat with --apply.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        log("ERROR: " + str(error))
        sys.exit(1)
