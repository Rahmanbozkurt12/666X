# PRO CEX Radar

Yüksek ikna / az sinyal. **Garanti yok** — sıkı filtre ile “kesine yakın” aday.

## 3 CORE kural (hepsi şart)

1. Hacim sessiz → **0→+**
2. Fiyat **erken** (14g dibe yakın, 24s ≤ +%5)
3. En az **3 / 8 kaliteli CEX** aynı uyanış

## TEYİT (🟢 YÜKSEK için)

- RSI 30–55  
- 15m higher-low veya kırılım  
- BTC rejim OK  
- Funding aşırı pozitif değil  

| Sinyal | Anlam |
|--------|--------|
| 🟢 YÜKSEK | CORE + teyit → AL adayı |
| 🟡 ORTA | CORE var, teyit eksik → bekle |
| 🔴 GEÇ | kaçmiş |

## 8 CEX

Binance · OKX · Bybit · Bitget · Gate · KuCoin · MEXC · Coinbase

## Çalıştır

```powershell
pip install requests ccxt
cd C:\Users\Rahman\OneDrive\Desktop\bot
python pro_cex_radar.py --once --dry-run
python pro_cex_radar.py --backtest
python pro_cex_radar.py --missed
python pro_cex_radar.py
```

Config yoksa gömülü ayarla çalışır.
