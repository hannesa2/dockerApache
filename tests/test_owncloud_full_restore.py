import bz2
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "owncloud/test-full-restore-beelink.sh"
BLOCKS = re.findall(r"<<'PY'\n(.*?)\nPY", SCRIPT.read_text(), re.DOTALL)
IMAGES_CODE = next(code for code in BLOCKS if 'ids = {image["Id"]' in code)
PERMISSIONS_CODE = next(code for code in BLOCKS if "import bz2" in code)


class FullRecoveryTest(unittest.TestCase):
    def test_requires_matching_saved_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / "images.json"
            metadata.write_text(json.dumps([{"Id": "app"}, {"Id": "db"}]))
            for images, expected in [(("app", "db"), 0), (("other", "db"), 1)]:
                result = subprocess.run(
                    [sys.executable, "-c", IMAGES_CODE, str(metadata), *images],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)

    def permissions_case(self, missing=False, escape=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "volume/files"
            target.mkdir(parents=True)
            file = target / "sample"
            file.write_bytes(b"restored bytes")
            file.chmod(0o600)
            prefix = "/daten/beelink-runtime/rootfs/daten/owncloud/data/"
            name = "sample"
            if missing:
                name = "absent"
            if escape:
                outside = root / "outside"
                outside.mkdir()
                (outside / "sample").write_bytes(b"must not touch")
                (target / "escape").symlink_to(outside, target_is_directory=True)
                name = "escape/sample"
            metadata = root / "fileinfo.bz2"
            with bz2.open(metadata, "wb") as stream:
                stream.write(
                    f"{stat.S_IFREG | 0o640} {os.getuid()} {os.getgid()} {prefix}{name}\n".encode()
                )
            result = subprocess.run(
                [sys.executable, "-c", PERMISSIONS_CODE, str(metadata), str(root)],
                capture_output=True, text=True,
            )
            if not missing and not escape:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o640)
                self.assertEqual(file.read_bytes(), b"restored bytes")
            return result

    def test_restores_backintime_permissions(self):
        self.permissions_case()

    def test_rejects_missing_restored_file(self):
        result = self.permissions_case(missing=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is missing", result.stderr)

    def test_rejects_metadata_symlink_escape(self):
        result = self.permissions_case(escape=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("escapes", result.stderr)


if __name__ == "__main__":
    unittest.main()
