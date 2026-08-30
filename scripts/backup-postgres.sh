#!/bin/sh
set -eu
umask 077

backup_dir="${BACKUP_DIR:-/backups}"
key_file="${BACKUP_ENCRYPTION_KEY_FILE:-/run/secrets/backup_encryption_key}"
status_file="${BACKUP_STATUS_FILE:-$backup_dir/backup.status}"

mkdir -p "$backup_dir/daily" "$backup_dir/monthly"

if [ -s /run/secrets/postgres_password ]; then
  PGPASSWORD="$(cat /run/secrets/postgres_password)"
  export PGPASSWORD
fi

while true; do
  if [ -s "$key_file" ]; then
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    daily_file="$backup_dir/daily/trading-$stamp.sql.gz.enc"
    sql_file="$backup_dir/.trading-$stamp.sql"
    gzip_file="$backup_dir/.trading-$stamp.sql.gz"
    encrypted_file="$daily_file.tmp"
    backup_ok=false
    rm -f "$sql_file" "$gzip_file" "$encrypted_file"
    if pg_dump --no-owner --no-acl --file "$sql_file" \
      && gzip -9 -c "$sql_file" > "$gzip_file" \
      && openssl enc -aes-256-cbc -salt -pbkdf2 -pass "file:$key_file" \
        -in "$gzip_file" -out "$encrypted_file"; then
      rm -f "$sql_file" "$gzip_file"
      mv "$encrypted_file" "$daily_file"
      if openssl enc -d -aes-256-cbc -pbkdf2 -pass "file:$key_file" \
        -in "$daily_file" | gzip -t; then
        backup_ok=true
        printf '%s OK %s\n' "$(date -u +%FT%TZ)" "$daily_file" > "$status_file"
      fi
    fi
    rm -f "$sql_file" "$gzip_file" "$encrypted_file"
    if [ "$backup_ok" != true ]; then
      rm -f "$daily_file"
      printf '%s ERROR backup creation or verification failed\n' \
        "$(date -u +%FT%TZ)" > "$status_file"
    fi
    find "$backup_dir/daily" -type f -mtime +30 -delete

    if [ "$backup_ok" = true ] && [ "$(date -u +%d)" = "01" ]; then
      cp "$daily_file" "$backup_dir/monthly/trading-${stamp%T*}.sql.gz.enc"
      find "$backup_dir/monthly" -type f -mtime +370 -delete
    fi
  else
    printf '%s ERROR backup skipped: encryption key unavailable\n' \
      "$(date -u +%FT%TZ)" > "$status_file"
  fi

  sleep 86400
done
