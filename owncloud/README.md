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
