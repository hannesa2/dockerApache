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
        self.assertEqual(PAGE.HTML.count("<table>"), 1)
        self.assertIn("beelink &ndash; Einstiegspunkt und Kommentar", PAGE.HTML)
        self.assertIn("mac2016 &ndash; Einstiegspunkt und Kommentar", PAGE.HTML)
        for rows in PAGE.INVENTORY.values():
            for name, url, _note in rows:
                self.assertIn(name, PAGE.HTML)
                if url:
                    for item in (url,) if isinstance(url, str) else url:
                        self.assertIn(f'href="{item}"', PAGE.HTML)
        owncloud_row = next(row for row in PAGE.HTML.splitlines()
                            if '<th scope="row">ownCloud</th>' in row)
        self.assertIn("https://www.mxtracks.info/owncloud/", owncloud_row)
        self.assertIn("https://www16.mxtracks.info:9443/owncloud/", owncloud_row)
        mx_row = next(row for row in PAGE.HTML.splitlines()
                      if '<th scope="row">MXServer / GlassFish</th>' in row)
        self.assertIn("https://www.mxtracks.info/backend/MXServer/rest/tracks/", mx_row)
        self.assertIn("https://www16.mxtracks.info:8443/backend/MXServer/rest/tracks/", mx_row)

    def test_update_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "intranet"
            directory.mkdir()
            page = directory / "index.html"
            page.write_text(PAGE.LEGACY_HTML)
            config = root / "zz-intranet.conf"
            config.write_text(PAGE.CONFIG)
            with patch.object(PAGE, "verify_page") as verify:
                PAGE.update_page(directory, config)
                self.assertEqual(page.read_text(encoding="utf-8"), PAGE.HTML)
                self.assertEqual(page.stat().st_mode & 0o777, 0o644)
                PAGE.update_page(directory, config)
                self.assertEqual(verify.call_count, 2)

    def test_update_rolls_back_on_verification_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "intranet"
            directory.mkdir()
            page = directory / "index.html"
            page.write_text(PAGE.LEGACY_HTML)
            config = root / "zz-intranet.conf"
            config.write_text(PAGE.CONFIG)
            with patch.object(PAGE, "verify_page", side_effect=RuntimeError("bad response")):
                with self.assertRaisesRegex(RuntimeError, "bad response"):
                    PAGE.update_page(directory, config)
            self.assertEqual(page.read_text(), PAGE.LEGACY_HTML)
            self.assertEqual(list(directory.iterdir()), [page])

    def test_update_refuses_unrecognized_page_or_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "intranet"
            directory.mkdir()
            page = directory / "index.html"
            config = root / "zz-intranet.conf"
            page.write_text("custom page")
            config.write_text(PAGE.CONFIG)
            with self.assertRaisesRegex(RuntimeError, "page differs"):
                PAGE.update_page(directory, config)
            self.assertEqual(page.read_text(), "custom page")
            page.write_text(PAGE.LEGACY_HTML)
            config.write_text("custom config")
            with self.assertRaisesRegex(RuntimeError, "configuration differs"):
                PAGE.update_page(directory, config)
            self.assertEqual(page.read_text(), PAGE.LEGACY_HTML)


if __name__ == "__main__":
    unittest.main()
