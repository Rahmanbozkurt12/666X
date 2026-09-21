# Multi-chain cüzdan → Binance kopya

Dosya: `multi_chain_binance_copy.py`

## Ne yapar

- **Ethereum + Base + Solana** cüzdanlarını tarar
- Cüzdan **AL** → Binance’te aynı coin’i (USDT çifti varsa) **AL**
- Cüzdan **SAT** → Binance’te **SAT**
- Serbest USDT’yi **10 eşit slot**a böler → her emir max 1 slot
- Binance’te olmayan coin → `SKIP` (logda görünür)
- Açılışta geçmiş tx kopyalanmaz

## Kurulum

```bash
pip install requests
```

1. `BINANCE_API_KEY` / `BINANCE_API_SECRET` doldur (veya env)
2. `WALLETS` listesine adres ekle:

```python
{
    "address": "0xSENIN_ETH...",
    "label": "eth-1",
    "chain": "ethereum",   # ethereum | base | solana
    "enabled": True,
},
```

3. Çalıştır:

```bash
python multi_chain_binance_copy.py
```

## Opsiyonel

| Env / alan | Ne işe yarar |
|---|---|
| `HELIUS_API_KEY` | Solana RPC (önerilir) |
| `ETHERSCAN_API_KEY` | ETH/Base explorer |
| `BALANCE_SLOTS = 10` | Kaç eşit parçaya bölünsün |
| `DRY_RUN = True` | Emir atmadan sadece log |
| `LIVE = False` | Paper bakiye |

## Notlar

- Aynı saniye kopya değil (~25 sn poll)
- Aynı anda max `MAX_OPEN_BASES` (10) farklı coin
- State: `output/multi_chain_copy_state.json`
