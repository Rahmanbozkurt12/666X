@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo GitHub'dan GUNCEL allbinancee.py indiriliyor...
echo Branch: cursor/high-winrate-edge-d7b1
if not exist output mkdir output
if not exist config mkdir config
powershell -NoProfile -Command ^
  "$b='https://raw.githubusercontent.com/Rahmanbozkurt12/666X/cursor/high-winrate-edge-d7b1'; Invoke-WebRequest -Uri \"$b/allbinancee.py\" -OutFile 'allbinancee.py' -UseBasicParsing; Copy-Item -Force allbinancee.py output\allbinancee.py; Invoke-WebRequest -Uri \"$b/config/binance_dip_buy_radar.json\" -OutFile 'config\binance_dip_buy_radar.json' -UseBasicParsing; if (Test-Path 'AL_SAT_CALISTIR.bat') { Invoke-WebRequest -Uri \"$b/AL_SAT_CALISTIR.bat\" -OutFile 'AL_SAT_CALISTIR.bat' -UseBasicParsing -ErrorAction SilentlyContinue }"
echo.
if exist allbinancee.py (
  echo TAMAM: allbinancee.py indirildi.
) else (
  echo HATA: indirme basarisiz. Asagidaki linki tarayicida ac:
  echo https://raw.githubusercontent.com/Rahmanbozkurt12/666X/cursor/high-winrate-edge-d7b1/allbinancee.py
)
echo.
echo 1^) allbinancee.py ac
echo 2^) BINANCE_API_KEY_HARDCODE / SECRET doldur
echo 3^) python allbinancee.py
echo.
pause
