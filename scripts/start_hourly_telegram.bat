@echo off
REM BTC/ETH saatlik Telegram ozeti
REM Bilgisayar acik kaldigi surece her 1 saatte bir mesaj atar.
REM Bookmap gerekmez - OKX public veri kullanir.

cd /d "%~dp0\.."

if not exist ".env" (
  echo [.env yok] Once .env.example dosyasini .env yapip TELEGRAM bilgilerini doldur.
  pause
  exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
  echo Python bulunamadi. https://www.python.org/downloads/ kur ve PATH'e ekle.
  pause
  exit /b 1
)

echo Saatlik pulse basliyor. Pencereyi kapatma. Durdurmak icin Ctrl+C.
python btc_eth_pulse.py --hourly
pause
