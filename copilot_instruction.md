# Copilot Instructions für dieses Projekt

## Projektüberblick
Dieses Repository enthält zwei unabhängige Docker-Stacks für selbst gehostete Web-Umgebungen:

- `apache/`: Apache + PHP + MySQL 8.4
- `nextcloud/`: Nextcloud + PostgreSQL + Redis

Das Repository selbst enthält keine Anwendungscodebasis im klassischen Sinne, sondern Deployment-Konfigurationen, Dockerfiles, Backup-Skripte und Beispiel-Setups.

## Wichtige Regeln

- Änderungen an `docker-compose.yml` müssen konsistent mit den bind mounts und Umgebungsvariablen bleiben.
- Host-seitige Daten werden außerhalb der Container gespeichert, daher niemals Container-Volumes als primäre Persistenzstrategie verwenden, wenn das Projekt bereits `data/` oder `NEXTCLOUD_DATA_DIR`/`APACHE_DATA_DIR` nutzt.
- Bevor du Änderungen an Umgebungsvariablen oder Ports vornimmst, prüfe die vorhandenen `.env.example`- und `.env`-Dateien.
- Für Docker-Änderungen: bevorzugt `docker compose`-Konventionen und prüfe bestehende Services (`db`, `apache`, `nextcloud`, `redis`).
- Wenn du neue Beispiel-Dateien oder Konfigurationen hinzufügst, halte sie kompatibel mit den vorhandenen Ordnerstrukturen unter `data/` und `sample-data/`.

## Strukturen

### Apache Stack
Pfad: `apache/`

- `docker-compose.yml`: MySQL + Apache service
- `Dockerfile`: PHP 8.3 Apache Image mit MySQL-/PHP-Erweiterungen
- `backup.sh` / `restore.sh`: Sicherungs- und Wiederherstellungslogik
- `data/`: Laufzeitdaten auf dem Host
  - `www/`
  - `vhosts/`
  - `logs/apache/`
  - `mysql/`
  - `mysql-config/`
- `sample-data/`: Beispiel-HTML/PHP und vhost-Konfigurationen
- `VHOST_SAMPLE.md`: Dokumentation für virtuelle Hosts

### Nextcloud Stack
Pfad: `nextcloud/`

- `docker-compose.yml`: Nextcloud + PostgreSQL + Redis
- `Dockerfile`: Nextcloud-Apache Image mit zusätzlichen PHP-Paketen
- `backup.sh` / `restore.sh`: Backup/Restore
- `nextcloud.service`: Beispiel für systemd-Start
- `.env` / `.env.example`: Konfiguration für Datenpfad, Port, Zugangsdaten

## Typische Änderungen

- Compose-Konfigurationen: nur im jeweiligen Stack-Verzeichnis ändern
- Backup/Restore-Skripte: robust, host-seitig, ohne zusätzliche lokale Abhängigkeiten
- Dockerfile: nur notwendige Pakete/Erweiterungen ergänzen
- Dokumentation: README und Stack-spezifische Doku aktualisieren, wenn Verhalten sich ändert

## Sicherheits- und Betriebsrichtlinien

- Keine echten Passwörter oder Secrets in Beispieldateien einbetten
- Standardwerte nur als `changeme`-artige Platzhalter akzeptieren
- Keine hardcodierten Produktionspfade oder Container-internen Datenorte in die allgemeine Doku schreiben
- Bei Risiko von Portkonflikten immer die `.env`-Konfiguration prüfen
- Private Schlüssel, `.env`-Dateien und Certbot-Accountdaten niemals ausgeben, committen oder in Logs kopieren.
- Production runs on beelink; mac2016 is the standby mirror. Use the managed systemd jobs in `monitoring/beelink-jobs.py` for backups, mirror syncs and availability checks.
- Certificate renewal and deployment are managed by `monitoring/renew-beelink-certificates.py` and `beelink-certificates.timer`. Keep private keys and certificate material out of the repository and logs.
- GitLab is backed up from beelink to mac2016 using a dedicated restricted SSH key and validating receiver. Never combine it with the certificate-deploy key or weaken either forced command.
- The mirror is an independent copy, not live replication. Verify the latest successful job and keep production/mirror GitLab versions compatible before restore or failover.
- Treat `gitlab-secrets.json`, host `.env` files and all encrypted configuration backups as private key material.

## Geeignetes Verhalten für Copilot

- Wenn du nicht sicher bist, prüfe zuerst die vorhandenen `docker-compose.yml`- und `.env.example`-Dateien.
- Priorisiere kleine, gezielte Konfigurationsänderungen.
- Wenn du ein neues Feature ergänzt, dokumentiere die Nutzung direkt in der passenden README oder Stack-Doku.
- Halte die Projektstruktur sauber: Stack-spezifische Dateien bleiben in ihrem jeweiligen Ordner.
- Bei Zertifikatsarbeiten zuerst den Erneuerungsstatus auf beelink und mac2016 sowie die eingeschränkten Deploy-/Empfängerpfade prüfen; bestehende Zertifikate nicht ungefragt neu ausstellen.


## Verfügbare Einstiegspunkte

- `README.md`: Gesamtübersicht der beiden Stacks
- `apache/docker-compose.yml`
- `nextcloud/docker-compose.yml`
- `apache/Dockerfile`
- `nextcloud/Dockerfile`

## Bevorzugte Form

Bei Code- oder Konfigurationsänderungen:

1. Ziel-Datei identifizieren
2. Kontext aus der entsprechenden Doku oder Compose-Datei lesen
3. Kleine, saubere Änderung durchführen
4. Auswirkungen auf Docker-Volumes, Ports und Umgebungsvariablen prüfen
5. Wenn nötig, Doku ergänzen
