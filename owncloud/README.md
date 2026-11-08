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

## Latitude: move production off the temporary T1 disk

The production containers still use the `owncloud-staging` project name after
the 10.16.7 cutover. Until the following migration is executed, their database,
config, apps, and files remain on `/media/hannes/T1/owncloud-staging`.
Switching Git branches or validating Compose does not relocate these mounts.

The permanent stack is `latitude-compose.yml`. It keeps the project name,
network names, database credentials/identity, images, and loopback port 8093,
but stores files in `/daten/owncloud/data` and database, config, and apps in
sibling directories under `/daten/owncloud`. Its root-only credentials are
stored in `/daten/owncloud/production.env`; no `OWNCLOUD_STAGE_*` variables
are required for permanent operation.

Run directly on Latitude in a sudo-capable terminal:

```bash
cd /home/hannes/git/dockerApache
sudo bash owncloud/move-latitude-storage.sh --preflight
# Only after reviewing the comparison and passing all checks:
sudo bash owncloud/move-latitude-storage.sh --migrate
```

Preflight only reads application data. It checks the source mounts, both HTTP
routes, old native maintenance mode, internal mount, and sufficient free space,
and prints a dry-run rsync summary. It refuses an active mirror cron job:
keep that job disabled until the authoritative Latitude/mac2016 data source
has been decided. The actual migration updates the existing internal data
incrementally, including deletions, so the old native copy is no longer a
separate rollback copy. It must remain in maintenance.

HTTPS checks connect directly to the local Apache with
`--resolve www.mxtracks.info:443:127.0.0.1`, retaining hostname and certificate
verification. This avoids the Fritz!Box/public-DNS hairpin path; it verifies
the local production proxy, not external internet reachability.

The migration first copies files online, then enables maintenance for the final
sync and consistent PostgreSQL dump. It copies the PostgreSQL cluster only
after stopping its container, recreates the project on internal storage, and
checks both routes before releasing maintenance. T1 is not deleted. Failure
before release attempts to restart T1 production; once internal production may
accept writes, automatic rollback is deliberately disabled.

Logs, the database dump, configuration/apps archive, and old environment file
are stored under `/daten/owncloud/backups/pre-internal-<timestamp>/`. Protect
these credentials and additionally back up to a different disk/host. A failed
run leaves partial internal state for inspection; do not blindly rerun it.
An installed mirror sync script is backed up and refreshed to use internal
storage, but no mirror job is started or enabled. The migration also installs
`/etc/systemd/system/docker.service.d/owncloud-data.conf`: Docker will require
`/daten` to be mounted before starting on future boots. If the encrypted data
disk cannot be unlocked/mounted, Docker will not start (including other stacks),
rather than silently starting ownCloud against an empty directory. Docker is
not restarted during installation of this guard.

After successful migration, manage production with:

```bash
sudo docker compose --env-file /daten/owncloud/production.env \
  -f owncloud/latitude-compose.yml ps
sudo docker compose --env-file /daten/owncloud/production.env \
  -f owncloud/latitude-compose.yml up -d
```

Do not use the staging clone/upgrade/cutover scripts for this production stack.
Before removing T1, confirm no production container mounts it and perform a
controlled restart with `/daten` mounted. `/daten` is an encrypted internal
disk and must be mounted before Docker starts its containers after boot.

## Operations

### Latitude: isolated 10.16.7 to 11.1.0 upgrade test

ownCloud Server 11.1.0 is the official release dated November 3, 2026. Its
official `owncloud/server:11.1.0` image was verified to contain PHP 8.3.6.
The test does not change the production image or remove host PHP packages.

Run the read-only preflight directly on Latitude:

```bash
cd /home/hannes/git/dockerApache
sudo bash owncloud/prepare-upgrade-test.sh --preflight
```

After review, `--prepare` refreshes the old T1 files from current production
(including deletions), snapshots the production database/config/apps under
a brief maintenance window, then returns production to service before
attempting the isolated upgrade:

```bash
sudo bash owncloud/prepare-upgrade-test.sh --prepare
```

Preparation reuses locally available Docker images and pulls only missing
images before changing data. Test startup disables automatic pulls. If a
missing-image pull fails because the sudo Docker credential helper is
unconfigured, resolve that login configuration or pull the public image as
the regular Docker user first; no test snapshot has been started at that point.

**This consumes the old T1 rollback file snapshot.** Old T1 database/config
directories are not modified, but must no longer be treated as a consistent
rollback set with the refreshed files. Keep an independent current backup.
Fresh test state and its root-only environment are in
`/media/hannes/T1/owncloud-upgrade-11.1`; file data is reused from
`/media/hannes/T1/owncloud-staging/files` to avoid a second 795 GB copy.
Preparation refuses existing test state and is not blindly resumable.
If preparation stops while verifying production recovery after the completed
snapshot, `--resume-snapshot` can continue that snapshot without recopying files
or stopping production. It requires the dump, original maintenance config and
snapshot log, and refuses any already-created test database or containers.
Do not use it after the test upgrade has started. Maintenance-off HTTP checks
allow a bounded delay for the web runtime to observe the CLI config change.

The test stack has a separate project, database, Redis and internal-only
network, no automatic restart, no background cron, and bounded Docker logs.
It binds only localhost:8094. Access from your client:

```bash
ssh -N -L 8094:127.0.0.1:8094 latitude
```

Open `http://localhost:8094`, and verify login, download/upload, encrypted
files, contacts, sharing, and enabled apps. Outbound integrations (mail,
external storage, federation, OnlyOffice) cannot reach production or the
internet and cannot be validated in this isolated test. Review disabled apps
after the upgrade: compatibility is not established by a healthy status alone.

For logs and stopping the test, use sudo on Latitude:

```bash
sudo docker compose --env-file /media/hannes/T1/owncloud-upgrade-11.1/test.env \
  -f owncloud/upgrade-test-compose.yml logs --tail 100 owncloud
sudo docker compose --env-file /media/hannes/T1/owncloud-upgrade-11.1/test.env \
  -f owncloud/upgrade-test-compose.yml stop
```

No production cutover is included. Production remains under `/daten/owncloud`
and must not become dependent on T1 again. Removing host PHP 7 requires a
separate review of Apache handlers, CLI defaults and native scheduled jobs;
Apache on Latitude still loads mod_php7.4 even though production ownCloud is
already dockerized.

If the isolated upgrade stops because Marketplace access is blocked and reports
`checksum`, `contacts`, and `files_textviewer` as incompatible, the targeted
recovery helper downloads official Contacts 1.6.0 on the host and installs it
offline. It preserves the old Contacts code and disables only `checksum` and
`files_textviewer` in the test database; neither has a compatible release in the
11.1 Marketplace catalogue. It does not open the isolated network or alter
production. Run against the stopped, existing test:

```bash
sudo bash owncloud/resume-upgrade-apps.sh
```

Contacts data remains in the cloned database. Verify contact listing/editing
and app compatibility in the test before considering a production cutover.

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
