import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "artifact_mirror", Path(__file__).resolve().parents[1] / "github-artifacts/sync-beelink.py")
MIRROR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIRROR)


class ArtifactMirrorTest(unittest.TestCase):
    def test_extracts_regular_file_and_preserves_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "files.tar"
            target = root / "files"
            target.mkdir()
            with tarfile.open(archive, "w") as tar:
                item = tarfile.TarInfo("artifact.zip")
                item.size = 4
                item.mtime = 1234567890
                tar.addfile(item, io.BytesIO(b"data"))
            self.assertEqual(MIRROR.extract(archive, target), 1)
            self.assertEqual((target / "artifact.zip").read_bytes(), b"data")
            self.assertEqual(int((target / "artifact.zip").stat().st_mtime), 1234567890)

    def test_rejects_empty_and_unsafe_archives(self):
        for name, kind in ((None, None), ("../escape", tarfile.REGTYPE),
                           ("/absolute", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / "files.tar"
                target = root / "files"
                target.mkdir()
                with tarfile.open(archive, "w") as tar:
                    if name:
                        item = tarfile.TarInfo(name)
                        item.type = kind
                        item.linkname = "/etc" if kind == tarfile.SYMTYPE else ""
                        tar.addfile(item)
                with self.assertRaises(RuntimeError):
                    MIRROR.extract(archive, target)


if __name__ == "__main__":
    unittest.main()
