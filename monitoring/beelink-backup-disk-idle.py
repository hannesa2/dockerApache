#!/usr/bin/env python3
"""Spin down only the identified beelink backup HDD after inactivity."""

import ctypes
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import time


DISK = Path("/dev/disk/by-id/ata-WDC_WD20SMZW-11JW8S0_WD-WXG1EA84H743")
SERIAL = "WD-WXG1EA84H743"
IDLE_SECONDS = 300
POLL_SECONDS = 10
SG_IO = 0x2285


class SgIoHeader(ctypes.Structure):
    _fields_ = [
        ("interface_id", ctypes.c_int), ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte), ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort), ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p), ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p), ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint), ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p), ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte), ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte), ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort), ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint), ("info", ctypes.c_uint),
    ]


def identity(fd):
    opened = os.fstat(fd)
    current = DISK.stat()
    if not stat.S_ISBLK(opened.st_mode) or (
        opened.st_dev, opened.st_ino, opened.st_rdev
    ) != (current.st_dev, current.st_ino, current.st_rdev):
        raise RuntimeError("Backup disk disappeared or its device node changed")
    return opened.st_rdev


def open_disk():
    fd = os.open(DISK, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        device = identity(fd)
        name = f"{os.major(device)}:{os.minor(device)}"
        result = subprocess.run(
            ["udevadm", "info", "--query=property", f"--path=/sys/dev/block/{name}"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines()
                          if "=" in line)
        if properties.get("ID_SERIAL_SHORT") != SERIAL:
            raise RuntimeError("Backup disk serial does not match")
        if identity(fd) != device:
            raise RuntimeError("Backup disk changed during identification")
        return fd, Path("/sys/dev/block") / name / "stat"
    except Exception:
        os.close(fd)
        raise


def disk_activity(path):
    fields = [int(value) for value in path.read_text().split()]
    if len(fields) < 11:
        raise RuntimeError("Incomplete disk I/O statistics")
    # Counts and sectors include reads, writes, discards and flushes if present.
    counters = tuple(fields[i] for i in (0, 1, 2, 4, 5, 6, 11, 12, 13, 15)
                     if i < len(fields))
    return counters, fields[8]


def stop_disk(fd):
    command = (ctypes.c_ubyte * 6)(0x1B, 0, 0, 0, 0, 0)
    sense = (ctypes.c_ubyte * 64)()
    header = SgIoHeader()
    header.interface_id = ord("S")
    header.dxfer_direction = -1
    header.cmd_len = len(command)
    header.mx_sb_len = len(sense)
    header.cmdp = ctypes.addressof(command)
    header.sbp = ctypes.addressof(sense)
    header.timeout = 30000
    fcntl.ioctl(fd, SG_IO, header)
    if header.status or header.host_status or header.driver_status or header.info & 1:
        raise RuntimeError(
            f"SCSI stop failed: status={header.status}, host={header.host_status}, "
            f"driver={header.driver_status}, sense={bytes(sense[:header.sb_len_wr]).hex()}"
        )


class IdleTracker:
    def __init__(self):
        self.previous = None
        self.last_activity = None
        self.stopped = False

    def update(self, counters, in_flight, now):
        if self.previous != counters or in_flight:
            if self.stopped:
                print("Disk activity detected after standby request", flush=True)
            self.previous = counters
            self.last_activity = now
            self.stopped = False
        return not self.stopped and now - self.last_activity >= IDLE_SECONDS


def monitor():
    fd = None
    last_error = None
    tracker = IdleTracker()
    while True:
        try:
            if fd is None:
                fd, stats_path = open_disk()
                tracker = IdleTracker()
                print("Monitoring verified backup HDD; idle timeout 300 seconds", flush=True)
            identity(fd)
            counters, in_flight = disk_activity(stats_path)
            if tracker.update(counters, in_flight, time.monotonic()):
                identity(fd)
                latest, pending = disk_activity(stats_path)
                if latest == counters and not pending:
                    stop_disk(fd)
                    tracker.stopped = True
                    print("SCSI standby command accepted", flush=True)
                else:
                    tracker.update(latest, pending, time.monotonic())
            last_error = None
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            message = f"{type(error).__name__}: {error}"
            if message != last_error:
                print(f"Backup disk standby monitoring failed: {message}", flush=True)
            last_error = message
            if fd is not None:
                os.close(fd)
                fd = None
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    monitor()
