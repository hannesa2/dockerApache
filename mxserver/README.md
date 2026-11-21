# MXServer standby mirror

The production GlassFish application and PostgreSQL 16 database run on
beelink. mac2016 hosts an independent Payara 5 / PostgreSQL 14 standby copy.
The mirror's database, picture files and file-realm keyfile are synchronized
from beelink by the `beelink-job-mxserver` systemd job. Mirror-only writes are
overwritten by the next successful sync.

The application is built from the sibling `MXServer` checkout. On mac2016,
keep `~/git/MXServer` next to `~/git/dockerApache` and prepare its storage and
runtime:

```bash
cd ~/git/dockerApache
./mxserver/prepare-mirror.sh
```

The production sync is restricted to beelink production mode and verifies the
encrypted T7, source database container, mirror markers, SSH host key and
database readiness before replacing the mirror database. The job starts the
mirror application after the database and picture sync; on failure it attempts
to restore service and reports the error.

Check the production schedule and latest run on beelink:

```bash
systemctl status beelink-job-mxserver.timer beelink-job-mxserver.service --no-pager
sudo journalctl -u beelink-job-mxserver.service -n 100 --no-pager
```

The mirror endpoint is `https://www16.mxtracks.info:9433/backend/`. Apache
terminates TLS and proxies `/backend/` to the Payara application. The router
forwards public TCP 9433 to mac2016 port 9433. Mirror-generated mail is
captured by the private Mailpit service rather than delivered to production
recipients.

Payara 6 is not a drop-in replacement: the application uses Java EE 7
`javax.*` APIs and GlassFish-specific security configuration. Any runtime
migration must be tested separately without changing the active Payara 5
mirror or its database.
