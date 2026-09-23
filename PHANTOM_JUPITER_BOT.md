# phantom.py — her yeni coin $0.50 → küçük kârda sat ($0.65)

## 1) `phantom_keys.json` (aynı klasör)

```json
{
  "apiKey": "",
  "walletPublicKey": "ADRESIN",
  "privateKey": "UZUN_PRIVATE_KEY"
}
```

Key’i `.py` içine yazma.

## 2) Çalıştır

```bash
pip install requests solders
python phantom.py
```

Önce `DRY_RUN = True`. Canlı: `False`.

## Strateji

| | |
|--|--|
| Giriş | Her yeni `*/SOL` havuz ≈ **$0.50** |
| Satış | ≈ **$0.65** (küçük kâr) |
| Stop | ≈ **$0.35** |
