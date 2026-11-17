import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "jobs", Path(__file__).resolve().parents[1] / "monitoring/beelink-jobs.py")
JOBS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(JOBS)


class BeelinkJobsTest(unittest.TestCase):
    def test_schedules_preserve_source_times_and_disable_catchup(self):
        definitions = JOBS.units()
        self.assertEqual(len(definitions), 14)
        expected = {"postgresql": "19:15:00", "apache": "03:05:00",
                    "gitlab": "03:15:00", "owncloud": "04:00:00",
                    "mxserver": "04:30:00", "matomo": "04:45:00", "monitor": "*:00:00"}
        for job, time in expected.items():
            service = definitions[f"beelink-job-{job}.service"]
            timer = definitions[f"beelink-job-{job}.timer"]
            self.assertIn(f"--run {job}", service)
            self.assertNotIn("Requires=beelink-staging", service)
            self.assertIn(f"OnCalendar=*-*-* {time}", timer)
            self.assertIn("Persistent=false", timer)

    def test_docker_sources_replace_frozen_native_sources(self):
        environment = JOBS.environment()
        self.assertEqual(environment["BEELINK_PRODUCTION"], "1")
        self.assertEqual(environment["POSTGRES_OWNCLOUD_CONTAINER"], "beelink-staging-owncloud-1")
        self.assertEqual(environment["POSTGRES_OWNCLOUD_USER"], "beelink_restore")
        self.assertIn("beelink-runtime/matomo", environment["MATOMO_SOURCE_DIR"])

    def test_job_failure_is_recorded_and_mailed(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(JOBS, "STATE", Path(temporary)), \
                    patch.object(JOBS.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], 1)), \
                    patch.object(JOBS, "run") as mail:
                with self.assertRaisesRegex(RuntimeError, "status 1"):
                    JOBS.notified_run("owncloud")
            report = json.loads((Path(temporary) / "owncloud.json").read_text())
            self.assertEqual(report["result"], "FAILED")
            self.assertEqual(mail.call_args.args[-1], "hannes.software@gmx.at")
            self.assertIn("FAILED", mail.call_args.args[2])

    def test_success_is_recorded_and_mailed_every_backup_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(JOBS, "STATE", Path(temporary)), \
                    patch.object(JOBS.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], 0)), \
                    patch.object(JOBS, "run") as mail:
                JOBS.notified_run("postgresql")
                JOBS.notified_run("postgresql")
                self.assertEqual(mail.call_count, 2)
            report = json.loads((Path(temporary) / "postgresql.json").read_text())
            self.assertEqual(report["result"], "SUCCESS")
            self.assertEqual((Path(temporary) / "postgresql.json").stat().st_mode & 0o777, 0o644)
            self.assertEqual(Path(temporary).stat().st_mode & 0o777, 0o755)

    def test_unreadable_status_directory_is_not_reported_as_pending(self):
        with patch.object(JOBS.os, "access", return_value=False), \
                patch("sys.argv", ["jobs", "--report"]):
            with self.assertRaisesRegex(RuntimeError, "missing or unreadable"):
                JOBS.main()

    def test_monitor_mails_initial_status_and_changes_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(JOBS, "STATE", Path(temporary)), \
                    patch.object(JOBS.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], 0)), \
                    patch.object(JOBS, "run") as mail:
                JOBS.notified_run("monitor")
                JOBS.notified_run("monitor")
                self.assertEqual(mail.call_count, 1)

    def test_mail_failure_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(JOBS, "STATE", Path(temporary)), \
                    patch.object(JOBS.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], 0)), \
                    patch.object(JOBS, "run", side_effect=RuntimeError("mail unavailable")):
                with self.assertRaisesRegex(RuntimeError, "mail unavailable"):
                    JOBS.notified_run("gitlab")

    def test_failed_preflight_does_not_install_timers(self):
        with patch.object(JOBS, "preflight", side_effect=RuntimeError("mirror missing")), \
                patch.object(JOBS, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "mirror missing"):
                JOBS.install()
            run.assert_not_called()

    def test_wrong_host_does_not_modify_source(self):
        with patch.object(JOBS.os, "getuid", return_value=0), \
                patch.object(JOBS.socket, "gethostname", return_value="latitude"):
            with self.assertRaisesRegex(RuntimeError, "directly on beelink"):
                JOBS.guard()


if __name__ == "__main__":
    unittest.main()
