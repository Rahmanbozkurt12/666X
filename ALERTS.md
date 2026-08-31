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

## Raydium yeni LP yakalama

Phantom değil, **Raydium programı + pool adresi** izlenir. Yeni açılan SOL çiftli havuzlar için:

```bash
python raydium_lp_alert.py --once --dry-run   # test
python raydium_lp_alert.py                    # canlı loop + Telegram
```

Filtreler: `config/raydium_lp_watch.json` (`min_reserve_usd`, `dex_allowlist`, `require_pump_mint_suffix`, vb.)
İlk poll sadece state seed eder; sonraki poll'larda yeni havuzlar alert olur.
