import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    "beelink_dns", Path(__file__).resolve().parents[1] / "monitoring/start-beelink-dns.py")
DNS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DNS)


class BeelinkDnsTest(unittest.TestCase):
    def test_dns_has_no_host_ports_or_host_network(self):
        config = DNS.compose_config("image")
        service = config["services"]["dns"]
        self.assertEqual(service["restart"], "no")
        self.assertTrue(service["read_only"])
        self.assertEqual(service["networks"], ["dns_test"])
        for key in ("ports", "network_mode", "privileged"):
            self.assertNotIn(key, service)
        for mount in service["volumes"]:
            self.assertTrue(mount["read_only"])
            self.assertFalse(mount["bind"]["create_host_path"])

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
