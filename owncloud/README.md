# ownCloud on Latitude and mac2016

Latitude is production; mac2016 is the standby mirror. Both were upgraded to
ownCloud 11.1.0 on November 9, 2026 and run with PHP 8.3.6 and PostgreSQL 16.
Both are online with maintenance disabled. The mirror is a separate copy, not
continuous live replication: its currency depends on successful synchronization.
Do not allow independent writes on both instances if an exact standby is needed.

The mac2016 upgrade used `upgrade-mac2016.sh` without an additional backup as
requested. Both its PHP image and bind-mounted code were updated; Contacts
1.6.0 was installed, and incompatible `checksum`, `files_textviewer` and legacy
`gallery` were disabled. PDF and media viewers remain enabled.
The mirror data mount must target `/mnt/data/files`, matching persisted account
homes. Concurrent deployment edits briefly broke this mapping; the main user's
cache was recovered from Latitude with original file IDs retained.
4,154 reversible double-encoded filenames were normalized without changing
file contents; one collision was retained without overwriting. The helper
`fix-mirror-filenames.py` defaults to a dry run and requires `--apply` to rename.
Representative JPEG/PDF/MOV/MP4 streams and an image thumbnail were verified.

The mirror's pre-upgrade code and database dump, plus a post-upgrade database
dump, are stored under
`/Volumes/SamsungT5_1T/ownCloudBackup/owncloud/backups/pre-10.16.7-20261106_074955/`.

## mac2016 setup

This preparation was originally run on mac2016 before its upgrades. It prepares
private runtime directories and passwords and builds the isolated PHP image.
Do not rerun it against the upgraded instance: the old source configuration
is not the current 11.1 runtime configuration. For a fresh existing mirror, run:

```bash
./owncloud/prepare-mirror.sh
```

## Latitude synchronization

Latitude is the authoritative source again. The version upgrade did not start
or enable any sync job. Review source paths, runtime version and schedules
before enabling replication; this standalone mirror upgrade did not refresh
all mirror files or import the full current production database.

To enable the daily **04:00 Latitude time** ownCloud sync, run directly on
Latitude in a sudo-capable session:

```bash
sudo bash monitoring/install-sync-job-status.sh
sudo bash owncloud/install-latitude-sync.sh
```

The installer first runs a complete 11.1.0 sync and installs the cron entry only
after success. It refuses an existing cron file rather than creating duplicate
jobs. The job-email wrapper records start/completion/result for availability
reports. Files (including deletions), writable apps, mirror-only configuration
and PostgreSQL are synchronized from Latitude. Filenames are already UTF-8;
the former ISO-8859-1 conversion must not be applied to these production files.
Production is briefly stopped in maintenance for the final consistent file/DB
snapshot, then returned online. The mirror is stopped during synchronization
and becomes reachable again afterwards, as requested. Writes made independently
on mac2016 are overwritten by the next sync. This is daily replication, not
continuous synchronization.

Some historical trash files on Latitude had Latin-1 filenames mixed with valid
UTF-8. On November 9, 2026, 115 invalid names were normalized; one collision
was retained under a unique `latin1-conflict-…` name without overwriting either
file. Changes are journaled in the production config directory as
`filename-normalization-<timestamp>.jsonl` with base64 paths. The
`normalize-latitude-filenames.php` helper defaults to preflight; apply must run
with the production web container stopped. It refuses invalid names outside
the trash tree pending separate metadata review. A blanket rsync Latin-1
conversion is not appropriate for this mixed-encoding source.

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

### Latitude: controlled production upgrade to 11.1.0

After the isolated browser test succeeds, run on Latitude:

```bash
sudo bash owncloud/upgrade-latitude.sh --preflight
sudo bash owncloud/upgrade-latitude.sh --apply
```

Apply consumes the approved T1 test file copy: it stops the test web container
and refreshes `/media/hannes/T1/owncloud-staging/files` from current production,
including deletions. Under a production maintenance window it makes the final
file sync and saves a consistent database dump, config/apps archive and private
deployment metadata under `/media/hannes/T1/owncloud-production-pre-11.1`.
This is the independent pre-upgrade backup; never run test preparation or
recovery against its file tree afterwards. The old test database no longer
matches these files. Keep T1 until the new production has been verified and
another independent backup is available.

The upgrade installs verified Contacts 1.6.0, disables `checksum` and
`files_textviewer`, repairs persisted user-home/upload paths using the same
transaction tested in the isolated clone, and explicitly runs `occ upgrade`.
ONLYOFFICE is not disabled in production. Before releasing maintenance it
checks version/schema status over loopback and the local TLS proxy and reads
representative JPEG/PDF/MOV/MP4 streams through ownCloud.

The Compose default remains 10.16.7; successful cutover persists
`OWNCLOUD_IMAGE=owncloud/server:11.1.0` in the private `production.env` so normal
Compose invocations recreate the upgraded version. The script refuses existing
upgrade backup state. Failures after the maintenance window begins leave the
web container offline for inspection; never start 10.16.7 against an upgraded
database. Rollback requires the saved database, config and apps together, plus
the matching file backup. No automatic rollback occurs after release because
users may have written new data. Host PHP removal is a separate operation.

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
It publishes no host ports: Docker does not expose published ports on this
internal network. The scripts check HTTP through the container's bridge IP
with the trusted `Host: localhost:8094` header. Access from your client using
the current test IP (it can change when the container is recreated):

```bash
TEST_IP="$(ssh latitude 'docker inspect -f '\''{{with index .NetworkSettings.Networks "owncloud-upgrade-test_isolated"}}{{.IPAddress}}{{end}}'\'' owncloud-upgrade-test-owncloud-1')"
test -n "$TEST_IP" && ssh -N -L "127.0.0.1:8094:$TEST_IP:8080" latitude
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
recovery helper installs official Contacts 1.6.0 offline. It verifies the
release archive against its SHA-256 before use. If GitHub returns an error,
stage the previously verified archive at `/home/hannes/contacts-1.6.0.tar.gz`;
otherwise the helper retries the official download to a temporary file and
discards it unless its checksum matches. The helper preserves the old Contacts
code and disables only `checksum` and `files_textviewer` in the test database;
neither has a compatible release in the 11.1 Marketplace catalogue. It does
not open the isolated network or alter production. Run against the stopped,
existing test:

```bash
sudo bash owncloud/resume-upgrade-apps.sh
```

Recovery releases maintenance only in the isolated copy before explicitly
running `occ upgrade`. Exit status 3 means it is already up to date; other
nonzero upgrade statuses stop recovery. A retained maintenance flag otherwise
causes the image's startup upgrade to skip work indefinitely.

Both preparation and recovery relocate cloned `oc_accounts.home` values from
`/daten/owncloud/data/<user>` to `/mnt/data/files/<user>`, along with upload
storage paths. Changing `datadirectory` alone does not relocate persisted user
homes. The transactional repair preserves account IDs, numeric storage IDs,
file-cache rows and share references; unexpected home paths abort the repair.
Historical data-root cache entries are retained, not merged or deleted.
Recovery also disables ONLYOFFICE only in the test, so PDF/media viewing does
not depend on an external document server blocked by the isolated network.

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
