https://www.docker.com/blog/multi-arch-images/

```
docker buildx ls
docker buildx create --name apacheGenalogie
docker buildx use apacheGenalogie
docker buildx inspect --bootstrap
cat <<EOF > Dockerfile\nFROM ubuntu\nRUN apt-get update && apt-get install -y curl\nWORKDIR /src\nCOPY . .\nEOF
docker buildx build --platform linux/amd64,linux/arm64,linux/arm/v7 -t hannesa2/demo:latest --push .
```

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

#### macOS (Docker Desktop)
Enable **Start Docker Desktop at login**:
> Docker Desktop → Settings → General → ✅ Start Docker Desktop at login

That's it. The containers start automatically with Docker Desktop.

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
