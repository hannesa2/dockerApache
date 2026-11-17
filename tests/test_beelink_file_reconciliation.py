import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "monitoring/reconcile-latitude-files.sh"
SPEC = importlib.util.spec_from_file_location(
    "file_reconciliation", ROOT / "monitoring/check-beelink-reconciled-files.py")
FILES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FILES)


class FileReconciliationTest(unittest.TestCase):
    def test_explicit_mode_and_source_host_required(self):
        result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--sync-frozen", result.stderr)
        with tempfile.TemporaryDirectory() as temporary:
            for name, response in (("id", "0"), ("hostname", "beelink")):
                path = Path(temporary) / name
                path.write_text("#!/bin/sh\nprintf '%s\\n' '" + response + "'\n")
                path.chmod(0o755)
            result = subprocess.run(
                ["bash", str(SCRIPT), "--sync-frozen"], capture_output=True, text=True,
                env=dict(os.environ, PATH=temporary + os.pathsep + os.environ["PATH"]))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("directly on Latitude", result.stderr)

    def test_sync_preserves_adapted_configs_and_upgraded_core(self):
        source = SCRIPT.read_text()
        for expected in (
            "--exclude=/wordpress/wp-config.php", "--exclude=/wordpress/",
            "--exclude=/config/config.ini.php", "--exclude=/tmp/",
            "--dry-run --itemize-changes", "--network none", "--read-only",
            "--chown=10001:10001", "files-sync-pending.json",
        ):
            self.assertIn(expected, source)
        self.assertNotIn("--inplace --", source)
        self.assertNotIn("gitlab-backup", source)
        self.assertNotIn("systemctl stop cron", source)

    def fixture(self, root):
        transfer = root / "transfer"
        transfer.mkdir()
        runtime = root / "runtime"
        (runtime / "database-reconciliation").mkdir(parents=True)
        (runtime / "database-reconciliation/report.json").write_text("{}")
        protected = {name: {"id": name, "started": "fixed", "running": True}
                     for name in ("homeassistant", "camera-postgres-db-1",
                                  "beelink-app-test-gitlab-1")}
        (transfer / "files-sync-complete.json").write_text(json.dumps({"protected": protected}))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(FILES, "TRANSFER", transfer))
        stack.enter_context(patch.object(FILES, "REPORT", runtime / "report.json"))
        stack.enter_context(patch.object(FILES.control, "RUNTIME", runtime))
        stack.enter_context(patch.object(
            FILES.control, "container_identity",
            side_effect=lambda name: protected[name] if name in protected else
            {"id": name, "started": "fixed", "running": False}))
        stack.enter_context(patch.object(
            FILES.control, "capture",
            side_effect=lambda *args: "active" if args[0] == "systemctl" else ""))
        for module, methods in (
            (FILES.wordpress, ("relocate_links", "repair_permissions", "verify")),
            (FILES.matomo, ("repair_permissions", "verify")),
            (FILES.apps, ("start_owncloud",)),
            (FILES.artifacts, ("verify",)),
            (FILES.control, ("wait_applications", "preflight")),
        ):
            for method in methods:
                stack.enter_context(patch.object(module, method))
        stack.enter_context(patch.object(FILES.wordpress, "core_version", return_value="6.9.11"))
        run = stack.enter_context(patch.object(FILES.control, "run"))
        return transfer, run

    def test_verification_reports_success_after_all_app_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, run = self.fixture(Path(temporary))
            FILES.verify()
            report = json.loads(FILES.REPORT.read_text())
            self.assertTrue(report["isolated"])
            self.assertTrue(report["camera_reconciliation_pending"])
            FILES.artifacts.verify.assert_called_once()
            FILES.wordpress.verify.assert_called_once()
            self.assertFalse(any("beelink-app-test-gitlab-1" in call.args
                                 for call in run.call_args_list))

    def test_incomplete_transfer_blocks_all_app_startups(self):
        with tempfile.TemporaryDirectory() as temporary:
            transfer, run = self.fixture(Path(temporary))
            (transfer / "files-sync-pending.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "transfer incomplete"):
                FILES.verify()
            run.assert_not_called()
            FILES.wordpress.verify.assert_not_called()

    def test_failed_checks_leave_frontend_stopped_and_no_success_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, run = self.fixture(Path(temporary))
            FILES.matomo.verify.side_effect = RuntimeError("Matomo check failed")
            with self.assertRaisesRegex(RuntimeError, "Matomo check failed"):
                FILES.verify()
            self.assertFalse(FILES.REPORT.exists())
            run.assert_called_with("docker", "stop", "beelink-frontend-test-frontend-1")

    def test_running_sync_worker_blocks_all_mutations(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, run = self.fixture(Path(temporary))
            FILES.control.capture.side_effect = lambda *args: "sync-container"
            with self.assertRaisesRegex(RuntimeError, "File sync is still active"):
                FILES.verify()
            run.assert_not_called()
            FILES.wordpress.repair_permissions.assert_not_called()

    def test_running_affected_app_blocks_permission_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, run = self.fixture(Path(temporary))
            FILES.control.container_identity.side_effect = lambda name: {"running": True}
            with self.assertRaisesRegex(RuntimeError, "Expected app stopped"):
                FILES.verify()
            run.assert_not_called()
            FILES.wordpress.repair_permissions.assert_not_called()


if __name__ == "__main__":
    unittest.main()
