import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "beelink_docker_runner", Path(__file__).resolve().parents[1] /
    "monitoring/install-beelink-docker-runner.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class BeelinkDockerRunnerTest(unittest.TestCase):
    def test_config_uses_docker_without_privilege_or_socket_in_jobs(self):
        config = RUNNER.runner_config("glrt-abcdefghijk123456")
        self.assertIn('executor = "docker"', config)
        self.assertIn("privileged = false", config)
        self.assertIn('git.mxtracks.info:192.168.178.142', config)
        self.assertNotIn("docker.sock", config)
        self.assertIn('token = "glrt-abcdefghijk.123456"',
                      RUNNER.runner_config("glrt-abcdefghijk.123456"))
        for token in ("bad", "glrt-'; injected", "glrt-short", "glrt-\n" + "a" * 20):
            with self.assertRaises(RuntimeError):
                RUNNER.runner_config(token)

    def test_manager_socket_and_mount_bound_unit(self):
        service = RUNNER.compose_config()["services"]["runner"]
        self.assertEqual(service["restart"], "no")
        self.assertNotIn("ports", service)
        self.assertTrue(any(v["source"] == "/var/run/docker.sock"
                            for v in service["volumes"]))
        self.assertIn("BindsTo=beelink-production.service daten.mount", RUNNER.UNIT_TEXT)
        self.assertIn("Requires=beelink-production.service", RUNNER.UNIT_TEXT)

    def test_existing_unexpected_configuration_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(RUNNER.os, "chown"):
            path = Path(temporary) / "config"
            path.write_text("existing")
            with self.assertRaisesRegex(RuntimeError, "differs"):
                RUNNER.write_exact(path, "new", 0o600)
            self.assertEqual(path.read_text(), "existing")
            RUNNER.write_exact(path, "existing", 0o600)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_compose_serializes_cleanly(self):
        self.assertEqual(json.loads(json.dumps(RUNNER.compose_config())),
                         RUNNER.compose_config())


if __name__ == "__main__":
    unittest.main()
