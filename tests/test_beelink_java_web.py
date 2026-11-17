import importlib.util
import json
import tempfile
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "beelink_java_web", ROOT / "monitoring/start-beelink-java-web.py")
WEB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WEB)


class BeelinkJavaWebTest(unittest.TestCase):
    def test_isolated_compose_uses_complete_java_images_and_original_app_paths(self):
        config = WEB.compose_config("sha256:apache", {"MYSQL_HOST": "production"},
                                    {8: "sha256:java8", 11: "sha256:java11"})
        self.assertEqual(config["name"], "beelink-java-web")
        for service in config["services"].values():
            self.assertEqual(service["networks"], ["isolated"])
            self.assertEqual(service["restart"], "no")
            for key in ("ports", "network_mode", "privileged"):
                self.assertNotIn(key, service)
            for mount in service["volumes"]:
                self.assertTrue(mount["source"].startswith("/daten/beelink-runtime/"))
                self.assertFalse(mount["bind"]["create_host_path"])
                self.assertNotIn("docker.sock", mount["source"])
        glassfish = config["services"]["glassfish"]
        self.assertEqual(glassfish["environment"]["AS_JAVA"], WEB.JAVA_HOME)
        self.assertEqual(glassfish["image"], "sha256:java8")
        self.assertEqual(glassfish["entrypoint"], [WEB.ASADMIN])
        self.assertEqual(config["services"]["wildfly"]["image"], "sha256:java11")
        self.assertTrue(next(m for m in glassfish["volumes"]
                             if m["target"] == "/srv/postgres/data/mxinfo")["read_only"])
        self.assertEqual(config["services"]["apache"]["environment"]["MYSQL_HOST"],
                         "beelink-staging-apache-mysql-1")

    def test_patch_jdbc_without_changing_credentials_or_other_pools(self):
        source = f"""<domain><jdbc-connection-pool name="{WEB.POOL}">
<property name="ServerName" value="localhost"/>
<property name="Url" value="jdbc:postgresql://localhost:5432/MXServer"/>
<property name="PortNumber" value="0"/>
<property name="DatabaseName" value="MXServer"/>
<property name="User" value="hannes"/><property name="Password" value="fixture"/>
</jdbc-connection-pool><jdbc-connection-pool name="DerbyPool">
<property name="ServerName" value="localhost"/></jdbc-connection-pool></domain>"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "domain.xml"
            path.write_text(source)
            path.chmod(0o640)
            WEB.patch_glassfish(path)
            tree = ET.parse(path)
            properties = {p.get("name"): p.get("value")
                          for p in tree.find("jdbc-connection-pool").findall("property")}
            self.assertEqual(properties["ServerName"], "beelink-staging-mxserver-1")
            self.assertEqual(properties["PortNumber"], "5432")
            self.assertEqual(properties["Password"], "fixture")
            self.assertEqual(properties["User"], "hannes")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(tree.findall("jdbc-connection-pool")[1][0].get("value"), "localhost")
            # Applying the same patch again is safe.
            WEB.patch_glassfish(path)

    def test_rejects_unexpected_jdbc_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "domain.xml"
            path.write_text("<domain/>")
            with self.assertRaisesRegex(RuntimeError, "production MXServer"):
                WEB.patch_glassfish(path)
            self.assertEqual(path.read_text(), "<domain/>")

    def test_only_stopped_exact_old_launcher_configuration_is_upgraded(self):
        desired = WEB.compose_config("apache", {}, {8: "java8", 11: "java11"})
        previous = json.loads(json.dumps(desired))
        previous["services"]["glassfish"]["entrypoint"] = ["/opt/glassfish-4.0/bin/asadmin"]
        with patch.object(WEB.apps, "capture", side_effect=["id", "false", "", ""]):
            WEB.upgrade_launcher(previous, desired)
        with patch.object(WEB.apps, "capture", side_effect=["id", "true"]):
            with self.assertRaisesRegex(RuntimeError, "Stop Java/web"):
                WEB.upgrade_launcher(previous, desired)
        previous["services"]["glassfish"]["networks"] = ["production"]
        with self.assertRaisesRegex(RuntimeError, "configuration differs"):
            WEB.upgrade_launcher(previous, desired)

    def test_removes_only_known_java_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "asadmin"
            path.write_text('JAVA=${AS_JAVA}/bin/java\nJAVA=/opt/jdk1.8.0_201/bin/java\nexec "$JAVA" "$@"\n')
            path.chmod(0o755)
            WEB.patch_launcher(path)
            self.assertIn("JAVA=${AS_JAVA}/bin/java", path.read_text())
            self.assertNotIn("/opt/jdk1.8.0_201", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            with self.assertRaisesRegex(RuntimeError, "Unexpected GlassFish"):
                WEB.patch_launcher(path)

    def test_jdbc_probe_uses_application_java_and_driver_without_admin_tls(self):
        with patch.object(WEB.apps, "run") as run:
            WEB.verify_jdbc()
        command = run.call_args.args[-1]
        self.assertIn('"$JAVA_HOME/bin/javac"', command)
        self.assertIn("domains/domain1/lib/*", command)
        self.assertEqual(run.call_args.kwargs["input"], WEB.JDBC_PROBE)
        self.assertIn("beelink-staging-mxserver-1:5432/MXServer", WEB.JDBC_PROBE)
        self.assertIn("public.tracks", WEB.JDBC_PROBE)
        self.assertNotIn("disabledAlgorithms", command)

    def test_retired_copy_apache_is_not_started_or_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work / "copy-sites-retired").touch()
            deployments = {"outcome": "success", "result": {
                name: {"enabled": True, "status": "OK"}
                for name in ("camera-rs.war", "jboss-kitchensink.war")}}
            with patch.object(WEB, "WORK", work), \
                    patch.object(WEB.apps, "run") as run, \
                    patch.object(WEB.apps, "wait_http"), \
                    patch.object(WEB.apps, "capture", return_value=json.dumps(deployments)), \
                    patch.object(WEB, "verify_jdbc"), \
                    patch.object(WEB, "verify_copy_sites") as copy_sites:
                WEB.verify()
            self.assertEqual(run.call_args_list[0].args,
                             (*WEB.COMPOSE, "up", "-d", "glassfish", "wildfly"))
            copy_sites.assert_not_called()
            self.assertIn("Copy sites retired",
                          json.loads((work / "java-web-report.json").read_text())["apache"])


if __name__ == "__main__":
    unittest.main()
