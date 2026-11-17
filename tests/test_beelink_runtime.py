import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "monitoring/prepare-beelink-runtime.sh"


class BeelinkRuntimeTest(unittest.TestCase):
    def test_requires_explicit_mode(self):
        result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--prepare-isolated", result.stderr)

    def test_checks_host_before_accessing_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            for name, response in (("id", "0"), ("hostname", "latitude")):
                executable = path / name
                executable.write_text("#!/bin/sh\nprintf '%s\\n' '" + response + "'\n")
                executable.chmod(0o755)
            result = subprocess.run(
                ["bash", str(SCRIPT), "--prepare-isolated"],
                env=dict(os.environ, PATH=temporary + os.pathsep + os.environ["PATH"]),
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("directly on beelink", result.stderr)

    @unittest.skipUnless(hasattr(os, "setxattr") and shutil.which("rsync"),
                         "Requires rsync and Linux user xattrs")
    def test_in_place_fake_super_preserves_contents_and_inode(self):
        if os.uname().sysname != "Linux":
            self.skipTest("Linux rsync fake-super metadata")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "subdir").mkdir()
            file = path / "subdir/file"
            file.write_bytes(b"untouched application file")
            file.chmod(0o600)
            inode = file.stat().st_ino
            os.link(file, path / "hardlink")
            os.symlink("/outside/must-not-be-followed", path / "symlink")
            os.setxattr(file, "user.rsync.%stat",
                        f"100640 0,0 {os.getuid()}:{os.getgid()}".encode())
            os.setxattr(file, "user.application", b"preserved")
            subprocess.run(
                ["rsync", "-aHAX", "--fake-super", "-M--super", str(path) + "/", str(path) + "/"],
                check=True, capture_output=True,
            )
            self.assertEqual(file.read_bytes(), b"untouched application file")
            self.assertEqual(file.stat().st_ino, inode)
            self.assertEqual((path / "hardlink").stat().st_ino, inode)
            self.assertEqual(file.stat().st_mode & 0o777, 0o640)
            self.assertNotIn("user.rsync.%stat", os.listxattr(file))
            self.assertEqual(os.getxattr(file, "user.application"), b"preserved")
            self.assertEqual(os.readlink(path / "symlink"), "/outside/must-not-be-followed")


if __name__ == "__main__":
    unittest.main()
