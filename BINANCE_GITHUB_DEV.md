# Binance × GitHub Dev Anomaly Scanner

Binance USDT coinlerini tarar, resmi GitHub reposunu eşleştirir, **7/30/90** aktivite anomalisini skorlar, hacim/CVD/order-book ile birleştirir.

> **Önemli:** Bu otomatik alım botu değildir. Eski skor tablosu mega-cap gürültüsüyle zarar üretebilir.  
> Sadece `tier=ACTIONABLE` adaylarıyla (ve stop ile) düşün. Detay: [`WHY_LOSS.md`](WHY_LOSS.md)

## Tier’lar

- `ACTIONABLE` — gerçek commit/release spike + market confluence
- `WATCH` — izle
- `NOISE` — işlem yok (veto nedeni raporda)

## Run

```bash
export GITHUB_TOKEN=...
export BINANCE_API_BASE=https://data-api.binance.vision

# varsayılan: trade mode, mega-cap hariç
python3 binance_github_dev_scanner.py --mode trade --top 50 --skip-coingecko

# mid-cap
python3 binance_github_dev_scanner.py --mode trade --rank-from 20 --rank-to 100 --skip-coingecko
```

Rapor: `output/binance_github_dev_report.json` → `trade_candidates`
