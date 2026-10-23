# GitHub Copilot Instructions

## Project overview

This repository contains two self-contained Docker Compose stacks and a set of
helper scripts for running and maintaining them:

| Folder | Stack | Database |
|--------|-------|----------|
| `nextcloud/` | Nextcloud (production-apache) | PostgreSQL 16 |
| `apache/` | Apache 2.4 + PHP 8.3 | MySQL 8.4 |

Both stacks are built for **linux/amd64** and **linux/arm64** (Apple Silicon,
Raspberry Pi, x86 servers). All persistent data lives **outside** the containers
via bind-mounts configured through a `.env` file.

---

## Repository layout

```
dockerApache/
├── Dockerfile                  # Legacy Ubuntu/Apache/MySQL image (not used by stacks)
├── README.md                   # Combined user-facing documentation
├── apache/
│   ├── Dockerfile              # Custom Apache+PHP image (built locally by compose)
│   ├── docker-compose.yml
│   ├── .env.example
│   ├── backup.sh               # Backup MySQL + web root + vhosts
│   ├── restore.sh              # Restore from backup
│   ├── migrate-native-to-docker.sh # One-shot migration from native Apache+MySQL
│   ├── sync-to-mirror.sh       # Live stream source → mirror (no temp files)
│   ├── VHOST_SAMPLE.md         # Virtual host setup guide
│   └── sample-data/            # Sample vhost configs and index.php
│       ├── vhosts/
│       └── www/
└── nextcloud/
    ├── Dockerfile              # Custom Nextcloud image (published to Docker Hub)
    ├── docker-compose.yml
    ├── .env.example
    ├── backup.sh               # Backup PostgreSQL + config + user data
    ├── restore.sh              # Restore from backup
    ├── sync-to-mirror.sh       # Live stream source → mirror (no temp files)
    ├── migrate-from-owncloud.sh# Migrate users/files/contacts from ownCloud
    └── nextcloud.service       # systemd unit for Linux boot
```

---

## Key conventions

### Environment variables
- All runtime config comes from `.env` (copied from `.env.example`).
- Data directories use `${NEXTCLOUD_DATA_DIR:-./data}` / `${APACHE_DATA_DIR:-./data}`.
- Never hard-code paths or passwords — always use env vars with fallback defaults.

### Docker Compose
- `version: "3.9"` syntax.
- All services use `restart: unless-stopped`.
- Health checks on database services; app container depends on `service_healthy`.
- No named volumes — only bind-mounts so data survives `docker compose down -v`.
- Platform is auto-detected; no `platform:` key needed in compose files.

### Dockerfiles
- Base images: `nextcloud:production-apache`, `php:8.3-apache` (or similar Debian-based).
- Multi-arch builds: `linux/amd64,linux/arm64` via `docker buildx`.
- Nextcloud image is published to `hannesa2/nextcloud` on Docker Hub via CI.
- Apache image is built locally by `docker compose up -d --build` (not pushed).

### Shell scripts
- All scripts: `#!/usr/bin/env bash`, `set -euo pipefail`.
- Locale: `export LC_ALL=C; export LANG=C` to silence tar/locale warnings.
- Logging: timestamps via `log()` function that writes to both stdout and a log file.
- Use `sudo` for all operations on directories owned by `www-data`.
- Use `sudo mkdir -p`, `sudo rsync`, `sudo chown -R www-data:root` for data dirs.
- Use `tee -a "$LOG"` to stream output to both screen and log file.
- `convmv -f iso-8859-1 -t utf-8 -r --notest` after rsync to fix Latin-1 filenames.
- Scripts that use `sudo` **must be run directly on the server** (interactive login
  or cron), not via a non-interactive `ssh host './script.sh'` pipe — sudo requires
  a terminal or cached credentials.

### Backup / restore pattern
- Backups: timestamped sub-folders under `./backups/YYYYMMDD_HHMMSS/`.
- Database: `pg_dump | gzip` / `gunzip | psql` (Nextcloud) or `mysqldump --no-tablespaces --single-transaction` (Apache).
- Files: `tar -czf` for config; `rsync --delete` for large data trees.
- Mirror sync (`sync-to-mirror.sh`): streams directly source → mirror via SSH pipes, no temp files.

### CI/CD (GitHub Actions)
- Nextcloud workflow: `.github/workflows/nextcloud.yml`
  - Triggers on push to `master` and tags matching `nc-*`.
  - Uses `docker/setup-docker-action@v4` with containerd snapshotter.
  - Uses `docker/build-push-action@v6` with `cache-from/to: type=gha`.
  - Pushes to Docker Hub only on non-PR events (`push: ${{ github.event_name != 'pull_request' }}`).
- Apache workflow: `.github/workflows/docker-image.yml`
  - Same pattern; tags match `*`.

### occ commands (Nextcloud)
Always run as `www-data`:
```bash
docker compose exec --user www-data nextcloud php occ <command>
```
Common commands used in this project:
- `files:scan <user>` / `files:scan --all`
- `user:add --password-from-env <username>`
- `user:resetpassword <username>`
- `config:system:set trusted_domains <n> --value="<domain>"`
- `config:system:set overwritehost --value="<domain>"`
- `config:system:set overwriteprotocol --value="https"`
- `maintenance:mode --on` / `--off`

---

## Trusted domains behaviour
`NEXTCLOUD_TRUSTED_DOMAINS` in `.env` is space-separated and sets the initial
trusted domains on first install. After first run, use `occ config:system:set`
to add/change domains — `docker compose restart` alone does **not** re-apply
`.env` changes; use `docker compose down && docker compose up -d`.

---

## Port assignments (defaults)
| Service | Port |
|---------|------|
| Apache stack HTTP | 8081 |
| Apache stack HTTPS | 8443 |
| Nextcloud | 8082 |
| PostgreSQL (internal) | 5432 |
| MySQL (internal) | 3306 |
| Redis (internal) | 6379 |

---

## Reverse proxy (nginx/Apache on host)
When Nextcloud is behind a reverse proxy, set:
```bash
occ config:system:set overwritehost     --value="nextcloud.example.com"
occ config:system:set overwriteprotocol --value="https"
occ config:system:set trusted_proxies 0 --value="<proxy-LAN-IP>"
```
On **Linux** use the host's LAN IP for `ProxyPass` (not `host.docker.internal`).

---

## Fritz!Box NAT hairpinning / split DNS

Fritz!Box does **not** support NAT loopback. When a LAN device accesses
`nextcloud.mxtracks.info`, its DNS resolves to the public IP → the request
hits Fritz!Box's own HTTPS interface → Fritz!Box returns its error page.

### Fix: split DNS with the optional dnsmasq service

```
┌─ home WiFi ─────────────────────────────────────────────┐
│  mobile → DNS query direct to dnsmasq (192.168.178.129) │
│         ← 192.168.178.129  ✓  (A + AAAA overridden)    │
│  mobile → HTTPS 192.168.178.129:443 (reverse proxy)     │
│         ← Nextcloud  ✓                                  │
└─────────────────────────────────────────────────────────┘
```

1. In `.env`:
   ```dotenv
   NEXTCLOUD_LOCAL_IP=192.168.178.129          # latitude LAN IPv4
   NEXTCLOUD_LOCAL_IPV6=fdf4:be15:98e0:...     # latitude stable ULA IPv6
   DNS_UPSTREAM=8.8.8.8                        # upstream for all other queries
   ```
2. Start dnsmasq: `docker compose --profile split-dns up -d`
3. The Fritz!Box **DNS-Rebind-Schutz** exception for `nextcloud.mxtracks.info`
   must remain in place (Heimnetz → Netzwerk → DNS-Rebind-Schutz).
4. On Android set **Private DNS → Off** (not "Automatic").
   Fritz!Box supports DoT on port 853; "Automatic" makes Android use Fritz!Box's
   DoT resolver which handles queries *internally*, bypassing dnsmasq entirely.

### ⚠️ Fritz!Box-wide DNS-Server setting does NOT work for this setup

`nextcloud.mxtracks.info` is a CNAME to `*.myfritz.net` (Fritz!Box's own
MyFRITZ DDNS domain). Fritz!Box resolves `*.myfritz.net` **internally** —
it never forwards those queries to the configured upstream DNS. Even with
*Bevorzugter DNSv4-Server* = `192.168.178.129`, Fritz!Box "sees through" the
CNAME and returns its public IP directly.

**The only reliable fix is per-device DNS on Android:**

> WiFi settings → long-press network → Modify → Advanced →
> IP settings: **Static** → **DNS 1**: `192.168.178.129`, **DNS 2**: `8.8.8.8`

This bypasses Fritz!Box's resolver entirely and talks straight to dnsmasq.

dnsmasq overrides both A (IPv4) and AAAA (IPv6) records for `OVERWRITEHOST`
and forwards all other queries to `DNS_UPSTREAM`, so normal internet DNS is unaffected.

---

## Mirror sync (`sync-to-mirror.sh`)

### Topology
| Role | Host | OS | Data path |
|------|------|----|-----------|
| **Source** (production) | `latitude` | Ubuntu Linux | `NEXTCLOUD_DATA_DIR=/media/hannes/T5` |
| **Mirror** (standby) | `mac2016` | macOS | `MIRROR_DATA_DIR=/Volumes/SamsungT1` |

The script is **always run on the source server** (`latitude`).  
It SSHes into the mirror (`mac2016`) to run Docker Compose commands there.

### What it does (in order)
1. Enable maintenance mode on source
2. Enable maintenance mode on mirror (suppress errors if not yet set up)
3. Stream PostgreSQL dump source → mirror via SSH pipe (`pg_dump | ssh | psql`)
4. Stream `config/` + `custom_apps/` via tar pipe; patch `config.php` on mirror
   (strips `overwritehost`/`overwriteprotocol`, updates DB credentials)
5. Rsync `data/` incrementally source → mirror (`sudo rsync` — data owned by `www-data`)
6. `docker compose down && docker compose up -d` on mirror so the entrypoint
   re-populates `html/`; wait up to 20 min for `versioncheck.php` to appear
7. `occ upgrade` + `occ maintenance:repair` on mirror
8. Set `trusted_domains` on mirror to `localhost` + `$MIRROR_HOST`
9. Disable maintenance mode on mirror, then on source

### Key `.env` variables for sync
```dotenv
MIRROR_HOST=mac2016               # SSH hostname of the mirror
MIRROR_USER=hannes                # SSH user on the mirror
MIRROR_SSH_PORT=22
MIRROR_NEXTCLOUD_DIR=/Users/hannes/git/dockerApache/nextcloud  # compose dir on mirror
MIRROR_DATA_DIR=/Volumes/SamsungT1  # data root on mirror (if different from compose dir)
MIRROR_EXTRA_PATH=/usr/local/bin    # prepended to PATH on mirror (Docker Desktop on macOS)
MIRROR_SHELL=zsh                    # login shell on mirror (zsh for macOS, bash for Linux)
```

### Running the sync
```bash
# On the source server (must be an interactive/sudo-capable session):
cd ~/git/dockerApache/nextcloud
./sync-to-mirror.sh

# Or via cron (credentials must be cached or passwordless sudo configured):
0 3 * * * /home/hannes/git/dockerApache/nextcloud/sync-to-mirror.sh >> /var/log/nc-mirror-$(date +\%Y\%m\%d).log 2>&1
```

> **Important:** `sync-to-mirror.sh` uses `sudo rsync` to read the `www-data`-owned
> data directory. It must be run in a session where `sudo` can authenticate
> (interactive login with cached credentials). Running it via a non-interactive
> `ssh latitude './sync-to-mirror.sh'` will fail with
> *"sudo: a terminal is required to read the password"*.

---

## ownCloud migration
`nextcloud/migrate-from-owncloud.sh` handles:
1. User discovery via `occ user:list` (or filesystem scan fallback).
2. File rsync with `--delete` (exact mirror of ownCloud source).
3. `convmv` pass to fix ISO-8859-1 → UTF-8 filenames.
4. Contact export via ownCloud CardDAV HTTP endpoint.
5. Contact import into Nextcloud via CardDAV PUT.
6. `files:scan` per user.

Configure via `.env`:
```dotenv
OWNCLOUD_DATA_DIR=/path/to/owncloud/data
OWNCLOUD_OCC=/path/to/owncloud/occ
OWNCLOUD_URL=http://localhost/owncloud
OWNCLOUD_ADMIN_USER=admin
OWNCLOUD_ADMIN_PASSWORD=secret
```

---

## Apache stack – native-to-Docker migration

`apache/migrate-native-to-docker.sh` handles a one-shot migration of existing
native Apache + MySQL websites into the Docker Apache stack.

### Sites migrated (latitude server)

| Native docroot | Docker www subdir | New (copy) domain |
|----------------|-------------------|-------------------|
| `/srv/www/vhosts/dev.mxtracks` | `dev.mxtracks` | `devcopy.mxtracks.info` |
| `/srv/www/vhosts/mxdocs` | `mxdocs` | `wwwcopy.mxtracks.info` |

Native and Docker stacks run **side-by-side** during the transition period:
- Native Apache remains on ports 80/443, serving the original domains.
- Docker container runs on ports 8081/8444; native Apache proxies the *copy* domains to it.

### What the script does
1. `docker compose up -d --build` – starts the stack (safe to re-run).
2. `sudo rsync` each web root into `$APACHE_DATA_DIR/www/<subdir>/`.
3. `convmv` pass to fix any ISO-8859-1 filenames.
4. `mysqldump` each database via the native MySQL WordPress user.
5. Creates DB + user in Docker MySQL (`CREATE DATABASE IF NOT EXISTS …`).
6. Imports the dump via the Docker MySQL root user.
7. Patches `wp-config.php`: `DB_HOST localhost` → `db` (Docker service name).
8. Writes a Docker vhost config into `$APACHE_DATA_DIR/vhosts/<domain>.conf`.
9. Writes a native-Apache reverse-proxy snippet (`native-proxy-<domain>.conf`).
10. `apache2ctl graceful` inside the container to pick up new vhosts.

### Running the migration
```bash
# On latitude (interactive session – sudo required):
cd ~/git/dockerApache/apache
cp .env.example .env   # set MYSQL_ROOT_PASSWORD to a strong password
./migrate-native-to-docker.sh
```

### Post-migration steps (printed by the script)
```bash
sudo a2enmod proxy proxy_http
sudo cp native-proxy-devcopy.mxtracks.info.conf /etc/apache2/sites-available/
sudo a2ensite devcopy.mxtracks.info.conf
sudo certbot --apache -d devcopy.mxtracks.info
sudo apache2ctl configtest && sudo systemctl reload apache2
```

### Multi-database approach
`MYSQL_DATABASE` in `.env` creates one initial DB. The migration script creates
additional databases (`wordpress`, `wordpress_mxtracks`) directly via the root
user after the container starts — no extra `docker-compose.yml` changes needed.

### WordPress URL fix
After DNS points to the new domain, run per site:
```bash
docker compose exec apache wp --path=/var/www/html/<subdir>/wordpress \
  search-replace 'https://<old-domain>' 'https://<new-domain>' \
  --all-tables --skip-columns=guid
```

---

## Apache stack – mirror sync

`apache/sync-to-mirror.sh` streams the running Docker Apache+WordPress stack from
the source server (latitude) to a standby mirror (mac2016).

### Topology
| Role | Host | OS | Data path |
|------|------|----|-----------|
| **Source** (production) | `latitude` | Ubuntu Linux | `APACHE_DATA_DIR=/srv/www.docker` |
| **Mirror** (standby) | `mac2016` | macOS | `MIRROR_DATA_DIR=~/git/dockerApache/apache/data` |

### Site mapping
| Source domain | Mirror domain | DB | www subdir |
|---------------|--------------|-----|------------|
| `devcopy.mxtracks.info` | `dev16.mxtracks.info` | `wordpress` | `dev.mxtracks` |
| `wwwcopy.mxtracks.info` | `www16.mxtracks.info` | `wordpress_mxtracks` | `mxdocs` |

### What it does (per site)
1. Prepares / starts mirror Docker stack (starts Docker Desktop on macOS if needed)
2. Drops + recreates DB on mirror, streams `mysqldump` source → mirror via SSH pipe
3. `rsync --delete` web root source → mirror (incremental, no temp files)
4. Patches `wp-config.php` on mirror (`WP_HOME`, `WP_SITEURL`, `DB_HOST`, HTTPS proxy detection)
5. Sets `siteurl` / `home` WordPress options directly in mirror DB
6. Writes mirror vhost config into `MIRROR_DATA_DIR/vhosts/<domain>.conf`
7. `apache2ctl graceful` inside mirror container

### Running
```bash
# On latitude (interactive session):
cd ~/git/dockerApache/apache
./sync-to-mirror.sh

# Nightly cron:
0 3 * * * /home/hannes/git/dockerApache/apache/sync-to-mirror.sh >> /var/log/apache-mirror-$(date +\%Y\%m\%d).log 2>&1
```

### Key `.env` variables for sync
```dotenv
MIRROR_HOST=mac2016
MIRROR_USER=hannes
MIRROR_SSH_PORT=22
MIRROR_APACHE_DIR=~/git/dockerApache/apache
MIRROR_DATA_DIR=~/git/dockerApache/apache/data
MIRROR_MYSQL_ROOT_PASSWORD=changeme_root
MIRROR_HTTP_PORT=8080
MIRROR_EXTRA_PATH=/usr/local/bin    # Docker Desktop on macOS
MIRROR_SHELL=zsh
```

