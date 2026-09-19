# Binance Dip AL Radarı v2.1 — Uç potansiyel + al/sat

**Önemli:** Eski ~1500 satırlık dosya sadece radar. Al/sat için **bu repodaki güncel** `binance_dip_buy_radar.py` (~2200 satır) gerekir.

## 1) API key

Dosyada `from __future__` **altında**:

```python
BINANCE_API_KEY_HARDCODE = "senin_key"
BINANCE_API_SECRET_HARDCODE = "senin_secret"
LIVE_HARDCODE = True   # gerçek alım
TRADE_HARDCODE = True  # AL bulununca al/sat
```

## 2) Çalıştır

```powershell
cd C:\Users\Rahman\OneDrive\Desktop\bot

# Gerçek alım
python binance_dip_buy_radar.py --once --trade --live

# veya çift tık
.\AL_SAT_CALISTIR.bat
```

Konsolda mutlaka: `[MODE] ⚠️ LIVE Binance spot`  
`PAPER` görüyorsan Binance’ta alım olmaz → `LIVE_HARDCODE = True` yap.

## Ne alır / satar?

| | |
|--|--|
| Alır | **AL** + güçlü **İZLE** (max 10 coin, USDT eşit bölünür) |
| Satar | **SL** tam · **TP1** %50 · **TP2** kalanı |

Binance API: **Enable Spot & Margin Trading** açık olsun; IP restrict varsa PC IP ekle.

Pozisyon: `output/binance_dip_buy_positions.json`
