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

The first attempt with `backintime-beelink.sh` was stopped after more than
13 hours of downtime. A preliminary online copy was completed on 26 November
2026. A final pass completed on 26 November 2026 at 06:40 with snapshot
`20261126-063812-580`; it included a verified PostgreSQL dump and ownCloud was
confirmed healthy afterwards. The partial isolated restore test below passed;
a full application recovery has not yet been tested.

### Automatic consistent backups

`schedule-backintime-beelink.py --install` installs the two backup wrappers and
the scheduler under `/usr/local/libexec/owncloud-backintime`. Run installation
locally as root on beelink from the repository checkout at
`~/git/dockerApache/owncloud`; the scheduler and both wrappers are maintained
there. The installer copies them to the root-owned runtime directory.
Installation enables timers but does not start a backup. Deployment requires
this privileged step; changing this repository alone does not install it.

The `owncloud-backintime-cycle.timer` runs daily at **23:00 local time**:

* Create a fresh online preliminary snapshot (or resume a valid incomplete one).
  It is never counted as a consistent backup.
* Only after online success, check space and remaining changed files again.
  More than 1 GiB of remaining changes or reaching 02:40 prevents maintenance.
* Run the existing consistent final wrapper as a bounded systemd service.
  It acquires the production lifecycle and mirror locks before maintenance;
  lock contention fails safely and triggers an alert rather than stopping
  ownCloud while waiting. The final service has a **15-minute runtime limit**
  plus up to 180 seconds for recovery, not a guarantee of three-minute downtime.
* Only after final success, normal application recovery and matching snapshot
  dump/capture verification is the new ID recorded as a consistent success.
* Afterwards, `backup-dev-wordpress-beelink.sh` backs up `dev.mxtracks.info`
  online into its **separate** Back In Time history at
  `/datenBackup/dev-wordpress-root/backintime/beelink/root/1`. It saves
  `/daten/beelink-runtime/wordpress/www/dev.mxtracks` and a compressed
  `wordpress` MySQL dump from `beelink-databases-native-mysql-1`. The site
  remains online; file and database states can differ in time. The dump uses
  `--single-transaction` and requires all base tables to be InnoDB. Dump gzip
  integrity and the snapshot's captured dump are checked. The MySQL dump has
  not been independently restored by this check.

The online phase has a maximum runtime of three hours, shortened near the
02:40 deadline. Existing night jobs start at 03:05. No catch-up job runs after
boot outside the backup window. A running full restore rehearsal defers the
backup and pruning, with an explicit alert; the next daily run tries again.

At setup, the 1.8 TiB backup disk had about **799 GiB used and 941 GiB free**.
That is useful headroom, not proof that arbitrary future changes fit. Before
each phase, rsync dry-run statistics estimate changed full-file bytes. Required
space is **100 GiB reserve + 1.5 times the estimated delta + 1 GiB overhead**.
Insufficient space fails before maintenance; no emergency deletion sacrifices
the newest backup. Growth during copying can still exhaust space, so capacity
checks do not replace failure alerts or monitoring.

Retention keeps the union of the newest verified snapshot on **7 distinct
dates, 4 distinct ISO weeks and 3 distinct months**. These overlap rather than
representing 14 guaranteed separate snapshots. The original verified
`20261126-063812-580` is additionally pinned. Unknown/manual snapshots are not
deleted. Only scheduler-managed consistent snapshots outside that union and
managed online preliminaries older than a verified final are removed, through
Back In Time's removal command. Pruning never runs during the full restore test.
Hardlinks share unchanged data; changing/deleting large files can still require
more storage than this disk provides. Retention never guarantees future capacity.
Dev WordPress applies the same 7/4/3 bucket retention independently, only
to scheduler-verified dev snapshots; unknown snapshots are left untouched.
Its first run needs space for a full copy; before every dev backup, the full
website size plus headroom and the 100 GiB safety reserve are required even if
an incremental copy may use less. If dev backup fails, the overall scheduled
job reports failure while the already verified ownCloud snapshot is retained.
If the ownCloud phase fails or is deferred, dev is not backed up that night.

`owncloud-backintime-monitor.timer` checks hourly at minute 30 for a missing
mount, less than 100 GiB free, a failed cycle or a consistent backup older than
30 hours. Initial state, changed errors and recovery are mailed to
`hannes.software@gmx.at` using the host's existing mail setup. Mail delivery
failures are service failures, not suppressed. Private state and verified IDs
are kept in `/var/lib/owncloud-backintime`.

Installation preserves hannes/root crontabs and comments out only active
`backintime backup --background` entries, including the old Sunday 14:00 job;
other jobs are untouched. Do not enable a GUI schedule bypassing these wrappers.

```bash
sudo python3 ~/git/dockerApache/owncloud/schedule-backintime-beelink.py --install
systemctl list-timers 'owncloud-backintime*' --no-pager
sudo journalctl -u owncloud-backintime-cycle.service \
  -u owncloud-backintime-preliminary-auto.service \
  -u owncloud-backintime-final-auto.service --no-pager -n 100
```

### Partial restore rehearsal on the Samsung SSD

On 26 November 2026 the test passed for snapshot `20261126-063812-580`:
the dump restored successfully, with 5 rows in `oc_users` and 266476 rows in
`oc_filecache`. Fourteen sample files (including configuration and the data
marker) were restored and checksum-verified, covering four users. Artifacts
remain at `/datenRestoreTest/stage1-RMxe54mu`. The test container was confirmed
stopped with networking disabled, and production remained healthy.

`owncloud/test-backintime-restore-beelink.sh --test` runs as root on beelink, with the
encrypted Samsung 870 QVO mounted manually at `/datenRestoreTest`. It checks
the exact test and backup filesystem UUIDs before writing. It does not configure
boot-time mounting, stop production, or restore into original paths.

The one-time disk initializer is `owncloud/prepare-owncloud-restore-test-disk.sh`.
It erases and reformats the specifically identified Samsung SSD; it is not a
cron job. Run it only when deliberately preparing that test disk, after
reviewing the disk identity and confirming that its existing contents can be
destroyed:

```bash
sudo bash ~/git/dockerApache/owncloud/prepare-owncloud-restore-test-disk.sh \
  --erase-and-prepare
```

The script uses snapshot `20261126-063812-580`, verifies and copies its database
dump, and copies up to three user files per user for up to four users (64 MiB per
file, 256 MiB total), plus the data marker and application configuration. It
compares SHA-256 checksums and retains a private sample manifest. This checks
byte-for-byte restoration, not whether each document opens in its application.

PostgreSQL 16 runs from an already installed, pinned image in a separate
container with no network or production mounts, capped at one CPU and 1 GiB RAM.
The dump is restored transactionally with errors fatal; ownCloud users and
file-cache tables must exist and contain rows. The container is stopped on exit.
The database, copied files and result remain under a newly created
`/datenRestoreTest/stage1-*` directory. They contain private data and credentials.
The stopped `owncloud-restore-test` container is also retained for inspection;
inspect it before removing it to run another test. A `RESULT.txt` containing
`PASS` is written only after restoration, database checks and a final production
health check succeed. A full application recovery remains a separate test.

### Full isolated ownCloud recovery rehearsal

`owncloud/test-full-restore-beelink.sh --test` prepares a full, independent recovery of
snapshot `20261126-063812-580` on the same encrypted test SSD. This rehearsal is
prepared but has not yet been run against the full snapshot. It requires a local
root invocation; the previous partial test and its stopped container can remain.

The script copies all backed-up user data, writable apps and configuration to
`/datenRestoreTest/full-recovery-20261126-063812-580`. It restores ownership and
modes from Back In Time's `fileinfo.bz2`; the snapshot's on-disk ownership alone
is not sufficient. There are no hardlinks back to the snapshot. Copying is capped
at 16 MiB/s with idle I/O priority, so about 799 GiB takes **at least 14 hours**
and may take substantially longer on the backup HDD. Production remains online,
but shared CPU, memory and disk resources mean zero performance impact cannot
be guaranteed. Space is checked with 10 GiB reserved for the database and overhead.
An interrupted copy can be resumed before database/configuration restoration
starts. After that phase starts, a rerun refuses existing artifacts for inspection.

Saved application environment metadata is used to evaluate the image's dynamic
configuration, preserving instance secrets and the database prefix. Only the
test configuration is rewritten: local test database, local URLs, no production
Redis, no mail delivery and no cron or image installation/upgrade hooks. Primary
object storage requires a separate plan and causes an explicit failure.
External storage and external integrations are **not tested**: both containers
share a network namespace with only loopback (`--network none` on the database),
with no published ports or production mounts. There is deliberately no browser
URL on the host. The restored ownCloud uses the exact saved application image;
PostgreSQL and Apache are CPU/memory limited.

Checks cover database restoration, ownCloud version/status, app enumeration, the
HTTP login form, existing user file reads through ownCloud's filesystem API, and
authenticated WebDAV upload/download/deletion using a new test-only account.
Existing user passwords are not reset. These checks do not prove every app,
document, external integration or original user's interactive login works.
Only after both test containers stop and production is healthy does the script
write `RESULT.txt` containing `PASS`. Private test data, logs and stopped
containers remain for inspection. Abrupt termination or host failure may require
manual cleanup of the test containers.

Run the tracked script from the beelink checkout, independently of the terminal:

```bash
sudo install -o root -g root -m 0755 ~/git/dockerApache/owncloud/test-full-restore-beelink.sh \
  /usr/local/sbin/owncloud-full-restore-test
sudo systemd-run --unit=owncloud-full-restore-test \
  --property=Nice=15 --property=IOSchedulingClass=idle \
  --property=CPUWeight=10 --property=IOWeight=10 \
  --property=TimeoutStopSec=180s --property=KillMode=control-group \
  /usr/local/sbin/owncloud-full-restore-test --test
sudo journalctl -fu owncloud-full-restore-test.service
```

Stopping `journalctl` does not stop the rehearsal. Neither the test script nor
the SSD setup configures boot-time mounting or automatic startup.

`backintime-online-beelink.sh` resumes the existing `new_snapshot` while
ownCloud is running. It does not acquire the production lifecycle lock, stop
the app, or dump the database. Its snapshot is **not a restorable backup**:
files can change during the copy, and its database dump is stale. The final
consistent pass is always required afterwards. Back In Time
can report errors on files modified during the live copy; inspect the journal.
This preliminary copy took nearly 40 hours on the current disk.

On beelink, install the tracked preliminary-copy script from the checkout:

```bash
sudo install -o root -g root -m 0755 ~/git/dockerApache/owncloud/backintime-online-beelink.sh \
  /usr/local/sbin/owncloud-backintime-online-beelink
sudo systemd-run --unit=owncloud-backintime-online --property=TimeoutStartSec=0 \
  /usr/local/sbin/owncloud-backintime-online-beelink --backup
sudo journalctl -fu owncloud-backintime-online.service
```

Stopping the journal display with Ctrl+C does not stop the backup. This unit is
one-off and creates no timer. A live copy may compete for disk bandwidth with
production and mirror jobs; it must not be used concurrently with the old
offline script.

The intended first scope is ownCloud only:

* The production data directory and `apps/owncloud/volume` paths listed above,
  including configuration and writable apps.
* A fresh, verified PostgreSQL dump of `owncloud_stage`, taken from
  `beelink-databases-owncloud-1` while ownCloud is stopped.
* The information needed to reconstruct the matching application and database
  runtime, including the production image identity and configuration. These
  contain credentials and must be protected along with the snapshots.

The final-pass wrapper verifies the mount identities and serializes with the production
lifecycle and mirror locks. It checks ownCloud health, enables maintenance,
stops the web container, validates a PostgreSQL dump and runs Back In Time in
the foreground. Before stopping ownCloud it requires the latest snapshot to
contain the expected data, apps and configuration, and refuses an incomplete
`new_snapshot`. Back In Time reuses unchanged files through hardlinks. It
attempts service recovery even on error and reports recovery
failures. The generated profile has scheduling and automatic deletion disabled;
the wrapper must always be used. Snapshots go to the root-private
`/datenBackup/owncloud-root`, separate from the existing user profile.

The final snapshot still scans the entire tree and may take hours even with
hardlinks. No retention or automatic failure notification is configured.
Install the tracked final-pass script from the checkout on beelink:

```bash
sudo install -o root -g root -m 0755 ~/git/dockerApache/owncloud/backintime-beelink.sh \
  /usr/local/sbin/owncloud-backintime-final-beelink
sudo systemd-run --unit=owncloud-backintime-final --property=RuntimeMaxSec=2h \
  --property=TimeoutStopSec=180s --property=KillMode=control-group \
  /usr/local/sbin/owncloud-backintime-final-beelink --backup
sudo journalctl -fu owncloud-backintime-final.service
```

After two hours systemd stops the unit; cleanup attempts to restore ownCloud.
Recovery can take additional time and is not guaranteed after a forced kill,
power loss or a hung disk. Verify the journal, container and HTTP status after
termination. If the time limit is reached, the final snapshot is not complete.
A full isolated restore rehearsal must succeed before relying on this backup or
enabling a schedule.

If an offline backup is interrupted by power loss or SIGKILL, its cleanup trap
cannot run: inspect and recover ownCloud manually before attempting another
backup. A consistent snapshot needs `owncloud.dump`, image and container metadata under
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
