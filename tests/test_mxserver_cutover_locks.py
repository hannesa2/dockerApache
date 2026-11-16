from pathlib import Path
import subprocess
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / "mxserver/cutover-locks.sh"


class CutoverLocksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backup = self.root / "backup.lock"
        self.backup.touch(mode=0o600)
        self.mirror = self.root / "mirror.lock"

    def run_guard(self, body):
        return subprocess.run(
            ["bash", "-c",
             'set -eu; source "$1"; shift; ' + body,
             "test", str(HELPER), str(self.backup), str(self.mirror)],
            text=True, capture_output=True,
        )

    def test_acquires_and_releases_both_locks(self):
        result = self.run_guard(
            'acquire_cutover_locks "$1" "$2"; '
            'test -d "$2"; release_cutover_locks; test ! -e "$2"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.backup.exists())

    def test_backup_busy_does_not_create_mirror_lock(self):
        holder = subprocess.Popen(
            ["python3", "-c",
             "import fcntl,sys,time; f=open(sys.argv[1],'a'); "
             "fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); time.sleep(30)",
             str(self.backup)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        def stop_holder():
            if holder.poll() is None:
                holder.terminate()
            holder.communicate(timeout=5)
        self.addCleanup(stop_holder)
        self.assertEqual(holder.stdout.readline().strip(), "ready")
        result = self.run_guard('acquire_cutover_locks "$1" "$2"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backup is already running", result.stderr)
        self.assertFalse(self.mirror.exists())

    def test_existing_mirror_lock_is_not_removed(self):
        self.mirror.mkdir()
        result = self.run_guard('acquire_cutover_locks "$1" "$2"')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.mirror.is_dir())
        self.assertEqual(subprocess.run(["flock", "-n", str(self.backup), "true"]).returncode, 0)

    def test_missing_and_symlinked_backup_locks_are_rejected(self):
        self.backup.unlink()
        result = self.run_guard('acquire_cutover_locks "$1" "$2"')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.mirror.exists())
        target = self.root / "target"
        target.touch()
        self.backup.symlink_to(target)
        result = self.run_guard('acquire_cutover_locks "$1" "$2"')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.mirror.exists())


if __name__ == "__main__":
    unittest.main()
