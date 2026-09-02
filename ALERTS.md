# CEX Wallet Telegram Alerts

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

## Yeni / aktif cüzdan keşfi + birikim sinyali

**FET alan cüzdanlar istenmiyor** — `exclude_symbols: ["FET"]` varsayılan.

```bash
# Balina cüzdanlarda son 3 saatte hangi altcoin girişi var? (FET hariç)
python whale_altcoin_scan.py --hours 3

# Yeni/aktif cüzdan keşfi (LINK, PEPE, LDO… — FET hariç)
python active_wallet_discovery.py --activity-hours 72

# Birikim sinyali
python accumulation_alert.py --once --dry-run
```

Ayarlar: `config/active_wallet_discovery.json`, `config/accumulation_watch.json`

### Eski yöntem (eth-labels — önerilmez)

```bash
python labeled_wallet_fetcher.py --name-tag Binance --max 1000
```

`Binance Dep` = borsaya satış için yatırım adresleri.
