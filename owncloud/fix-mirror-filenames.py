#!/usr/bin/env python3
import argparse
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--apply", action="store_true")
args = parser.parse_args()
root = Path("/Volumes/SamsungT7_2T/ownCloudBackup/owncloud/data")
if not root.is_dir() or root.is_symlink():
    raise SystemExit("Expected the existing mac2016 mirror data directory")

changes = []
collisions = []
for directory, dirs, files in os.walk(root, topdown=False, followlinks=False):
    for name in dirs + files:
        try:
            repaired = name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired == name:
            continue
        source = Path(directory) / name
        target = Path(directory) / repaired
        if target.exists() or target.is_symlink():
            collisions.append((source, target))
            continue
        changes.append((source, target))

print(f"Reversible double-encoded filename corrections: {len(changes)}")
print(f"Collisions retained without overwriting: {len(collisions)}")
if args.apply:
    for source, target in changes:
        os.rename(source, target)
    print("Mirror filenames repaired; file contents were not modified")
