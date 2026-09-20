# Tek dosya: gerçek Binance al/sat

## 1) İndir
https://raw.githubusercontent.com/Rahmanbozkurt12/666X/cursor/cex-5m-volume-bottom-d7b1/allbinancee.py

veya `KODU_INDIR.bat`

## 2) Key doldur (dosyanın üstü)

```python
BINANCE_API_KEY_HARDCODE = "senin_api_key"
BINANCE_API_SECRET_HARDCODE = "senin_secret_key"
LIVE_HARDCODE = True
TRADE_HARDCODE = True
```

`BURAYA_API_KEY` yazısını silip kendi key’ini yaz.

## 3) Çalıştır

```powershell
cd C:\Users\Rahman\OneDrive\Desktop\bot
python allbinancee.py --once
```

veya `output` klasöründeyse:

```powershell
python c:\Users\Rahman\OneDrive\Desktop\bot\output\allbinancee.py --once
```

Konsolda görmelisin:
- `[keys] kaynak=DOSYA`
- `[MODE] ⚠️ LIVE`
- `[account] USDT free ≈ ...`

`-1022` = Secret yanlış. Yeni secret kopyala.
