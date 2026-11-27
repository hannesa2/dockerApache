import datetime as dt
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "owncloud/schedule-backintime-beelink.py"
spec = importlib.util.spec_from_file_location("owncloud_schedule", SCRIPT)
schedule = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schedule)


class ScheduleTest(unittest.TestCase):
    def test_dev_verification_and_retention_only_delete_managed_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            history = root / "history"
            stage = root / "stage"
            state.mkdir()
            history.mkdir()
            stage.mkdir()
            site = Path("/test/wordpress-site")
            latest = "20261127-230000-001"
            old = "20200101-000000-001"
            unknown = "20190101-000000-001"
            for sid in (latest, old, unknown):
                (history / sid).mkdir()
            (history / "last_snapshot").symlink_to(latest)
            snapshot = history / latest
            (snapshot / "config").touch()
            web = snapshot / "backup" / site.relative_to("/") / "wordpress"
            web.mkdir(parents=True)
            (web / "wp-config.php").touch()
            saved = snapshot / "backup" / stage.relative_to("/")
            saved.mkdir(parents=True)
            for dest in (stage, saved):
                (dest / "wordpress.sql.gz").write_bytes(b"database")
                (dest / "captured-at").write_text("2026-11-27T23:00:00+0100")
            (saved / "wordpress.sql.gz.sha256").write_text(
                hashlib.sha256(b"database").hexdigest() + "  dump\n")
            records = state / "dev-verified"
            records.mkdir()
            (records / (old + ".json")).touch()
            for day in range(1, 8):
                sid = f"202611{27 - day:02}-230000-001"
                (history / sid).mkdir()
                (records / (sid + ".json")).touch()
            for sid in ("20261027-230000-001", "20260927-230000-001"):
                (history / sid).mkdir()
                (records / (sid + ".json")).touch()

            def remove(*args):
                self.assertNotEqual(args[-1], latest)
                self.assertNotEqual(args[-1], unknown)
                (history / args[-1]).rmdir()

            with patch.object(schedule, "STATE", state), \
                    patch.object(schedule, "DEV_HISTORY", history), \
                    patch.object(schedule, "DEV_STAGE", stage), \
                    patch.object(schedule, "DEV_SITE", site), \
                    patch.object(schedule, "BACKUP_LOCK", root / "lock"), \
                    patch.object(schedule, "run_phase"), \
                    patch.object(schedule, "run", side_effect=remove):
                schedule.backup_dev()
            self.assertTrue((records / (latest + ".json")).is_file())
            self.assertTrue((history / unknown).exists())
            self.assertFalse((history / old).exists())

    def test_cycle_promotes_only_successful_fresh_final(self):
        class Night(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 26, 23, 0).astimezone()

        baseline = "20261126-063812-580"
        preliminary = "20261126-230001-001"
        final = "20261126-230101-001"
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            for name in ("verified", "preliminary"):
                (state / name).mkdir()
            with patch.object(schedule, "STATE", state), \
                    patch.object(schedule, "mounts"), \
                    patch.object(schedule, "active", return_value=False), \
                    patch.object(schedule, "prune"), \
                    patch.object(schedule, "capacity", return_value=0), \
                    patch.object(schedule.dt, "datetime", Night), \
                    patch.object(schedule, "snapshots", side_effect=[
                        [baseline], [baseline], [baseline, preliminary],
                        [baseline, preliminary], [baseline, preliminary, final],
                    ]), \
                    patch.object(schedule, "run_phase") as phase, \
                    patch.object(schedule, "backup_dev") as dev, \
                    patch.object(schedule, "verify", return_value={"snapshot": final}) as verify:
                schedule.cycle()
                self.assertEqual(phase.call_count, 2)
                self.assertEqual(phase.call_args.args[-1], 900)
                verify.assert_called_once_with(final, current=True)
                dev.assert_called_once()
            self.assertTrue((state / "verified" / (final + ".json")).is_file())
            self.assertFalse((state / "verified" / (preliminary + ".json")).exists())

    def test_failed_online_phase_never_starts_offline(self):
        class Night(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 26, 23, 0).astimezone()

        baseline = "20261126-063812-580"
        with patch.object(schedule, "mounts"), \
                patch.object(schedule, "active", return_value=False), \
                patch.object(schedule, "prune"), \
                patch.object(schedule, "capacity", return_value=0), \
                patch.object(schedule.dt, "datetime", Night), \
                patch.object(schedule, "snapshots", return_value=[baseline]), \
                patch.object(schedule, "run_phase", side_effect=RuntimeError("online failed")) as phase:
            with self.assertRaisesRegex(RuntimeError, "online failed"):
                schedule.cycle()
            phase.assert_called_once()
            self.assertIn("online", phase.call_args.args[1])

    def test_retention_uses_union_of_daily_weekly_monthly_buckets(self):
        start = dt.datetime(2026, 6, 1, 23)
        ids = [(start + dt.timedelta(days=day)).strftime("%Y%m%d-%H%M%S-001")
               for day in range(179)]
        keep = schedule.retained(ids)
        self.assertTrue(set(ids[-7:]).issubset(keep))
        self.assertLessEqual(len(keep), 14)
        self.assertEqual({sid[:6] for sid in keep}, {"202609", "202610", "202611"})
        for sid in keep:
            day = dt.datetime.strptime(sid[:15], "%Y%m%d-%H%M%S")
            later_same_day = day.strftime("%Y%m%d") + "-235959-001"
            self.assertNotIn(sid, schedule.retained([sid, later_same_day]))

    def test_space_estimate_keeps_reserve_and_delta_headroom(self):
        with patch.object(schedule, "capture", return_value="Total transferred file size: 1,024 bytes"), \
                patch.object(schedule.shutil, "disk_usage") as usage:
            usage.return_value.free = schedule.RESERVE + 2 * 1024**3
            self.assertEqual(schedule.capacity("20261126-063812-580"), 4096)
            usage.return_value.free = schedule.RESERVE
            with self.assertRaisesRegex(RuntimeError, "Insufficient free"):
                schedule.capacity("20261126-063812-580")

    def test_unparseable_delta_is_not_treated_as_zero(self):
        with patch.object(schedule, "capture", return_value="unexpected output"):
            with self.assertRaisesRegex(RuntimeError, "Cannot parse"):
                schedule.capacity("20261126-063812-580")

    def test_restore_rehearsal_defers_all_pruning(self):
        with patch.object(schedule, "active", return_value=True), \
                patch.object(schedule, "run") as run:
            schedule.prune_locked()
            run.assert_not_called()

    def test_restore_rehearsal_refuses_cycle_before_copying(self):
        with patch.object(schedule, "mounts"), \
                patch.object(schedule, "active", return_value=True), \
                patch.object(schedule, "run_phase") as phase:
            with self.assertRaisesRegex(RuntimeError, "rehearsal"):
                schedule.cycle()
            phase.assert_not_called()

    def test_units_do_not_collide_with_legacy_offline_unit(self):
        units = schedule.units()
        self.assertNotIn("owncloud-backintime.service", units)
        self.assertIn("23:00:00", units["owncloud-backintime-cycle.timer"])
        self.assertIn("*:30:00", units["owncloud-backintime-monitor.timer"])
        self.assertTrue(all("Persistent=false" in value for name, value in units.items()
                            if name.endswith(".timer")))

    def test_pruning_preserves_pinned_and_unknown_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, history = root / "state", root / "history"
            for name in ("verified", "preliminary"):
                (state / name).mkdir(parents=True)
            unknown = "20200101-000000-001"
            newest = "20261126-230000-001"
            old_online = "20261125-230000-001"
            for sid in (schedule.PINNED, newest, old_online, unknown):
                (history / sid).mkdir(parents=True)
            (history / newest / "config").touch()
            for sid in (schedule.PINNED, newest):
                (state / "verified" / (sid + ".json")).write_text("{}")
            (state / "preliminary" / (old_online + ".json")).write_text("{}")

            def remove(*args):
                self.assertEqual(args[-1], old_online)
                self.assertIn("--skip-confirmation", args)
                (history / args[-1]).rmdir()

            with patch.object(schedule, "STATE", state), \
                    patch.object(schedule, "HISTORY", history), \
                    patch.object(schedule, "active", return_value=False), \
                    patch.object(schedule, "verify"), \
                    patch.object(schedule, "run", side_effect=remove):
                schedule.prune_locked()
            self.assertTrue((history / unknown).exists())
            self.assertTrue((history / schedule.PINNED).exists())


if __name__ == "__main__":
    unittest.main()
