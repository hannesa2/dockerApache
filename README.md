## Backup-HDD auf beelink

Die vorhandene verschlüsselte WD-Festplatte wird über einen Schlüssel auf dem
verschlüsselten Systemlaufwerk bei Zugriff unter `/datenBackup` eingebunden.
`nofail` und systemd-Automount sollen den Boot auch ohne HDD ermöglichen; der
Fall ohne Platte und das automatische Entsperren nach Neustart sind noch nicht
praktisch geprüft. Schlüssel und LUKS-Header-Backups müssen getrennt und sicher
außerhalb von beelink aufbewahrt werden.

Der Dienst `beelink-backup-disk-idle.service` überwacht ausschließlich die
WD-HDD anhand ihrer Seriennummer und sendet nach mindestens 300 Sekunden ohne
I/O einen SCSI-Standby-Befehl über den geprüften offenen Geräte-Handle. Er
überwacht die physischen I/O-Zähler; anhaltende Zugriffe können die Abschaltung
verzögern. `hd-idle.service` bleibt maskiert, damit es keine anderen Laufwerke
anhand eines wiederverwendeten Gerätenamens abschaltet. Ein akzeptierter
Standby-Befehl belegt nicht, dass die USB-HDD tatsächlich stillsteht;
physisches Abschalten und Aufwachen müssen separat überprüft werden.

```bash
systemctl status beelink-backup-disk-idle.service --no-pager
journalctl -u beelink-backup-disk-idle.service --no-pager -n 50
```

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

`.env`-Dateien und private Anwendungskonfigurationen bleiben an ihren
Originalpfaden unverschlüsselt. Nur die verschlüsselten Kopien gehören in Git.
Prüfe insbesondere bei Dateien außerhalb dieses Repositorys, dass Klartext
nicht anderweitig versioniert wird.

Installiere GnuPG (`gpg`) und initialisiere das gemeinsame Skript:

```bash
git submodule update --init buildSrc
```

Das Skript benötigt ein aktuelles GnuPG 2.x mit `--pinentry-mode` und
`--no-symkey-cache`. Ältere MacGPG-Versionen wie 2.0.30 werden nicht
unterstützt. Auf macOS kann GnuPG über Homebrew installiert werden; falls
`gpg --version` danach noch die alte Version anzeigt, den `bin`-Ordner des
Homebrew-GnuPG-Pakets im `PATH` vor die alte Installation setzen:

```bash
brew install gnupg
export PATH="$(brew --prefix gnupg)/bin:$PATH"
gpg --version
```

Verschlüssele die Dateien nach einer Änderung erneut. Das Skript fragt die
Passphrase zweimal verdeckt ab und verwendet symmetrisches AES-256 mit
GnuPG-Integritätsschutz. Eine lange, einzigartige Passphrase muss unabhängig
vom Backup aufbewahrt werden; ohne sie ist keine Wiederherstellung möglich.

```bash
./private/private.sh --crypt
./private/private.sh --decrypt
```

Ohne weitere Argumente verarbeitet der Aufruf `apache/.env`, `nextcloud/.env`
und Matomos `config.ini.php`. Einzelne Ziele können gewählt werden:

```bash
./private/private.sh --crypt apache nextcloud
MATOMO_CONFIG_PATH=/pfad/zur/config.ini.php ./private/private.sh --decrypt matomo
./private/private.sh --crypt camera-postgres
```

Auf beelink verwendet Matomo die aktive Docker-Konfiguration unter
`/daten/beelink-runtime/matomo/www/piwik/config/config.ini.php`.
`./private/private.sh --crypt` bleibt ein Aufruf als normaler Benutzer:
für diese geschützte Datei fragt das Skript bei Bedarf nach sudo und liest
eine temporäre Kopie mit Modus 0600. Vor der Passphrase-Abfrage prüft ein
tatsächlicher Leseversuch den Zugriff; Fehler werden ausdrücklich gemeldet.
GPG, verschlüsselte Dateien und der
lokale Prüfstatus bleiben beim Benutzer; die temporäre Klartextkopie wird
auch bei Fehlern entfernt. Die Prüfung liest dieselbe aktive Datei mit
`sudo -n`; ohne gültige sudo-Berechtigung meldet sie einen Fehler statt einen
veralteten Stand zu bestätigen. Wenn sudo-Anmeldedaten fehlen, weist sie auf
`sudo -v` im selben Terminal hin; anschließend den Git-Befehl erneut ausführen.
Eine Entschlüsselung in das geschützte
Produktionsverzeichnis erfolgt nicht automatisch; dafür einen beschreibbaren
`MATOMO_CONFIG_PATH` angeben und die Wiederherstellung bewusst durchführen.

Der Standardpfad für Matomo ist auf anderen Linux-Hosts
`/srv/www/vhosts/mxdocs/piwik/config/config.ini.php` und auf macOS
`/Users/hannes/server/apache/www/mxdocs/piwik/config/config.ini.php`.
Nach erfolgreichem Verschlüsseln merken sich Verschlüsselung, Entschlüsselung
und Prüfung den dafür verwendeten Matomo-Pfad lokal pro Host und Checkout.
`MATOMO_CONFIG_PATH` hat Vorrang; beim nächsten erfolgreichen Verschlüsseln
wird der angegebene Pfad als neuer lokaler Standard gespeichert. Ausnahme:
beelink verwendet ohne explizites `MATOMO_CONFIG_PATH` immer die aktive
Docker-Konfiguration statt eines früher gespeicherten Pfads. In einem
frischen Checkout ohne lokalen Prüfstatus gilt wieder der Plattform-Standard.
Verschlüsselte Kopien liegen in `private-config/gpg/`; beim Entschlüsseln
erhalten sie den kurzen Hostnamen vor `.enc`, z. B.
`apache.env.mac2016.enc` und
`matomo-config.ini.beelink.enc`. Bestehende verschlüsselte Alt-Konfigurationen
bleiben erhalten und werden nicht als beelink-Konfiguration verwendet.
`--decrypt` verwendet nur Dateien für den aktuellen kurzen Hostnamen
(`hostname -s`). Unterschiedliche Rechner müssen
eindeutige Hostnamen haben. Nach einer Umbenennung des Rechners muss die
zugehörige Sicherungsdatei entsprechend umbenannt werden.

Für Kamera-PostgreSQL wird `camera-postgres/.env` als
`camera-postgres.env.<hostname>.enc` verschlüsselt. Dieses optionale Ziel muss
explizit angegeben werden; der Aufruf ohne Ziele bleibt unverändert. Die
Änderungsprüfung berücksichtigt es, sobald die `.env` oder eine verschlüsselte
Kopie auf dem jeweiligen Host existiert.
Für die vorbereitete MXServer-PostgreSQL-Konfiguration gilt entsprechend
`./private/private.sh --crypt mxserver-postgres`; der Standardaufruf bleibt
ebenfalls unverändert.

Bereits vorhandene Dateien ohne Hostname bleiben unverändert und werden nicht
automatisch verwendet. Sie können nach Prüfung ihrer Herkunft auf den passenden
Hostnamen umbenannt werden, zum Beispiel:

```bash
mv -i private-config/gpg/apache.env.enc private-config/gpg/apache.env.mac2016.enc
```

Beim Entschlüsseln müssen die jeweiligen Zielverzeichnisse existieren. Fehlende Eingaben führen
vor der Verarbeitung zu einem Fehler. Beim Entschlüsseln wird eine bestehende
Klartextdatei vor dem Ersetzen als `<Dateiname>.YYMMDD-hhmm` gesichert, zum
Beispiel `apache/.env.261115-1120`. Sind bestehende und entschlüsselte Datei
inhaltlich identisch, bleibt die bestehende Datei unverändert und es wird keine
Sicherung erstellt. Sicherung und wiederhergestellte Datei erhalten bei einer
Änderung Modus `0600`. Existiert bereits eine Sicherung für diese Minute,
bricht der Vorgang ohne Überschreiben ab. Die Sicherung enthält Klartext und
ist wie die Originaldatei zu schützen. Dieses Verhalten gilt auch beim direkten
Aufruf von `buildSrc/scripts/decrypt.sh`: Für beliebige Dateien und insbesondere
Matomo außerhalb dieses Repositorys müssen Klartext-Sicherungen gegebenenfalls
in den dortigen Git-Ignore-Regeln ausgeschlossen werden. Beim Verschlüsseln werden bestehende
verschlüsselte Dateien nur nach Bestätigung überschrieben.
Die Verarbeitung erfolgt nacheinander: Scheitert ein späteres Ziel, bleiben
bereits verarbeitete Dateien bestehen.

Ohne interaktives Terminal kann `CRYPT_PASS` gesetzt werden; beim Verschlüsseln
muss `CRYPT_PASS_CONFIRM` denselben Wert enthalten. Bestehende verschlüsselte
Ziele werden ohne Terminal nicht überschrieben. Umgebungsvariablen können lokal einsehbar
sein; Passphrasen niemals in Skripte oder Git schreiben.

### Erinnerung bei privaten Änderungen

Nach erfolgreichem Verschlüsseln merkt sich `private.sh` lokal einen mit einem
zufälligen Schlüssel geschützten Prüfwert für Klartext und Ciphertext. Der
Schlüssel und die Prüfwerte liegen im lokalen Git-Verzeichnis, nicht im
Repository. Ohne vorheriges Verschlüsseln auf diesem Checkout ist der Stand
unbekannt und wird als Warnung gemeldet; fehlende Klartextdateien ohne
zugehörige Host-Sicherung werden nicht überwacht.

Für Meldungen vor `git status`, `git commit` und `git pull` die Shell-Funktion
**auf jedem Rechner und für jeden Benutzer**, der Git verwendet, in die
jeweilige `~/.bashrc` oder `~/.zshrc` eintragen (Pfad anpassen):

```bash
source "$HOME/git/dockerApache/private/git-notice.sh" "$HOME/git/dockerApache"
```

Danach eine neue Shell öffnen oder die entsprechende Datei neu einlesen. Die
Meldung blockiert Git nicht und aktualisiert oder committet nichts automatisch.
Sie gilt für interaktive Aufrufe über diese Shell-Funktion einschließlich
`git -C ...`, nicht für IDEs, Skripte, `command git` oder andere Shells. Bei
Warnungen die genannten Ziele mit `./private/private.sh --crypt <ziel>`
verschlüsseln und die `.enc`-Dateien selbst committen und pushen.
Eine manuelle Prüfung ist möglich:

```bash
python3 private/check-private.py check
```

Zum Deaktivieren die `source .../private/git-notice.sh`-Zeile aus der
verwendeten Shell-Konfiguration entfernen (`~/.bashrc`, `~/.zshrc` oder die
Datei, in die sie eingetragen wurde). Eine neue Shell übernimmt die Änderung;
in der aktuellen Shell entfernt `unset -f git` die Funktion sofort. Der lokale
Prüfstatus bleibt dadurch erhalten.

Frühere OpenSSL-CBC-Dateien des buildSrc-Skripts sind nicht kompatibel und
müssen separat mit der alten Skriptversion entschlüsselt werden. Vor jedem
Commit prüfen, dass nur Ciphertext hinzugefügt wird.

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
verschlüsselte Dateien ablegen. Bereits versehentlich committete Klartexte
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

### Production host and scheduled operations

Production runs on **beelink**; **mac2016** is the standby mirror. The
beelink systemd jobs back up PostgreSQL and synchronize Apache/WordPress,
GitLab, ownCloud, MXServer, and Matomo to mac2016. They share lifecycle locks,
verify the encrypted data mount, and report job status and failures.

Certificate renewal and deployment are managed on beelink by
`monitoring/renew-beelink-certificates.py` and the
`beelink-certificates.timer`. The renewal job deploys active certificates to
the local frontend and DNS services, then transfers mirror certificates to
mac2016. The timer definition schedules renewal daily at 02:48; the encrypted
configuration backup is scheduled on Sundays at 02:55. These schedules require
mac2016 to be awake and reachable and take effect only after the installed
systemd timers have been updated. Check the timer and recent logs with:

```bash
systemctl status beelink-certificates.timer --no-pager
sudo journalctl -u beelink-certificates.service -n 100 --no-pager
```

For the mac2016 nightly standby window, `monitoring/mac2016-night-wake.py`
installs a user LaunchAgent that starts an indefinite Amphetamine session with
closed-display mode at 02:44. The separate `pmset` wake schedule and a logged-in
GUI session are required. The beelink job timer checks every five minutes from
04:50 through 23:55 and requests sleep only after today's Apache, GitLab,
ownCloud, MXServer, Matomo, certificate renewal and Home Assistant snapshot all
succeed; the Sunday check also requires the encrypted configuration backup.
The sleep request is held if any required work fails or is still running.
A detected wake/start failure is logged to `~/Library/Logs/mac2016-night-wake.log`
and triggers a failure email. After a reboot, the GUI user must log in again.
The beelink hourly monitor accepts mac2016 being offline only after a successful
post-jobs sleep request has been recorded.
Install the beelink side from its staged repository checkout with
`sudo python3 monitoring/install-mac2016-night-schedule.py`. It reloads systemd
unit definitions without restarting services, restarts only the three affected
timers, and enables the guarded sleep-check timer. Because the certificate timer
and Sunday backup timer are persistent, an already-missed occurrence may run
immediately after installation.

The job schedules and read-only health/status commands are defined in
`monitoring/beelink-jobs.py`. On beelink, inspect the last results with:

```bash
python3 /usr/local/bin/beelink-job-status --report
systemctl list-timers 'beelink-job-*'
```

mac2016 runs `monitoring/systems-health-check-mac.sh` hourly at minute 10.
It checks mac2016 locally and queries beelink over SSH; the hourly beelink
monitor checks production services and job freshness. Preserve the explicit
`HOME` and `PATH` in mac2016's cron entry.

### GitHub artifacts mirror

The `/github` file-management app runs on beelink and is mirrored on mac2016
by `github-artifacts/sync-beelink.py`. Its private upload files and parameter
file stay outside the repository. Disable the mirror sync before promoting
mac2016 as the writable primary.

### ownCloud and MXServer mirrors

Production ownCloud and MXServer run on beelink. Their systemd jobs synchronize
the independent mac2016 standby copies; mirror-only changes are overwritten by
the next successful sync. See [`owncloud/README.md`](owncloud/README.md) and
[`mxserver/README.md`](mxserver/README.md) for service checks and mirror details.

### GitLab, Docker-Runner und mac2016 gemeinsam aktualisieren

`monitoring/update-beelink-gitlab.py` aktualisiert die aktive Produktion auf
beelink, den Docker-Runner und den GitLab-Standby auf mac2016. Es verwendet
die produktiven Runtime-Dateien, nicht die frueheren Migrationsskripte:

| Dienst | Aktive Konfiguration |
|--------|----------------------|
| GitLab auf beelink | `/daten/beelink-runtime/apps/compose.json` |
| Docker-Runner | `/etc/beelink-docker-runner/compose.json` |
| GitLab auf mac2016 | `/Users/hannes/git/dockerApache/gitlab/docker-compose.yml` |

Direkt auf beelink im aktuellen Repository-Checkout ausfuehren. mac2016 muss
eingeschaltet und ueber die vorhandenen SSH-Schluessel erreichbar sein.
Beide GitLab-Instanzen muessen anfangs dieselbe CE-Version verwenden.
Der Standby muss mit der oben genannten Compose-Datei ohne Overrides laufen;
abweichende Dateien oder Daten-Mounts werden nicht automatisch umgeschrieben.
Auf beiden Hosts muessen mindestens 50 GiB frei sein; fuer grosse Installationen
zusaetzlich genug Platz fuer die GitLab-Archive und deren Sicherungskopien einplanen.
Eine unabhaengige, getestete Off-Host-Sicherung bleibt erforderlich.

Zuerst nur pruefen (die Versionsnummer ist ein **Beispiel**, kein automatisch
ermitteltes Update-Ziel):

```bash
TARGET_VERSION=19.5.4  # Gewuenschte, verfuegbare Patch-Version einsetzen
sudo python3 monitoring/update-beelink-gitlab.py --versions "$TARGET_VERSION"
```

Der Standardaufruf prueft Mount, laufende Versionen, Runner-Registrierung,
GitLab-Zustand und Migrationen lokal sowie auf dem Standby. Er laedt keine
Images und aendert keine Anwendungskonfiguration. Die Pruefung kann auf noch
laufende Hintergrundmigrationen warten und bricht bei Fehlern ab.

Vor der Ausfuehrung unter **GitLab Admin > CI/CD > Runners alle Runner pausieren**
und laufende Jobs auslaufen lassen. Das Skript kontrolliert diesen Zustand,
stoppt aber keine laufenden CI-Jobs gewaltsam. Dann denselben Aufruf freigeben:

```bash
sudo python3 monitoring/update-beelink-gitlab.py \
  --versions "$TARGET_VERSION" --apply
```

Falls GitLab und Runner unterschiedliche Patch-Releases haben, die
verfuegbare Runner-Version explizit mit `--runner-versions` angeben. Sie muss
zur Major/Minor-Version von GitLab passen und darf kein Downgrade sein.
Ohne diese Option wird fuer beide dieselbe Versionsnummer verwendet.

**Minor-/Major-Upgrades:** Zuerst den
[offiziellen Upgrade-Pfad](https://docs.gitlab.com/update/upgrade_paths/) und
die Release-Hinweise pruefen. Alle erforderlichen Zwischenstopps mit ihren
konkreten Patch-Versionen in aufsteigender Reihenfolge an `--versions` uebergeben
und `--upgrade-path-reviewed` hinzufuegen. Bei `--runner-versions` muss dann
genau eine Runner-Version pro Stopp angegeben werden. Das Skript ermittelt
keinen Upgrade-Pfad, waehlt kein `latest` und bestaetigt mit diesem Schalter
nicht selbst die Vollstaendigkeit des angegebenen Pfads.

Bei `--apply` sperrt das Skript die gemeinsamen beelink-Lifecycle-Jobs und den
mac2016-Backup-Empfaenger. Vor jedem GitLab-Schritt laedt es die benoetigten
Images auf beiden Hosts, erstellt und verifiziert GitLab-Backups und sichert
`/etc/gitlab` einschliesslich der Secrets sowie die bisherigen Compose-Dateien.
Danach wird ausschliesslich der GitLab-Service neu erstellt; andere
Anwendungen bleiben unveraendert. Readiness, Datenbank- und
Hintergrundmigrationen sowie GitLab-/Secrets-Pruefungen muessen erfolgreich
sein, bevor der naechste Schritt beginnt. Eine Migrationspruefung wartet
hoechstens eine Stunde, die Readiness-Pruefung etwa 20 Minuten.
Zum Schluss startet und verifiziert es den Runner und fuehrt den normalen
GitLab-Mirror-Backup/Restore-Lauf aus. **Die Runner bleiben in GitLab pausiert**:
anschliessend wieder freigeben und einen echten CI-Testjob ausfuehren.

Backups werden nicht automatisch geloescht:

```text
beelink: /daten/beelink-backups/gitlab-updates/<UTC-Zeit>/<Zielversion>/
mac2016: /Users/hannes/server/gitlab/backups/updates/<UTC-Zeit>/<Zielversion>/
```

`gitlab-backup.tar`, `backup-id` und `gitlab-configuration.tar` gehoeren
zusammen und enthalten sensible Daten. Das Fortschritts-/Fehlerprotokoll liegt
auf beelink unter `<UTC-Zeit>/update.log`; nach vollstaendigem Erfolg liegt
dort auch `completed.json`. Der abschliessende Mirror-Lauf verwendet den
vorhandenen Backup-Empfaenger und dessen normale Ausgabe.

**Bei Fehlern kein automatischer Rollback und kein blindes Wiederholen.**
`/daten/beelink-backups/gitlab-updates/update-in-progress.json` blockiert
weitere Updates. Auf mac2016 kann ausserdem
`/Users/hannes/.local/state/gitlab-mac-sync/lock/updater` verbleiben und den
normalen Mirror-Lauf blockieren. Erst Log, tatsaechlich laufende Versionen,
Migrationen und Backups pruefen; bei Bedarf die gesicherte Version **mit den
dazugehoerigen Daten und Secrets** wiederherstellen. Ein altes Image allein
ist nach einer Datenbankmigration kein Rollback. Sperren erst nach
abgeschlossener manueller Wiederherstellung entfernen bzw. archivieren;
es gibt keinen automatischen Resume-Modus. Bei einem Fehler nur im letzten
Mirror-Lauf kann stattdessen dieser nach Beheben der Ursache separat ueber
`sudo systemctl start beelink-job-gitlab.service` wiederholt werden. Den
Pending-Marker danach erst bei nachgewiesenem Gesamterfolg archivieren.

Die Runtime-Dateien bleiben fuer weitere Updates massgeblich. Historische
Restore-/Migrationsskripte und der Runner-Erstinstaller sind bewusst auf
ihren Ausgangsstand gepinnt und werden nicht als Updater erneut ausgefuehrt.
Die Compose-Aenderung im Checkout auf mac2016 bleibt als lokale Aenderung
sichtbar und sollte nach erfolgreichem Update ins Repository uebernommen werden.

### beelink: encrypted Samsung T7 at /daten

The T7 contains beelink production data and is verified by its filesystem UUID
before managed jobs or services access it.

`monitoring/setup-beelink-t7.sh` prepares the specifically identified 2 TB
Samsung T7 (serial `H758101R0JN7T5S`) as a whole-disk LUKS2/ext4 volume.
**It destroys the previous contents.** Run directly on beelink:

```bash
cd ~/git/dockerApache
sudo bash monitoring/setup-beelink-t7.sh --erase-confirmed-t7
```

The script refuses mounted devices, unexpected device identities, existing
configuration or a nonempty `/daten`. The unlock key is root-only at
`/etc/cryptsetup-keys.d/beelink-t7.key` on the encrypted system filesystem.
This protects a detached T7, not against access from the running host.
The existing system-drive unlock configuration is unchanged.

UUID-based crypttab configuration uses `headless` (no password prompt) and
short timeouts. The optional systemd automount makes a missing T7 nonfatal
at boot; access to `/daten` can still fail if the drive is absent or broken.
Do not unplug a mounted drive. Jobs writing there must verify
`mountpoint -q /daten` first, rather than writing to the system disk on failure.
`hannes` owns the mounted filesystem.

Setup verifies automatic key-based reopening and a write/read operation.
Verify a reboot with the drive connected and a boot with it disconnected
separately. Logs, original fstab/crypttab and a LUKS header backup remain under
`/root/beelink-t7-setup.*`. Back up the key and header securely off-host:
there is no recovery passphrase, and loss of the key makes the data inaccessible.
If setup fails, inspect the log and retained state instead of blindly rerunning.

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
| `mysql_wordpress.sql.gz` | `wordpress` database (gzipped SQL) |
| `mysql_wordpress_mxtracks.sql.gz` | `wordpress_mxtracks` database (gzipped SQL) |
| `www.tar.gz` | Web root files |
| `vhosts.tar.gz` | Apache vhost configs |
| `databases.txt`, `backup-complete` | Database list and completion marker |

`MYSQL_BACKUP_DATABASES` in `.env` overrides the space-separated database
list. Each database is dumped separately; a missing database or failed dump
fails the backup. `backup-complete` is written only after all dumps and file
archives succeed. A successful dump is not a tested restoration or an atomic
snapshot across the database and web files.

The restore validates all backup files before changing anything, then asks
separately before replacing each database and the web/vhost files. Existing web
and vhost directories are retained with a `.pre-restore-<timestamp>` suffix.
Restoring a database is destructive and a failed import may leave it incomplete;
take an independent backup first. MySQL users, passwords and grants are **not**
part of these dumps: retain or recreate them separately when restoring onto a
fresh server. Legacy backups without `backup-complete` must be restored manually.

`MYSQL_DATABASE` and `MYSQL_USER` are optional initial-setup settings. Leave
them empty to avoid creating `webapp` on a fresh MySQL data directory; changing
these settings does not change an already initialized MySQL database or user.

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
the domain to beelink's **local IP** instead of the public one.

```bash
# Add to nextcloud/.env:
NEXTCLOUD_LOCAL_IP=192.168.178.142          # beelink LAN IPv4
NEXTCLOUD_LOCAL_IPV6=                     # set beelink stable ULA IPv6 if available
DNS_UPSTREAM=8.8.8.8                        # upstream DNS (not Fritz!Box – avoids loop)

# Start dnsmasq
docker compose --profile split-dns up -d
```

Keep the **DNS-Rebind-Schutz** exception for `nextcloud.mxtracks.info` in place
(Heimnetz → Netzwerk → DNS-Rebind-Schutz).

#### ⚠️ Fritz!Box-wide DNS setting does NOT work for MyFRITZ domains

Setting *Bevorzugter DNSv4-Server* to `192.168.178.142` in
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
> → IP settings: **Static** → DNS 1: `192.168.178.142`

Android 12 – 16 (new UI, long-press removed):
> Settings → Network & internet → Internet → tap the **⚙️ gear icon** next to
> your Wi-Fi network → tap the **✏️ pencil icon** (top-right) → IP settings:
> **Static** → DNS 1: `192.168.178.142`, DNS 2: `8.8.8.8`

Also set **Private DNS → Off** (Settings → Network & internet → Private DNS).

---

#### Fix B — Android Private DNS with DoT (works on ALL Android versions)

Android's *Private DNS* feature uses **DNS-over-TLS (DoT)** on port 853.
Point it at `nextcloud.mxtracks.info` — Android resolves that hostname via
Fritz!Box (gets the public IP), connects to port 853, Fritz!Box forwards it to
beelink's CoreDNS service (which holds the valid Let's Encrypt cert), and from
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
   - Internal host: **192.168.178.142** (beelink)
   - Internal port: **853**

4. On Android:
   > Settings → Network & internet → **Private DNS**
   > → *Private DNS provider hostname* → `nextcloud.mxtracks.info`

No static IP needed — works automatically on home WiFi **and** over mobile data.

---

#### IPv6 / Happy Eyeballs

Android prefers IPv6. Without an AAAA override in dnsmasq, Android receives
the public IPv6 address and bypasses local routing. Set `NEXTCLOUD_LOCAL_IPV6`
in `.env` to beelink's stable ULA IPv6 address:

```bash
# Find beelink's stable ULA address (scope global, not temporary):
ip -6 addr show scope global | grep -v temporary
```
