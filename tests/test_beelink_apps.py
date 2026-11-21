import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
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
                patch.object(APPS.socket, "gethostname", return_value="other-host"), \
                patch("sys.argv", ["start-beelink-apps", "--start-isolated"]), \
                patch.object(APPS.os, "listdir") as listdir:
            with self.assertRaisesRegex(RuntimeError, "root directly on beelink"):
                APPS.main()
            listdir.assert_not_called()

    def test_compose_publishes_only_gitlab_ssh_and_keeps_database_network_internal(self):
        config = APPS.compose_config({
            "OWNCLOUD_DB_PASSWORD": "private",
            "OWNCLOUD_DB_HOST": "production",
            "OWNCLOUD_CROND_ENABLED": "true",
        })
        self.assertEqual(config["name"], "beelink-apps")
        self.assertEqual(config["networks"]["isolated"]["name"], "beelink-databases_isolated")
        self.assertEqual(config["networks"]["egress"], {"driver": "bridge"})
        for name, service in config["services"].items():
            self.assertEqual(service["restart"], "no")
            self.assertNotIn("privileged", service)
            self.assertNotIn("network_mode", service)
            if name == "gitlab":
                self.assertEqual(set(service["networks"]), {"isolated", "egress"})
                self.assertEqual(service["ports"], [
                    {"target": 22, "published": 40022, "host_ip": "192.168.178.142",
                     "protocol": "tcp"}])
            else:
                self.assertEqual(service["networks"], ["isolated"])
                self.assertNotIn("ports", service)
            for mount in service.get("volumes", []):
                self.assertFalse(mount["bind"]["create_host_path"])
                self.assertNotIn("docker.sock", mount["source"])
        cloud = config["services"]["owncloud-app"]
        self.assertEqual(cloud["environment"]["OWNCLOUD_CROND_ENABLED"], "false")
        self.assertEqual(cloud["environment"]["OWNCLOUD_BACKGROUND_MODE"], "webcron")
        self.assertEqual(cloud["environment"]["OWNCLOUD_LOG_FILE"], "/mnt/data/owncloud.log")
        self.assertEqual(cloud["environment"]["OWNCLOUD_DB_HOST"], "beelink-databases-owncloud-1")
        self.assertFalse(next(m for m in cloud["volumes"]
                              if m["target"] == "/mnt/data/files")["read_only"])

    def test_gitlab_keeps_existing_smtp_values_and_enables_workers_mail_and_ssh(self):
        source = ("gitlab_rails['smtp_address'] = 'relay.example.invalid'\n"
                  "gitlab_rails['smtp_port'] = 587\n"
                  "gitlab_rails['smtp_user_name'] = 'gitlab'\n"
                  "gitlab_rails['smtp_password'] = 'private'\n")
        result = APPS.gitlab_config(source)
        self.assertIn(source, result)
        self.assertIn("sidekiq['enable'] = true", result)
        self.assertIn("gitlab_rails['smtp_enable'] = true", result)
        self.assertIn("gitlab_rails['gitlab_email_enabled'] = true", result)
        self.assertIn("gitlab_rails['gitlab_shell_ssh_port'] = 40022", result)
        self.assertIn("letsencrypt['enable'] = false", result)
        self.assertNotIn("gitlab_rails['smtp_tls'] = true", result)
        self.assertNotIn("private", APPS.GITLAB_CONFIG)
        implicit_tls = APPS.gitlab_config(source.replace("smtp_port'] = 587",
                                                          "smtp_port'] = 465"))
        self.assertIn("gitlab_rails['smtp_tls'] = true", implicit_tls)
        self.assertIn("gitlab_rails['smtp_enable_starttls_auto'] = false", implicit_tls)
        with self.assertRaisesRegex(RuntimeError, "SMTP relay settings are incomplete"):
            APPS.gitlab_config("gitlab_rails['smtp_enable'] = true")
        for source in (
                "# gitlab_rails['smtp_address'] = 'relay.example.invalid'\n"
                "# gitlab_rails['smtp_port'] = 587",
                "gitlab_rails['smtp_address'] = 'relay.example.invalid'\n"
                "gitlab_rails['smtp_address'] = 'other.example.invalid'\n"
                "gitlab_rails['smtp_port'] = 587",
                "gitlab_rails['smtp_address'] = 'relay.example.invalid'\n"
                "gitlab_rails['smtp_port'] = 70000"):
            with self.subTest(source=source):
                with self.assertRaises(RuntimeError):
                    APPS.gitlab_config(source)

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

    def test_only_exact_restricted_compose_is_upgraded_after_all_apps_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "apps"
            config_path = work / "gitlab/config/gitlab.rb"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(APPS.RESTRICTED_GITLAB_CONFIG)
            source_config = root / "host-configuration/srv/gitlab/config/gitlab.rb"
            source_config.parent.mkdir(parents=True)
            source_config.write_text(
                "gitlab_rails['smtp_address'] = 'relay.example.invalid'\n"
                "gitlab_rails['smtp_port'] = 587\n")
            desired = APPS.compose_config({})
            previous = json.loads(json.dumps(desired))
            previous["services"]["gitlab"].pop("ports")
            previous["services"]["gitlab"]["networks"] = ["isolated"]
            previous["networks"].pop("egress")
            compose_path = work / "compose.json"
            compose_path.parent.mkdir(parents=True, exist_ok=True)
            compose_path.write_text(json.dumps(previous))
            (work / "prepared").touch()
            compose = ["docker", "compose", "-f", str(compose_path)]
            with patch.object(APPS, "APPS", work), \
                    patch.object(APPS, "RUNTIME", root), \
                    patch.object(APPS, "ROOTFS", root / "rootfs"), \
                    patch.object(APPS, "COMPOSE", compose), \
                    patch.object(APPS, "capture", side_effect=["test-container", "false"]):
                APPS.upgrade_restricted_config(previous, desired)
            self.assertEqual(json.loads(compose_path.read_text()), desired)
            self.assertTrue((config_path.parent /
                             "gitlab.rb.before-production-features").is_file())
            self.assertIn("smtp_enable'] = true", config_path.read_text())
            with patch.object(APPS, "capture", side_effect=["test-container", "true"]), \
                    patch.object(APPS, "APPS", work), \
                    patch.object(APPS, "RUNTIME", root), \
                    patch.object(APPS, "ROOTFS", root / "rootfs"), \
                    patch.object(APPS, "COMPOSE", compose):
                with self.assertRaisesRegex(RuntimeError, "Stop every"):
                    APPS.upgrade_restricted_config(previous, desired)
            previous["services"]["owncloud-app"]["networks"] = ["production"]
            with self.assertRaisesRegex(RuntimeError, "configuration differs"):
                APPS.upgrade_restricted_config(previous, desired)


if __name__ == "__main__":
    unittest.main()
