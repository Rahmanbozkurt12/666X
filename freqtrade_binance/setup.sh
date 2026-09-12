#!/usr/bin/env bash
# Download successful open-source Binance stack pieces:
#   Freqtrade engine (Docker) + NostalgiaForInfinityX7 strategy (5m)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
STRAT_DIR="$ROOT/user_data/strategies"
STRAT_FILE="$STRAT_DIR/NostalgiaForInfinityX7.py"
NFI_URL="https://raw.githubusercontent.com/iterativv/NostalgiaForInfinity/main/NostalgiaForInfinityX7.py"

mkdir -p "$STRAT_DIR" "$ROOT/user_data/logs" "$ROOT/user_data/data"

echo "==> Downloading NostalgiaForInfinityX7 (successful 5m strategy)…"
curl -fsSL "$NFI_URL" -o "$STRAT_FILE.tmp"
BYTES=$(wc -c < "$STRAT_FILE.tmp" | tr -d ' ')
if [[ "$BYTES" -lt 100000 ]]; then
  echo "ERROR: strategy download looks too small ($BYTES bytes)" >&2
  rm -f "$STRAT_FILE.tmp"
  exit 1
fi
mv "$STRAT_FILE.tmp" "$STRAT_FILE"
echo "    saved $STRAT_FILE ($BYTES bytes)"

# Also place at compose mount path expected by docker-compose
# (same file; compose binds strategies/NostalgiaForInfinityX7.py)

if ! command -v docker >/dev/null 2>&1; then
  echo "==> docker not found. Install Docker to run: docker compose up -d"
else
  echo "==> Docker OK. Start dry-run with:"
  echo "      cd $ROOT && docker compose up -d"
fi

echo
echo "Config is dry_run=true. Live needs API keys in configs/exampleconfig_secret.json"
echo "Strategy: GPL-3.0 (NostalgiaForInfinity) | Engine: GPL-3.0 (Freqtrade)"
