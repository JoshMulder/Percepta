#!/usr/bin/env bash
# Back up the Percepta database.
#
#   ./scripts/backup.sh                    # write a dump into backups/
#   PERCEPTA_BACKUP_DIR=/mnt/x ./scripts/backup.sh
#
# What is and is not covered, because a backup you misunderstand is worse than
# none at all:
#
#   Covered      Postgres: organisations, users, stations, grants, credentials,
#                audit log, power history.
#   NOT covered  SECRETS_ENCRYPTION_KEY and the private CA in certs/. Both live
#                outside the database on purpose. Restoring this dump without
#                the encryption key leaves every encrypted column unreadable,
#                and without the CA every station in the field must be
#                re-enrolled. Back those up separately, and NOT beside the dump
#                - a key stored next to the data it protects is not a control.
#   NOT covered  Redis. Everything in it is reconstructible: leases re-acquire,
#                revocations have a poll backstop, and station ACL users are
#                rebuilt from Postgres at start-up.
#   NOT covered  Cached map tiles. Large, and refetchable.
#
# Restoring is in docs/06-backup-and-restore.md. A backup nobody has restored is
# a hypothesis, so that document exists to be rehearsed rather than read.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="${PERCEPTA_BACKUP_DIR:-$HERE/backups}"
KEEP="${PERCEPTA_BACKUP_KEEP:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DIR/percepta-$STAMP.sql.gz"

mkdir -p "$DIR"
chmod 700 "$DIR"

# Read the database name and user from the environment the stack actually uses,
# so a backup cannot quietly target a different database than the one running.
cd "$HERE"
DB_USER="$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2- || echo percepta)"
DB_NAME="$(grep -E '^POSTGRES_DB=' .env 2>/dev/null | cut -d= -f2- || echo percepta)"
DB_USER="${DB_USER:-percepta}"
DB_NAME="${DB_NAME:-percepta}"

echo "Dumping $DB_NAME as $DB_USER -> $OUT"

# --clean --if-exists so the dump can be replayed over an existing database
# without hand-editing. Written through a temporary name and moved into place
# only on success, so an interrupted run never leaves a half-file that looks
# like a backup.
TMP="$OUT.partial"
if ! docker compose exec -T postgres pg_dump \
      -U "$DB_USER" -d "$DB_NAME" --clean --if-exists --no-owner \
      | gzip -9 > "$TMP"; then
  rm -f "$TMP"
  echo "Backup FAILED - nothing written." >&2
  exit 1
fi

# A dump that gunzips cleanly and mentions a table we know exists. Cheap, and it
# catches the case that matters: a "successful" run that captured nothing
# because the container was up but the database was not.
if ! gzip -t "$TMP" 2>/dev/null || ! zgrep -qm1 'CREATE TABLE public.ground_stations' "$TMP"; then
  rm -f "$TMP"
  echo "Backup FAILED verification - nothing written." >&2
  exit 1
fi

mv "$TMP" "$OUT"
chmod 600 "$OUT"
echo "Wrote $(du -h "$OUT" | cut -f1) to $OUT"

# Prune by count rather than age: a stack that has been off for a month should
# still have its last backups when it comes back.
mapfile -t OLD < <(ls -1t "$DIR"/percepta-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)))
if [ ${#OLD[@]} -gt 0 ]; then
  echo "Pruning ${#OLD[@]} backup(s) beyond the most recent $KEEP."
  rm -f "${OLD[@]}"
fi

echo
echo "Remember: this dump is useless without SECRETS_ENCRYPTION_KEY, and every"
echo "station must be re-enrolled without certs/ca.key. Store both elsewhere."
