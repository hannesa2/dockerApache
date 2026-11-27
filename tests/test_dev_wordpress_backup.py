import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "owncloud/backup-dev-wordpress-beelink.sh"
FAKE = r'''#!/usr/bin/env python3
import os
from pathlib import Path
import shutil
import sys
root = Path(os.environ["TEST_ROOT"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "events").open("a") as out:
    out.write(name + " " + " ".join(args) + "\n")
if name == "id":
    print("0")
elif name == "hostname":
    print("beelink")
elif name == "stat":
    print("0:700")
elif name == "findmnt":
    print("fe91ecb5-c1a4-4e6c-9edf-f3c9c75cffe1" if "/datenBackup" in args
          else "c0c42461-ae4c-4edb-a993-bba94a05ed6c")
elif name == "docker":
    if "mysql" in args:
        print("15\t1" if os.environ.get("TEST_FAIL") == "engine" else "15\t0")
    elif "mysqldump" in args:
        if os.environ.get("TEST_FAIL") == "dump":
            sys.exit(1)
        print("-- SQL dump\nCREATE TABLE test (id int);")
    elif any("/var/www/html" in arg for arg in args):
        print("/daten/beelink-runtime/wordpress/www")
    else:
        print("true")
elif name == "backintime":
    history = root / "destination/backintime/beelink/root/1"
    if "backup" in args:
        snap = history / "20261127-230000-001"
        for source in (root / "site", root / "stage"):
            shutil.copytree(source, snap / "backup" / str(source).lstrip("/"),
                            dirs_exist_ok=True)
        if os.environ.get("TEST_FAIL") == "snapshot":
            (snap / "backup" / str(root / "stage/wordpress.sql.gz").lstrip("/")).unlink()
        (history / "last_snapshot").symlink_to(snap.name)
    elif "show" in args:
        print(history / "20261127-230000-001")
'''


class DevWordpressBackupTest(unittest.TestCase):
    def run_script(self, failure=""):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for path in ("site/wordpress", "stage", "destination", "bin", "run"):
                (root / path).mkdir(parents=True)
            (root / "site/wordpress/wp-config.php").write_text("private")
            fake = root / "bin/fake"
            fake.write_text(FAKE)
            fake.chmod(0o755)
            for command in ("id", "hostname", "findmnt", "stat", "docker",
                            "curl", "backintime", "flock"):
                (root / "bin" / command).symlink_to(fake)
            source = SCRIPT.read_text().replace(
                "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                f'export PATH="{root}/bin:$PATH"')
            for old, new in (
                ("/daten/beelink-runtime/wordpress/www/dev.mxtracks", root / "site"),
                ("/daten/beelink-backups/backintime-dev-wordpress", root / "stage"),
                ("/datenBackup/dev-wordpress-root", root / "destination"),
                ("/run/lock/owncloud-backintime-online.lock", root / "run/lock"),
                ("/run/dev-wordpress-backintime.XXXXXXXX", root / "run/config.XXXXXXXX"),
            ):
                source = source.replace(old, str(new))
            source = source.replace("100 * 1024**3 + size * 3 // 2 + 1024**3",
                                    "size * 3 // 2")
            script = root / "script.sh"
            script.write_text(source)
            result = subprocess.run(["bash", str(script), "--backup"], capture_output=True,
                                    text=True, env={**os.environ, "TEST_ROOT": temp,
                                                    "TEST_FAIL": failure})
            events = (root / "events").read_text()
            return result, events

    def test_creates_online_snapshot_without_stopping_services(self):
        result, events = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Verified online dev snapshot", result.stdout)
        self.assertIn("--single-transaction", events)
        self.assertNotIn("docker stop", events)
        self.assertNotIn("maintenance", events)

    def test_invalid_engine_or_dump_never_runs_snapshot(self):
        for failure in ("engine", "dump"):
            with self.subTest(failure=failure):
                result, events = self.run_script(failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("backintime ", events)

    def test_missing_dump_in_snapshot_is_rejected(self):
        result, _ = self.run_script("snapshot")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing required content", result.stderr)


if __name__ == "__main__":
    unittest.main()
