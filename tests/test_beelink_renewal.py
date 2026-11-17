import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "renewal", Path(__file__).resolve().parents[1] /
    "monitoring/renew-beelink-certificates.py")
RENEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENEW)


class BeelinkRenewalTest(unittest.TestCase):
    def test_validate_accepts_private_standalone_state_and_rejects_source_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "certificates"
            config = state / "etc/letsencrypt"
            served = root / "host-configuration/etc/letsencrypt"
            served.mkdir(parents=True)
            (config / "renewal").mkdir(parents=True)
            (state / "prepared.json").write_text("{}")
            directory = config / "live/site"
            directory.mkdir(parents=True)
            archive = config / "archive/site"
            archive.mkdir(parents=True)
            text = f"archive_dir = {archive}\n"
            for key, name in (("cert", "cert.pem"), ("privkey", "privkey.pem"),
                              ("chain", "chain.pem"), ("fullchain", "fullchain.pem")):
                (archive / name).write_text("fixture")
                (directory / name).symlink_to(archive / name)
                text += f"{key} = {directory / name}\n"
            text += ("[renewalparams]\nauthenticator = standalone\n"
                     "server = https://acme-v02.api.letsencrypt.org/directory\n"
                     "[acme_renewal_info]\nari_retry_after = 2026-11-18T00:00:00\n")
            renewal = config / "renewal/site.conf"
            renewal.write_text(text)
            details = [{"State": {"Running": True}, "Mounts": [{
                "Destination": "/etc/letsencrypt", "Source": str(served), "RW": False}]}]
            with patch.object(RENEW, "RUNTIME", root), \
                    patch.object(RENEW, "STATE", state), \
                    patch.object(RENEW, "CONFIG", config), \
                    patch.object(RENEW, "SERVED", served), \
                    patch.object(RENEW, "LINEAGES", {"site": "example.org"}), \
                    patch.object(RENEW, "capture", return_value=json.dumps(details)), \
                    patch.object(RENEW.subprocess, "run",
                                 return_value=CompletedProcess([], 1, "masked\n")):
                RENEW.validate()
                renewal.write_text(text.replace(
                    "[acme_renewal_info]", "post_hook = systemctl start apache2\n[acme_renewal_info]"))
                with self.assertRaisesRegex(RuntimeError, "hook/installer"):
                    RENEW.validate()

    def test_standalone_failure_restores_http_without_stopping_https(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "paused"
            commands = []

            def execute(*args):
                commands.append(args)
                if args[0] == "certbot":
                    raise RuntimeError("ACME validation failed")

            with patch.object(RENEW, "PAUSED", marker), \
                    patch.object(RENEW, "active", return_value=True), \
                    patch.object(RENEW, "guard"), \
                    patch.object(RENEW, "run", side_effect=execute):
                with self.assertRaisesRegex(RuntimeError, "ACME validation"):
                    RENEW.renew(dry_run=True)
            self.assertFalse(marker.exists())
            self.assertEqual(commands[-1],
                             ("systemctl", "start", "beelink-public-web-80.socket"))
            self.assertNotIn("443", repr(commands))
            certbot = commands[1]
            self.assertIn("--dry-run", certbot)
            self.assertIn("--no-directory-hooks", certbot)
            self.assertIn("/dev/null", certbot)
            self.assertIn(str(RENEW.CONFIG), certbot)

    def test_stale_pause_marker_requires_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "paused"
            marker.touch()
            with patch.object(RENEW, "PAUSED", marker), \
                    patch.object(RENEW, "active", return_value=True), \
                    patch.object(RENEW, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "restore-http"):
                    RENEW.renew()
                run.assert_not_called()

    def test_restore_does_not_restart_stacks_after_mount_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "paused"
            marker.touch()
            with patch.object(RENEW, "PAUSED", marker), \
                    patch.object(RENEW, "guard", side_effect=RuntimeError("T7 missing")), \
                    patch.object(RENEW, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "T7 missing"):
                    RENEW.restore_http()
                run.assert_not_called()
            self.assertTrue(marker.exists())

    def test_timer_has_persistent_schedule_mount_guard_and_emergency_http_restore(self):
        definitions = RENEW.units()
        service = definitions["beelink-certificates.service"]
        timer = definitions["beelink-certificates.timer"]
        self.assertIn("BindsTo=daten.mount", service)
        self.assertIn("ConditionPathIsMountPoint=/daten", service)
        self.assertIn("ExecCondition=/usr/bin/systemctl is-active --quiet beelink-staging", service)
        self.assertIn("--restore-http", service)
        self.assertNotIn("Requires=beelink-staging", service)
        self.assertIn("Persistent=true", timer)
        self.assertIn("OnCalendar=*-*-* 00,12:00:00", timer)

    def test_enable_requires_successful_acme_and_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            for failing in ("renew", "deploy"):
                with patch.object(RENEW, "LOCK", Path(temporary) / "lock"), \
                        patch.object(RENEW, "guard"), \
                        patch.object(RENEW, "validate"), \
                        patch.object(RENEW, "renew") as renew, \
                        patch.object(RENEW, "deploy") as deploy, \
                        patch.object(RENEW, "notify"), \
                        patch.object(RENEW, "install") as install, \
                        patch("sys.argv", ["renew", "--test-and-enable"]):
                    {"renew": renew, "deploy": deploy}[failing].side_effect = RuntimeError(failing)
                    with self.assertRaisesRegex(RuntimeError, failing):
                        RENEW.main()
                    install.assert_not_called()

    def test_deployment_rolls_back_links_when_reload_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            served = root / "served"
            original = served / "archive/old"
            original.mkdir(parents=True)
            live = served / "live/site"
            live.mkdir(parents=True)
            (source / "live/site").mkdir(parents=True)
            for name in RENEW.FILES:
                (original / name).write_text("old")
                (live / name).symlink_to("../../archive/old/" + name)
                (source / "live/site" / name).write_text("new")
            with patch.object(RENEW, "CONFIG", source), \
                    patch.object(RENEW, "SERVED", served), \
                    patch.object(RENEW, "material", return_value={"site": "abc"}), \
                    patch.object(RENEW, "reload_consumers", side_effect=[
                        RuntimeError("bad reload"), None]) as reload, \
                    patch.object(RENEW, "verify_served") as verify:
                with self.assertRaisesRegex(RuntimeError, "bad reload"):
                    RENEW.deploy()
                self.assertEqual(reload.call_count, 2)
                verify.assert_not_called()
            for name in RENEW.FILES:
                self.assertEqual((live / name).read_text(), "old")

    def test_notification_submits_mail_for_success_and_failure(self):
        for success in (True, False):
            with patch.object(RENEW.subprocess, "run") as run:
                RENEW.notify(success, None if success else "ACME validation failed")
                args = run.call_args.args[0]
                self.assertEqual(args[-1], "hannes.software@gmx.at")
                self.assertIn("SUCCESS" if success else "FAILED", args[2])
                self.assertTrue(run.call_args.kwargs["check"])
                self.assertNotIn("privkey", run.call_args.kwargs["input"])

    def test_mirror_deployment_uses_restricted_key_verified_host_and_exact_tar_members(self):
        import tarfile
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary)
            directory = config / "live/mirror"
            directory.mkdir(parents=True)
            archive = config / "archive/mirror"
            archive.mkdir(parents=True)
            for name in ("fullchain.pem", "privkey.pem"):
                (archive / name).write_text("fixture")
                (directory / name).symlink_to(archive / name)
            (config / "renewal").mkdir()
            (config / "renewal/mirror.conf").touch()
            context = MagicMock()
            tls = context.wrap_socket.return_value.__enter__.return_value
            tls.getpeercert.return_value = b"DER"
            commands = []

            def execute(*args, **kwargs):
                commands.append(args)
                if args[0] == "ssh":
                    with tarfile.open(fileobj=kwargs["stdin"]) as stream:
                        self.assertEqual(stream.getnames(), ["fullchain.pem", "privkey.pem"])
                        self.assertTrue(all(m.isfile() for m in stream.getmembers()))

            with patch.object(RENEW, "CONFIG", config), \
                    patch.object(RENEW, "MIRRORS", ("mirror",)), \
                    patch.object(RENEW, "run", side_effect=execute), \
                    patch.object(RENEW, "capture", return_value="matching public key"), \
                    patch.object(RENEW.subprocess, "check_output", return_value=b"DER"), \
                    patch.object(RENEW.socket, "create_connection", return_value=MagicMock()), \
                    patch.object(RENEW.ssl, "create_default_context", return_value=context):
                RENEW.deploy_mirrors()
            ssh = commands[-1]
            self.assertIn("StrictHostKeyChecking=yes", ssh)
            self.assertIn("HostKeyAlias=mac2016", ssh)
            self.assertEqual(ssh[-2:], ("hannes@192.168.178.70", "mirror"))

    def test_failed_mail_submission_is_not_silently_accepted(self):
        with patch.object(RENEW.subprocess, "run", side_effect=RuntimeError("mail failed")):
            with self.assertRaisesRegex(RuntimeError, "mail failed"):
                RENEW.notify(True)

    def test_unchanged_deployment_verifies_without_restarting_dns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, served = root / "source", root / "served"
            archive = served / "archive/beelink-abc"
            archive.mkdir(parents=True)
            live = served / "live/site"
            live.mkdir(parents=True)
            (source / "live/site").mkdir(parents=True)
            for name in RENEW.FILES:
                (archive / name).write_text("same")
                (source / "live/site" / name).write_text("same")
                (live / name).symlink_to("../../archive/beelink-abc/" + name)
            with patch.object(RENEW, "CONFIG", source), \
                    patch.object(RENEW, "SERVED", served), \
                    patch.object(RENEW, "material", return_value={"site": "abc"}), \
                    patch.object(RENEW, "reload_consumers") as reload, \
                    patch.object(RENEW, "verify_served") as verify:
                RENEW.deploy()
                reload.assert_not_called()
                verify.assert_called_once_with({"site": "abc"})

    def test_mismatching_served_fingerprint_fails(self):
        class FakeTLS:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def getpeercert(self, binary_form):
                return b"wrong certificate"

        from unittest.mock import MagicMock
        context = MagicMock()
        context.wrap_socket.return_value = FakeTLS()
        details = [{"NetworkSettings": {"Networks": {
            "beelink-dns-test_dns_test": {"IPAddress": "172.20.0.2"}}}}]
        with patch.object(RENEW, "LINEAGES", {"site": "www.mxtracks.info"}), \
                patch.object(RENEW, "capture", return_value=json.dumps(details)), \
                patch.object(RENEW.socket, "create_connection", return_value=MagicMock()), \
                patch.object(RENEW.ssl, "create_default_context", return_value=context), \
                patch.object(RENEW.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "fingerprint"):
                RENEW.verify_served({"site": "expected"})
        context.wrap_socket.assert_called_with(
            unittest.mock.ANY, server_hostname="www.mxtracks.info")


if __name__ == "__main__":
    unittest.main()
