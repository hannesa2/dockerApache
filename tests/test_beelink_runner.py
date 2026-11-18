import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    "beelink_runner", Path(__file__).resolve().parents[1] /
    "monitoring/prepare-beelink-gitlab-runner.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


CONFIG = """concurrent = 1
check_interval = 0

[[runners]]
  name = "latitude"
  url = "https://git.mxtracks.info/"
  token = "private-runner-token"
  executor = "docker"
  [runners.docker]
    image = "alpine:latest"
    privileged = false
"""


class BeelinkGitLabRunnerTest(unittest.TestCase):
    def test_preserves_registration_and_changes_only_executor(self):
        result = RUNNER.shell_config(CONFIG)
        self.assertIn('token = "private-runner-token"', result)
        self.assertIn('url = "https://git.mxtracks.info/"', result)
        self.assertIn('executor = "shell"', result)
        self.assertIn('image = "alpine:latest"', result)
        self.assertEqual(result.count('executor = "shell"'), 1)

    def test_rejects_missing_or_ambiguous_registration(self):
        for source in (
                CONFIG.replace("token = \"private-runner-token\"\n", ""),
                CONFIG.replace("https://git.mxtracks.info/", "https://unexpected.invalid/"),
                CONFIG + "\n[[runners]]\nname = \"second\"\n"):
            with self.subTest(source=source):
                with self.assertRaises(RuntimeError):
                    RUNNER.shell_config(source)


if __name__ == "__main__":
    unittest.main()
