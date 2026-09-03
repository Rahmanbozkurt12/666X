@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "side_color.py" (
  echo [HATA] side_color.py bu klasorde yok.
  echo bookmap_bridge.py ile ayni klasore kopyala.
  pause
  exit /b 1
)

echo Bookmap koprusu basliyor...
echo NOT: book.py / "bot tr.py" dosyasini burada CALISTIRMA.
echo      book.py sadece Bookmap code editor icinde Enable edilir.
echo.

python bookmap_bridge.py %*
pause
