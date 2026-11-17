import base64
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
    "beelink_mxserver_auth", Path(__file__).resolve().parents[1] /
    "monitoring/check-beelink-mxserver-auth.py")
AUTH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTH)


class BeelinkMxserverAuthTest(unittest.TestCase):
    def test_api_probe_uses_verified_hostname_and_basic_auth_only_when_given(self):
        response = MagicMock(status=200)
        response.read.return_value = b'{"version":"1"}'
        connection = MagicMock()
        connection.getresponse.return_value = response
        sock = MagicMock()
        with patch.object(http.client, "HTTPSConnection", return_value=connection), \
                patch.object(AUTH.socket, "create_connection", return_value=sock) as connect, \
                patch.object(AUTH.ssl, "create_default_context") as tls:
            status, size = AUTH.api_request("172.19.0.15", "user", "temporary-secret")
        self.assertEqual((status, size), (200, len(b'{"version":"1"}')))
        connect.assert_called_once_with(("172.19.0.15", 443), timeout=15)
        tls.return_value.wrap_socket.assert_called_once_with(sock, server_hostname=AUTH.DOMAIN)
        headers = connection.request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"],
                         "Basic " + base64.b64encode(b"user:temporary-secret").decode())
        self.assertEqual(connection.request.call_args.args,
                         ("GET", AUTH.ENDPOINT))
        connection.close.assert_called_once()

    def test_anonymous_probe_sends_no_authorization_header(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 401
        connection.getresponse.return_value.read.return_value = b""
        with patch.object(http.client, "HTTPSConnection", return_value=connection), \
                patch.object(AUTH.socket, "create_connection"), \
                patch.object(AUTH.ssl, "create_default_context"):
            self.assertEqual(AUTH.api_request("172.19.0.15"), (401, 0))
        self.assertEqual(connection.request.call_args.kwargs["headers"], {})

    def test_file_realm_probe_reuses_existing_role_mapped_principal(self):
        self.assertIn('case "replace":', AUTH.JAVA_SOURCE)
        self.assertIn('realm.removeUser(args[2]);', AUTH.JAVA_SOURCE)
        self.assertIn('realm.addUser(args[2], password, new String[0]);', AUTH.JAVA_SOURCE)
        self.assertIn('case "verify":', AUTH.JAVA_SOURCE)
        self.assertNotIn("UserRole", AUTH.JAVA_SOURCE)
        self.assertEqual(Path(AUTH.REMOTE_SOURCE).name, "FileRealmProbe.java")
        self.assertEqual(Path(AUTH.REMOTE_CLASS).name, "FileRealmProbe")

    def setup_verify(self, root, api_statuses, realm_action=None):
        work = root / "java-web"
        work.mkdir()
        keyfile = work / "keyfile"
        keyfile.write_bytes(b"original GlassFish realm\n")
        keyfile.chmod(0o600)
        pending = work / "pending.json"
        report = work / "report.json"
        responses = iter(api_statuses)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(AUTH, "WORK", work))
        stack.enter_context(patch.object(AUTH, "KEYFILE", keyfile))
        stack.enter_context(patch.object(AUTH, "PENDING", pending))
        stack.enter_context(patch.object(AUTH, "REPORT", report))
        stack.enter_context(patch.object(AUTH.secrets, "token_hex",
                                         return_value="a" * 64))
        stack.enter_context(patch.object(AUTH, "api_request",
                                         side_effect=lambda *args: (next(responses), 12)))
        if realm_action is None:
            def realm_action(name, username, secret=None):
                if name == "replace":
                    keyfile.write_bytes(b"temporary GlassFish realm\n")
        action = stack.enter_context(patch.object(
            AUTH, "realm_action", side_effect=realm_action))
        run = stack.enter_context(patch.object(AUTH, "run"))
        wait = stack.enter_context(patch.object(AUTH, "wait_ready"))
        stack.enter_context(patch.object(
            AUTH.control, "container_identity",
            side_effect=lambda name: {"id": name, "started": "fixed", "running": True}))
        return keyfile, pending, report, action, run, wait

    def test_success_restores_exact_realm_and_rejects_removed_password(self):
        with tempfile.TemporaryDirectory() as temporary:
            keyfile, pending, report, action, run, wait = self.setup_verify(
                Path(temporary), [401, 200, 401, 401])
            AUTH.verify("172.19.0.15")
            self.assertEqual(keyfile.read_bytes(), b"original GlassFish realm\n")
            self.assertFalse(pending.exists())
            self.assertFalse((keyfile.parent / "mxserver-keyfile-before-auth-test").exists())
            recorded = json.loads(report.read_text())
            self.assertEqual(recorded["disposable_authenticated_status"], 200)
            self.assertTrue(recorded["original_keyfile_restored"])
            self.assertTrue(recorded["protected_services_unchanged"])
            self.assertEqual(action.call_args_list[0].args, ("replace", "user", "a" * 64))
            self.assertEqual(action.call_args_list[1].args, ("verify", "user", "a" * 64))
            self.assertEqual(run.call_count, 2)
            self.assertEqual(wait.call_count, 2)

    def test_api_failure_still_restores_realm_and_propagates_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            keyfile, pending, report, _, run, _ = self.setup_verify(
                Path(temporary), [401, 503, 401, 401])
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                AUTH.verify("172.19.0.15")
            self.assertEqual(keyfile.read_bytes(), b"original GlassFish realm\n")
            self.assertFalse(pending.exists())
            self.assertFalse(report.exists())
            self.assertEqual(run.call_count, 2)

    def test_unexpected_concurrent_keyfile_change_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def mutate(action, username, secret=None):
                if action == "replace":
                    keyfile.write_bytes(b"temporary GlassFish realm\n")
                elif action == "verify":
                    keyfile.write_bytes(b"unexpected concurrent change\n")
            keyfile, pending, report, _, _, _ = self.setup_verify(
                root, [401, 200], realm_action=mutate)
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                AUTH.verify("172.19.0.15")
            self.assertEqual(keyfile.read_bytes(), b"unexpected concurrent change\n")
            self.assertTrue(pending.exists())
            self.assertTrue((keyfile.parent / "mxserver-keyfile-before-auth-test").exists())
            self.assertFalse(report.exists())

    def test_preexisting_pending_state_blocks_any_auth_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keyfile, pending, _, _, run, _ = self.setup_verify(root, [401])
            pending.write_text('{"username":"user"}')
            with self.assertRaisesRegex(RuntimeError, "Existing MXServer"):
                AUTH.verify("172.19.0.15")
            run.assert_not_called()
            self.assertEqual(keyfile.read_bytes(), b"original GlassFish realm\n")


if __name__ == "__main__":
    unittest.main()
