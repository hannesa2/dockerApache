import importlib.util
import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location(
    "beelink_frontend", Path(__file__).resolve().parents[1] /
    "monitoring/start-beelink-frontend.py")
FRONT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FRONT)
HA_CONFIG = """default_config:
automation: !include automations.yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 127.0.0.1
    - ::1
script: !include scripts.yaml
"""


class BeelinkFrontendTest(unittest.TestCase):
    def test_isolated_compose_exposes_neither_ports_nor_private_keys_writable(self):
        service = FRONT.compose_config("image")["services"]["frontend"]
        self.assertEqual(service["networks"], ["isolated"])
        self.assertEqual(service["restart"], "no")
        for key in ("ports", "network_mode", "privileged"):
            self.assertNotIn(key, service)
        cert = next(m for m in service["volumes"] if m["target"] == "/etc/letsencrypt")
        self.assertTrue(cert["read_only"])
        self.assertFalse(cert["bind"]["create_host_path"])

    def test_original_names_and_exact_proxy_order(self):
        text = FRONT.vhosts()
        for domain in FRONT.SITES:
            self.assertIn("ServerName " + domain, text)
            self.assertIn("https://" + domain + "/", text)
        www = text.split("ServerName www.mxtracks.info\n")[-1].split("</VirtualHost>")[0]
        self.assertLess(www.index("ProxyPass /owncloud/"), www.index("ProxyPass / http"))
        self.assertLess(www.index("ProxyPass /piwik/"), www.index("ProxyPass / http"))
        self.assertIn("ProxyPass /backend/ http://beelink-java-web-glassfish-1:8081/", www)
        self.assertNotIn("192.168.178.129", text)
        self.assertNotIn("localhost:808", text)
        self.assertNotIn("nextcloud", text)
        self.assertIn("AllowEncodedSlashes NoDecode", text)
        self.assertNotIn("copy.mxtracks.info", text)
        self.assertNotIn("beelink-java-web-apache-1", text)
        self.assertIn("Require all denied", text)

    def test_exact_paths_are_excluded_before_catch_all_proxy(self):
        text = FRONT.vhosts()
        for domain in ("www.mxtracks.info", "dev.mxtracks.info"):
            https = text.split(f"ServerName {domain}\n")[-1].split("</VirtualHost>")[0]
            self.assertLess(https.index("ProxyPassMatch ^/wordpress$ !"),
                            https.index("ProxyPass / http"))
            self.assertIn("RedirectMatch 301 ^/wordpress$ /wordpress/", https)
        www = text.split("ServerName www.mxtracks.info\n")[-1].split("</VirtualHost>")[0]
        self.assertLess(www.index("ProxyPassMatch ^/owncloud$ !"),
                        www.index("ProxyPass / http"))

    def test_redirect_only_update_checks_and_reloads_without_restarting(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work / "vhosts").mkdir()
            path = work / "vhosts/frontend.conf"
            expected = FRONT.vhosts()
            original = expected.replace("    ProxyPassMatch ^/owncloud$ !\n", "").replace(
                "    ProxyPassMatch ^/wordpress$ !\n"
                "    RedirectMatch 301 ^/wordpress$ /wordpress/\n", "")
            path.write_text(original)
            responses = [f"HTTP/1.1 301 Moved Permanently\nLocation: https://{domain}/{route}/\n"
                         for domain, route in (("www.mxtracks.info", "owncloud"),
                                               ("www.mxtracks.info", "wordpress"),
                                               ("dev.mxtracks.info", "wordpress"))]
            with patch.object(FRONT, "WORK", work), \
                    patch.object(FRONT, "homeassistant_settings", return_value=None), \
                    patch.object(FRONT.apps, "RUNTIME", work), \
                    patch.object(FRONT.apps, "run") as run, \
                    patch.object(FRONT.apps, "capture", side_effect=responses):
                FRONT.fix_path_redirects()
            self.assertEqual(path.read_text(), expected)
            self.assertEqual((work / "frontend-before-path-redirects.conf").read_text(), original)
            self.assertEqual([call.args[-1] for call in run.call_args_list],
                             ["configtest", "graceful"])

    def test_redirect_only_update_rolls_back_on_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work / "vhosts").mkdir()
            path = work / "vhosts/frontend.conf"
            original = FRONT.vhosts().replace("    ProxyPassMatch ^/owncloud$ !\n", "")
            original = original.replace(
                "    ProxyPassMatch ^/wordpress$ !\n"
                "    RedirectMatch 301 ^/wordpress$ /wordpress/\n", "")
            path.write_text(original)
            with patch.object(FRONT, "WORK", work), \
                    patch.object(FRONT, "homeassistant_settings", return_value=None), \
                    patch.object(FRONT.apps, "RUNTIME", work), \
                    patch.object(FRONT.apps, "run", side_effect=[
                        RuntimeError("invalid"), None, None]):
                with self.assertRaisesRegex(RuntimeError, "invalid"):
                    FRONT.fix_path_redirects()
            self.assertEqual(path.read_text(), original)

    def test_unmigrated_routes_fail_explicitly_instead_of_falling_into_wordpress(self):
        text = FRONT.vhosts()
        for route in FRONT.PENDING:
            self.assertIn(f'<LocationMatch "^{route}(?:/|$)">', text)
        self.assertIn("Redirect 503 /", text)
        self.assertIn("production cutover pending", text)
        self.assertNotIn("host-gateway", text)

    def test_owncloud_patch_guards_test_database_and_uses_exact_proxy_ip(self):
        self.assertIn("beelink-databases-owncloud-1", FRONT.OWNCLOUD_PROXY)
        self.assertIn("owncloud_stage", FRONT.OWNCLOUD_PROXY)
        self.assertIn("$CONFIG['trusted_proxies'] = [$argv[1]]", FRONT.OWNCLOUD_PROXY)
        self.assertIn("$CONFIG['overwritewebroot'] = '/owncloud'", FRONT.OWNCLOUD_PROXY)
        self.assertIn("!rename(", FRONT.OWNCLOUD_PROXY)

    def test_https_probe_verifies_hostname_and_chain_without_insecure_flag(self):
        with patch.object(FRONT.subprocess, "check_output",
                          return_value="content\n200") as capture:
            self.assertEqual(FRONT.response("www.mxtracks.info", "/wordpress/"),
                             ["content", "200"])
        command = capture.call_args.args[0]
        self.assertIn("--resolve", command)
        self.assertIn("www.mxtracks.info:443:127.0.0.1", command)
        self.assertNotIn("-k", command)
        self.assertNotIn("--insecure", command)
        self.assertNotIn("--location", command)

    def test_https_probe_preserves_empty_body_and_reports_invalid_output(self):
        with patch.object(FRONT.subprocess, "check_output", return_value="\n500"):
            self.assertEqual(FRONT.response("www.mxtracks.info", "/wordpress/"),
                             ["", "500"])
        with patch.object(FRONT.subprocess, "check_output", return_value="500"):
            with self.assertRaisesRegex(RuntimeError, "Malformed HTTP probe response"):
                FRONT.response("www.mxtracks.info", "/wordpress/")

    def test_matomo_only_adds_original_proxy_host_to_isolated_config(self):
        source = ('[database]\nhost = "beelink-databases-native-mysql-1"\n'
                  'password = "fixture"\n[General]\ntrusted_hosts[] = "localhost"\n'
                  'proxy_scheme_headers[] = "one"\nproxy_scheme_headers[] = "two"\n')
        result = FRONT.matomo_proxy_config(source)
        self.assertIn('trusted_hosts[] = "www.mxtracks.info"', result)
        self.assertIn('password = "fixture"', result)
        self.assertEqual(result.count("proxy_scheme_headers[]"), 2)
        self.assertEqual(FRONT.matomo_proxy_config(result), result)
        with self.assertRaises(RuntimeError):
            FRONT.matomo_proxy_config(source.replace("beelink-databases-native-mysql-1", "localhost"))

    def test_rejects_changed_existing_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work / "vhosts").mkdir()
            (work / "vhosts/frontend.conf").write_text(FRONT.vhosts())
            config = FRONT.compose_config("image")
            (work / "compose.json").write_text(json.dumps(config))
            with patch.object(FRONT, "WORK", work), \
                    patch.object(FRONT, "compose_config", return_value=config):
                FRONT.prepare("image")
                (work / "vhosts/frontend.conf").write_text("unmanaged change")
                with self.assertRaises(RuntimeError):
                    FRONT.prepare("image")

    def test_exact_previous_copy_configuration_is_retired_and_backed_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work / "vhosts").mkdir()
            old = FRONT.vhosts("172.19.0.1", legacy_copy_sites=True)
            (work / "vhosts/frontend.conf").write_text(old)
            with patch.object(FRONT, "WORK", work):
                config = FRONT.compose_config("image", "172.19.0.15")
            (work / "compose.json").write_text(json.dumps(config))
            settings = {"frontend_ip": "172.19.0.15", "gateway": "172.19.0.1"}
            with patch.object(FRONT, "WORK", work), \
                    patch.object(FRONT, "homeassistant_settings", return_value=settings):
                FRONT.prepare("image")
                FRONT.prepare("image")
            self.assertEqual((work / "vhosts/frontend.conf").read_text(),
                             FRONT.vhosts("172.19.0.1"))
            self.assertEqual((work / "frontend-before-copy-retirement.conf").read_text(), old)
            self.assertEqual(json.loads((work / "compose.json").read_text()), config)

    def test_homeassistant_trust_preserves_includes_and_is_idempotent(self):
        result = FRONT.patch_homeassistant_config(HA_CONFIG, "172.19.0.15")
        self.assertIn("    - ::1\n    - 172.19.0.15\nscript:", result)
        self.assertEqual(result.replace("    - 172.19.0.15\n", ""), HA_CONFIG)
        self.assertEqual(FRONT.patch_homeassistant_config(result, "172.19.0.15"), result)
        for text in (HA_CONFIG.replace("true", "false"),
                     HA_CONFIG.replace("http:\n", "http: !include http.yaml\n"),
                     HA_CONFIG.replace("127.0.0.1", "172.19.0.0/16"),
                     HA_CONFIG + "\n" + HA_CONFIG):
            with self.assertRaises(RuntimeError):
                FRONT.patch_homeassistant_config(text, "172.19.0.15")
        with self.assertRaises(ValueError):
            FRONT.patch_homeassistant_config(HA_CONFIG, "not-an-ip")

    def test_ha_vhost_supports_websocket_without_widening_network_access(self):
        config = FRONT.compose_config("image", "172.19.0.15")
        service = config["services"]["frontend"]
        self.assertEqual(service["networks"], {"isolated": {"ipv4_address": "172.19.0.15"}})
        self.assertNotIn("ports", service)
        text = FRONT.vhosts("172.19.0.1")
        ha = text.split("ServerName homeassistant.mxtracks.info\n")[-1]
        self.assertIn("http://172.19.0.1:8123/ upgrade=websocket", ha)
        self.assertNotIn("Redirect 503", ha)
        self.assertIn(FRONT.blocked("/github"), text)
        with self.assertRaises(ValueError):
            FRONT.vhosts("latitude")

    def test_ha_config_is_unchanged_unless_explicit_restart_flag_given(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "configuration.yaml"
            path.write_text(HA_CONFIG)
            mounts = [{"Destination": "/config", "Source": str(path.parent)}]
            with patch.object(FRONT, "HOMEASSISTANT_CONFIG", path), \
                    patch.object(FRONT.apps, "capture", return_value=json.dumps(mounts)), \
                    patch.object(FRONT.apps, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "brief restart"):
                    FRONT.configure_homeassistant({"frontend_ip": "172.19.0.15"}, False)
                run.assert_not_called()
            self.assertEqual(path.read_text(), HA_CONFIG)

    def test_ha_invalid_configuration_is_rolled_back_without_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            path = work / "configuration.yaml"
            path.write_text(HA_CONFIG)
            mounts = [{"Destination": "/config", "Source": str(path.parent)}]
            with patch.object(FRONT, "HOMEASSISTANT_CONFIG", path), \
                    patch.object(FRONT, "WORK", work), \
                    patch.object(FRONT.apps, "capture", return_value=json.dumps(mounts)), \
                    patch.object(FRONT.apps, "run", side_effect=RuntimeError("invalid")) as run:
                with self.assertRaisesRegex(RuntimeError, "invalid"):
                    FRONT.configure_homeassistant({"frontend_ip": "172.19.0.15"}, True)
                self.assertEqual(run.call_count, 1)
                self.assertIn("check_config", run.call_args.args)
            self.assertEqual(path.read_text(), HA_CONFIG)
            self.assertEqual((work / "homeassistant-before-proxy.yaml").read_text(), HA_CONFIG)

    def test_ha_configuration_validation_precedes_restart_and_retry_does_not_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            path = work / "configuration.yaml"
            path.write_text(HA_CONFIG)
            mounts = [{"Destination": "/config", "Source": str(path.parent)}]
            with patch.object(FRONT, "HOMEASSISTANT_CONFIG", path), \
                    patch.object(FRONT, "WORK", work), \
                    patch.object(FRONT.apps, "capture", return_value=json.dumps(mounts)), \
                    patch.object(FRONT.apps, "run") as run:
                FRONT.configure_homeassistant({"frontend_ip": "172.19.0.15"}, True)
                self.assertIn("check_config", run.call_args_list[0].args)
                self.assertEqual(run.call_args_list[1].args, ("docker", "restart", "homeassistant"))
                run.reset_mock()
                FRONT.configure_homeassistant({"frontend_ip": "172.19.0.15"}, True)
                run.assert_not_called()

    def test_websocket_probe_verifies_accept_hash_and_homeassistant_challenge(self):
        nonce = b"x" * 16
        key = base64.b64encode(nonce).decode()
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        body = b'{"type":"auth_required"}'
        response = (f"HTTP/1.1 101 Switching Protocols\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
        tcp = MagicMock()
        tls = MagicMock()
        stream = tls.wrap_socket.return_value.__enter__.return_value
        stream.makefile.return_value = io.BytesIO(response + bytes([0x81, len(body)]) + body)
        with patch("socket.create_connection", return_value=tcp), \
                patch("ssl.create_default_context", return_value=tls), \
                patch("os.urandom", return_value=nonce), \
                patch("sys.argv", ["probe", "172.19.0.15"]):
            exec(compile(FRONT.WEBSOCKET_CHECK, "<websocket-probe>", "exec"), {})
        self.assertEqual(tls.wrap_socket.call_args.kwargs["server_hostname"],
                         "homeassistant.mxtracks.info")
        self.assertIn(b"GET /api/websocket HTTP/1.1", stream.sendall.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
