@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo GitHub'dan guncel allbinancee.py indiriliyor...
if not exist output mkdir output
powershell -NoProfile -Command ^
  "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/Rahmanbozkurt12/666X/cursor/cex-5m-volume-bottom-d7b1/allbinancee.py' -OutFile 'allbinancee.py' -UseBasicParsing; Copy-Item -Force allbinancee.py output\allbinancee.py; Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/Rahmanbozkurt12/666X/cursor/cex-5m-volume-bottom-d7b1/AL_SAT_CALISTIR.bat' -OutFile 'AL_SAT_CALISTIR.bat' -UseBasicParsing"
echo.
echo TAMAM. Simdi allbinancee.py ac:
echo   BINANCE_API_KEY_HARDCODE = "senin_key"
echo   BINANCE_API_SECRET_HARDCODE = "senin_secret"
echo Kaydet, sonra:  python allbinancee.py --once
echo.
pause
