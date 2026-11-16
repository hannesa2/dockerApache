import os
from pathlib import Path
import subprocess
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / "monitoring/check-beelink-homeassistant.sh"


class BeelinkMonitorTest(unittest.TestCase):
    def run_checks(self, http="200", ssh_status="0", response="beelink\nrunning|none"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, body in {
                "curl": 'printf "%s" "$TEST_HTTP"; [ "$TEST_HTTP" != error ]',
                "ssh": 'printf "%s\\n" "$TEST_RESPONSE"; exit "$TEST_SSH_STATUS"',
            }.items():
                command = root / name
                command.write_text("#!/bin/sh\n" + body + "\n")
                command.chmod(0o755)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                       TEST_HTTP=http, TEST_SSH_STATUS=ssh_status, TEST_RESPONSE=response)
            command = (
                'record() { printf "%s|%s|%s\\n" "$1" "$2" "$3"; }; '
                f'source "{HELPER}"; check_beelink_homeassistant'
            )
            result = subprocess.run(["bash", "-c", command], env=env, text=True,
                                    capture_output=True, check=True)
            return result.stdout

    def test_running_container_and_http(self):
        output = self.run_checks()
        self.assertIn("OK|beelink Home Assistant|HTTP 200", output)
        self.assertIn("OK|beelink container homeassistant|state=running health=none", output)

    def test_http_and_container_fail_independently(self):
        output = self.run_checks(http="error", response="beelink\nexited|none")
        self.assertIn("FAIL|beelink Home Assistant|HTTP 000", output)
        self.assertIn("FAIL|beelink container homeassistant|state=exited", output)

    def test_ssh_failure_does_not_hide_http_success(self):
        output = self.run_checks(ssh_status="255")
        self.assertIn("OK|beelink Home Assistant|HTTP 200", output)
        self.assertIn("FAIL|beelink SSH / Home Assistant container|", output)

    def test_wrong_host_is_rejected(self):
        output = self.run_checks(response="other-host\nrunning|healthy")
        self.assertIn("FAIL|beelink SSH / Home Assistant container|", output)
        self.assertNotIn("OK|beelink container homeassistant|", output)


if __name__ == "__main__":
    unittest.main()
