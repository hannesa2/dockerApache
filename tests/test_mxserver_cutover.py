import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CutoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        root = self.root
        for directory in ("mxserver", "mxserver-postgres", "bin", "etc", "backup/dump",
                          "server", "home", "state"):
            (root / directory).mkdir(parents=True)
        for name in ("cutover-locks.sh", "build-freeze-hba.py", "verify-postgres-data.sql",
                     "wait-latitude-postgres.sh", "glassfish-mxserver-postgres.conf"):
            shutil.copy2(ROOT / "mxserver" / name, root / "mxserver" / name)
        (root / "mxserver/sync-latitude.sh").write_text("new mirror sync\n")
        (root / "mxserver-postgres/docker-compose.yml").write_text("services: {}\n")
        (root / "mirror").write_text("old mirror sync\n")
        (root / "etc/pg_hba.conf").write_text("local all all peer\n")
        (root / "etc/start.conf").write_text("# original\nauto\n")
        (root / "etc/server.crt").write_text("test certificate")
        (root / "etc/server.key").write_text("test key")
        (root / "home/.pgpass").write_text("localhost:5432:MXServer:hannes:test\n")
        (root / "home/.pgpass").chmod(0o600)
        (root / "backup/dump/.postgresql-backup.lock").touch()
        (root / "state/native").touch()
        (root / "state/glassfish").touch()
        (root / "state/cgroup.procs").write_text("2529\n")

        script = (ROOT / "mxserver/cutover-latitude-postgres.sh").read_text()
        replacements = {
            "/etc/postgresql/16/main/pg_hba.conf": str(root / "etc/pg_hba.conf"),
            "/etc/postgresql/16/main/start.conf": str(root / "etc/start.conf"),
            "/etc/mxserver-postgres-dockerized": str(root / "etc/marker"),
            "/home/hannes/server/mxserver-postgres": str(root / "server/mxserver-postgres"),
            "/usr/local/sbin/mxserver-mirror-sync": str(root / "mirror"),
            "/usr/local/sbin/mxserver-postgres-wait": str(root / "boot-wait"),
            "/etc/systemd/system/glassfish.service.d/mxserver-postgres.conf": str(root / "etc/glassfish.service.d/mxserver-postgres.conf"),
            "/daten/sicherung.postgresql/dump/.postgresql-backup.lock": str(root / "backup/dump/.postgresql-backup.lock"),
            "/run/lock/mxserver-mirror-sync.lock": str(root / "state/mirror.lock"),
            "/daten/sicherung.postgresql": str(root / "backup"),
            "/home/hannes/.pgpass": str(root / "home/.pgpass"),
            "/sys/fs/cgroup${glassfish_group}/cgroup.procs": str(root / "state/cgroup.procs"),
        }
        for old, new in replacements.items():
            self.assertIn(old, script)
            script = script.replace(old, new)
        (root / "mxserver/cutover.sh").write_text(script)

        mock = root / "bin/mock"
        mock.write_text(r"""#!/usr/bin/env python3
import os
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(os.environ["MOCK_ROOT"])
state = root / "state"
name = Path(sys.argv[0]).name
args = sys.argv[1:]
failure = os.environ.get("FAIL_AT", "")
def fail_once(point):
    flag = state / ("failed-" + point)
    if failure == point and not flag.exists():
        flag.touch()
        sys.exit(1)
with (state / "calls").open("a") as out:
    out.write(name + " " + " ".join(args) + "\n")
if name == "id":
    print("0" if args == ["-u"] else "hannes")
elif name == "hostname":
    print("Latitude")
elif name in ("mountpoint", "pg_restore"):
    pass
elif name == "stat":
    print("600:hannes" if "%a:%U" in args else "hannes")
elif name == "ss":
    pass
elif name == "openssl":
    if "-pubin" in args:
        sys.stdin.read()
    if "-checkend" not in args:
        print("same-public-key")
elif name == "git":
    print("old mirror sync")
elif name == "chown":
    pass
elif name == "cp":
    if os.environ.get("FAIL_ROLLBACK") == "hba" and args[-1] == str(root / "etc/pg_hba.conf"):
        print("simulated restore failure", file=sys.stderr)
        sys.exit(1)
    sys.exit(subprocess.run(["/bin/cp"] + args).returncode)
elif name == "install":
    if args[-1] == str(root / "mirror"):
        fail_once("mirror")
    if args[-1] == str(root / "etc/marker"):
        fail_once("marker")
    if "-d" in args:
        Path(args[-1]).mkdir(parents=True, exist_ok=True)
    else:
        shutil.copyfile(args[-2], args[-1])
        Path(args[-1]).chmod(0o600 if "0600" in args else 0o644)
elif name == "systemctl":
    if args[0] == "daemon-reload":
        sys.exit(0)
    if args[0] == "show":
        print("/system.slice/glassfish.service")
        sys.exit(0)
    if args[0] == "kill":
        if failure != "leftover":
            (state / "cgroup.procs").write_text("")
        sys.exit(0)
    target = "glassfish" if args[-1] == "glassfish" else "native"
    flag = state / target
    if args[0] == "is-active":
        sys.exit(0 if flag.exists() else 3)
    if args[0] == "stop":
        flag.unlink(missing_ok=True)
        if target == "glassfish" and failure != "leftover" and not os.environ.get("FAILED_UNIT"):
            (state / "cgroup.procs").write_text("")
        elif target == "glassfish":
            (state / "failed-leftover").touch()
    if args[0] == "start":
        if target == "glassfish" and (state / "cgroup.procs").read_text().strip():
            print("Admin port occupied", file=sys.stderr)
            sys.exit(1)
        if target == "glassfish" and (state / "exposed").exists():
            fail_once("glassfish")
        flag.touch()
        if target == "glassfish":
            (state / "cgroup.procs").write_text("2529\n")
elif name == "pg_ctlcluster":
    if args[-1] == "stop":
        (state / "native").unlink(missing_ok=True)
        fail_once("native-stop")
    elif args[-1] == "start":
        (state / "native").touch()
    elif args[-1] == "reload":
        fail_once("freeze")
elif name == "runuser":
    command = " ".join(args)
    if "pg_dumpall" in command:
        print("CREATE ROLE postgres;\nALTER ROLE postgres WITH LOGIN;")
    elif "pg_dump" in command:
        fail_once("dump")
        print("test dump")
    elif "psql" in command and "current_setting" in command:
        settings = {"server_version_num": "160015", "port": "5432", "ssl": "on",
                    "hba_file": str(root / "etc/pg_hba.conf"),
                    "ssl_cert_file": str(root / "etc/server.crt"),
                    "ssl_key_file": str(root / "etc/server.key")}
        print(next(value for key, value in settings.items() if "'" + key + "'" in command))
    elif "pg_database WHERE NOT" in command:
        print("0")
    elif "pg_database WHERE datname=" in command:
        print("1")
    elif "pg_hba_file_rules" in command or "pg_stat_activity" in command:
        print("1" if failure == "sessions" and "pg_stat_activity" in command else "0")
    elif "-h localhost" in command:
        if "Temporary MXServer migration freeze." in (root / "etc/pg_hba.conf").read_text() \
                and (state / "native").exists():
            print(os.environ.get("FREEZE_ERROR", "pg_hba.conf rejects connection"), file=sys.stderr)
            sys.exit(2)
    elif " -d MXServer" in command:
        sys.stdin.read()
        print("public.example|1|42")
    else:
        print("unexpected runuser " + command, file=sys.stderr)
        sys.exit(1)
elif name == "psql":
    fail_once("stage-login")
    print("hannes|MXServer|t")
elif name == "docker":
    if args[:2] == ["image", "inspect"]:
        pass
    elif args[0] == "inspect":
        if "-f" not in args:
            sys.exit(1)
        print("starting" if failure == "boot-wait" else "healthy")
    elif args[0] == "run":
        print("999")
    elif args[0] == "exec":
        if "pg_stat_activity" in " ".join(args):
            print("8")
        elif "pg_restore" in args or "psql" in args:
            if not sys.stdin.isatty():
                sys.stdin.read()
            if "pg_restore" in args:
                fail_once("restore")
            elif "-d" in args and "MXServer" in args:
                if failure == "checksum":
                    (state / "failed-checksum").touch()
                    print("public.example|1|43")
                else:
                    print("public.example|1|42")
    elif args[0] == "compose":
        if "stop" in args and os.environ.get("FAIL_ROLLBACK") == "stop":
            sys.exit(1)
        if "up" in args and "--force-recreate" in args:
            (state / "exposed").touch()
            fail_once("exposure")
        if "up" in args and "--force-recreate" not in args:
            fail_once("staging")
elif name == "curl":
    print("200")
elif name == "sleep":
    pass
elif name == "seq":
    print("1\n2")
else:
    print("unexpected mock " + name, file=sys.stderr)
    sys.exit(1)
""")
        mock.chmod(0o755)
        for name in ("id", "hostname", "mountpoint", "stat", "ss", "openssl", "git", "chown",
                     "cp", "install", "systemctl", "pg_ctlcluster", "runuser", "psql", "docker",
                     "pg_dump", "pg_dumpall", "pg_restore", "curl", "sleep", "seq"):
            (root / "bin" / name).symlink_to(mock)
        self.env = dict(os.environ, PATH=str(root / "bin") + os.pathsep + os.environ["PATH"],
                        MOCK_ROOT=str(root))

    def run_cutover(self, failure="", **extra):
        return subprocess.run(
            ["bash", str(self.root / "mxserver/cutover.sh"), "--execute"],
            env=dict(self.env, FAIL_AT=failure, **extra), text=True, capture_output=True, timeout=30,
        )

    def assert_pre_exposure_failure_restores_native(self, failure):
        result = self.run_cutover(failure)
        self.assertNotEqual(result.returncode, 0)
        if failure != "sessions":
            self.assertTrue((self.root / ("state/failed-" + failure)).exists(), result.stderr)
        self.assertTrue((self.root / "state/native").exists())
        self.assertTrue((self.root / "state/glassfish").exists())
        self.assertFalse((self.root / "etc/marker").exists())
        self.assertEqual((self.root / "etc/pg_hba.conf").read_text(), "local all all peer\n")
        self.assertEqual((self.root / "etc/start.conf").read_text(), "# original\nauto\n")
        self.assertEqual((self.root / "mirror").read_text(), "old mirror sync\n")
        self.assertFalse((self.root / "state/mirror.lock").exists())
        self.assertFalse((self.root / "etc/glassfish.service.d/mxserver-postgres.conf").exists())
        self.assertFalse((self.root / "boot-wait").exists())

    def test_pre_exposure_failures_restore_native(self):
        self.assert_pre_exposure_failure_restores_native(os.environ.get("TEST_FAILURE", "staging"))

    def test_rollback_stop_failure_keeps_glassfish_stopped(self):
        result = self.run_cutover("staging", FAIL_ROLLBACK="stop")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / "state/failed-staging").exists(), result.stderr)
        self.assertFalse((self.root / "state/glassfish").exists())
        self.assertIn("Stop staging container manually", result.stderr)
        self.assertTrue((self.root / "state/mirror.lock").is_dir())

    def test_rollback_hba_failure_keeps_glassfish_stopped(self):
        result = self.run_cutover("staging", FAIL_ROLLBACK="hba")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / "state/failed-staging").exists(), result.stderr)
        self.assertFalse((self.root / "state/glassfish").exists())
        self.assertIn("Restore native pg_hba.conf", result.stderr)
        self.assertTrue((self.root / "state/mirror.lock").is_dir())

    def test_post_exposure_failure_does_not_restore_stale_native(self):
        failure = os.environ.get("TEST_POST_FAILURE", "exposure")
        result = self.run_cutover(failure)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / ("state/failed-" + failure)).exists(), result.stderr)
        self.assertIn("Do NOT restart the stale native database", result.stderr)
        self.assertTrue((self.root / "state/exposed").exists())
        self.assertFalse((self.root / "state/native").exists())
        self.assertFalse((self.root / "state/glassfish").exists())
        self.assertTrue((self.root / "etc/marker").exists())
        self.assertTrue((self.root / "boot-wait").exists())
        self.assertTrue((self.root / "etc/glassfish.service.d/mxserver-postgres.conf").exists())
        self.assertIn("manual", (self.root / "etc/start.conf").read_text())
        self.assertTrue((self.root / "state/mirror.lock").is_dir())

    def test_success_switches_sources_and_restarts_glassfish(self):
        result = self.run_cutover()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "state/native").exists())
        self.assertTrue((self.root / "state/glassfish").exists())
        self.assertTrue((self.root / "etc/marker").exists())
        self.assertIn("MXSERVER_POSTGRES_PORT=5432",
                      (self.root / "mxserver-postgres/.env").read_text())
        self.assertIn("manual", (self.root / "etc/start.conf").read_text())
        self.assertEqual((self.root / "mirror").read_text(), "new mirror sync\n")
        self.assertFalse((self.root / "state/mirror.lock").exists())
        archive = next((self.root / "backup").glob("mxserver-cutover.*"))
        self.assertTrue((archive / "MXServer.dump").is_file())
        self.assertTrue((archive / "roles.sql").is_file())
        self.assertFalse((archive / "pgpass-stage").exists())

    def test_boot_gate_requires_healthy_database(self):
        wait = self.root / "mxserver/wait-latitude-postgres.sh"
        ready = subprocess.run(["bash", str(wait)], env=self.env, capture_output=True, text=True)
        self.assertEqual(ready.returncode, 0, ready.stderr)
        not_ready = subprocess.run(["bash", str(wait)], env=dict(self.env, FAIL_AT="boot-wait"),
                                   capture_output=True, text=True)
        self.assertNotEqual(not_ready.returncode, 0)
        self.assertIn("GlassFish will not start", not_ready.stderr)

    def test_german_freeze_error_is_recognized(self):
        result = self.run_cutover(FREEZE_ERROR="FATAL: pg_hba.conf lehnt Verbindung ab für Host")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_unit_leftover_is_terminated_before_freeze(self):
        result = self.run_cutover(FAILED_UNIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / "state/calls").read_text()
        self.assertIn("systemctl kill --kill-who=all --signal=SIGTERM glassfish", calls)
        self.assertLess(calls.index("systemctl kill"), calls.index("pg_ctlcluster 16 main reload"))

    def test_other_connection_errors_are_not_accepted_as_freeze(self):
        result = self.run_cutover(FREEZE_ERROR="FATAL: Passwort-Authentifizierung fehlgeschlagen")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not verify the freeze", result.stderr)
        self.assertTrue((self.root / "state/glassfish").exists())
        self.assertFalse((self.root / "state/exposed").exists())

    def test_leftover_process_prevents_freeze_and_duplicate_start(self):
        result = self.run_cutover("leftover")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GlassFish processes remain", result.stderr)
        self.assertEqual((self.root / "etc/pg_hba.conf").read_text(), "local all all peer\n")
        self.assertFalse((self.root / "state/exposed").exists())
        self.assertNotIn("systemctl start glassfish", (self.root / "state/calls").read_text())


if __name__ == "__main__":
    unittest.main()
