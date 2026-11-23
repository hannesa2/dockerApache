import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "owncloud/backintime-beelink.sh"

FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
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
    print(("wrong-uuid" if os.environ.get("TEST_FAIL") == "mount" else
           "fe91ecb5-c1a4-4e6c-9edf-f3c9c75cffe1") if "/datenBackup" in args
          else "c0c42461-ae4c-4edb-a993-bba94a05ed6c")
elif name == "stat":
    print("0:700")
elif name == "curl":
    print(json.dumps({"installed": True, "maintenance": os.environ.get("TEST_UNHEALTHY") == "1"}))
elif name == "docker":
    if args[0] == "inspect" and "-f" in args:
        template = args[args.index("-f") + 1]
        if ".State.Running" in template:
            print("false" if (root / "stopped").exists() and args[-1] == "web-id" else "true")
        elif ".Config.Labels" in template:
            print("beelink-apps")
        elif '"/mnt/data/files"' in template:
            print(root / "data")
        elif '"/mnt/data"' in template:
            print(root / "volume")
        elif ".Image" in template:
            print("sha256:example")
        else:
            print("db-id" if "databases-owncloud" in args[-1] else "web-id")
    elif args[0] == "stop":
        (root / "stopped").touch()
    elif args[0] == "start":
        (root / "stopped").unlink(missing_ok=True)
    elif "pg_dump" in args:
        if os.environ.get("TEST_FAIL") == "dump":
            sys.exit(1)
        print("database archive")
    elif args[0] == "inspect" or args[:2] == ["image", "inspect"]:
        print("{}")
elif name == "backintime":
    if "check-config" in args and os.environ.get("TEST_FAIL") == "config":
        sys.exit(1)
    if "backup" in args:
        if os.environ.get("TEST_FAIL") == "backup":
            sys.exit(1)
        snapshot = root / "destination/backintime/beelink/root/1/20260101-000000-001"
        for source in (root / "data", root / "volume/apps", root / "volume/config", root / "stage"):
            target = snapshot / "backup" / str(source).lstrip("/")
            shutil.copytree(source, target, dirs_exist_ok=True)
        if os.environ.get("TEST_FAIL") == "missing":
            (snapshot / "backup" / str(root / "stage/owncloud.dump").lstrip("/")).unlink()
    elif "show" in args:
        print(root / "destination/backintime/beelink/root/1/20260101-000000-001")
'''


class OwncloudBackintimeTest(unittest.TestCase):
    def run_script(self, failure=None, unhealthy=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for path in ("data", "volume/apps", "volume/config", "stage", "destination", "bin", "run"):
                (root / path).mkdir(parents=True, exist_ok=True)
            (root / "data/.ocdata").touch()
            (root / "volume/config/config.php").touch()
            fake = root / "bin/fake"
            fake.write_text(FAKE)
            fake.chmod(0o755)
            for command in ("id", "hostname", "findmnt", "stat", "curl", "docker",
                            "backintime", "flock", "systemctl"):
                (root / "bin" / command).symlink_to(fake)
            source = SCRIPT.read_text()
            source = source.replace(
                "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                f'export PATH="{root}/bin:$PATH"')
            for old, new in (
                ("/daten/beelink-runtime/rootfs/daten/owncloud/data", root / "data"),
                ("/daten/beelink-runtime/apps/owncloud/volume", root / "volume"),
                ("/daten/beelink-backups/backintime-owncloud", root / "stage"),
                ("/datenBackup/owncloud-root", root / "destination"),
                ("/run/lock/owncloud-mirror-sync.lock", root / "run/owncloud.lock"),
                ("/run/lock/beelink-staging-control.lock", root / "run/lifecycle.lock"),
                ("/run/owncloud-backintime-config.XXXXXXXX", root / "run/config.XXXXXXXX"),
                ("ls -ld /daten/. /datenBackup/.", f"ls -ld {root}/data/. {root}/destination/."),
            ):
                source = source.replace(old, str(new))
            script = root / "script.sh"
            script.write_text(source)
            result = subprocess.run(
                ["bash", str(script), "--backup"], capture_output=True, text=True,
                env={**os.environ, "TEST_ROOT": temp, "TEST_FAIL": failure or "",
                     "TEST_UNHEALTHY": "1" if unhealthy else "0"})
            events = (root / "events").read_text()
            self.assertFalse((root / "run/owncloud.lock").exists())
            self.assertFalse((root / "stopped").exists())
            return result, events

    def test_success_and_recovery(self):
        result, events = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(events.index("check-config --no-crontab"), events.index("maintenance:mode --on"))
        self.assertLess(events.index("docker stop"), events.index("pg_dump"))
        self.assertLess(events.index("pg_dump"), events.index(" backup\n"))
        self.assertIn("docker start web-id", events)
        self.assertIn("maintenance:mode --off", events)

    def test_failures_restore_service(self):
        for failure in ("dump", "backup", "missing"):
            with self.subTest(failure=failure):
                result, events = self.run_script(failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("docker start web-id", events)
                self.assertIn("maintenance:mode --off", events)

    def test_existing_maintenance_does_not_change_service(self):
        result, events = self.run_script(unhealthy=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("maintenance:mode --on", events)
        self.assertNotIn("docker stop", events)

    def test_preflight_failure_does_not_change_service(self):
        for failure in ("mount", "config"):
            with self.subTest(failure=failure):
                result, events = self.run_script(failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("maintenance:mode --on", events)
                self.assertNotIn("docker stop", events)


if __name__ == "__main__":
    unittest.main()
