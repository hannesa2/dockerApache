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
import contextlib

ROOT = Path(__file__).resolve().parent.parent
BEELINK_MATOMO = Path("/daten/beelink-runtime/matomo/www/piwik/config/config.ini.php")


def short_hostname():
    return subprocess.check_output(["hostname", "-s"], text=True).strip()


def privileged_matomo(path):
    return path == BEELINK_MATOMO and short_hostname().lower() == "beelink"


def canonical(path):
    return path if privileged_matomo(path) else path.resolve()


@contextlib.contextmanager
def plaintext_stream(path):
    if privileged_matomo(path):
        process = subprocess.Popen(["sudo", "-n", "cat", "--", str(path)],
                                   stdout=subprocess.PIPE)
        try:
            yield process.stdout
        finally:
            process.stdout.close()
            status = process.wait()
            if status:
                raise subprocess.CalledProcessError(status, process.args)
    else:
        with path.open("rb") as stream:
            yield stream


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
    with plaintext_stream(path) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            mac.update(chunk)
    return mac.hexdigest()


def matomo_path(directory, cipher):
    override = os.environ.get("MATOMO_CONFIG_PATH")
    if override:
        return Path(override)
    if short_hostname().lower() == "beelink":
        return BEELINK_MATOMO
    if (directory / "key").exists():
        key = key_for(directory)
        name = hmac.new(key, str(cipher.resolve()).encode(), hashlib.sha256).hexdigest()
        record_path = directory / (name + ".json")
        if record_path.exists():
            record = json.loads(record_path.read_text())
            path = Path(record["plain_path"])
            if not path.is_absolute() or record["cipher_path"] != str(cipher.resolve()):
                raise ValueError(f"Invalid Matomo monitoring record: {record_path}")
            return path
    if sys.platform == "darwin":
        return Path("/Users/hannes/server/apache/www/mxdocs/piwik/config/config.ini.php")
    return Path("/srv/www/vhosts/mxdocs/piwik/config/config.ini.php")


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
    protected = privileged_matomo(plain)
    if not protected and not plain.exists() and not cipher.exists():
        return False
    if not protected and not plain.is_file():
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
        print(digest(canonical(Path(sys.argv[2])), key_for(directory, create=True)))
    elif len(sys.argv) == 5 and sys.argv[1] == "record":
        write_record(directory, key_for(directory), canonical(Path(sys.argv[2])),
                     Path(sys.argv[3]).resolve(), sys.argv[4])
    elif len(sys.argv) == 2 and sys.argv[1] in ("check", "matomo-path"):
        host = subprocess.run(["hostname", "-s"], check=True, text=True,
                              capture_output=True).stdout.strip()
        if not host or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in host) or not host[0].isalnum():
            raise ValueError("Invalid short hostname")
        cipher_dir = ROOT / "private-config/gpg"
        matomo_cipher = cipher_dir / f"matomo-config.ini.{host}.enc"
        if sys.argv[1] == "matomo-path":
            print(matomo_path(directory, matomo_cipher))
            return 0
        key = key_for(directory) if (directory / "key").exists() else None
        entries = [
            ("apache", ROOT / "apache/.env", cipher_dir / f"apache.env.{host}.enc"),
            ("nextcloud", ROOT / "nextcloud/.env", cipher_dir / f"nextcloud.env.{host}.enc"),
            ("matomo", matomo_path(directory, matomo_cipher), matomo_cipher),
            ("camera-postgres", ROOT / "camera-postgres/.env", cipher_dir / f"camera-postgres.env.{host}.enc"),
            ("mxserver-postgres", ROOT / "mxserver-postgres/.env", cipher_dir / f"mxserver-postgres.env.{host}.enc"),
        ]
        warnings = [check_one(directory, key, name, canonical(plain), cipher.resolve())
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
        raise ValueError("Usage: check-private.py check | matomo-path | prepare <plain> | record <plain> <cipher> <digest>")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"ERROR: Private configuration check failed: {error}", file=sys.stderr)
        sys.exit(2)
