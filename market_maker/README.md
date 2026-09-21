# Binance Market Maker Bot (açık kaynak)

Kaynak: https://github.com/yuthavithi/binance-makret-maker-bot (MIT)

**Varsayılan:** canlı Binance spot + **USDT bakiyesini 8'e böl** → 8 likit pair'de paralel limit AL/SAT.

## Kurulum
```bash
cd market_maker
pip install -r requirements.txt
```

## Canlı (8 slot)
1. Binance API (Spot Trade) key/secret → `market_maker_config.json` veya env
2. `"testnet": false`, `"balance_slots": 8`, `"split_live_balance": true`
3. Pair listesi (`symbols`) — varsayılan:
   `BTC ETH BNB SOL XRP DOGE ADA AVAX` /USDT
4. Çalıştır:
```bash
python binance_market_maker_bot_3.py
```

Örnek: 800 USDT serbest → her coin **~100 USDT** slot ile AL (limit) / elinde base varsa SAT.

Durdurmak: `Ctrl+C` (tüm açık emirler iptal).

## Pair değiştir
`market_maker_config.json` → `exchange.symbols` listesini düzenle (en fazla `balance_slots` kadar kullanılır).
