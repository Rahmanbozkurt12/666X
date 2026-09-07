# CEX Wallet Telegram Alerts

Public labeled CEX/DEX wallets (Binance, BtcTurk, Coinbase, OKX, Paribu, PancakeSwap, Binance Alpha, pump.fun) are polled for token transfers; Telegram gets IN/OUT alerts.

## Binance momentum scanner (yükseliş adayı)

Public Binance market data ile USDT spot coinleri tarar (API key gerekmez).  
30 dk momentum + hacim artışı filtreler. Bu bir tahmin / %70 garanti değildir.

```bash
pip install -r requirements.txt

# tek tarama
python binance_momentum_scanner.py --top 5

# her 30 dk tekrar
python binance_momentum_scanner.py --loop --interval 1800

# Telegram'a gönder (aynı .env)
set -a; source .env; set +a
python binance_momentum_scanner.py --telegram --top 5

# mesajı sadece konsolda gör
python binance_momentum_scanner.py --dry-run
```

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
