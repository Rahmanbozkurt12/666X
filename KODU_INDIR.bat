@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo Eski binance_dip_buy_radar.py yedekleniyor...
if exist binance_dip_buy_radar.py (
  copy /Y binance_dip_buy_radar.py binance_dip_buy_radar.ESKI.py >nul
)
echo GitHub'dan GUNCEL kod indiriliyor (al/sat dahil ~2200 satir)...
powershell -NoProfile -Command ^
  "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/Rahmanbozkurt12/666X/cursor/cex-5m-volume-bottom-d7b1/binance_dip_buy_radar.py' -OutFile 'binance_dip_buy_radar.py' -UseBasicParsing; Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/Rahmanbozkurt12/666X/cursor/cex-5m-volume-bottom-d7b1/AL_SAT_CALISTIR.bat' -OutFile 'AL_SAT_CALISTIR.bat' -UseBasicParsing; Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/Rahmanbozkurt12/666X/cursor/cex-5m-volume-bottom-d7b1/config/binance_dip_buy_radar.json' -OutFile 'config\binance_dip_buy_radar.json' -UseBasicParsing"
if errorlevel 1 (
  echo INDIRME BASARISIZ — internet / GitHub erisimini kontrol et
  pause
  exit /b 1
)
echo.
echo TAMAM. Simdi dosyada key yaz:
echo   BINANCE_API_KEY_HARDCODE = "..."
echo   BINANCE_API_SECRET_HARDCODE = "..."
echo   LIVE_HARDCODE = True
echo.
echo Sonra: AL_SAT_CALISTIR.bat
echo.
pause
