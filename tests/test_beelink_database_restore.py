import gzip
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "beelink_restore", ROOT / "monitoring/restore-beelink-databases.py")
RESTORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESTORE)


class BeelinkDatabaseRestoreTest(unittest.TestCase):
    def test_isolated_compose_preserves_existing_services(self):
        config = RESTORE.compose_config(
            Path("/daten/beelink-staging"), "pg-secret",
            {"apache-mysql": "apache-secret", "native-mysql": "native-secret"},
            {"apache-mysql": "sha256:source-image", "native-mysql": "mysql:8.0.46"},
        )
        self.assertEqual(config["name"], "beelink-staging")
        self.assertEqual(config["networks"], {"isolated": {"internal": True}})
        self.assertEqual(len(config["services"]), 5)
        for service in config["services"].values():
            self.assertNotIn("ports", service)
            self.assertNotIn("container_name", service)
            self.assertNotIn("network_mode", service)
            self.assertEqual(service["networks"], ["isolated"])
            self.assertEqual(service["restart"], "no")
            for mount in service["volumes"]:
                self.assertTrue(mount["source"].startswith("/daten/beelink-staging/"))
                self.assertFalse(mount["bind"]["create_host_path"])
        self.assertEqual(config["services"]["native-mysql"]["image"], "mysql:8.0.46")
        self.assertEqual(config["services"]["apache-mysql"]["image"], "sha256:source-image")
        self.assertEqual(config["services"]["native-mysql"]["command"], ["--event-scheduler=OFF"])

    def test_schema_table_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dump.sql.gz"
            with gzip.open(path, "wt") as stream:
                stream.write(
                    "-- Current Database: `mysql`\nCREATE TABLE IF NOT EXISTS `user` (\n"
                    "-- Current Database: `wordpress`\nCREATE TABLE `wp_options` (\n"
                    "CREATE TABLE `wp_posts` (\n")
            self.assertEqual(RESTORE.mysql_schemas(path), ["mysql", "wordpress"])
            self.assertEqual(RESTORE.mysql_tables(path),
                             {"mysql": {"user"}, "wordpress": {"wp_options", "wp_posts"}})

    def test_checksum_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "db.dump").write_bytes(b"test")
            (path / "SHA256SUMS").write_text(
                RESTORE.hashlib.sha256(b"test").hexdigest() + "  ./db.dump\n")
            RESTORE.verify_checksums(path)
            (path / "db.dump").write_bytes(b"corrupted")
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                RESTORE.verify_checksums(path)
            (path / "SHA256SUMS").write_text("0" * 64 + "  ../secret\n")
            with self.assertRaisesRegex(RuntimeError, "Unexpected"):
                RESTORE.verify_checksums(path)

    def test_postgres_table_count_excludes_table_data(self):
        listing = (
            "217; 1259 16393 TABLE public xslfile hannes\n"
            "219; 1259 16399 TABLE public access hannes\n"
            "3760; 0 16393 TABLE DATA public xslfile hannes\n"
            "100; 0 1 SEQUENCE public sequence hannes\n")
        self.assertEqual(RESTORE.postgres_table_count(listing), 2)

    def test_wrong_host_does_not_create_directories(self):
        with patch.object(RESTORE.socket, "gethostname", return_value="latitude"), \
                patch("sys.argv", ["restore", "--restore-isolated"]), \
                patch.object(RESTORE.os, "listdir") as listdir:
            with self.assertRaisesRegex(RuntimeError, "directly on beelink"):
                RESTORE.main()
            listdir.assert_not_called()


if __name__ == "__main__":
    unittest.main()
