import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "standby", Path(__file__).resolve().parents[1] / "monitoring/sync-homeassistant-standby.py")
STANDBY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STANDBY)


class SnapshotTest(unittest.TestCase):
    def test_extracts_private_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "snapshot.tar"
            target = root / "config"
            target.mkdir()
            with tarfile.open(archive, "w") as tar:
                data = b"private settings"
                item = tarfile.TarInfo(".storage/core.config")
                item.size = len(data)
                tar.addfile(item, io.BytesIO(data))
            STANDBY.extract_config(archive, target)
            file = target / ".storage/core.config"
            self.assertEqual(file.read_bytes(), data)
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_refuses_paths_outside_snapshot_and_symlinks(self):
        for name, kind in (("../outside", tarfile.REGTYPE),
                           ("/absolute", tarfile.REGTYPE),
                           ("link", tarfile.SYMTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / "snapshot.tar"
                target = root / "config"
                target.mkdir()
                with tarfile.open(archive, "w") as tar:
                    item = tarfile.TarInfo(name)
                    item.type = kind
                    if kind == tarfile.SYMTYPE:
                        item.linkname = "/etc"
                    tar.addfile(item)
                with self.assertRaisesRegex(RuntimeError, "Unsafe member"):
                    STANDBY.extract_config(archive, target)


if __name__ == "__main__":
    unittest.main()
