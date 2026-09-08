# CEX Wallet Telegram Alerts

Public labeled CEX/DEX wallets (Binance, BtcTurk, Coinbase, OKX, Paribu, PancakeSwap, Binance Alpha, pump.fun) are polled for token transfers; Telegram gets IN/OUT alerts.

## Binance momentum scanner (yükseliş adayı)

Public Binance market data ile USDT spot coinleri tarar (API key gerekmez, Telegram yok).  
Her çalıştırmada 30 dk momentum + hacim artışı olan adayları konsola yazar.  
Bu bir tahmin / %70 garanti değildir.

```bash
pip install -r requirements.txt

# her çalıştırdığında güncel yükseliş adayını söyler
python3 binance_momentum_scanner.py

# sadece en güçlü 3 aday
python3 binance_momentum_scanner.py --top 3
```

## Binance radar scanner (ileri seviye)

Momentum + kademeli hacim + order-book derinliği + büyük işlem akışı + duyuru eşleşmesi.  
On-chain holder / DEX pool için güvenli placeholder bırakır (kontrat eşlemesi olmadan uydurma yapmaz).  
Telegram opsiyonel.

```bash
python3 binance_radar_scanner.py --top 10
python3 binance_radar_scanner.py --loop --interval 300 --top 10
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
