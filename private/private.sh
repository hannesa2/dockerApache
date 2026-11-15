#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/buildSrc/scripts/decrypt.sh"
CIPHER_DIR="$ROOT/private-config/gpg"

usage() {
  echo "Usage: $0 --crypt|--decrypt [apache|nextcloud|matomo ...]" >&2
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
for target in "${targets[@]}"; do
  case "$target" in
    apache) plain="$ROOT/apache/.env"; cipher="$CIPHER_DIR/apache.env" ;;
    nextcloud) plain="$ROOT/nextcloud/.env"; cipher="$CIPHER_DIR/nextcloud.env" ;;
    matomo) plain="$(python3 "$ROOT/private/check-private.py" matomo-path)"; cipher="$CIPHER_DIR/matomo-config.ini" ;;
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
    [[ -f "$plain" && -r "$plain" ]] || {
      echo "ERROR: Cannot read $target configuration: $plain" >&2
      exit 1
    }
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
trap 'unset CRYPT_PASS CRYPT_PASS_CONFIRM' EXIT

for i in "${!plain_paths[@]}"; do
  if [[ "$mode" == --crypt ]]; then
    fingerprint="$(python3 "$ROOT/private/check-private.py" prepare "${plain_paths[$i]}")"
    "$HELPER" --crypt --output "${cipher_paths[$i]}.enc" "${plain_paths[$i]}"
    python3 "$ROOT/private/check-private.py" record "${plain_paths[$i]}" \
      "${cipher_paths[$i]}.enc" "$fingerprint"
  else
    "$HELPER" --output "${plain_paths[$i]}" "${cipher_paths[$i]}"
  fi
done
