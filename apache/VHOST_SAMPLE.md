# Virtual Host (vhost) Setup Guide

All vhost config files (`*.conf`) are placed in `data/vhosts/` and loaded automatically by Apache on start.

---

## Directory structure

```
data/
  vhosts/
    000-default.conf          ← catch-all / default site (localhost)
    myapp.local.conf          ← one file per named vhost
  www/
    index.php                 ← default site web root
    sites/
      myapp.local/            ← named site web root
        index.php
        phpinfo.php
```

---

## Adding a new site

### 1. Create the site files

```bash
mkdir -p data/www/sites/mynewsite.local
# drop your PHP/HTML files in there
```

### 2. Create a vhost config

```bash
cp sample-data/vhosts/myapp.local.conf data/vhosts/mynewsite.local.conf
```

Edit the copy — change at minimum:
- `ServerName` → `mynewsite.local`
- `DocumentRoot` → `/var/www/html/sites/mynewsite.local`
- `<Directory ...>` path → same as DocumentRoot
- Log filenames → `mynewsite.local-error.log` / `mynewsite.local-access.log`

### 3. Add to /etc/hosts (local testing only)

```bash
echo "127.0.0.1  mynewsite.local www.mynewsite.local" | sudo tee -a /etc/hosts
```

### 4. Reload Apache (no full restart needed)

```bash
docker compose exec apache apachectl graceful
```

Open http://mynewsite.local:8081 in your browser.

---

## Testing without editing /etc/hosts

Use `curl` with an explicit `Host` header — no DNS entry required:

```bash
# Test default site
curl -s -o /dev/null -w "%{http_code}" http://localhost:8081/

# Test a named vhost
curl -H "Host: myapp.local" http://localhost:8081/

# See full response headers
curl -I -H "Host: myapp.local" http://localhost:8081/
```

---

## Included sample configs

| File | ServerName | DocumentRoot |
|---|---|---|
| `000-default.conf` | `localhost` | `/var/www/html` |
| `myapp.local.conf` | `myapp.local` | `/var/www/html/sites/myapp.local` |

> **Note:** The `000-` prefix ensures the default vhost is loaded first.  
> Apache serves the first matching vhost alphabetically, so the default catches all unmatched hostnames.

