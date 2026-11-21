import hashlib
import hmac
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("private_beelink", ROOT / "private/check-private.py")
PRIVATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PRIVATE)


class PrivateBeelinkTest(unittest.TestCase):
    def test_beelink_uses_live_runtime_path_and_explicit_override_wins(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(PRIVATE, "short_hostname", return_value="beelink"), \
                patch.dict(PRIVATE.os.environ, {}, clear=True):
            root = Path(temporary)
            self.assertEqual(PRIVATE.matomo_path(root, root / "cipher"),
                             PRIVATE.BEELINK_MATOMO)
            with patch.dict(PRIVATE.os.environ, MATOMO_CONFIG_PATH="/custom/config"):
                self.assertEqual(PRIVATE.matomo_path(root, root / "cipher"),
                                 Path("/custom/config"))

    def test_privilege_is_limited_to_exact_beelink_runtime_file(self):
        with patch.object(PRIVATE, "short_hostname", return_value="beelink"):
            self.assertTrue(PRIVATE.privileged_matomo(PRIVATE.BEELINK_MATOMO))
            self.assertFalse(PRIVATE.privileged_matomo(Path("/etc/shadow")))
            with patch.object(Path, "resolve", side_effect=PermissionError):
                self.assertEqual(PRIVATE.canonical(PRIVATE.BEELINK_MATOMO),
                                 PRIVATE.BEELINK_MATOMO)
        with patch.object(PRIVATE, "short_hostname", return_value="other-host"):
            self.assertFalse(PRIVATE.privileged_matomo(PRIVATE.BEELINK_MATOMO))

    def test_privileged_digest_reads_stream_without_printing_plaintext(self):
        process = Mock(stdout=io.BytesIO(b"private configuration"))
        process.wait.return_value = 0
        process.args = ["sudo", "-n", "cat", "--", str(PRIVATE.BEELINK_MATOMO)]
        with patch.object(PRIVATE, "short_hostname", return_value="beelink"), \
                patch.object(PRIVATE.subprocess, "Popen", return_value=process) as popen:
            result = PRIVATE.digest(PRIVATE.BEELINK_MATOMO, b"key")
        self.assertEqual(result, hmac.new(b"key", b"private configuration",
                                         hashlib.sha256).hexdigest())
        popen.assert_called_once_with(process.args, stdout=subprocess.PIPE)

    def test_privileged_read_failure_is_not_hashed_as_empty_success(self):
        process = Mock(stdout=io.BytesIO(b""), args=["sudo"])
        process.wait.return_value = 1
        with patch.object(PRIVATE, "short_hostname", return_value="beelink"), \
                patch.object(PRIVATE.subprocess, "Popen", return_value=process):
            with self.assertRaises(subprocess.CalledProcessError):
                PRIVATE.digest(PRIVATE.BEELINK_MATOMO, b"key")


if __name__ == "__main__":
    unittest.main()
