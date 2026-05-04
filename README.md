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

