# Market Radar

CEX FOMO / short-squeeze / dump-risk **uyarı** tarayıcısı.

## Kritik uyarı
- **Otomatik al-sat yok**
- Yanlış sinyal olabilir
- Bu kod para kaybını engellemez
- Yatırım tavsiyesi değildir

## Çalıştır
```bash
# güvenli: sadece konsol
python market_radar.py --once --dry-run

# Telegram (önce .env)
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python market_radar.py --once --live-telegram

# sürekli
python market_radar.py --live-telegram
```

Config: `config/market_radar.json`  
Çıktı: `output/market_radar_last.json`

## Self-check
```bash
python tests/test_market_radar.py
```
