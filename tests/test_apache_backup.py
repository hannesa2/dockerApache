import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ApacheBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.apache = self.root / "apache"
        self.apache.mkdir()
        for name in ("backup.sh", "restore.sh", "docker-compose.yml"):
            shutil.copy(ROOT / "apache" / name, self.apache / name)
        self.data = self.root / "data"
        for folder in ("www", "vhosts"):
            (self.data / folder).mkdir(parents=True)
            (self.data / folder / "test.txt").write_text(folder)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text("""#!/bin/bash
printf '%s\\n' "$*" >> "$DOCKER_LOG"
if [[ " $* " == *" mysqldump "* ]]; then
    [[ "${FAIL_DATABASE:-}" != "${!#}" ]] || exit 1
    printf 'CREATE DATABASE IF NOT EXISTS `%s`;\\n' "${!#}"
elif [[ " $* " == *" mysql "* ]] && [[ " $* " != *" -e "* ]]; then
    cat >/dev/null
fi
""")
        docker.chmod(0o755)
        sudo = self.bin / "sudo"
        sudo.write_text("#!/bin/sh\nif [ \"$1\" = LANG=C ]; then shift; fi\nexec \"$@\"\n")
        sudo.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        APACHE_DATA_DIR=str(self.data), DOCKER_LOG=str(self.root / "docker.log"))
        for key in ("MYSQL_BACKUP_DATABASES", "BACKUP_DIR"):
            self.env.pop(key, None)

    def backup(self, **overrides):
        return subprocess.run(["bash", str(self.apache / "backup.sh"), str(self.root / "backups")],
                              env=dict(self.env, **overrides), text=True, capture_output=True)

    def restore(self, directory, replies):
        return subprocess.run(["bash", str(self.apache / "restore.sh"), str(directory)],
                              env=self.env, input=replies, text=True, capture_output=True)

    def test_backup_covers_both_and_restore_is_selective(self):
        result = self.backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = next((self.root / "backups").iterdir())
        self.assertEqual((backup / "databases.txt").read_text().splitlines(),
                         ["wordpress", "wordpress_mxtracks"])
        self.assertTrue((backup / "backup-complete").is_file())
        log = self.root / "docker.log"
        log.write_text("")
        result = self.restore(backup, "y\nn\nn\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = log.read_text()
        self.assertIn("DROP DATABASE IF EXISTS `wordpress`", commands)
        self.assertNotIn("DROP DATABASE IF EXISTS `wordpress_mxtracks`", commands)
        self.assertEqual((self.data / "www/test.txt").read_text(), "www")

    def test_failed_dump_never_marks_backup_complete(self):
        result = self.backup(FAIL_DATABASE="wordpress_mxtracks")
        self.assertNotEqual(result.returncode, 0)
        backup = next((self.root / "backups").iterdir())
        self.assertFalse((backup / "backup-complete").exists())

    def test_missing_dump_blocks_all_restores(self):
        self.assertEqual(self.backup().returncode, 0)
        backup = next((self.root / "backups").iterdir())
        (backup / "mysql_wordpress_mxtracks.sql.gz").unlink()
        log = self.root / "docker.log"
        log.write_text("")
        result = self.restore(backup, "y\ny\ny\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(log.read_text(), "")

    def test_custom_database_list_is_supported(self):
        self.assertEqual(self.backup(MYSQL_BACKUP_DATABASES="custom").returncode, 0)
        backup = next((self.root / "backups").iterdir())
        self.assertEqual((backup / "databases.txt").read_text(), "custom\n")


if __name__ == "__main__":
    unittest.main()
