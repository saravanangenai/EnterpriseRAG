#!/usr/bin/env bash
# Restore a backup made by backup.sh.   sudo erag-restore /var/backups/erag/erag-<stamp>.tgz[.age]
# The current data folder is moved aside (not deleted) so you can undo.
#   AGE_IDENTITY  path to your age private key file (needed for .age archives)
#   APP_DIR       default /opt/erag
#   NO_SERVICE=1  skip stopping/starting the erag service (for testing)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/erag}"
ARCHIVE="${1:-}"
[ -f "$ARCHIVE" ] || { echo "Usage: erag-restore <backup file>"; exit 1; }
DATA="$APP_DIR/data"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

src="$ARCHIVE"
if [[ "$ARCHIVE" == *.age ]]; then
  [ -n "${AGE_IDENTITY:-}" ] || { echo "Set AGE_IDENTITY to your age private key file"; exit 1; }
  age -d -i "$AGE_IDENTITY" -o "$tmp/backup.tgz" "$ARCHIVE"
  src="$tmp/backup.tgz"
fi
tar tzf "$src" | grep -qx 'app.db' || { echo "Archive has no app.db: not an erag backup"; exit 1; }

[ "${NO_SERVICE:-0}" = "1" ] || systemctl stop erag
if [ -d "$DATA" ]; then
  aside="$DATA.before-restore-$(date -u +%Y%m%d-%H%M%S)"
  mv "$DATA" "$aside"
  echo "Previous data moved to $aside"
fi
mkdir -p "$DATA"
tar xzf "$src" -C "$DATA"
mkdir -p "$DATA/raw" "$DATA/assets" "$DATA/captions" "$DATA/cache"
if id erag >/dev/null 2>&1; then chown -R erag:erag "$DATA"; fi
chmod 750 "$DATA"
[ "${NO_SERVICE:-0}" = "1" ] || systemctl start erag
echo "Restored from $ARCHIVE."
echo "Qdrant vectors are not part of the backup. If the collection is empty or out of date, rebuild it:"
echo "  cd $APP_DIR && sudo -u erag .venv/bin/python -m scripts.reindex --all"
