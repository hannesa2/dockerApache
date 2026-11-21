# Camera PostgreSQL

The databases `camDoor`, `camHinten`, and `camPi3` run in a dedicated
PostgreSQL 16 container on beelink. The bind-mounted data directory and
credentials are private. The beelink PostgreSQL job includes these databases
in its regular backups.

The container publishes PostgreSQL on beelink's LAN address
`192.168.178.142:5432` and loopback. `pg_hba.conf` allows the `cam` role only
from `192.168.178.0/24`; other network users are rejected. Docker port
forwarding can bypass UFW, so do not expose this port through the router.
SCRAM does not encrypt database traffic; TLS is not enforced. Review these
settings if the LAN, address, or clients change.

```bash
cd ~/git/dockerApache/camera-postgres
docker compose ps
docker compose logs db
```

`.env` and the data directory contain sensitive credentials and data. A
container restart does not recreate or reimport databases; initialization
environment variables apply only to an empty data directory. Changing
`CAMERA_PASSWORD` or `POSTGRES_PASSWORD` alone does not update an initialized
PostgreSQL role.

Encrypt the host-specific configuration separately, using a passphrase kept
outside the repository:

```bash
cd ~/git/dockerApache
./private/private.sh --crypt camera-postgres
```

The database backups do not include `.env` or other server configuration.
