#!/usr/bin/env bash
# Run on your dev PC (needs Node 18+). Builds the React UI and packs everything the VM needs.
#   bash deploy/make-bundle.sh      ->  ../erag-bundle-<date>.tgz
# The VM has no Node: it only receives the built frontend/dist.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "Building the UI..."
(cd frontend && npm ci && npm run build)

out="../erag-bundle-$(date +%Y%m%d-%H%M%S).tgz"
tar czf "$out" \
  --exclude='.venv' --exclude='node_modules' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='.ruff_cache' --exclude='data' --exclude='.env' --exclude='frontend/src' \
  --transform 's,^\./,erag/,' ./
echo "Bundle: $(cd .. && pwd)/$(basename "$out")"
echo "Next: scp it to the VM, 'tar xzf' it, then 'sudo bash erag/deploy/install.sh --domain YOUR.DOMAIN'"
