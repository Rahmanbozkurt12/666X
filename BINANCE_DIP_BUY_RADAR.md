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
(**`from __future__` satırının üstüne yazma** — SyntaxError verir)
veya ortam değişkeni kullan:

```powershell
# Paper (emir atmaz) — max 10 coin, USDT eşit bölünür, SL/TP1/TP2
python binance_dip_buy_radar.py --once --trade --dry-run --skip-multi-cex --fast

# Canlı (gerçek para)
# LIVE=1 + key gerekli
$env:LIVE="1"
$env:BINANCE_API_KEY="..."
$env:BINANCE_API_SECRET="..."
python binance_dip_buy_radar.py --once --trade
```

Kurallar:
- Sadece **AL** (UÇ öncelikli)
- En fazla **10** açık pozisyon
- Serbest USDT’yi bu turda alınacak coine eşit böler
- **SL** tam çıkış · **TP1** %50 sat · **TP2** kalanı
- Varsayılan **DRY-RUN**; canlı için `LIVE=1`

Pozisyon: `output/binance_dip_buy_positions.json`  
İşlem log: `output/binance_dip_buy_trades.jsonl`

Config yoksa gömülü varsayılanla çalışır.
