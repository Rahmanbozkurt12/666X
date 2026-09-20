@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === PAPER test (gercek emir YOK) ===
python binance_dip_buy_radar.py --once --trade --dry-run --skip-multi-cex --fast
echo.
pause
