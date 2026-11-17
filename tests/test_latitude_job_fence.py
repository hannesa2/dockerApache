import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "fence", Path(__file__).resolve().parents[1] /
    "monitoring/disable-latitude-migration-jobs.py")
FENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FENCE)


class LatitudeJobFenceTest(unittest.TestCase):
    def test_disables_only_known_jobs_and_preserves_every_other_line(self):
        unrelated = ("SHELL=/bin/bash\n# existing comment\n"
                     "1 06 * * * ~/gitUpstreamCheck.sh ~/git/mlkit\n"
                     "0 1 * * * root /usr/local/bin/unrelated-backup\n\n")
        jobs = "".join(f"0 3 * * * {command} >> /tmp/job.log 2>&1\n"
                       for command in FENCE.COMMANDS)
        updated, count = FENCE.disabled(unrelated + jobs)
        self.assertEqual(count, len(FENCE.COMMANDS))
        self.assertTrue(updated.startswith(unrelated))
        self.assertEqual(updated.count(FENCE.MARKER), len(FENCE.COMMANDS))
        self.assertEqual(FENCE.disabled(updated), (updated, 0))

    def test_rejects_wrong_host_before_changing_schedules(self):
        with patch.object(FENCE.os, "getuid", return_value=0), \
                patch.object(FENCE.socket, "gethostname", return_value="beelink"), \
                patch("sys.argv", ["fence", "--disable"]):
            with self.assertRaisesRegex(RuntimeError, "directly on Latitude"):
                FENCE.main()


if __name__ == "__main__":
    unittest.main()
