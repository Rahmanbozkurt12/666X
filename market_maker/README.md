# Binance Market Maker Bot (açık kaynak)

Kaynak: https://github.com/yuthavithi/binance-makret-maker-bot (MIT)

## Kurulum
```bash
cd market_maker
pip install -r requirements.txt
```

## Testnet (sahte para) — ÖNCE BUNU
1. https://testnet.binance.vision → GitHub login → API Key
2. `market_maker_config.json` içine key/secret yaz
3. `"testnet": true` kalsın
4. `python binance_market_maker_bot_3.py`

## Canlı (gerçek para)
`"testnet": false` + gerçek Binance API (Spot izinli). Küçük sermaye ile dene.

Ne yapar: order book mid etrafında limit AL+SAT (spread). Trend tahmin etmez.
