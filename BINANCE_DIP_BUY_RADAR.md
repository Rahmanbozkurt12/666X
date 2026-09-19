# Binance Dip AL Radarı

Tüm Binance USDT spot coinlerini tarar; **+%50 olduktan sonra değil**, dipten / yükselişin başında **🟢 AL** verir.

AR örneği: +%50’de haber geç kalmıştır. Bu radar, 14g dibe yakın + 5m hacim uyanışı + RSI toparlanma aşamasında yakalamayı hedefler.

## Analiz katmanları

| # | Katman | Ne bakıyor |
|---|--------|------------|
| 1 | Teknik dip | 14g dibe yakınlık |
| 2 | Hacim | 5m dipten hacim×, 30g hacim uyanışı |
| 3 | Momentum erken | 0→+, yeşil mum, higher-low |
| 4 | RSI | Toparlanma (25–55); >70 → GEÇ |
| 5 | EMA | 1h EMA7 kırılımı (erken) |
| 6 | Relative strength | BTC’ye göre erken güç |
| 7 | 24s pencere | Henüz şişmemiş (−25% … +8%) |
| 8 | Likidite | Min quote volume |

## Sinyaller

- **🟢 AL** — skor ≥ 62 + hacim dip + fiyat dipe yakın + 24s henüz kaçmamış
- **🟡 İZLE** — gelişiyor, onay eksik
- **🔴 GEÇ** — zaten yükselmiş / RSI aşırı alım (AR +%50 tipi)
- **⚪ YOK** — sinyal yok

## Çalıştırma

```bash
pip install -r requirements.txt
python3 binance_dip_buy_radar.py --once --dry-run --top 20
python3 binance_dip_buy_radar.py          # 3 dk loop + Telegram
```

Ayar: `config/binance_dip_buy_radar.json`  
Çıktı: `output/binance_dip_buy_signals.json`

Tek `.py` dosyasını `output/` içine kopyalasanız da çalışır:
config yoksa **gömülü varsayılan** ayarlar kullanılır.

## Önerilen klasör yapısı (Windows)

```
bot/
  binance_dip_buy_radar.py
  config/
    binance_dip_buy_radar.json
  output/          ← sonuçlar buraya yazılır
```

```powershell
cd C:\Users\Rahman\OneDrive\Desktop\bot
python binance_dip_buy_radar.py --once --dry-run
```

Config yolunu elle vermek için:
```powershell
python binance_dip_buy_radar.py --once --config "C:\Users\Rahman\OneDrive\Desktop\bot\config\binance_dip_buy_radar.json"
```

