# 666X  
The official repository for the 666X meme token project

## Binance trail-exit bot

`binance_trail_bot.py` — Z-Score/ADX tarama + **zirve trail** çıkış:

- Yükselişte sabit +1% satmaz; peak takip eder
- Peak’ten `%0.020` düşünce kârdan satar (`--trail-pct`)
- Alıştan `%0.50` düşünce stop satar (`--stop-pct`)

```bash
pip install -r requirements.txt
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...
python3 binance_trail_bot.py              # dry-run (varsayılan)
python3 binance_trail_bot.py --live       # gerçek emir
```
