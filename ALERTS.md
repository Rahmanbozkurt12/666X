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

## Etiketli cüzdan + birikim sinyali (FET vb.)

Ücretsiz eth-labels API ile cüzdan kaydet, sonra token birikimini izle:

```bash
# 1) Cüzdanları kaydet (ekrandaki gibi — API key yok)
python labeled_wallet_fetcher.py --name-tag Binance --max 1000

# 2) Birikim izle (ör. 1000 cüzdandan 100+ FET birikince alert)
python accumulation_alert.py --once --dry-run
python accumulation_alert.py
```

Ayarlar: `config/accumulation_watch.json` (`min_token_balance`, `min_accumulators_for_alert`, …)

Not: `Binance Dep` adresleri borsaya yatırım içindir; alım sinyali için `smart_money_wallets.json` ile `--merge` kullanmak daha mantıklı olabilir.
