#!/usr/bin/env python3
"""Install the beelink intranet link page without changing public vhosts."""

import importlib.util
import os
from pathlib import Path
import socket
import subprocess


ROOT = Path(__file__).resolve().parent
VHOSTS = Path("/daten/beelink-runtime/frontend/vhosts")
HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>beelink - Intranet</title>
  <style>
    body { font: 16px/1.5 system-ui, sans-serif; color: #17212b;
           background: #f3f5f7; margin: 0; padding: 2rem 1rem; }
    main { max-width: 860px; margin: auto; background: white;
           padding: clamp(1rem, 4vw, 2rem); border-radius: 12px; }
    h1 { margin-top: 0; }
    table { border-collapse: collapse; width: 100%; }
    th, td { padding: .8rem; text-align: left; border-bottom: 1px solid #dce2e8; }
    th { background: #edf2f7; }
    a { color: #0757a6; overflow-wrap: anywhere; }
    .note { color: #475569; margin-bottom: 0; }
  </style>
</head>
<body>
<main>
  <h1>beelink - Intranet</h1>
  <p>Direkter Zugriff im WLAN/LAN.</p>
  <table>
    <thead><tr><th scope="col">Dienst</th><th scope="col">URL</th></tr></thead>
    <tbody>
      <tr><td>WildFly Anwendungen</td><td><a href="http://192.168.178.142:8080/">http://192.168.178.142:8080/</a></td></tr>
      <tr><td>WildFly Verwaltung</td><td><a href="http://192.168.178.142:9990/console/">http://192.168.178.142:9990/console/</a></td></tr>
      <tr><td>GlassFish MXServer</td><td><a href="http://192.168.178.142:8081/MXServer/">http://192.168.178.142:8081/MXServer/</a></td></tr>
      <tr><td>GlassFish Verwaltung</td><td><a href="https://192.168.178.142:4848/">https://192.168.178.142:4848/</a></td></tr>
    </tbody>
  </table>
  <p class="note">Verwaltung mit vorhandenen Zugangsdaten. GlassFish verwendet ein
  selbstsigniertes Zertifikat. WildFly-Verwaltung nur im vertrauensw&uuml;rdigen LAN nutzen.</p>
</main>
</body>
</html>
"""
CONFIG = """<VirtualHost *:80>
    ServerName beelink
    ServerAlias beelink.local 192.168.178.142
    DocumentRoot /etc/apache2/sites-enabled/intranet
    <Directory /etc/apache2/sites-enabled/intranet>
        Options -Indexes
        AllowOverride None
        Require all granted
        DirectoryIndex index.html
    </Directory>
</VirtualHost>
"""


def allow_apache_traversal(directory):
    stat = directory.stat()
    os.chown(directory, stat.st_uid, 33)
    directory.chmod((stat.st_mode & 0o777) | 0o010)
    return stat


def install():
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    spec = importlib.util.spec_from_file_location(
        "intranet_control", ROOT / "control-beelink-staging.py")
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    control.check_mount()
    directory = VHOSTS / "intranet"
    config = VHOSTS / "zz-intranet.conf"
    if directory.exists() or directory.is_symlink() or config.exists() or config.is_symlink():
        raise RuntimeError("Intranet configuration already exists; inspect before replacing")
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    page = directory / "index.html"
    succeeded = False
    previous_permissions = VHOSTS.stat()
    command = ["docker", "exec", "beelink-frontend-frontend-1", "apache2ctl"]
    try:
        page.write_text(HTML)
        page.chmod(0o644)
        config.write_text(CONFIG)
        config.chmod(0o644)
        allow_apache_traversal(VHOSTS)
        subprocess.run([*command, "configtest"], check=True)
        subprocess.run([*command, "graceful"], check=True)
        body = subprocess.check_output([
            "curl", "--noproxy", "*", "--fail", "--silent", "--show-error",
            "--retry", "5", "--retry-delay", "1", "--retry-all-errors",
            "--max-time", "15", "--resolve", "beelink:80:192.168.178.142",
            "http://beelink/"], text=True)
        if body != HTML:
            raise RuntimeError("Intranet response differs from the installed page")
        succeeded = True
    finally:
        if not succeeded:
            if config.exists():
                config.unlink()
            if page.exists():
                page.unlink()
            directory.rmdir()
            os.chown(VHOSTS, previous_permissions.st_uid, previous_permissions.st_gid)
            VHOSTS.chmod(previous_permissions.st_mode & 0o777)
            subprocess.run([*command, "configtest"], check=True)
            subprocess.run([*command, "graceful"], check=True)
    print("Intranet page installed: http://beelink/ (public vhosts unchanged).")


if __name__ == "__main__":
    os.umask(0o077)
    import fcntl
    with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install()
