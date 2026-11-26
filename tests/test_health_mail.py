from email import policy
from email.parser import BytesParser
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "monitoring/send-health-mail.py"
spec = importlib.util.spec_from_file_location("health_mail", SCRIPT)
mail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mail)


class HealthMailTest(unittest.TestCase):
    def test_multipart_preserves_text_and_emphasizes_only_failure_markers(self):
        report = ("FAIL|storage|<50 GiB & unknown\n"
                  "Result: FAILED\n"
                  "OK|test|FAIL is a word\n")
        result = BytesParser(policy=policy.default).parsebytes(
            mail.message("Availability ä", "test@example.com", report).as_bytes())
        self.assertEqual(result.get_body(preferencelist=("plain",)).get_content().splitlines(),
                         report.splitlines())
        rendered = result.get_body(preferencelist=("html",)).get_content()
        self.assertIn("<strong>FAIL</strong>|storage|&lt;50 GiB &amp; unknown", rendered)
        self.assertIn("Result: <strong>FAILED</strong>", rendered)
        self.assertIn("OK|test|FAIL is a word", rendered)
        self.assertEqual(rendered.count("<strong>"), 2)

    def test_submission_failure_propagates(self):
        import io
        import subprocess
        with patch.object(mail.sys, "argv", ["helper", "subject", "test@example.com"]), \
                patch.object(mail.sys, "stdin", io.StringIO("FAIL|test|error")), \
                patch.object(mail.subprocess, "run",
                             side_effect=subprocess.CalledProcessError(1, "sendmail")):
            with self.assertRaises(subprocess.CalledProcessError):
                mail.main()


if __name__ == "__main__":
    unittest.main()
