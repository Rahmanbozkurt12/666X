# 666X  
The official repository for the 666X meme token project

## Solana bot reverse-engineering (dry-run)

Target wallet analysis tools (public RPC only — no private keys):

```bash
# 1) Fingerprint programs + fee wallets from a trader address
python3 solana_bot_fingerprint.py --wallet A6PSQFRfv93hoAn1LhQGRT2dYQtjDKX6SE2vN9MEvbot --limit 40

# 2) Show reconstructed trading loop + paper demo
python3 solana_pump_behavior_bot.py --once
python3 solana_pump_behavior_bot.py --simulate-trade
```

Config: `config/solana_target_wallet.json`  
These scripts reconstruct **behavior** from chain data. They do not recover closed-source bot code.
