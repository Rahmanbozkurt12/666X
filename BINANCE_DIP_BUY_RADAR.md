# Binance Dip AL Radarı v2.1 — Uç potansiyel + al/sat

**Hedef setup (AR tipi):** Hacim 0→+ geçmiş, fiyat henüz yatay veya sadece **+%3/+%5**, ama **+%50/+%70** uç potansiyeli yüksek.

## Ne tarıyor?

| Analiz | Anlam |
|--------|--------|
| HACIM_0→+ | Sessiz tabandan hacim artıya döndü |
| ERKEN_RALLI | 24s ≤ +%5 (kaçmamış) |
| DIPTE_KALIYOR | 14g dibe hâlâ yakın |
| UC_ALANI_30g | 30g high’a boşluk var |
| TABAN_SIKISMA | Sıkışma → kırılım |
| UC_POTANSIYEL | ~%40–70 tahmin |

## Sinyaller

- **🚀 UÇ** — +50/+70 adayı (öncelikli AL)
- **🟢 AL** — dipten erken
- **🟡 İZLE** — gelişiyor
- **🔴 GEÇ** — zaten yükselmiş

## Radar

```powershell
cd C:\Users\Rahman\OneDrive\Desktop\bot
python binance_dip_buy_radar.py --once --dry-run --top 20
```

## Gerçek / paper al-sat

API key’lerini dosyada **`BINANCE_API_KEY_HARDCODE`** satırına yaz
(**`from __future__` satırının üstüne yazma** — SyntaxError verir).

```powershell
cd C:\Users\Rahman\OneDrive\Desktop\bot

# PAPER (Binance'a emir gitmez)
python binance_dip_buy_radar.py --once --trade

# GERÇEK ALIM — --live ŞART
python binance_dip_buy_radar.py --once --trade --live
```

Kurallar:
- **AL** + güçlü **İZLE** (skor/uç eşiği üstü)
- En fazla **10** açık pozisyon · USDT eşit bölünür
- **SL** tam · **TP1** %50 · **TP2** kalanı
- Key yazmak yetmez → **`--live`** olmadan gerçek emir yok

Konsolda şunu görmelisin: `[MODE] ⚠️ LIVE Binance spot`
`[MODE] PAPER` görüyorsan Binance hesabında alım olmaz.
