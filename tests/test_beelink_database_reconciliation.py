import gzip
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location(
    "database_reconciliation", Path(__file__).resolve().parents[1] /
    "monitoring/reconcile-beelink-databases.py")
DB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DB)


class DatabaseReconciliationTest(unittest.TestCase):
    def test_filter_preserves_application_sql_but_not_system_credentials(self):
        source = b"""-- session setup
SET NAMES utf8mb4;
-- Current Database: `mysql`
USE `mysql`;
CREATE TABLE `user` (name text);
INSERT INTO `user` VALUES ('source-root');
-- Current Database: `wordpress`
CREATE DATABASE `wordpress`;
USE `wordpress`;
CREATE TABLE `posts` (id int);
INSERT INTO `posts` VALUES (1);
-- Current Database: `sys`
USE `sys`;
CREATE VIEW `system_view` AS SELECT 1;
-- Dump completed on today
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dump.gz"
            path.write_bytes(gzip.compress(source))
            result = b"".join(DB.application_sql(path, {"wordpress"}))
        self.assertIn(b"SET NAMES", result)
        self.assertIn(b"INSERT INTO `posts` VALUES (1)", result)
        self.assertNotIn(b"source-root", result)
        self.assertNotIn(b"USE `mysql`", result)
        self.assertNotIn(b"system_view", result)

    def test_filter_rejects_unexpected_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dump.gz"
            path.write_bytes(gzip.compress(b"-- Current Database: `unexpected`\n"))
            with self.assertRaisesRegex(RuntimeError, "Unexpected MySQL schema"):
                list(DB.application_sql(path, {"wordpress"}))

    def test_mysql_import_drops_only_expected_apps_and_keeps_system_schema(self):
        process = MagicMock()
        process.wait.return_value = 0
        tables = {name: {"posts"} for name in DB.SCHEMAS["apache-mysql"]}
        with patch.object(DB.restore, "mysql_schemas",
                          return_value=["mysql", "wordpress", "wordpress_mxtracks"]), \
                patch.object(DB.restore, "mysql_tables", return_value=tables), \
                patch.object(DB, "application_sql", return_value=iter([b"application SQL\n"])), \
                patch.object(DB, "run") as run, \
                patch.object(DB.subprocess, "Popen", return_value=process), \
                patch.object(DB.subprocess, "check_output",
                             return_value="wordpress\tposts\nwordpress_mxtracks\tposts\n"):
            result = DB.restore_mysql("apache-mysql", Path("fixture"))
        self.assertEqual(result, {"wordpress": 1, "wordpress_mxtracks": 1})
        sql = run.call_args.kwargs["input"]
        self.assertNotIn("`mysql`", sql)
        self.assertIn("DROP DATABASE IF EXISTS `wordpress`", sql)
        process.stdin.write.assert_called_once_with(b"application SQL\n")

    def test_mysql_failure_is_not_reported_as_success(self):
        process = MagicMock()
        process.wait.return_value = 1
        with patch.object(DB.restore, "mysql_schemas",
                          return_value=["mysql", "wordpress", "wordpress_mxtracks"]), \
                patch.object(DB.restore, "mysql_tables", return_value={}), \
                patch.object(DB, "application_sql", return_value=iter([b"SQL"])), \
                patch.object(DB, "run"), \
                patch.object(DB.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "restore failed"):
                DB.restore_mysql("apache-mysql", Path("fixture"))

    def test_postgres_restore_targets_only_isolated_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.dump"
            path.write_bytes(b"fixture")
            with patch.object(DB, "run") as run, \
                    patch.object(DB.subprocess, "check_output", side_effect=["listing", "12"]), \
                    patch.object(DB.restore, "postgres_table_count", return_value=12):
                self.assertEqual(DB.restore_postgres("owncloud", "owncloud_stage", path), 12)
            commands = [call.args for call in run.call_args_list]
            self.assertTrue(all("beelink-databases-owncloud-1" in command for command in commands))
            self.assertIn("--exit-on-error", commands[1])
            self.assertIn("--create", commands[1])

    def test_wrong_host_exits_before_mutation(self):
        with patch.object(DB.os, "getuid", return_value=0), \
                patch.object(DB.socket, "gethostname", return_value="Latitude"), \
                patch("sys.argv", ["reconcile", "--snapshots", "/unused"]):
            with self.assertRaisesRegex(RuntimeError, "directly on beelink"):
                DB.main()

    def test_affected_apps_exclude_gitlab_and_existing_cameras(self):
        self.assertNotIn("beelink-apps-gitlab-1", DB.APP_CONTAINERS)
        self.assertNotIn("camera-postgres-db-1", DB.APP_CONTAINERS)
        self.assertNotIn("homeassistant", DB.APP_CONTAINERS)
        self.assertNotIn("beelink-databases-cameras-1", DB.APP_CONTAINERS)


if __name__ == "__main__":
    unittest.main()
