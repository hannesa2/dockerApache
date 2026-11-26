#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C LANG=C HOME=/root
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $# == 1 && "$1" == --test ]] || fail "Usage: sudo bash $0 --test"
[[ "$(id -u)" == 0 && "$(hostname -s)" == beelink ]] ||
    fail "Run as root on beelink."
for cmd in docker findmnt flock python3 sha256sum cmp curl; do
    command -v "$cmd" >/dev/null || fail "Missing command: $cmd"
done

target=/datenRestoreTest
snapshot=/datenBackup/owncloud-root/backintime/beelink/root/1/20261126-063812-580
stage="$snapshot/backup/daten/beelink-backups/backintime-owncloud"
container=owncloud-restore-test
image=sha256:81e77a4833487b8132fe4b00bedd39dc3f74b8641e387f19312a48f7de218c2e
started=0
work=""
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if (( started )); then
        docker stop --time 30 "$container" >/dev/null || status=1
    fi
    if [[ -n "$work" ]]; then
        echo "Test artifacts retained at $work"
    fi
    if (( status != 0 )); then
        echo "ERROR: Restore test incomplete; inspect retained artifacts." >&2
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
exec 9>/run/lock/owncloud-restore-test.lock
flock -n 9 || fail "Another restore test is running."
[[ ! -L "$target" &&
   "$(findmnt -rn -M "$target" -t ext4 -o UUID)" == 4f29138d-ca50-4a46-bf9d-d4e37e3b64c9 ]] ||
    fail "Expected encrypted Samsung test filesystem is not mounted."
[[ "$(findmnt -rn -M /datenBackup -t ext4 -o UUID)" == fe91ecb5-c1a4-4e6c-9edf-f3c9c75cffe1 ]] ||
    fail "Expected backup disk is not mounted."
[[ -d "$snapshot" && ! -e "$snapshot/failed" && -s "$stage/owncloud.dump" ]] ||
    fail "Consistent snapshot or database dump missing."
docker image inspect "$image" >/dev/null
[[ -z "$(docker ps -aq --filter name="^/${container}$")" ]] ||
    fail "Test container already exists; inspect before retrying."

healthy() {
    curl --noproxy '*' -fsS --max-time 10 \
        --resolve www.mxtracks.info:443:127.0.0.1 \
        https://www.mxtracks.info/owncloud/status.php |
        python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("installed") and not s.get("maintenance") and not s.get("needsDbUpgrade")'
}
healthy || fail "Production is not healthy before the test."
work="$(mktemp -d "$target/stage1-XXXXXXXX")"
echo "Testing snapshot $(basename "$snapshot"); no production mounts or network."

python3 - "$snapshot/backup" "$work" <<'PY'
import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys

source, target = map(Path, sys.argv[1:])
stage = source / "daten/beelink-backups/backintime-owncloud"
dump = stage / "owncloud.dump"
expected = (stage / "owncloud.dump.sha256").read_text().split()[0]

def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()

if digest(dump) != expected:
    raise SystemExit("ERROR: Snapshot dump checksum mismatch.")
shutil.copy2(dump, target / "owncloud.dump")
if digest(target / "owncloud.dump") != expected:
    raise SystemExit("ERROR: Copied dump checksum mismatch.")

data = source / "daten/beelink-runtime/rootfs/daten/owncloud/data"
volume = source / "daten/beelink-runtime/apps/owncloud/volume"
samples = [data / ".ocdata", volume / "config/config.php"]
total = 0
users = 0
for user in sorted(data.iterdir()):
    files = user / "files"
    if user.is_symlink() or files.is_symlink() or not files.is_dir():
        continue
    count = 0
    for root, dirs, names in os.walk(files, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(root) / d).is_symlink())
        for name in sorted(names):
            path = Path(root) / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_size > 64 * 1024**2 or total + info.st_size > 256 * 1024**2:
                continue
            samples.append(path)
            total += info.st_size
            count += 1
            if count == 3:
                break
        if count == 3:
            break
    if count:
        users += 1
    if users == 4:
        break
if len(samples) == 2:
    raise SystemExit("ERROR: No user files selected for restoration.")
manifest = []
for path in samples:
    if path.is_symlink() or not path.is_file():
        raise SystemExit("ERROR: Required sample is not a regular file.")
    relative = path.relative_to(source)
    restored = target / "files" / relative
    restored.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, restored)
    checksum = digest(path)
    if digest(restored) != checksum:
        raise SystemExit("ERROR: Restored sample checksum mismatch.")
    manifest.append(f"{checksum}  {relative}\n")
(target / "sample-checksums.txt").write_text("".join(manifest))
print(f"Verified dump and {len(samples)} restored sample files from {users} users.")
PY

mkdir "$work/pgdata"
docker run -d --name "$container" --network none --restart no \
    --memory 1g --cpus 1 --pids-limit 256 \
    --label purpose=owncloud-restore-test \
    --mount "type=bind,src=$work/pgdata,dst=/var/lib/postgresql/data" \
    -e POSTGRES_HOST_AUTH_METHOD=trust "$image" >/dev/null
started=1
ready=0
for attempt in {1..60}; do
    if [[ "$(docker exec "$container" cat /proc/1/comm)" == postgres ]] &&
        docker exec "$container" pg_isready -U postgres >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
(( ready )) || fail "Isolated PostgreSQL did not become ready."
docker exec "$container" createdb -U postgres owncloud_restore_test
docker exec -i "$container" pg_restore -U postgres -d owncloud_restore_test \
    --exit-on-error --single-transaction --no-owner --no-privileges < "$work/owncloud.dump"
docker exec -i "$container" psql -X -U postgres -d owncloud_restore_test \
    -v ON_ERROR_STOP=1 > "$work/database-check.txt" <<'SQL'
SELECT count(*) AS restored_public_tables
FROM pg_tables WHERE schemaname = 'public';
DO $$
DECLARE
    item record;
    rows_count bigint;
    found_users boolean := false;
    found_filecache boolean := false;
BEGIN
    FOR item IN SELECT tablename FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename ~ '_(users|filecache)$'
    LOOP
        EXECUTE format('SELECT count(*) FROM public.%I', item.tablename) INTO rows_count;
        IF rows_count = 0 THEN
            RAISE EXCEPTION 'Restored ownCloud table % is empty', item.tablename;
        END IF;
        found_users := found_users OR item.tablename ~ '_users$';
        found_filecache := found_filecache OR item.tablename ~ '_filecache$';
        RAISE NOTICE 'Restored table %: % rows', item.tablename, rows_count;
    END LOOP;
    IF NOT found_users OR NOT found_filecache THEN
        RAISE EXCEPTION 'Required ownCloud users/filecache tables missing';
    END IF;
END $$;
SQL
healthy || fail "Production health check failed after the test."
printf 'PASS: dump restored into isolated PostgreSQL 16; sample checksums match.\n' |
    tee "$work/RESULT.txt"
echo "This is a partial restore test, not a full ownCloud application recovery."
