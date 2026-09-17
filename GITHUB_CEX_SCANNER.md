# GitHub CEX Coin Erken Uyarı Tarayıcısı

Major CEX GitHub org/repo'larını tarar; **listing / new asset / trading pair** sinyallerini coin pompalanmadan önce Telegram'a basar.

## Ne tarar?

1. **Org events** — Binance, OKX, Bybit, Coinbase, KuCoin, Gate, Bitget, MEXC, HTX, Kraken, BtcTurk, Paribu… push / release / issue / PR
2. **Yeni public repo** — CEX org'unda yeni repo açılması
3. **Watch repos** — bilinen API/docs repo commit'leri + değişen dosya yolları
4. **Code search** — `listing`, `will list`, `opens trading` vb. (rate limit sıkı; `--skip-code-search` ile kapat)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# doldur:
#   GITHUB_TOKEN=...          # şart gibi (5000 req/saat; yoksa 60/saat)
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_CHAT_ID=...
set -a; source .env; set +a
```

Hedef listesi: `config/github_cex_targets.json`

## Run

```bash
# smoke test
python github_cex_scanner.py --once --dry-run --priority high --skip-code-search

# canlı (tüm high+medium+low org'lar)
python github_cex_scanner.py

# sadece top CEX'ler, code search kapalı (daha az rate limit)
python github_cex_scanner.py --priority high --skip-code-search
```

İlk poll **bootstrap** yapar (tarihsel spam yok). Sonraki poll'larda sadece **yeni** sinyaller alert olur.

## State

- `output/github_cex_state.json` — görülen event id'leri
- `output/github_cex_alerts.jsonl` — gönderilen alert log

Sıfırlamak: `python github_cex_scanner.py --reset-state --once --dry-run`
