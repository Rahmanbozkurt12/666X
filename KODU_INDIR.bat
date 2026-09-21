@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo  ALPHA kodu indiriliyor (taker/basis/lead)
echo  Branch: cursor/high-winrate-edge-d7b1
echo ========================================
if not exist output mkdir output
if not exist config mkdir config

set "BASE=https://raw.githubusercontent.com/Rahmanbozkurt12/666X/cursor/high-winrate-edge-d7b1"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; $b='%BASE%'; Write-Host 'Indiriliyor...' -ForegroundColor Cyan; Invoke-WebRequest -Uri \"$b/allbinancee.py\" -OutFile 'allbinancee.py' -UseBasicParsing; Copy-Item -Force allbinancee.py 'output\allbinancee.py'; Invoke-WebRequest -Uri \"$b/config/binance_dip_buy_radar.json\" -OutFile 'config\binance_dip_buy_radar.json' -UseBasicParsing; try { Invoke-WebRequest -Uri \"$b/AL_SAT_CALISTIR.bat\" -OutFile 'AL_SAT_CALISTIR.bat' -UseBasicParsing } catch {}; $sz=(Get-Item 'allbinancee.py').Length; Write-Host (\"Boyut: $sz byte\"); if (Select-String -Path 'allbinancee.py' -Pattern 'alpha_filters' -Quiet) { Write-Host 'ALPHA VAR: taker + basis + lead-lag' -ForegroundColor Green } else { Write-Host 'UYARI: alpha_filters bulunamadi - eski dosya olabilir' -ForegroundColor Yellow }"

echo.
if exist allbinancee.py (
  echo TAMAM: allbinancee.py indirildi.
  echo.
  echo Calistirma:
  echo   1^) allbinancee.py ac
  echo   2^) BINANCE_API_KEY_HARDCODE / SECRET doldur
  echo   3^) python allbinancee.py
  echo.
  echo Baslarken su satiri gormelisin:
  echo   [alpha] taker^>%%58 + basis prem + lead bybit,okx
) else (
  echo HATA: indirme basarisiz.
  echo Tarayicida ac / Kaydet:
  echo %BASE%/allbinancee.py
)
echo.
pause
