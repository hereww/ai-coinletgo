#!/bin/sh
set -eu

backup_file="${1:?usage: restore-postgres.sh BACKUP_FILE CONFIRM_RESTORE}"
confirmation="${2:-}"
if [ "$confirmation" != "CONFIRM_RESTORE" ]; then
  printf '%s\n' 'Refusing restore: pass CONFIRM_RESTORE as the second argument.' >&2
  exit 2
fi
key_file="${BACKUP_ENCRYPTION_KEY_FILE:-/run/secrets/backup_encryption_key}"
[ -s "$key_file" ] || { printf '%s\n' 'Backup encryption key is unavailable.' >&2; exit 1; }
[ -s "$backup_file" ] || { printf '%s\n' 'Backup file is unavailable.' >&2; exit 1; }

if ! openssl enc -d -aes-256-cbc -pbkdf2 -pass "file:$key_file" -in "$backup_file" | gzip -t; then
  printf '%s\n' 'Backup verification failed; database was not modified.' >&2
  exit 1
fi

openssl enc -d -aes-256-cbc -pbkdf2 -pass "file:$key_file" -in "$backup_file" \
  | gzip -dc \
  | psql --set ON_ERROR_STOP=1 --no-owner --no-acl
