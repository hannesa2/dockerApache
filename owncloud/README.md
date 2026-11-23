# ownCloud on beelink and mac2016

| | beelink | mac2016 |
|---|---|---|
| Role | Production | Standby mirror |
| URL | https://www.mxtracks.info/owncloud/ | https://www16.mxtracks.info:9443/owncloud/ |
| Web container | `beelink-apps-owncloud-app-1` | `apache-owncloud-1` |
| Database container | `beelink-databases-owncloud-1` | `apache-owncloud-db-1` |

Both installations use ownCloud 11.1.0 and PostgreSQL 16. The mirror is a
separate copy, not live replication. Do not write to both instances: the next
sync overwrites mirror changes and removes files deleted in production.

On beelink the production files are under
`/daten/beelink-runtime/rootfs/daten/owncloud/data` and the application
configuration and writable apps are under
`/daten/beelink-runtime/apps/owncloud/volume`. On mac2016 the mirror is under
`/Volumes/SamsungT5_1T/ownCloudBackup/owncloud`. These directories include private
data and credentials. The beelink T7 and mac2016 T5 must be mounted.

## Mirror synchronization

The `beelink-job-owncloud.timer` systemd timer schedules the sync at 04:00 on
beelink. The job checks both installations, enables production maintenance for
the final copy, syncs files, configuration, apps and PostgreSQL, then restores
production and the mirror. Both may be temporarily unavailable during the job.
The mirror's pre-sync database dump is retained under its `backups` directory.
The job reports success or failure; check its current state before relying on
the mirror:

```bash
systemctl status beelink-job-owncloud.timer beelink-job-owncloud.service --no-pager
sudo journalctl -u beelink-job-owncloud.service -n 100 --no-pager
```

The implementation is `sync-beelink.sh` and runs only in beelink production
mode. Updating the repository does not update the root-owned installed copy
automatically.
Do not run a manual sync while another is active or while the mirror is
accepting independent production writes.

## Back In Time backup

The Back In Time backup is separate from the mac2016 mirror: it is intended to
retain snapshots on beelink's encrypted `/datenBackup` disk, even after files
are deleted in production. Back In Time 1.6.1 is installed on beelink. The
backup disk's ext4 filesystem UUID is `fe91ecb5-c1a4-4e6c-9edf-f3c9c75cffe1`.

`backintime-beelink.sh` provides a **manual**, root-only backup. It has not yet
been run against production, and no complete snapshot or restore has been
verified. Do not start a direct Back In Time snapshot of the live ownCloud
directories and treat it as a consistent, restorable backup.

The intended first scope is ownCloud only:

* The production data directory and `apps/owncloud/volume` paths listed above,
  including configuration and writable apps.
* A fresh, verified PostgreSQL dump of `owncloud_stage`, taken from
  `beelink-databases-owncloud-1` while ownCloud is stopped.
* The information needed to reconstruct the matching application and database
  runtime, including the production image identity and configuration. These
  contain credentials and must be protected along with the snapshots.

The wrapper verifies the mount identities and serializes with the production
lifecycle and mirror locks. It checks ownCloud health, enables maintenance,
stops the web container, validates a PostgreSQL dump and runs Back In Time in
the foreground. It attempts service recovery even on error and reports recovery
failures. The generated profile has scheduling and automatic deletion disabled;
the wrapper must always be used. Snapshots go to the root-private
`/datenBackup/owncloud-root`, separate from the existing user profile.

The first snapshot copies approximately 799 GiB and can keep ownCloud unavailable
for hours; subsequent snapshots can also take time. About 1.7 TiB was free on
the destination when checked on 23 November 2026; check it again before each
run. No retention or automatic failure notification is configured. A full
isolated restore rehearsal must succeed before relying on this backup or
enabling a schedule.

After transferring the script to beelink, log in to beelink directly and install
it as root. The following assumes it was transferred to
`/home/hannes/owncloud-backintime-beelink.sh`:

```bash
sudo install -o root -g root -m 0755 /home/hannes/owncloud-backintime-beelink.sh \
  /usr/local/sbin/owncloud-backintime-beelink
df -h /daten /datenBackup
sudo systemd-run --unit=owncloud-backintime --property=TimeoutStartSec=0 \
  /usr/local/sbin/owncloud-backintime-beelink --backup
sudo journalctl -fu owncloud-backintime.service
```

This starts a one-off service, not a timer; check `systemctl status
owncloud-backintime.service` and the journal for both snapshot and recovery
errors. If interrupted by power loss or SIGKILL, the cleanup trap cannot run:
inspect and recover ownCloud manually before attempting another backup.
The snapshot contains `owncloud.dump`, image and container metadata under
`/daten/beelink-backups/backintime-owncloud` within its `backup` tree. It also
contains a configuration captured with maintenance enabled. A recovery must
restore the matching data, apps, configuration and PostgreSQL dump together,
recreate the matching images and credentials, and explicitly disable
maintenance before exposing the service. The image metadata is not an image
archive; ensure matching images remain available independently. Test this in
an isolated environment rather than overwriting production.

## Check the applications

```bash
# On beelink
docker exec --user www-data beelink-apps-owncloud-app-1 occ status
curl --noproxy '*' --resolve www.mxtracks.info:443:127.0.0.1 \
  https://www.mxtracks.info/owncloud/status.php

# On mac2016
docker exec --user www-data apache-owncloud-1 php /var/www/owncloud/occ status
curl --noproxy '*' --resolve www16.mxtracks.info:9443:127.0.0.1 \
  https://www16.mxtracks.info:9443/owncloud/status.php
```

Domain URLs require DNS to resolve to the intended host. The mirror can lag
behind production; verify the latest successful sync before any failover.
