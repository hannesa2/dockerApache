# ownCloud 10.16.7 staging copy

This stack clones the native ownCloud installation to a separate database and
data directory. It does not alter the native installation and binds the test
web port to `127.0.0.1` only.

The native Latitude instance uses PostgreSQL and stores its files under
`/daten/owncloud/data` (438+ GB). The copied file data goes to
`/media/hannes/T1/owncloud-staging/files` on the ext4 T1 disk. The database,
configuration, writable apps, and dump are kept in sibling directories under
`/media/hannes/T1/owncloud-staging` as well. Before copying, the script verifies
that T1 is mounted as ext4 and checks its free space; it refuses to reuse a
non-empty staging directory. Its output shows the data and database sizes, each
writable app directory, filesystem free space, and estimated requirements
including the dump, restored database, and a 5 GiB safety reserve.
The staging ownCloud service can reach the internet to fetch compatible apps
during upgrade; PostgreSQL and Redis remain on the internal-only network, and
the web port remains bound to loopback. Compose explicitly sets the database
table prefix (`oc_`) to match the restored native database; the image otherwise
generates an empty prefix override from its environment.

## Run on Latitude

Copy this directory to Latitude, then run:

```bash
sudo ./owncloud-staging/clone-native.sh
```

The script requires root because the native ownCloud configuration and data are
protected. It makes an initial data copy while ownCloud is online, then briefly
enables maintenance mode for a final file sync and PostgreSQL dump. The source
is returned to normal operation before the staging database is restored and
upgraded. A trap attempts to restore normal operation if an error occurs during
the snapshot. The helper takes the same lock as the scheduled ownCloud mirror
sync, and exits without copying if that sync is already running.

The helper expects native ownCloud 10.16.6 with PostgreSQL database/user
`owncloud` on localhost and does not support an external object store. It keeps
source database credentials only in a root-only temporary password file and
generates a separate random password for the staging database.
Secrets, database files, and the dump are stored with restricted permissions in
`/media/hannes/T1/owncloud-staging`; the run log is at
`/media/hannes/T1/owncloud-staging/clone.log`. Do not copy this directory into
Git or expose it.

After the copy, create an SSH tunnel from the client:

```bash
sudo docker compose --env-file /media/hannes/T1/owncloud-staging/staging.env \
  -f owncloud-staging/compose.yml up -d owncloud
```

Then, on the client:

```bash
ssh -N -L 8093:127.0.0.1:8093 latitude
```

Open `http://localhost:8093`. Sign in with an existing ownCloud account and
test files, sharing, contacts, calendars, and clients. The staging web service
is left stopped after the upgrade; stop it again after testing with:

```bash
sudo docker compose --env-file /media/hannes/T1/owncloud-staging/staging.env \
  -f owncloud-staging/compose.yml stop owncloud
```

The native instance continues to serve the production URL.

If the database and files were copied but the ownCloud upgrade failed, run
`sudo ./upgrade-staging.sh`. It uses the official image migration wrapper,
explicitly initializes the ownCloud config link for one-off CLI containers,
validates the staged database and backup, and leaves the staging web server
stopped when finished.
The helper disables the legacy `gallery` app in the staging database because it
is not available for ownCloud 10.16.7; the native installation is unchanged.

This only stops/keeps stopped the staging web server. The native ownCloud is
served by the host Apache process, which also serves other sites; the helper
does not stop Apache or the separate evaluation instance.

If the script stops during the initial PostgreSQL startup before any files,
configuration, or database dump have been copied, rerun it with
`sudo ./clone-native.sh --resume`. Resume mode preserves the staging password
and database volume and refuses to continue if it finds copied files, a config,
a dump, or tables already imported into the staging database.

The PostgreSQL dump is retained at
`/media/hannes/T1/owncloud-staging/backup/native-database.dump` as a restricted
rollback artifact. Keep it only as long as needed and remove it securely after
the staging exercise and backup-retention decision.

## Production cutover

After testing the staging copy, `cutover-native.sh` can take a final consistent
file/database snapshot, upgrade only the staging database, and proxy only
`/owncloud/` from the production HTTPS vhost to the staging container. It does
not stop host Apache or other sites. The native ownCloud is put in maintenance
mode during the final snapshot and remains in maintenance after cutover as a
rollback copy; do not route traffic back to it after users have written data to
the new instance without reconciling those writes.

First deploy the updated `compose.yml` and script to Latitude, then run the
read-only checks:

```bash
sudo ./owncloud-staging/cutover-native.sh --preflight
```

Only if preflight succeeds, execute the cutover:

```bash
sudo ./owncloud-staging/cutover-native.sh
```

The script requires root, a healthy staging PostgreSQL database, enough free
space on T1 for the final dump, and a valid host Apache configuration. It
backs up the production vhost and the final native PostgreSQL snapshot on T1.
Before enabling writes on the new instance it verifies that the production
HTTPS route reports installed ownCloud 10.16.7. If an earlier step fails, it
restores the original vhost and disables native maintenance mode. If failure
occurs after the new instance is released from maintenance, it intentionally
does not roll back automatically.
