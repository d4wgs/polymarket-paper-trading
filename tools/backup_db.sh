#!/usr/bin/env bash
# Nightly, on the Pi (cron): consistent online copy of the live database -> data/backups/paper_us-YYYY-MM-DD.db.gz
# Uses SQLite's .backup (safe while the bot is writing). Keeps the newest $KEEP copies on the Pi.
#   crontab:  30 3 * * *  /home/sujayketh/polymarket-paper/tools/backup_db.sh >> /home/sujayketh/polymarket-paper/data/backup.log 2>&1
# The copy OFF the Pi is made by tools/pull_backup.ps1 on the PC.
set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${DB:-$APP/data/paper_us.db}"
OUT="$APP/data/backups"
KEEP="${KEEP:-3}"
mkdir -p "$OUT"
DAY="$(date +%F)"
TMP="$OUT/.tmp-$DAY.db"
rm -f "$TMP"
sqlite3 "$DB" ".backup '$TMP'"
[ "$(sqlite3 "$TMP" 'PRAGMA integrity_check;')" = "ok" ] || { echo "$(date) integrity check FAILED"; rm -f "$TMP"; exit 1; }
gzip -f "$TMP"
mv "$TMP.gz" "$OUT/paper_us-$DAY.db.gz"
ls -1t "$OUT"/paper_us-*.db.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "$(date) backup ok: $OUT/paper_us-$DAY.db.gz ($(du -h "$OUT/paper_us-$DAY.db.gz" | cut -f1))"
