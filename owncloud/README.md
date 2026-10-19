# ownCloud standby mirror

The production ownCloud at latitude is version 10.13.4, which rejects PHP 8.0
and newer. Its mirror therefore uses the isolated official
`owncloud/server:10.13.4` image, whose runtime is PHP 7.4; it is not executed by
the main PHP 8.3 Apache container.

## mac2016 setup

`prepare-mirror.sh` requires the existing 10.13.4 installation and data copy
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
markers, the source `.ocdata` marker, ownCloud 10.13.4, and the expected source
database. Rsync is incremental and removes mirror-only files to keep the
standby aligned. A failed mirror database restore leaves the mirror in
maintenance mode rather than serving mismatched data.

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
replaced by the next daily source sync. ownCloud 10.13.4 and PHP 7.4 are both
legacy software; the dedicated runtime is for compatibility and should be
upgraded separately rather than changing the production-matched backup code
in place.
