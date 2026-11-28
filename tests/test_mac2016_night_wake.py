import importlib.util
import datetime as dt
import fcntl
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "night_wake", Path(__file__).resolve().parents[1] / "monitoring/mac2016-night-wake.py")
WAKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WAKE)


class NightWakeTest(unittest.TestCase):
    def test_wake_starts_indefinite_session_and_checks_closed_display(self):
        with patch.object(WAKE.subprocess, "run") as run, \
                patch.object(WAKE, "SLEEP_STATE") as state:
            WAKE.wake()
        args, kwargs = run.call_args
        self.assertIn("duration:0, interval:0", args[0][2])
        self.assertIn("enable closed display mode", args[0][2])
        self.assertIn("if not (session is active)", args[0][2])
        self.assertIn("if not (closed display mode enabled)", args[0][2])
        self.assertTrue(kwargs["check"])
        self.assertEqual(kwargs["timeout"], 90)

    def test_installs_daily_launch_agent_without_starting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            plist = home / "Library/LaunchAgents" / (WAKE.LABEL + ".plist")
            plist.parent.mkdir(parents=True)
            installed = home / "bin/mac2016-night-wake.py"
            with patch.object(WAKE, "HOME", home), \
                    patch.object(WAKE, "PLIST", plist), \
                    patch.object(WAKE, "INSTALLED", installed), \
                    patch.object(WAKE, "LOG", home / "wake.log"), \
                    patch.object(WAKE.socket, "gethostname", return_value="mac2016"), \
                    patch.object(WAKE.subprocess, "run") as run:
                WAKE.install()
                definition = plistlib.loads(plist.read_bytes())
                self.assertEqual(definition["StartCalendarInterval"], {"Hour": 2, "Minute": 44})
                self.assertEqual(definition["ProgramArguments"],
                                 ["/usr/bin/python3", str(installed), "--wake"])
                self.assertEqual(run.call_args.args[0][1], "bootstrap")
                self.assertEqual(installed.read_bytes(), Path(WAKE.__file__).read_bytes())

    def test_sleep_schedules_sleep_outside_ssh(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(WAKE, "LOG", Path(temporary) / "sleep.log"), \
                patch.object(WAKE.socket, "gethostname", return_value="mac2016"), \
                patch.object(WAKE, "check_standby") as check, \
                patch.object(WAKE.subprocess, "run") as run, \
                patch.object(WAKE.subprocess, "Popen") as popen:
            WAKE.sleep()
        check.assert_called_once()
        run.assert_not_called()
        self.assertIn("--sleep-now", popen.call_args.args[0])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_sleep_now_delays_sleep_until_ssh_can_return(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(WAKE, "SLEEP_STATE", Path(temporary) / "state"), \
                patch.object(WAKE, "idle_background_wake", return_value=True), \
                patch.object(WAKE.socket, "gethostname", return_value="mac2016"), \
                patch.object(WAKE.time, "sleep") as sleep, \
                patch.object(WAKE, "check_standby") as check, \
                patch.object(WAKE.subprocess, "run") as run:
            WAKE.sleep_now()
        sleep.assert_called_once_with(10)
        check.assert_called_once()
        self.assertIn("end session", " ".join(run.call_args_list[0].args[0]))
        self.assertEqual(run.call_args_list[1].args[0], ["/usr/bin/pmset", "sleepnow"])

    def test_idle_requires_known_idle_time_and_no_user_assertions(self):
        assertions = ("UserIsActive 0\nPreventSystemSleep 0\n"
                      "PreventUserIdleSystemSleep 0\n")
        for idle, status, expected in (
            ('"HIDIdleTime" = 3600000000000', assertions, True),
            ('"HIDIdleTime" = 100', assertions, False),
            ('unknown', assertions, False),
            ('"HIDIdleTime" = 3600000000000',
             assertions.replace("UserIsActive 0", "UserIsActive 1"), False),
        ):
            with self.subTest(idle=idle, status=status), \
                    patch.object(WAKE.subprocess, "check_output",
                                 side_effect=[idle, status]):
                self.assertEqual(WAKE.idle_background_wake(), expected)

    def test_watchdog_only_resleeps_dark_wake_and_cancels_full_wake(self):
        now = dt.datetime.now().astimezone()
        requested = now.replace(hour=4, minute=50, second=0, microsecond=0)
        class FixedDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return requested.replace(hour=8)
        for kind, expected in (("DarkWake", True), ("Wake", False)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary) / "state"
                state.write_text(requested.isoformat())
                event = requested.replace(hour=7).strftime("%Y-%m-%d %H:%M:%S %z")
                with patch.object(WAKE, "SLEEP_STATE", state), \
                        patch.object(WAKE.dt, "datetime", FixedDateTime), \
                        patch.object(WAKE.socket, "gethostname", return_value="mac2016"), \
                        patch.object(WAKE, "idle_background_wake", return_value=True), \
                        patch.object(WAKE, "check_standby"), \
                        patch.object(WAKE.subprocess, "check_output",
                                     return_value=event + f" {kind} from Deep Idle"), \
                        patch.object(WAKE.subprocess, "run") as run:
                    WAKE.sleep_watchdog()
                    self.assertEqual(run.called, expected)
                    self.assertEqual(state.exists(), expected)

    def test_standby_requires_recent_snapshot_and_unlocked_snapshot_job(self):
        now = dt.datetime(2026, 11, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
        class FixedDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz is not None else now.astimezone()

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(WAKE, "SNAPSHOTS", Path(temporary)), \
                patch.object(WAKE.dt, "datetime", FixedDateTime), \
                patch.object(WAKE.socket, "gethostname", return_value="mac2016"):
            lock_path = Path(temporary) / ".lock"
            lock_path.touch()
            name = now.strftime("snapshot-%Y%m%dT%H%M%SZ")
            snapshot = Path(temporary) / name
            snapshot.mkdir()
            (snapshot / ".beelink-standby-snapshot").touch()
            WAKE.check_standby()
            with lock_path.open("r") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "still running"):
                    WAKE.check_standby()


if __name__ == "__main__":
    unittest.main()
