# Gecikmeli cüzdan kopya botu (`mev_copy_trader.py`)

60 sn (ayarlanabilir) gecikmeli copy-trade: izlenen cüzdan AL/SAT → sinyal → hold kontrolü → (opsiyonel) Binance.

## Gerçekçi uyarı

Jared / UniV4 / Eff6 tipi **MEV** botlar çoğu trade’i **aynı blokta** alıp satar.  
60 sn sonra kopyalamak genelde **zarar**dır. Bot bu yüzden:

- aynı tx içinde altcoin IN+OUT → **atomic_mev** skip
- delay sonunda token satılmışsa → **SKIP_NO_HOLD**

Asıl işe yarayan hedef: **dakikalarca tutan** sniper / smart-money cüzdanları.  
`rsync-builder` kapalı (builder, trader değil). Maestro adresini Arkham’dan elle ekle.

## Çalıştır (varsayılan = GERÇEK al-sat)

```bash
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...
python mev_copy_trader.py
```

Kağıt mod (emir yok):

```bash
python mev_copy_trader.py --dry-run
python mev_copy_trader.py --once --dry-run
```

Sadece Binance’te USDT listeli coinler; max `$max_copy_usd` (config, varsayılan 50).

Config: `config/mev_copy_wallets.json`  
Log: `output/mev_copy_signals.jsonl`  
State: `output/mev_copy_state.json`
