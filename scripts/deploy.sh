#!/usr/bin/env bash
# Safe deploy: tests gate everything. Never pushes or restarts on red.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== tests ==="
python3 -m pytest tests/ -q
echo "=== tests green, committing (if needed) ==="
git add -A
if ! git diff --cached --quiet; then
    msg="${1:-chore: deploy}"
    git commit -m "$msg"
fi
echo "=== push ==="
git push origin main
echo "=== VM pull + restart ==="
ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=no Mumbai-Bot \
    "git -C /home/ubuntu/kotaktegramv2 pull --ff-only && pm2 restart kotak-trader"
echo "=== health ==="
sleep 45
ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=no Mumbai-Bot \
    "curl -s -m 15 'http://127.0.0.1:8001/api/autotrender/engine-status' | python3 -c 'import json,sys; json.load(sys.stdin); print(\"engine-status ok\")'"
echo "=== deployed ==="
