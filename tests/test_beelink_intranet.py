import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "intranet", Path(__file__).resolve().parents[1] / "monitoring/install-beelink-intranet.py")
PAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PAGE)


class IntranetTest(unittest.TestCase):
    def test_worker_traversal_preserves_private_directory_listing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            path.chmod(0o700)
            with patch.object(PAGE.os, "chown") as chown:
                previous = PAGE.allow_apache_traversal(path)
            self.assertEqual(previous.st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o710)
            chown.assert_called_once_with(path, previous.st_uid, 33)

    def test_all_requested_links_are_present(self):
        for url in ("http://192.168.178.142:8080/",
                    "http://192.168.178.142:9990/console/",
                    "http://192.168.178.142:8081/MXServer/",
                    "https://192.168.178.142:4848/"):
            self.assertIn(f'href="{url}"', PAGE.HTML)
        self.assertIn("ServerName beelink\n", PAGE.CONFIG)
        self.assertNotIn("ServerAlias *", PAGE.CONFIG)


if __name__ == "__main__":
    unittest.main()
