#!/usr/bin/env bash
# Back up the SQLite database and the stored files (raw uploads, extracted images, captions).
# Qdrant vectors are NOT included: they live in Qdrant Cloud, and can be rebuilt by re-ingesting
# from the raw files in this backup.
#
# Settings (environment, or /etc/erag-backup.env when run by the systemd timer):
#   APP_DIR        default /opt/erag
#   BACKUP_DIR     default /var/backups/erag
#   KEEP           how many backups to keep, default 7
#   AGE_RECIPIENT  an `age` public key: if set (and `age` is installed) the archive is encrypted
#   GCS_BUCKET     optional bucket name: copy the result there with gsutil (off-VM copy)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/erag}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/erag}"
KEEP="${KEEP:-7}"
# First interpreter that really runs (a stub named python3 can exist without Python behind it).
PY=""
for cand in "$APP_DIR/.venv/bin/python" python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sqlite3' >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -n "$PY" ] || { echo "No working Python found"; exit 1; }

DATA="$APP_DIR/data"
[ -f "$DATA/app.db" ] || { echo "No database at $DATA/app.db"; exit 1; }
umask 077
mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%d-%H%M%S)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# A consistent copy of the live database (plain cp of a busy SQLite file can be corrupt).
"$PY" - "$DATA/app.db" "$tmp/app.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY

dirs=()
for d in raw assets captions; do [ -d "$DATA/$d" ] && dirs+=("$d"); done
out="$BACKUP_DIR/erag-$stamp.tgz"
tar czf "$out" -C "$DATA" "${dirs[@]}" -C "$tmp" app.db

if [ -n "${AGE_RECIPIENT:-}" ]; then
  command -v age >/dev/null || { echo "AGE_RECIPIENT is set but 'age' is not installed"; rm -f "$out"; exit 1; }
  age -r "$AGE_RECIPIENT" -o "$out.age" "$out"
  rm -f "$out"
  out="$out.age"
else
  echo "NOTE: backup is not encrypted (set AGE_RECIPIENT to encrypt)."
fi

if [ -n "${GCS_BUCKET:-}" ]; then
  command -v gsutil >/dev/null && gsutil -q cp "$out" "gs://$GCS_BUCKET/" || echo "WARNING: off-VM copy skipped (gsutil missing or failed)"
fi

# Keep the newest $KEEP archives.
ls -1t "$BACKUP_DIR"/erag-*.tgz* 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do rm -f "$old"; done
echo "Backup written: $out ($(du -h "$out" | cut -f1))"
