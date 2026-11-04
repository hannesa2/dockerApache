#!/usr/bin/env python3
import os
from pathlib import Path
import pty
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "buildSrc/scripts/decrypt.sh"


class PrivateConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "gnupg"
        self.home.mkdir(mode=0o700)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        date = self.bin / "date"
        date.write_text('#!/bin/sh\n[ "$1" = +%y%m%d-%H%M ] || exit 1\nprintf "261115-1120\\n"\n')
        date.chmod(0o700)
        self.env = os.environ.copy()
        self.env.update(GNUPGHOME=str(self.home), CRYPT_PASS="test passphrase",
                        CRYPT_PASS_CONFIRM="test passphrase",
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"])

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, *args, env=None, terminal_input=None):
        if terminal_input is None:
            return subprocess.run(
                ["bash", str(SCRIPT), *map(str, args)], cwd=self.root,
                env=env or self.env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True,
            )
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                ["bash", str(SCRIPT), *map(str, args)], cwd=self.root,
                env=env or self.env, stdin=slave, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            os.close(slave)
            slave = -1
            os.write(master, terminal_input.encode())
            stdout, stderr = process.communicate(timeout=30)
            return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        finally:
            os.close(master)
            if slave != -1:
                os.close(slave)

    def encrypt(self, source, output):
        result = self.run_script("--crypt", "--output", output, source)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_round_trip_and_wrong_password(self):
        source = self.root / "source with spaces"
        source.write_bytes(b"private data\0with binary\n")
        ciphertext = self.root / "snapshot.enc"
        self.encrypt(source, ciphertext)
        self.assertEqual(ciphertext.stat().st_mode & 0o777, 0o600)
        output = self.root / "restored"
        wrong_env = dict(self.env, CRYPT_PASS="wrong")
        result = self.run_script("--output", output, self.root / "snapshot", env=wrong_env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        result = self.run_script("--output", output, self.root / "snapshot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_bytes(), source.read_bytes())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_tampering_is_rejected(self):
        source = self.root / "source"
        source.write_bytes(b"private config\n")
        ciphertext = self.root / "snapshot.enc"
        self.encrypt(source, ciphertext)
        contents = bytearray(ciphertext.read_bytes())
        contents[-1] ^= 1
        ciphertext.write_bytes(contents)
        output = self.root / "restored"
        result = self.run_script("--output", output, self.root / "snapshot")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".decrypt-tmp.*")), [])

    def test_passphrase_mismatch_leaves_no_output(self):
        source = self.root / "source"
        source.write_text("data")
        output = self.root / "snapshot.enc"
        result = self.run_script(
            "--crypt", "--output", output, source,
            env=dict(self.env, CRYPT_PASS_CONFIRM="different"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_overwrite_requires_confirmation(self):
        source = self.root / "source"
        source.write_text("new")
        output = self.root / "snapshot.enc"
        output.write_text("existing")
        for answer in (None, "n\n"):
            result = self.run_script("--crypt", "--output", output, source,
                                     terminal_input=answer)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(), "existing")
        result = self.run_script("--crypt", "--output", output, source, terminal_input="y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(output.read_text(errors="replace"), "existing")

    def test_existing_plaintext_and_symlinks_are_protected(self):
        source = self.root / "source"
        source.write_text("new")
        self.encrypt(source, self.root / "snapshot.enc")
        output = self.root / "restored"
        output.write_text("original")
        result = self.run_script("--output", output, self.root / "snapshot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(), "new")
        backup = self.root / "restored.261115-1120"
        self.assertEqual(backup.read_text(), "original")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        output.write_text("changed again")
        result = self.run_script("--output", output, self.root / "snapshot")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_text(), "changed again")
        self.assertEqual(backup.read_text(), "original")
        output.unlink()
        output.symlink_to(source)
        result = self.run_script("--output", output, self.root / "snapshot",
                                 terminal_input="y\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(source.read_text(), "new")

    def test_identical_plaintext_is_left_unchanged(self):
        source = self.root / "source"
        source.write_text("data")
        self.encrypt(source, self.root / "snapshot.enc")
        source.chmod(0o640)
        inode = source.stat().st_ino
        backup = self.root / "source.261115-1120"
        backup.write_text("earlier backup")
        result = self.run_script("--output", source, self.root / "snapshot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(source.stat().st_ino, inode)
        self.assertEqual(source.stat().st_mode & 0o777, 0o640)
        self.assertEqual(backup.read_text(), "earlier backup")
        backup.unlink()
        result = self.run_script("--output", source, self.root / "snapshot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(backup.exists())

    def test_failed_decrypt_and_comparison_keep_original(self):
        source = self.root / "source"
        source.write_text("encrypted")
        self.encrypt(source, self.root / "snapshot.enc")
        source.write_text("current")
        result = self.run_script("--output", source, self.root / "snapshot",
                                 env=dict(self.env, CRYPT_PASS="wrong"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(source.read_text(), "current")
        self.assertFalse((self.root / "source.261115-1120").exists())
        cmp_command = self.bin / "cmp"
        cmp_command.write_text("#!/bin/sh\nexit 2\n")
        cmp_command.chmod(0o700)
        result = self.run_script("--output", source, self.root / "snapshot")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(source.read_text(), "current")
        self.assertFalse((self.root / "source.261115-1120").exists())
        self.assertEqual(list(self.root.glob(".decrypt-tmp.*")), [])


if __name__ == "__main__":
    unittest.main()
