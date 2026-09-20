# PRO CEX Trader — Binance al/sat

## En kolay (tek dosya)

`pro_cex_radar` / satır 46 hatası alıyorsan **bunu kullan**:

```powershell
python pro_cex_bot.py --once --dry-run --fast
```

Tek dosya; başka py gerekmez.

## İki dosyalı kullanım

Aynı klasörde `pro_cex_radar.py` + `pro_cex_trader.py` olmalı.

## Kurallar

| | |
|--|--|
| Alış | Sadece 🟢 YÜKSEK |
| Max | **10 coin** |
| Bakiye | Eşit bölünür |
| Satış | SL · TP1 %50 · TP2 |

## Canlı

```powershell
$env:LIVE="1"
$env:BINANCE_API_KEY="..."
$env:BINANCE_API_SECRET="..."
python pro_cex_bot.py --once
```
