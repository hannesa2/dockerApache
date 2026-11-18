import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "latitude_application_fence", Path(__file__).resolve().parents[1] /
    "monitoring/fence-latitude-application-starts.py")
FENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FENCE)


class LatitudeApplicationFenceTest(unittest.TestCase):
    def test_targets_only_migrated_application_services_and_containers(self):
        self.assertEqual(set(FENCE.SERVICES), {
            "apache2.service", "php8.3-fpm.service", "php8.4-fpm.service",
            "glassfish.service", "wildfly.service", "githubArtifactsPy.service"})
        self.assertEqual(set(FENCE.CONTAINERS), {
            "apache-apache-1", "owncloud-staging-owncloud-1", "gitlab", "gitlab-runner"})
        self.assertNotIn("camera-postgres-db-1", FENCE.CONTAINERS)
        self.assertNotIn("homeassistant", FENCE.CONTAINERS)

    def test_wrong_host_is_rejected_before_touching_services(self):
        with patch.object(FENCE.os, "getuid", return_value=0), \
                patch.object(FENCE.socket, "gethostname", return_value="beelink"), \
                patch("sys.argv", ["fence", "--fence"]), \
                patch.object(FENCE, "fence") as fence:
            with self.assertRaisesRegex(RuntimeError, "directly on Latitude"):
                FENCE.main()
            fence.assert_not_called()

    def test_reads_disabled_unit_state_without_treating_it_as_command_failure(self):
        with patch.object(FENCE.subprocess, "run",
                          return_value=subprocess.CompletedProcess(
                              [], 1, stdout="disabled\n", stderr="")):
            self.assertEqual(FENCE.unit_enabled("apache2.service"), "disabled")


if __name__ == "__main__":
    unittest.main()
