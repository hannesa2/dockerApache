#!/usr/bin/env python3
"""Track local changes without storing plaintext hashes in the repository."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def state_directory():
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute",
         "--git-path", "private-config-monitor"],
        check=True, text=True, capture_output=True,
    )
    return Path(result.stdout.strip())


def key_for(directory, create=False):
    path = directory / "key"
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(os.urandom(32))
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("Invalid local monitoring key")
    return key


def digest(path, key):
    mac = hmac.new(key, digestmod=hashlib.sha256)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            mac.update(chunk)
    return mac.hexdigest()


def write_record(directory, key, plain, cipher, expected):
    actual = digest(plain, key)
    if not hmac.compare_digest(actual, expected):
        raise ValueError(f"{plain} changed during encryption; encrypt it again")
    record = {"plain_path": str(plain), "cipher_path": str(cipher),
              "plain_digest": actual, "cipher_digest": digest(cipher, key)}
    name = hmac.new(key, str(cipher).encode(), hashlib.sha256).hexdigest()
    fd, temporary = tempfile.mkstemp(prefix=".record-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
        os.replace(temporary, directory / (name + ".json"))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_one(directory, key, name, plain, cipher):
    if not plain.exists() and not cipher.exists():
        return False
    if not plain.is_file():
        print(f"WARNING: {name}: plaintext missing or inaccessible: {plain}", file=sys.stderr)
        return True
    if not cipher.is_file():
        print(f"WARNING: {name}: encrypted copy missing: {cipher}", file=sys.stderr)
        return True
    if key is None:
        print(f"WARNING: {name}: encryption not yet verified on this checkout", file=sys.stderr)
        return True
    record_name = hmac.new(key, str(cipher).encode(), hashlib.sha256).hexdigest()
    record_path = directory / (record_name + ".json")
    if not record_path.exists():
        print(f"WARNING: {name}: encryption not yet verified on this checkout", file=sys.stderr)
        return True
    record = json.loads(record_path.read_text())
    if (record.get("plain_path") != str(plain) or record.get("cipher_path") != str(cipher)
            or not hmac.compare_digest(record["plain_digest"], digest(plain, key))
            or not hmac.compare_digest(record["cipher_digest"], digest(cipher, key))):
        print(f"WARNING: {name}: plaintext or encrypted copy changed since encryption", file=sys.stderr)
        return True
    return False


def main():
    directory = state_directory()
    if len(sys.argv) == 3 and sys.argv[1] == "prepare":
        print(digest(Path(sys.argv[2]).resolve(), key_for(directory, create=True)))
    elif len(sys.argv) == 5 and sys.argv[1] == "record":
        write_record(directory, key_for(directory), Path(sys.argv[2]).resolve(),
                     Path(sys.argv[3]).resolve(), sys.argv[4])
    elif len(sys.argv) == 2 and sys.argv[1] == "check":
        host = subprocess.run(["hostname", "-s"], check=True, text=True,
                              capture_output=True).stdout.strip()
        if not host or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in host) or not host[0].isalnum():
            raise ValueError("Invalid short hostname")
        key = key_for(directory) if (directory / "key").exists() else None
        cipher_dir = ROOT / "private-config/gpg"
        entries = [
            ("apache", ROOT / "apache/.env", cipher_dir / f"apache.env.{host}.enc"),
            ("nextcloud", ROOT / "nextcloud/.env", cipher_dir / f"nextcloud.env.{host}.enc"),
            ("matomo", Path(os.environ.get("MATOMO_CONFIG_PATH",
             "/Users/hannes/server/apache/www/mxdocs/piwik/config/config.ini.php")),
             cipher_dir / f"matomo-config.ini.{host}.enc"),
        ]
        warnings = [check_one(directory, key, name, plain.resolve(), cipher.resolve())
                    for name, plain, cipher in entries]
        if any(warnings):
            print("WARNING: update with ./private/private.sh --crypt <target>, then commit the .enc files.",
                  file=sys.stderr)
            print(f"WARNING: Reminder from {ROOT / 'private/git-notice.sh'} "
                  f"(check: {ROOT / 'private/check-private.py'}). "
                  "To disable: remove the git-notice.sh source line from ~/.bashrc or ~/.zshrc "
                  "and restart the shell (or run unset -f git).", file=sys.stderr)
            return 1
    else:
        raise ValueError("Usage: check-private.py check | prepare <plain> | record <plain> <cipher> <digest>")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"ERROR: Private configuration check failed: {error}", file=sys.stderr)
        sys.exit(2)
