import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "operations", Path(__file__).resolve().parents[1] /
    "monitoring/activate-beelink-operations.py")
OPS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OPS)


class BeelinkOperationsTest(unittest.TestCase):
    def test_mount_orders_encrypted_device_before_mount(self):
        definitions = OPS.definitions()
        mount = definitions["daten.mount.d/beelink-encryption.conf"]
        self.assertIn("Requires=systemd-cryptsetup@beelink\\x2dt7.service", mount)
        self.assertIn("After=systemd-cryptsetup@beelink\\x2dt7.service", mount)
        self.assertNotIn("reboot", definitions["beelink-operations-check.service"])
        for port in (80, 443):
            socket = definitions[f"beelink-public-web-{port}.socket.d/boot-order.conf"]
            self.assertIn("DefaultDependencies=no", socket)
            self.assertIn("Conflicts=shutdown.target", socket)
            self.assertIn("Before=shutdown.target", socket)

    def test_checks_run_all_jobs_sequentially_even_after_failure(self):
        calls = []

        def execute(command):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1 if "apache" in command[-1] else 0)

        with patch.object(OPS.subprocess, "run", side_effect=execute), \
                patch.object(OPS, "run") as report:
            with self.assertRaisesRegex(RuntimeError, "apache"):
                OPS.checks()
            self.assertEqual(len(calls), 6)
            self.assertEqual([command[-1] for command in calls],
                             ["beelink-job-" + job + ".service" for job in OPS.JOBS])
            report.assert_called_once()

    def test_wrong_host_does_not_change_boot_configuration(self):
        with patch.object(OPS.os, "getuid", return_value=0), \
                patch.object(OPS.socket, "gethostname", return_value="latitude"), \
                patch("sys.argv", ["operations", "--prepare-and-check"]), \
                patch.object(OPS, "prepare") as prepare:
            with self.assertRaisesRegex(RuntimeError, "directly on beelink"):
                OPS.main()
            prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
