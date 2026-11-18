import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "beelink_artifacts", Path(__file__).resolve().parents[1] /
    "monitoring/start-beelink-artifacts.py")
ART = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ART)
ROOT = Path(__file__).resolve().parents[1]


class BeelinkArtifactsTest(unittest.TestCase):
    def test_artifacts_are_internal_with_only_the_data_mount_writable(self):
        config = ART.compose_config("image")
        service = config["services"]["artifacts"]
        self.assertEqual(service["networks"], ["isolated"])
        self.assertEqual(service["user"], "10001:10001")
        self.assertEqual(service["restart"], "no")
        self.assertTrue(service["read_only"])
        self.assertEqual(service["tmpfs"], ["/tmp"])
        for key in ("ports", "network_mode", "privileged"):
            self.assertNotIn(key, service)
        files, parameters = service["volumes"]
        self.assertEqual(files["target"], "/app/files")
        self.assertFalse(files["read_only"])
        self.assertEqual(parameters["target"], "/app/parameter.yml")
        self.assertTrue(parameters["read_only"])
        for mount in service["volumes"]:
            self.assertTrue(mount["source"].startswith("/daten/beelink-runtime/artifacts/"))
            self.assertFalse(mount["bind"]["create_host_path"])

    def test_config_preserves_auth_and_changes_only_artifact_directory(self):
        self.assertIn("config['file']['directory'] = '/app/files'", ART.CONFIGURE)
        self.assertIn("'/daten/githubArtifactsPy'", ART.CONFIGURE)
        self.assertIn("'/github'", ART.CONFIGURE)
        self.assertIn("path.chmod(0o600)", ART.CONFIGURE)
        self.assertNotIn("print(config", ART.CONFIGURE)
        self.assertNotIn("config['authentication'] =", ART.CONFIGURE)
        compile(ART.CONFIGURE, "<configure>", "exec")
        compile(ART.CHECK_AUTH, "<auth-check>", "exec")
        compile(ART.CHECK_UPLOAD, "<upload-check>", "exec")

    def test_upload_probe_requires_auth_and_cleans_up_its_artifact(self):
        self.assertIn("authenticated upload", ART.CHECK_UPLOAD)
        self.assertIn("if error.code != 401", ART.CHECK_UPLOAD)
        self.assertIn("5000/delete", ART.CHECK_UPLOAD)
        self.assertIn("upload-safety.patch", (ROOT / "github-artifacts/Dockerfile").read_text())

    def test_upload_patch_confines_mutations_and_protects_file_reads(self):
        text = (ROOT / "github-artifacts/upload-safety.patch").read_text()
        self.assertIn("def artifact_path(name):", text)
        self.assertIn("@basic_auth_required\n def get():", text)
        self.assertIn("old_path.rename(new_path)", text)
        self.assertIn("path.unlink()", text)
        self.assertIn("job = secure_filename(request.form[\"job\"])", text)

    def test_existing_readonly_service_migrates_to_new_image_only_when_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            with patch.object(ART, "WORK", work):
                desired = ART.compose_config("new-image")
                previous = json.loads(json.dumps(desired))
                previous["services"]["artifacts"]["image"] = "old-image"
                previous["services"]["artifacts"]["volumes"][0]["read_only"] = True
                (work / "compose.json").write_text(json.dumps(previous))
                (work / "prepared").touch()
                (work / "files").mkdir()
                (work / "files" / "existing").write_text("unchanged")
                (work / "parameter.yml").write_text("private")
                with patch.object(ART.apps, "capture", side_effect=["id", "true"]):
                    with self.assertRaisesRegex(RuntimeError, "Stop artifacts"):
                        ART.prepare("new-image")
                with patch.object(ART.apps, "capture", side_effect=["id", "false"]):
                    ART.prepare("new-image")
                self.assertEqual(json.loads((work / "compose.json").read_text()), desired)
                self.assertEqual((work / "files" / "existing").read_text(), "unchanged")
                self.assertEqual((work / "parameter.yml").read_text(), "private")

    def test_artifact_proxy_precedes_wordpress_and_preserves_prefix(self):
        text = ART.frontend.vhosts("172.19.0.1", artifacts=True)
        www = text.split("ServerName www.mxtracks.info\n")[-1].split("</VirtualHost>")[0]
        self.assertIn('RequestHeader set X-Forwarded-Prefix "/github"', www)
        self.assertNotIn(ART.frontend.blocked("/github"), www)
        self.assertLess(www.index("ProxyPass /github/"), www.index("ProxyPass / http"))
        self.assertIn(ART.frontend.blocked("/nexus"), www)

    def test_exact_existing_frontend_is_upgraded_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            work = runtime / "frontend"
            (work / "vhosts").mkdir(parents=True)
            (runtime / "artifacts").mkdir()
            (runtime / "artifacts/prepared").touch()
            front = ART.frontend
            settings = {"frontend_ip": "172.19.0.15", "gateway": "172.19.0.1"}
            with patch.object(front, "WORK", work), \
                    patch.object(front.apps, "RUNTIME", runtime), \
                    patch.object(front, "homeassistant_settings", return_value=settings):
                config = front.compose_config("image", settings["frontend_ip"])
                (work / "compose.json").write_text(json.dumps(config))
                (work / "vhosts/frontend.conf").write_text(front.vhosts(settings["gateway"]))
                front.prepare("image")
                front.prepare("image")
                self.assertEqual((work / "vhosts/frontend.conf").read_text(),
                                 front.vhosts(settings["gateway"], artifacts=True))
            self.assertTrue((work / "frontend-before-artifacts.conf").is_file())


if __name__ == "__main__":
    unittest.main()
