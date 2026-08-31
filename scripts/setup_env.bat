@echo off
REM Ilk kurulum: .env ornegini kopyalar (varsa dokunmaz)
cd /d "%~dp0\.."
if exist ".env" (
  echo .env zaten var.
) else (
  copy /Y ".env.example" ".env" >nul
  echo .env olusturuldu. Notepad ile TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID doldur.
  notepad ".env"
)
echo.
echo Test icin: python btc_eth_pulse.py --once --telegram
pause
