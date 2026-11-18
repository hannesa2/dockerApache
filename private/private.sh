#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/buildSrc/scripts/decrypt.sh"
CIPHER_DIR="$ROOT/private-config/gpg"

usage() {
  echo "Usage: $0 --crypt|--decrypt [apache|nextcloud|matomo|camera-postgres|mxserver-postgres ...]" >&2
  exit 2
}

[[ $# -ge 1 ]] || usage
mode="$1"
[[ "$mode" == --crypt || "$mode" == --decrypt ]] || usage
shift
targets=("$@")
if [[ ${#targets[@]} -eq 0 ]]; then
  targets=(apache nextcloud matomo)
fi

[[ -x "$HELPER" ]] || {
  echo "ERROR: Missing executable $HELPER; initialize or update the buildSrc submodule." >&2
  exit 1
}
command -v python3 >/dev/null || { echo "ERROR: python3 is required." >&2; exit 1; }

host="$(hostname -s)" || {
  echo "ERROR: Cannot determine the short hostname." >&2
  exit 1
}
[[ "$host" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || {
  echo "ERROR: Invalid short hostname: $host" >&2
  exit 1
}

plain_paths=()
cipher_paths=()
protected_matomo=/daten/beelink-runtime/matomo/www/piwik/config/config.ini.php
needs_matomo_sudo=0
for target in "${targets[@]}"; do
  case "$target" in
    apache) plain="$ROOT/apache/.env"; cipher="$CIPHER_DIR/apache.env" ;;
    nextcloud) plain="$ROOT/nextcloud/.env"; cipher="$CIPHER_DIR/nextcloud.env" ;;
    matomo) plain="$(python3 "$ROOT/private/check-private.py" matomo-path)"; cipher="$CIPHER_DIR/matomo-config.ini" ;;
    camera-postgres) plain="$ROOT/camera-postgres/.env"; cipher="$CIPHER_DIR/camera-postgres.env" ;;
    mxserver-postgres) plain="$ROOT/mxserver-postgres/.env"; cipher="$CIPHER_DIR/mxserver-postgres.env" ;;
    *) usage ;;
  esac
  cipher="$cipher.$host"
  for previous in "${cipher_paths[@]}"; do
    [[ "$previous" != "$cipher" ]] || {
      echo "ERROR: Duplicate target: $target" >&2
      exit 1
    }
  done
  if [[ "$mode" == --crypt ]]; then
    if [[ "$host" == beelink && "$plain" == "$protected_matomo" ]]; then
      needs_matomo_sudo=1
    elif [[ ! -f "$plain" || ! -r "$plain" ]]; then
      echo "ERROR: Cannot read $target configuration: $plain" >&2
      exit 1
    fi
  else
    [[ -f "$cipher.enc" && -r "$cipher.enc" ]] || {
      echo "ERROR: Cannot read encrypted $target configuration: $cipher.enc" >&2
      exit 1
    }
    [[ -d "$(dirname -- "$plain")" ]] || {
      echo "ERROR: Output directory does not exist: $(dirname -- "$plain")" >&2
      exit 1
    }
  fi
  plain_paths+=("$plain")
  cipher_paths+=("$cipher")
done

if [[ "$needs_matomo_sudo" -eq 1 ]]; then
  sudo -v || {
    echo "ERROR: sudo authentication is required to read the Matomo configuration." >&2
    exit 1
  }
  if ! sudo -n test -f "$protected_matomo" ||
     ! sudo -n cat -- "$protected_matomo" > /dev/null; then
    echo "ERROR: Cannot read Matomo configuration with sudo: $protected_matomo" >&2
    exit 1
  fi
fi

if [[ -z "${CRYPT_PASS:-}" ]]; then
  [[ -t 0 ]] || { echo "ERROR: A terminal or CRYPT_PASS is required." >&2; exit 1; }
  IFS= read -r -s -p "Passphrase: " CRYPT_PASS
  printf '\n' >&2
  [[ -n "$CRYPT_PASS" ]] || { echo "ERROR: Empty passphrase." >&2; exit 1; }
  if [[ "$mode" == --crypt ]]; then
    IFS= read -r -s -p "Repeat passphrase: " CRYPT_PASS_CONFIRM
    printf '\n' >&2
  fi
fi
if [[ "$mode" == --crypt && "${CRYPT_PASS_CONFIRM:-}" != "$CRYPT_PASS" ]]; then
  echo "ERROR: Passphrases do not match (set CRYPT_PASS_CONFIRM when using CRYPT_PASS)." >&2
  exit 1
fi
export CRYPT_PASS
if [[ "$mode" == --crypt ]]; then
  export CRYPT_PASS_CONFIRM
  mkdir -p -- "$CIPHER_DIR"
fi
private_snapshot=''
trap '[[ -z "$private_snapshot" ]] || rm -f -- "$private_snapshot"; unset CRYPT_PASS CRYPT_PASS_CONFIRM' EXIT

for i in "${!plain_paths[@]}"; do
  if [[ "$mode" == --crypt ]]; then
    fingerprint="$(python3 "$ROOT/private/check-private.py" prepare "${plain_paths[$i]}")"
    input="${plain_paths[$i]}"
    if [[ "$host" == beelink && "$input" == "$protected_matomo" ]]; then
      private_snapshot="$(mktemp "${TMPDIR:-/tmp}/private-matomo.XXXXXXXX")"
      sudo -n cat -- "$input" > "$private_snapshot"
      input="$private_snapshot"
    fi
    "$HELPER" --crypt --output "${cipher_paths[$i]}.enc" "$input"
    python3 "$ROOT/private/check-private.py" record "${plain_paths[$i]}" \
      "${cipher_paths[$i]}.enc" "$fingerprint"
    if [[ -n "$private_snapshot" ]]; then
      rm -f -- "$private_snapshot"
      private_snapshot=''
    fi
  else
    "$HELPER" --output "${plain_paths[$i]}" "${cipher_paths[$i]}"
  fi
done
