import ast
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "monitoring/seed-latitude-to-beelink.sh"


class BeelinkSeedTest(unittest.TestCase):
    def test_requires_explicit_mode(self):
        result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--seed-only", result.stderr)

    def test_checks_host_before_running_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            for name, response in (("id", "0"), ("hostname", "beelink")):
                executable = path / name
                executable.write_text("#!/bin/sh\nprintf '%s\\n' '" + response + "'\n")
                executable.chmod(0o755)
            result = subprocess.run(
                ["bash", str(SCRIPT), "--seed-only"],
                env=dict(os.environ, PATH=temporary + os.pathsep + os.environ["PATH"]),
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("directly on Latitude", result.stderr)

    def test_seeds_without_switching_production(self):
        source = SCRIPT.read_text()
        self.assertIn("rsync --fake-super", source)
        self.assertIn("c0c42461-ae4c-4edb-a993-bba94a05ed6c", source)
        self.assertIn('gitlab-backup create "BACKUP=$gitlab_backup"', source)
        self.assertIn('pg_restore -f /dev/null "$dump"', source)
        self.assertNotIn("docker stop", source)
        self.assertNotIn("systemctl stop", source)
        self.assertNotIn("crontab -r", source)
        self.assertNotIn("--delete", source)
        self.assertIn("if [[ \"$result\" == 24 ]]", source)
        self.assertNotIn('"/srv/gitlab/data"', source)
        self.assertNotIn('"/daten/owncloud/database"', source)

    def test_isolated_orchestration(self):
        source = SCRIPT.read_text()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("bin", "monitoring", "daten", "run/lock"):
                (root / name).mkdir(parents=True)
            (root / "etc/mysql").mkdir(parents=True)
            (root / "etc/mysql/debian.cnf").write_text("[client]\nuser=root\n")
            for original in ("/daten", "/srv", "/opt", "/etc", "/home", "/usr/local", "/run/lock"):
                source = source.replace(original, str(root) + original)
            data_code = source.split('python3 - "$work/data-paths" <<\'PY\'\n', 1)[1].split("\nPY", 1)[0]
            tree = ast.parse(data_code)
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "paths"
                        for target in node.targets):
                    for name in ast.literal_eval(node.value):
                        Path(name).mkdir(parents=True, exist_ok=True)
            for name in ("camera-postgres/Dockerfile", "camera-postgres/docker-compose.yml",
                         "camera-postgres/pg_hba.conf", "mxserver-postgres/docker-compose.yml",
                         "mxserver-postgres/pg_hba.conf"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n")
            script = root / "monitoring/seed.sh"
            script.write_text(source)
            mock = root / "bin/mock"
            mock.write_text(r"""#!/usr/bin/env python3
import os
from pathlib import Path
import sys

root = Path(os.environ["SEED_TEST_ROOT"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "calls").open("a") as out:
    out.write(name + " " + " ".join(args) + "\n")
if name == "id":
    print("0")
elif name == "hostname":
    print("Latitude")
elif name == "stat":
    print("0:700")
elif name == "mysql":
    mode = os.environ.get("SEED_MYSQL_AUTH", "config")
    if mode == "none" or (mode == "defaults" and args[0].startswith("--defaults-file=")):
        raise SystemExit(1)
elif name == "docker":
    if args[0] == "inspect":
        print("true" if "-f" in args else "[]")
    elif args[:2] == ["ps", "-aq"]:
        print("test-container")
    elif "gitlab-backup" in args:
        backup = next(value[7:] for value in args if value.startswith("BACKUP="))
        target = root / "srv/gitlab/data/backups" / (backup + "_gitlab_backup.tar")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("test GitLab application backup\n")
    else:
        print("test database snapshot")
elif name == "mysqldump":
    expected = "--user=root" if os.environ.get("SEED_MYSQL_AUTH") == "defaults" else "--defaults-file="
    if not args[0].startswith(expected):
        raise SystemExit("Native dump did not reuse verified credentials")
    print("test SQL snapshot")
elif name == "crontab":
    print("# test crontab")
elif name == "git":
    sys.stdout.buffer.write(b"monitoring/seed.sh\0")
elif name == "runuser":
    if args[:4] != ["-u", "hannes", "--", "git"]:
        raise SystemExit("Git must run as the repository owner")
    os.execvp(args[3], args[3:])
elif name == "tar":
    if "-czf" in args:
        Path(args[args.index("-czf")+1]).write_text("test config archive")
elif name == "rsync":
    if "--relative" in args and os.environ.get("SEED_FILE_FAILURE"):
        sys.exit(int(os.environ["SEED_FILE_FAILURE"]))
elif name not in ("ssh", "mountpoint", "pg_restore", "mysql", "systemctl", "dpkg-query", "findmnt"):
    raise SystemExit("Unexpected command: " + name)
""")
            mock.chmod(0o755)
            for name in ("id", "hostname", "docker", "mysqldump", "crontab", "git", "tar",
                         "rsync", "ssh", "mountpoint", "pg_restore", "mysql", "systemctl",
                         "dpkg-query", "findmnt", "runuser", "stat"):
                (root / "bin" / name).symlink_to(mock)
            environment = dict(os.environ, PATH=str(root / "bin") + os.pathsep + os.environ["PATH"],
                               SEED_TEST_ROOT=str(root))
            for code, expected in (("", 0), ("24", 0), ("23", 1)):
                with self.subTest(rsync_exit=code):
                    result = subprocess.run(
                        ["bash", str(script), "--seed-only"],
                        env=dict(environment, SEED_FILE_FAILURE=code),
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    if code == "":
                        work = next((root / "daten").glob("latitude-beelink-seed.*"))
                        calls_before = (root / "calls").read_text()
                        result = subprocess.run(
                            ["bash", str(script), "--finish-seed", str(work)],
                            env=dict(environment, SEED_MYSQL_AUTH="none"),
                            capture_output=True, text=True, timeout=30,
                        )
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        calls = (root / "calls").read_text()[len(calls_before):]
                        self.assertIn("runuser -u hannes -- git", calls)
                        self.assertNotIn("gitlab-backup create", calls)
                        self.assertNotIn("mysql ", calls)
                        self.assertNotIn("--relative", calls)
                    if code == "24":
                        self.assertIn("WARNING: Live files vanished", result.stdout)
                    elif code == "23":
                        self.assertIn("Incomplete seed", result.stdout)
            for mode, expected in (("defaults", 0), ("none", 1)):
                with self.subTest(mysql_auth=mode):
                    result = subprocess.run(
                        ["bash", str(script), "--seed-only"],
                        env=dict(environment, SEED_MYSQL_AUTH=mode),
                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    if mode == "none":
                        self.assertIn("Run interactively", result.stderr)
            work = next((root / "daten").glob("latitude-beelink-seed.*"))
            (work / "seed.log").write_text("Incomplete live-file transfer\n")
            result = subprocess.run(
                ["bash", str(script), "--finish-seed", str(work)],
                env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not reach the repository step", result.stderr)


if __name__ == "__main__":
    unittest.main()
