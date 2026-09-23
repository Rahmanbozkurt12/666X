# Neden zarar ettiniz? (dürüst teşhis)

Bu araç **otomatik trading botu değildir**. Erken uyarı tarayıcısıdır. Zararın tipik nedenleri:

1. **Mega-cap gürültüsü** — BTC/ETH/SOL/SUI her gün commit atar. Eski skor bunu “sinyal” sandı.
2. **Pump peşinde koşma** — 24s zaten +%20 iken skor yüksek görünüp geç giriş.
3. **CVD/order-book yok sayıldı** — sell baskısında alım.
4. **Stop yok** — sinyal ≠ emir; invalidation olmadan işlem.

## Yeni trade-safe kurallar

| Tier | Anlam |
|------|--------|
| **ACTIONABLE** | Gerçek dev anomali + market confluence — tek trade adayı |
| **WATCH** | İzle, alma |
| **NOISE** | İşlem yasak (veto listesi raporda) |

Hard veto örnekleri: `mega_cap_noise`, `no_dev_anomaly`, `already_pumped`, `cvd_sell_pressure`, `ask_heavy_book`.

## Doğru kullanım

```bash
# Sadece ACTIONABLE (varsayılan) — yoksa İŞLEM AÇMA
python3 binance_github_dev_scanner.py --mode trade --top 50 --skip-coingecko

# Mid-cap band (hacim sırası 15–80, mega hariç)
python3 binance_github_dev_scanner.py --mode trade --rank-from 15 --rank-to 80 --skip-coingecko

# Araştırma (eski tarz tablo; trade için kullanma)
python3 binance_github_dev_scanner.py --mode research --include-mega --symbols BTC,ETH,SOL
```

Risk: ACTIONABLE çıksa bile hesap başına risk ≤ %1; stop = `risk.invalidation`.
