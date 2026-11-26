import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "owncloud/test-backintime-restore-beelink.sh"
COPY_CODE = SCRIPT.read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


class RestoreSamplesTest(unittest.TestCase):
    def run_copy(self, corrupt=False, user_files=True):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "backup", root / "test"
            target.mkdir()
            stage = source / "daten/beelink-backups/backintime-owncloud"
            stage.mkdir(parents=True)
            (stage / "owncloud.dump").write_bytes(b"database")
            checksum = hashlib.sha256(b"wrong" if corrupt else b"database").hexdigest()
            (stage / "owncloud.dump.sha256").write_text(checksum + "  /original/dump\n")
            data = source / "daten/beelink-runtime/rootfs/daten/owncloud/data"
            data.mkdir(parents=True)
            (data / ".ocdata").touch()
            config = source / "daten/beelink-runtime/apps/owncloud/volume/config"
            config.mkdir(parents=True)
            (config / "config.php").write_bytes(b"private config")
            if user_files:
                files = data / "test-user/files"
                files.mkdir(parents=True)
                (files / "document.txt").write_bytes(b"restored document")
                (files / "excluded-link").symlink_to(stage / "owncloud.dump")
            result = subprocess.run(
                ["python3", "-c", COPY_CODE, str(source), str(target)],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                self.assertEqual((target / "owncloud.dump").read_bytes(), b"database")
                manifest = (target / "sample-checksums.txt").read_text()
                self.assertIn("document.txt", manifest)
                self.assertNotIn("excluded-link", manifest)
                self.assertEqual(len(manifest.splitlines()), 3)
                self.assertEqual((files / "document.txt").read_bytes(), b"restored document")
                self.assertNotEqual(
                    (files / "document.txt").stat().st_ino,
                    (target / "files" / (files / "document.txt").relative_to(source)).stat().st_ino,
                )
            return result

    def test_restores_independent_sample_copies(self):
        result = self.run_copy()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_corrupted_dump(self):
        result = self.run_copy(corrupt=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)

    def test_rejects_absent_user_samples(self):
        result = self.run_copy(user_files=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No user files", result.stderr)


if __name__ == "__main__":
    unittest.main()
