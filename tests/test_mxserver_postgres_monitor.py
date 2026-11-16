from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MARKER = "/etc/mxserver-postgres-dockerized"


class MXServerPostgresMonitorTest(unittest.TestCase):
    def test_selects_expected_database_service_on_both_hosts(self):
        for name in ("systems-health-check.sh", "systems-health-check-mac.sh"):
            script = (ROOT / "monitoring" / name).read_text()
            start = script.index(f"if [[ -f {MARKER} ]]; then")
            end = script.index("\nfi", start) + len("\nfi")
            block = script[start:end]
            with tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / "marker"
                block = block.replace(MARKER, str(marker))
                command = (
                    'check_unit() { echo "unit:$1"; }; '
                    'check_container() { echo "container:$1"; }; ' + block
                )
                native = subprocess.run(["bash", "-c", command], check=True,
                                        text=True, capture_output=True)
                self.assertEqual(native.stdout.strip(), "unit:postgresql@16-main")
                marker.touch()
                docker = subprocess.run(["bash", "-c", command], check=True,
                                        text=True, capture_output=True)
                self.assertEqual(docker.stdout.strip(), "container:mxserver-postgres-db-1")


if __name__ == "__main__":
    unittest.main()
