# 666X  
The official repository for the 666X meme token project

## Binance Spot bot

`binance_spot_bot.py` — EMA9/21 + RSI anlık alım-satım (varsayılan **dry-run**).

```bash
pip install -r requirements.txt
python3 binance_spot_bot.py --once          # tek tur paper
python3 binance_spot_bot.py                 # sürekli paper
# LIVE: .env içine BINANCE_API_KEY/SECRET, sonra --live
```

Config: `config/binance_spot_bot.json` · State: `output/binance_spot_bot_state.json`
