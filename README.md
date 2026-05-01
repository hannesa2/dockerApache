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

# Open http://localhost:8080 (or the port you configured)
```

### Stop / restart the stack

```bash
cd nextcloud

# Stop all containers (data is preserved)
docker compose down

# Stop and remove volumes (WARNING: deletes all container-internal data)
# → safe here because all data lives outside via bind-mounts
docker compose down -v

# Restart
docker compose restart

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

