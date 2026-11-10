#!/usr/bin/env bash
# Install or update Enterprise RAG on a small Debian/Ubuntu VM (tested target: 1 GB RAM).
# Run as root from the unpacked bundle:   sudo bash deploy/install.sh --domain rag.example.com
# Safe to re-run: it updates code and dependencies and never overwrites .env or data/.
set -euo pipefail

APP_DIR=/opt/erag
APP_USER=erag
DOMAIN=""
NO_HTTPS=0
SWAP_GB=2

usage() { echo "Usage: sudo bash deploy/install.sh (--domain NAME | --no-https) [--swap-gb N]"; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --no-https) NO_HTTPS=1; shift ;;
    --swap-gb) SWAP_GB="${2:-2}"; shift 2 ;;
    *) usage ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)."; exit 1; }
{ [ -n "$DOMAIN" ] || [ "$NO_HTTPS" -eq 1 ]; } || usage
command -v apt-get >/dev/null || { echo "This script supports Debian/Ubuntu (apt) only."; exit 1; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
log() { printf '\n== %s\n' "$*"; }

if [ ! -f "$SRC_DIR/frontend/dist/index.html" ]; then
  echo "WARNING: frontend/dist is missing, so the web UI will not be served (the API still works)."
  echo "         Build it on your PC with deploy/make-bundle.sh and ship that bundle."
fi

log "1/9 Swap ($SWAP_GB GB) - a 1 GB VM needs it while parsing documents"
if ! swapon --show | grep -q .; then
  if [ ! -f /swapfile ]; then
    fallocate -l "${SWAP_GB}G" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB * 1024))
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo 'vm.swappiness=10' > /etc/sysctl.d/99-erag.conf
sysctl -q -p /etc/sysctl.d/99-erag.conf || true

log "2/9 System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip rsync curl ca-certificates caddy
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"Python {sys.version.split()[0]} found; 3.11 or newer is required.")
PY

log "3/9 Service user"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"

log "4/9 Copy application code"
rsync -a --delete \
  --exclude '.venv' --exclude 'node_modules' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude 'data' --exclude '.env' --exclude 'tests' --exclude 'postman' --exclude 'docs' \
  --exclude 'frontend/src' --exclude 'frontend/package*.json' --exclude 'frontend/*.ts' \
  --exclude 'frontend/*.json' --exclude 'frontend/index.html' \
  "$SRC_DIR/" "$APP_DIR/"
# --delete above must not remove these, so they were excluded; make sure the dirs exist.
mkdir -p "$APP_DIR/data/raw" "$APP_DIR/data/assets" "$APP_DIR/data/captions" "$APP_DIR/data/cache"

log "5/9 Python environment (this takes a few minutes)"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/python" -m spacy download en_core_web_sm

log "6/9 Secrets file"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  secret="$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  sed -i "s|^JWT_SECRET=.*|JWT_SECRET=${secret}|" "$APP_DIR/.env"
  NEW_ENV=1
else
  NEW_ENV=0
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"
chmod 750 "$APP_DIR/data"

log "7/9 API service (systemd)"
install -m 644 "$SRC_DIR/deploy/erag.service" /etc/systemd/system/erag.service
systemctl daemon-reload
systemctl enable erag.service
systemctl restart erag.service

log "8/9 Caddy (reverse proxy, HTTPS)"
if [ "$NO_HTTPS" -eq 1 ]; then
  sed -e 's|{{SITE}}|:80|' -e '/{{HSTS}}/d' "$SRC_DIR/deploy/Caddyfile.template" > /etc/caddy/Caddyfile
  echo "WARNING: serving plain HTTP. Logins and tokens travel unencrypted. Use --domain for anything real."
else
  sed -e "s|{{SITE}}|${DOMAIN}|" \
      -e 's|{{HSTS}}|Strict-Transport-Security "max-age=31536000"|' \
      "$SRC_DIR/deploy/Caddyfile.template" > /etc/caddy/Caddyfile
fi
systemctl enable caddy
systemctl reload caddy || systemctl restart caddy

log "9/9 Nightly backup (systemd timer)"
install -m 755 "$SRC_DIR/deploy/backup.sh" /usr/local/bin/erag-backup
install -m 755 "$SRC_DIR/deploy/restore.sh" /usr/local/bin/erag-restore
install -m 644 "$SRC_DIR/deploy/erag-backup.service" /etc/systemd/system/erag-backup.service
install -m 644 "$SRC_DIR/deploy/erag-backup.timer" /etc/systemd/system/erag-backup.timer
install -d -m 700 -o "$APP_USER" -g "$APP_USER" /var/backups/erag
systemctl daemon-reload
systemctl enable --now erag-backup.timer

echo
echo "Done. Next steps:"
if [ "$NEW_ENV" -eq 1 ]; then
  echo "  1. Edit $APP_DIR/.env: add OPENAI_API_KEY, GROQ_API_KEY, QDRANT_URL, QDRANT_API_KEY"
  echo "     (JWT_SECRET was generated for you). Then: sudo systemctl restart erag"
else
  echo "  1. Existing $APP_DIR/.env kept as is."
fi
echo "  2. Create the first admin:"
echo "     cd $APP_DIR && sudo -u $APP_USER $APP_DIR/.venv/bin/python -m scripts.create_admin --username admin --role admin"
echo "  3. Check:  systemctl status erag caddy   and   curl -s http://127.0.0.1:8000/health"
echo "  4. Encrypted backups: see deploy/README.md (AGE_RECIPIENT)."
