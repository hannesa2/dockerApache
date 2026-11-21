import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "gitlab_update", Path(__file__).resolve().parents[1] /
    "monitoring/update-beelink-gitlab.py")
UPDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATE)


class GitlabUpdateTest(unittest.TestCase):
    def test_exact_versions_only(self):
        for value in ("latest", "19.5", "19.5.4-ce.0", "19.5.4;id", "../19.5.4"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                UPDATE.version(value)
        self.assertEqual(UPDATE.image("19.5.4"), "gitlab/gitlab-ce:19.5.4-ce.0")
        self.assertEqual(UPDATE.image("19.5.4", runner=True),
                         "gitlab/gitlab-runner:v19.5.4")

    def test_upgrade_stops_require_explicit_review_across_minor_versions(self):
        UPDATE.validate_path("19.5.3", ["19.5.4", "19.5.5"], False)
        UPDATE.validate_path("19.5.3", ["19.8.9", "19.11.7", "20.2.3"], True)
        with self.assertRaisesRegex(RuntimeError, "upgrade-path-reviewed"):
            UPDATE.validate_path("19.5.3", ["19.6.1"], False)
        for targets in (["19.5.3"], ["19.4.9"], ["19.5.4", "19.5.3"]):
            with self.subTest(targets=targets), self.assertRaisesRegex(
                    RuntimeError, "strictly increasing"):
                UPDATE.validate_path("19.5.3", targets, True)

    def test_runner_patch_versions_may_differ_but_never_downgrade(self):
        UPDATE.validate_runners("19.5.2", ["19.5.4", "19.6.1"], ["19.5.3", "19.6.0"])
        for runners in (["19.5.3"], ["19.4.3", "19.6.0"], ["19.5.1", "19.6.0"]):
            with self.subTest(runners=runners), self.assertRaises(RuntimeError):
                UPDATE.validate_runners("19.5.2", ["19.5.4", "19.6.1"], runners)

    def test_atomic_json_preserves_other_configuration_and_is_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "compose.json"
            original = {"services": {"gitlab": {"image": "old", "volumes": ["keep"]},
                                      "owncloud": {"image": "untouched"}}}
            UPDATE.write_json(path, original)
            self.assertEqual(json.loads(path.read_text()), original)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.with_name(path.name + ".update-tmp").write_text("unfinished")
            with self.assertRaisesRegex(RuntimeError, "temporary"):
                UPDATE.write_json(path, {})
            self.assertEqual(json.loads(path.read_text()), original)

    def test_json_rejects_symlink_without_modifying_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "original"
            target.write_text("unchanged")
            link = Path(temporary) / "compose"
            link.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                UPDATE.write_json(link, {})
            self.assertEqual(target.read_text(), "unchanged")

    def test_mirror_worker_cli_dispatch(self):
        with patch.object(sys, "argv", ["-", "--mirror-worker", "plan",
                                       "19.5.3", "20261121T080000Z"]), \
                patch.object(UPDATE, "mirror_worker") as worker:
            UPDATE.main()
        worker.assert_called_once_with("plan", "19.5.3", "20261121T080000Z")

    def test_mirror_lock_blocks_receiver_and_requires_owner(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(UPDATE, "MIRROR_LOCK", Path(temporary) / "lock"):
            UPDATE.mirror_worker("acquire", "19.5.3", "20261121T080000Z")
            with self.assertRaises(FileExistsError):
                UPDATE.mirror_worker("acquire", "19.5.3", "20261121T080001Z")
            with self.assertRaisesRegex(RuntimeError, "not owned"):
                UPDATE.mirror_worker("release", "19.5.3", "20261121T080001Z")
            UPDATE.mirror_worker("release", "19.5.3", "20261121T080000Z")
            self.assertFalse(UPDATE.MIRROR_LOCK.exists())

    def test_mirror_refuses_incomplete_restore(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(UPDATE, "MIRROR_LOCK", Path(temporary) / "lock"):
            (Path(temporary) / "restore-in-progress").touch()
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                UPDATE.mirror_worker("acquire", "19.5.3", "20261121T080000Z")
            self.assertTrue(UPDATE.MIRROR_LOCK.is_dir())

    def test_mirror_update_preserves_yaml_and_waits_for_target_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "lock"
            lock.mkdir()
            (lock / "updater").write_text("20261121T080000Z")
            compose = root / "docker-compose.yml"
            before = ("services:\n  gitlab:\n"
                      "    image: gitlab/gitlab-ce:19.5.3-ce.0\n"
                      "    container_name: gitlab16\n    volumes:\n"
                      "      - /original/data:/var/opt/gitlab\n")
            compose.write_text(before)
            with patch.object(UPDATE, "MIRROR_LOCK", lock), \
                    patch.object(UPDATE, "MIRROR_COMPOSE", compose), \
                    patch.object(UPDATE, "mirror_check", return_value="19.5.3"), \
                    patch.object(UPDATE, "dc", return_value=json.dumps({
                        "services": {"gitlab": {"image": UPDATE.image("19.5.4")}}})) as dc, \
                    patch.object(UPDATE, "check_gitlab") as check:
                UPDATE.mirror_worker("update", "19.5.4", "20261121T080000Z")
            self.assertEqual(compose.read_text(), before.replace("19.5.3", "19.5.4"))
            dc.assert_any_call(compose, "up", "-d", "--no-deps", "--pull", "never", "gitlab")
            check.assert_called_once_with("gitlab16", "19.5.4", wait=True)
            self.assertTrue(lock.is_dir())

    def test_mirror_backup_prepares_git_owned_workspace_and_refuses_leftovers(self):
        for contents in ("", "leftover_archive.tar"):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                lock = root / "lock"
                lock.mkdir()
                (lock / "updater").write_text("20261121T080000Z")
                with patch.object(UPDATE, "MIRROR_LOCK", lock), \
                        patch.object(UPDATE, "mirror_check"), \
                        patch.object(UPDATE, "execute",
                                     side_effect=lambda *args: contents if args[1] == "find"
                                     else "") as execute, \
                        patch.object(UPDATE, "backup") as backup, \
                        patch.object(UPDATE.shutil, "copy2"):
                    if contents:
                        with self.assertRaisesRegex(RuntimeError, "not empty"):
                            UPDATE.mirror_worker("backup", "19.5.4", "20261121T080000Z")
                        backup.assert_not_called()
                    else:
                        UPDATE.mirror_worker("backup", "19.5.4", "20261121T080000Z")
                        backup.assert_called_once()
                        self.assertEqual(backup.call_args.kwargs["backup_root"],
                                         "/gitlab-backup-archives/.sync")
                        execute.assert_any_call("gitlab16", "rmdir", "--",
                                                "/gitlab-backup-archives/.sync")
                    execute.assert_any_call("gitlab16", "install", "-d", "-o", "git",
                                            "-g", "git", "-m", "0700",
                                            "/gitlab-backup-archives/.sync")

    def test_dry_run_never_calls_update(self):
        with patch.object(sys, "argv", ["update", "--versions", "19.5.4"]), \
                patch.object(UPDATE, "Path"), \
                patch.object(UPDATE.fcntl, "flock"), \
                patch.object(UPDATE, "load_jobs") as jobs, \
                patch.object(UPDATE, "preflight", return_value="19.5.3") as preflight, \
                patch.object(UPDATE, "update") as update, \
                patch.object(UPDATE, "log"):
            UPDATE.main()
            update.assert_not_called()
            args = preflight.call_args.args[1]
            self.assertFalse(args.apply)
            self.assertEqual(args.runner_versions, ["19.5.4"])
            self.assertIs(preflight.call_args.args[0], jobs.return_value)

    def test_preflight_refuses_unfinished_update_before_touching_compose(self):
        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary) / "pending.json"
            pending.touch()
            with patch.object(UPDATE, "PENDING", pending), \
                    patch.object(UPDATE.os, "getuid", return_value=0), \
                    patch.object(UPDATE.socket, "gethostname", return_value="beelink"), \
                    patch.object(UPDATE, "command") as command:
                with self.assertRaisesRegex(RuntimeError, "Unfinished update"):
                    UPDATE.preflight(SimpleNamespace(guard=Mock()), SimpleNamespace())
                command.assert_not_called()

    def test_failed_background_migration_prevents_next_health_checks(self):
        def execute(container, *args, **kwargs):
            if args == ("gitlab-rake", "gitlab:background_migrations:wait"):
                raise RuntimeError("failed background migration")
        with patch.object(UPDATE.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0)), \
                patch.object(UPDATE, "installed", return_value="19.5.4"), \
                patch.object(UPDATE, "execute", side_effect=execute) as run:
            with self.assertRaisesRegex(RuntimeError, "failed background migration"):
                UPDATE.check_gitlab("gitlab16", "19.5.4", wait=True)
            self.assertFalse(any("gitlab:doctor:secrets" in call.args
                                 for call in run.call_args_list))

    def test_command_reports_failure_and_timeout(self):
        result = SimpleNamespace(returncode=1, stdout="failure detail", stderr="error detail")
        with patch.object(UPDATE.subprocess, "run", return_value=result), \
                patch.object(UPDATE, "log") as log:
            with self.assertRaisesRegex(RuntimeError, "Command failed"):
                UPDATE.command("docker", "pull", "unavailable")
            self.assertTrue(any("failure detail" in call.args[0]
                                for call in log.call_args_list))
        with patch.object(UPDATE.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("docker", 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                UPDATE.command("docker", timeout=1)

    def test_backup_copies_data_and_secrets_before_removing_container_archive(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(UPDATE, "installed", return_value="19.5.3"), \
                patch.object(UPDATE, "execute") as execute:
            target = Path(temporary) / "20261121T080000Z" / "19.5.4"
            UPDATE.backup("gitlab16", target, "/gitlab-backup-archives/.sync")
            calls = [call.args for call in execute.call_args_list]
            self.assertIn(("gitlab16", "gitlab-rake", "gitlab:backup:verify",
                           "BACKUP=preupdate_20261121T080000Z_19.5.3"), calls)
            self.assertIn(("gitlab16", "cat", "/gitlab-backup-archives/.sync/"
                           "preupdate_20261121T080000Z_19.5.3_gitlab_backup.tar"), calls)
            self.assertEqual(calls[-1][1:3], ("rm", "--"))
            self.assertTrue((target / "gitlab-configuration.tar").is_file())
            self.assertTrue((target / "backup-id").is_file())

    def fixture(self, temporary):
        root = Path(temporary)
        apps = root / "apps.json"
        runner = root / "runner.json"
        original = {"name": "beelink-apps", "services": {
            "gitlab": {"image": "sha256:old", "volumes": ["keep-data"]},
            "owncloud-app": {"image": "unchanged"}}}
        apps.write_text(json.dumps(original))
        runner.write_text(json.dumps({"services": {
            "runner": {"image": "gitlab/gitlab-runner:v19.5.3", "volumes": ["keep-token"]}}}))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in (("APPS", apps), ("RUNNER", runner),
                            ("BACKUPS", root / "backups"),
                            ("PENDING", root / "backups/update-in-progress.json")):
            stack.enter_context(patch.object(UPDATE, name, value))
        mocks = {}
        for name in ("require_paused_ci", "command", "remote", "backup", "check_gitlab", "dc"):
            mocks[name] = stack.enter_context(patch.object(UPDATE, name))
        mocks["backup"].side_effect = lambda container, path: path.mkdir()
        stack.enter_context(patch.object(UPDATE, "log"))
        jobs = SimpleNamespace(guard=Mock(), gitlab=Mock())
        args = SimpleNamespace(versions=["19.5.4", "19.5.5"],
                               runner_versions=["19.5.4", "19.5.5"])
        return root, original, mocks, jobs, args

    def test_success_updates_only_gitlab_and_runner_and_syncs_standby(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, original, mocks, jobs, args = self.fixture(temporary)
            UPDATE.update(jobs, args, "19.5.3")
            actual = json.loads(UPDATE.APPS.read_text())
            original["services"]["gitlab"]["image"] = UPDATE.image("19.5.5")
            self.assertEqual(actual, original)
            runner = json.loads(UPDATE.RUNNER.read_text())["services"]["runner"]
            self.assertEqual(runner["image"], UPDATE.image("19.5.5", runner=True))
            self.assertEqual(runner["volumes"], ["keep-token"])
            self.assertFalse(UPDATE.PENDING.exists())
            self.assertEqual(len(list(root.glob("backups/*/completed.json"))), 1)
            self.assertEqual([call.args[1] for call in mocks["remote"].call_args_list],
                             ["acquire", "pull", "backup", "update",
                              "pull", "backup", "update", "release"])
            jobs.gitlab.assert_called_once_with()
            for call in mocks["dc"].call_args_list:
                if "up" in call.args:
                    self.assertEqual(call.args[1:],
                                     ("up", "-d", "--no-deps", "--pull", "never", "gitlab"))

    def test_failure_retains_pending_state_and_does_not_restart_or_rollback(self):
        for failing in ("backup", "check_gitlab", "remote", "dc"):
            with self.subTest(failing=failing), tempfile.TemporaryDirectory() as temporary:
                _, _, mocks, jobs, args = self.fixture(temporary)
                mocks[failing].side_effect = RuntimeError("injected failure")
                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    UPDATE.update(jobs, args, "19.5.3")
                self.assertTrue(UPDATE.PENDING.exists())
                jobs.gitlab.assert_not_called()
                self.assertFalse(any(call.args[:2] == ("systemctl", "start")
                                     for call in mocks["command"].call_args_list))
                self.assertFalse(any(call.args[1] == "release"
                                     for call in mocks["remote"].call_args_list))

    def test_unpaused_ci_fails_before_creating_update_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, _, mocks, jobs, args = self.fixture(temporary)
            mocks["require_paused_ci"].side_effect = RuntimeError("Pause ALL runners")
            with self.assertRaisesRegex(RuntimeError, "Pause ALL"):
                UPDATE.update(jobs, args, "19.5.3")
            self.assertFalse(UPDATE.BACKUPS.exists())
            mocks["remote"].assert_not_called()
            mocks["command"].assert_not_called()

    def test_failed_final_sync_does_not_mark_update_completed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _, _, jobs, args = self.fixture(temporary)
            jobs.gitlab.side_effect = RuntimeError("sync failed")
            with self.assertRaisesRegex(RuntimeError, "sync failed"):
                UPDATE.update(jobs, args, "19.5.3")
            self.assertTrue(UPDATE.PENDING.exists())
            self.assertEqual(list(root.glob("backups/*/completed.json")), [])

    def test_runner_patch_override_is_used_for_pull_and_persistent_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, _, mocks, jobs, args = self.fixture(temporary)
            args.versions = ["19.5.5"]
            args.runner_versions = ["19.5.4"]
            UPDATE.update(jobs, args, "19.5.3")
            self.assertEqual(json.loads(UPDATE.RUNNER.read_text())[
                "services"]["runner"]["image"], "gitlab/gitlab-runner:v19.5.4")
            mocks["command"].assert_any_call("docker", "pull", "gitlab/gitlab-runner:v19.5.4")


if __name__ == "__main__":
    unittest.main()
