import importlib.util
import contextlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "production_names", ROOT / "monitoring/rename-beelink-production.py")
NAMES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NAMES)


class ProductionNamesTest(unittest.TestCase):
    def test_mapping_preserves_data_paths_locks_and_source_container_names(self):
        source = """
"name": "beelink-staging"
beelink-staging-owncloud-1
beelink-app-test-gitlab-1
beelink-wordpress-test-apache-1
beelink-matomo-test-matomo-1
beelink-artifacts-test-artifacts-1
beelink-frontend-test-frontend-1
beelink-dns-test-dns-1
beelink-dns-test_dns_test
"dns_test"
beelink-staging_isolated
beelink-staging.service
/daten/beelink-staging/compose.json
/usr/local/libexec/beelink-staging/control-beelink-staging.py
/run/lock/beelink-staging-control.lock
/run/lock/beelink-staging-restart-test.lock
owncloud-staging-owncloud-1
"""
        result = NAMES.renamed(source)
        for expected in (
                '"name": "beelink-databases"', "beelink-databases-owncloud-1",
                "beelink-apps-gitlab-1", "beelink-wordpress-apache-1",
                "beelink-matomo-matomo-1", "beelink-artifacts-artifacts-1",
                "beelink-frontend-frontend-1", "beelink-dns-dns-1",
                "beelink-dns_resolver", '"resolver"', "beelink-databases_isolated",
                "beelink-production.service", "/daten/beelink-staging/compose.json",
                "/usr/local/libexec/beelink-staging/control-beelink-staging.py",
                "/run/lock/beelink-staging-control.lock",
                "/run/lock/beelink-staging-restart-test.lock", "owncloud-staging-owncloud-1"):
            self.assertIn(expected, result)
        self.assertEqual(NAMES.renamed(result), result)

    def test_deployed_source_refactor_matches_installed_migration(self):
        # Regression: snippets inside shell/Java/PHP strings also need new names.
        for filename in (
                "control-beelink-staging.py", "start-beelink-apps.py",
                "start-beelink-frontend.py", "start-beelink-dns.py",
                "start-beelink-artifacts.py", "beelink-jobs.py",
                "renew-beelink-certificates.py"):
            source = (ROOT / "monitoring" / filename).read_text()
            self.assertEqual(NAMES.renamed(source), source, filename)
        controller = (ROOT / "monitoring/control-beelink-staging.py").read_text()
        self.assertIn("Beelink production applications and databases", controller)
        self.assertIn("beelink-production.service", controller)

    def test_configuration_rewrite_does_not_change_bind_mounts_or_credentials(self):
        config = {
            "name": "beelink-app-test",
            "services": {"owncloud-app": {
                "environment": {"OWNCLOUD_DB_HOST": "beelink-staging-owncloud-1",
                                "OWNCLOUD_DB_PASSWORD": "original-value"},
                "volumes": [{"type": "bind", "source": "/daten/beelink-staging/owncloud",
                             "target": "/data", "read_only": False}]}},
            "networks": {"isolated": {"name": NAMES.OLD_NETWORK, "external": True}},
        }
        result = json.loads(NAMES.renamed(json.dumps(config)))
        self.assertEqual(result["services"]["owncloud-app"]["volumes"],
                         config["services"]["owncloud-app"]["volumes"])
        self.assertEqual(result["services"]["owncloud-app"]["environment"],
                         {"OWNCLOUD_DB_HOST": "beelink-databases-owncloud-1",
                          "OWNCLOUD_DB_PASSWORD": "original-value"})

    def test_network_rollback_preserves_frontend_subnet_and_compose_identity(self):
        network = {"Driver": "bridge", "Internal": True, "EnableIPv6": False,
                   "Options": {}, "IPAM": {"Config": [
                       {"Subnet": "172.19.0.0/16", "Gateway": "172.19.0.1"}]},
                   "Labels": {"com.docker.compose.project": "beelink-staging",
                              "com.docker.compose.network": "isolated",
                              "com.docker.compose.config-hash": "original-hash"}}
        with patch.object(NAMES, "run") as run:
            NAMES.create_network(network, NAMES.OLD_NETWORK)
        args = run.call_args.args
        self.assertIn("172.19.0.0/16", args)
        self.assertIn("172.19.0.1", args)
        self.assertIn("--internal", args)
        self.assertIn("com.docker.compose.project=beelink-staging", args)
        self.assertIn("com.docker.compose.config-hash=original-hash", args)
        self.assertEqual(args[-1], NAMES.OLD_NETWORK)
        network["IPAM"]["Config"][0]["Subnet"] = "172.21.0.0/16"
        with patch.object(NAMES, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "pinned"):
                NAMES.create_network(network, NAMES.OLD_NETWORK)
            run.assert_not_called()

    def test_new_compose_network_pins_original_subnet_and_gateway(self):
        source = json.dumps({
            "name": "beelink-staging", "services": {},
            "networks": {"isolated": {"internal": True}}})
        network = {"IPAM": {"Config": [
            {"Subnet": "172.19.0.0/16", "Gateway": "172.19.0.1"}]}}
        result = json.loads(NAMES.database_config(source, network))
        self.assertEqual(result["name"], "beelink-databases")
        self.assertEqual(result["networks"]["isolated"], {
            "internal": True, "ipam": {"config": [
                {"subnet": "172.19.0.0/16", "gateway": "172.19.0.1"}]}})

    def test_initial_restore_cannot_start_old_project_on_live_production_directories(self):
        restore = NAMES.load(ROOT / "monitoring/restore-beelink-databases.py",
                             "restore_names_guard")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            (destination / "compose.json").write_text('{"name": "beelink-databases"}')
            with patch.object(restore, "DESTINATION", destination), \
                    patch.object(restore, "verify_checksums") as verify, \
                    patch.object(restore, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "initial restore must not be rerun"):
                    restore.restore()
                verify.assert_not_called()
                run.assert_not_called()

    def test_private_snapshot_restores_exact_content_permissions_and_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup = root / "backup"
            backup.mkdir()
            path = root / "original"
            path.write_text("before\n")
            path.chmod(0o640)
            metadata = NAMES.save(backup, {path: "after\n"})
            path.write_text("after\n")
            with patch.object(NAMES.os, "chown") as chown:
                NAMES.restore_files(backup, metadata)
            self.assertEqual(path.read_text(), "before\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual((backup / "0").stat().st_mode & 0o777, 0o600)
            chown.assert_called_once_with(path, path.stat().st_uid, path.stat().st_gid)

    def test_protected_identity_ignores_changing_healthcheck_history(self):
        with patch.object(NAMES, "inspect", return_value={
                "Id": "same", "State": {"StartedAt": "before", "Running": True,
                                       "Health": {"Log": ["changes every minute"]}}}):
            self.assertEqual(NAMES.identity("protected"),
                             {"id": "same", "started": "before", "running": True})

    def test_refuses_to_delete_network_with_foreign_endpoint(self):
        with patch.object(NAMES, "capture", return_value="network"), \
                patch.object(NAMES, "inspect", return_value={
                    "Containers": {"foreign": {"Name": "unrelated"}}}), \
                patch.object(NAMES, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "endpoints"):
                NAMES.remove_network("network")
            run.assert_not_called()

    def test_socket_ordering_fix_survives_new_service_dependencies(self):
        source = (ROOT / "monitoring/publish-beelink-web.py").read_text()
        self.assertIn("BindsTo=beelink-production.service daten.mount", source)
        self.assertIn("PartOf=beelink-production.service", source)
        operations = (ROOT / "monitoring/activate-beelink-operations.py").read_text()
        self.assertIn("DefaultDependencies=no", operations)
        self.assertIn("Before=shutdown.target", operations)

    def test_health_excludes_only_explicitly_paused_timer(self):
        timer = "beelink-certificates.timer"
        with patch.object(NAMES, "capture", return_value=json.dumps({
                "failures": ["Inactive unit: " + timer]})), \
                patch.object(NAMES.time, "sleep"):
            NAMES.wait_health(paused_timers=[timer])
            with self.assertRaisesRegex(RuntimeError, "did not settle"):
                NAMES.wait_health()
        with patch.object(NAMES, "capture", return_value=json.dumps({
                "failures": ["Inactive unit: " + timer, "TLS/HTTP failed: www"]})), \
                patch.object(NAMES.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "did not settle"):
                NAMES.wait_health(paused_timers=[timer])
        with self.assertRaises(ValueError):
            NAMES.wait_health(paused_timers=["beelink-production.service"])

    def exercise_migration(self, fail_start=False):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as patches:
            root = Path(temporary)
            units = root / "units"
            units.mkdir()
            old_unit = units / NAMES.OLD_UNIT
            old_unit.write_text("Requires=beelink-staging.service\n")
            original_config = json.dumps({
                "name": "beelink-staging",
                "services": {"mxserver": {"volumes": [
                    {"type": "bind", "source": "/daten/beelink-staging/mxserver/data",
                     "target": "/var/lib/postgresql/data"}]}}})
            config = root / "compose.json"
            config.write_text(original_config)
            stacks = (("beelink-staging", config, ("mxserver",)),)
            old = SimpleNamespace(STACKS=stacks, preflight=Mock(), wait_databases=Mock())
            network = {"Driver": "bridge", "Internal": True, "EnableIPv6": False,
                       "Options": {}, "IPAM": {"Config": [
                           {"Subnet": "172.19.0.0/16", "Gateway": "172.19.0.1"}]},
                       "Containers": {}, "Labels": {}}
            bind = {"Type": "bind", "Source": "/daten/beelink-staging/mxserver/data",
                    "Destination": "/var/lib/postgresql/data", "RW": True}

            def inspect(kind, name):
                if kind == "network":
                    return network
                return {"Mounts": [bind], "State": {"Running": False},
                        "NetworkSettings": {"Networks": {}}}

            paused = False

            def capture(*args):
                if args[:2] == ("docker", "ps"):
                    return "beelink-staging-mxserver-1"
                if args[:3] == ("docker", "network", "ls"):
                    return NAMES.OLD_NETWORK
                if args[:2] == ("systemctl", "list-units"):
                    return ("beelink-job-monitor.timer loaded active waiting\n"
                            "beelink-certificates.timer loaded active waiting\n")
                if args[:2] == ("systemctl", "show"):
                    return "inactive"
                if args[:2] == ("python3", "/usr/local/bin/beelink-job-status"):
                    return json.dumps({"failures": [
                        "Inactive unit: beelink-certificates.timer"] if paused else []})
                raise AssertionError(args)

            failed = False

            def run(*args, **kwargs):
                nonlocal failed, paused
                if args[:3] == ("systemctl", "stop", "beelink-job-monitor.timer"):
                    paused = True
                if args[:3] == ("systemctl", "start", "beelink-job-monitor.timer"):
                    paused = False
                if (fail_start and not failed and args[:3] ==
                        ("systemctl", "start", NAMES.NEW_UNIT)):
                    failed = True
                    raise subprocess.CalledProcessError(1, args)

            mocks = {}
            for name, value in {
                    "UNITS": units, "RUNTIME": root, "MARKER": root / "result.json",
                    "BACKUPS": root / "backups"}.items():
                patches.enter_context(patch.object(NAMES, name, value))
            for name, kwargs in {
                    "load": {"return_value": old},
                    "active": {"return_value": True},
                    "identity": {"return_value": {"id": "untouched"}},
                    "inspect": {"side_effect": inspect},
                    "capture": {"side_effect": capture},
                    "file_plan": {"return_value": {
                        old_unit: NAMES.renamed(old_unit.read_text()),
                        config: NAMES.renamed(original_config)}},
                    "lifecycle_lock": {"side_effect": contextlib.nullcontext},
                    "remove_containers": {}, "remove_network": {}, "create_network": {},
                    "compose": {},
                    "run": {"side_effect": run},
            }.items():
                mocks[name] = patches.enter_context(patch.object(NAMES, name, **kwargs))
            # File ownership is unchanged by the real operation; avoid root-only chown in tests.
            patches.enter_context(patch.object(NAMES.os, "chown"))
            if fail_start:
                with self.assertRaises(subprocess.CalledProcessError):
                    NAMES.apply()
                self.assertEqual(config.read_text(), original_config)
                self.assertTrue(old_unit.exists())
                self.assertFalse((units / NAMES.NEW_UNIT).exists())
                self.assertFalse((root / "result.json").exists())
                self.assertTrue(any(call.args[:3] == ("systemctl", "start", NAMES.OLD_UNIT)
                                    for call in mocks["run"].call_args_list))
                self.assertEqual(mocks["remove_containers"].call_count, 2)
            else:
                NAMES.apply()
                self.assertEqual(json.loads(config.read_text())["name"], "beelink-databases")
                self.assertFalse(old_unit.exists())
                self.assertTrue((units / NAMES.NEW_UNIT).exists())
                self.assertTrue((root / "result.json").is_file())
                self.assertEqual(mocks["remove_containers"].call_count, 1)
            self.assertEqual(mocks["run"].call_args.args,
                             ("systemctl", "is-active", "--quiet", "beelink-certificates.timer"))
            self.assertFalse(paused)
            commands = [call.args for call in mocks["run"].call_args_list]
            self.assertFalse(any("--run" in command or "--execute" in command
                                 for command in commands))
            return mocks

    def test_apply_updates_names_enables_boot_and_restores_schedule_without_sync(self):
        self.exercise_migration()

    def test_failed_new_service_restores_configuration_boot_and_schedule(self):
        self.exercise_migration(fail_start=True)


if __name__ == "__main__":
    unittest.main()
