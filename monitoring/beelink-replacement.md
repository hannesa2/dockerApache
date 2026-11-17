# Latitude to beelink replacement

## Status and scope

Production remains assigned to Latitude, with its web services currently
offline for the coordinated cutover. ownCloud is in maintenance mode and its
app container is stopped; native Apache/PHP/GlassFish/WildFly, Docker Apache,
GitLab runner, and GitLab Puma/Sidekiq/SSH are stopped. Source databases and
DNS remain running. Scheduled jobs have not yet been fenced; this is not a
durable single-writer boundary. No router, schedule, monitoring target or
mirror source is switched.

There are **three remaining completion milestones**, not an open-ended series
of preparation tasks:

1. **Finish readiness decisions:** retain the twelve currently-unavailable
   proxy paths at HTTP 503 as accepted; settle public DNS/TLS/IPv6 and
   final fail-closed boot behavior. No guessed backend routing or unplanned
   disk-removal test.
2. **One coordinated cutover:** freeze source writers, reconcile and verify
   final files/databases, then change router forwarding and enforce the
   single-writer boundary.
3. **One post-cutover operations batch:** move/verify backups, mirrors,
   scheduled jobs and monitoring; set production boot policy and encrypt
   destination private configuration.

The active user-cron inventory is now recorded for that operations batch:
Latitude has PostgreSQL backup (19:15), Apache mirror sync (03:05), hourly
health monitoring, and GitLab standby backup (03:15), plus two unrelated Git
upstream checks. mac2016 has hourly health monitoring at minute 10, with its
explicit HOME/PATH, plus one unrelated Git upstream check. beelink currently
has no user crontab jobs. Latitude's root-run MXServer mirror job is separate
and must also be moved/disabled at cutover. No schedule has been changed.

The user explicitly accepted keeping the twelve currently unavailable proxy
paths at HTTP 503 after cutover. The decision does not restore those backends
or change production now.

Read-only network preflight on 2026-11-17: Latitude currently owns TCP 80/443;
beelink has no host listener or published Docker port on either, so there is no
accidental overlap. Beelink has both global and ULA IPv6 plus a default IPv6
route, but the web frontend currently has no IPv6 listener/public reachability
test. Latitude manages and periodically renews the production Let's Encrypt
certificates through its active snap.certbot.renew timer. Keep that renewal
active while Latitude serves public traffic; beelink's copied certificates
are valid for staging, but automatic renewal has not been transferred. Before
the public switch, confirm the actual renewal challenge method and make
beelink's renewal path work with the intended router/DNS configuration.
Transfer renewal ownership only after that succeeds, then disable Latitude's
renewal timer to avoid two hosts managing the same certificates. Router
forwarding/DNS and production certificate renewal are therefore still
required before public traffic can be switched.

The destination is the encrypted Samsung T7 mounted at `/daten` on beelink
(`192.168.178.142`). The filesystem UUID is checked before transfer. Existing
beelink Home Assistant and camera PostgreSQL must be retained until their
individual migrations are explicitly verified.

The agreed scope includes the active repository stacks and their native
dependencies: Apache/PHP websites and Matomo, ownCloud 11, MXServer with its
PostgreSQL database and GlassFish runtime, GitLab/runner, GitHub artifacts,
DNS/DoT and the WildFly-backed proxy endpoints. The stopped Nextcloud stack
must not be reactivated as production just because it exists in the checkout.
Its former `/media/hannes/T5/nextcloud` directory is currently absent on
Latitude; container definitions can be preserved, but that missing dataset
cannot be archived from this source. The existing mac2016 Nextcloud copy
must remain untouched.

Destination architecture: every active service already Docker-based on
mac2016 must also run in Docker on beelink, including Matomo, original
WordPress sites and MXServer. The user explicitly permits GitHub artifacts to
remain native on beelink; its migration is not a priority. References below to "native"
describe the Latitude source, not a requirement to install host PHP/MySQL/Java
on beelink. The explicitly retired Nextcloud instance remains excluded.

## 1. Initial seed

Run directly on **Latitude**, not beelink. Root is needed to read the private
application files, TLS material, native database credentials and schedules.
SSH uses Latitude's existing `hannes` key to reach `hannes@beelink`.
Native MySQL authentication first checks `/etc/mysql/debian.cnf`, then the
existing root client defaults. If neither works, the script requests the
native MySQL root password privately in the terminal before starting the copy.
This is not the Docker MySQL password. It is not written to the migration log
or placed in command-line arguments.

```bash
cd ~/git/dockerApache
sudo bash monitoring/seed-latitude-to-beelink.sh --seed-only
```

Allow several hours: ownCloud alone currently contains about 800 GiB.
The script holds a separate seed lock, creates logical PostgreSQL and MySQL
snapshots, checks the PostgreSQL archives, creates an official GitLab
application backup, and seeds the application file trees. It does not stop
source services or run a mirror sync. There is no automatic source cleanup.

`/daten/latitude-migration` on beelink is private staging, **not an active
application directory**. It contains credentials, password verifiers,
database contents and TLS private keys. Do not publish, commit or make it
world-readable. `rsync --fake-super` stores source ownership and permissions
in extended attributes because the receiver is not root. These files must
be restored with fake-super-aware rsync before a service uses them; do not
point containers straight at this staging tree.

The source retains a private `/daten/latitude-beelink-seed.*` directory with
logs and logical dumps. Successful completion writes `seed-complete` on the
destination. Interrupted copies can be reseeded; a different or unmarked
destination is rejected. Rsync exit 24 is explicitly logged as an inconsistent
live-file seed, never a production-ready snapshot.

If the log reached `Seeding repository code` but failed there (for example
because an older script invoked Git as root), finish only that final step
using the retained private directory:

```bash
sudo bash monitoring/seed-latitude-to-beelink.sh --finish-seed /daten/latitude-beelink-seed.ZJcfWaBL
```

This reuses the existing snapshots and file seed, copies the repository as
listed by Git running as `hannes`, and verifies destination database checksums
before writing `seed-complete`. It does not create new database or GitLab
backups or repeat the large live-file transfer. Earlier failures must use
`--seed-only` instead. Neither mode performs a production switch.

MySQL dumps include system schemas for recovery evidence. They are **not**
instructions to restore MySQL 8.0 system tables into MySQL 8.4. Restore the
user schemas and reconcile accounts using the appropriate server version.
GitLab must use a supported, matching-version backup restore, not copied
live PostgreSQL/Redis data directories.

## 2. Destination preparation and verification (in progress)

`monitoring/restore-beelink-databases.py --restore-isolated` restores logical
snapshots into the separate `beelink-staging` Compose project under
`/daten/beelink-staging`. It checks the T7 UUID and snapshot hashes, restores
PostgreSQL roles/databases, and verifies MySQL application table names against
the dump. MySQL 8.0.46 remains separate from the source-version Apache MySQL
image. Load the actual source Apache MySQL image first; `--apache-mysql-image`
can identify its local image ID when Docker's image store changes manifest IDs
during save/load. Image versions must match the dump headers.

The database network is internal, no database host ports are published, and
MySQL's event scheduler is disabled. Containers deliberately use `restart: no`
in this temporary staging project: they must not restart ahead of the optional
T7 mount. Existing beelink cameras on 5432 and Home Assistant are unchanged.
An interrupted restore is marked and rejected on retry rather than overwritten.
Successful components are reused and checked again on subsequent runs.
Private credentials and Compose configuration must remain inside the protected
staging directory. `database-report.json` records successful database checks,
not application readiness.

After all database restores succeed, run on beelink:

```bash
sudo bash ~/git/dockerApache/monitoring/prepare-beelink-runtime.sh --prepare-isolated
```

The source Java 8/11 runtimes, their system configuration, WildFly configuration
and GlassFish init script must first be transferred separately as
`metadata/native-runtimes.tar.gz`; the initial seed did not include these.
They have been transferred for this migration. They are only unpacked into
the private runtime tree, not installed or started on the host. Absolute
symlinks and service settings still require adaptation before startup.

This atomically **moves**, rather than duplicates, the large live-file seed
to `/daten/beelink-runtime/rootfs`. Rsync then decodes fake-super ownership,
modes, ACLs and xattrs **in place** without another bulk copy. Host configuration
is unpacked privately into `/daten/beelink-runtime/host-configuration`, never
installed over beelink's `/etc`. The helper checks for any remaining fake-super
metadata before recording success. It does not install or start host services.
An incomplete metadata conversion can be retried if no application container
is using the runtime tree.

After this promotion, `/daten/latitude-migration/files-promoted` records the
new live-file location. Do not rerun the source seed against the old `files/`
location; the updated seed script refuses it. Final reconciliation must use
the promoted runtime tree with real ownership instead. Database snapshots and
GitLab archives remain in the original private seed directory.

### Isolated ownCloud and GitLab application checks

`monitoring/start-beelink-apps.py --start-isolated` is the next root-run step
on beelink. The ownCloud 11.1.0, Redis 7 and exact source GitLab images must
already be loaded. The GitLab source image was transferred without changing
its filesystem layers; its local image ID is pinned in the helper.

From mac2016, launch the step independently of the SSH terminal:

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-app-stage \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/start-beelink-apps.py --start-isolated'
```

Follow its output with:

```bash
ssh -t hannes@beelink 'sudo journalctl -fu beelink-app-stage'
```

This creates the separate `beelink-app-test` project with private state under
`/daten/beelink-runtime/apps`. Both applications remain on the existing
internal Docker network with **no published host ports**. Readiness is checked
from inside the containers; these are not yet browser-accessible production
endpoints. OwnCloud uses the restored database with a separate test config,
copied custom apps and disabled cron. The seeded user files must be writable:
ownCloud rejects even CLI startup with a read-only data directory. Writes are
confined to the isolated beelink copy; Latitude remains authoritative and a
quiesced final reconciliation must replace test changes before cutover.
An older read-only test configuration is upgraded only when the container is
stopped and all other configuration matches exactly. The helper
checks the application's status and performs an actual database-backed user
query without logging identities. Readiness fails immediately if a container
exits, rather than waiting for the HTTP timeout.

GitLab checks the backup hash and version, hardlinks the archive on the same
T7 instead of duplicating it, preserves source encryption secrets, and restores
into separate data directories using the exact 19.5.3 image. Sidekiq, outbound
email, Let's Encrypt and usage reporting are disabled; no runner is started.
Application services are stopped during restore. Readiness and secret
decryption are checked before marking completion. A failed application step
stops both test application containers; an interrupted GitLab restore is
explicitly rejected on retry until inspected. The database test containers
and existing beelink services are left unchanged.

These are staging-only settings. TLS, production proxy paths, write access,
background workers and fail-closed boot ordering still need a separate
controlled activation. The applications must not be exposed as production
based only on these initial-copy checks.

### Isolated copied Apache and Java service checks

After ownCloud and GitLab succeed, run the prepared Java/web step:

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-java-web-stage \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/start-beelink-java-web.py \
  --start-isolated \
  --apache-image beelink-source-apache:20261116'
```

Follow `sudo journalctl -fu beelink-java-web-stage` on beelink. The image ID
above names the transferred production `apache-apache` image; filesystem-layer
hashes were checked against Latitude. Its local image ID is
`sha256:c25e359ee9db4db5f1d8c8c969789d03c9342fee41e718b69c38ffa0db55cdfd`.
The helper resolves the tag to its immutable ID before persisting the staging
configuration. Do not substitute an unrelated image.

The separate `beelink-java-web` project publishes no host ports, uses only
the internal staging network, and never starts automatically at boot.
It copies the small GlassFish, WildFly and Docker web-root trees into private
`/daten/beelink-runtime/java-web` working directories. The large MXServer
picture tree is mounted read-only. Source files in `rootfs` are not modified.

Temporary containers provide complete Java 8/11 runtimes using
`eclipse-temurin:8-jdk-jammy` and `eclipse-temurin:11-jdk-jammy`, pinned to their
local image IDs at preparation time. The transferred original runtimes remain
archived: Latitude's Java 11 installation has missing jurisdiction-policy files
under `/etc/java-11-openjdk/security/policy/unlimited`, and a fresh WildFly CLI
there fails with `Couldn't parse jurisdiction policy files`. Do not reuse
that incomplete runtime or alter the running source during staging.
This tests the existing applications without installing obsolete Java
packages into beelink's newer host OS; it is not yet a decision about final
production service installation. GlassFish uses its standard
`glassfish/bin/asadmin` launcher after removing its exact known legacy
`JAVA=/opt/jdk1.8.0_201/bin/java` override in the private working copy.
Both launchers were locally modified on Latitude to hard-code host Java
paths, overriding `AS_JAVA` and failing inside the staging container.
Unknown launcher changes are rejected. An existing exact old launcher configuration
is upgraded only with all Java/web test containers stopped; data is retained.
GlassFish's production JDBC pool is changed
only in its working copy to reach the isolated MXServer database. Copied
WordPress configurations point at the isolated Apache MySQL instance and
disable request-triggered WP-Cron. There are no scheduled syncs or runners.

The original GlassFish admin TLS endpoint cannot negotiate with the modern
Java security defaults. The helper does not re-enable obsolete TLS protocols:
instead, it compiles a temporary JDBC probe in the application container,
reads the isolated pool URL/credentials from its domain configuration, loads
the application's PostgreSQL driver and queries the restored tracks table.
This verifies Java/driver/credentials/data connectivity, **not** the managed
GlassFish pool. The separate authenticated MXServer API check below has now
passed. The old admin TLS endpoint remains unverified; its failure is not
worked around by weakening Java's TLS security.

The MXServer deployment maps `UserRole` directly to the file-realm principal
`user`, and `/rest/*` requires an authenticated role. The API authentication
test succeeded on 2026-11-17 at 13:36:27:

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-mxserver-auth-final \
  --property=TimeoutStartSec=10min \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/check-beelink-mxserver-auth.py \
  --check-isolated'
```

The test changes only the staged GlassFish file realm: it temporarily replaces
the cloned `user` password with a random one, restarts only the isolated
GlassFish container, and requests `/backend/MXServer/rest/version` through
verified frontend TLS. It restores the exact original keyfile, restarts
GlassFish again and confirms the temporary password and anonymous requests
both return 401. The original source realm and credentials are never changed
or used. The keyfile resides inside the root-only `0700` runtime tree (its
original file mode remains unchanged). A private backup and pending marker are
retained if cleanup cannot prove that no concurrent keyfile edit would be
overwritten. The managed staging lifecycle lock prevents overlap with its
controller; Home Assistant, camera PostgreSQL, other app containers and host
ports are unaffected.
The anonymous request and the same temporary credentials after restoration
returned 401; the authenticated disposable principal received HTTP 200 with a
nonempty response. The original keyfile was restored byte-for-byte, and
GlassFish returned to running state. The file-realm helper compiled and
authenticated the staged principal before that API request. This proves an
authenticated end-to-end REST request against the isolated database-backed
MXServer deployment; it does not test the modern-Java-incompatible GlassFish
admin TLS endpoint or public router reachability.

Checks cover MXServer HTTP and the direct JDBC query, WildFly HTTP and the
runtime state of both original deployments, Apache configuration and both
copy-domain vhosts, and database-backed WordPress queries. Failure stops only
this new project's containers. Success writes `java-web-report.json`;
it does not assert that all proxy endpoints or applications are ready.
The native Apache/PHP frontend, native WordPress/Matomo sites, GitHub artifact
service, TLS/proxy integration and final reconciliation are still separate
remaining steps.

### Isolated production Matomo check

The mac2016 Matomo mirror is already Docker-based. Latitude's authoritative
Matomo still uses `/srv/www/vhosts/mxdocs/piwik` and native MySQL `piwik_db`;
the copied Docker web root also contains a Matomo directory, but its old
localhost database setting does not make it an independent Docker deployment.

After Java/web staging completes, start the dedicated test:

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-matomo-stage \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/start-beelink-matomo.py --start-isolated'
```

Follow `sudo journalctl -fu beelink-matomo-stage` on beelink. The helper copies
the authoritative Matomo files, excluding caches, into private
`/daten/beelink-runtime/matomo`, uses the transferred Apache/PHP image, and
connects only to the restored `piwik_db` in the internal staging network.
Native localhost-only MySQL accounts cannot authenticate across containers:
a new random test account is granted privileges only on this restored schema.
Its credentials remain in the private copied Matomo config. Existing source
and restored application accounts are not modified.

No host ports, cron jobs, mirror jobs or outbound network are enabled.
Checks cover Apache configuration, database authentication and restored sites,
Matomo's CLI version command and an HTTP response from `index.php`.
They do not establish browser/UI correctness or final public TLS readiness.
Failure stops only the dedicated Matomo test container.
The copied document-root directories are owned by container `www-data`
(UID/GID 33), with mode 0750; files use 0640 and the credential file 0600.
The private host parent remains root-only. This is reapplied on retry because
the initial root-owned 0700 document root prevented PHP from reading config.
Symlink targets are not traversed or modified.

### Isolated original WordPress sites

`monitoring/start-beelink-wordpress.py` prepares the original
`dev.mxtracks.info` and `www.mxtracks.info` sites, not the previously checked
copy-domain sites. Like mac2016, they use Apache/PHP and MySQL in Docker.
The authoritative files come from `/srv/www/vhosts/{dev.mxtracks,mxdocs}`
in the seed, never from mac2016's mirror or `/srv/www.docker`.

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-wordpress-stage \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/start-beelink-wordpress.py --start-isolated'
```

Follow `sudo journalctl -fu beelink-wordpress-stage` on beelink.
Working copies, Apache vhosts and logs live under private
`/daten/beelink-runtime/wordpress`. The existing version-matched MySQL 8.0
Docker restore supplies `wordpress` and `wordpress_mxtracks`; this deliberately
does not import MySQL 8.0 system tables into mac2016's MySQL 8.4 version.
Each site gets a new random account scoped to its own restored schema.
Original accounts, database URL rows and source files remain unchanged.
Copied `WP_HOME`/`WP_SITEURL` constants keep the original domains and normalize
their scheme to HTTPS: the restored www database still contains HTTP URLs.
The source www `wp-config.php` has a stray `d` before its opening doc comment,
which fails PHP syntax validation. The helper removes that exact known token
only in the private working copy and logs the correction.

The helper excludes Git metadata and Matomo (already staged separately).
It relocates the dev site's absolute Query Monitor symlink within the copied
tree and rejects escaping/missing targets. Container `www-data` owns the
copied files, with directories 0750, files 0640 and `wp-config.php` 0600.
WP-Cron, automatic updates and external WordPress HTTP requests are disabled.
The internal-only HTTP vhosts supply the HTTPS proxy header expected by the
original WordPress URLs, avoiding redirects to Latitude during HTTP probes.
No host ports, source syncs, production schedules or boot activation are added.

Checks require Apache syntax, actual WordPress credentials and nonempty posts,
the original home/siteurl values, and HTTP 200 WordPress HTML from both the
homepage and login page without following redirects. Success writes
`wordpress-report.json`; failure stops only this project's Apache.
Browser rendering, public TLS, remaining proxy routes, final consistent
database/file synchronization and production cutover are separate steps.

The original www site still has WordPress 5.0.25, incompatible with PHP 8.3
(curly-brace string offsets). The user approved upgrading **only the isolated
www copy** to mac2016's core version 6.9.11:

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-wordpress-upgrade \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/start-beelink-wordpress.py \
  --start-isolated --upgrade-www-core'
```

The downloader has internet access but mounts only an empty core download
directory, never application files or credentials. WP-CLI downloads pinned
English core files and verifies official checksums. Before replacement, the
test Apache is stopped and the entire www WordPress tree and restored
`wordpress_mxtracks` schema are saved privately in
`/daten/beelink-runtime/wordpress/www-before-6.9.11`. Core replacement preserves
`wp-content`, configuration and uploads; only old core directories are
reconciled. The restored www database is upgraded with plugins/themes skipped.
Verification additionally loads the original active WP-Piwik 1.0.19 and
Twenty Fourteen 2.3 without disabling them. Neither the dev site's core nor
Latitude/mac2016 is upgraded. Final fresh restores must repeat the www
database upgrade against the final isolated copy before production.

### Isolated TLS frontend and proxy integration

The next step adds an internal-only Apache TLS reverse proxy, still without
host ports, automatic restart, router changes or certificate renewal jobs:

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-frontend-stage \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/start-beelink-frontend.py --start-isolated'
```

Follow `sudo journalctl -fu beelink-frontend-stage` on beelink.
The helper reads the archived Let's Encrypt tree read-only, checks original
hostnames, matching private/public keys and at least seven days of remaining
validity, and configures the original www/dev/git vhosts.
Original WordPress routes to its dedicated test Apache container.
The www vhost routes `/piwik/`, `/owncloud/` and `/backend/` to the isolated
Matomo, ownCloud and GlassFish containers, with longer-term public HTTPS URLs
but no connection to Latitude. GitLab is proxied including its own assets.

The copied ownCloud config is privately backed up, then adjusted for the
original HTTPS `/owncloud` subpath and the exact test proxy IP (not the entire
Docker network). The copied Matomo config is also backed up and the original
www hostname added to its trusted hosts without changing credentials.
These changes affect only beelink test copies. Matomo's standalone preparation
helper resets trusted hosts; rerun the frontend helper after such a reset.

Verification uses `curl --resolve` **without** `--insecure` or redirects:
actual certificate chain/hostname validation, both original WordPress
pages, Matomo, ownCloud status/login, GitLab sign-in, MXServer public page and
its expected HTTP 401 authentication challenge. A separate disposable-account
WebDAV test has verified ownCloud authentication and file operations. All HTTP
vhosts redirect to HTTPS. A successful run saves `frontend-report.json`.
Authenticated MXServer API, browser rendering, public reachability, certificate
renewal and final boot ordering remain separate requirements.

The frontend deliberately reports explicit HTTP 503 for unconnected routes,
rather than accidentally serving WordPress or routing back into production.
This includes `/github` (native migration allowed but not yet performed),
LAN camera routes and Home Assistant (existing beelink service untouched;
proxy trust and dedicated connectivity still needed). The source baseline
on 2026-11-17 already returns 503 for `/surveil`, `/nexus`, `/temperatur`
and 502 for `/pi-backend`; these are recorded pre-existing outages, not working
backends to silently discard. They still require a conscious cutover decision.
The internal frontend is not production-ready while these routes remain open.

At 13:15 on 2026-11-17, a strict-certificate probe from the isolated network
confirmed that `/surveil`, `/nexus`, `/temperatur`, `/pi-backend`, all seven
LAN-camera paths (`/pi3-live`, `/door-live`, `/easy-live`, `/fly-live`,
`/easy-gyro`, `/easy-gyroAll`, `/easy-gyroComp`) and `dev:/pi` return explicit
503. On Latitude, the matching local listeners on 5001, 8077 and 8082 were
closed; only the separate GlassFish 8081 listener among those routes was
active. `pi3`, `door`, `easy` and `fly` did not resolve on either Latitude or
beelink. The copied camera WAR is not a replacement: its local Motion paths
are absent, and it does not serve the Pi's 8081/8082 backends. Therefore these
12 routes are intentionally not redirected to an unverified service. Their
original devices/services must be restored or their retirement explicitly
accepted before a production frontend switch.

#### Existing beelink Home Assistant

The internal bridge gateway can reach beelink's host-networked Home Assistant
on port 8123. Its current trust list contains only loopback, so forwarded
requests correctly fail HTTP 400. Do not remove forwarded headers or trust the
entire Docker subnet to bypass that check.

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-homeassistant-proxy \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/start-beelink-frontend.py \
  --start-isolated --connect-homeassistant'
```

This step **briefly restarts the existing Home Assistant container** when its
configuration changes. It pins the frontend's current IP in Compose, backs up
the previous frontend definition/vhosts and Home Assistant configuration in
the private frontend directory, and adds only that exact IP to the existing
inline trust list. Unexpected trust/config/mount layouts are rejected.
The existing Home Assistant image, data and Docker definition are not replaced.
Its `!include` directives and other YAML content are preserved.

Home Assistant's own `check_config` must succeed before restart. A failed
validation restores the original file without restarting. Retries with
unchanged configuration do not restart Home Assistant. The frontend remains
internal-only with no published ports; its internal gateway is used directly,
without an external Docker network or routing back to Latitude.

The full existing TLS/proxy checks are repeated. Home Assistant must return
HTTP 200 through HTTPS, and a separate verified-TLS WebSocket probe must
complete HTTP 101, validate `Sec-WebSocket-Accept`, and receive Home Assistant's
`auth_required` frame. This tests live WebSocket forwarding, not merely static
HTML. Authentication and public/router reachability remain separate checks.
Rerun the same command after any deliberate frontend-network/IP replacement;
do not silently broaden proxy trust.

#### Retired copy domains

The user no longer needs `devcopy.mxtracks.info` or `wwwcopy.mxtracks.info`.
Rerunning `start-beelink-frontend.py --start-isolated` recognizes and backs up
the exact previous generated vhosts, removes those two domains and their
certificate requirements, validates Apache and reloads the frontend.
Unknown/retired Host headers are denied, not served by the first original
vhost. The checks explicitly require HTTP 403 for retired Host headers.

The helper stops only `beelink-java-web`'s old copy-site `apache` service and
sets `java-web/copy-sites-retired`. Subsequent Java/web checks start only
GlassFish and WildFly and never reactivate that Apache. Copied files and the
old Compose definition remain privately retained for recovery, not serving.
Original WordPress, Matomo, Java applications, Latitude and mac2016 are not
removed or synchronized by this cleanup.

#### GitHub artifact downloads

The existing mac2016 `local/github-artifacts:server-relocation` image has
been transferred to beelink; its root filesystem layer hashes match.
The user allows a native implementation, but reusing this already working
container avoids installing Python dependencies on the destination host.
The original `/daten/githubArtifactsPy` download directory was missing from
the initial file seed. It was separately copied into private
`/daten/latitude-migration/github-artifacts-extra/files` (3,823 files,
365,548,190 bytes on 2026-11-17). This is another live seed, not a final sync.

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-artifacts-stage \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/start-beelink-artifacts.py --start-isolated'
```

The helper copies those files to `/daten/beelink-runtime/artifacts`, adapts
only the file directory in the copied original credentials, and runs as
UID/GID 10001 on the internal network with no published ports. The root
filesystem, configuration and artifact files are read-only; only temporary
Gunicorn files use tmpfs. There are no GitHub API calls, polling or sync jobs.
Uploads/updates/deletes remain disabled by the read-only mount until a
separate production step; no test writes use source or mirror files.

The exact current frontend configuration is backed up and `/github/` is
connected ahead of WordPress with the working mac2016 prefix headers.
Checks cover existing credentials against the read-only hostname endpoint,
the anonymous authentication challenge, prefix-aware HTML/version through
verified HTTPS, and an original file download whose SHA256 must match.
The full existing TLS/Home Assistant checks run again without restarting
unchanged Home Assistant. Success saves `artifacts-report.json`.
The original credential config remains private and credentials are never
passed as process arguments or printed. Final reconciliation must include
the artifact directory explicitly; the original seed helper did not.

### Mount-bound staging controller (no automatic boot activation yet)

`monitoring/control-beelink-staging.py` brings the already prepared isolated
stacks under one systemd unit without restoring databases, upgrading apps or
activating production. Before startup it requires the exact ext4 T7 UUID,
private root-owned Compose files, existing bind sources under `/daten`,
`create_host_path: false`, internal-only networking, no published ports and
Docker `restart: no` in both configuration and actual containers.
The retired copy Apache, existing camera PostgreSQL and Home Assistant are
not managed by this controller.

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-install-staging-control \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/control-beelink-staging.py \
  --install-and-start'
```

This installs root-owned snapshots of the controller and its frontend/artifact
helpers under `/usr/local/libexec/beelink-staging`, not a boot-time root command
in a user-writable Git checkout. It validates and installs
the initially user-owned database Compose definition as a private root-owned
`/daten/beelink-runtime/database-compose.json`, retaining the original database
project name and absolute bind paths without changing the existing file.
An existing differing snapshot is rejected rather than overwritten.
It then installs
`beelink-staging.service`, reloads systemd and starts the unit.
**It does not enable boot startup.** Existing running stacks are reconciled
without pulling/building images; databases must be ready before apps start.
The artifact helper repeats the full current TLS/WebSocket and download
checks without restarting unchanged Home Assistant.

The unit orders after and binds to `daten.mount`: mount disappearance stops
the managed migration stacks. The stop controller uses container labels and
explicit service lists and does not read `/daten`, so it works after unmount.
No Docker auto-restart policy bypasses the mount guard. On an app-start/check
failure only managed migration stacks are stopped and the unit fails visibly.

Inspect with `sudo systemctl status beelink-staging --no-pager` and
`sudo journalctl -u beelink-staging --no-pager -n 60`. A read-only preflight is
available as `sudo python3 /usr/local/libexec/beelink-staging/control-beelink-staging.py --check`.
Code updates in Git do not change installed root snapshots: rerun the installer
to refresh them deliberately. Restart/reboot and disk-absent boot tests remain
separate controlled steps. Do not enable this staging unit as a substitute
for the final single-writer synchronization/production configuration.

#### Controlled cold restart of migration stacks

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-staging-restart-test \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/control-beelink-staging.py \
  --install-and-restart-test'
```

This refreshes installed root-owned helpers, records container identities and
start times, then actually stops and starts the managed migration stacks via
systemd. It does not reboot beelink, unmount the T7 or restart Home Assistant/
the existing camera database. It may take several minutes while GitLab starts.
Database readiness precedes applications; GitLab readiness, ownCloud,
GlassFish and WildFly HTTP startup are awaited before the full existing
TLS/WebSocket/artifact-download checks run.

The test requires every managed container's start time to change while both
protected containers retain their exact identity/start time; retired Apache
must remain stopped. Success saves private `staging-restart-report.json`.
Inspect the test and service journals directly. Boot remains disabled.
This is a real container cold restart test, **not** evidence of successful
host reboot or operation with an absent/unplugged disk.

Restore isolated copies under `/daten`, resolve all absolute path references,
restore credentials and certificates, and disable outgoing mail, scheduled
application writes, GitLab runners and external integrations during staging.
No standby may overwrite mac2016 while Latitude is authoritative.

The beelink camera container currently owns port 5432, while Latitude cameras
use 5433 and MXServer uses 5432. Preserve destination camera data before
reassigning ports. DNS answers must not redirect clients to an unverified
destination. Keep Home Assistant intact.

### DNS and DNS-over-TLS staging

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-dns-stage \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/start-beelink-dns.py --start-isolated'
```

This stages CoreDNS 1.11.3 (the transferred source image) under private
`/daten/beelink-runtime/dns`, without host ports or host networking.
It replaces the combination of dnsmasq plus TLS wrapper only in this test:
original www/dev/git/Home Assistant names resolve to beelink IPv4
192.168.178.142 and its currently present ULA IPv6. Mirror www16/dev16
retain 192.168.178.70 and explicit empty AAAA responses.
Retired copy domains and the retired Nextcloud application get no local
override. The **DoT certificate hostname** remains nextcloud.mxtracks.info
for compatibility with existing Private DNS clients; that does not start
Nextcloud or redirect its HTTP service.

Unlike the app network, this dedicated DNS-only bridge permits outgoing DNS
queries to the existing Fritz!Box resolver 192.168.178.1 for other names.
It has no attachment to the app network and no published port 53/853.
CoreDNS runs as root as on the source to read the private TLS key, mounted
read-only; the root filesystem is read-only too. No package install is needed.

Checks exercise UDP53, TCP53 and TLS853 with real certificate-chain/hostname
verification, all overridden IPv4/IPv6 answers, mirror AAAA suppression and
upstream example.org resolution. Success writes `dns-report.json`.
The helper checks the expected ULA is actually assigned before starting.
This tests returned AAAA records, not external IPv6 reachability.
The DNS test has `restart: no`. After successful DNS staging, refresh its
mount-bound controller integration with:

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-staging-add-dns \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/control-beelink-staging.py \
  --install-and-restart-test'
```

This repeats the controlled restart including DNS, all app TLS checks and
UDP/TCP/DoT queries. The controller explicitly permits only this dedicated
DNS bridge as an exception to internal app networking, with read-only mounts
and no ports; it rejects other containers attached to that bridge.
No boot activation or LAN/router/client DNS changes are made.
Public DNS binding, client settings, renewal and final production lifecycle
integration remain cutover steps.

The controller refresh and actual cold restart including DNS succeeded on
2026-11-17 at 11:13:56 (beelink local time). All managed containers restarted;
Home Assistant and the existing camera PostgreSQL retained their identities
and start times. Verified TLS, artifact download and UDP/TCP/DoT queries passed.
The installed staging unit remains disabled for boot.

### Authenticated ownCloud WebDAV check

```bash
ssh -t hannes@beelink 'sudo systemd-run --unit=beelink-owncloud-webdav \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/check-beelink-owncloud-webdav.py \
  --check-isolated'
```

The isolated functional test succeeded on 2026-11-17 at 13:17:28. The helper
checked the T7 UUID, all staging isolation policies and the exact ownCloud
staging database/data path before creating a randomly named disposable user.
It uses no existing user's password or files, and does not start/restart apps.
The test holds the staging lifecycle lock, so it cannot overlap a managed
restart. Its client runs inside the existing isolated artifact container,
reaching the frontend IP directly with real TLS hostname/chain validation.

The check confirmed anonymous 401, then authenticated directory creation,
binary upload, byte-identical download, PROPFIND listing/size, directory
deletion and a subsequent 404 over verified HTTPS. Generated credentials
travel over stdin only.
The disposable account and all its data are removed even if a probe fails;
success is recorded only after checking that both have disappeared.
`owncloud-webdav-report.json` records results, without credentials.
An interrupted or failed cleanup leaves private `owncloud-webdav-pending.json`
with only the generated username. Inspect and remove that exact test account
using ownCloud's `occ user:delete`, confirm its data is absent, then remove
that marker before retrying; never delete existing users or broad data paths.
The disposable account and all test data were confirmed absent after cleanup;
the private `owncloud-webdav-report.json` records results without credentials.
This tests real WebDAV storage/authentication but not an existing user's
password, external storage or desktop/mobile client compatibility.

### Java camera dependency findings

The restored WildFly `camera-rs` deployment answered its harmless
`/camera-rs/rest/json/beelink-stage` request with the expected JSON greeting
on 2026-11-17. Its deployed classes use local Motion paths
`/etc/motion/motion.conf` and `/home/pi/motion`, absent in this container;
they do not establish a PostgreSQL connection. Camera data endpoints and
reload/restart/delete actions were not invoked. A running WAR is not evidence
that a local camera installation works.

Latitude's effective `/pi-backend` proxy targets **pi3:8082/camera-rs/rest**,
not the Latitude/beelink WildFly. The `/pi` and camera live/gyro routes
likewise target separate LAN camera devices. Do not wire these routes to the
restored local WildFly or claim a missing PostgreSQL datasource is their
cause. The kitchensink deployment is a separate sample with its own H2
datasource, not the camera PostgreSQL service. The existing camera database
and its consumers remain unchanged.

Persisted services require an actual ext4 mount check for `/daten`, not just
`mountpoint`: systemd's autofs layer can exist without the disk being mounted.
Boot ordering must fail closed instead of creating empty service directories
on the system drive when the T7 is absent.

## 3. Production switch (requires a separate controlled step)

### GitLab final reconciliation

The existing frozen backup is
`beelink_cutover_frozen_20261117T1307Z_gitlab_backup.tar`, SHA256
`3a80e503862817c1b526be58db93745f94d962061ff83cb4bee4ce49c1cb7362`.
GitLab recorded completion at 13:37:42 UTC on 2026-11-17. Its source archive
was readable and Puma, Sidekiq and SSH remained down. Do not create another
backup merely to repeat this check.

The completed archive and a separate private archive of `gitlab.rb` and
`gitlab-secrets.json` are transferred to
`/daten/latitude-migration/cutover-20261117` on beelink. Only the completed
filename, with a matching `SHA256SUMS`, is ready to use; `.partial` is not.
The source configuration is retained privately, not installed over staging.

After transfer, run directly on beelink:

```bash
sudo systemd-run --unit=beelink-gitlab-final-reconcile \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/reconcile-beelink-gitlab.py \
  --backup /daten/latitude-migration/cutover-20261117/beelink_cutover_frozen_20261117T1307Z_gitlab_backup.tar \
  --sha256 3a80e503862817c1b526be58db93745f94d962061ff83cb4bee4ce49c1cb7362
```

The helper takes the existing staging lifecycle lock, checks the mount and
isolation, verifies the archive checksum/version and matches source encryption
secrets against the running copy. It hardlinks the existing archive rather than
creating or copying another backup. Only isolated GitLab is restored; its
staging configuration, disabled Sidekiq/email and certificate policy are
preserved. Services are stopped individually: `gitlab-ctl stop puma sidekiq
sshd` only stopped Puma and is not a valid way to stop all three.

Success requires readiness, a secrets check and unchanged protected service
identities; it writes private `apps/gitlab-reconciliation-report.json`.
Inspect `sudo journalctl -u beelink-gitlab-final-reconcile --no-pager -n 40`.
On failure, access stays stopped and a pending marker requires inspection
before retrying. This does not reconcile other databases/files, activate
production, change routing or transfer certificate renewal.

GitLab final reconciliation completed successfully on beelink on 2026-11-17
at 15:08:01 local time, including readiness, encryption-secret validation and
unchanged protected services. Do not repeat its backup or restore.

### Remaining application database snapshots

Run directly and interactively on **Latitude**:

```bash
cd ~/git/dockerApache
sudo bash monitoring/collect-latitude-cutover-databases.sh --collect-frozen
```

This collects fresh logical ownCloud/MXServer PostgreSQL and native/Docker
MySQL snapshots after checking the stopped web writers. It refuses active
MySQL scheduled events, validates archive readability, rechecks the writer
freeze and transfers each run into a new private subdirectory of beelink's
`/daten/latitude-migration/cutover-20261117`, verifying SHA256 there.
Native MySQL credentials, if needed, are requested privately in the terminal.
It neither creates a GitLab backup nor restores anything.

The reported `beelink-cutover-databases.XXXXXXXX` directory identifies this
collection; `collection-complete` means only these snapshots were transferred.
Files, camera databases and scheduled writers are not reconciled by this
step, and no global single-writer guarantee is claimed. Preserve both copies
until restore and verification finish. Subsequent reconciliation must preserve
beelink's WordPress 6.9.11 core and reapply its schema upgrade after restoring
the source www database; never blindly overwrite that core with Latitude's.

The collection `beelink-cutover-databases.4I5W0deI` completed and all four
transferred checksums were independently verified on beelink.
Run its import directly on **beelink**:

```bash
sudo systemd-run --unit=beelink-databases-final-reconcile \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/reconcile-beelink-databases.py \
  --snapshots /daten/latitude-migration/cutover-20261117/beelink-cutover-databases.4I5W0deI/databases
```

The importer holds the staging lifecycle lock, checks the mount/isolation and
snapshot manifest, and stops only the affected isolated ownCloud, Java,
WordPress and Matomo apps. It replaces the existing isolated application
schemas, **not MySQL system schemas or users**, so beelink's schema-scoped
application grants remain intact. PostgreSQL roles are retained while the
ownCloud and MXServer databases are recreated from their final dumps.
Table inventories must match the snapshots. WordPress's existing 6.9.11 core
is untouched; its www schema upgrade is reapplied before WordPress/Matomo,
ownCloud database access and Java startup checks.

GitLab, Home Assistant and the existing camera PostgreSQL must keep their
identities/start times. The staged camera databases are also not imported.
Success writes private `database-reconciliation/report.json`; a failure
retains `pending.json` and stops affected apps, with no automatic retry.
Inspect with:

```bash
sudo journalctl -fu beelink-databases-final-reconcile
```

The final success line is `Final application databases restored and checked`.
This is database reconciliation only: file/camera reconciliation, scheduled
writer fencing and public activation remain outstanding. Router, certificate
renewal and source services are not changed by this import.

The final application database import completed on 2026-11-17 at 15:35:57
local time. WordPress www was upgraded from schema 43764 to 60717 against
its retained 6.9.11 core; WordPress HTTP, Matomo, ownCloud and Java readiness
checks passed. Do not compare database sizes while imports are still running.

### Final application files

Run directly on **Latitude**:

```bash
cd ~/git/dockerApache
sudo bash monitoring/reconcile-latitude-files.sh --sync-frozen
```

The script requires the source web writers to remain stopped, stops the
remaining `githubArtifactsPy.service` source writer and the affected isolated
destination apps/frontend. It verifies the T7 UUID, actual existing container
mounts and the retained WordPress core. A networkless disposable container
holds the destination staging lifecycle lock for the entire transfer; do not
run other migration helpers concurrently.

Incremental rsync reconciles ownCloud files/apps, MXServer's `mxinfo` files,
original WordPress sites, Matomo and artifact downloads directly into their
existing destination trees. Each short-lived receiver is based on the already
loaded Apache image, has no ports/network or automatic restart and only one
target data tree mounted. No additional image/dependency install is required.
It uses normal rsync temporary-file replacement and delete-delay, not
`--inplace`, a second bulk copy or a tar of the 800 GiB ownCloud tree.

Destination WordPress configurations, the upgraded www core and Matomo
configuration/tmp are excluded. The www site's `wp-content` is synchronized
separately. Artifact ownership/modes remain 10001:10001, directories 0750 and
files 0640. A second dry-run must show no size/mtime/metadata differences;
this is not a full content-hash pass over all files.

Private `files-sync-pending.json` blocks accidental overlapping/repeated runs;
on successful transfer it becomes `files-sync-complete.json`. Failure leaves
affected destination apps stopped and logs under the exact reported
`/daten/beelink-cutover-files.XXXXXXXX` source directory. Inspect before
removing a pending marker or retrying. GitLab, source databases, cameras,
Home Assistant, router and certificate renewal are not modified.

After transfer, run directly on **beelink**:

Prepare this command now, but do not execute or queue it until the Latitude
sync exits successfully. A completed transfer marker alone is insufficient
while its receiver or lifecycle-lock container is still running. The checker
rejects either active worker and requires the affected apps to be stopped
before changing permissions or starting checks. No timer or automatic
post-sync execution is installed.

```bash
sudo systemd-run --unit=beelink-files-final-check \
  /usr/bin/python3 /home/hannes/git/dockerApache/monitoring/check-beelink-reconciled-files.py \
  --check-isolated
sudo journalctl -fu beelink-files-final-check
```

This requires completed database/file reconciliation, repairs copied
WordPress/Matomo permissions and relocates copied website symlinks. It then
repeats application checks, verified frontend TLS/WebSocket and artifact
download checks. GitLab, Home Assistant and the existing camera PostgreSQL
must keep their identities/start times. The final report is private
`/daten/beelink-runtime/files-reconciliation-report.json`; failure leaves
the frontend stopped. Source services remain offline, camera reconciliation
and public activation remain separate, and no claim of a durable global
single-writer boundary is made while source scheduled jobs are unfenced.

### Camera decision and certificate handover

The user accepted retaining existing camera data without a new import when
already present. Exact read-only counts on 2026-11-17 found `public.motion`
in each of `camDoor`, `camHinten` and `camPi3` on both Latitude and beelink's
existing camera PostgreSQL, with **zero rows on both hosts**. No additional
camera import is needed for these tables, and no existing camera service was
changed. Earlier database/file reports' camera-pending flags predate this
decision; they are not evidence of missing rows.

Final application file/TLS verification completed at 15:57:23 local time.
The next step concerns public activation and certificate renewal, not another
database restore or file sync.

Latitude's actual renewal method is **standalone HTTP-01**. Its pre-hook
stops native Apache and its post-hook **unconditionally starts native Apache**.
Keeping the timer enabled without fencing that restart would break the source
freeze. The next scheduled timer was 18:24 local time at the latest inventory.
Prepare the handover directly on **Latitude**:

```bash
cd ~/git/dockerApache
sudo bash monitoring/prepare-latitude-certificate-cutover.sh --prepare-frozen
```

This requires native/Docker Apache already stopped and the renewal timer
active, refuses a concurrently running renewal, and applies a **runtime-only
mask to native Apache** so the existing post-hook cannot reactivate it. It
does not stop/disable the timer, modify hooks or run a renewal. The runtime
mask is not a permanent source fence: reboot removes it. Only before beelink
accepts production writes, an explicitly chosen source rollback can remove it
with `sudo systemctl unmask --runtime apache2.service`; do not restart source
writers casually after production moves.

The helper transfers the five required web/DoT lineages, their renewal
configuration and ACME account state into a new private
`beelink-cutover-certificates.XXXXXXXX` subdirectory of the cutover transfer
directory, checking certificates have at least seven days remaining and
verifying SHA256 on beelink. Private keys/account state must not be printed or
committed. Existing Latitude deploy hooks, including mac2016 deployment,
are **not** installed on beelink. Source remains the renewal owner and the
destination's live certificate mounts remain unchanged during this transfer.
Standalone renewal on beelink still needs the public HTTP-01 route, controlled
frontend stop/start and certificate reload for frontend/DNS. A successful
transfer is not evidence that destination renewal works. Public IPv4/IPv6,
ports, DNS and renewal dry-run remain required for the handover.

The private transfer `beelink-cutover-certificates.mdiPpoEJ` completed at
16:02:52 local time. Source Apache is runtime-masked, Latitude renewal stays
active, and all five certificates have more than seven days remaining.

Beelink currently has no Certbot executable. Prepare its dedicated Certbot
state directly on **beelink**, suppressing the distribution's default renewal
timer before package installation:

```bash
sudo systemctl mask certbot.timer
sudo apt-get update && sudo apt-get install --no-install-recommends certbot
sudo python3 ~/git/dockerApache/monitoring/prepare-beelink-certificates.py \
  --source /daten/latitude-migration/cutover-20261117/beelink-cutover-certificates.mdiPpoEJ
```

The default timer remains persistently masked, including after reboot.
No custom renewal timer is enabled yet.
The helper verifies transfer checksums, rejects unexpected archive paths,
links and source hooks, imports only the five required lineages/accounts under
root-private `/daten/beelink-runtime/certificates`, and rewrites absolute
renewal paths to that location. `certbot certificates` checks local inventory
only; it does not request/renew certificates or prove HTTP-01 reachability.
Failure leaves the private directory for inspection rather than silently
retrying extraction. Existing frontend/DNS certificate mounts are unchanged.
Do not invoke bare `certbot renew` or enable its default timer as a shortcut.

The initial preparation stopped before extraction because the transferred
ACME account tree has two legacy v02-to-v01 absolute directory aliases.
The importer now accepts only those exact known account aliases, requires
their destination directories in the archive and rewrites them to relative
links within the private configuration tree. Other absolute links and archive
descendants of symlinks remain rejected. The actual 165-member transferred
archive passes validation. Certbot is already installed; rerun only the
Python preparation command, not the package installation.

### LAN web port publication before router changes

After private Certbot preparation succeeds, run on **beelink**:

```bash
sudo python3 ~/git/dockerApache/monitoring/publish-beelink-web.py --publish-lan
```

This installs and starts only two TCP socket proxies on IPv4/IPv6 ports
80/443, pointing at the verified pinned frontend address. Existing Compose
files and their no-published-port policies remain unchanged. The sockets and
workers bind to the staging service and T7 mount; mount/staging loss closes
them. They are not enabled for boot and are not automatically restarted
after a staging restart. Existing units or occupied ports cause a visible
failure instead of being overwritten. A failed check closes both sockets.

The helper checks all four hostnames' HTTP redirects and verified HTTPS
responses through the LAN IPv4 and IPv6 loopback. This is not an external
IPv6/router test. TCP forwarding does not preserve the original client IP,
so frontend access logs see the forwarding host rather than the remote
client. No untrusted forwarded-IP header is promoted to a trusted client IP.
LAN clients can write once these sockets are active; source web writers must
remain fenced. This is deliberate exposure, not isolated-only operation,
even though the internal Compose network remains isolated.

Only after these checks pass should the router's existing **TCP 80 and 443**
IPv4 forwards be moved from Latitude (`192.168.178.129`) to beelink
(`192.168.178.142`), and the corresponding IPv6 firewall/DDNS destination
updated deliberately. Do not forward database ports, DNS port 53 or unrelated
services. GitLab SSH 40022 and DNS/DoT 853 are not published by this helper.
Keep existing router rules recorded for rollback. External HTTP-01 renewal
testing and transfer of timer ownership still follow the router change;
do not disable Latitude renewal beforehand.

Before changing router forwarding, freeze all source writers and perform
final incremental file reconciliation plus fresh database backups/restores.
An initial live seed cannot safely become production after hours of new
source writes. Verify all sites, APIs, TLS, files, authentication and service
startup on beelink using local DNS overrides.

Only then change router forwarding. Preserve a clear single-writer boundary:
after beelink accepts writes, Latitude's old databases are stale and must not
be restarted or resynchronized over the new source.

### Private certificate renewal on beelink

The five imported production/DoT lineages are prepared for independent renewal:

```bash
# Directly on beelink, with interactive sudo:
sudo python3 ~/git/dockerApache/monitoring/renew-beelink-certificates.py --test-and-enable
```

Successfully executed on November 17 at 17:09:33 CET: the ACME dry-run and all
five served-certificate checks passed. The private timer is enabled and active;
its first scheduled check is November 18 at 00:38:28 CET. Both public web sockets
were confirmed active afterward. The unrelated `CPUAccounting` warnings from
the distribution's XFS units did not prevent installation.

The command first
checks the private Certbot configuration and both read-only certificate mounts.
It then temporarily stops only the public port-80 socket/worker, runs a real
Let's Encrypt staging `renew --dry-run` against all five lineages, and restores
HTTP forwarding even on failure. HTTPS stays online. Public DNS and forwarding
must let the ACME servers reach beelink; the router AAAA remains a possible
validation blocker. Failure never enables a new timer.

After a successful dry-run, the helper validates certificate hostnames, expiry
and key matches, copies immutable certificate generations into the existing
mounted certificate tree, and switches its live links. Apache is gracefully
reloaded; only CoreDNS is restarted to load its TLS files. Verified TLS
connections compare the actually served leaf fingerprints for all five names
against the private Certbot state. Failed deployment restores the old links.
Unchanged subsequent renewals verify certificates without restarting CoreDNS.

Only after those checks does it install the root-owned helper and
`beelink-certificates.service/.timer`. The persistent timer checks twice daily,
with randomized delay. The service uses the same lifecycle lock as staging,
checks the exact encrypted T7 UUID and active managed stacks, and cannot start
the stacks itself. `ExecStopPost` restores a paused HTTP socket after process
termination, but will not reopen it after T7/staging loss. The distro
`certbot.timer` remains masked. Inspect actual runs with:

```bash
systemctl list-timers beelink-certificates.timer
sudo journalctl -u beelink-certificates.service --no-pager
```

If an interrupted interactive execution leaves a pause marker, recover with
the same helper's `--restore-http` mode after ensuring T7 and staging are active.

**Source ownership is not yet handed over.** Latitude's
`snap.certbot.renew.timer` was still active, next due 18:24 on November 17.
It also owns `dev16`, `www16`, `git16` and retired copy-domain lineages, which
are not in the five-lineage import. Do not disable the entire source timer
and claim mirror renewal has been migrated. Transfer the mirror lineages and
their restricted deployment hook separately before retiring source renewal.
The mirror's current certificates are valid; local `/etc/hosts` misrouting
was responsible for the reported browser certificate errors.

#### Per-run status mail and mirror certificate handover

The updated renewal helper sends a SUCCESS or FAILED mail for each execution
(including guard/ACME/deployment failures) to `hannes.software@gmx.at`.
Mail submission failure is an explicit job failure, not a successful fallback.
Recovery-only `--restore-http` executions do not send duplicate status mails.
A test notification from beelink was accepted by its configured SMTP relay
on November 17 at 17:14:42 CET (`status=sent`); this proves relay acceptance,
not inbox placement. The updated helper must still be installed with a
successful `--test-and-enable` execution under interactive sudo.

The source transfer helper also accepts `--prepare-mirrors`, collecting only
`dev16.mxtracks.info`, `www16.mxtracks.info` and `git16.mxtracks.info`:

```bash
# Directly on Latitude:
sudo bash ~/git/dockerApache/monitoring/prepare-latitude-certificate-cutover.sh --prepare-mirrors

# Directly on beelink, using the exact private destination printed above:
sudo python3 ~/git/dockerApache/monitoring/renew-beelink-certificates.py \
  --import-mirrors /daten/latitude-migration/cutover-20261117/beelink-cutover-certificates.EXACT
```

The import validates archive membership/checksums using the existing safe
certificate importer, retains existing ACME accounts, and relocates renewal
paths. It tests all eight lineages against ACME staging before installing
the updated helper. Source hooks are never imported. If the import succeeds
but ACME/deployment fails, inspect the error and retry `--test-and-enable`,
not `--import-mirrors` (which deliberately refuses existing mirror lineages).

Mirror deployment uses the existing restricted
`/home/hannes/.ssh/mac2016-cert-deploy` key; its public fingerprint matches
Latitude's key. Strict host checking uses the existing `mac2016` known-host
alias while connecting to `192.168.178.70`. A read-only invalid-command probe
confirmed that the remote forced-command receiver accepts the key and rejects
arbitrary commands. Only `fullchain.pem` and `privkey.pem` are streamed for each
allowed mirror name; no private key is printed. The existing receiver reloads
mirror Apache, then the sender verifies the actual TLS certificate fingerprint.
The deployment is retried on later runs even when Certbot renews nothing.

Do not retire Latitude's renewal ownership until this eight-lineage dry-run,
mirror deployment and status mail have succeeded. Neither source timer nor
source renewal configurations have been disabled by these preparations.

On November 17 at 17:19:31 CET the eight-lineage dry-run and deployment
completed successfully, including verified served certificates on mac2016
for dev16/www16/git16. The updated beelink timer is active and enabled; the
status email was accepted by the SMTP relay at 17:19:32. The source timer was
still active afterward, with its service inactive. The remaining ownership
handover requires interactive sudo directly on Latitude:

```bash
sudo systemctl disable --now snap.certbot.renew.timer
systemctl is-active snap.certbot.renew.timer
systemctl is-enabled snap.certbot.renew.timer
```

Expect `inactive` and `disabled` (these queries return nonzero for those states).
Retain source certificate/account files for rollback. Retired devcopy/wwwcopy
lineages are not renewed by beelink; production and mirror names are covered.
The source timer shutdown is pending confirmation, not yet performed.

### Exact-path redirects after router switch

The phone's `/owncloud` 404 was reproduced directly on beelink:
`/owncloud/` reaches ownCloud, but `/owncloud` was consumed by the catch-all
WordPress proxy before `RedirectMatch` could run. Exact `ProxyPassMatch`
exclusions now allow frontend redirects for `/owncloud` and `/wordpress`;
the latter also prevents the backend's redirect to plain HTTP.

The repair is prepared and deployed, but requires interactive sudo on beelink:

```bash
sudo python3 ~/git/dockerApache/monitoring/start-beelink-frontend.py --fix-path-redirects
```

This mode accepts only the exact known old/current frontend configuration,
backs up the old vhosts, validates Apache, and gracefully reloads without
restarting containers or touching ownCloud. It verifies all three HTTPS
redirects (www ownCloud plus www/dev WordPress), restoring and reloading the
original configuration on failure. It does not run the staging startup flow.

Direct LAN and forced current-public-IPv4 tests returned 200 for dev `/`
and `/wordpress/`. The public-IP test originated inside the LAN, so it is
not proof of external reachability. This Mac's `/etc/hosts` still maps
`dev.mxtracks.info` to Latitude IPv4 `192.168.178.129` and an old Latitude
ULA IPv6; www also has an old ULA override. Those client overrides must be
updated separately. Public AAAA still resolves to the router rather than
beelink; phone Wi-Fi/VPN DNS and true external IPv6 remain unverified.

## 4. Jobs, mirrors and monitoring (after router switch)

The November 17 17:22 schedule audit found that all seven old application
backup/mirror/monitor schedules still existed on Latitude, whereas beelink
had only the certificate timer. Root crontabs were not accessible without
interactive sudo. Do not wait for these old jobs to run against frozen data.
The first handover step is:

```bash
# Directly on Latitude:
sudo python3 ~/git/dockerApache/monitoring/disable-latitude-migration-jobs.py --disable
```

The helper backs up root/hannes crontabs and the four known application
`/etc/cron.d` files privately under `/var/backups/latitude-cutover-jobs/`.
It comments out only known migrated job commands, verifies the resulting
crontabs, keeps unrelated upstream checks and OS maintenance, records timer
and process inventories, and disables the old snap Certbot timer. It reports
remaining root/user cron entries for review instead of claiming unknown jobs
are migrated. It does not terminate an already-running job; the preceding
process check found no active known backup/mirror workers.

The source fence completed at 17:26:13 CET: four hannes entries plus the three
system cron mirror entries were disabled. Root's crontab is empty; only the
two unrelated upstream Git checks remain in hannes' crontab. The private
rollback inventory is `/var/backups/latitude-cutover-jobs/20261117T172613115557`.

Destination scheduling activation is still pending. Copying the source schedules without
adapting their implementations is unsafe: the PostgreSQL backup still dumps
native host MXServer and old ownCloud containers; the GitLab backup hard-codes
the old `gitlab` container; the Apache/Matomo jobs assume native/old Compose
paths. Source ownCloud/MXServer helpers require root to inspect.

### Installing adapted beelink schedules

The adapted scripts and installer are deployed. Directly on beelink:

```bash
sudo python3 ~/git/dockerApache/monitoring/beelink-jobs.py --install
```

The installer checks the exact T7 UUID, private runtime, PostgreSQL readiness,
source files, normal SSH identity, managed mirror mount markers and GitLab
version compatibility. Root-owned copies and seven systemd service/timer
pairs live outside the writable checkout. Each execution rechecks the mount
and active stacks before touching production; a shared lifecycle lock prevents
overlapping jobs or staging operations. Failures are recorded and mailed.
Times stay 19:15 PostgreSQL, 03:05 Apache/WordPress, 03:15 GitLab, 04:00 ownCloud,
04:30 MXServer, 04:45 Matomo, and minute 0 hourly for monitoring. Missed timers
do not trigger surprise catch-up restores at boot.

Before enabling timers, installation runs an actual PostgreSQL-only backup
with complete archive decoding checks and a live health check. It does not
repeat full GitLab or ownCloud snapshots/restores during installation. Mirror
jobs still require their first actual scheduled/manual run to establish success;
passing preflight is not a verified mirror restore. The existing mac2016
monitor is backed up and updated to query beelink, retaining its minute-10
crontab entry and explicit HOME/PATH. The reverse SSH path was checked.

The PostgreSQL job dumps MXServer and ownCloud from their new isolated DB
containers, and the unchanged camera container; no native host database is
used on beelink. Archives are under `/daten/beelink-backups/postgresql/daily/`
with scoped 30-day retention. GitLab uses the existing restricted mirror
receiver/key and pending-snapshot retry mechanism, but the new source container.
ownCloud and MXServer reuse the existing guarded mirror workflows with an
explicit beelink mode, new paths, DB roles, pinned SSH host alias and ownCloud
HTTPS status endpoint. The ownCloud job pauses source writes briefly for its
final incremental snapshot, as before. Matomo now dumps its Docker database.

The WordPress job targets only the two existing mirror schemas/docroots,
backs up mirror databases/configs first, preserves mirror `wp-config.php`
credentials, excludes Matomo, updates serialized URLs with WP-CLI, and checks
mirror HTTPS. Its `.maintenance` marker remains on failure. No mirror vhost,
certificate, ownCloud or GitLab data is overwritten by that job.

All six backup/sync jobs mail each run to `hannes.software@gmx.at`.
Hourly monitoring sends initial status and failure/recovery transitions.
Statuses are under `/var/lib/beelink-jobs/`; journals retain job output.
The public read-only status helper contains no credentials:

```bash
python3 /usr/local/bin/beelink-job-status --report
systemctl list-timers 'beelink-job-*'
sudo journalctl -u beelink-job-postgresql.service --no-pager
```

The status explicitly shows `PENDING FIRST RUN` until a job actually finishes.
Health monitoring flags failed runs immediately and missing/stale runs after
30 hours. General production boot/lifecycle activation is separate: the job
timers do not unlock the T7 or start application stacks themselves.

Installation completed at 17:39 on November 17: all seven timers are enabled.
The first PostgreSQL backup completed at 17:39:30 under
`/daten/beelink-backups/postgresql/daily/20261117_173930`, including full archive
read checks. The initial local and mac2016 availability checks passed, and
Postfix reports relay acceptance for the notifications. The five mirror jobs
still await their first run.

An unprivileged follow-up exposed a status-directory permissions bug: the
installer's umask reduced the requested 0755 to 0700, hiding status files from
the public reader and mac2016 monitor. The helper now explicitly chmods its
status directory to 0755 (only nonsecret JSON files are readable; lock files
remain private) and reports unreadable status as an error instead of pending.
For the already installed instance apply without rerunning backups:

```bash
sudo chmod 755 /var/lib/beelink-jobs
sudo install -m 700 ~/git/dockerApache/monitoring/beelink-jobs.py /usr/local/libexec/beelink-jobs/beelink-jobs.py
sudo install -m 755 ~/git/dockerApache/monitoring/beelink-jobs.py /usr/local/bin/beelink-job-status
python3 /usr/local/bin/beelink-job-status --report
```

Move the repository-related user and root cron jobs to beelink, preserving
times, notifications, locks and backup retention. Disable Latitude's matching
cron entries and any other repository writers without deleting unrelated
jobs. Verify beelink-to-mac2016 SSH, including the restricted GitLab backup
key, with the **actual cron user** before enabling jobs.

### Combined boot setup and immediate mirror verification

On November 17 the user authorized running the five mirror jobs now rather
than waiting for their first night runs, together with boot enablement:

```bash
# Directly on beelink:
sudo python3 ~/git/dockerApache/monitoring/activate-beelink-operations.py --prepare-and-check
sudo journalctl -fu beelink-operations-check.service
```

The prepared helper checks inputs and the private root-owned T7 key, refreshes
the installed startup controller/helpers, adds an explicit encrypted-device
dependency to `daten.mount`, and enables the existing managed stack plus both
public web sockets for boot. UUID checks and mount-bound shutdown are retained;
no Docker restart policies, app networks, encrypted key contents, or protected
containers are changed. This does not reboot or restart the live stack.

It launches a systemd-owned, sequential run of Apache, GitLab, ownCloud,
MXServer, Matomo and monitoring through their actual installed job services.
An independent job failure does not suppress later checks, but makes the
overall verification fail. Existing per-job mails and status records apply.
The runner survives an SSH disconnect; journals provide progress. This is
an actual mirror update/restore, not a dry-run, and ownCloud's existing final
snapshot briefly pauses its source writes. Normal timers remain enabled.

The combined command is deployed but pending interactive sudo execution.
Neither configuring dependencies nor successful job runs proves a full-machine
reboot: encrypted-device unlock and service recovery after an actual reboot
must still be checked in a separate controlled restart window.

The combined command subsequently ran on November 17: boot enablement is
confirmed; Apache (17:50), GitLab (18:23), MXServer (18:24) and Matomo (18:37)
mirror jobs succeeded. The scheduled 19:15 PostgreSQL backup also succeeded.
ownCloud failed before synchronization because its inherited regular-file
guard rejected `/dev/null`, deliberately used as the empty beelink Compose
environment. Monitoring correctly reported that failed job, not a second
application outage. The guard now explicitly accepts only readable `/dev/null`
on beelink and still requires a readable regular environment file on Latitude.
Regression tests passed on macOS and Linux; the corrected script is deployed
in the checkout but the installed root-owned copy needs updating:

```bash
sudo install -m 700 ~/git/dockerApache/owncloud/sync-latitude.sh /usr/local/libexec/beelink-jobs/owncloud/sync-latitude.sh
sudo systemctl start beelink-job-owncloud.service && sudo systemctl start beelink-job-monitor.service
```

Only ownCloud and monitoring need rerunning; do not repeat successful full
mirror jobs. ownCloud success and an actual machine reboot remain unverified.

The 19:29 ownCloud retry passed the environment guard but stopped before sync
at a second inherited check: `findmnt --target /daten` included the autofs
layer alongside ext4. The corrected guard explicitly selects the exact ext4
mount (`findmnt -rn -M /daten -t ext4 -o FSTYPE`), confirmed returning `ext4`
on beelink. Four environment/mount regression tests pass on both hosts.
The checkout is updated; repeat the `install` and two-service commands above
using the exact `.sh` destination (not `.shsh`). Successful ownCloud sync
remains pending, and this failure did not start synchronization.

The 19:31 retry completed the initial incremental files/apps/config copy, then
failed when enabling source maintenance: invoking `php /var/www/owncloud/occ`
directly omitted the image entrypoint environment, making `datadirectory`
appear empty. Source web status confirms maintenance=false and normal service.
The mirror was intentionally left stopped in maintenance; its DB replacement
had not started. The beelink branch now uses the image's `/usr/bin/occ` wrapper
as www-data, verified with a successful live `occ status`, and checks CLI
readiness before any mirror changes. Five focused regression tests passed
on both hosts. Updated checkout deployed; install the corrected root copy
and rerun only ownCloud then monitoring using the commands above.

At 19:37:08 CET the corrected ownCloud job completed successfully, followed
by monitoring at 19:37:10. All seven recorded jobs now show SUCCESS.
Source and mirror ownCloud HTTPS status checks both confirm 11.1.0 installed,
maintenance=false and needsDbUpgrade=false. The managed stack is boot-enabled.
This confirms actual executions of all mirror jobs. Subsequent host reboot
verification is recorded below.

The actual 19:40:43 reboot unlocked/mounted the correct T7 and recovered all
applications; startup checks finished at 19:42:56. Both public web sockets,
however, were dropped from the boot transaction due to an ordering cycle:
their implicit `Before=sockets.target` conflicted with waiting for the
Docker-backed staging service (which starts after basic/network targets).
The prepared `--fix-web-boot` mode adds socket-only `DefaultDependencies=no`
drop-ins, retaining explicit sysinit ordering, shutdown conflict/order, and
the existing staging/T7 bindings. It restores both sockets and verifies live
health without restarting applications or rerunning mirror jobs:

```bash
sudo python3 ~/git/dockerApache/monitoring/activate-beelink-operations.py --fix-web-boot
```

The corrected unit ordering was applied and verified after the second reboot
at 19:47:54 on 2026-11-17. The expected T7 filesystem mounted automatically,
staging startup completed at 19:50:02, and both public web sockets started
automatically immediately afterwards. All eight timers were scheduled; all
seven retained job results remained SUCCESS. After GitLab's startup health
check settled, `beelink-job-status --health-json` returned an empty failures
list and systemd reported zero failed units. This confirms boot recovery,
not the future overnight scheduled executions; no mirror jobs were rerun.

### Production naming correction (applied)

The corrected retry completed on 2026-11-17 around 22:25. Live inspection
confirmed the renamed production containers running, GitLab healthy,
`beelink-production.service` active and boot-enabled, both public sockets
active, all eight timers scheduled, an empty health failures list and no
failed systemd units. Home Assistant and the original camera database retained
their pre-migration uptime. The subsequent reboot at 22:29:33 was verified:
production startup completed at 22:31:42 and both public sockets opened
automatically. All renamed containers recovered, GitLab became healthy,
the live health failures list was empty and systemd had no failed units.
The upcoming overnight scheduled jobs have not yet been verified.

The first attempt on 2026-11-17 rolled back: the health check incorrectly
required the certificate timer while the migration itself had paused it.
At 22:17, live health was clean, the original names/boot service were restored,
and all eight schedules were active again. The corrected helper excludes only
exact inactive-timer failures for timers it deliberately paused, in both the
forward and rollback checks. It then resumes and checks every captured timer
and repeats the full, unfiltered health check before recording completion.
Regression coverage now exercises the real health-check function in both the
successful transition and rollback, including a paused certificate timer.
The corrected helper was deployed and used for the successful retry.

`monitoring/rename-beelink-production.py --apply` replaces the misleading live
Compose project/container names: `beelink-staging` becomes `beelink-databases`,
`beelink-app-test` becomes `beelink-apps`, and `-test` is removed from the
WordPress, Matomo, artifacts, DNS and frontend project names. The Java project,
Home Assistant and original camera PostgreSQL names remain unchanged.
The lifecycle unit becomes `beelink-production.service`; dependencies,
root-installed helpers, backup/mirror scripts, health checks and frontend/
database hostname configurations are migrated together.

Run directly on beelink with interactive sudo:

```bash
sudo python3 ~/git/dockerApache/monitoring/rename-beelink-production.py --apply
```

Expect a few minutes of application downtime, including GitLab readiness.
The helper refuses active backup/renewal operations, pauses the eight timers,
creates a private rollback inventory under
`/var/backups/beelink-production-names/`, stops only managed containers,
and recreates them under their correct Compose projects. Images are neither
pulled nor rebuilt; no database dumps are imported and no mirror jobs rerun.
The renamed internal network keeps subnet/gateway `172.19.0.0/16` /
`172.19.0.1` explicitly pinned in both database Compose snapshots, preserving
frontend `172.19.0.15` and Home Assistant trust. Socket boot-order drop-ins
remain intact. Live TLS/HTTP health, protected-container identity and all
bind mounts must match before success is recorded in
`/daten/beelink-runtime/production-names.json`. On failure the helper attempts
to restore the previous configuration/names; schedules stay paused if
recovery does not complete.

Existing data paths such as `/daten/beelink-staging`, installed helper paths
under `/usr/local/libexec/beelink-staging`, and the shared lifecycle lock
retain their historical names intentionally: they are persistent references,
not disposable test data. The initial isolated database restore tool is
fenced against rerunning its old project on the now-production directories.
The change required an interactive root invocation on beelink.

All mirror sources then become beelink. Both health monitors must check only
beelink and mac2016, including storage, all migrated services, source job
reports and notification delivery. Preserve the mac2016 cron HOME/PATH.
Test actual backup and mirror runs and restart behavior before declaring
replacement complete. Encrypt the destination's private configuration using
host-specific `private/private.sh` targets; never copy Latitude's encrypted
filename as if it represented beelink's configuration.
