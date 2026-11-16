#!/usr/bin/env python3
"""Prepare, but do not install, a temporary HBA to block MXServer clients."""

import argparse
import os
from pathlib import Path


RULES = """\
# Temporary MXServer migration freeze. Existing sessions must also be closed.
local "MXServer" postgres peer
local "MXServer" all reject
host "MXServer" all 0.0.0.0/0 reject
host "MXServer" all ::/0 reject
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if not args.source.is_file() or args.source.is_symlink():
        parser.error("source must be an existing regular file, not a symlink")
    original = args.source.read_text(encoding="utf-8")
    if not original.strip():
        parser.error("source must not be empty")
    if "Temporary MXServer migration freeze." in original:
        parser.error("source already contains migration freeze rules")
    data = RULES + "\n" + original
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(args.destination, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(data)


if __name__ == "__main__":
    main()
