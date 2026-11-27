import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "check_installed", Path(__file__).resolve().parents[1] / "private/check-installed.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class InstalledHelperCheckTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.libexec = self.root / "libexec"
        self.directory = self.libexec / "beelink-jobs"
        self.directory.mkdir(parents=True)
        self.source = self.root / "monitoring/job.py"
        self.source.parent.mkdir()
        self.source.write_text("current\n")
        self.installed = self.directory / "job.py"
        self.installed.write_text("current\n")
        self.status_helper = self.root / "beelink-job-status"
        self.status_helper.write_text("status\n")
        (self.source.parent / "beelink-jobs.py").write_text("status\n")
        self.files = {self.libexec: b"d", self.directory: b"d", self.installed: b"f"}
        self.patches = [
            patch.object(CHECK, "ROOT", self.root),
            patch.object(CHECK, "LIBEXEC", self.libexec),
            patch.object(CHECK, "STATUS_HELPER", self.status_helper),
            patch.object(CHECK, "GROUPS", {"beelink-jobs": {"job.py": "monitoring/job.py"}}),
            patch.object(CHECK, "STANDALONE", {}),
            patch.object(CHECK, "OPTIONAL", {}),
            patch.object(CHECK, "PINNED", set()),
            patch.object(CHECK, "hostname", return_value="beelink"),
            patch.object(CHECK.subprocess, "run", side_effect=self.run_command),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def run_command(self, command, **kwargs):
        if command[2] == "/usr/bin/find":
            output = b"\0".join(kind + b"\t" + str(path).encode()
                                for path, kind in self.files.items()) + b"\0"
            return subprocess.CompletedProcess(command, 0, stdout=output)
        path = Path(command[-1])
        if not path.is_file():
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing")
        digest = CHECK.hashlib.sha256(path.read_bytes()).hexdigest()
        return subprocess.CompletedProcess(command, 0, stdout=f"{digest}  {path}\n", stderr="")

    def checked(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            status = CHECK.check()
        return status, output.getvalue()

    def test_equal_and_changed(self):
        self.assertEqual(self.checked(), (0, ""))
        self.installed.write_text("old\n")
        status, output = self.checked()
        self.assertEqual(status, 1)
        self.assertIn("Installed helper differs:", output)
        self.assertIn(str(self.installed), output)
        self.assertIn(str(self.source), output)

    def test_missing_source_and_installed_helper(self):
        self.source.unlink()
        status, output = self.checked()
        self.assertEqual(status, 1)
        self.assertIn("Repository source missing:", output)
        self.source.write_text("current\n")
        self.installed.unlink()
        del self.files[self.installed]
        status, output = self.checked()
        self.assertEqual(status, 1)
        self.assertIn("Cannot check installed helper", output)

    def test_unknown_script_is_reported_but_pinned_is_ignored(self):
        unknown = self.directory / "unknown.py"
        pinned = self.directory / "pinned.py"
        self.files.update({unknown: b"f", pinned: b"f"})
        CHECK.PINNED.add(pinned)
        status, output = self.checked()
        self.assertEqual(status, 1)
        self.assertIn(str(unknown), output)
        self.assertNotIn(str(pinned), output)

    def test_subdirectories_are_not_unmapped_files(self):
        self.files[self.directory / "__pycache__"] = b"d"
        self.assertEqual(self.checked(), (0, ""))

    def test_symlinked_helper_or_group_is_reported(self):
        self.files[self.installed] = b"l"
        self.assertIn("helper is a symlink", self.checked()[1])
        self.files[self.directory] = b"l"
        self.assertIn("directory is a symlink", self.checked()[1])

    def test_missing_group_is_reported(self):
        del self.files[self.directory]
        self.assertIn("directory missing", self.checked()[1])

    def test_optional_helpers_checked_only_when_installed(self):
        helper = self.directory / "mail.py"
        CHECK.OPTIONAL["beelink-jobs/mail.py"] = "monitoring/job.py"
        self.assertEqual(self.checked(), (0, ""))
        helper.write_text("outdated\n")
        self.files[helper] = b"f"
        status, output = self.checked()
        self.assertEqual(status, 1)
        self.assertIn(str(helper), output)
        helper.write_text("current\n")
        self.assertEqual(self.checked(), (0, ""))

    def test_other_hosts_do_not_use_sudo(self):
        with patch.object(CHECK, "hostname", return_value="mac2016"), \
                patch.object(CHECK.subprocess, "run") as run:
            self.assertEqual(CHECK.check(), 0)
            run.assert_not_called()

    def test_sudo_failure_is_not_treated_as_success(self):
        with patch.object(CHECK.subprocess, "run",
                          side_effect=subprocess.CalledProcessError(1, ["sudo"])):
            with self.assertRaises(subprocess.CalledProcessError):
                CHECK.check()


if __name__ == "__main__":
    unittest.main()
