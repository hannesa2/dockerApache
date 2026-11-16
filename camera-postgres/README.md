# Camera PostgreSQL

The databases `camDoor`, `camHinten`, and `camPi3` run in a dedicated
PostgreSQL 16 container on beelink and Latitude. Each host has its own data
and credentials; there is no automatic synchronization. Database files persist
in the bind-mounted `CAMERA_POSTGRES_DATA_DIR`.

| Host | LAN address and port | Data directory |
| --- | --- | --- |
| beelink | `192.168.178.142:5432` | `/home/hannes/server/postgresql-cameras` |
| Latitude | `192.168.178.129:5433` | `/home/hannes/server/postgresql-cameras` |

Latitude's native PostgreSQL remains on port 5432 for the active `MXServer`.
The native camera databases were removed after migration. Camera clients must
use port 5433 and the Latitude Docker `cam` credentials; existing clients are
not switched automatically. The two hosts' Docker databases are independent.

`cam` owns the camera databases and authenticates with SCRAM. The published port
is bound to the configured LAN IPv4 and loopback. `pg_hba.conf` allows network
access for `cam` only from `192.168.178.0/24`; other addresses and network
database users are rejected. Docker port forwarding can bypass UFW, so do not
expose this port via the router. SCRAM does not encrypt database traffic; TLS is
not enforced. Review these settings when the LAN, address, or clients change.
If the LAN address changes, update `.env` before recreating the container.

```bash
cd ~/git/dockerApache/camera-postgres
docker compose ps
docker compose logs db
```

`.env` and the data directory contain sensitive credentials and data. A Docker
restart does not recreate or reimport databases; initialization environment
variables apply only to an empty data directory. `CAMERA_PASSWORD` stores the
password for `cam` and `POSTGRES_PASSWORD` the administrator password.
Changing either entry alone does not update an initialized PostgreSQL role.
Connections to `postgres` and `template1` were revoked from `PUBLIC` on the
deployed instances; this must be repeated after initializing a new cluster.

Encrypt each host's configuration separately, using a passphrase kept outside
the repository:

```bash
cd ~/git/dockerApache
./private/private.sh --crypt camera-postgres
```

Latitude's scheduled PostgreSQL job backs up the Docker camera databases.
Older backup sets may also contain the former native databases and are retained
under the job's normal retention policy. The final native dumps in
`20261116_083210` were taken with connection limits set to zero; if restored
with `pg_restore -C`, set each restored database's connection limit back to
`-1` before accepting ordinary client connections. The beelink instance has no
independent backup configured; configure one before storing important camera
data there. A database dump does not back up the `.env`, roles, or other server
configuration.
