import importlib.util
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "feature_migration", Path(__file__).resolve().parents[1] /
    "monitoring/prepare-beelink-production-features.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class BeelinkFeatureMigrationTest(unittest.TestCase):
    def test_wrong_host_does_not_prepare(self):
        with patch.object(MIGRATION.os, "getuid", return_value=0), \
                patch.object(MIGRATION.socket, "gethostname", return_value="latitude"), \
                patch("sys.argv", ["prepare", "--prepare-stopped"]), \
                patch.object(MIGRATION, "prepare") as prepare:
            with self.assertRaisesRegex(RuntimeError, "directly on beelink"):
                MIGRATION.main()
            prepare.assert_not_called()

    def test_requires_explicit_mode(self):
        with patch("sys.argv", ["prepare"]):
            with self.assertRaises(SystemExit):
                MIGRATION.main()

    def test_running_managed_service_is_rejected_before_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            runtime.chmod(0o700)
            control = Mock(RUNTIME=runtime)
            control.capture.return_value = "active"
            original_stat = Path.stat

            def root_stat(path, **kwargs):
                result = original_stat(path, **kwargs)
                if path == runtime:
                    values = list(result)
                    values[4] = 0
                    return os.stat_result(values)
                return result

            with patch.object(MIGRATION, "load", side_effect=[control, Mock(), Mock(),
                                                            Mock(), Mock()]), \
                    patch.object(Path, "stat", root_stat):
                with self.assertRaisesRegex(RuntimeError, "Stop beelink-production"):
                    MIGRATION.prepare()
            control.run.assert_not_called()
            self.assertEqual(list(runtime.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
