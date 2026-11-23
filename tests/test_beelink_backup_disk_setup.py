from pathlib import Path
import subprocess
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "monitoring/setup-beelink-backup-disk.sh"


class BackupDiskSetupTest(unittest.TestCase):
    def test_requires_explicit_existing_disk_argument(self):
        result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--configure-existing", result.stderr)

    def test_preserves_filesystem_and_protects_boot(self):
        source = SCRIPT.read_text()
        for destructive in ("luksFormat", "wipefs", "mkfs", "luksErase", "luksKillSlot"):
            self.assertNotIn(destructive, source)
        self.assertIn("cryptsetup luksAddKey", source)
        self.assertIn("cryptsetup open --test-passphrase", source)
        self.assertIn("luksHeaderBackup", source)
        self.assertIn("luks,noauto,nofail,headless", source)
        self.assertIn("nofail,x-systemd.automount", source)
        self.assertIn("WD-WXG1EA84H743", source)
        self.assertIn("2000365289472", source)
        self.assertIn('$(readlink -f "$backing")', source)
        self.assertIn('findmnt -rn -M /datenBackup -t ext4 -o UUID', source)
        self.assertLess(source.index('cryptsetup open --test-passphrase'),
                        source.index('>> /etc/crypttab'))


if __name__ == "__main__":
    unittest.main()
