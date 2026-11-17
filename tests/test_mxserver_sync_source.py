import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "mxserver/sync-latitude.sh"


class MirrorSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (root / "pictures").mkdir()
        for name in ("keyfile", "ssh-key", "known-hosts"):
            (root / name).write_text("fixture")
        replacements = {
            "/daten/beelink-runtime/rootfs/srv/postgres/data/mxinfo": str(root / "pictures"),
            "/daten/beelink-runtime/java-web/glassfish/glassfish/domains/domain1/config/keyfile":
                str(root / "keyfile"),
            "/srv/postgres/data/mxinfo": str(root / "pictures"),
            "/opt/glassfish-4.0/glassfish/domains/domain1/config/keyfile": str(root / "keyfile"),
            "/home/hannes/.ssh/id_rsa": str(root / "ssh-key"),
            "/home/hannes/.ssh/known_hosts": str(root / "known-hosts"),
            "/run/lock/mxserver-mirror-sync.lock": str(root / "lock"),
            "/etc/mxserver-postgres-dockerized": str(root / "cutover-marker"),
        }
        source = SOURCE.read_text()
        for old, new in sorted(replacements.items(), key=lambda item: -len(item[0])):
            self.assertIn(old, source)
            source = source.replace(old, new)
        self.script = root / "sync.sh"
        self.script.write_text(source)
        self.env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                        TEST_LOG=str(root / "calls"))
        commands = {
            "id": 'echo 0',
            "hostname": 'echo beelink',
            "findmnt": 'echo c0c42461-ae4c-4edb-a993-bba94a05ed6c',
            "pg_dump": 'exit 0',
            "runuser": 'echo "native $*" >> "$TEST_LOG"; [ "${DUMP_FAIL:-0}" = 0 ] || exit 1; echo snapshot',
            "docker": """
                echo "docker $*" >> "$TEST_LOG"
                if [ "$1" = inspect ]; then echo true; exit 0; fi
                [ "${DUMP_FAIL:-0}" = 0 ] || exit 1
                echo snapshot
            """,
            "ssh": 'echo "ssh $*" >> "$TEST_LOG"; cat >/dev/null',
            "scp": 'exit 0',
            "rsync": 'exit 0',
        }
        for name, body in commands.items():
            command = bin_dir / name
            command.write_text("#!/bin/sh\n" + body + "\n")
            command.chmod(0o755)

    def run_sync(self, *args, **env):
        return subprocess.run(["bash", str(self.script), *args],
                              env=dict(self.env, **env), stdin=subprocess.DEVNULL,
                              text=True, capture_output=True)

    def calls(self):
        return (self.root / "calls").read_text()

    def test_native_source_remains_default(self):
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("native -u postgres -- pg_dump", self.calls())
        self.assertNotIn("docker exec", self.calls())

    def test_docker_source_is_explicit(self):
        result = self.run_sync("--source=docker")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker exec --user postgres mxserver-postgres-db-1 pg_dump", self.calls())
        self.assertNotIn("native -u postgres", self.calls())

    def test_marker_selects_docker_source(self):
        (self.root / "cutover-marker").touch()
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker exec --user postgres mxserver-postgres-db-1 pg_dump", self.calls())
        self.assertNotIn("native -u postgres", self.calls())

    def test_explicit_native_overrides_marker(self):
        (self.root / "cutover-marker").touch()
        result = self.run_sync("--source=native")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("native -u postgres -- pg_dump", self.calls())

    def test_failed_dump_does_not_stop_mirror_application(self):
        result = self.run_sync("--source=docker", DUMP_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("stop mxserver", self.calls())

    def test_invalid_source_fails_before_changes(self):
        result = self.run_sync("--source=unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "calls").exists())

    def test_beelink_uses_new_container_role_and_pinned_mirror_alias(self):
        result = self.run_sync(BEELINK_PRODUCTION="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn("beelink-staging-mxserver-1 pg_dump -U beelink_restore", calls)
        self.assertIn("HostKeyAlias=mac2016", calls)
        self.assertIn("hannes@192.168.178.70", calls)
        self.assertNotIn("native -u postgres", calls)


if __name__ == "__main__":
    unittest.main()
