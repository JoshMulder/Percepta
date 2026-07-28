#!/usr/bin/env sh
# Scheduled backups, run inside the `backup` compose service.
#
# An interval rather than a cron expression, deliberately. Parsing cron in shell
# is a liability nobody needs, and "every N hours" is what this actually wants -
# a backup that lands at an unhelpful moment is a backup you take again, not a
# scheduling problem. Everything is configured in .env.
#
# It uses the same postgres image as the database, so pg_dump always matches the
# server version. A newer server with an older pg_dump refuses outright, which
# is the kind of thing that surfaces at the worst possible time.
set -eu

DIR="${BACKUP_DIR:-/backups}"
INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-6}"
KEEP="${BACKUP_KEEP:-28}"
DB_HOST="${POSTGRES_HOST:-postgres}"
DB_USER="${POSTGRES_USER:-percepta}"
DB_NAME="${POSTGRES_DB:-percepta}"

INTERVAL_SECONDS=$((INTERVAL_HOURS * 3600))

echo "Backups: every ${INTERVAL_HOURS}h, keeping ${KEEP}, into ${DIR}"
echo "Reminder: these dumps are useless without SECRETS_ENCRYPTION_KEY, and"
echo "every station must be re-enrolled without certs/ca.key. Neither is in here."

mkdir -p "$DIR"

# On start, wait a short while rather than dumping immediately - a container
# that restart-loops for another reason should not also hammer the database.
sleep 30

while true; do
  STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  OUT="$DIR/percepta-$STAMP.sql.gz"
  TMP="$OUT.partial"

  if pg_dump -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" \
       --clean --if-exists --no-owner 2>/tmp/dump.err | gzip -9 > "$TMP"; then
    # Verified before it is allowed to look like a backup: gunzips cleanly, and
    # contains a table we know exists. Catches the case that matters - a run
    # that "succeeded" against a database that was up but empty.
    if gzip -t "$TMP" 2>/dev/null && zgrep -qm1 'CREATE TABLE public.ground_stations' "$TMP"; then
      mv "$TMP" "$OUT"
      chmod 600 "$OUT"
      echo "$(date -u +%FT%TZ) wrote $(du -h "$OUT" | cut -f1) $OUT"
    else
      rm -f "$TMP"
      echo "$(date -u +%FT%TZ) FAILED verification - nothing written" >&2
    fi
  else
    rm -f "$TMP"
    echo "$(date -u +%FT%TZ) pg_dump FAILED: $(tail -1 /tmp/dump.err 2>/dev/null)" >&2
  fi

  # Prune by count, not age: a stack that has been off for a month should still
  # have its last backups when it comes back.
  ls -1t "$DIR"/percepta-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    echo "$(date -u +%FT%TZ) pruning $old"
    rm -f "$old"
  done

  sleep "$INTERVAL_SECONDS"
done
