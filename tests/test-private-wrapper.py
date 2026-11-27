#!/usr/bin/env python3
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]


class PrivateWrapperTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for directory in ("private", "buildSrc/scripts", "apache", "nextcloud", "matomo", "camera-postgres", "mxserver-postgres", "gnupg", "bin"):
            (self.root / directory).mkdir(parents=True, mode=0o700)
        for path in ("private/private.sh", "private/check-private.py", "private/check-git.py",
                     "private/check-installed.py",
                     "private/git-notice.sh", "buildSrc/scripts/decrypt.sh"):
            shutil.copy2(SOURCE / path, self.root / path)
            (self.root / path).chmod(0o700)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        hostname = self.root / "bin/hostname"
        hostname.write_text('#!/bin/sh\n[ "$1" = -s ] || exit 1\nprintf "%s\\n" "$TEST_HOST"\n')
        hostname.chmod(0o700)
        date = self.root / "bin/date"
        date.write_text('#!/bin/sh\n[ "$1" = +%y%m%d-%H%M ] || exit 1\nprintf "%s\\n" "$TEST_DATE"\n')
        date.chmod(0o700)
        self.env = os.environ.copy()
        self.env.update(
            PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"],
            TEST_HOST="mac2016",
            TEST_DATE="261115-1120",
            GNUPGHOME=str(self.root / "gnupg"),
            CRYPT_PASS="test passphrase",
            CRYPT_PASS_CONFIRM="test passphrase",
            MATOMO_CONFIG_PATH=str(self.root / "matomo/config.ini.php"),
        )
        self.plain = [
            self.root / "apache/.env",
            self.root / "nextcloud/.env",
            self.root / "matomo/config.ini.php",
        ]
        for index, path in enumerate(self.plain):
            path.write_text(f"private configuration {index}\n")

    def tearDown(self):
        self.temp.cleanup()

    def run_wrapper(self, *args, env=None):
        return subprocess.run(
            [str(self.root / "private/private.sh"), *args], cwd="/",
            env=env or self.env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
        )

    def test_round_trip_all_files(self):
        expected = [path.read_bytes() for path in self.plain]
        result = self.run_wrapper("--crypt")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("apache.env", "nextcloud.env", "matomo-config.ini"):
            self.assertTrue((self.root / f"private-config/gpg/{name}.mac2016.enc").is_file())
        for path in self.plain:
            path.unlink()
        result = self.run_wrapper("--decrypt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([path.read_bytes() for path in self.plain], expected)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in self.plain))

    def test_missing_input_aborts_before_writing(self):
        self.plain[-1].unlink()
        result = self.run_wrapper("--crypt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("matomo", result.stderr)
        self.assertFalse((self.root / "private-config/gpg/apache.env.mac2016.enc").exists())

    def test_selected_target_and_encrypted_overwrite_refusal(self):
        result = self.run_wrapper("--crypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        cipher = self.root / "private-config/gpg/apache.env.mac2016.enc"
        original = cipher.read_bytes()
        result = self.run_wrapper("--crypt", "apache")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(cipher.read_bytes(), original)
        self.assertFalse((self.root / "private-config/gpg/nextcloud.env.mac2016.enc").exists())

    def test_restore_saves_existing_plaintext_and_rejects_duplicate_backup(self):
        result = self.run_wrapper("--crypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.plain[0].write_text("current configuration\n")
        result = self.run_wrapper("--decrypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = self.root / "apache/.env.261115-1120"
        self.assertEqual(backup.read_text(), "current configuration\n")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.plain[0].read_text(), "private configuration 0\n")
        self.plain[0].write_text("new current configuration\n")
        result = self.run_wrapper("--decrypt", "apache")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(backup.read_text(), "current configuration\n")
        self.assertEqual(self.plain[0].read_text(), "new current configuration\n")
        self.assertEqual(list((self.root / "apache").glob(".decrypt-tmp.*")), [])

    def test_identical_restore_preserves_original_and_existing_backup(self):
        result = self.run_wrapper("--crypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = self.root / "apache/.env.261115-1120"
        backup.write_text("existing backup\n")
        self.plain[0].chmod(0o640)
        inode = self.plain[0].stat().st_ino
        result = self.run_wrapper("--decrypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Configuration unchanged:", result.stdout)
        self.assertEqual(self.plain[0].stat().st_ino, inode)
        self.assertEqual(self.plain[0].stat().st_mode & 0o777, 0o640)
        self.assertEqual(backup.read_text(), "existing backup\n")
        backup.unlink()
        result = self.run_wrapper("--decrypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(backup.exists())
        self.assertEqual(list((self.root / "apache").glob(".decrypt-tmp.*")), [])

    def test_comparison_error_preserves_original(self):
        result = self.run_wrapper("--crypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        cmp_command = self.root / "bin/cmp"
        cmp_command.write_text("#!/bin/sh\nexit 2\n")
        cmp_command.chmod(0o700)
        result = self.run_wrapper("--decrypt", "apache")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not compare", result.stderr)
        self.assertEqual(self.plain[0].read_text(), "private configuration 0\n")
        self.assertFalse((self.root / "apache/.env.261115-1120").exists())
        self.assertEqual(list((self.root / "apache").glob(".decrypt-tmp.*")), [])

    def test_failed_decryption_preserves_original_without_backup(self):
        result = self.run_wrapper("--crypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.plain[0].write_text("current configuration\n")
        result = self.run_wrapper("--decrypt", "apache", env=dict(self.env, CRYPT_PASS="wrong"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.plain[0].read_text(), "current configuration\n")
        self.assertFalse((self.root / "apache/.env.261115-1120").exists())
        self.assertEqual(list((self.root / "apache").glob(".decrypt-tmp.*")), [])

    def test_restore_rejects_symlink(self):
        result = self.run_wrapper("--crypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.plain[0].unlink()
        self.plain[0].symlink_to(self.plain[1])
        result = self.run_wrapper("--decrypt", "apache")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.plain[1].read_text(), "private configuration 1\n")

    def test_bad_arguments_and_mismatched_confirmation(self):
        self.assertNotEqual(self.run_wrapper("--unknown").returncode, 0)
        self.assertNotEqual(self.run_wrapper("--crypt", "apache", "apache").returncode, 0)
        result = self.run_wrapper("--crypt", "apache",
                                  env=dict(self.env, CRYPT_PASS_CONFIRM="wrong"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "private-config/gpg/apache.env.mac2016.enc").exists())

    def test_hosts_are_isolated_and_restore_their_own_files(self):
        result = self.run_wrapper("--crypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.plain[0].write_text("beelink configuration\n")
        beelink_env = dict(self.env, TEST_HOST="beelink")
        result = self.run_wrapper("--crypt", "apache", env=beelink_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "private-config/gpg/apache.env.mac2016.enc").is_file())
        self.assertTrue((self.root / "private-config/gpg/apache.env.beelink.enc").is_file())
        self.plain[0].unlink()
        result = self.run_wrapper("--decrypt", "apache", env=beelink_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.plain[0].read_text(), "beelink configuration\n")
        self.plain[0].unlink()
        result = self.run_wrapper("--decrypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.plain[0].read_text(), "private configuration 0\n")

    def test_camera_config_roundtrip_and_monitor(self):
        camera = self.root / "camera-postgres/.env"
        camera.write_text("camera secret\n")
        result = self.run_wrapper("--crypt", "camera-postgres")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "private-config/gpg/camera-postgres.env.mac2016.enc").is_file())
        camera.unlink()
        result = self.run_wrapper("--decrypt", "camera-postgres")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(camera.read_text(), "camera secret\n")
        for path in self.plain:
            path.unlink()
        result = subprocess.run(["python3", str(self.root / "private/check-private.py"), "check"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        camera.write_text("changed\n")
        result = subprocess.run(["python3", str(self.root / "private/check-private.py"), "check"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("camera-postgres: plaintext or encrypted copy changed", result.stderr)

    def test_mxserver_config_roundtrip_and_monitor(self):
        config = self.root / "mxserver-postgres/.env"
        config.write_text("mxserver bootstrap secret\n")
        result = self.run_wrapper("--crypt", "mxserver-postgres")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "private-config/gpg/mxserver-postgres.env.mac2016.enc").is_file())
        config.unlink()
        result = self.run_wrapper("--decrypt", "mxserver-postgres")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_text(), "mxserver bootstrap secret\n")
        for path in self.plain:
            path.unlink()
        config.write_text("changed\n")
        result = subprocess.run(["python3", str(self.root / "private/check-private.py"), "check"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("mxserver-postgres: plaintext or encrypted copy changed", result.stderr)

    def test_missing_host_ciphertext_and_invalid_hostname(self):
        result = self.run_wrapper("--crypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.plain[0].unlink()
        result = self.run_wrapper("--decrypt", "apache", env=dict(self.env, TEST_HOST="other-host"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.plain[0].exists())
        result = self.run_wrapper("--crypt", "nextcloud", env=dict(self.env, TEST_HOST="../unsafe"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid short hostname", result.stderr)

    def test_change_monitor_tracks_each_host_and_cipher(self):
        result = self.run_wrapper("--crypt", "apache", "nextcloud")
        self.assertEqual(result.returncode, 0, result.stderr)
        def check(env=None):
            return subprocess.run(
                ["python3", str(self.root / "private/check-private.py"), "check"],
                env=env or self.env, capture_output=True, text=True,
            )
        self.plain[2].unlink()
        self.assertEqual(check().returncode, 0)
        self.plain[0].write_text("changed")
        result = check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("apache: plaintext or encrypted copy changed", result.stderr)
        self.assertIn("private/git-notice.sh", result.stderr)
        self.assertIn("private/check-private.py", result.stderr)
        self.assertIn("unset -f git", result.stderr)
        self.plain[0].write_text("private configuration 0\n")
        cipher = self.root / "private-config/gpg/apache.env.mac2016.enc"
        cipher.write_bytes(cipher.read_bytes() + b"x")
        self.assertEqual(check().returncode, 1)
        result = check(dict(self.env, TEST_HOST="beelink"))
        self.assertIn("encrypted copy missing", result.stderr)

    def test_matomo_override_persists_for_check_and_restore(self):
        result = self.run_wrapper("--crypt", "matomo")
        self.assertEqual(result.returncode, 0, result.stderr)
        env = dict(self.env)
        del env["MATOMO_CONFIG_PATH"]
        self.plain[0].unlink()
        self.plain[1].unlink()
        result = subprocess.run(
            ["python3", str(self.root / "private/check-private.py"), "check"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.plain[2].write_text("modified")
        result = subprocess.run(
            ["python3", str(self.root / "private/check-private.py"), "check"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("matomo: plaintext or encrypted copy changed", result.stderr)
        result = self.run_wrapper("--decrypt", "matomo", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.plain[2].read_text(), "private configuration 2\n")
        self.assertEqual((self.root / "matomo/config.ini.php.261115-1120").read_text(),
                         "modified")
        result = subprocess.run(
            ["python3", str(self.root / "private/check-private.py"), "check"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        other = self.root / "matomo/other.ini"
        other.write_text("other")
        result = subprocess.run(
            ["python3", str(self.root / "private/check-private.py"), "check"],
            env=dict(env, MATOMO_CONFIG_PATH=str(other)), capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("matomo: plaintext or encrypted copy changed", result.stderr)

    def test_invalid_matomo_record_is_reported(self):
        result = self.run_wrapper("--crypt", "matomo")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.root / ".git/private-config-monitor"
        record = next(state.glob("*.json"))
        record.write_text('{"plain_path": "relative", "cipher_path": "invalid"}')
        env = dict(self.env)
        del env["MATOMO_CONFIG_PATH"]
        result = self.run_wrapper("--decrypt", "matomo", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid Matomo monitoring record", result.stderr)

    def test_beelink_encrypts_protected_matomo_without_root_owned_outputs(self):
        sudo = self.root / "bin/sudo"
        sudo.write_text(
            '#!/bin/sh\n'
            '[ "$1" != -v ] || exit 0\n'
            '[ "$1" != -n ] || shift\n'
            'case "$1" in\n'
            'test) [ "$2" != -r ] ;;\n'
            'cat) [ "$3" = /daten/beelink-runtime/matomo/www/piwik/config/config.ini.php ] '
            '|| exit 9; exec /bin/cat "$TEST_MATOMO_INPUT" ;;\n'
            '*) exit 8 ;;\n'
            'esac\n'
        )
        sudo.chmod(0o755)
        env = dict(self.env, TEST_HOST="beelink",
                   TEST_MATOMO_INPUT=str(self.plain[2]), TMPDIR=str(self.root))
        del env["MATOMO_CONFIG_PATH"]
        result = self.run_wrapper("--crypt", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        cipher = self.root / "private-config/gpg/matomo-config.ini.beelink.enc"
        self.assertTrue(cipher.is_file())
        self.assertEqual(cipher.stat().st_uid, os.getuid())
        self.assertEqual(list(self.root.glob("private-matomo.*")), [])
        self.assertNotIn("private configuration 2", result.stdout + result.stderr)
        result = subprocess.run(
            ["python3", str(self.root / "private/check-private.py"), "check"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.plain[2].write_text("changed active configuration")
        result = subprocess.run(
            ["python3", str(self.root / "private/check-private.py"), "check"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("matomo: plaintext or encrypted copy changed", result.stderr)

        cipher.unlink()
        helper = self.root / "buildSrc/scripts/decrypt.sh"
        helper.write_text("#!/bin/sh\nexit 1\n")
        result = self.run_wrapper("--crypt", "matomo", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.root.glob("private-matomo.*")), [])
        self.assertFalse(cipher.exists())

    def test_beelink_read_failure_is_reported_before_encryption(self):
        sudo = self.root / "bin/sudo"
        sudo.write_text(
            '#!/bin/sh\n'
            '[ "$1" != -v ] || exit 0\n'
            '[ "$1" != -n ] || shift\n'
            'case "$1" in\n'
            'test) exit 0 ;;\n'
            'cat) exit 1 ;;\n'
            '*) exit 8 ;;\n'
            'esac\n'
        )
        sudo.chmod(0o755)
        env = dict(self.env, TEST_HOST="beelink")
        del env["MATOMO_CONFIG_PATH"]
        result = self.run_wrapper("--crypt", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR: Cannot read Matomo configuration with sudo:", result.stderr)
        self.assertFalse((self.root / "private-config/gpg").exists())

    def test_git_shell_notice_for_selected_commands(self):
        result = self.run_wrapper("--crypt", "apache")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.plain[0].write_text("changed")
        shell = (
            'source "$1/private/git-notice.sh" "$1"; '
            'git -C "$1" "$2"'
        )
        for command in ("status", "commit", "pull"):
            result = subprocess.run(
                ["bash", "-c", shell, "test", str(self.root), command],
                cwd="/", env=self.env, capture_output=True, text=True,
            )
            self.assertIn("apache: plaintext or encrypted copy changed", result.stderr)
            self.assertIn("private/git-notice.sh", result.stderr)
            self.assertIn("unset -f git", result.stderr)
        result = subprocess.run(
            ["bash", "-c", shell, "test", str(self.root), "branch"],
            cwd="/", env=self.env, capture_output=True, text=True,
        )
        self.assertNotIn("WARNING: apache", result.stderr)

    def test_git_pull_checks_installed_helpers_again_after_success(self):
        marker = self.root / "pulled"
        checker = self.root / "private/check-installed.py"
        checker.write_text(
            "from pathlib import Path\n"
            "import os, sys\n"
            "if Path(os.environ['PULL_MARKER']).exists():\n"
            "    print('post-pull installed warning', file=sys.stderr)\n"
        )
        git = self.root / "bin/git"
        git.write_text(
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  if [ "$arg" = pull ]; then touch "$PULL_MARKER"; exit 0; fi\n'
            "done\n"
            f'exec "{shutil.which("git")}" "$@"\n'
        )
        git.chmod(0o700)
        result = subprocess.run(
            ["bash", "-c", 'source "$1/private/git-notice.sh" "$1"; git -C "$1" pull',
             "test", str(self.root)],
            cwd="/", env=dict(self.env, PULL_MARKER=str(marker)),
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("post-pull installed warning"), 1)


if __name__ == "__main__":
    unittest.main()
