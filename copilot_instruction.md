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
- Auf `latitude` ist der Snap-Certbot-Timer der aktive Erneuerungsdienst; den deaktivierten `certbot.timer` nicht ohne vorherige Prüfung wieder aktivieren.
- Zertifikate für `dev16.mxtracks.info` und `www16.mxtracks.info` werden durch Certbots Deploy-Hook nach erfolgreicher Erneuerung automatisch per eingeschränktem SSH-Schlüssel nach `mac2016` übertragen. Der Hook ist unter `/etc/letsencrypt/renewal-hooks/deploy/mac2016-cert-deploy` installiert.
- Der SSH-Schlüssel darf ausschließlich `~/bin/mac2016-cert-receiver.sh` ausführen. Keine interaktive Anmeldung und keinen allgemeinen SSH-Zugang für den Zertifikatstransfer einrichten.
- Der Empfänger installiert Zertifikat und privaten Schlüssel in `/Volumes/SamsungT5_1T/apache/certs`, prüft die Apache-Konfiguration im OrbStack-Container und lädt Apache graceful neu. Schlüsselberechtigungen müssen `0600` bleiben.
- Certbot-Deploy-Hooks laufen nur nach einer tatsächlich erfolgreichen Zertifikatserneuerung, nicht bei jedem Timerlauf. Zum gezielten erneuten Verteilen eines bereits ausgestellten Zertifikats den installierten Hook mit `RENEWED_LINEAGE=/etc/letsencrypt/live/<domain>` ausführen; keine unnötige erzwungene Neuausstellung (`--force-renewal`) durchführen.
- Nach Änderungen an der Certbot-Konfiguration beide Zertifikate mit `sudo snap run certbot.renew --dry-run --cert-name <domain>` testen. Ein Dry-Run löst nicht in jeder Certbot-Version Deploy-Hooks aus; den SSH-Transfer separat über den installierten Deploy-Hook validieren.
- Änderungen am SSH-Empfänger oder an den Hook-Skripten auf beiden Hosts prüfen. Keine Zertifikate oder privaten Schlüssel zu Testzwecken in Repository-Dateien oder temporäre Git-Dateien kopieren.

## Geeignetes Verhalten für Copilot

- Wenn du nicht sicher bist, prüfe zuerst die vorhandenen `docker-compose.yml`- und `.env.example`-Dateien.
- Priorisiere kleine, gezielte Konfigurationsänderungen.
- Wenn du ein neues Feature ergänzt, dokumentiere die Nutzung direkt in der passenden README oder Stack-Doku.
- Halte die Projektstruktur sauber: Stack-spezifische Dateien bleiben in ihrem jeweiligen Ordner.
- Bei Zertifikatsarbeiten zuerst den Erneuerungsstatus auf `latitude`, den eingeschränkten Deploy-Hook und die Zertifikatspfade auf `mac2016` prüfen; bestehende Zertifikate nicht ungefragt neu ausstellen.

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
