import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "monitoring/postgresql-backup.sh"


@unittest.skipUnless(sys.platform.startswith("linux"), "Backup uses Linux utilities")
class PostgreSQLBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.dump_dir = self.root / "dump"
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        POSTGRES_BACKUP_ROOT=str(self.dump_dir),
                        POSTGRES_BACKUP_MOUNT=str(self.root))
        self.command("mountpoint", 'exit "${MOUNT_FAILURE:-0}"')
        self.command("pg_dump", """
            while [ "$#" -gt 0 ]; do
                if [ "$1" = "-d" ] && [ "$2" = "${FAIL_DATABASE:-none}" ]; then exit 1; fi
                if [ "$1" = "-f" ]; then printf 'archive' > "$2"; exit 0; fi
                shift
            done
            exit 1
        """)
        self.command("pg_restore", """
            [ "${FAIL_RESTORE:-0}" = 0 ] || exit 1
            if [ "$1" = "-l" ]; then echo '1; 0 0 TABLE DATA public example owner'; fi
        """)
        self.command("docker", '[ "${FAIL_DOCKER:-0}" = 0 ] || exit 1; printf archive')
        self.command("date", 'echo 20261115_191501')

    def command(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def run_backup(self, **env):
        return subprocess.run(["bash", str(SCRIPT)], env=dict(self.env, **env),
                              text=True, capture_output=True, check=False)

    def test_complete_backup_and_scoped_retention(self):
        managed = self.dump_dir / "daily"
        old = managed / "20260901_120000"
        old.mkdir(parents=True)
        (old / "backup-complete").write_text("complete")
        incomplete = managed / "20260901_120001"
        incomplete.mkdir()
        legacy = self.dump_dir / "old.bak"
        legacy.write_text("legacy")
        past = time.time() - 31 * 86400
        os.utime(old, (past, past))
        os.utime(incomplete, (past, past))
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        final = managed / "20261115_191501"
        self.assertEqual(len(list(final.glob("*.dump"))), 5)
        self.assertFalse((final / "host-owncloud.dump").exists())
        self.assertTrue((final / "backup-complete").is_file())
        self.assertEqual(final.stat().st_mode & 0o777, 0o700)
        self.assertFalse(old.exists())
        self.assertTrue(incomplete.exists())
        self.assertTrue(legacy.exists())

    def test_failure_never_publishes_or_expires_backup(self):
        managed = self.dump_dir / "daily"
        old = managed / "20260901_120000"
        old.mkdir(parents=True)
        (old / "backup-complete").write_text("complete")
        past = time.time() - 31 * 86400
        os.utime(old, (past, past))
        result = self.run_backup(FAIL_DATABASE="camDoor")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(old.exists())
        self.assertFalse((managed / "20261115_191501").exists())
        self.assertEqual(list(managed.glob(".incomplete.*")), [])

    def test_unmounted_storage_is_rejected(self):
        result = self.run_backup(MOUNT_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.dump_dir.exists())

    def test_corrupt_archive_fails(self):
        result = self.run_backup(FAIL_RESTORE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dump_dir / "daily/20261115_191501").exists())

    def test_docker_dump_failure_fails_the_job(self):
        result = self.run_backup(FAIL_DOCKER="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dump_dir / "daily/20261115_191501").exists())

    def test_existing_completed_backup_is_not_overwritten(self):
        self.assertEqual(self.run_backup().returncode, 0)
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.dump_dir / "daily/20261115_191501/backup-complete").is_file())


if __name__ == "__main__":
    unittest.main()
