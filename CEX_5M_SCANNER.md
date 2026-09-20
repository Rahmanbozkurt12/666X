# Çoklu CEX 5m Dipten Hacim Tarayıcısı

5 dakikalık mumlarda **dipten hacim yükselişi** ve **0→+ dönüş** tarar; borsalar arası ortak coin'i bulur.

## Desteklenen CEX

| CEX | Durum |
|-----|--------|
| Binance | ✅ |
| Coinbase | ✅ |
| Upbit | ✅ |
| OKX | ✅ |
| Bybit | ✅ |
| Bitget | ✅ |
| Gate | ✅ |
| KuCoin | ✅ |
| Robinhood | ❌ public market API yok |
| Coinspace | ❌ cüzdan (CEX değil) |
| Bitxex | ❌ public CCXT yok |

## Sinyal mantığı

Her borsada (varsayılan top 120 likit coin, 5m OHLCV):

1. **Hacim dipten yükseliyor** — son mum hacmi, önceki pencere dibinin ~1.45× üstünde ve artıyor
2. **Fiyat tetik** — 0→+ getiri, kırmızıdan yeşil muma geçiş, veya dipe yakın bounce

Coin bazında **confluence %** = `(sinyal veren borsa) / (listelendiği borsa) × 100`

- **🟢 AL** → en az 3 borsada sinyal **ve** confluence ≥ %50
- **🔴 BEKLE** → aksi halde (yine de top liste gösterilir)

## Çalıştırma

```bash
pip install -r requirements.txt
python cex_5m_volume_scanner.py --once --dry-run
python cex_5m_volume_scanner.py --once --top 20
# sürekli (5 dk):
python cex_5m_volume_scanner.py
```

Telegram için `.env` içine `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.

Sonuç JSON: `output/cex_5m_volume_signals.json`  
Ayarlar: `config/cex_5m_scanner.json`
