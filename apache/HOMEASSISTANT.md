# Home Assistant – Reverse Proxy Setup
Exposes the Home Assistant Docker container running on **latitude** at
`http://localhost:8123` via a public HTTPS domain `homeassistant.mxtracks.info`,
using the native Apache reverse proxy already present on latitude.
---
## Architecture
```
Browser (LAN/WAN)
  │
  │  HTTPS :443
  ▼
Native Apache on latitude   (/etc/apache2)
  │  HTTP :8123 (localhost)
  ▼
Home Assistant Docker container
```
---
## One-time Setup on latitude
### 1 – Issue the Let's Encrypt certificate
The pre/post hooks in `/etc/letsencrypt/renewal-hooks/` only run during
`certbot renew`, **not** during manual `certbot certonly`.  Stop Apache first:
```bash
sudo systemctl stop apache2
sudo certbot certonly --standalone -d homeassistant.mxtracks.info
sudo systemctl start apache2
```
### 2 – Enable required Apache modules
```bash
sudo a2enmod proxy proxy_http proxy_wstunnel rewrite headers ssl
```
### 3 – Install and enable the vhost
```bash
sudo cp ~/git/dockerApache/apache/native-proxy-homeassistant.conf \
         /etc/apache2/sites-available/homeassistant.mxtracks.info.conf
sudo a2ensite homeassistant.mxtracks.info.conf
sudo apache2ctl configtest && sudo systemctl reload apache2
```
The vhost config (`native-proxy-homeassistant.conf`) provides:
- HTTP → HTTPS redirect (301)
- WebSocket proxy (`proxy_wstunnel`) for real-time HA updates
- `X-Forwarded-Proto: https` header so HA knows it is behind TLS
- HSTS header
### 4 – Tell Home Assistant to trust the Apache proxy
Home Assistant rejects requests from untrusted proxies with **400 Bad Request**.
Find the config directory:
```bash
# The volume mount path is shown under "Mounts" in docker inspect:
docker inspect $(docker ps -q -f name=home) | grep -A3 '"Mounts"'
# Common paths:
# ~/.homeassistant/configuration.yaml
# /usr/share/hassio/homeassistant/configuration.yaml
```
Add (or merge) the `http:` block in `configuration.yaml`:
```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 127.0.0.1
    - ::1
```
Restart Home Assistant:
```bash
docker restart homeassistant   # replace with actual container name
```
### 5 – Verify
```bash
# Bypass DNS (tests Apache + HA directly):
curl -sL --resolve homeassistant.mxtracks.info:443:192.168.178.129 \
  https://homeassistant.mxtracks.info -o /dev/null -w "%{http_code}\n"
# Expected: 200
# Check the issued cert:
echo | openssl s_client -connect 192.168.178.129:443 \
  -servername homeassistant.mxtracks.info 2>/dev/null \
  | openssl x509 -noout -subject -issuer
# Expected: subject=CN = homeassistant.mxtracks.info
#           issuer=C = US, O = Let's Encrypt, …
```
---
## LAN access (Fritz!Box NAT hairpin fix)
`homeassistant.mxtracks.info` is a CNAME to `*.myfritz.net`.
Fritz!Box resolves it to its **own** IP internally, intercepting HTTPS with its
self-signed certificate.  The fix is to override DNS on each device so the
domain resolves to latitude's **LAN IP** instead.
### Mac / Linux – /etc/hosts
Add **both** IPv4 and IPv6 entries.  The IPv6 entry is required because macOS
(Happy Eyeballs) prefers IPv6 and Fritz!Box hands out its own IPv6 via Router
Advertisement — without an override, the browser still hits Fritz!Box even with
the IPv4 entry in place.
```bash
sudo tee -a /etc/hosts << 'EOF'
# latitude – internal services (Fritz!Box NAT hairpin workaround)
192.168.178.129                        homeassistant.mxtracks.info
fdf4:be15:98e0:0:162:361:f85d:ef9b    homeassistant.mxtracks.info
# Other services on latitude (add if not already present):
192.168.178.129                        nextcloud.mxtracks.info
fdf4:be15:98e0:0:162:361:f85d:ef9b    nextcloud.mxtracks.info
192.168.178.129                        www.mxtracks.info
fdf4:be15:98e0:0:162:361:f85d:ef9b    www.mxtracks.info
192.168.178.129                        dev.mxtracks.info
fdf4:be15:98e0:0:162:361:f85d:ef9b    dev.mxtracks.info
EOF
# Flush DNS cache:
sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder
```
### Latitude itself – /etc/hosts
So that `curl` and other tools on latitude resolve the domain correctly:
```bash
echo "127.0.0.1  homeassistant.mxtracks.info" | sudo tee -a /etc/hosts
```
### Android
Use **Private DNS** (DoT) pointing to the CoreDNS container on latitude
(see `nextcloud/` README section *Fix B – Android Private DNS*), or configure
a static IP with DNS `192.168.178.129` in WiFi settings.
### dnsmasq (if split-DNS profile is running)
Add the domain to `EXTRA_LOCAL_DOMAINS` in `nextcloud/.env`:
```dotenv
EXTRA_LOCAL_DOMAINS=www.mxtracks.info dev.mxtracks.info homeassistant.mxtracks.info
```
Then restart dnsmasq:
```bash
cd ~/git/dockerApache/nextcloud
docker compose --profile split-dns up -d --force-recreate dnsmasq
```
---
## Certificate Renewal
Certbot auto-renews via systemd timer or cron.  The pre/post hooks in
`/etc/letsencrypt/renewal-hooks/` stop Apache before the ACME challenge and
restart it after, so renewal is fully automatic.
Verify hooks are present:
```bash
ls /etc/letsencrypt/renewal-hooks/pre/   # stop-apache.sh
ls /etc/letsencrypt/renewal-hooks/post/  # start-apache.sh
```
Test renewal dry-run:
```bash
sudo certbot renew --dry-run --cert-name homeassistant.mxtracks.info
```
---
## Troubleshooting
| Symptom | Cause | Fix |
|---------|-------|-----|
| Browser shows Fritz!Box self-signed cert | NAT hairpin: DNS returns public IP | Add IPv4 + IPv6 to `/etc/hosts` |
| `curl` shows self-signed from latitude | `/etc/hosts` on latitude missing | `echo "127.0.0.1 homeassistant.mxtracks.info" \| sudo tee -a /etc/hosts` |
| `400 Bad Request` from aiohttp | HA doesn't trust the proxy | Add `trusted_proxies` to `configuration.yaml` and restart HA |
| `405 Method Not Allowed` | HA doesn't support HEAD requests | Normal – use `curl -sL` (GET) to test, not `curl -I` (HEAD) |
| WebSocket disconnects / real-time not working | `proxy_wstunnel` module missing | `sudo a2enmod proxy_wstunnel && sudo systemctl reload apache2` |
