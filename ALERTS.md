# CEX Alerts

## 1) Binance USDT × GitHub Dev Anomaly (asıl amaç)

Binance USDT coin → resmi GitHub repo → 7/30/90 aktivite anomalisi → hacim/CVD/order-book birleşik skor.

Detay: [`BINANCE_GITHUB_DEV.md`](BINANCE_GITHUB_DEV.md)

```bash
python3 binance_github_dev_scanner.py --symbols BTC,ETH,SOL
python3 binance_github_dev_scanner.py --top 30 --min-score 50
```

## 2) GitHub CEX org scanner (exchange repo sinyalleri)

Major CEX GitHub org'larını tarar → listing / new asset sinyali → Telegram.

Detay: [`GITHUB_CEX_SCANNER.md`](GITHUB_CEX_SCANNER.md)

```bash
python github_cex_scanner.py --once --dry-run --priority high --skip-code-search
python github_cex_scanner.py --priority high
```

## 3) CEX Wallet Telegram Alerts

Public labeled CEX/DEX wallets (Binance, BtcTurk, Coinbase, OKX, Paribu, PancakeSwap, Binance Alpha, pump.fun) are polled for token transfers; Telegram gets IN/OUT alerts.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
set -a; source .env; set +a
```

Edit `config/watched_wallets.json` to add/remove addresses (Arkham labels welcome).

## Run

```bash
# smoke test (no Telegram send if env missing)
python telegram_cex_alert.py --once --dry-run

# live loop
python telegram_cex_alert.py
```

First poll per wallet only seeds state (no historical spam). Later polls alert on new transfers.

Spam airdrops (decimals=0 + huge amount) are filtered by default.

## Notes

- Ethereum: Blockscout (no key)
- BSC: needs `ETHERSCAN_API_KEY` (Etherscan V2)
- Solana: public RPC or `HELIUS_API_KEY`
- Phantom is a user wallet app — add specific addresses you care about under `venue: Phantom`
- CEX hot-wallet traffic is noisy; start with a short watchlist
