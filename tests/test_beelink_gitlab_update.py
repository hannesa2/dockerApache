import contextlib
import importlib.util
import json
from pathlib import Path
import shutil
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
                    patch.object(UPDATE, "command", return_value='[{}]'), \
                    patch.object(UPDATE, "mirror_files", return_value=[compose]), \
                    patch.object(UPDATE, "mirror_config", return_value=(
                        {"services": {"gitlab": {"image": UPDATE.image("19.5.4"),
                         "environment": {"GITLAB_OMNIBUS_CONFIG": "unchanged"}}}},
                        False)), \
                    patch.object(UPDATE, "mirror_dc") as dc, \
                    patch.object(UPDATE, "check_gitlab") as check:
                UPDATE.mirror_worker("update", "19.5.4", "20261121T080000Z")
            self.assertEqual(compose.read_text(), before.replace("19.5.3", "19.5.4"))
            dc.assert_any_call([compose], "up", "-d", "--no-deps", "--pull", "never", "gitlab")
            check.assert_called_once_with("gitlab16", "19.5.4", wait=True)
            self.assertTrue(lock.is_dir())

    def test_mirror_saves_configuration_without_backing_up_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "lock"
            lock.mkdir()
            (lock / "updater").write_text("20261121T080000Z")
            directory = root / "backups" / "20261121T080000Z" / "19.5.4"
            original_path = UPDATE.Path
            def path(value):
                if value == "/Users/hannes/server/gitlab/backups/updates":
                    return root / "backups"
                return original_path(value)
            with patch.object(UPDATE, "MIRROR_LOCK", lock), \
                    patch.object(UPDATE, "Path", side_effect=path), \
                    patch.object(UPDATE, "mirror_check"), \
                    patch.object(UPDATE, "command", return_value='[{}]'), \
                    patch.object(UPDATE, "mirror_files", return_value=[
                        root / "docker-compose.yml", root / "docker-compose.override.yml"]), \
                    patch.object(UPDATE, "execute") as execute, \
                    patch.object(UPDATE, "backup") as backup, \
                    patch.object(UPDATE.shutil, "copy2") as copy:
                UPDATE.mirror_worker("save-config", "19.5.4", "20261121T080000Z")
            backup.assert_not_called()
            self.assertTrue((directory / "gitlab-configuration.tar").exists())
            self.assertEqual([call.args[0].name for call in copy.call_args_list],
                             ["docker-compose.yml", "docker-compose.override.yml"])
            self.assertEqual(execute.call_args.args,
                             ("gitlab16", "tar", "-cf", "-", "-C", "/etc", "gitlab"))

    def test_mirror_files_accepts_only_existing_labeled_base_and_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "docker-compose.yml"
            override = base.with_name("docker-compose.override.yml")
            base.touch()
            override.touch()
            def details(value):
                return {"Config": {"Labels": {"com.docker.compose.project.config_files": value}}}
            with patch.object(UPDATE, "MIRROR_COMPOSE", base):
                self.assertEqual(UPDATE.mirror_files(details(str(base))), [base])
                self.assertEqual(UPDATE.mirror_files(details(f"{base},{override}")), [base, override])
                with self.assertRaisesRegex(RuntimeError, "Unexpected"):
                    UPDATE.mirror_files(details(f"{base},{override},/tmp/extra.yml"))
                override.unlink()
                override.symlink_to(base)
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    UPDATE.mirror_files(details(f"{base},{override}"))

    def test_override_path_normalization_preserves_private_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            override = Path(temporary) / "docker-compose.override.yml"
            before = ('{"environment": "unrelated secret", "config": '
                      '"gitlab_rails[\\u0027backup_path\\u0027] = \\u0027ignored\\u0027"}')
            old = "gitlab_rails['backup_path'] = '/gitlab-backup-archives'"
            before += old
            override.write_text(before)
            override.chmod(0o600)
            UPDATE.normalize_mirror_override(override)
            self.assertEqual(override.read_text(), before.replace(
                old, "gitlab_rails['backup_path'] = '/gitlab-backup-archives/.sync'"))
            self.assertEqual(override.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(RuntimeError, "changed"):
                UPDATE.normalize_mirror_override(override)

    def test_mirror_config_requires_known_recoverable_backup_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "docker-compose.yml"
            override = base.with_name("docker-compose.override.yml")
            old = "gitlab_rails['backup_path'] = '/gitlab-backup-archives'"
            override.write_text(old)
            def response(value):
                return json.dumps({"services": {"gitlab": {"environment": {
                    "GITLAB_OMNIBUS_CONFIG": value}}}})
            with patch.object(UPDATE, "mirror_dc") as dc:
                dc.return_value = response(old)
                self.assertTrue(UPDATE.mirror_config([base, override])[1])
                with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                    UPDATE.mirror_config([base])
                dc.return_value = response(old.replace("/gitlab-backup-archives", "/elsewhere"))
                with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                    UPDATE.mirror_config([base, override])
                dc.return_value = response(old.replace("/gitlab-backup-archives",
                                                       UPDATE.MIRROR_BACKUP_ROOT))
                self.assertFalse(UPDATE.mirror_config([base, override])[1])

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose is not available")
    def test_mirror_update_renders_and_preserves_real_compose_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "lock"
            lock.mkdir()
            (lock / "updater").write_text("20261121T080000Z")
            base = root / "docker-compose.yml"
            base.write_text(
                "services:\n  gitlab:\n"
                "    image: gitlab/gitlab-ce:19.5.3-ce.0\n"
                "    environment:\n      GITLAB_OMNIBUS_CONFIG: |\n"
                "        gitlab_rails['backup_path'] = '/gitlab-backup-archives/.sync'\n")
            override = root / "docker-compose.override.yml"
            old = "gitlab_rails['backup_path'] = '/gitlab-backup-archives'"
            private_config = "external_url 'https://example.test'\n" + old + "\n"
            override.write_text(json.dumps({"services": {"gitlab": {"environment": {
                "GITLAB_OMNIBUS_CONFIG": private_config}}}}))
            before = override.read_text()
            override.chmod(0o600)
            up_calls = []
            def command(*args, **kwargs):
                if args[:2] == ("docker", "inspect"):
                    return "[{}]"
                if "up" in args:
                    up_calls.append(args)
                    return ""
                return subprocess.run(args, text=True, capture_output=True, check=True).stdout
            with patch.object(UPDATE, "MIRROR_LOCK", lock), \
                    patch.object(UPDATE, "MIRROR_COMPOSE", base), \
                    patch.object(UPDATE, "mirror_files", return_value=[base, override]), \
                    patch.object(UPDATE, "mirror_check"), \
                    patch.object(UPDATE, "command", side_effect=command), \
                    patch.object(UPDATE, "check_gitlab") as check:
                UPDATE.mirror_worker("update", "19.5.4", "20261121T080000Z")
            self.assertEqual(override.read_text(), before.replace(
                old, "gitlab_rails['backup_path'] = '/gitlab-backup-archives/.sync'"))
            self.assertEqual(override.stat().st_mode & 0o777, 0o600)
            self.assertEqual(up_calls[0][:6],
                             ("docker", "compose", "-f", base, "-f", override))
            check.assert_called_once_with("gitlab16", "19.5.4", wait=True)

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
        for name in ("require_paused_ci", "command", "remote", "backup", "check_gitlab",
                     "dc", "wait_runner"):
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
                             ["acquire", "pull", "save-config", "update",
                              "pull", "save-config", "update", "release"])
            jobs.gitlab.assert_called_once_with()
            mocks["wait_runner"].assert_called_once_with("19.5.5")
            self.assertEqual(mocks["backup"].call_count, 2)
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

    def test_ci_guard_checks_active_runners_and_in_progress_builds(self):
        with patch.object(UPDATE, "execute") as execute:
            UPDATE.require_paused_ci()
        container, executable, command, source = execute.call_args.args
        self.assertEqual((container, executable, command),
                         (UPDATE.CONTAINER, "gitlab-rails", "runner"))
        self.assertIn("Ci::Runner.active.exists?", source)
        self.assertIn("Ci::Build.where(status: %w[running preparing]).exists?", source)
        self.assertNotIn("where(paused:", source)

    def test_wait_runner_waits_for_matching_running_container(self):
        with patch.object(UPDATE, "command", side_effect=[
                "active", "active", "gitlab/gitlab-runner:v19.5.3",
                "active", "gitlab/gitlab-runner:v19.6.0"]), \
                patch.object(UPDATE, "dc", side_effect=["", "old-id", "new-id"]), \
                patch.object(UPDATE.time, "sleep"):
            UPDATE.wait_runner("19.6.0")

    def test_wait_runner_times_out(self):
        with patch.object(UPDATE, "command", return_value="active"), \
                patch.object(UPDATE, "dc", return_value=""), \
                patch.object(UPDATE.time, "monotonic", side_effect=[0, 121]):
            with self.assertRaisesRegex(RuntimeError, "Timed out"):
                UPDATE.wait_runner("19.6.0", timeout=120)

    def test_failed_standby_configuration_backup_prevents_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, original, mocks, jobs, args = self.fixture(temporary)
            def remote(jobs, action, target, run_id):
                if action == "save-config":
                    raise RuntimeError("standby configuration backup failed")
            mocks["remote"].side_effect = remote
            with self.assertRaisesRegex(RuntimeError, "standby configuration backup failed"):
                UPDATE.update(jobs, args, "19.5.3")
            self.assertEqual(json.loads(UPDATE.APPS.read_text()), original)
            self.assertTrue(UPDATE.PENDING.exists())
            mocks["dc"].assert_not_called()
            jobs.gitlab.assert_not_called()

    def test_runner_wait_failure_prevents_verification_and_sync(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, _, mocks, jobs, args = self.fixture(temporary)
            mocks["wait_runner"].side_effect = RuntimeError("runner not ready")
            with self.assertRaisesRegex(RuntimeError, "runner not ready"):
                UPDATE.update(jobs, args, "19.5.3")
            self.assertFalse(any("verify" in call.args for call in mocks["dc"].call_args_list))
            self.assertFalse(any(call.args[1] == "release"
                                 for call in mocks["remote"].call_args_list))
            self.assertTrue(UPDATE.PENDING.exists())
            jobs.gitlab.assert_not_called()

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
