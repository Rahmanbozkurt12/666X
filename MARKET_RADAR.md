# Market Radar (hardened)

CEX FOMO / short-squeeze / dump-risk **uyarı** sistemi.

## Güvenlik katmanları
- Otomatik al-sat **yok**
- Varsayılan **dry-run**
- Daha sıkı eşikler (vol/chg/funding/OI)
- `min_severity_to_alert` (varsayılan 4)
- Squeeze için **taker buy** zorunlu
- Noise kind’lar mute (`CEX_ORDERBOOK`)
- Config validate + HTTP retry/backoff + fail-soft
- Cooldown 6 saat + cycle başına max 8 alert

## Kur
```bash
bash scripts/setup_market_radar.sh
```

## Kullan
```bash
python3 market_radar.py --health
python3 market_radar.py --once --dry-run
python3 tests/test_market_radar.py
```

Telegram:
```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python3 market_radar.py --once --live-telegram
```

**Bu yazılım para kaybını engellemez.** Sadece araştırma uyarısıdır.
