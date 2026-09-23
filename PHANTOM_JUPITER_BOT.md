# Phantom key → Jupiter likidite avcısı

Bot **Phantom’a bağlanmaz**. Ayrı cüzdanın private key’i ile Jupiter’da AL/SAT yapar.

## Ne yapar

1. Solana’da **yeni + trending** havuzları tarar  
2. **Pool adresini** on-chain kontrol eder  
3. Likidite↑ + hacim↑ ise **girer** (Jupiter AL)  
4. Satış rotası yoksa **girmez** (içeride kalmasın)  
5. Mint/freeze authority varsa **atlar**  
6. Havuz likiditesi zirveden düşünce **çıkar** (+ SL/TP/süre)  
7. Round-trip **komisyon tamponu** (~%2.5) hesaba katılır  

## Kurulum (Phantom’dan key çıkmasa da olur)

```bash
pip install requests solders base58

# 1) Bot cüzdanı üret
python3 -c "from solders.keypair import Keypair; import base58; k=Keypair(); print('ADRES', k.pubkey()); print('KEY', base58.b58encode(bytes(k)).decode())"

# 2) Phantom → Gönder → ADRES'e SOL yolla

# 3) Çalıştır
export SOLANA_PRIVATE_KEY='KEY'
# önerilir:
export HELIUS_API_KEY='...'
python phantom_jupiter_bot.py
```

Önce `DRY_RUN = True` bırak. Canlı için `False`.

## Ayarlar (dosya içi)

| Ayar | Anlam |
|------|--------|
| `BUY_SOL` | Her AL miktarı |
| `MIN_LIQ_USD` | Min havuz likiditesi |
| `LIQ_DROP_FROM_PEAK_PCT` | Zirveden düşünce SAT |
| `ROUNDTRIP_FEE_PCT` | Komisyon tamponu |
| `MAX_OPEN` | Aynı anda max coin |

KEY’i Git’e / chat’e koyma.
