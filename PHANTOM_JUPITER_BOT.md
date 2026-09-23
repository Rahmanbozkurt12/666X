# phantom.py — $0.50 AL → $1 SAT

## Key dosyası (zorunlu)

Aynı klasöre `phantom_keys.json` koy:

```json
{
  "apiKey": "",
  "walletPublicKey": "Cztef...adresin",
  "privateKey": "uzun_private_key"
}
```

- `privateKey` → bot bununla imzalar  
- `walletPublicKey` → kontrol + Solscan  
- `apiKey` → bu botta gerekmez (boş bırak)

Örnek: `phantom_keys.example.json` → kopyala → `phantom_keys.json` adını ver → doldur.

**Git’e / chat’e privateKey koyma.**

## Çalıştır

```bash
pip install requests solders
python phantom.py
```

Önce `DRY_RUN = True`. Canlı: `False`.
Adrese SOL yolla (`walletPublicKey`).
