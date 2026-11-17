import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "monitoring/prepare-latitude-certificate-cutover.sh"


class LatitudeCertificateCutoverTest(unittest.TestCase):
    def test_explicit_mode_required(self):
        result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--prepare-frozen", result.stderr)

    def test_other_host_rejected_before_services_or_files_are_changed(self):
        with tempfile.TemporaryDirectory() as temporary:
            for name, response in (("id", "0"), ("hostname", "beelink")):
                path = Path(temporary) / name
                path.write_text("#!/bin/sh\nprintf '%s\\n' '" + response + "'\n")
                path.chmod(0o755)
            result = subprocess.run(
                ["bash", str(SCRIPT), "--prepare-frozen"], capture_output=True, text=True,
                env=dict(os.environ, PATH=temporary + os.pathsep + os.environ["PATH"]))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("directly on Latitude", result.stderr)

    def test_renewal_and_privacy_guards(self):
        source = SCRIPT.read_text()
        self.assertIn("systemctl mask --runtime apache2.service", source)
        self.assertIn("systemctl is-active --quiet snap.certbot.renew.timer", source)
        self.assertIn("snap.certbot.renew.service -p ActiveState", source)
        self.assertIn("-checkend 604800", source)
        self.assertIn("sha256sum -c SHA256SUMS", source)
        self.assertIn("umask 077", source)
        self.assertIn("c0c42461-ae4c-4edb-a993-bba94a05ed6c", source)
        for forbidden in ("systemctl stop snap.certbot", "systemctl disable",
                          "certbot renew", "docker restart", "--delete"):
            self.assertNotIn(forbidden, source)

    def test_exports_only_required_lineages_and_accounts_not_old_hooks(self):
        source = SCRIPT.read_text()
        self.assertIn("etc/letsencrypt/accounts", source)
        self.assertIn("nextcloud.mxtracks.info-0002", source)
        self.assertIn('etc/letsencrypt/live/$name', source)
        self.assertIn('etc/letsencrypt/archive/$name', source)
        self.assertNotIn("etc/letsencrypt/renewal-hooks", source)
        self.assertNotIn("mac2016", source)


if __name__ == "__main__":
    unittest.main()
