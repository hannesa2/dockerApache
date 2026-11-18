import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "beelink_config_backup", Path(__file__).resolve().parents[1] /
    "monitoring/backup-beelink-config.py")
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)


class BeelinkConfigBackupTest(unittest.TestCase):
    def test_environment_parser_requires_exact_settings(self):
        values = BACKUP.parse_environment(
            "RESTIC_REPOSITORY=sftp:backup:/repo\n"
            "RESTIC_PASSWORD_FILE=/root/restic-password\n")
        self.assertEqual(values["RESTIC_REPOSITORY"], "sftp:backup:/repo")
        for text in ("RESTIC_REPOSITORY=sftp:backup:/repo\nUNKNOWN=value\n",
                     "RESTIC_REPOSITORY=repo\nRESTIC_REPOSITORY=other\n",
                     "RESTIC_REPOSITORY=repo\n"):
            with self.subTest(text=text):
                with self.assertRaises(RuntimeError):
                    BACKUP.parse_environment(text)

    def test_manifest_and_restore_verification_cover_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config").write_text("private configuration")
            (source / "alias").symlink_to("config")
            expected = BACKUP.manifest([source])
            restored_root = root / "restore"
            restored = restored_root / source.relative_to("/")
            restored.mkdir(parents=True)
            (restored / "config").write_text("private configuration")
            (restored / "alias").symlink_to("config")
            BACKUP.verify_restored(expected, restored_root)
            (restored / "config").write_text("different")
            with self.assertRaisesRegex(RuntimeError, "differs"):
                BACKUP.verify_restored(expected, restored_root)

    def test_repository_must_be_explicitly_external_or_absolute(self):
        with self.assertRaisesRegex(RuntimeError, "absolute local path or remote"):
            BACKUP.validate_repository({
                "RESTIC_REPOSITORY": "relative/repository",
                "RESTIC_PASSWORD_FILE": "/root/password"})


if __name__ == "__main__":
    unittest.main()
