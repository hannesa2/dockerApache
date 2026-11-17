import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import json


SPEC = importlib.util.spec_from_file_location(
    "java_lan", Path(__file__).resolve().parents[1] / "monitoring/publish-beelink-java-lan.py")
LAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAN)


class JavaLanTest(unittest.TestCase):
    def test_sockets_bind_only_lan_interface_after_firewall_and_production(self):
        for port in LAN.PORTS:
            units = LAN.definitions(port)
            socket = units[f"beelink-java-lan-{port}.socket"]
            self.assertIn(f"ListenStream=192.168.178.142:{port}", socket)
            self.assertIn("BindToDevice=enp1s0", socket)
            self.assertIn("DefaultDependencies=no", socket)
            self.assertIn("Before=shutdown.target", socket)
            self.assertIn("After=beelink-production.service daten.mount "
                          "beelink-java-lan-firewall.service", socket)
            self.assertNotIn("ListenStream=0.0.0.0", socket)
            self.assertNotIn("[::]", socket)
            service = units[f"beelink-java-lan-{port}.service"]
            self.assertIn(f"--forward {port}", service)
            self.assertIn("BindsTo=beelink-production.service daten.mount", service)

    def test_firewall_scopes_drops_to_four_ports_and_permits_only_lan_or_loopback(self):
        rules = LAN.firewall_rules()
        self.assertIn("policy accept;", rules)
        self.assertIn('iifname "enp1s0" ip saddr 192.168.178.0/24 accept', rules)
        self.assertIn('iifname "lo" accept', rules)
        scope = "ip daddr 192.168.178.142 tcp dport { 8080, 9990, 8081, 4848 }"
        self.assertEqual(rules.count(scope), 3)
        self.assertIn(scope + " counter drop", rules)
        self.assertNotIn("flush ruleset", rules)
        self.assertNotIn("ufw", rules)
        self.assertLess(rules.index("ip saddr"), rules.index("counter drop"))

    def test_firewall_validates_before_applying_and_boot_unit_precedes_listeners(self):
        with patch.object(LAN, "guard"), patch.object(LAN, "run") as run:
            LAN.firewall()
        self.assertEqual(run.call_args_list[0].args,
                         ("/usr/sbin/nft", "--check", "--file", "-"))
        self.assertEqual(run.call_args_list[1].args, ("/usr/sbin/nft", "--file", "-"))
        self.assertIn("RemainAfterExit=yes", LAN.firewall_definition())

    def test_management_requires_unauthenticated_401(self):
        with patch.object(LAN, "capture", side_effect=["200", "401", "404", "401"]):
            LAN.probe({port: LAN.LAN for port in LAN.PORTS})
        with patch.object(LAN, "capture", side_effect=["200", "200"]):
            with self.assertRaisesRegex(RuntimeError, "9990"):
                LAN.probe({port: LAN.LAN for port in LAN.PORTS})

    def test_target_is_resolved_from_running_container_and_expected_network(self):
        info = {"State": {"Running": True}, "NetworkSettings": {"Networks": {
            "beelink-databases_isolated": {"IPAddress": "172.19.0.11"}}}}
        with patch.object(LAN, "capture", return_value=json.dumps([info])):
            self.assertEqual(LAN.target(4848), "172.19.0.11")
        info["NetworkSettings"]["Networks"]["beelink-databases_isolated"]["IPAddress"] = "8.8.8.8"
        with patch.object(LAN, "capture", return_value=json.dumps([info])):
            with self.assertRaisesRegex(RuntimeError, "network address"):
                LAN.target(4848)

    def test_forward_drops_root_before_exec_with_dynamic_target(self):
        with patch.object(LAN, "guard"), patch.object(LAN, "target", return_value="172.19.0.10"), \
                patch.object(LAN, "run"), \
                patch.object(LAN.os, "setgroups") as groups, \
                patch.object(LAN.os, "setgid") as gid, \
                patch.object(LAN.os, "setuid") as uid, \
                patch.object(LAN.os, "execv") as execute:
            LAN.forward(9990)
        groups.assert_called_once_with([])
        gid.assert_called_once()
        uid.assert_called_once()
        execute.assert_called_once_with(
            "/usr/lib/systemd/systemd-socket-proxyd",
            ["/usr/lib/systemd/systemd-socket-proxyd", "172.19.0.10:9990"])


if __name__ == "__main__":
    unittest.main()
