#!/usr/bin/env python3
"""Exercise the receiver lifecycle with isolated Docker and disk-space mocks."""
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


RECEIVER = Path(__file__).with_name("mac2016-backup-receiver.sh")
BACKUP_ID = "git16dr_20261111T160000Z_19.4.1"
HARNESS = r'''
docker() { python3 "$MOCK_DOCKER" "$@"; }
function /bin/df() {
    printf 'Filesystem blocks used available capacity mounted\nmock 200000000 0 200000000 0%% /\n'
}
source "$1"
'''
MOCK_DOCKER = r'''
import os
from pathlib import Path
import subprocess
import sys

args = sys.argv[1:]
home = Path(os.environ["HOME"])
root = home / "server/gitlab"
workspace = root / "backups/.sync"
with (home / "calls").open("a") as log:
    log.write(" ".join(args) + "\n")
if args[0] == "inspect":
    if ".Config.Image" in args[2]:
        print("gitlab/gitlab-ce:19.4.1-ce.0 true")
    else:
        print(f"{root}/backups /gitlab-backup-archives")
        print(f"{root}/config /etc/gitlab")
        print(f"{root}/data /var/opt/gitlab")
    sys.exit(0)
if args[:2] != ["exec", "gitlab16"]:
    raise SystemExit("Unexpected mock Docker command")
command = args[2:]
command = [value.replace("/gitlab-backup-archives", str(root / "backups"))
           .replace("/etc/gitlab", str(root / "config")) for value in command]
if command[0] == "gitlab-rake":
    sys.exit(1 if os.environ.get("FAIL_VERIFY") else 0)
if command[0] == "gitlab-backup":
    if command[1] != "restore":
        raise SystemExit("Local backup creation must never be requested")
    sys.exit(42 if os.environ.get("FAIL_RESTORE") else 0)
if command[:2] == ["gitlab-ctl", "reconfigure"] and os.environ.get("FAIL_CONFIGURE"):
    secrets = (root / "config/gitlab-secrets.json").read_text()
    sys.exit(1 if "source" in secrets else 0)
if command[0] in ("gitlab-ctl", "chown", "curl"):
    sys.exit(0)
sys.exit(subprocess.run(command).returncode)
'''


def bundle(valid=True):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data in (
            (BACKUP_ID + "_gitlab_backup.tar", inner_archive()),
            ("gitlab.rb", b"# source configuration\n"),
            ("gitlab-secrets.json", b'{"test": "source"}\n'),
        ):
            entry = tarfile.TarInfo(name if valid else "../unexpected")
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return output.getvalue()


def inner_archive():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        data = b"gitlab_version: 19.4.1\n"
        entry = tarfile.TarInfo("backup_information.yml")
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    return output.getvalue()


class ReceiverTests(unittest.TestCase):
    def run_receiver(self, valid=True, **flags):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        root = home / "server/gitlab"
        for directory in ("backups", "config", "data"):
            (root / directory).mkdir(parents=True)
        (root / "config/gitlab-secrets.json").write_text('{"test": "mirror"}\n')
        (root / "config/gitlab.rb").write_text("# preserve mirror settings\n")
        # Reconfigure creates an empty backup workspace even before a sync.
        (root / "backups/.sync").mkdir()
        mock = home / "docker.py"
        mock.write_text(MOCK_DOCKER)
        result = subprocess.run(
            ["bash", "-c", HARNESS, "receiver-test", str(RECEIVER)],
            input=bundle(valid),
            capture_output=True,
            env=dict(os.environ, HOME=str(home), MOCK_DOCKER=str(mock),
                     SSH_ORIGINAL_COMMAND="backup " + BACKUP_ID, **flags),
        )
        calls = (home / "calls").read_text()
        self.assertFalse((home / ".local/state/gitlab-mac-sync/lock").exists())
        self.assertFalse(list((root / "backups").glob(".incoming.*")))
        self.assertNotIn("gitlab-backup create", calls)
        self.assertEqual((root / "config/gitlab.rb").read_text(),
                         "# preserve mirror settings\n")
        return result, home, calls

    def test_success_restores_and_removes_temporary_archive(self):
        result, home, calls = self.run_receiver()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(list((home / "server/gitlab/backups").iterdir()))
        self.assertIn("gitlab-backup restore BACKUP=" + BACKUP_ID, calls)
        self.assertEqual(
            (home / ".local/state/gitlab-mac-sync/last-restored-id").read_text(),
            BACKUP_ID + "\n",
        )

    def test_invalid_bundle_does_not_stop_services(self):
        result, _, calls = self.run_receiver(valid=False)
        self.assertEqual(result.returncode, 65)
        self.assertNotIn("gitlab-ctl stop", calls)

    def test_version_validation_failure_does_not_stop_services(self):
        result, home, calls = self.run_receiver(FAIL_VERIFY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("gitlab-ctl stop", calls)
        self.assertFalse(list((home / "server/gitlab/backups").iterdir()))

    def test_failed_restore_cleans_archive_but_keeps_services_stopped(self):
        result, home, calls = self.run_receiver(FAIL_RESTORE="1")
        self.assertEqual(result.returncode, 42)
        self.assertFalse(list((home / "server/gitlab/backups").iterdir()))
        self.assertTrue((home / ".local/state/gitlab-mac-sync/restore-in-progress").exists())
        self.assertNotIn("gitlab-ctl restart", calls)
        self.assertNotIn("gitlab-ctl start ", calls)
        for service in ("nginx", "puma", "sidekiq"):
            self.assertTrue(calls.rfind("gitlab-ctl stop " + service) >
                            calls.index("gitlab-backup restore"))

    def test_failed_configuration_restores_previous_secrets(self):
        result, home, calls = self.run_receiver(FAIL_CONFIGURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            (home / "server/gitlab/config/gitlab-secrets.json").read_text(),
            '{"test": "mirror"}\n',
        )
        self.assertNotIn("gitlab-backup restore", calls)
        self.assertFalse(list((home / "server/gitlab/backups").iterdir()))
        for service in ("nginx", "puma", "sidekiq"):
            self.assertIn("gitlab-ctl start " + service, calls)


if __name__ == "__main__":
    unittest.main()
