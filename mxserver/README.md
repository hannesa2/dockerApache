# MXServer standby mirror

The MXServer application is a Java EE 7 WAR deployed on GlassFish 4.1 on
latitude. Its mirror uses Payara 5 (Java EE 8 compatible) with Java 8, an
isolated PostgreSQL 14 database, and a private SMTP sink. The mirror has its
own database and picture files; the daily sync replaces those copies with the
latest latitude snapshot.

## Prepare mac2016

The MXServer checkout must be available at `~/git/MXServer`, next to the
`dockerApache` checkout. Then prepare the persistent storage, private
credentials, container image, and mirror database services:

```bash
cd ~/git/dockerApache
cp apache/.env.example apache/.env  # only if apache/.env does not exist
./mxserver/prepare-mirror.sh
```

The default data directory is `/Volumes/SamsungT5_1T/MXServerMirror`. The
script preserves any existing passwords and generates private ones for new
settings in `apache/.env`. Do not commit that file. `MXSERVER_PAYARA_UID`
must match the macOS account that owns the picture directory.

## Payara 6 migration assessment

The current application is not a drop-in deployment for Payara 6. The sibling
`MXServer` project is a Java EE 7 WAR (`javax.*`) compiled with Java 7 source
and target settings. Its POM pins Java EE 7 APIs, EclipseLink 2.5.1, and
Jersey 2.0; its deployment descriptors also contain GlassFish-specific
security-role mappings and a `file` authentication realm.

Payara 6 is a Jakarta EE runtime, so the `javax.*` application and descriptors
must be migrated or transformed before a meaningful Payara 6 test. The
existing MXServer source and `MXServer/Dockerfile` do not need to be changed
for this investigation: the migration test should build a separate staging
image from the same source, leaving the Payara 5 mirror service untouched.
That staging service must use its own database and mail sink, not the live or
active mirror database. Authentication tests must account for the staged
file-realm users/keyfile.

The first compatibility gate is therefore a separate Payara 6 staging build
and deployment of a Jakarta-transformed WAR. Only after it deploys should API,
database reads/writes, file handling, and file-realm authentication be tested.
The existing Apache `/backend/` routing and active Payara 5 service remain
unchanged during this work.

## Latitude PostgreSQL migration rehearsal

Latitude MXServer now uses Docker PostgreSQL 16 on port 5432. The cutover on
2026-11-16 preserved existing credentials and TLS; the application opened eight
JDBC connections over TLS 1.3 after loading the tracks list. A fresh backup
verified all 19 MXServer table-data entries, and the full mac2016 mirror sync
succeeded. The native cluster is stopped with `start.conf=manual`; its data
remains for recovery only and must not be restarted as a second source.
The GlassFish Docker-readiness gate is installed. A full host reboot has not
been tested.

The following rehearsal instructions describe the **pre-cutover native**
setup and must not be run against the retired native cluster.
`rehearse-latitude-postgres.sh` creates a temporary snapshot and restores
it into a temporary PostgreSQL 16 container bound only to loopback. It
restores role password verifiers and verifies the existing `hannes` password
using `~/.pgpass` over TLS with a temporary certificate. Run as `hannes`:

```bash
cd ~/git/dockerApache
docker pull postgres:14-alpine
bash mxserver/rehearse-latitude-postgres.sh
```

The rehearsal removes its temporary container, volume and sensitive snapshot
and role files on exit. It also tests the intended Docker-source dump against
an isolated PostgreSQL 14 mirror container with no network, comparing row
counts and order-independent content checksums for every public table.
It does not stop or change the native service, switch clients or install a
mirror job. It is not a production migration or a persistent backup. The
eventual cutover must preserve existing TLS and authentication, update the
root-run mac2016 mirror job and verify GlassFish before retiring native
PostgreSQL.

For privileged cutover prerequisites, run directly on Latitude:

```bash
cd ~/git/dockerApache
sudo bash mxserver/preflight-latitude-postgres.sh
```

This checks the running native service, TLS key and certificate, existing
credentials, backup lock, database inventory and available space. It does not
migrate data or change services; do not share passwords or private keys when
reporting its output.

For an additional privileged staging test using the **actual** PostgreSQL TLS
certificate, key and native role verifiers, run:

```bash
sudo bash mxserver/stage-latitude-postgres.sh
```

It restores a temporary snapshot into an isolated PostgreSQL 16 container
bound only to `127.0.0.1:55432`, checks the certificate fingerprint and TLS
login using the existing `hannes` credentials, and tests the Docker mirror
dump. Temporary volumes, secrets and the container are removed on exit.
This is not a cutover or a backup; the native service and its clients keep
running. Do not start it if port 55432 is already in use.

The Latitude database configuration is in `mxserver-postgres/`. It
uses Linux host networking, persistent bind mounts, TLS and SCRAM network
authentication. It must not be started on port 5432 while native PostgreSQL
occupies that port. The bootstrap password initializes an empty container;
after restoring the native roles it is not necessarily the actual database
administrator password. Its optional host-specific private configuration can
be encrypted with `./private/private.sh --crypt mxserver-postgres`.

The prepared availability monitors use the native PostgreSQL check until a
root-owned `/etc/mxserver-postgres-dockerized` marker exists; thereafter they
check container `mxserver-postgres-db-1`. The cutover creates the marker under
the job locks immediately before exposing the Docker database; before that
exposure a failed cutover removes it again. Both monitoring hosts have the
prepared scripts installed; their schedules were not changed.

The cutover exclusively acquires both the PostgreSQL backup job's
`flock` and the MXServer mirror job's directory lock. The reusable
`cutover-locks.sh` helper implements that convention but does not itself stop
services or perform a cutover. A stale mirror lock is an error and must not be
removed without investigation.

`build-freeze-hba.py` prepares a separate HBA file with rules rejecting new
`MXServer` network and non-`postgres` local connections while allowing the
local `postgres` user to dump the database. It never installs or reloads this
file. PostgreSQL HBA reloads **do not disconnect existing sessions**; a
cutover must stop GlassFish, reload and verify the freeze, and then check for
remaining sessions before taking the final dump. All pre-existing active
sessions must be accounted for. The original HBA must be recoverable on failure;
the helper is not a cutover script.

The native PostgreSQL data directory must be retained for recovery. Automatic
rollback to the native copy is only safe before the Docker database can accept
production writes. Once Docker has been exposed to clients, its data may be
newer; stop writers and reconcile or restore that newer state before reverting.
An old native directory must never simply be restarted as the source of truth
after Docker has accepted writes.

The prepared `sync-latitude.sh` selects native PostgreSQL until
`/etc/mxserver-postgres-dockerized` exists, then selects the container
`mxserver-postgres-db-1`, without changing the cron command. It also accepts
explicit `--source=native` and `--source=docker` overrides. The cutover replaces
the installed root job while holding its lock. A successful cutover alone does
not prove that a complete mirror sync or a scheduled backup has succeeded.

## Latitude production cutover

The cutover must be run directly on Latitude as root, not through an
unattended SSH command. It stops GlassFish and temporarily rejects new
MXServer connections. It aborts rather than disconnecting any remaining
sessions. It saves the final database dump, role definitions and original
configuration in a root-only directory on `/daten`, restores into a
loopback-only staging container, compares counts and checksums for ordinary
tables and materialized views across user schemas, and verifies TLS and the
existing password. It then stops the native cluster, changes its `start.conf`
to `manual`, and starts the Docker database on port 5432. The native data
directory is retained. The source and restored schema are additionally checked
by the dump and restore commands, but checksums are not a substitute for an
independently tested backup.

```bash
cd ~/git/dockerApache
sudo bash mxserver/cutover-latitude-postgres.sh --execute
```

The script installs a GlassFish systemd startup gate that waits for the Docker
database to be healthy and queryable before starting GlassFish. It waits up to
five minutes; a timeout leaves GlassFish stopped and requires investigation
and a manual restart. The script does not test a full host reboot.
Before stopping GlassFish it sets `KillMode=control-group`, because the legacy
SysV stop command can report success while leaving the Java server running.
It verifies that the service process group is empty before installing the
database freeze. A failed systemd state with an otherwise running, responding
GlassFish is accepted for this controlled stop. Since stopping an already
failed unit may do nothing, remaining service-group processes receive a
targeted SIGTERM via systemd. The script waits up to two minutes and aborts
without freezing the database if they remain; it does not force a SIGKILL.
Freeze checks recognize both
English and German PostgreSQL rejection messages; the server's `lc_messages`
setting, not the shell locale, controls these messages.

After a successful cutover, encrypt the newly generated host-specific `.env`
and verify the actual backup and mirror jobs separately:

```bash
./private/private.sh --crypt mxserver-postgres
sudo /usr/local/sbin/mxserver-mirror-sync
```

The mirror sync replaces the mac2016 standby data. Check its result and the
installed PostgreSQL backup job's next run (or run that job explicitly) before
considering the migration fully verified.
GlassFish can initialize its JDBC pool only on the first database request.
If the cutover reports no JDBC sessions while the application is deployed,
perform an authenticated, read-only application request and inspect the
container's `pg_stat_activity`. Do not rerun the cutover or restart native
PostgreSQL. A successful standalone JDBC connection proves the driver and
credentials work, but does not prove that the application has used its pool.

### Interrupted cutover and recovery

The script prints the root-only recovery directory. Before Docker is exposed
it attempts to stop staging, restore the original configuration and mirror
job, and restart the native database and GlassFish. If any restoration step
fails, it leaves GlassFish stopped and reports the failed action. After Docker
may have accepted writes it deliberately **does not** restart native
PostgreSQL: the old native data may already be stale. In either of those cases
the mirror directory lock is intentionally retained to prevent a scheduled
mirror overwrite; the backup `flock` is released when the process exits.
Investigate the state before removing the retained lock.

An uncatchable termination or power failure can interrupt either phase and
leave a freeze rule, staging container, job lock, changed boot configuration
or marker behind. Inspect the services, container, marker, logs, recovery
directory and locks before acting; do not blindly remove a lock or start the
native database. If Docker could have accepted writes, preserve its data and
reconcile it before any rollback. Files in the recovery directory include
password verifiers and database contents; keep the directory private.

## Install sync on latitude

The sync reads the live PostgreSQL database `MXServer` as the local `postgres`
account, streams a plain SQL dump into a staging database (so the source and
mirror PostgreSQL client versions do not need matching archive formats),
mirrors `/srv/postgres/data/mxinfo/`, and copies the
GlassFish file-realm keyfile so existing application users can authenticate.
It streams the database dump over SSH and transfers pictures incrementally.
The PostgreSQL and SMTP credentials are newly generated for the mirror; the
production database password and SMTP configuration are not copied.

```bash
cd ~/git/dockerApache
sudo ./mxserver/install-latitude-sync.sh
```

The installer runs the initial sync and installs a daily root cron job at
04:30, after the ownCloud mirror job. Each run sends a success/failure email
through the existing `run-sync-with-email` wrapper. The database dump is
restored into a staging database before replacing the active mirror database;
the mirror app is stopped only for the final database swap and restarts after
the sync.

## URL and routing

Apache terminates TLS with the existing `www16.mxtracks.info` certificate and
proxies `/backend/` to the app's GlassFish context root
`/backend/MXServer/`. After the first successful sync, the script activates
the proxy on Apache's host port 9433 and waits up to five minutes for Payara
to finish startup. During startup it retries connection failures and HTTP
404/502/503/504 responses; Payara may return 404 before the WAR deployment
completes. The FRITZ!Box must forward public TCP
9433 to mac2016 port 9433.

```text
https://www16.mxtracks.info:9433/backend/
```

The HTTP `/backend` REST API supports writes, but writes on this standby are
independent and will be overwritten by the next database/picture sync. Mail
generated by the mirror is captured by the private Mailpit service and is not
delivered to production recipients.

## Operations

```bash
# mac2016
docker compose -f apache/docker-compose.yml --profile mxserver ps
docker compose -f apache/docker-compose.yml --profile mxserver logs mxserver

# latitude
sudo /usr/local/sbin/mxserver-mirror-sync
tail -n 100 /var/log/mxserver-mirror-sync.log
```

GlassFish on latitude runs natively and uses the Docker MXServer database.
The mac2016 mirror uses its own Payara application and PostgreSQL 14 copy;
the daily sync now dumps from the Latitude PostgreSQL container.
