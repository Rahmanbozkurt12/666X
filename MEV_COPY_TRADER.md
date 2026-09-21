# Tek dosya copy trader (canlı Binance)

`mev_copy_trader.py` içinde config + bot birleşik.

1. Dosya başında `BINANCE_API_KEY` / `BINANCE_API_SECRET` doldur  
2. `pip install ccxt requests`  
3. `python mev_copy_trader.py` → **CANLI** al-sat  

Test: `python mev_copy_trader.py --dry-run`

- Sadece Binance USDT listeli coin  
- Max `$max_copy_usd` (50)  
- 60 sn delay + hold filtresi  
- MEV aynı-tx gir-çık → skip  
