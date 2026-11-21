# Home Assistant – Reverse Proxy Setup
## Current deployment (2026-11-14)

Production now runs on **beelink** at `http://beelink:8123/`, with configuration
under `/home/hannes/homeassistant/config`. Its `homeassistant` container uses
host networking and `restart: unless-stopped`.

**mac2016** has a stopped container using `/Users/hannes/server/homeassistant/config`.
It publishes port 8123 and has `restart: no`, so it does not start automatically.
`monitoring/sync-homeassistant-standby.py --install`, run as the mac2016 user,
creates a daily 03:50 LaunchAgent and immediately makes a private snapshot under
`~/server/homeassistant/standby-snapshots/`. It retains seven managed snapshots
and attempts to send failure notifications by email. The user LaunchAgent
requires the macOS user session, Docker and network access to beelink; mac2016
must be awake for the job to run. The running production instance is not
stopped. Its SQLite history uses SQLite's online backup API; configuration files
are copied while Home Assistant runs, so simultaneous settings changes are not
a transactional point-in-time snapshot.
Both hosts currently use Home Assistant **2025.4.2**, pinned to image digest
`ghcr.io/home-assistant/home-assistant@sha256:205fbf9450ebb5570eb0e4e53e64b1af040bf5725056b14293c659ca4dcd8a05`.

For manual failover, first stop Home Assistant on beelink (or ensure that host is
offline). On mac2016, confirm `docker inspect -f '{{.State.Running}}' homeassistant`
prints `false`, inspect the snapshot dates, and choose the newest successful
`snapshot-...` directory. Preserve the existing config and copy the selected
snapshot into the container's existing bind-mount path before starting it:

```bash
ls -ld ~/server/homeassistant/standby-snapshots/snapshot-*
# Set SNAPSHOT to the selected full snapshot path.
SNAPSHOT="$HOME/server/homeassistant/standby-snapshots/snapshot-YYYYMMDDTHHMMSSZ"
test -f "$SNAPSHOT/.beelink-standby-snapshot" || exit 1
test "$(docker inspect -f '{{.State.Running}}' homeassistant)" = false || exit 1
test ! -e ~/server/homeassistant/config-before-failover || exit 1
mv ~/server/homeassistant/config ~/server/homeassistant/config-before-failover &&
  cp -a "$SNAPSHOT" ~/server/homeassistant/config &&
  docker start homeassistant
```

Access it at `http://mac2016:8123/`. This does not switch public DNS or the
reverse proxy. macOS container networking and hardware access differ from
Linux; device discovery, USB and Bluetooth functionality need separate testing.
Stop the Mac container before restarting production on beelink. Changes made
during failover are not synchronized back automatically.
