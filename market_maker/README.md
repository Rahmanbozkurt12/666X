# Binance Market Maker Bot (açık kaynak)

Kaynak: https://github.com/yuthavithi/binance-makret-maker-bot (MIT)

Varsayılan: **CANLI** Binance spot (`testnet: false`). Gerçek USDT ile limit AL+SAT.

## Kurulum
```bash
cd market_maker
pip install -r requirements.txt
```

## Canlı (gerçek para)
1. https://www.binance.com → API Management → Spot Trade izinli key oluştur
2. `market_maker_config.json` içine `api_key` / `api_secret` yaz  
   (veya `export BINANCE_API_KEY=... BINANCE_API_SECRET=...`)
3. `"testnet": false` kalsın
4. `total_capital` = gerçek sermayen (küçük başla, örn. 50–100 USDT)
5. Çalıştır:
```bash
python binance_market_maker_bot_3.py
```
Durdurmak: `Ctrl+C` (açık emirler iptal edilir).

## Testnet (sahte para)
`"testnet": true` + https://testnet.binance.vision API key.

## Ne yapar
Order book mid etrafında Avellaneda–Stoikov / volatilite spread ile limit bid+ask. Trend tahmin etmez; envanter + drawdown kill-switch var.
