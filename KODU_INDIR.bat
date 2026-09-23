@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo  COKLU SCALP kodu indiriliyor (10 slot)
echo ========================================

set "DEST=%USERPROFILE%\OneDrive\Desktop\bot\output"
if not exist "%DEST%" set "DEST=%USERPROFILE%\Desktop\bot\output"
if not exist "%DEST%" mkdir "%DEST%" 2>nul
if not exist "%DEST%\config" mkdir "%DEST%\config" 2>nul

set "BASE=https://raw.githubusercontent.com/Rahmanbozkurt12/666X/cursor/high-winrate-edge-d7b1"
set "OUT=%DEST%\tum_borsalar_prof_al_sat.py"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; $b='%BASE%'; $out='%OUT%'; $dest='%DEST%'; Write-Host ('Hedef: ' + $out) -ForegroundColor Cyan; Invoke-WebRequest -Uri ($b+'/allbinancee.py') -OutFile $out -UseBasicParsing; Copy-Item -Force $out (Join-Path $dest 'allbinancee.py'); try { New-Item -ItemType Directory -Force -Path (Join-Path $dest 'config') | Out-Null; Invoke-WebRequest -Uri ($b+'/config/binance_dip_buy_radar.json') -OutFile (Join-Path $dest 'config\binance_dip_buy_radar.json') -UseBasicParsing } catch {}; $sz=(Get-Item $out).Length; Write-Host ('Boyut: ' + $sz + ' byte'); if (Select-String -Path $out -Pattern 'max_positions.: 10' -Quiet) { Write-Host 'OK: max_positions=10 / coklu alim' -ForegroundColor Green } else { Write-Host 'UYARI: yeni ayar yok' -ForegroundColor Yellow }; if (Select-String -Path $out -Pattern 'alpha_filters' -Quiet) { Write-Host 'OK: alpha_filters var' -ForegroundColor Green }"

echo.
echo Calistir:
echo   python "%OUT%"
echo.
echo Veya VS Code'da ac: %OUT%
echo.
pause
