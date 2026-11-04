#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
KEY_CIPHERTEXT="$SCRIPT_DIR/age-identity.txt.enc"
OPENSSL_ITERATIONS=600000
TEMP_KEY=""
TEMP_OUTPUT=""
TEMP_CIPHERTEXT=""

cleanup() {
  [[ -z "$TEMP_KEY" ]] || rm -f "$TEMP_KEY"
  [[ -z "$TEMP_OUTPUT" ]] || rm -f "$TEMP_OUTPUT"
  [[ -z "$TEMP_CIPHERTEXT" ]] || rm -f "$TEMP_CIPHERTEXT"
}
trap cleanup EXIT

usage() {
  printf 'Usage: %s setup-key | apache | nextcloud | matomo\n' "$0" >&2
  exit 2
}

read_passphrase() {
  [[ -t 0 ]] || {
    echo "Run this command from an interactive terminal." >&2
    exit 1
  }
  IFS= read -r -s -p "Memorable passphrase: " PASSPHRASE
  printf '\n' >&2
  [[ -n "$PASSPHRASE" ]] || {
    echo "Passphrase must not be empty." >&2
    exit 1
  }
}

setup_key() {
  local identity_file confirm
  [[ ! -e "$KEY_CIPHERTEXT" ]] || {
    echo "$KEY_CIPHERTEXT already exists; refusing to overwrite it." >&2
    exit 1
  }

  identity_file="${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"
  [[ -r "$identity_file" ]] || {
    echo "Cannot read Age identity: $identity_file" >&2
    exit 1
  }
  age-keygen -y "$identity_file" >/dev/null

  read_passphrase
  IFS= read -r -s -p "Repeat passphrase: " confirm
  printf '\n' >&2
  [[ "$PASSPHRASE" == "$confirm" ]] || {
    unset PASSPHRASE confirm
    echo "Passphrases do not match." >&2
    exit 1
  }
  unset confirm

  TEMP_CIPHERTEXT="$(mktemp "$SCRIPT_DIR/.age-identity.XXXXXX")"
  TEMP_KEY="$(mktemp "${TMPDIR:-/tmp}/sops-age-identity.XXXXXX")"
  chmod 600 "$TEMP_CIPHERTEXT" "$TEMP_KEY"
  printf '%s\n' "$PASSPHRASE" |
    openssl enc -aes-256-cbc -salt -pbkdf2 -iter "$OPENSSL_ITERATIONS" \
      -pass stdin -in "$identity_file" -out "$TEMP_CIPHERTEXT"
  printf '%s\n' "$PASSPHRASE" |
    openssl enc -d -aes-256-cbc -pbkdf2 -iter "$OPENSSL_ITERATIONS" \
      -pass stdin -in "$TEMP_CIPHERTEXT" -out "$TEMP_KEY"
  unset PASSPHRASE

  age-keygen -y "$TEMP_KEY" >/dev/null
  SOPS_AGE_KEY_FILE="$TEMP_KEY" sops --decrypt \
    --input-type dotenv --output-type dotenv \
    "$SCRIPT_DIR/apache.env.enc" >/dev/null
  mv "$TEMP_CIPHERTEXT" "$KEY_CIPHERTEXT"
  TEMP_CIPHERTEXT=""
  rm -f "$TEMP_KEY"
  TEMP_KEY=""
  echo "Encrypted Age identity created: $KEY_CIPHERTEXT"
  echo "Keep the passphrase memorable; without it the SOPS files cannot be recovered."
}

decrypt_config() {
  local name="$1" input output format
  case "$name" in
    apache)
      input="$SCRIPT_DIR/apache.env.enc"
      output="$SCRIPT_DIR/../apache/.env"
      format=dotenv
      ;;
    nextcloud)
      input="$SCRIPT_DIR/nextcloud.env.enc"
      output="$SCRIPT_DIR/../nextcloud/.env"
      format=dotenv
      ;;
    matomo)
      input="$SCRIPT_DIR/matomo-config.ini.enc"
      output="${MATOMO_CONFIG_PATH:-/Volumes/SamsungT5_1T/apache/www/mxdocs/piwik/config/config.ini.php}"
      format=ini
      ;;
    *)
      usage
      ;;
  esac

  [[ -r "$KEY_CIPHERTEXT" ]] || {
    echo "Run '$0 setup-key' first to create the passphrase-protected Age identity." >&2
    exit 1
  }
  [[ -r "$input" ]] || {
    echo "Cannot read encrypted config: $input" >&2
    exit 1
  }
  [[ -d "$(dirname "$output")" ]] || {
    echo "Output directory does not exist: $(dirname "$output")" >&2
    exit 1
  }

  read_passphrase
  TEMP_KEY="$(mktemp "${TMPDIR:-/tmp}/sops-age-identity.XXXXXX")"
  chmod 600 "$TEMP_KEY"
  printf '%s\n' "$PASSPHRASE" |
    openssl enc -d -aes-256-cbc -pbkdf2 -iter "$OPENSSL_ITERATIONS" \
      -pass stdin -in "$KEY_CIPHERTEXT" -out "$TEMP_KEY"
  unset PASSPHRASE
  age-keygen -y "$TEMP_KEY" >/dev/null

  TEMP_OUTPUT="$(mktemp "$(dirname "$output")/.sops-decrypt.XXXXXX")"
  chmod 600 "$TEMP_OUTPUT"
  SOPS_AGE_KEY_FILE="$TEMP_KEY" sops --decrypt \
    --input-type "$format" --output-type "$format" "$input" > "$TEMP_OUTPUT"
  mv -f "$TEMP_OUTPUT" "$output"
  TEMP_OUTPUT=""
  echo "Decrypted $name config to $output"
}

[[ $# -eq 1 ]] || usage
case "$1" in
  setup-key) setup_key ;;
  apache|nextcloud|matomo) decrypt_config "$1" ;;
  *) usage ;;
esac
