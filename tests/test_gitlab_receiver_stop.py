from pathlib import Path
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "gitlab/mac2016-backup-receiver.sh"


class GitlabReceiverStopTest(unittest.TestCase):
    def run_stop(self, status="down: /opt/gitlab/service/puma: 1s, normally up",
                 stop_exit=0, status_exit=0, service="puma"):
        function = SOURCE.read_text().split("stop_service() {", 1)[1].split(
            "\ncleanup() {", 1)[0]
        script = """set -euo pipefail
docker() {
    printf '%s\\n' "$*" >&2
    if [ "$4" = status ]; then
        printf '%s\\n' "$STATUS"
        return "$STATUS_EXIT"
    fi
    return "$STOP_EXIT"
}
stop_service() {""" + function + '\nstop_service "$SERVICE"\n'
        import os
        return subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                              env={**os.environ, "STATUS": status, "STOP_EXIT": str(stop_exit),
                                   "STATUS_EXIT": str(status_exit), "SERVICE": service})

    def test_waits_five_minutes_and_verifies_down(self):
        result = self.run_stop()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sv -w 300 down /opt/gitlab/service/puma", result.stderr)
        self.assertIn("sv status /opt/gitlab/service/puma", result.stderr)

    def test_timeout_remains_failure_and_does_not_verify_success(self):
        result = self.run_stop(stop_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not stop within 300", result.stderr)
        self.assertNotIn("sv status", result.stderr)

    def test_running_or_unreadable_service_remains_failure(self):
        for kwargs in ({"status": "run: /opt/gitlab/service/puma: (pid 123) 10s"},
                       {"status_exit": 1}, {"status": ""}):
            with self.subTest(kwargs=kwargs):
                self.assertNotEqual(self.run_stop(**kwargs).returncode, 0)

    def test_unknown_service_never_executes_docker(self):
        result = self.run_stop(service="postgresql")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("exec gitlab16", result.stderr)

    def test_all_receiver_stop_paths_use_verified_helper(self):
        source = SOURCE.read_text()
        self.assertNotIn('gitlab-ctl stop "$service"', source)
        self.assertEqual(source.count('stop_service "$service"'), 4)
        self.assertLess(source.index('stop_service "$service"\ndone'),
                        source.index('restore_started=1'))


if __name__ == "__main__":
    unittest.main()
