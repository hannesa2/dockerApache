import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


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

    def test_weekly_timer_serializes_backups_and_notifies_failures(self):
        definitions = BACKUP.units()
        service = definitions["beelink-config-backup.service"]
        timer = definitions["beelink-config-backup.timer"]
        self.assertIn("OnFailure=beelink-config-backup-failure.service", service)
        self.assertIn("/usr/bin/flock -w 14400 /run/lock/beelink-staging-control.lock", service)
        self.assertIn("--backup-and-verify", service)
        self.assertIn("OnCalendar=Sun *-*-* 02:15:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("--notify-failure", definitions["beelink-config-backup-failure.service"])

    def test_weekly_install_requires_a_verified_report(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(BACKUP, "STATE", Path(temporary)), \
                patch.object(BACKUP, "validate_repository"):
            with self.assertRaisesRegex(RuntimeError, "Run and verify"):
                BACKUP.install_weekly({})
            (Path(temporary) / "latest.json").write_text(
                '{"snapshot":"invalid","verified_entries":0}')
            with self.assertRaisesRegex(RuntimeError, "Invalid verified"):
                BACKUP.install_weekly({})

    def test_failure_notification_reports_mail_errors(self):
        with patch.object(BACKUP.subprocess, "run") as run:
            BACKUP.notify_failure()
        self.assertIn("hannes.software@gmx.at", run.call_args.args[0])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_restic_gets_root_home_under_systemd(self):
        with patch.dict(BACKUP.os.environ, {}, clear=True), \
                patch.object(BACKUP.subprocess, "run") as run:
            BACKUP.run_restic({"RESTIC_REPOSITORY": "sftp:backup:/repo"}, "snapshots")
        self.assertEqual(run.call_args.kwargs["env"]["HOME"], "/root")
        self.assertEqual(run.call_args.kwargs["env"]["RESTIC_REPOSITORY"],
                         "sftp:backup:/repo")


if __name__ == "__main__":
    unittest.main()
