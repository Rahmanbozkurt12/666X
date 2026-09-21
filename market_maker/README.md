# Binance Spot Market Maker — CANLI

**Testnet yok.** Gerçek Binance spot AL/SAT.

## Ne yapar
1. Serbest USDT bakiyesini **8’e böler**
2. 8 pair’de limit **bid + ask** koyar (Post-Only / GTX)
3. Spread ≥ komisyon×2×safety + min edge — fee’ye ezilmez
4. Slot drawdown `%5` → kill switch (emirler iptal)

Varsayılan pairler: BTC ETH BNB SOL XRP DOGE ADA AVAX /USDT

## Kurulum (Windows)
```powershell
cd market_maker
pip install -r requirements.txt
```

`market_maker_config.json` düzenle:
```json
"api_key": "GERCEK_KEY",
"api_secret": "GERCEK_SECRET",
"testnet": false
```

Çalıştır:
```powershell
python binance_market_maker_bot_3.py
```

Doğru banner: `Binance Market Maker — CANLI AL/SAT (testnet YOK)`

Durdur: `Ctrl+C`

## İndir
https://github.com/Rahmanbozkurt12/666X/archive/refs/heads/cursor/mev-copy-delayed-d7b1.zip  
→ içinden `market_maker` klasörünü kullan.
