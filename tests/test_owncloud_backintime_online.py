import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "owncloud/backintime-online-beelink.sh"
FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ["TEST_ROOT"])
with (root / "events").open("a") as out:
    out.write(name + " " + " ".join(args) + "\n")
if name == "id":
    print("0")
elif name == "hostname":
    print("beelink")
elif name == "findmnt":
    print("fe91ecb5-c1a4-4e6c-9edf-f3c9c75cffe1" if "/datenBackup" in args
          else "c0c42461-ae4c-4edb-a993-bba94a05ed6c")
elif name == "stat":
    print("0:700")
elif name == "systemctl":
    sys.exit(0 if "beelink-production.service" in args else 3)
elif name == "curl":
    print(json.dumps({"installed": True, "maintenance": os.environ.get("TEST_UNHEALTHY") == "1"}))
elif name == "docker":
    template = args[args.index("-f") + 1]
    if ".State.Running" in template:
        print("true")
    elif ".Config.Labels" in template:
        print("beelink-apps")
    elif '"/mnt/data/files"' in template:
        print(root / "data")
    else:
        print(root / "volume")
'''


class OnlineBackupTest(unittest.TestCase):
    def run_script(self, marker=True, unhealthy=False, scheduled=False, fresh=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for path in ("data", "volume/apps", "volume/config", "stage",
                         "destination/backintime/beelink/root/1/new_snapshot/backup", "bin", "run"):
                (root / path).mkdir(parents=True, exist_ok=True)
            (root / "data/.ocdata").touch()
            (root / "volume/config/config.php").touch()
            if marker:
                (root / "destination/backintime/beelink/root/1/new_snapshot/save_to_continue").touch()
            if fresh:
                pending = root / "destination/backintime/beelink/root/1/new_snapshot"
                (pending / "save_to_continue").unlink(missing_ok=True)
                (pending / "backup").rmdir()
                pending.rmdir()
            fake = root / "bin/fake"
            fake.write_text(FAKE)
            fake.chmod(0o755)
            for command in ("id", "hostname", "findmnt", "stat", "curl", "docker",
                            "backintime", "flock", "systemctl"):
                (root / "bin" / command).symlink_to(fake)
            source = SCRIPT.read_text().replace(
                "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                f'export PATH="{root}/bin:$PATH"')
            for old, new in (
                ("/daten/beelink-runtime/rootfs/daten/owncloud/data", root / "data"),
                ("/daten/beelink-runtime/apps/owncloud/volume", root / "volume"),
                ("/daten/beelink-backups/backintime-owncloud", root / "stage"),
                ("/datenBackup/owncloud-root", root / "destination"),
                ("/run/lock/owncloud-backintime-online.lock", root / "run/lock"),
                ("/run/owncloud-backintime-online.XXXXXXXX", root / "run/config.XXXXXXXX"),
                ("ls -ld /daten/. /datenBackup/.", f"ls -ld {root}/data/. {root}/destination/."),
            ):
                source = source.replace(old, str(new))
            script = root / "script.sh"
            script.write_text(source)
            result = subprocess.run(
                ["bash", str(script), "--scheduled" if scheduled else "--backup"],
                capture_output=True, text=True,
                env={**os.environ, "TEST_ROOT": temp, "TEST_UNHEALTHY": "1" if unhealthy else "0"})
            return result, (root / "events").read_text()

    def test_runs_without_stopping_or_locking_production(self):
        result, events = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check-config --no-crontab", events)
        self.assertIn(" backup\n", events)
        self.assertNotIn("docker stop", events)
        self.assertNotIn("maintenance:mode", events)
        self.assertIn("not a consistent or restorable backup", result.stdout)
        self.assertNotIn("/run/lock/beelink-staging-control.lock", SCRIPT.read_text())

    def test_missing_marker_or_unhealthy_server_refuses_backup(self):
        for kwargs in ({"marker": False}, {"unhealthy": True}):
            with self.subTest(kwargs=kwargs):
                result, events = self.run_script(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("backintime ", events)

    def test_scheduled_mode_can_create_fresh_preliminary(self):
        result, events = self.run_script(scheduled=True, fresh=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" backup\n", events)
        self.assertNotIn("maintenance:mode", events)

    def test_scheduled_mode_preserves_unresumable_pending(self):
        result, events = self.run_script(scheduled=True, marker=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("backintime ", events)


if __name__ == "__main__":
    unittest.main()
