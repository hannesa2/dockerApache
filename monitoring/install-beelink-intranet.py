#!/usr/bin/env python3
"""Install the beelink intranet link page without changing public vhosts."""

import argparse
import hashlib
import html
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parent
VHOSTS = Path("/daten/beelink-runtime/frontend/vhosts")
LEGACY_HTML = """<!doctype html>
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

INVENTORY = {
    "beelink": (
        ("WordPress www", "https://www.mxtracks.info/wordpress/", "Produktion"),
        ("WordPress dev", "https://dev.mxtracks.info/wordpress/", "Produktion"),
        ("Matomo", "https://www.mxtracks.info/piwik/", "Produktion"),
        ("ownCloud", "https://www.mxtracks.info/owncloud/", "Produktion"),
        ("MXServer / GlassFish", ("https://www.mxtracks.info/backend/MXServer/",
                                  "https://www.mxtracks.info/backend/MXServer/rest/tracks/",
                                  "http://192.168.178.142:8081/MXServer/"),
         "Produktion; REST erfordert Anmeldung; letzter Link direkter LAN-Zugriff"),
        ("GitLab", "https://git.mxtracks.info/users/sign_in", "Produktion"),
        ("GitHub-Artefakte", "https://www.mxtracks.info/github/", "Produktion"),
        ("Home Assistant", ("https://homeassistant.mxtracks.info/", "http://beelink:8123/"),
         "Produktion; LAN-Zugriff unverschlüsselt"),
        ("WildFly Anwendungen", "http://192.168.178.142:8080/", "Direkter LAN-Zugriff"),
        ("WildFly Verwaltung", "http://192.168.178.142:9990/console/", "Unverschlüsselt; nur im vertrauenswürdigen LAN"),
        ("GlassFish Verwaltung", "https://192.168.178.142:4848/", "Selbstsigniertes Zertifikat"),
        ("GitLab Runner", None, "Docker-Runner; keine Weboberfläche"),
        ("DNS / DNS over TLS", None, "DNS-Dienst; keine Weboberfläche"),
        ("PostgreSQL / MySQL / Redis", None, "Interne Datenbanken und Cache; keine Weboberfläche"),
        ("Sicherungen und Spiegeljobs", None, "Systemdienste; keine Weboberfläche"),
    ),
    "mac2016": (
        ("WordPress www", "https://www16.mxtracks.info:8443/wordpress/", "Spiegel"),
        ("WordPress dev", "https://dev16.mxtracks.info:8443/wordpress/", "Spiegel"),
        ("Matomo", "https://www16.mxtracks.info:8443/piwik/", "Spiegel"),
        ("ownCloud", "https://www16.mxtracks.info:9443/owncloud/", "Spiegel"),
        ("MXServer / GlassFish", ("https://www16.mxtracks.info:9433/backend/MXServer/",
                                  "https://www16.mxtracks.info:8443/backend/MXServer/rest/tracks/"),
         "GlassFish-Spiegel; REST erfordert Anmeldung"),
        ("GitLab", "https://git16.mxtracks.info/users/sign_in", "Standby"),
        ("GitHub-Artefakte", "https://www16.mxtracks.info:8443/github/", "Spiegel; Prüfung am 19.11.2026: HTTP 404"),
        ("Home Assistant", "http://mac2016:8123/", "Vorbereiteter Standby; ausgeschaltet"),
        ("Nextcloud", "http://mac2016:8082/", "Eigenständige Instanz; HTTP im LAN"),
        ("PostgreSQL / MySQL / Redis", None, "Interne Datenbanken und Cache; keine Weboberfläche"),
        ("Sicherungen und Spiegeljobs", None, "Verschlüsseltes Restic-Repository und Spiegel; keine Weboberfläche"),
    ),
}


def render_page():
    by_host = {host: {name: (url, note) for name, url, note in rows}
               for host, rows in INVENTORY.items()}
    names = list(dict.fromkeys(name for rows in INVENTORY.values()
                               for name, _url, _note in rows))

    def cell(entry):
        if entry is None:
            return "<td>&mdash;</td>"
        url, note = entry
        urls = (url,) if isinstance(url, str) else (url or ())
        link = "".join(f'<a href="{html.escape(item, quote=True)}">'
                       f'{html.escape(item)}</a><br>' for item in urls)
        return f'<td>{link}<span class="comment">{html.escape(note)}</span></td>'

    rows = [f'<tr><th scope="row">{html.escape(name)}</th>'
            + cell(by_host["beelink"].get(name))
            + cell(by_host["mac2016"].get(name)) + "</tr>" for name in names]
    return """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>beelink - Intranet</title>
  <style>
    body { font: 16px/1.5 system-ui, sans-serif; color: #17212b;
           background: #f3f5f7; margin: 0; padding: 2rem 1rem; }
    main { max-width: 1200px; margin: auto; background: white;
           padding: clamp(1rem, 4vw, 2rem); border-radius: 12px; }
    h1 { margin-top: 0; }
    .table-wrap { overflow-x: auto; }
    table { border-collapse: collapse; width: 100%; min-width: 640px; }
    th, td { padding: .8rem; text-align: left; vertical-align: top; border-bottom: 1px solid #dce2e8; }
    thead th { background: #edf2f7; }
    tbody th { width: 18%; }
    a { color: #0757a6; overflow-wrap: anywhere; }
    .note, .comment { color: #475569; }
  </style>
</head>
<body>
<main>
  <h1>beelink - Intranet</h1>
  <p>Inventar und Einstiegsadressen. Die Liste zeigt keine Live-Statusanzeige.</p>
  <p class="note">Spiegel sind keine unabhängigen Produktionssysteme. Für Domain-URLs
  muss die DNS-Auflösung zum jeweiligen Host passen; insbesondere im WLAN kann
  eine abweichende DNS-Auflösung den Zugriff verhindern. Direkte Java-Ports sind
  auf das vertrauenswürdige LAN beschränkt.</p>
  <div class="table-wrap"><table>
    <thead><tr><th scope="col">Dienst</th>
      <th scope="col">beelink &ndash; Einstiegspunkt und Kommentar</th>
      <th scope="col">mac2016 &ndash; Einstiegspunkt und Kommentar</th></tr></thead>
    <tbody>
""" + "\n".join(rows) + """
    </tbody>
  </table></div>
</main>
</body>
</html>
"""


HTML = render_page()
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


def replace_page(page, content):
    fd, name = tempfile.mkstemp(prefix=".index-", dir=page.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
        temporary.chmod(0o644)
        os.replace(temporary, page)
    finally:
        temporary.unlink(missing_ok=True)


def verify_page(expected):
    body = subprocess.check_output([
        "curl", "--noproxy", "*", "--fail", "--silent", "--show-error",
        "--retry", "5", "--retry-delay", "1", "--retry-all-errors",
        "--max-time", "15", "--resolve", "beelink:80:192.168.178.142",
        "http://beelink/"], text=True, encoding="utf-8")
    if body != expected:
        raise RuntimeError("Intranet response differs from the installed page")


def update_page(directory, config):
    page = directory / "index.html"
    if (directory.is_symlink() or not directory.is_dir() or config.is_symlink()
            or not config.is_file() or page.is_symlink() or not page.is_file()):
        raise RuntimeError("Expected existing intranet directory, configuration and page")
    if config.read_text() != CONFIG:
        raise RuntimeError("Intranet configuration differs; inspect before updating")
    previous = page.read_text(encoding="utf-8")
    previous_digest = hashlib.sha256(previous.encode("utf-8")).hexdigest()
    known_digests = {
        "2f5b202d3fb01f9e55a780ae93a12ebd15da74da490528c11e8e55fdba3a381c",
        "c08a7efafe62503ace51553061dbddd7743adab63390eb1f7c45e025e8ba7601",
    }
    if previous not in (LEGACY_HTML, HTML) and previous_digest not in known_digests:
        raise RuntimeError("Intranet page differs; inspect before updating")
    if previous == HTML:
        verify_page(HTML)
        return
    replace_page(page, HTML)
    try:
        verify_page(HTML)
    except Exception:
        replace_page(page, previous)
        raise


def install(update=False):
    if os.getuid() != 0 or socket.gethostname().split(".")[0].lower() != "beelink":
        raise RuntimeError("Run as root directly on beelink")
    spec = importlib.util.spec_from_file_location(
        "intranet_control", ROOT / "control-beelink-staging.py")
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    control.check_mount()
    directory = VHOSTS / "intranet"
    config = VHOSTS / "zz-intranet.conf"
    if update:
        update_page(directory, config)
        print("Intranet inventory updated: http://beelink/ (public vhosts unchanged).")
        return
    if directory.exists() or directory.is_symlink() or config.exists() or config.is_symlink():
        raise RuntimeError("Intranet configuration already exists; inspect before replacing")
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    page = directory / "index.html"
    succeeded = False
    previous_permissions = VHOSTS.stat()
    command = ["docker", "exec", "beelink-frontend-frontend-1", "apache2ctl"]
    try:
        page.write_text(HTML, encoding="utf-8")
        page.chmod(0o644)
        config.write_text(CONFIG)
        config.chmod(0o644)
        allow_apache_traversal(VHOSTS)
        subprocess.run([*command, "configtest"], check=True)
        subprocess.run([*command, "graceful"], check=True)
        verify_page(HTML)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="update an existing unmodified intranet page")
    args = parser.parse_args()
    os.umask(0o077)
    import fcntl
    with Path("/run/lock/beelink-staging-control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install(update=args.update)
