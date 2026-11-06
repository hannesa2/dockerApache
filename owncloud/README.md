# ownCloud on mac2016

mac2016 is the current ownCloud source; Latitude is offline. No Latitude sync
is needed or should be run for this setup. ownCloud on mac2016 was upgraded
locally from 10.16.6 to 10.16.7 in its isolated
`owncloud/server:10.16.7` PHP runtime and PostgreSQL 16 database. The database
schema upgrade completed successfully. User files and database contents are
the existing mac2016 copy; they were not replaced from Latitude.

The application currently remains in maintenance mode, and Apache denies
`/owncloud` on both HTTP and HTTPS. This keeps it closed until you intentionally
enable service; maintenance mode alone is not a network security boundary.

The mirror's pre-upgrade code and database dump, plus a post-upgrade database
dump, are stored under
`/Volumes/SamsungT7_2T/ownCloudBackup/owncloud/backups/pre-10.16.7-20261106_074955/`.

## mac2016 setup

This preparation has already been run on mac2016. It prepares private runtime
directories and passwords and builds the isolated PHP image. The T7 code was
then upgraded locally to 10.16.7; the original 10.16.6 code and database dump
are retained in the backup directory above. For a fresh existing mirror, run:

```bash
./owncloud/prepare-mirror.sh
```

## Latitude synchronization

The Latitude-to-mac2016 sync scripts are not part of the current operating
procedure. Latitude is offline, mac2016 is the source, and running an old
Latitude sync could overwrite mac2016's current files and database.

## Operations

```bash
# On mac2016
docker compose -f apache/docker-compose.yml --profile owncloud ps
docker compose -f apache/docker-compose.yml --profile owncloud logs owncloud
```

## Enable ownCloud access

The activation helper replaces the Apache deny blocks in both virtual hosts,
validates the Apache configuration, reloads it, and checks the HTTPS status
endpoint. It does not depend on Latitude or a sync marker:

```bash
./owncloud/activate-proxy.sh
docker compose -f apache/docker-compose.yml --profile owncloud exec -T --user www-data owncloud \
  php /var/www/owncloud/occ maintenance:mode --off
```

After the route check succeeds, disable maintenance mode to serve requests.
Because mac2016 is now the source, do not sync old Latitude data over it.
To close access again, first enable maintenance mode on mac2016:

```bash
docker compose -f apache/docker-compose.yml --profile owncloud exec -T --user www-data owncloud \
  php /var/www/owncloud/occ maintenance:mode --on
```

Replace the two proxy `Include` lines in the active vhost with this block, then
validate/reload Apache and confirm the URL returns HTTP 403:

```apache
<LocationMatch "^/owncloud(?:/|$)">
    Require all denied
</LocationMatch>
```
