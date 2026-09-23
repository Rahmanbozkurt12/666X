# phantom.py — $0.50 AL → $1 SAT (yeni havuz)

Her açılan Solana `*/SOL` havuza yaklaşık **$0.50** girer, pozisyon **~$1** olunca satar.

## Kurulum

```bash
pip install requests solders
python -c "from solders.keypair import Keypair; k=Keypair(); print('ADRES', k.pubkey()); print('KEY', k)"
```

1. `ADRES`'e Binance/Phantom'dan SOL yolla  
2. `phantom.py` → `SOLANA_PRIVATE_KEY = "KEY"`  
3. Önce `DRY_RUN = True` → `python phantom.py`  
4. Canlı: `DRY_RUN = False`

Helius/Jupiter API **gerekmez**.
