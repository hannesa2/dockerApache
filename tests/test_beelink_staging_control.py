import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "beelink_staging_control", Path(__file__).resolve().parents[1] /
    "monitoring/control-beelink-staging.py")
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)


def config():
    return {"name": "project", "services": {
        "app": {"restart": "no", "networks": ["isolated"], "volumes": []}},
        "networks": {"isolated": {"external": True, "name": "beelink-databases_isolated"}}}


class BeelinkStagingControlTest(unittest.TestCase):
    def test_database_snapshot_preserves_project_and_source_without_chown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            original = json.dumps({"fixture": "private"})
            source.write_text(original)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            with patch.object(CONTROL, "RUNTIME", runtime), \
                    patch.object(CONTROL, "DATABASE_SOURCE", source), \
                    patch.object(CONTROL, "check_mount"), \
                    patch.object(CONTROL, "validate_config") as validate, \
                    patch.object(CONTROL.os, "chown") as chown, \
                    patch.object(CONTROL.os, "getuid", return_value=0):
                real_stat = Path.stat
                def stat(path, **kwargs):
                    result = real_stat(path, **kwargs)
                    values = list(result)
                    values[4] = 0
                    return os.stat_result(values)
                with patch.object(Path, "stat", stat):
                    CONTROL.install_database_snapshot()
                    CONTROL.install_database_snapshot()
                    snapshot = runtime / "database-compose.json"
                    self.assertEqual(json.loads(snapshot.read_text()), json.loads(original))
                    self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
                    snapshot.write_text('{"different": true}')
                    with self.assertRaisesRegex(RuntimeError, "snapshot differs"):
                        CONTROL.install_database_snapshot()
                validate.assert_called_with(CONTROL.STACKS[0][0], json.loads(original),
                                            CONTROL.STACKS[0][2])
                chown.assert_not_called()
            self.assertEqual(source.read_text(), original)

    def test_mount_guard_rejects_autofs_missing_or_wrong_filesystem_uuid(self):
        for uuid in ("", "wrong", CONTROL.UUID + "\nother"):
            with patch.object(CONTROL.os, "listdir"), \
                    patch.object(CONTROL, "capture", return_value=uuid):
                with self.assertRaisesRegex(RuntimeError, "refusing startup"):
                    CONTROL.check_mount()
        with patch.object(CONTROL.os, "listdir"), \
                patch.object(CONTROL, "capture", return_value=CONTROL.UUID) as capture:
            CONTROL.check_mount()
        self.assertIn("ext4", capture.call_args.args)
        self.assertIn("-M", capture.call_args.args)

    def test_unit_binds_mount_but_does_not_bypass_docker_restart_guard(self):
        self.assertIn("BindsTo=daten.mount", CONTROL.UNIT_TEXT)
        self.assertIn("RequiresMountsFor=/daten", CONTROL.UNIT_TEXT)
        self.assertIn("After=docker.service daten.mount", CONTROL.UNIT_TEXT)
        self.assertIn("/usr/local/libexec/", CONTROL.UNIT_TEXT)
        self.assertNotIn("/home/hannes/git", CONTROL.UNIT_TEXT)
        self.assertNotIn("Restart=always", CONTROL.UNIT_TEXT)

    def test_accepts_only_internal_nonrestarting_services(self):
        CONTROL.validate_config("project", config(), ("app",))
        for key, value in (("restart", "unless-stopped"), ("ports", ["80:80"]),
                           ("network_mode", "host"), ("privileged", True),
                           ("networks", ["isolated", "outside"]),
                           ("volumes", [{"type": "bind", "source": "/etc",
                                         "bind": {"create_host_path": False}}])):
            candidate = config()
            candidate["services"]["app"][key] = value
            with self.subTest(key=key):
                with self.assertRaises(RuntimeError):
                    CONTROL.validate_config("project", candidate, ("app",))

    def test_source_boot_mounts_cannot_be_silently_created(self):
        candidate = config()
        candidate["services"]["app"]["volumes"] = [
            {"type": "bind", "source": "/daten/fixture", "bind": {"create_host_path": True}}]
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            CONTROL.validate_config("project", candidate, ("app",))
        candidate["networks"]["isolated"]["name"] = "external"
        candidate["services"]["app"]["volumes"] = []
        with self.assertRaisesRegex(RuntimeError, "network reference"):
            CONTROL.validate_config("project", candidate, ("app",))

    def test_starts_only_selected_existing_services_without_restore_or_build(self):
        with patch.object(CONTROL, "preflight"), \
                patch.object(CONTROL, "check_mount") as mount, \
                patch.object(CONTROL, "wait_databases") as databases, \
                patch.object(CONTROL, "wait_applications") as applications, \
                patch.object(CONTROL, "run") as run:
            CONTROL.start()
        self.assertEqual(mount.call_count, len(CONTROL.STACKS))
        databases.assert_called_once()
        applications.assert_called_once()
        commands = [call.args for call in run.call_args_list]
        self.assertEqual(len(commands), len(CONTROL.STACKS) + 2)
        for project, _, services in CONTROL.STACKS:
            command = next(command for command in commands if project in command)
            self.assertIn("--no-build", command)
            self.assertIn("--pull", command)
            self.assertIn("never", command)
            self.assertEqual(command[-len(services):], services)
        java = next(command for command in commands if "beelink-java-web" in command)
        self.assertNotIn("apache", java)
        self.assertNotIn("homeassistant", [word for command in commands for word in command])
        self.assertTrue(commands[-2][1].endswith("start-beelink-artifacts.py"))
        self.assertTrue(commands[-1][1].endswith("start-beelink-dns.py"))

    def test_failed_mount_preflight_does_not_touch_containers(self):
        with patch.object(CONTROL, "preflight", side_effect=RuntimeError("no mount")), \
                patch.object(CONTROL, "run") as run, patch.object(CONTROL, "stop") as stop:
            with self.assertRaises(RuntimeError):
                CONTROL.start()
            run.assert_not_called()
            stop.assert_not_called()

    def test_start_failure_stops_only_managed_stacks(self):
        with patch.object(CONTROL, "preflight"), patch.object(CONTROL, "check_mount"), \
                patch.object(CONTROL, "run", side_effect=RuntimeError("failed")), \
                patch.object(CONTROL, "stop") as stop:
            with self.assertRaises(RuntimeError):
                CONTROL.start()
            stop.assert_called_once()

    def test_stop_uses_labels_without_reading_missing_disk_or_stopping_retired_apache(self):
        stacks = (("beelink-java-web", Path("/unmounted/compose.json"), ("glassfish", "wildfly")),)
        def capture(*args):
            if args[1] == "ps":
                return "glassfish\napache\noneoff"
            name = args[-1]
            return json.dumps([{"State": {"Running": True}, "Config": {"Labels": {
                "com.docker.compose.service": "glassfish" if name == "oneoff" else name,
                "com.docker.compose.oneoff": "True" if name == "oneoff" else "False"}}}])
        with patch.object(CONTROL, "STACKS", stacks), \
                patch.object(CONTROL, "capture", side_effect=capture), \
                patch.object(CONTROL, "run") as run, \
                patch.object(CONTROL, "check_mount") as mount:
            CONTROL.stop()
        mount.assert_not_called()
        run.assert_called_once_with("docker", "stop", "--time", "60", "glassfish")

    def test_database_checks_fail_fast_when_container_exits(self):
        with patch.object(CONTROL, "capture", return_value="false"), \
                patch.object(CONTROL.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "container stopped"):
                CONTROL.wait_databases(attempts=1)
        sleep.assert_not_called()

    def test_application_readiness_waits_and_fails_fast_on_exit(self):
        with patch.object(CONTROL, "capture", return_value="true"), \
                patch.object(CONTROL.subprocess, "run") as run:
            run.return_value.returncode = 0
            CONTROL.wait_applications(attempts=1)
            self.assertEqual(run.call_count, 4)
        with patch.object(CONTROL, "capture", return_value="false"), \
                patch.object(CONTROL.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "Application stopped"):
                CONTROL.wait_applications(attempts=1)
            sleep.assert_not_called()

    def test_restart_records_cold_start_and_protected_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            restarted = False
            def identity(name):
                return {"id": name, "started": "new" if restarted and name == "project-app-1"
                        else "old", "running": name != "beelink-java-web-apache-1"}
            def run(*args):
                nonlocal restarted
                self.assertEqual(args, ("systemctl", "restart", "beelink-production.service"))
                restarted = True
            with patch.object(CONTROL, "RUNTIME", Path(temporary)), \
                    patch.object(CONTROL, "STACKS", (("project", Path("/unused"), ("app",)),)), \
                    patch.object(CONTROL, "capture", return_value="active"), \
                    patch.object(CONTROL, "container_identity", side_effect=identity), \
                    patch.object(CONTROL, "run", side_effect=run):
                CONTROL.restart_test()
            report = json.loads((Path(temporary) / "staging-restart-report.json").read_text())
            self.assertEqual(report["managed_restarted"], ["project-app-1"])
            self.assertFalse(report["disk_absent_tested"])
            self.assertEqual(set(report["protected_unchanged"]),
                             {"homeassistant", "camera-postgres-db-1"})

    def test_restart_detects_unexpected_protected_service_change(self):
        identities = [
            {"id": "ha", "started": "old", "running": True},
            {"id": "camera", "started": "old", "running": True},
            {"id": "ha", "started": "new", "running": True},
        ]
        with patch.object(CONTROL, "STACKS", ()), \
                patch.object(CONTROL, "capture", return_value="active"), \
                patch.object(CONTROL, "container_identity", side_effect=identities), \
                patch.object(CONTROL, "run"):
            with self.assertRaisesRegex(RuntimeError, "Protected existing service changed"):
                CONTROL.restart_test()

    def test_installs_snapshots_and_validates_unit_without_enabling_boot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for name in ("control-beelink-staging.py", "start-beelink-apps.py",
                         "start-beelink-frontend.py", "start-beelink-artifacts.py",
                         "start-beelink-dns.py"):
                (source / name).write_text("# fixture\n")
            unit = root / "beelink-production.service"
            library = root / "libexec"
            with patch.object(CONTROL, "__file__", str(source / "control-beelink-staging.py")), \
                    patch.object(CONTROL, "LIBEXEC", library), \
                    patch.object(CONTROL, "UNIT", unit), \
                    patch.object(CONTROL, "install_database_snapshot"), \
                    patch.object(CONTROL, "preflight"), \
                    patch.object(CONTROL.os, "chown"), \
                    patch.object(CONTROL, "run") as run:
                CONTROL.install()
            self.assertEqual(unit.read_text(), CONTROL.UNIT_TEXT)
            self.assertEqual((library / "start-beelink-frontend.py").read_text(), "# fixture\n")
            self.assertEqual((library / "control-beelink-staging.py").stat().st_mode & 0o777, 0o644)
            self.assertEqual(run.call_args_list[0].args[:2], ("systemd-analyze", "verify"))
            self.assertIn("/run/systemd/generator:",
                          run.call_args_list[0].kwargs["env"]["SYSTEMD_UNIT_PATH"])
            self.assertEqual(run.call_args_list[1].args, ("systemctl", "daemon-reload"))
            self.assertFalse(any("enable" in call.args for call in run.call_args_list))

    def test_dns_network_exception_is_narrow_and_readonly(self):
        candidate = {"name": "beelink-dns", "services": {"dns": {
            "restart": "no", "networks": ["resolver"], "read_only": True, "volumes": []}},
            "networks": {"resolver": {"driver": "bridge"}}}
        CONTROL.validate_config("beelink-dns", candidate, ("dns",))
        candidate["services"]["dns"]["read_only"] = False
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            CONTROL.validate_config("beelink-dns", candidate, ("dns",))
        candidate["services"]["dns"]["read_only"] = True
        candidate["networks"]["resolver"]["external"] = True
        with self.assertRaisesRegex(RuntimeError, "DNS network"):
            CONTROL.validate_config("beelink-dns", candidate, ("dns",))


if __name__ == "__main__":
    unittest.main()
