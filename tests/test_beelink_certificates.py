import importlib.util
import io
from pathlib import Path
import tarfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "beelink_certificates", Path(__file__).resolve().parents[1] /
    "monitoring/prepare-beelink-certificates.py")
CERT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CERT)


class BeelinkCertificatesTest(unittest.TestCase):
    def test_accepts_expected_lineage_and_account_files(self):
        for name in (
            "etc/letsencrypt/live/www.mxtracks.info",
            "etc/letsencrypt/archive/www.mxtracks.info/fullchain1.pem",
            "etc/letsencrypt/renewal/www.mxtracks.info.conf",
            "etc/letsencrypt/accounts/acme-v02.api.letsencrypt.org/directory/account/private_key.json",
        ):
            CERT.validate_member(tarfile.TarInfo(name))

    def test_accepts_only_internal_matching_certificate_links(self):
        member = tarfile.TarInfo("etc/letsencrypt/live/www.mxtracks.info/fullchain.pem")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../archive/www.mxtracks.info/fullchain1.pem"
        CERT.validate_member(member)
        for target in ("/etc/passwd", "../../../passwd",
                       "../../archive/git.mxtracks.info-0001/fullchain1.pem"):
            member.linkname = target
            with self.assertRaises(RuntimeError):
                CERT.validate_member(member)

    def test_rejects_traversal_hooks_and_special_members(self):
        for name in ("/etc/passwd", "etc/letsencrypt/../../passwd",
                     "etc/letsencrypt/renewal-hooks/post/source-restart",
                     "etc/letsencrypt/renewal/www16.mxtracks.info.conf"):
            with self.assertRaises(RuntimeError):
                CERT.validate_member(tarfile.TarInfo(name))
        member = tarfile.TarInfo("etc/letsencrypt/accounts/device")
        member.type = tarfile.CHRTYPE
        with self.assertRaises(RuntimeError):
            CERT.validate_member(member)

    def test_accepts_only_known_legacy_acme_account_aliases(self):
        for server, original in CERT.ACCOUNT_LINKS.items():
            member = tarfile.TarInfo(f"etc/letsencrypt/accounts/{server}/directory")
            member.type = tarfile.SYMTYPE
            member.linkname = f"/etc/letsencrypt/accounts/{original}/directory"
            CERT.validate_member(member)
            for target in ("/etc/passwd", "../../outside",
                           "/etc/letsencrypt/accounts/unknown/directory"):
                member.linkname = target
                with self.assertRaises(RuntimeError):
                    CERT.validate_member(member)

    def test_account_links_are_relocated_and_require_existing_directory(self):
        for include_target in (True, False):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                target = tarfile.TarInfo(
                    "etc/letsencrypt/accounts/acme-v01.api.letsencrypt.org/directory")
                target.type = tarfile.DIRTYPE
                if include_target:
                    archive.addfile(target)
                link = tarfile.TarInfo(
                    "etc/letsencrypt/accounts/acme-v02.api.letsencrypt.org/directory")
                link.type = tarfile.SYMTYPE
                link.linkname = "/" + target.name
                archive.addfile(link)
            stream.seek(0)
            with tarfile.open(fileobj=stream) as archive:
                if include_target:
                    members = CERT.validated_members(archive)
                    self.assertEqual(members[-1].linkname,
                                     "../acme-v01.api.letsencrypt.org/directory")
                else:
                    with self.assertRaisesRegex(RuntimeError, "target is missing"):
                        CERT.validated_members(archive)

    def test_wrong_host_does_not_change_anything(self):
        with patch.object(CERT.os, "getuid", return_value=0), \
                patch.object(CERT.socket, "gethostname", return_value="Latitude"), \
                patch("sys.argv", ["prepare", "--source", "/unused"]):
            with self.assertRaisesRegex(RuntimeError, "directly on beelink"):
                CERT.main()


if __name__ == "__main__":
    unittest.main()
