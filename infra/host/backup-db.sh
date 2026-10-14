#!/bin/sh
# Daily dump of both PostgreSQL databases (Prefect + research_autopilot) on Eagle (docs/operations/recovery.md §5).
# Runs as rixyt from cron (Docker Desktop is rixyt's); passwords never reach the command line.
#   30 4 * * * $HOME/workspace/ResearchAutopilot/infra/host/backup-db.sh >> $HOME/backups/research-autopilot/backup.log 2>&1
set -eu

REPO="${RA_BACKUP_REPO:-$HOME/workspace/ResearchAutopilot}"
DEST="${RA_BACKUP_DIR:-$HOME/backups/research-autopilot}"
KEEP_DAYS="${RA_BACKUP_KEEP_DAYS:-14}"
COPY_DIR="${RA_BACKUP_COPY_DIR:-}"   # optional second location (e.g. a synced folder on another disk)

umask 077
mkdir -p "$DEST"
ts=$(date +%Y%m%dT%H%M%S)
cd "$REPO"
compose() { docker compose --env-file .env -f infra/compose.yaml -f infra/compose.host.yaml "$@"; }

for db in prefect research_autopilot; do
    target='"$POSTGRES_DB"'
    [ "$db" = research_autopilot ] && target=research_autopilot
    compose exec -T postgres sh -c "pg_dump -U \"\$POSTGRES_USER\" -d $target -Fc" > "$DEST/$db-$ts.dump.part"
    # a dump that pg_restore cannot list is not kept
    compose exec -T postgres pg_restore --list < "$DEST/$db-$ts.dump.part" > /dev/null
    mv "$DEST/$db-$ts.dump.part" "$DEST/$db-$ts.dump"
    if [ -n "$COPY_DIR" ]; then mkdir -p "$COPY_DIR" && cp "$DEST/$db-$ts.dump" "$COPY_DIR/"; fi
done
echo "git=$(git rev-parse --short HEAD) prefect=3.8.8 postgres=16.15" > "$DEST/meta-$ts.txt"

find "$DEST" -maxdepth 1 -name '*.dump' -mtime +"$KEEP_DAYS" -delete
find "$DEST" -maxdepth 1 -name 'meta-*.txt' -mtime +"$KEEP_DAYS" -delete
find "$DEST" -maxdepth 1 -name '*.part' -mtime +1 -delete
echo "$(date -Is) backup ok: $(ls "$DEST"/*-"$ts".dump | xargs -n1 basename | tr '\n' ' ')"
