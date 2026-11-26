#!/usr/bin/env python3
"""Send an availability report with emphasized failure markers."""

from email.message import EmailMessage
from email.policy import SMTP
import html
import re
import subprocess
import sys


def message(subject, recipient, report):
    mail = EmailMessage(policy=SMTP)
    mail["To"] = recipient
    mail["Subject"] = subject
    mail.set_content(report)
    rendered = html.escape(report)
    rendered = re.sub(r"(?m)^FAIL(?=\|)", "<strong>FAIL</strong>", rendered)
    rendered = re.sub(r"(?<![A-Z])FAILED(?![A-Z])", "<strong>FAILED</strong>", rendered)
    mail.add_alternative(
        '<!doctype html><html><body><pre style="white-space:pre-wrap">'
        + rendered + "</pre></body></html>\n",
        subtype="html",
    )
    return mail


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: send-health-mail.py SUBJECT RECIPIENT")
    mail = message(sys.argv[1], sys.argv[2], sys.stdin.read())
    subprocess.run(["/usr/sbin/sendmail", "-t", "-oi"],
                   input=mail.as_bytes(), check=True)


if __name__ == "__main__":
    main()
