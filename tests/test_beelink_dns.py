import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "beelink_dns", Path(__file__).resolve().parents[1] / "monitoring/start-beelink-dns.py")
DNS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DNS)


class BeelinkDnsTest(unittest.TestCase):
    def test_dns_publishes_only_dual_stack_dns_and_dot_ports(self):
        config = DNS.compose_config("image")
        service = config["services"]["dns"]
        self.assertEqual(service["restart"], "no")
        self.assertTrue(service["read_only"])
        self.assertEqual(service["networks"], ["resolver"])
        self.assertEqual(len(service["ports"]), 6)
        self.assertEqual(
            {(item["host_ip"], item["published"], item["protocol"])
             for item in service["ports"]},
            {(host, port, protocol)
             for host in (DNS.IPV4, DNS.IPV6)
             for port, protocol in ((53, "udp"), (53, "tcp"), (853, "tcp"))})
        for key in ("network_mode", "privileged"):
            self.assertNotIn(key, service)
        for mount in service["volumes"]:
            self.assertTrue(mount["read_only"])
            self.assertFalse(mount["bind"]["create_host_path"])
        self.assertNotIn("ports", DNS.compose_config("image", publish=False)["services"]["dns"])

    def test_docker_bindings_require_both_families_independent_of_order(self):
        expected = DNS.expected_bindings()
        self.assertEqual(len(expected), 3)
        self.assertTrue(all(len(bindings) == 2 for bindings in expected.values()))
        actual = {key: list(reversed(value)) for key, value in expected.items()}
        for value in actual.values():
            value[0] = dict(value[0], HostIp="fdf4:be15:98e0::7a55:36ff:fe0e:d07e")
        self.assertEqual(DNS.normalize_bindings(actual), DNS.normalize_bindings(expected))
        actual["53/udp"].pop()
        self.assertNotEqual(DNS.normalize_bindings(actual), DNS.normalize_bindings(expected))

    def test_existing_unpublished_compose_is_upgraded_only_when_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            with patch.object(DNS, "WORK", work), \
                    patch.object(DNS.apps, "capture", side_effect=["id", "true"]):
                (work / "compose.json").write_text(json.dumps(DNS.compose_config("image", False)))
                (work / "Corefile").write_text(DNS.corefile())
                with self.assertRaisesRegex(RuntimeError, "Stop the DNS"):
                    DNS.prepare("image")
                self.assertNotIn("ports", json.loads(
                    (work / "compose.json").read_text())["services"]["dns"])
            with patch.object(DNS, "WORK", work), \
                    patch.object(DNS.apps, "capture", side_effect=["id", "false"]):
                DNS.prepare("image")
                self.assertEqual(json.loads((work / "compose.json").read_text()),
                                 DNS.compose_config("image"))

    def test_primary_addresses_and_mirror_ipv6_suppression_on_both_transports(self):
        text = DNS.corefile()
        for domain in DNS.DOMAINS:
            self.assertEqual(text.count(DNS.IPV4 + " " + domain + "\n"), 2)
            self.assertEqual(text.count(DNS.IPV6 + " " + domain + "\n"), 2)
        self.assertEqual(text.count("template IN AAAA www16.mxtracks.info dev16.mxtracks.info"), 2)
        self.assertEqual(text.count("forward . 192.168.178.1"), 2)
        self.assertNotIn("192.168.178.129", text)
        self.assertNotIn("copy.mxtracks", text)
        self.assertNotIn(DNS.IPV4 + " nextcloud.mxtracks.info", text)
        self.assertIn("tls://.:853", text)

    def test_probe_verifies_tls_and_dns_response_identity(self):
        compile(DNS.PROBE, "<dns-probe>", "exec")
        self.assertIn("ssl.create_default_context()", DNS.PROBE)
        self.assertIn("server_hostname=dot_host", DNS.PROBE)
        self.assertIn("ident != identifier", DNS.PROBE)
        self.assertIn("flags & 15", DNS.PROBE)
        self.assertIn("('udp', 'tcp', 'tls')", DNS.PROBE)
        self.assertIn("example.org", DNS.PROBE)
        self.assertNotIn("CERT_NONE", DNS.PROBE)


if __name__ == "__main__":
    unittest.main()
