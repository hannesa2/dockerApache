# ownCloud standby mirror

The production ownCloud at latitude is version 10.16.6, which requires PHP
7.2–7.4. Its mirror therefore uses the isolated official
`owncloud/server:10.16.6` image, whose runtime is PHP 7.4 (currently 7.4.33);
it is not executed by the main PHP 8.3 Apache container. Only the image's
Apache/PHP runtime is used — the actual ownCloud application code and config
are bind-mounted in from the mirrored data (see below), so the image's own
embedded ownCloud version is irrelevant to the mounted application version and
can be moved forward within the 10.x line independently of it.

The image tag currently matches the mounted application version, but that is
coincidental, not required: the image only supplies the OS/PHP runtime, never
its own bundled application code (see above). `10.16.6` is ownCloud's actively
maintained current 10.x release, rebuilt on Ubuntu 22.04 with an up-to-date,
patched PHP 7.4 — e.g. it already carries the October 2026 "php2shell" fixes
(CVE-2026-109618 / CVE-2026-109619). Re-pull/rebuild this image periodically
(`docker compose build owncloud`) to keep picking up PHP security fixes,
independently of any ownCloud application upgrade.

## mac2016 setup

`prepare-mirror.sh` requires the existing 10.16.6 installation and data copy
on Samsung T7. It creates private runtime directories and passwords, adds the
Apache `/owncloud/` proxy include, builds the PHP image, and attaches Apache to
the private ownCloud proxy network:

```bash
./owncloud/prepare-mirror.sh
```

## latitude setup

From the repository checkout on latitude, run once as root:

```bash
sudo ./owncloud/install-latitude-sync.sh
```

The installer registers a daily 03:45 cron job and performs the initial sync.
The root job is necessary because latitude's ownCloud data directory is
readable only by `www-data`. It uses the existing latitude-to-mac2016 SSH key
and known-hosts entry. Bulk file changes sync before maintenance mode; latitude
is briefly put in maintenance for a final file delta and consistent
PostgreSQL 14 custom-format database dump. A trap restores source maintenance
mode if the job fails.

To configure completion emails without starting another sync, run:

```bash
sudo ./owncloud/install-sync-notifications.sh
```

This wraps the scheduled ownCloud mirror, GitLab backup transfer, and
PostgreSQL backup jobs. Each sends a completion email to
`hannes.software@gmx.at` with status, times, exit code, and the relevant end of
the log. Failed PostgreSQL commands now stop the script so failures are
reported accurately. The installer sends one test email and does not run any
backup or sync.

On mac2016, the data sync requires Homebrew rsync 3 or newer because the
system-provided rsync 2.6.9 cannot receive the legacy Latin-1 filenames used
by some ownCloud files. Install it before the first sync:

```bash
brew install rsync
```

The sync explicitly invokes `/usr/local/bin/rsync` on mac2016 and converts
Latin-1 filenames to valid UTF-8 for the T7 filesystem. Already transferred
files are reused on retry.

The T7 tree has explicit safety markers. The sync refuses to run without those
markers, the source `.ocdata` marker, ownCloud 10.16.6, and the expected source
database. Rsync is incremental and removes mirror-only files to keep the
standby aligned. A failed mirror database restore leaves the mirror in
maintenance mode rather than serving mismatched data.

The container mounts the T7 data at the same absolute path used on latitude
(`/daten/owncloud/data`). The copied ownCloud database stores local storage IDs
with that path; preserving it avoids stale file-cache references. Container
startup creates the PHP session directory at `/mnt/data/sessions` with access
for `www-data`, as required by the ownCloud image's PHP configuration. The
mirror config generator is mounted from this repository so sync runs always
use the current data-directory setting rather than a stale copy baked into an
older image. The rsync excludes this runtime-only directory from deletion, and
the sync recreates it before enabling the mirror after each database restore.

## Operations

```bash
# On mac2016
docker compose -f apache/docker-compose.yml --profile owncloud ps
docker compose -f apache/docker-compose.yml --profile owncloud logs owncloud

# On latitude
sudo /usr/local/sbin/owncloud-mirror-sync
tail -n 100 /var/log/owncloud-mirror-sync.log
```

The mirror remains a standby, not a second independent writer. Its writes are
replaced by the next daily source sync. ownCloud 10.16.6 and PHP 7.4 are both
legacy software; the dedicated runtime is for compatibility and should be
upgraded separately rather than changing the production-matched backup code
in place. "Upgraded separately" means bumping the `owncloud/server` image tag
in [`Dockerfile`](Dockerfile) to a current 10.x release for a patched PHP 7.4
runtime (see above) — not touching the mounted 10.16.6 application code, which
must keep matching latitude's real production version.
