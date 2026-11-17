import importlib.util
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "beelink_matomo", Path(__file__).resolve().parents[1] / "monitoring/start-beelink-matomo.py")
MATOMO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MATOMO)


class BeelinkMatomoTest(unittest.TestCase):
    def test_repairs_traversal_without_exposing_credentials_or_following_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "www"
            config = root / "piwik/config/config.ini.php"
            config.parent.mkdir(parents=True)
            config.write_text("private fixture")
            outside = Path(temporary) / "outside"
            outside.write_text("untouched")
            outside.chmod(0o600)
            (root / "link").symlink_to(outside)
            with patch.object(MATOMO.os, "chown") as chown:
                MATOMO.repair_permissions(root)
            for path in (root, root / "piwik", config.parent):
                self.assertEqual(path.stat().st_mode & 0o777, 0o750)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.read_text(), "private fixture")
            self.assertFalse(any(str(c.args[0]).endswith("/link") for c in chown.call_args_list))

    def test_patch_preserves_credentials_and_repeated_options(self):
        source = ('; <?php exit; ?>\n[database]\nhost = "127.0.0.1"\n'
                  'dbname = "piwik_db"\npassword = "fixture"\n'
                  '[General]\ntrusted_hosts[] = "production"\n'
                  'proxy_scheme_headers[] = "one"\nproxy_scheme_headers[] = "two"\n')
        result = MATOMO.patch_config(source)
        self.assertIn('host = "beelink-staging-native-mysql-1"', result)
        self.assertIn('password = "fixture"', result)
        self.assertNotIn('"production"', result)
        self.assertEqual(result.count("proxy_scheme_headers[]"), 2)
        self.assertEqual(MATOMO.patch_config(result), result)

    def test_rejects_wrong_database_or_ambiguous_host(self):
        for text in (
                '[database]\nhost = "db"\ndbname = "other"\n[General]\n',
                '[database]\nhost = "db"\nhost = "db2"\ndbname = "piwik_db"\n[General]\n'):
            with self.assertRaises(RuntimeError):
                MATOMO.patch_config(text)

    def test_compose_has_no_production_access(self):
        config = MATOMO.compose_config("sha256:apache")
        service = config["services"]["matomo"]
        self.assertEqual(service["restart"], "no")
        self.assertEqual(service["networks"], ["isolated"])
        self.assertNotIn("ports", service)
        self.assertNotIn("network_mode", service)
        for mount in service["volumes"]:
            self.assertTrue(mount["source"].startswith("/daten/beelink-runtime/matomo/"))
            self.assertFalse(mount["bind"]["create_host_path"])

    def test_test_account_only_changes_database_section(self):
        source = ('[database]\nhost = "localhost"\ndbname = "piwik_db"\n'
                  'username = "piwik"\npassword = "original"\n[General]\n')
        result = MATOMO.patch_config(source, "matomo_test_123", "new_secret")
        self.assertIn('username = "matomo_test_123"', result)
        self.assertIn('password = "new_secret"', result)
        with self.assertRaises(RuntimeError):
            MATOMO.patch_config(source, "invalid'account", "secret")


if __name__ == "__main__":
    unittest.main()
