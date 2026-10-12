#!/bin/bash
set -euo pipefail
export LC_ALL=C LANG=C

case "${SSH_ORIGINAL_COMMAND:-}" in
    dev16.mxtracks.info|www16.mxtracks.info)
        site="$SSH_ORIGINAL_COMMAND"
        ;;
    *)
        echo "Rejected certificate deployment request" >&2
        exit 64
        ;;
esac

cert_dir="/Volumes/SamsungT5_1T/apache/certs"
staging_dir="$(/usr/bin/mktemp -d "$cert_dir/.incoming.XXXXXX")"
trap 'rm -rf "$staging_dir"' EXIT
archive="$staging_dir/certificates.tar"

/bin/cat > "$archive"

members="$(/usr/bin/tar -tf "$archive")"
expected="$(printf 'fullchain.pem\nprivkey.pem')"
if [ "$members" != "$expected" ]; then
    echo "Rejected unexpected certificate archive contents" >&2
    exit 65
fi

if ! /usr/bin/tar -tvf "$archive" |
    /usr/bin/awk 'substr($0, 1, 1) != "-" { invalid = 1 } END { exit invalid }'; then
    echo "Rejected non-regular certificate archive entries" >&2
    exit 65
fi

/usr/bin/tar -xf "$archive" -C "$staging_dir"
for file in fullchain.pem privkey.pem; do
    if [ ! -f "$staging_dir/$file" ] || [ -L "$staging_dir/$file" ]; then
        echo "Rejected invalid certificate file: $file" >&2
        exit 65
    fi
done

/usr/bin/install -m 0644 "$staging_dir/fullchain.pem" \
    "$cert_dir/$site-fullchain.pem.new"
/usr/bin/install -m 0600 "$staging_dir/privkey.pem" \
    "$cert_dir/$site-privkey.pem.new"
/bin/mv -f "$cert_dir/$site-fullchain.pem.new" "$cert_dir/$site-fullchain.pem"
/bin/mv -f "$cert_dir/$site-privkey.pem.new" "$cert_dir/$site-privkey.pem"

"$HOME/.orbstack/bin/docker" --context orbstack \
    compose -f "$HOME/git/dockerApache/apache/docker-compose.yml" \
    exec -T apache apachectl -t
"$HOME/.orbstack/bin/docker" --context orbstack \
    compose -f "$HOME/git/dockerApache/apache/docker-compose.yml" \
    exec -T apache apachectl -k graceful

echo "Installed renewed certificate for $site"
