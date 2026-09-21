# Wallet copy + ana bot

## Ana bot (`allbinancee.py`)
Aynı klasöre **ikisini birden** koy:
- `allbinancee.py`
- `mev_copy_trader.py`

Trade açıkken her turda `[wallet_copy]` çalışır → 60 sn hold → Binance AL/SAT → `positions.json`.

```bash
python allbinancee.py
```

Kapat: `wallet_copy.enabled = false`

## Sadece copy
```bash
python mev_copy_trader.py
```
