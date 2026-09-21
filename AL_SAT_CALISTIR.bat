@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo === Binance GERCEK AL/SAT ===
echo Once allbinancee.py icinde API KEY + SECRET doldur.
echo.
if exist allbinancee.py (
  python allbinancee.py --once
) else if exist binance_dip_buy_radar.py (
  python binance_dip_buy_radar.py --once
) else if exist output\allbinancee.py (
  python output\allbinancee.py --once
) else (
  echo allbinancee.py bulunamadi
)
echo.
pause
