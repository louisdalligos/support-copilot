#!/bin/sh
# Back up both databases on a schedule; keep BACKUP_KEEP_DAYS days of dumps.
#
# Runs in the `backup` service of docker-compose.prod.yml. Dumps are custom-format
# (pg_dump -Fc), written to a temporary name first so a half-written file is never
# mistaken for a good backup. Restore with:
#
#   docker compose exec -T postgres pg_restore --clean --if-exists -U copilot -d copilot < backups/copilot-<stamp>.dump

set -eu

KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"

while true; do
  stamp="$(date -u +%Y-%m-%dT%H%MZ)"
  for db in copilot phoenix; do
    out="/backups/${db}-${stamp}.dump"
    if pg_dump -Fc --dbname="$db" --file="${out}.partial"; then
      mv "${out}.partial" "$out"
      echo "backup ok: $(basename "$out") ($(du -h "$out" | cut -f1))"
    else
      rm -f "${out}.partial"
      echo "backup FAILED for $db" >&2
    fi
  done
  find /backups -name '*.dump' -mtime "+${KEEP_DAYS}" -print -delete
  sleep "$INTERVAL"
done
