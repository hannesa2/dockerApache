import ctypes
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "backup_disk_idle", ROOT / "monitoring/beelink-backup-disk-idle.py"
)
idle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(idle)


class BackupDiskIdleTest(unittest.TestCase):
    def test_idle_threshold_and_resume(self):
        tracker = idle.IdleTracker()
        self.assertFalse(tracker.update((1, 2), 0, 0))
        self.assertFalse(tracker.update((1, 2), 0, 299))
        self.assertTrue(tracker.update((1, 2), 0, 300))
        tracker.stopped = True
        self.assertFalse(tracker.update((1, 2), 0, 310))
        self.assertFalse(tracker.update((2, 2), 0, 320))
        self.assertTrue(tracker.update((2, 2), 0, 620))

    def test_active_io_resets_timer(self):
        tracker = idle.IdleTracker()
        self.assertFalse(tracker.update((1,), 0, 0))
        self.assertFalse(tracker.update((1,), 1, 300))
        self.assertFalse(tracker.update((1,), 0, 599))
        self.assertTrue(tracker.update((1,), 0, 600))

    def test_disk_statistics_include_discard_and_flush(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stat"
            path.write_text(" ".join(str(i) for i in range(17)))
            counters, pending = idle.disk_activity(path)
        self.assertEqual(counters, (0, 1, 2, 4, 5, 6, 11, 12, 13, 15))
        self.assertEqual(pending, 8)

    def test_identifies_same_open_device_node(self):
        device = SimpleNamespace(st_mode=stat.S_IFBLK, st_dev=1, st_ino=2, st_rdev=3)
        replacement = SimpleNamespace(st_mode=stat.S_IFBLK, st_dev=1, st_ino=4, st_rdev=3)
        with patch.object(idle.os, "fstat", return_value=device), \
             patch.object(idle, "DISK") as disk:
            disk.stat.return_value = device
            self.assertEqual(idle.identity(10), 3)
            disk.stat.return_value = replacement
            with self.assertRaises(RuntimeError):
                idle.identity(10)

    def test_scsi_stop_uses_open_descriptor_and_reports_failure(self):
        def verify_command(fd, request, header):
            self.assertEqual((fd, request), (19, idle.SG_IO))
            self.assertEqual(ctypes.string_at(header.cmdp, header.cmd_len),
                             b"\x1b\x00\x00\x00\x00\x00")
            self.assertEqual(header.timeout, 30000)

        with patch.object(idle.fcntl, "ioctl", side_effect=verify_command):
            idle.stop_disk(19)

        def fail_command(fd, request, header):
            header.status = 2

        with patch.object(idle.fcntl, "ioctl", side_effect=fail_command):
            with self.assertRaisesRegex(RuntimeError, "SCSI stop failed"):
                idle.stop_disk(19)

    def test_udev_identity_is_required(self):
        with patch.object(idle.os, "open", return_value=19), \
             patch.object(idle.os, "close") as close, \
             patch.object(idle, "identity", return_value=os.makedev(8, 32)), \
             patch.object(idle.subprocess, "run",
                          return_value=SimpleNamespace(stdout="ID_SERIAL_SHORT=wrong\n")):
            with self.assertRaisesRegex(RuntimeError, "serial"):
                idle.open_disk()
            close.assert_called_once_with(19)

    def test_installer_keeps_hd_idle_masked(self):
        installer = (ROOT / "monitoring/install-beelink-backup-disk-idle.sh").read_text()
        self.assertIn('== masked', installer)
        self.assertNotIn("unmask", installer)


if __name__ == "__main__":
    unittest.main()
