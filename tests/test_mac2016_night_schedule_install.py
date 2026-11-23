import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "night_schedule_installer",
    Path(__file__).resolve().parents[1] / "monitoring/install-mac2016-night-schedule.py")
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class NightScheduleInstallerTest(unittest.TestCase):
    def test_backup_helpers_target_files_inside_existing_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit_dir = root / "units"
            unit_dir.mkdir()
            backup_dir = root / "backup"
            backup_dir.mkdir(mode=0o700)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir(mode=0o700)
            (unit_dir / "beelink-certificates.timer").write_text(INSTALLER.OLD_CERTIFICATE_TIMER)
            (unit_dir / "beelink-config-backup.timer").write_text(INSTALLER.OLD_BACKUP_TIMER)
            with patch.object(INSTALLER, "UNIT_DIR", unit_dir), \
                    patch.object(INSTALLER, "INSTALLED_JOBS", jobs_dir), \
                    patch.object(INSTALLER.backup, "INSTALLED", backup_dir), \
                    patch.object(INSTALLER.renewal, "INSTALLED", root / "renewal.py"), \
                    patch.object(INSTALLER.os, "getuid", return_value=0), \
                    patch.object(INSTALLER.socket, "gethostname", return_value="beelink"), \
                    patch.object(INSTALLER.jobs, "guard"), \
                    patch.object(INSTALLER, "ensure_private_directory"), \
                    patch.object(INSTALLER, "install_copy") as copy, \
                    patch.object(INSTALLER, "run") as run:
                definitions = {}
                definitions.update(INSTALLER.renewal.units())
                definitions.update(INSTALLER.backup.units())
                for name in ("beelink-certificates.service", "beelink-config-backup.service",
                             "beelink-config-backup-failure.service"):
                    (unit_dir / name).write_text(definitions[name])
                INSTALLER.install()
            verify = next(call for call in run.call_args_list
                          if call.args[:2] == ("systemd-analyze", "verify"))
            self.assertIn("/run/systemd/generator:", verify.kwargs["env"]["SYSTEMD_UNIT_PATH"])
            targets = [call.args[1] for call in copy.call_args_list]
            self.assertIn(backup_dir / "backup-beelink-config.py", targets)
            self.assertNotIn(backup_dir, targets)
            self.assertNotIn(backup_dir.parent / "control-beelink-staging.py", targets)

    def test_copy_refuses_directory_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_text("test")
            with self.assertRaisesRegex(RuntimeError, "root-owned regular file"):
                INSTALLER.install_copy(source, root)
            self.assertEqual(source.read_text(), "test")


if __name__ == "__main__":
    unittest.main()
