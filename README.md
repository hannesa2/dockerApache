## macOS mit OrbStack

OrbStack stellt die Docker-kompatible CLI bereit. Die bestehenden Compose-Dateien
können unverändert verwendet werden; Apache und Nextcloud bleiben getrennte Stacks
und behalten ihre Daten in den jeweiligen Host-Verzeichnissen.

```bash
# Im Repository-Verzeichnis: prüfen, dass OrbStack läuft und den OrbStack-Kontext nutzen
docker context ls
docker context use orbstack
docker context show

# Beide Stacks starten (Apache-Image wird lokal gebaut)
docker compose -f apache/docker-compose.yml up -d --build
docker compose -f nextcloud/docker-compose.yml up -d

# Status prüfen
docker compose -f apache/docker-compose.yml ps
docker compose -f nextcloud/docker-compose.yml ps
```

Die Weboberflächen sind über die in den jeweiligen `.env`-Dateien konfigurierten
Ports erreichbar (standardmäßig Apache unter `http://localhost:8081` und
Nextcloud unter `http://localhost:8082`). In den OrbStack-Einstellungen den
automatischen Start bei der Anmeldung aktivieren, damit die Container mit OrbStack
wieder starten.

Zum Stoppen die Stacks einzeln herunterfahren; die Host-Daten bleiben erhalten:

```bash
docker compose -f apache/docker-compose.yml down
docker compose -f nextcloud/docker-compose.yml down
```

## Private Konfiguration sichern und versionieren

`.env`-Dateien und private Anwendungskonfigurationen (zum Beispiel Matomos
`config.ini.php`) bleiben im normalen Arbeitsverzeichnis unverschlüsselt und
von Git ignoriert. Für sie gibt es zwei getrennte Schutzschichten:

- **Versionen:** SOPS verschlüsselt jede Datei mit einem öffentlichen
  [age](https://github.com/FiloSottile/age)-Empfänger. Nur verschlüsselte
  Dateien werden committet. Ein passwortgeschützter age-Schlüssel kann
  ebenfalls als Ciphertext versioniert werden; der Klartext-Schlüssel gehört
  niemals ins Repository.
- **Wiederherstellung:** Restic erstellt verschlüsselte, zeitpunktbezogene
  Backups auf einem anderen Datenträger oder einem entfernten Backup-Ziel.
  Repository-Passwort und Passphrase müssen unabhängig vom Backup-Ziel
  wiederherstellbar sein.

Installiere SOPS, age, OpenSSL und Restic über den Paketmanager des jeweiligen
Systems. Erzeuge einmalig einen age-Schlüssel:

```bash
age-keygen -o ~/.config/sops/age/keys.txt
age-keygen -y ~/.config/sops/age/keys.txt
```

Die zweite Ausgabe ist der öffentliche Empfänger, kein Geheimnis. Setze ihn in
`AGE_RECIPIENT` und verschlüssele die Dateien nach jeder Änderung erneut:

```bash
mkdir -p private-config
export AGE_RECIPIENT='age1...' # öffentlichen Empfänger einsetzen

sops --encrypt --age "$AGE_RECIPIENT" --input-type dotenv --output-type dotenv \
  apache/.env > private-config/apache.env.enc
sops --encrypt --age "$AGE_RECIPIENT" --input-type dotenv --output-type dotenv \
  nextcloud/.env > private-config/nextcloud.env.enc
sops --encrypt --age "$AGE_RECIPIENT" --input-type ini --output-type ini \
  /Users/hannes/server/apache/www/mxdocs/piwik/config/config.ini.php \
  > private-config/matomo-config.ini.enc
```

Schütze anschließend den privaten age-Schlüssel einmalig mit einer langen,
merkbaren Passphrase:

```bash
sops --decrypt --input-type dotenv --output-type dotenv \
  private-config/apache.env.enc > apache/.env
sops --decrypt --input-type ini --output-type ini \
  private-config/matomo-config.ini.enc \
  > /Users/hannes/server/apache/www/mxdocs/piwik/config/config.ini.php
```

Das Skript speichert den Age-Schlüssel nur als passwortgeschützte Datei
`private-config/age-identity.txt.enc` (AES-256-CBC mit PBKDF2) und fragt die
Passphrase beim Entschlüsseln verdeckt ab. Verwende eine einzigartige,
mehrwortige Passphrase; wer sie vergisst, kann die Dateien nicht mehr
wiederherstellen. Der private Age-Schlüssel wird nur vorübergehend in eine
Datei mit Zugriffsrechten `0600` entschlüsselt und danach entfernt.

Entschlüssele mit dem Wrapper in die ignorierten Originalpfade:

```bash
./private-config/decrypt.sh apache
./private-config/decrypt.sh nextcloud
./private-config/decrypt.sh matomo
```

Der Matomo-Zielpfad kann bei Bedarf mit `MATOMO_CONFIG_PATH=/pfad/zur/config.ini.php`
überschrieben werden. Prüfe vor dem Commit, dass ausschließlich Ciphertext
hinzugefügt wird.

Für Restic müssen `RESTIC_REPOSITORY` auf ein **anderes** Medium oder einen
entfernten Host und `RESTIC_PASSWORD_FILE` auf eine außerhalb des Repositorys
und Backup-Ziels aufbewahrte Passwortdatei zeigen. Initialisiere das Ziel nur
einmal mit `restic init`; danach können die kritischen Konfigurationen
regelmäßig gesichert werden:

```bash
restic backup apache/.env nextcloud/.env \
  /Users/hannes/server/apache/www/mxdocs/piwik/config/config.ini.php \
  --tag dockerApache-private-config
restic snapshots
```

Ergänze die Backup-Auswahl um weitere tatsächlich benötigte private Dateien,
etwa zusätzliche Stack-`.env`-Dateien und selbst verwaltete TLS-Schlüssel.
Führe regelmäßig einen Restore-Test in ein separates Testverzeichnis durch.
Ein Backup auf demselben Samsung-Laufwerk schützt nicht vor dessen Ausfall.
Ein privates Git-Repository ist optional; wenn verwendet, dort ebenfalls nur
SOPS-verschlüsselte Dateien ablegen. Bereits versehentlich committete Klartexte
gelten als offengelegt: Zugangsdaten rotieren und die Git-Historie bereinigen.

## Apache (with MySQL)

A self-contained Apache + PHP stack using **MySQL 8.4** as the database.  
All web files, vhost configs, logs and database files live **outside** the container on the host.  
The image is built for **linux/amd64** and **linux/arm64** (Apple Silicon, Raspberry Pi, …).

### Quick start

```bash
cd apache

# 1. Copy and edit the env file
cp .env.example .env
#    → set APACHE_DATA_DIR, ports, passwords, …

# 2. Create host directories and install the default vhost
mkdir -p data/www data/vhosts data/logs/apache data/mysql data/mysql-config

# Copy sample files to get started immediately
mkdir -p data/vhosts/
mkdir -p data/www/
cp sample-data/vhosts/000-default.conf data/vhosts/
cp sample-data/www/index.php data/www/
# Optional: second vhost example
# cp sample-data/vhosts/myapp.conf data/vhosts/

# 2.1 on MacOS, unlock the keychain to avoid "security: SecKeychainUnlock: The user name or passphrase you entered is not correct" errors when the container tries to access the mounted volumes
security -v unlock-keychain ~/Library/Keychains/login.keychain-db

# 3. Start the stack (builds the image automatically on first run)
docker compose up -d

# To force a rebuild after changing the Dockerfile:
docker compose up -d --build

# Open http://localhost:8081 (or the port you configured)
# Note: Apache uses 8081 by default to avoid conflict with Nextcloud (8082)
```

### Specifying the data directory via command line

```bash
APACHE_DATA_DIR=/mnt/mydata docker compose up -d
```

Everything Apache and MySQL stores is then written under `/mnt/mydata/`:

```
/mnt/mydata/
  www/                 ← Web root – put your PHP/HTML files here
  vhosts/              ← Apache vhost config files (*.conf)
  logs/apache/         ← Apache access and error logs
  mysql/               ← MySQL data files
  mysql-config/        ← Custom MySQL config (*.cnf)
```

### Deploying Let’s Encrypt certificates from latitude

On this setup, Certbot on `latitude` renews `www16.mxtracks.info` and
`dev16.mxtracks.info`. The deploy hook is also prepared to deploy
`git16.mxtracks.info` once its certificate has been issued. It sends only those
certificates to `mac2016` using a dedicated SSH key restricted to the
certificate receiver. After installing a renewed pair, the receiver validates
Apache's configuration and gracefully reloads Apache. The private keys are
installed with mode `0600`; they are not stored in this repository. The Snap
Certbot timer is the active renewal timer; the duplicate distribution-package
timer is disabled.

The scripts are `apache/letsencrypt-deploy-hook.sh` (installed on latitude as
`/etc/letsencrypt/renewal-hooks/deploy/mac2016-cert-deploy`) and
`apache/mac2016-cert-receiver.sh` (installed on macOS as
`~/bin/mac2016-cert-receiver.sh`). The receiver is specific to this host’s
OrbStack context, Compose file, and `/Users/hannes/server/apache/certs` data
directory. Run a Certbot dry-run after changing renewal settings:

```bash
sudo snap run certbot.renew --dry-run --cert-name dev16.mxtracks.info
sudo snap run certbot.renew --dry-run --cert-name www16.mxtracks.info
```

### GitLab CE standby copy

The GitLab CE service in `gitlab/docker-compose.yml` is a separate standby copy
of the GitLab instance on `latitude`, pinned to the same GitLab version. On
mac2016 it has no published host ports; it shares the private `gitlab_proxy`
Docker network with the Apache container. Apache selects the backend vhost from
the request hostname and reverse-proxies `git16.mxtracks.info` to GitLab over
internal HTTP. TLS terminates at Apache on port `443`; the GitLab container
does not manage certificates. Its config, logs and application data live under
`/Users/hannes/server/gitlab/`. There is currently no `git16.mxtracks.info`
certificate; HTTPS will become available after issuance and deployment.

DNS only resolves each hostname to an IP address; Apache routes by the HTTP
`Host` header and TLS SNI. The router must forward public TCP `9080` to
mac2016's host port `9080` for that legacy HTTP endpoint. GitLab's canonical URL
is now `https://git16.mxtracks.info/`, intended for home-LAN access. A private
`apache/docker-compose.override.yml` publishes host TCP `443` to container
TCP `443`; the existing high-port bindings remain available.
LAN DNS must resolve `git16.mxtracks.info` to mac2016 (`192.168.178.23` or
`192.168.178.70`), without returning the public IPv6 address. This DNS/client
configuration is separate from the server setup; changing the GitLab URL does
not configure the router's DNS. No public TCP 443 forwarding change is needed
for LAN-only use. If
`git16.mxtracks.info:9080` shows macOS's native Apache welcome page, check that
forwarding target: the request is reaching native port `80` rather than the
Docker Apache port. No additional public port is needed.

The Apache vhost snippets are `gitlab/apache-git16-http.conf` and
`gitlab/apache-git16-https.conf`. The HTTP vhost proxies requests to the
internal GitLab service. Enable the HTTPS vhost after the matching Let's
Encrypt certificate has been deployed to mac2016.

The source-side job creates a consistent GitLab backup once per day and streams
it to mac2016 over SSH. The dedicated SSH key can run only
`~/bin/gitlab-mac-backup-receiver.sh`; the receiver validates the archive,
keeps the newest backup from each of the two newest months, and refuses a
transfer if less than 50 GiB is free before receiving the backup. Daily transfers therefore
update the current month's retained snapshot rather than accumulating daily
copies. It stores `gitlab.rb` and
`gitlab-secrets.json` with each backup. The private key and secret file are
never stored in this repository.

This is a daily backup, not continuous replication: the mac2016 copy does not
receive changes immediately, and any writes made to the standby are independent
of the production instance. In a disaster, first ensure the `latitude` instance
cannot return as an active writer, then restore the latest backup on mac2016 and
route `git16.mxtracks.info` to it. Keep both instances on exactly the same
GitLab version for backup restoration. The public DNS/reverse-proxy cutover is a
separate networking step.

`www16`, `dev16` and `git16` all use the same MyFRITZ CNAME target, so use the
same HTTP-01 standalone flow as the existing `www16` and `dev16` lineages.
Apache on latitude must be stopped briefly so Certbot can listen on port 80.
For the initial issuance, run this on latitude; the shell trap restarts Apache
even if Certbot fails:

```bash
sudo bash -c '
set -e
systemctl stop apache2
trap "systemctl start apache2" EXIT
snap run certbot certonly --standalone \
  --cert-name git16.mxtracks.info \
  -d git16.mxtracks.info
'
```

After successful issuance, deploy the initial certificate to mac2016 using the
installed restricted deploy hook:

```bash
sudo env RENEWED_LINEAGE=/etc/letsencrypt/live/git16.mxtracks.info \
  /etc/letsencrypt/renewal-hooks/deploy/mac2016-cert-deploy
```

The existing Certbot pre/post hooks stop and start Apache during future
standalone renewals; the deploy hook transfers each renewed certificate to
mac2016 and reloads its Apache container.

The standby can be restored and run without a `git16` certificate; the
certificate is needed only to enable HTTPS at Apache. On the first restore,
GitLab's data/config/logs directories must be empty. Run the restore helper
from the repository:

```bash
./gitlab/restore-standby.sh
```

It restores the newest received backup by default, verifies the archive,
secrets file and available storage, and refuses to overwrite existing GitLab
state unless resuming an interrupted restore. Test it only when intentionally
initializing the standby; for a routine backup, the scheduled job transfers
data without starting or modifying the standby GitLab service.

### GitHub artifacts mirror

The `/github` file-management app from `latitude` is mirrored on mac2016 as a
private `github-artifacts` container, based on the pinned
`githubArtifactsPy` submodule revision `b657c71b4e025bfdcc4f2418c70e15301010b62a`.
Apache proxies `/github/` from the `www16.mxtracks.info` vhost to the app over
the private Compose network. The app's WSGI prefix handling preserves its
subpath URLs. The app has no published host port.

The initial upload files and `parameter.yml` live outside the repository under
`/Users/hannes/server/githubArtifactsPy/`. The parameter file contains the
same upload authentication as the latitude service and must remain private.
Do not commit it. A macOS LaunchAgent runs `github-artifacts/sync-latitude.sh`
at 03:35 each day. It mirrors files one way from
`latitude:/daten/githubArtifactsPy/` and propagates changes and deletions;
mirror-only changes will be overwritten or deleted. Disable that sync before
promoting mac2016 as the writable primary.

The existing HTTPS listener on host port `9443` remains unchanged. A second
local alias on host port `8443` serves the same Apache vhost, so the mirror URL
is `https://www16.mxtracks.info:8443/github/`. The host listener is added by
Compose; this setup does not change the FRITZ!Box port-forwarding rules.

### Matomo/Piwik mirror

On mac2016, Apache, GitLab, GitHub Artifacts and Nextcloud data reside under
`/Users/hannes/server/{apache,gitlab,githubArtifactsPy,nextcloud}` on internal
storage. ownCloud and MXServer data reside on the external Samsung T5 at
`/Volumes/SamsungT5_1T/ownCloudBackup/owncloud` and
`/Volumes/SamsungT5_1T/MXServerMirror`. Their installed Latitude sync jobs
retain the daily 04:00 and 04:30 schedules. After changing these volume paths,
run `sudo bash monitoring/relocate-t7-sync-paths.sh` on Latitude to update the
root-owned installed copies as well as the repository scripts.
Private `.env` files and `docker-compose.override.yml` files retain the running
services' credentials and host-specific settings; do not commit them.
The mac2016 health monitor checks the data directories and a 50 GiB free-space
reserve. The GitLab backup receiver also requires at least 50 GiB free before
accepting a new backup.
The internal filesystem is case-insensitive, so the existing MySQL databases
were migrated by SQL export/import rather than reusing the case-sensitive
external disk's MySQL binary data directory.

Matomo under `mxdocs/piwik` is served directly by the existing
Apache/PHP container at `https://www16.mxtracks.info:8443/piwik/`, including
the tracking endpoint `piwik.php`. `monitoring/sync-matomo.py` copies application
code and a consistent `piwik_db` snapshot from the **native production**
installation on Latitude, not the outdated Docker copy. The mirror retains its
own database credentials (`db` / `matomo_mirror`) and trusted hostnames.
Shipped configuration defaults (`config/global.ini.php` and related files) are
updated with the application code; only the private `config.ini.php*` files are
excluded from that copy. The defaults checksum is verified before reopening
the mirror.
The WordPress sync excludes `/piwik` from file copying and URL rewriting.

On Latitude, install the daily **04:45** sync and availability-mail reporting:

```bash
sudo bash monitoring/install-latitude-matomo-sync.sh
```

The installer requires a successful initial sync before enabling cron (a success
within the preceding 24 hours is reused). Run manually as `hannes` on Latitude:

```bash
/usr/local/sbin/run-sync-with-email "Matomo mirror sync" \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/sync-matomo.py --apply
```

Without `--apply`, only preflight checks run. Production is never stopped or
modified. Mirror `/piwik` returns HTTP 403 while its code/database are replaced;
on failure it remains blocked rather than serving an inconsistent installation.
Previous mirror database/config snapshots are retained in
`~/.local/state/matomo-mirror/pre-sync-*` on mac2016. Review and remove obsolete
snapshots manually as needed. Before retrying a failed replacement, inspect the
error and backup and remove only the sync-created root `piwik/.htaccess`
(`Require all denied`); do not remove Matomo's subdirectory access controls.
Mirror-only tracking and configuration changes are overwritten on the next sync,
never replicated back. Runtime credentials and backups are private; never commit
them. Current paths default to `/srv/www/vhosts/mxdocs/piwik` on Latitude and
`/Users/hannes/server/apache/www/mxdocs/piwik` on mac2016; the script accepts
`MATOMO_SOURCE_DIR` and `MATOMO_MIRROR_DIR` overrides.

On latitude, native Apache serves the primary Matomo installation from
`/srv/www/vhosts/mxdocs/piwik`. PHP 8.4-FPM is enabled only for PHP URLs below
`/piwik/` using Apache's late per-request `<If>` section, so it overrides the
global legacy mod_php handler without changing other sites. Install the scoped
handler and reload Apache:

```bash
sudo install -o root -g root -m 0644 apache/latitude-matomo-php84.conf \
  /etc/apache2/conf-available/latitude-matomo-php84.conf
sudo a2enconf latitude-matomo-php84
sudo apache2ctl configtest && sudo systemctl reload apache2
```

PHP 8.4-FPM and its required extensions must be installed and active. If the
config test fails, disable the new snippet with
`sudo a2disconf latitude-matomo-php84` before making other Apache changes.

### ownCloud on mac2016

mac2016 is the current ownCloud source while latitude is offline. The local
instance was upgraded from 10.16.6 to 10.16.7 using its existing code, files,
and PostgreSQL database; no latitude sync was needed or performed. It runs in
the dedicated `owncloud/server:10.16.7` PHP runtime, isolated from the main PHP
8.3 Apache container.

The application remains in maintenance mode, and Apache denies `/owncloud`
until service is intentionally enabled. Do not run
`owncloud/sync-latitude.sh` or install its scheduled job while latitude is
offline: it would copy stale latitude data over mac2016.

To open access on mac2016, run `./owncloud/activate-proxy.sh`, then disable
ownCloud maintenance mode. For the close-access procedure, see
[`owncloud/README.md`](owncloud/README.md#enable-owncloud-access). Proxy
activation does not require a sync marker.

### Hourly system availability monitor

The hourly check runs as the `hannes` user on latitude, checks the host
services and Docker applications on both latitude and mac2016, and checks
their HTTP endpoints. A second monitor runs from mac2016 so Latitude failures
are reported even if Latitude never starts. Both send an initial report, then
email only when checks fail or recover; unchanged failures are logged without
hourly email spam.

GitLab login-page checks allow 30 seconds on both hosts, compared with 8
seconds for other HTTP checks. This still checks the real login route; a
healthy container alone does not prove the web application is available.

Both availability emails include a Latitude sync-job overview: last start,
finish, result, last successful completion, and the next expected run from
enabled entries in the `hannes` crontab and `/etc/cron.d`. Expected times use
Latitude's timezone and assume the host and cron are running. Unscheduled jobs
(including ownCloud when no schedule is installed) are explicitly identified.
Missing historical timestamps are reported as unknown, not guessed from log
modification times. Job timing does not add hourly emails: unchanged
availability still suppresses notifications.

Install timestamp recording directly on Latitude:

```bash
sudo bash monitoring/install-sync-job-status.sh
```

The existing job-email wrapper writes atomic, credential-free status records
under `/home/hannes/.local/state/sync-job-notify/status/`. The installer also
wraps the existing Nextcloud cron command without changing its schedule.
Existing PostgreSQL, GitLab, Apache and MXServer wrapper jobs begin recording
on their next execution; no jobs are run by the installer. Both monitors use
`/home/hannes/bin/sync-job-report.py` on Latitude; the mac2016 monitor retrieves
the report over SSH and explicitly reports when Latitude cannot be reached.

The manually deployed Latitude GitLab can be recreated with bounded Docker
logs using `python3 monitoring/recreate-gitlab-logrotation.py --apply`, as a
user with Docker socket access, while `gitlab` is stopped. The helper preserves
the exact local image, configuration (including private environment), ports,
and `/srv/gitlab` bind mounts; it does not upgrade GitLab. Logs are limited to
three 50 MB files. Private original deployment metadata is saved under
`~/.local/state/gitlab-logrotation/<timestamp>/`.
The old container is retained as `gitlab-pre-logrotation-<timestamp>` with
automatic restart disabled. Never start both containers: their database files
are shared. These log bounds must also be retained in any future replacement
deployment; changing daemon defaults does not retrofit existing containers.

### Removing host PHP 7.x on latitude

Latitude's legacy Apache configuration loads `conf.d/` but not all
`conf-enabled/*.conf` snippets. If Matomo displays PHP source after module
removal, immediately run `sudo bash monitoring/fix-latitude-php-handler.sh`.
It explicitly includes the scoped PHP 8.4 handler via the loaded directory,
tests/reloads Apache, verifies execution of a temporary PHP probe (removed
on exit), and rejects Matomo responses containing PHP source. HTTP 200 alone
is not evidence that PHP executed.

The web applications use PHP 8.4 FPM; the legacy Apache PHP module and CLI
alternatives still need to be removed separately. After repairing any Apache
configuration errors, run the preflight and then the apply mode on latitude:

```bash
sudo bash monitoring/remove-host-php7.sh --preflight
sudo bash monitoring/remove-host-php7.sh --apply
```

The helper routes the mxdocs web root through PHP 8.4 FPM, disables enabled
PHP 7 Apache modules, verifies the Apache config and that Matomo and both
WordPress endpoints retain their current HTTP responses (or mxdocs WordPress
improves from HTTP 500 to 200), then purges installed
PHP 7.x packages without `autoremove`. It keeps a root-only record under
`/root/.local/state/remove-host-php7/`. The mxdocs WordPress endpoint currently
returns HTTP 500 under the old handler; the helper allows recovery to HTTP 200
but rejects other response changes.

Do not run `--apply` while `sudo apache2ctl -t` fails. An unprivileged test
can report certificate files as missing simply because it cannot access
protected Let's Encrypt directories. Latitude's privileged configuration test
passed on November 9, 2026; no certificate-path defect was established.

Install it on latitude from the current checkout:

```bash
cd ~/git/dockerApache
./monitoring/install-latitude-monitor.sh
```

The user crontab runs it at the start of every hour. Logs and notification
state are stored under `~/.local/state/systems-health-monitor/`. This
latitude-hosted monitor cannot send an alert while latitude itself is powered
off or disconnected; it reports that condition only after the host runs again.

Install the second monitor on mac2016 from its repository checkout:

```bash
cd ~/git/dockerApache
./monitoring/install-mac2016-monitor.sh
```

It runs at minute 10 of every hour from the `hannes` user crontab, independently
of an interactive login. It checks mac2016 locally and Latitude over SSH; when
Latitude is offline, mac2016 emails that failure. mac2016 itself must be
powered on, connected to the network, and able to send email for this alert to
work. Its log and separate notification state are stored under
`~/.local/state/systems-health-monitor/`.

ownCloud's dedicated image provides its own PHP runtime because this legacy
application line is incompatible with the main PHP 8.3 Apache runtime. Keeping
the application version matched between latitude and mac2016 is essential for
reliable standby synchronization.

### MXServer standby mirror

The MXServer Java EE application is built from the sibling `MXServer` checkout
and runs in a Payara 5/Java 8 container on mac2016. Its PostgreSQL 14 database
and picture files are independent copies synchronized daily from latitude.
Mirror-generated email is captured privately instead of being delivered.

On mac2016, ensure `~/git/MXServer` and `~/git/dockerApache` are sibling
checkouts, then prepare the runtime:

```bash
cd ~/git/dockerApache
./mxserver/prepare-mirror.sh
```

On latitude, install the initial sync and daily 04:30 job:

```bash
cd ~/git/dockerApache
sudo ./mxserver/install-latitude-sync.sh
```

The mirror endpoint is `https://www16.mxtracks.info:9433/backend/`. Apache
terminates TLS using the existing www16 certificate; configure the router to
forward public TCP 9433 to mac2016 port 9433. See `mxserver/README.md` for
sync behavior and operations.

### Backup & Restore

```bash
cd apache

# Backup (creates a timestamped folder under ./backups/)
./backup.sh
./backup.sh /mnt/nas/apache-backups   # custom location

# Restore from a specific backup folder
./restore.sh ./backups/20260502_120000
```

Each backup folder contains:

| File | Contents |
|---|---|
| `mysql_webapp.sql.gz` | Full MySQL dump (gzipped SQL) |
| `www.tar.gz` | Web root files |
| `vhosts.tar.gz` | Apache vhost configs |

#### Manual database-only commands

```bash
# Dump
docker compose exec db \
  mysqldump -u webapp -pchangeme_db webapp | gzip > backup.sql.gz

# Restore
gunzip -c backup.sql.gz | docker compose exec -T db \
  mysql -u webapp -pchangeme_db webapp

# Interactive MySQL shell
docker compose exec db mysql -u webapp -pchangeme_db webapp
```

### Build the custom image locally (optional)

```bash
# amd64
docker buildx build --platform linux/amd64 -t apache-custom:latest apache/

# arm64
docker buildx build --platform linux/arm64 -t apache-custom:latest apache/
```

> The image is built automatically by `docker compose up -d` from `apache/Dockerfile` — no pull from Docker Hub needed.

### Start on boot

Same as Nextcloud — `restart: unless-stopped` handles crashes automatically.

**macOS (OrbStack):** Enable OrbStack's automatic start at login in its settings.

**Linux (systemd):** Use the same pattern as the Nextcloud service, pointing `WorkingDirectory` to your `apache/` deploy folder.

---

## Nextcloud (with PostgreSQL)

A self-contained Nextcloud stack using **PostgreSQL** as the database.  
All data, config and database files live **outside** the container on the host.  
The image is built for **linux/amd64** and **linux/arm64** (Apple Silicon, Raspberry Pi, …).

### Quick start

```bash
cd nextcloud

# 1. Copy and edit the env file
cp .env.example .env
#    → set NEXTCLOUD_DATA_DIR, passwords, trusted domains, …

# 2. Start the stack
docker compose up -d

# Open http://localhost:8082 (or change NEXTCLOUD_PORT in .env)
# Note: if running alongside the Apache stack, make sure ports don't conflict
#       Apache defaults to 8081, Nextcloud to 8082
```

### Stop / restart the stack

```bash
cd nextcloud

# Stop all containers (data is preserved)
docker compose down

# Stop and remove volumes (WARNING: deletes all container-internal data)
# → safe here because all data lives outside via bind-mounts
docker compose down -v

# ⚠️  Restart – does NOT re-read .env changes
docker compose restart

# ✅  After changing .env, always use down + up to apply the new values:
docker compose down && docker compose up -d

# Stop a single service, e.g. only Nextcloud
docker compose stop nextcloud
```

### Specifying the data directory via command line

```bash
NEXTCLOUD_DATA_DIR=/mnt/mydata docker compose up -d
```

Everything Nextcloud and PostgreSQL stores is then written under `/mnt/mydata/`:

```
/mnt/mydata/
  postgres/            ← PostgreSQL database files
  nextcloud/
    html/              ← Nextcloud web-root (core files)
    data/              ← User files
    config/            ← config.php and additional config
    custom_apps/       ← Manually installed apps
```

### Architecture override

Docker auto-detects your platform.  
If you need to force a specific architecture, set `ARCH` in `.env`:

```
ARCH=arm64   # Apple Silicon, Raspberry Pi 4+
ARCH=amd64   # Intel / AMD
```

### Build the custom image locally (optional)

```bash
# amd64
docker buildx build --platform linux/amd64 -t nextcloud-custom:latest nextcloud/

# arm64
docker buildx build --platform linux/arm64 -t nextcloud-custom:latest nextcloud/
```

---

### Backup & Restore

Two helper scripts are provided in the `nextcloud/` folder.  
They work from **outside** the containers using `docker compose exec` and standard
Unix tools — no extra clients need to be installed on the host.

#### Backup

```bash
cd nextcloud

# Backup to the default ./backups/<timestamp>/ folder
./backup.sh

# Backup to a custom location
./backup.sh /mnt/nas/nextcloud-backups
# or via env
BACKUP_DIR=/mnt/nas/nextcloud-backups ./backup.sh
```

Each run creates a timestamped sub-folder, e.g. `backups/20260501_120000/`, containing:

| File | Contents |
|---|---|
| `postgres_nextcloud.sql.gz` | Full PostgreSQL dump (gzipped SQL) |
| `nextcloud_config.tar.gz` | `config/` + `custom_apps/` directories |
| `nextcloud_data.tar.gz` | All user files under `data/` |

#### Restore

```bash
cd nextcloud

# Restore from a specific backup folder
./restore.sh ./backups/20260501_120000
```

The script will:
1. Ask for confirmation before overwriting anything
2. Enable Nextcloud **maintenance mode** automatically
3. Drop & recreate the database, then replay the SQL dump
4. Unpack config and user-data archives
5. Run `occ upgrade` + `occ maintenance:repair`
6. Disable maintenance mode

#### Manual database-only commands

If you only need to interact with the database (without the helper scripts):

```bash
# Dump only the database
docker compose exec db \
  pg_dump -U nextcloud nextcloud | gzip > backup.sql.gz

# Restore only the database
gunzip -c backup.sql.gz | docker compose exec -T db \
  psql -U nextcloud nextcloud

# Interactive psql shell
docker compose exec db psql -U nextcloud nextcloud
```

> **Tip:** Add `./backup.sh` to your host's crontab for automated daily backups:
> ```
> 0 2 * * * /path/to/nextcloud/backup.sh /mnt/nas/nextcloud-backups >> /var/log/nc-backup.log 2>&1
> ```

---

### Start on boot

All services already have `restart: unless-stopped` — they auto-recover from crashes.
What you additionally need depends on your OS:

#### macOS (OrbStack)
Enable OrbStack's automatic start at login in its settings.

The containers start automatically with OrbStack.

#### Linux (systemd)
A ready-made systemd unit file is provided at `nextcloud/nextcloud.service`.

```bash
# 1. Adjust WorkingDirectory inside the file to match your deploy path
nano nextcloud/nextcloud.service

# 2. Install and enable
sudo cp nextcloud/nextcloud.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable nextcloud.service
sudo systemctl start nextcloud.service

# Check status
sudo systemctl status nextcloud.service
```

#### Scan for manually added files

If you copy files directly into the data directory on the host, tell Nextcloud to index them:

```bash
cd nextcloud

# Scan one user
docker compose exec --user www-data nextcloud php occ files:scan hannes

# Scan all users
docker compose exec --user www-data nextcloud php occ files:scan --all

# Only scan new/changed files (faster on large libraries)
docker compose exec --user www-data nextcloud php occ files:scan --all --unscanned
```

Files must be placed under:
```
$NEXTCLOUD_DATA_DIR/nextcloud/data/<username>/files/
```

---

### LAN access / Fritz!Box NAT hairpinning fix

When Nextcloud is behind a Fritz!Box and you access it via the public domain
(`nextcloud.mxtracks.info`) **from inside the same home WiFi**, Fritz!Box
intercepts the request instead of forwarding it — you get a Fritz!Box error page.

**Fix: start the optional `dnsmasq` split-DNS service** so LAN devices resolve
the domain to latitude's **local IP** instead of the public one.

```bash
# Add to nextcloud/.env:
NEXTCLOUD_LOCAL_IP=192.168.178.129          # latitude's LAN IPv4
NEXTCLOUD_LOCAL_IPV6=fdf4:be15:...          # latitude's LAN IPv6 (ip -6 addr show scope global)
DNS_UPSTREAM=8.8.8.8                        # upstream DNS (not Fritz!Box – avoids loop)

# Start dnsmasq
docker compose --profile split-dns up -d
```

Keep the **DNS-Rebind-Schutz** exception for `nextcloud.mxtracks.info` in place
(Heimnetz → Netzwerk → DNS-Rebind-Schutz).

#### ⚠️ Fritz!Box-wide DNS setting does NOT work for MyFRITZ domains

Setting *Bevorzugter DNSv4-Server* to `192.168.178.129` in
**Internet → Zugangsdaten → DNS-Server** is **not enough** when the Nextcloud
domain is a CNAME pointing to `*.myfritz.net`:

```
nextcloud.mxtracks.info.  CNAME  xyqvgh0pmfs04c1b.myfritz.net.
```

Fritz!Box resolves its own MyFRITZ hostnames **internally** — it never forwards
those queries to our dnsmasq. Clients must query dnsmasq directly:

---

#### Fix A — Per-device: static DNS in Android WiFi settings

Android 10 and earlier:
> Settings → Wi-Fi → **long-press** network → Modify network → Advanced options
> → IP settings: **Static** → DNS 1: `192.168.178.129`

Android 12 – 16 (new UI, long-press removed):
> Settings → Network & internet → Internet → tap the **⚙️ gear icon** next to
> your Wi-Fi network → tap the **✏️ pencil icon** (top-right) → IP settings:
> **Static** → DNS 1: `192.168.178.129`, DNS 2: `8.8.8.8`

Also set **Private DNS → Off** (Settings → Network & internet → Private DNS).

---

#### Fix B — Android Private DNS with DoT (works on ALL Android versions)

Android's *Private DNS* feature uses **DNS-over-TLS (DoT)** on port 853.
Point it at `nextcloud.mxtracks.info` — Android resolves that hostname via
Fritz!Box (gets the public IP), connects to port 853, Fritz!Box forwards it to
latitude's CoreDNS service (which holds the valid Let's Encrypt cert), and from
then on all DNS goes through dnsmasq → local IP. ✓

**One-time setup:**

1. Add to `nextcloud/.env`:
   ```dotenv
   DOT_CERT_DIR=/etc/letsencrypt/live/nextcloud.mxtracks.info-0002
   ```

2. Start both services:
   ```bash
   docker compose --profile split-dns --profile dot up -d
   ```

3. In Fritz!Box → **Heimnetz → Portfreigaben**: add a port forward:
   - Protocol: **TCP**
   - External port: **853**
   - Internal host: **192.168.178.129** (latitude)
   - Internal port: **853**

4. On Android:
   > Settings → Network & internet → **Private DNS**
   > → *Private DNS provider hostname* → `nextcloud.mxtracks.info`

No static IP needed — works automatically on home WiFi **and** over mobile data.

---

#### IPv6 / Happy Eyeballs

Android prefers IPv6. Without an AAAA override in dnsmasq, Android receives
the public IPv6 address and bypasses local routing. Set `NEXTCLOUD_LOCAL_IPV6`
in `.env` to latitude's stable ULA IPv6 address:

```bash
# Find latitude's stable ULA address (scope global, not temporary):
ip -6 addr show scope global | grep -v temporary
```
