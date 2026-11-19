import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "owncloud/sync-latitude.sh"


class OwncloudEnvironmentTest(unittest.TestCase):
    def test_beelink_occ_uses_image_wrapper_before_mirror_changes(self):
        source = SOURCE.read_text()
        start = source.index("source_occ() {")
        end = source.index("\n\n", start)
        with tempfile.TemporaryDirectory() as temporary:
            docker = Path(temporary) / "docker"
            docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*"\n')
            docker.chmod(0o755)
            result = subprocess.run(
                ["bash", "-euc", source[start:end] + "\nsource_occ status"],
                env={**os.environ, "PATH": temporary + ":" + os.environ["PATH"],
                     "BEELINK_PRODUCTION": "1", "SOURCE_WEB": "beelink-apps-owncloud-app-1"},
                text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(),
                             "exec --user www-data beelink-apps-owncloud-app-1 occ status")
        self.assertLess(source.index('source_occ status || die'),
                        source.index('remote_compose "stop owncloud"'))

    def test_mount_check_selects_ext4_instead_of_automount_layer(self):
        source = SOURCE.read_text()
        start = source.index('[[ "$(findmnt -rn -M /daten -t ext4 -o FSTYPE)')
        end = source.index("\n\n", start)
        with tempfile.TemporaryDirectory() as temporary:
            command = Path(temporary) / "findmnt"
            command.write_text(
                '#!/bin/sh\n'
                'if [ "$*" = "-rn -M /daten -t ext4 -o FSTYPE" ]; then\n'
                '  printf "%s\\n" "${TEST_FSTYPE:-ext4}"\n'
                'else\n  printf "autofs\\next4\\n"\nfi\n')
            command.chmod(0o755)
            for fstype, expected in (("ext4", 0), ("autofs", 1), ("xfs", 1)):
                result = subprocess.run(
                    ["bash", "-euc", 'die() { exit 1; };\n' + source[start:end]],
                    env={**os.environ, "PATH": temporary + ":" + os.environ["PATH"],
                         "TEST_FSTYPE": fstype}, capture_output=True)
                self.assertEqual(result.returncode, expected)

    def check(self, compose, environment):
        source = SOURCE.read_text()
        start = source.index('[[ -f "$SOURCE_COMPOSE" ]]')
        end = source.index('[[ -d "$SOURCE_DATA"', start)
        return subprocess.run(
            ["bash", "-euc", 'die() { echo "$*" >&2; exit 1; };\n' + source[start:end]],
            env={**os.environ, "SOURCE_COMPOSE": str(compose),
                 "SOURCE_ENV": str(environment)},
            text=True, capture_output=True)

    def test_beelink_accepts_dev_null_but_not_regular_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            compose = Path(temporary) / "compose.json"
            compose.write_text("{}")
            self.assertEqual(self.check(compose, "/dev/null").returncode, 0)
            self.assertNotEqual(self.check(compose, compose).returncode, 0)

    def test_missing_compose_fails(self):
        self.assertNotEqual(self.check("/nonexistent/compose.json", "/dev/null").returncode, 0)

    def test_script_requires_beelink_production(self):
        source = SOURCE.read_text()
        self.assertIn('"${BEELINK_PRODUCTION:-0}" == 1 && "$(hostname -s)" == beelink', source)
        self.assertNotIn("owncloud-staging", source)
        self.assertNotIn("latitude-compose.yml", source)
