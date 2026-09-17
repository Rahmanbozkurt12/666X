# Binance × GitHub Dev Anomaly Scanner

Binance USDT coinlerini tarar, her coin için resmi GitHub reposunu eşleştirir, son **7 / 30 / 90** günlük geliştirme aktivitesini geçmiş baseline ile karşılaştırır, olağandışı artışları skorlar ve Binance **hacim / CVD / order-book** verisiyle birleştirir.

## Akış

```
Binance USDT listesi
        ↓
GitHub reposunu bul (manual map → CoinGecko → GitHub search)
        ↓
Son 7 / 30 / 90 günlük aktiviteyi ölç
        ↓
Prior window / 90g ortalamayla karşılaştır
        ↓
Anormal değişiklik → github_score
        ↓
Hacim + CVD + order-book imbalance → market_score
        ↓
combined_score = 0.62*gh + 0.38*mkt
```

## GitHub metrikleri

| Metrik | Nasıl |
|--------|--------|
| Commit sayısı | 7 / 30 / 90 gün |
| Commit hızı değişimi | 7g vs prior 7g, 7g vs 90g ort, 30g vs prior 30g |
| Son commit zamanı | `days_since_commit` |
| Contributor | toplam + 30g delta |
| Release’ler | 7 / 30 / 90 |
| Büyük kod değişiklikleri | commit stats \|add+del\| ≥ 400 |

## Setup

```bash
pip install -r requirements.txt
# önerilir
export GITHUB_TOKEN=ghp_...
# opsiyonel Telegram
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
# geo-restricted ortamlarda (Cloud Agent varsayılanı)
export BINANCE_API_BASE=https://data-api.binance.vision
```

Manual eşlemeler: `config/coin_github_map.json`  
Cache: `output/coin_github_map_cache.json`

## Run

```bash
# hacme göre top 20
python3 binance_github_dev_scanner.py --top 20

# seçili coinler (hızlı test)
python3 binance_github_dev_scanner.py --symbols BTC,ETH,SOL,SUI,APT

# sadece yüksek anomali + Telegram
python3 binance_github_dev_scanner.py --top 40 --min-score 55 --telegram
```

Rapor: `output/binance_github_dev_report.json`
