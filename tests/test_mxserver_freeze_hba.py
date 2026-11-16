from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "mxserver/build-freeze-hba.py"


class FreezeHBATest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.output = self.root / "output"
        self.original = "local all all peer\nhost all all 127.0.0.1/32 scram-sha-256\n"
        self.source.write_text(self.original)

    def run_builder(self):
        return subprocess.run([sys.executable, str(SCRIPT), str(self.source), str(self.output)],
                              text=True, capture_output=True)

    def test_rules_precede_unchanged_original(self):
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.output.read_text()
        self.assertTrue(text.endswith(self.original))
        lines = [line for line in text.splitlines() if line and not line.startswith("#")]
        self.assertEqual(lines[:4], [
            'local "MXServer" postgres peer',
            'local "MXServer" all reject',
            'host "MXServer" all 0.0.0.0/0 reject',
            'host "MXServer" all ::/0 reject',
        ])
        self.assertEqual(self.source.read_text(), self.original)

    def test_refuses_overwrite(self):
        self.output.write_text("existing")
        self.assertNotEqual(self.run_builder().returncode, 0)
        self.assertEqual(self.output.read_text(), "existing")

    def test_rejects_empty_or_already_frozen_source(self):
        self.source.write_text("")
        self.assertNotEqual(self.run_builder().returncode, 0)
        self.source.write_text("# Temporary MXServer migration freeze.\nlocal all all peer\n")
        self.assertNotEqual(self.run_builder().returncode, 0)
        self.assertFalse(self.output.exists())

    def test_source_symlink_rejected(self):
        target = self.root / "target"
        self.source.rename(target)
        self.source.symlink_to(target)
        self.assertNotEqual(self.run_builder().returncode, 0)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
