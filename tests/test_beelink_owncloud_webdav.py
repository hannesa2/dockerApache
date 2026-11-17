import contextlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location(
    "beelink_webdav", Path(__file__).resolve().parents[1] /
    "monitoring/check-beelink-owncloud-webdav.py")
DAV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DAV)
USERNAME = "beelink-dav-" + "a" * 24
PASSWORD = "fixture-secret"
PAYLOAD = b"beelink isolated WebDAV check\n" + bytes(range(256)) * 4
FILE = "/owncloud/remote.php/dav/files/" + USERNAME + "/beelink-webdav-check/probe.bin"
LISTING = ('<d:multistatus xmlns:d="DAV:"><d:response><d:href>' + FILE +
           '</d:href><d:propstat><d:prop><d:getcontentlength>' + str(len(PAYLOAD)) +
           '</d:getcontentlength></d:prop><d:status>HTTP/1.1 200 OK</d:status>'
           '</d:propstat></d:response></d:multistatus>').encode()


class BeelinkWebDAVTest(unittest.TestCase):
    def run_probe(self, bodies=None, statuses=None):
        bodies = bodies or [b"", b"", b"", PAYLOAD, LISTING, b"", b""]
        statuses = statuses or [401, 201, 201, 200, 207, 204, 404]
        connections = []
        for status, body in zip(statuses, bodies):
            connection = MagicMock()
            connection.getresponse.return_value.status = status
            connection.getresponse.return_value.read.return_value = body
            connections.append(connection)
        settings = {"username": USERNAME, "password": PASSWORD, "ip": "172.19.0.15"}
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(settings))), \
                patch.object(http.client, "HTTPSConnection", side_effect=connections), \
                patch("socket.create_connection") as connect, \
                patch("ssl.create_default_context") as tls, \
                contextlib.redirect_stdout(output):
            exec(DAV.PROBE, {})
        self.assertEqual(connect.call_args.args[0], ("172.19.0.15", 443))
        tls.return_value.wrap_socket.assert_called_with(
            connect.return_value, server_hostname="www.mxtracks.info")
        for connection in connections:
            connection.close.assert_called_once()
        return json.loads(output.getvalue()), connections

    def test_real_probe_checks_exact_payload_listing_deletion_and_verified_tls(self):
        report, connections = self.run_probe()
        self.assertEqual(report["upload_bytes"], len(PAYLOAD))
        self.assertEqual(report["deleted_file_status"], 404)
        requests = [connection.request.call_args for connection in connections]
        self.assertNotIn("Authorization", requests[0].kwargs["headers"])
        self.assertEqual([request.args[0] for request in requests],
                         ["PROPFIND", "MKCOL", "PUT", "GET", "PROPFIND", "DELETE", "GET"])
        self.assertEqual(requests[2].kwargs["body"], PAYLOAD)

    def test_probe_rejects_redirect_instead_of_following_it(self):
        with self.assertRaisesRegex(RuntimeError, "expected 401, received 302"):
            self.run_probe(statuses=[302])

    def test_probe_rejects_changed_download(self):
        with self.assertRaisesRegex(RuntimeError, "differs from uploaded"):
            self.run_probe(bodies=[b"", b"", b"", b"corrupt"])

    def test_probe_rejects_missing_file_from_multistatus(self):
        with self.assertRaisesRegex(RuntimeError, "listing did not contain"):
            self.run_probe(bodies=[b"", b"", b"", PAYLOAD, b'<multistatus xmlns="DAV:"/>'])

    def test_probe_rejects_failed_delete(self):
        with self.assertRaisesRegex(RuntimeError, "expected 204, received 403"):
            self.run_probe(statuses=[401, 201, 201, 200, 207, 403])

    def verify_fixture(self, root, user_results, probe_result=None, run_error=None):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(DAV, "PENDING", root / "pending.json"))
        stack.enter_context(patch.object(DAV, "REPORT", root / "report.json"))
        stack.enter_context(patch.object(DAV.secrets, "token_hex", return_value="a" * 24))
        stack.enter_context(patch.object(DAV.secrets, "token_urlsafe", return_value=PASSWORD))
        stack.enter_context(patch.object(DAV, "users", side_effect=user_results))
        run = stack.enter_context(patch.object(DAV.control, "run", side_effect=run_error))
        probe = stack.enter_context(patch.object(
            DAV.subprocess, "run", return_value=probe_result or subprocess.CompletedProcess(
                ["probe"], 0, '{"upload_bytes": 1056}', "")))
        return run, probe

    def test_temporary_user_cleanup_and_secret_stdin_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run, probe = self.verify_fixture(root, [{}, {USERNAME: "test"}, {}])
            DAV.verify("172.19.0.15")
            commands = [call.args for call in run.call_args_list]
            self.assertTrue(any("user:delete" in command for command in commands))
            self.assertNotIn(PASSWORD, repr(commands))
            self.assertEqual(run.call_args_list[1].kwargs["input"], PASSWORD + "\n")
            self.assertEqual(json.loads(probe.call_args.kwargs["input"])["password"], PASSWORD)
            self.assertFalse((root / "pending.json").exists())
            self.assertTrue(json.loads((root / "report.json").read_text())[
                "temporary_account_removed"])

    def test_existing_user_is_never_changed_or_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run, probe = self.verify_fixture(root, [{USERNAME: "original"}])
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                DAV.verify("172.19.0.15")
            run.assert_not_called()
            probe.assert_not_called()

    def test_failed_probe_still_removes_test_account_without_success_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run, _ = self.verify_fixture(
                root, [{}, {USERNAME: "test"}, {}],
                subprocess.CompletedProcess(["probe"], 1, "", "WebDAV failed\n"))
            with self.assertRaises(subprocess.CalledProcessError):
                DAV.verify("172.19.0.15")
            self.assertTrue(any("user:delete" in call.args for call in run.call_args_list))
            self.assertFalse((root / "report.json").exists())
            self.assertFalse((root / "pending.json").exists())

    def test_failed_cleanup_keeps_recovery_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.verify_fixture(root, [{}, {USERNAME: "test"}, {USERNAME: "test"}])
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                DAV.verify("172.19.0.15")
            self.assertEqual(json.loads((root / "pending.json").read_text())["username"], USERNAME)
            self.assertFalse((root / "report.json").exists())

    def test_partial_user_creation_failure_still_cleans_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failure = subprocess.CalledProcessError(1, ["occ", "user:add"])
            run, probe = self.verify_fixture(
                root, [{}, {USERNAME: "partial"}, {}], run_error=[None, failure, None, None])
            with self.assertRaises(subprocess.CalledProcessError):
                DAV.verify("172.19.0.15")
            self.assertTrue(any("user:delete" in call.args for call in run.call_args_list))
            probe.assert_not_called()
            self.assertFalse((root / "pending.json").exists())
            self.assertFalse((root / "report.json").exists())

    def test_config_guard_rejects_nonstaging_database_before_user_creation(self):
        with patch.object(DAV.control, "preflight") as preflight, \
                patch.object(DAV.control, "capture", return_value="localhost"):
            with self.assertRaisesRegex(RuntimeError, "configuration: dbhost"):
                DAV.check_target()
        preflight.assert_called_once()

    def test_pending_marker_blocks_retry_and_does_not_replace_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "pending.json"
            marker.write_text('{"username":"existing-interrupted-check"}')
            with patch.object(DAV, "PENDING", marker), \
                    patch.object(DAV.control, "preflight"), \
                    patch.object(DAV.control, "capture", side_effect=[
                        "beelink-staging-owncloud-1", "owncloud_stage", "/mnt/data/files",
                        '{"installed":true}']):
                with self.assertRaisesRegex(RuntimeError, "unfinished test account"):
                    DAV.check_target()
            self.assertEqual(marker.read_text(), '{"username":"existing-interrupted-check"}')


if __name__ == "__main__":
    unittest.main()
