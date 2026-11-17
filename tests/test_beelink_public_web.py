import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "public_web", Path(__file__).resolve().parents[1] / "monitoring/publish-beelink-web.py")
WEB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WEB)


class PublicWebTest(unittest.TestCase):
    def test_units_are_dual_stack_mount_bound_and_not_boot_enabled(self):
        definitions = WEB.units(443, "172.19.0.15")
        listener = definitions["beelink-public-web-443.socket"]
        service = definitions["beelink-public-web-443.service"]
        self.assertIn("ListenStream=0.0.0.0:443", listener)
        self.assertIn("ListenStream=[::]:443", listener)
        self.assertIn("BindIPv6Only=ipv6-only", listener)
        for text in definitions.values():
            self.assertIn("BindsTo=beelink-staging.service daten.mount", text)
            self.assertNotIn("WantedBy", text)
        self.assertIn("systemd-socket-proxyd 172.19.0.15:443", service)
        self.assertIn("DynamicUser=yes", service)
        self.assertIn(WEB.control.UUID, service)

    def test_tls_probes_use_real_hostname_verification_without_proxy(self):
        with patch.object(WEB.control, "capture", side_effect=["301", "200"] * 4) as capture:
            WEB.probe("::1")
        self.assertEqual(capture.call_count, 8)
        for call in capture.call_args_list:
            self.assertIn("--noproxy", call.args)
            self.assertNotIn("--insecure", call.args)
            self.assertTrue(any(":[::1]" in value for value in call.args))

    def test_bad_http_status_fails(self):
        with patch.object(WEB.control, "capture", side_effect=["301", "503"]):
            with self.assertRaisesRegex(RuntimeError, "TLS HTTP check failed"):
                WEB.probe("192.168.178.142")

    def test_wrong_host_rejected(self):
        with patch.object(WEB.os, "getuid", return_value=0), \
                patch.object(WEB.socket, "gethostname", return_value="Latitude"), \
                patch("sys.argv", ["publish", "--publish-lan"]):
            with self.assertRaisesRegex(RuntimeError, "directly on beelink"):
                WEB.main()


if __name__ == "__main__":
    unittest.main()
