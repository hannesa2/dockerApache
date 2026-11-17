import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "gitlab_reconciliation", Path(__file__).resolve().parents[1] /
    "monitoring/reconcile-beelink-gitlab.py")
GITLAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GITLAB)


def archive(path, members, mode="w"):
    with tarfile.open(path, mode) as stream:
        for name, value in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            stream.addfile(member, io.BytesIO(value))


class GitlabReconciliationTest(unittest.TestCase):
    def test_backup_requires_matching_checksum_and_exact_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "backup.tar"
            archive(path, {"backup_information.yml": b":gitlab_version: 19.5.3\n"})
            GITLAB.verify_backup(path, hashlib.sha256(path.read_bytes()).hexdigest())
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                GITLAB.verify_backup(path, "0" * 64)
            archive(path, {"backup_information.yml": b":gitlab_version: 19.5.2\n"})
            with self.assertRaisesRegex(RuntimeError, "version"):
                GITLAB.verify_backup(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_configuration_requires_same_secrets_and_retains_native_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secrets = root / "gitlab-secrets.json"
            secrets.write_text('{"secret":"original"}')
            path = root / "configuration.tar.gz"
            archive(path, {"gitlab.rb": b"native settings",
                           "gitlab-secrets.json": secrets.read_bytes()}, "w:gz")
            GITLAB.verify_configuration(path, secrets)
            self.assertEqual(secrets.read_text(), '{"secret":"original"}')
            secrets.write_text('{"secret":"different"}')
            with self.assertRaisesRegex(RuntimeError, "secrets differ"):
                GITLAB.verify_configuration(path, secrets)

    def test_configuration_rejects_symlink_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "configuration.tar.gz"
            with tarfile.open(path, "w:gz") as stream:
                member = tarfile.TarInfo("gitlab.rb")
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                stream.addfile(member)
            with self.assertRaisesRegex(RuntimeError, "archive member"):
                GITLAB.verify_configuration(path, Path(temporary) / "unused")

    def test_services_are_stopped_individually(self):
        with patch.object(GITLAB, "run") as run:
            GITLAB.stop_writers()
        self.assertEqual([call.args[-1] for call in run.call_args_list],
                         ["nginx", "puma", "sshd"])
        self.assertTrue(all(call.args[-2] == "stop" for call in run.call_args_list))

    def setup_reconciliation(self, root):
        work = root / "apps"
        (work / "gitlab/config").mkdir(parents=True)
        (work / "gitlab/data/backups").mkdir(parents=True)
        (work / "gitlab/config/gitlab.rb").write_text(
            "sidekiq['enable'] = false\n"
            "gitlab_rails['smtp_enable'] = false\n"
            "gitlab_rails['gitlab_email_enabled'] = false\n"
            "letsencrypt['enable'] = false\n")
        backup = root / "beelink_cutover_frozen_test_gitlab_backup.tar"
        backup.write_bytes(b"fixture")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in (("WORK", work), ("PENDING", work / "pending.json"),
                            ("REPORT", work / "report.json")):
            stack.enter_context(patch.object(GITLAB, name, value))
        stack.enter_context(patch.object(GITLAB, "verify_configuration"))
        verify = stack.enter_context(patch.object(GITLAB, "verify_backup"))
        stack.enter_context(patch.object(GITLAB, "capture", return_value="19.5.3"))
        stack.enter_context(patch.object(GITLAB.control, "preflight"))
        stack.enter_context(patch.object(
            GITLAB.control, "container_identity",
            side_effect=lambda name: {"id": name, "running": True, "started": "fixed"}))
        stack.enter_context(patch.object(
            GITLAB.subprocess, "run", return_value=type("Result", (), {"returncode": 0})()))
        run = stack.enter_context(patch.object(GITLAB, "run"))
        stop = stack.enter_context(patch.object(GITLAB, "stop_writers"))
        return backup, run, stop, verify

    def test_success_restores_existing_archive_without_creating_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            backup, run, stop, verify = self.setup_reconciliation(Path(temporary))
            GITLAB.reconcile(backup, "a" * 64)
            self.assertFalse(GITLAB.PENDING.exists())
            report = json.loads(GITLAB.REPORT.read_text())
            self.assertEqual(report["backup"], backup.name)
            self.assertTrue(report["isolated"])
            verify.assert_called_once_with(
                GITLAB.WORK / "gitlab/data/backups" / backup.name, "a" * 64)
            commands = [call.args for call in run.call_args_list]
            self.assertTrue(any("restore" in command for command in commands))
            self.assertFalse(any("create" in command for command in commands))
            stop.assert_called_once()
            run.reset_mock()
            GITLAB.reconcile(backup, "a" * 64)
            run.assert_not_called()

    def test_failure_retains_pending_marker_and_stops_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            backup, run, stop, _ = self.setup_reconciliation(Path(temporary))
            run.side_effect = RuntimeError("restore failed")
            with self.assertRaisesRegex(RuntimeError, "restore failed"):
                GITLAB.reconcile(backup, "a" * 64)
            self.assertTrue(GITLAB.PENDING.exists())
            self.assertFalse(GITLAB.REPORT.exists())
            self.assertEqual(stop.call_count, 2)

    def test_pending_marker_prevents_another_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            backup, run, _, _ = self.setup_reconciliation(Path(temporary))
            GITLAB.PENDING.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                GITLAB.reconcile(backup, "a" * 64)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
