import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "monitoring/collect-latitude-cutover-databases.sh"


class LatitudeCutoverDatabasesTest(unittest.TestCase):
    def test_explicit_mode_required(self):
        result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--collect-frozen", result.stderr)

    def test_wrong_host_rejected_before_collection(self):
        with tempfile.TemporaryDirectory() as temporary:
            for name, response in (("id", "0"), ("hostname", "beelink")):
                path = Path(temporary) / name
                path.write_text("#!/bin/sh\nprintf '%s\\n' '" + response + "'\n")
                path.chmod(0o755)
            result = subprocess.run(
                ["bash", str(SCRIPT), "--collect-frozen"],
                env=dict(os.environ, PATH=temporary + os.pathsep + os.environ["PATH"]),
                capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("directly on Latitude", result.stderr)

    def test_gitlab_status_accepts_only_stopped_services(self):
        function = SCRIPT.read_text().split("check_frozen() {", 1)[1].split(
            "\n}\ncheck_frozen", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, text in (
                ("systemctl", "#!/bin/sh\necho inactive\n"),
                ("docker", """#!/bin/sh
if [ "$1" = inspect ]; then echo false; exit 0; fi
if [ "$WRITER_RUNNING" = yes ]; then echo "run: $5: (pid 10)"; exit 0; fi
echo "down: $5: 10s, normally up"
exit 3
"""),
            ):
                path = root / name
                path.write_text(text)
                path.chmod(0o755)
            command = (
                "set -euo pipefail\nfail() { echo \"$*\" >&2; exit 1; }\n"
                "check_frozen() {" + function + "\n}\ncheck_frozen\n")
            for running in ("no", "yes"):
                result = subprocess.run(
                    ["bash", "-c", command], capture_output=True, text=True,
                    env=dict(os.environ, PATH=temporary + os.pathsep + os.environ["PATH"],
                             WRITER_RUNNING=running))
                self.assertEqual(result.returncode == 0, running == "no", result.stderr)

    def test_collection_does_not_restore_or_create_gitlab_backup(self):
        source = SCRIPT.read_text()
        for forbidden in ("gitlab-backup", "docker stop", "systemctl stop", "--delete",
                          "crontab ", "pg_restore --clean", "mysql <"):
            self.assertNotIn(forbidden, source)
        self.assertIn('pg_restore -f /dev/null', source)
        self.assertIn('sha256sum -c SHA256SUMS', source)
        self.assertIn("GLOBAL.event_scheduler", source)
        self.assertIn("Source stays frozen", source)


if __name__ == "__main__":
    unittest.main()
