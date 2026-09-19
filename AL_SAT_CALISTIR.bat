@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo === Binance Dip AL/SAT ===
echo Key + LIVE_HARDCODE=True dosyada olmali.
echo.
python binance_dip_buy_radar.py --trade --live
echo.
pause
