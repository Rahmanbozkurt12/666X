# PRO CEX Trader — Binance al/sat

Radar **YÜKSEK** sinyallerini Binance spot’ta alır; bakiyeyi en fazla **10 coine** eşit böler.

## Güvenlik

- Varsayılan: **DRY-RUN** (gerçek emir yok)
- Canlı: `LIVE=1` + API key (spot trade yetkisi)

## Kurallar

| | |
|--|--|
| Alış | Sadece 🟢 YÜKSEK |
| Max pozisyon | **10** |
| Bakiye | Serbest USDT × 0.95 → bu turdaki coine eşit |
| Satış | SL tam · TP1 %50 · TP2 kalan |

## Çalıştır

```powershell
# Paper test
$env:PAPER_USDT="1000"
python pro_cex_trader.py --once --dry-run --fast

# Canlı (DİKKAT)
$env:LIVE="1"
$env:BINANCE_API_KEY="..."
$env:BINANCE_API_SECRET="..."
python pro_cex_trader.py --once
```

Pozisyonlar: `output/pro_cex_positions.json`  
İşlem log: `output/pro_cex_trades.jsonl`
