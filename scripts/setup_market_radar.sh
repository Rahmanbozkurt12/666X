#!/usr/bin/env bash
# Minimal, safe setup for Market Radar (alerts only — no trading).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== pip =="
python3 -m pip install -q -r requirements.txt

echo "== self-check =="
PYTHONPATH=. python3 tests/test_market_radar.py

echo "== health =="
PYTHONPATH=. python3 market_radar.py --health

echo "== dry-run once =="
PYTHONPATH=. python3 market_radar.py --once --dry-run

echo
echo "OK — kurulum tamam."
echo "Canlı Telegram için:"
echo "  export TELEGRAM_BOT_TOKEN=..."
echo "  export TELEGRAM_CHAT_ID=..."
echo "  python3 market_radar.py --once --live-telegram"
echo
echo "NOT: Otomatik al-sat yok. Yanlış sinyal / para kaybı riski size aittir."
