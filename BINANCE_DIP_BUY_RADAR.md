# Binance Dip AL Radarı v2

Tüm Binance USDT spot coinlerini tarar; **yükselmeden dipteyken** 🟢 AL verir.

## Yeni (v2)

- **BTC rejim filtresi** — BTC sert düşüyorsa AL engellenir / skor düşer
- **Futures funding** — negatif funding bonusu (API erişilebilirse)
- **Stop / TP1 / TP2 / RR** — her AL için risk önerisi
- **Zengin Telegram** — skor + SL/TP + funding
- **`--backtest`** — geçmişte sinyal verseydi ne olurdu?

## Çalıştırma

```powershell
cd C:\Users\Rahman\OneDrive\Desktop\bot
python binance_dip_buy_radar.py --once --dry-run --top 20
python binance_dip_buy_radar.py --backtest --backtest-symbols 40
python binance_dip_buy_radar.py
```

Config yoksa gömülü varsayılanla çalışır (`output/` içinden de OK).

## Sinyaller

| | |
|--|--|
| 🟢 AL | dip + hacim + erken momentum |
| 🟡 İZLE | gelişiyor / BTC rejim bekle |
| 🔴 GEÇ | zaten yükselmiş (AR +%50) |
