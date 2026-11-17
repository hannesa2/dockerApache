import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "beelink_wordpress", Path(__file__).resolve().parents[1] /
    "monitoring/start-beelink-wordpress.py")
WP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WP)
CONFIG = """<?php
define('DB_NAME', 'wordpress');
define('DB_HOST', 'localhost');
define('DB_USER', 'source');
define('DB_PASSWORD', 'old\\'password');
define('WP_HOME', 'https://dev.mxtracks.info/wordpress');
$table_prefix = 'wp_';
require_once ABSPATH . 'wp-settings.php';
"""


class BeelinkWordPressTest(unittest.TestCase):
    def test_patch_preserves_original_urls_and_disables_scheduled_activity(self):
        result = WP.patch_config(CONFIG, "wordpress", "wp_test_123", "secret123")
        for key in ("DISABLE_WP_CRON", "AUTOMATIC_UPDATER_DISABLED", "WP_HTTP_BLOCK_EXTERNAL"):
            self.assertIn(f"define('{key}', true);", result)
        self.assertIn("define('WP_ENVIRONMENT_TYPE', 'staging');", result)
        self.assertIn("define('DB_HOST', 'beelink-databases-native-mysql-1');", result)
        self.assertIn("define('DB_USER', 'wp_test_123');", result)
        self.assertIn("define('DB_PASSWORD', 'secret123');", result)
        self.assertIn("define('WP_HOME', 'https://dev.mxtracks.info/wordpress');", result)
        self.assertIn("$table_prefix = 'wp_';", result)
        self.assertNotIn("old\\'password", result)
        self.assertLess(result.index("DISABLE_WP_CRON"), result.index("wp-settings.php"))

    def test_existing_staging_constants_are_replaced(self):
        source = CONFIG.replace("<?php", "<?php\ndefine('DISABLE_WP_CRON', false);")
        result = WP.patch_config(source, "wordpress", "wp_test_123", "secret")
        self.assertEqual(result.count("define('DISABLE_WP_CRON'"), 1)
        self.assertIn("define('DISABLE_WP_CRON', true);", result)

    def test_repairs_known_source_typo_only_in_returned_copy(self):
        source = CONFIG.replace("<?php\n", "<?php\nd/** source header */\n", 1)
        result = WP.patch_config(source, "wordpress", "wp_test_123", "secret")
        self.assertIn("d/**", source)
        self.assertNotIn("d/**", result)
        self.assertIn("/** source header */", result)
        self.assertEqual(WP.patch_config(source.replace("\n", "\r\n"),
                                         "wordpress", "wp_test_123", "secret"), result)

    def test_unexpected_or_ambiguous_configs_rejected(self):
        for source in (
                CONFIG.replace("'wordpress'", "'unexpected'"),
                CONFIG + "\ndefine('DB_HOST', 'another');",
                CONFIG.replace("'localhost'", "getenv('DB_HOST')"),
                CONFIG.replace("<?php", ""),
                CONFIG + "\ndefine('DISABLE_WP_CRON', getenv('CRON'));"):
            with self.assertRaises(RuntimeError):
                WP.patch_config(source, "wordpress", "wp_test_123", "secret")
        with self.assertRaises(RuntimeError):
            WP.patch_config(CONFIG, "wordpress", "bad'account", "secret")

    def test_compose_uses_internal_docker_and_only_private_working_copies(self):
        config = WP.compose_config("sha256:apache")
        service = config["services"]["apache"]
        self.assertEqual(service["image"], "sha256:apache")
        self.assertEqual(service["restart"], "no")
        self.assertEqual(service["networks"], ["isolated"])
        for key in ("ports", "network_mode", "privileged"):
            self.assertNotIn(key, service)
        for mount in service["volumes"]:
            self.assertTrue(mount["source"].startswith("/daten/beelink-runtime/wordpress/"))
            self.assertFalse(mount["bind"]["create_host_path"])

    def test_original_vhosts_not_copy_or_mirror_domains(self):
        for domain, subdir, schema in WP.SITES:
            config = WP.vhost(domain, subdir)
            self.assertIn("ServerName " + domain, config)
            self.assertIn("DocumentRoot /var/www/html/" + subdir, config)
            self.assertIn('RequestHeader set X-Forwarded-Proto "https"', config)
            self.assertNotIn("ProxyPass", config)
            self.assertNotIn("SSLEngine", config)
        with self.assertRaises(RuntimeError):
            WP.vhost("wwwcopy.mxtracks.info", "mxdocs")

    def test_relocates_query_monitor_link_without_touching_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "site"
            target = root / "wordpress/wp-content/plugins/query-monitor/wp-content/db.php"
            target.parent.mkdir(parents=True)
            target.write_text("fixture")
            link = root / "wordpress/wp-content/db.php"
            original = Path("/srv/www/vhosts/dev.mxtracks")
            link.symlink_to(original / target.relative_to(root))
            WP.relocate_links(root, original)
            self.assertFalse(Path(link.readlink()).is_absolute())
            self.assertEqual(link.resolve(), target.resolve())
            WP.relocate_links(root, original)

    def test_rejects_external_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "site"
            root.mkdir()
            (root / "link").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(RuntimeError, "External"):
                WP.relocate_links(root, Path("/srv/www/vhosts/dev.mxtracks"))

    def test_www_http_urls_are_normalized_only_in_copied_config(self):
        source = CONFIG.replace("'wordpress');", "'wordpress_mxtracks');").replace(
            "https://dev.mxtracks.info/wordpress", "http://www.mxtracks.info/wordpress")
        result = WP.patch_config(source, "wordpress_mxtracks", "wp_test_123", "secret")
        self.assertIn("define('WP_HOME', 'https://www.mxtracks.info/wordpress');", result)
        self.assertIn("define('WP_SITEURL', 'https://www.mxtracks.info/wordpress');", result)
        with self.assertRaisesRegex(RuntimeError, "Unexpected original WordPress URL"):
            WP.patch_config(CONFIG.replace("dev.mxtracks.info", "wwwcopy.mxtracks.info"),
                            "wordpress", "wp_test_123", "secret")

    def test_permissions_keep_credentials_private_and_do_not_follow_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "site"
            root.mkdir()
            config = root / "wp-config.php"
            config.write_text("fixture")
            public = root / "index.php"
            public.touch()
            outside = Path(temporary) / "outside"
            outside.touch()
            outside.chmod(0o600)
            (root / "link").symlink_to(outside)
            with patch.object(WP.os, "chown"):
                WP.repair_permissions(root)
            self.assertEqual(root.stat().st_mode & 0o777, 0o750)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(public.stat().st_mode & 0o777, 0o640)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o600)

    def test_reads_version_without_executing_old_php(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "wp-includes").mkdir()
            version = root / "wp-includes/version.php"
            version.write_text("<?php\n$wp_version = '5.0.25';\n")
            self.assertEqual(WP.core_version(root), "5.0.25")
            version.write_text("<?php\n")
            with self.assertRaises(RuntimeError):
                WP.core_version(root)

    def test_upgrade_is_noop_for_matching_version_and_rejects_unknown(self):
        with patch.object(WP, "core_version", return_value=WP.WWW_VERSION), \
                patch.object(WP.apps, "run") as run:
            WP.upgrade_www_core("image")
            run.assert_not_called()
        with patch.object(WP, "core_version", return_value="unexpected"):
            with self.assertRaisesRegex(RuntimeError, "Unexpected original www"):
                WP.upgrade_www_core("image")

    def test_upgrade_backs_up_before_replacing_core_and_preserves_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            target = work / "www/mxdocs/wordpress"
            (target / "wp-includes").mkdir(parents=True)
            (target / "wp-includes/version.php").write_text("<?php\n$wp_version = '5.0.25';\n")
            (target / "wp-content").mkdir()
            (target / "wp-content/custom.php").write_text("original plugin")
            (target / "wp-config.php").write_text("private credentials")
            staging = work / ("core-" + WP.WWW_VERSION)
            (staging / "wp-includes").mkdir(parents=True)
            (staging / "wp-includes/version.php").write_text(
                "<?php\n$wp_version = '" + WP.WWW_VERSION + "';\n")
            (staging / "index.php").write_text("updated core")
            with patch.object(WP, "WORK", work), \
                    patch.object(WP, "core_version", side_effect=["5.0.25", WP.WWW_VERSION,
                                                                WP.WWW_VERSION]), \
                    patch.object(WP.apps, "run") as run:
                WP.upgrade_www_core("image")
            commands = [call.args for call in run.call_args_list]
            self.assertIn("verify-checksums", commands[1])
            self.assertEqual(commands[2], (*WP.COMPOSE, "stop"))
            self.assertIn("mysqldump", commands[3])
            self.assertTrue(all(command[0] == "rsync" for command in commands[4:]))
            backup = work / ("www-before-" + WP.WWW_VERSION)
            self.assertTrue((backup / "backup-complete").is_file())
            self.assertEqual((backup / "wordpress/wp-config.php").read_text(),
                             "private credentials")
            self.assertEqual((target / "wp-config.php").read_text(), "private credentials")
            self.assertEqual((target / "wp-content/custom.php").read_text(), "original plugin")
            self.assertEqual((target / "index.php").read_text(), "updated core")


if __name__ == "__main__":
    unittest.main()
