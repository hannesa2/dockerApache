import os
from pathlib import Path
import subprocess
import tempfile
import unittest


WRAPPER = Path(__file__).resolve().parents[1] / "owncloud/run-sync-with-email.sh"


class SyncJobEmailTest(unittest.TestCase):
    def run_job(self, name, exit_code=0, overrides=None, arguments=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mail = root / "mail"
            mail.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$MAIL_ARGS"\n'
                'cat > "$MAIL_BODY"\n'
            )
            mail.chmod(0o755)
            hostname = root / "hostname"
            hostname.write_text('#!/bin/sh\nprintf "Latitude\\n"\n')
            hostname.chmod(0o755)
            job = root / "job"
            job.write_text('#!/bin/sh\nexit "$JOB_EXIT"\n')
            job.chmod(0o755)
            reporter = root / "reporter.py"
            reporter.write_text("import sys\n")
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                       MAIL_ARGS=str(root / "args"), MAIL_BODY=str(root / "body"),
                       SYNC_JOB_REPORTER=str(reporter), JOB_EXIT=str(exit_code))
            env.update(overrides or {})
            result = subprocess.run(
                ["bash", str(WRAPPER), name, str(job)]
                + (arguments or []),
                env=env, capture_output=True, text=True, check=False,
            )
            return result, (root / "args").read_text(), (root / "body").read_text()

    def test_postgresql_route_on_success_and_failure(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                result, args, body = self.run_job("PostgreSQL backups", exit_code)
                self.assertEqual(result.returncode, exit_code)
                self.assertIn("Latitude -> /daten/sicherung.postgresql/dump/daily/ (lokal)", args)
                self.assertIn("Host: Latitude -> /daten/sicherung.postgresql/dump/daily/ (lokal)", body)
                self.assertIn("Job: PostgreSQL backups\n", body)

    def test_mirror_targets(self):
        targets = {
            "ownCloud mirror sync": "mac2016 /Volumes/SamsungT5_1T/ownCloudBackup/owncloud",
            "MXServer mirror sync": "mac2016 /Volumes/SamsungT5_1T/MXServerMirror",
            "Matomo mirror sync": "mac2016 /Users/hannes/server/apache/www/mxdocs/piwik",
            "GitLab standby backup": "mac2016.local GitLab-Standby (gitlab16; /Users/hannes/server/gitlab)",
        }
        for job, target in targets.items():
            with self.subTest(job=job):
                result, args, body = self.run_job(job, 1)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"Host: Latitude -> {target}", body)
                self.assertIn(f"Latitude -> {target}", args)

    def test_env_overrides_and_unknown_job(self):
        result, _, body = self.run_job(
            "Matomo mirror sync", overrides={"MATOMO_MIRROR_DIR": "/custom/matomo"})
        self.assertEqual(result.returncode, 0)
        self.assertIn("Host: Latitude -> mac2016 /custom/matomo", body)
        _, _, body = self.run_job("Unknown")
        self.assertIn("Host: Latitude -> unbekannt", body)

    def test_nextcloud_missing_configuration_is_unknown(self):
        _, _, body = self.run_job("Nextcloud mirror sync")
        self.assertIn("Host: Latitude -> unbekannt", body)


if __name__ == "__main__":
    unittest.main()
