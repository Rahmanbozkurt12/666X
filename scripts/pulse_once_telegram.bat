@echo off
REM Windows Gorev Zamanlayici icin: saatte 1 kez calisir, mesaj atar, cikar.
cd /d "%~dp0\.."
if not exist ".env" exit /b 1
python btc_eth_pulse.py --once --telegram
exit /b %ERRORLEVEL%
