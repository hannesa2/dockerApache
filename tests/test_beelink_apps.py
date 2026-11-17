import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "beelink_apps", ROOT / "monitoring/start-beelink-apps.py")
APPS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APPS)


class BeelinkApplicationsTest(unittest.TestCase):
    def test_explicit_mode_required(self):
        result = subprocess.run(
            ["python3", str(ROOT / "monitoring/start-beelink-apps.py")],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--start-isolated", result.stderr)

    def test_rejects_source_host_before_reading_data(self):
        with patch.object(APPS.os, "getuid", return_value=0), \
                patch.object(APPS.socket, "gethostname", return_value="latitude"), \
                patch("sys.argv", ["start-beelink-apps", "--start-isolated"]), \
                patch.object(APPS.os, "listdir") as listdir:
            with self.assertRaisesRegex(RuntimeError, "root directly on beelink"):
                APPS.main()
            listdir.assert_not_called()

    def test_compose_uses_internal_network_without_host_ports(self):
        config = APPS.compose_config({
            "OWNCLOUD_DB_PASSWORD": "private",
            "OWNCLOUD_DB_HOST": "production",
            "OWNCLOUD_CROND_ENABLED": "true",
        })
        self.assertEqual(config["name"], "beelink-app-test")
        self.assertEqual(config["networks"]["isolated"]["name"], "beelink-staging_isolated")
        for service in config["services"].values():
            self.assertEqual(service["networks"], ["isolated"])
            self.assertEqual(service["restart"], "no")
            self.assertNotIn("privileged", service)
            self.assertNotIn("network_mode", service)
            self.assertNotIn("ports", service)
            for mount in service.get("volumes", []):
                self.assertFalse(mount["bind"]["create_host_path"])
                self.assertNotIn("docker.sock", mount["source"])
        cloud = config["services"]["owncloud-app"]
        self.assertEqual(cloud["environment"]["OWNCLOUD_CROND_ENABLED"], "false")
        self.assertEqual(cloud["environment"]["OWNCLOUD_BACKGROUND_MODE"], "webcron")
        self.assertEqual(cloud["environment"]["OWNCLOUD_LOG_FILE"], "/mnt/data/owncloud.log")
        self.assertEqual(cloud["environment"]["OWNCLOUD_DB_HOST"], "beelink-staging-owncloud-1")
        self.assertFalse(next(m for m in cloud["volumes"]
                              if m["target"] == "/mnt/data/files")["read_only"])

    def test_gitlab_side_effects_disabled(self):
        for setting in (
                "sidekiq['enable'] = false",
                "gitlab_rails['smtp_enable'] = false",
                "gitlab_rails['gitlab_email_enabled'] = false",
                "letsencrypt['enable'] = false"):
            self.assertIn(setting, APPS.GITLAB_CONFIG)

    def test_owncloud_status_requires_real_database_query(self):
        status = {"installed": True, "maintenance": False,
                  "needsDbUpgrade": False, "versionstring": "11.1.0"}
        with patch.object(APPS, "dc") as dc, patch.object(APPS, "wait_http"), \
                patch.object(APPS, "capture", return_value=json.dumps(status)), \
                patch.object(APPS, "write_json"):
            APPS.start_owncloud()
            self.assertTrue(any("user:list" in call.args for call in dc.call_args_list))
        with patch.object(APPS, "dc"), patch.object(APPS, "wait_http"), \
                patch.object(APPS, "capture",
                             return_value=json.dumps(dict(status, needsDbUpgrade=True))):
            with self.assertRaisesRegex(RuntimeError, "Unexpected ownCloud"):
                APPS.start_owncloud()

    def test_http_timeout_is_explicit(self):
        with patch.object(APPS.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 1)), \
                patch.object(APPS, "capture",
                             side_effect=["test-container", '{"Running": true}'] * 2), \
                patch.object(APPS.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "readiness timed out"):
                APPS.wait_http("owncloud-app", ["curl"], attempts=2)

    def test_exited_container_fails_immediately(self):
        with patch.object(APPS, "capture", side_effect=[
                "test-container", '{"Running": false, "ExitCode": 1}']), \
                patch.object(APPS.subprocess, "run") as run, \
                patch.object(APPS.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, r"stopped: owncloud-app \(exit 1\)"):
                APPS.wait_http("owncloud-app", ["curl"])
            run.assert_not_called()
            sleep.assert_not_called()

    def test_only_exact_old_readonly_config_is_migrated(self):
        desired = APPS.compose_config({})
        previous = json.loads(json.dumps(desired))
        previous["services"]["owncloud-app"]["volumes"][1]["read_only"] = True
        with patch.object(APPS, "capture", side_effect=["test-container", "false"]):
            APPS.upgrade_readonly_config(previous, desired)
        with patch.object(APPS, "capture", side_effect=["test-container", "true"]):
            with self.assertRaisesRegex(RuntimeError, "Stop the test"):
                APPS.upgrade_readonly_config(previous, desired)
        previous["services"]["owncloud-app"]["networks"] = ["production"]
        with self.assertRaisesRegex(RuntimeError, "configuration differs"):
            APPS.upgrade_readonly_config(previous, desired)


if __name__ == "__main__":
    unittest.main()
