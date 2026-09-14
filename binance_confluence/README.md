# Multichain confluence → Binance spot

Birden fazla zincir/CEX sinyalini birleştirip **Binance USDT spot**’ta al/sat yapan bot.

## Mimari

| Modül | Veri | Gerekli key |
|--------|------|-------------|
| `volume_spike` | Binance 5m klines | — (public) |
| `orderbook` | Depth REST + WebSocket | — (public) |
| `onchain` | Holder velocity (Moralis/Etherscan) + smart-money flag file | `MORALIS_API_KEY` / `ETHERSCAN_API_KEY` |
| `news` | CryptoPanic + basit NLP | `CRYPTOPANIC_API_KEY` |
| `executor` | Binance spot market | `BINANCE_API_KEY/SECRET` (live) |

Confluence: sembol bazında skor topla → `min_score` üstü en güçlüyü al.

## Çalıştır

```bash
pip install -r requirements.txt
# opsiyonel keys:
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...
export MORALIS_API_KEY=...
export CRYPTOPANIC_API_KEY=...

python3 -m binance_confluence --once          # dry-run tarama
python3 -m binance_confluence --loop          # sürekli
python3 -m binance_confluence --live --loop   # gerçek emir
```

Config: `config/binance_confluence.json`  
State: `output/binance_confluence_state.json`

## On-chain watchlist

`config.onchain.watchlist` içine Binance’de listeli token kontratlarını yaz:

```json
{
  "binance_symbol": "LINKUSDT",
  "chain": "ethereum",
  "contract": "0x514910771af9ca656af840dff83e8264ecf986ca"
}
```

Smart money için harici webhook’un dokunduğu bir flag dosyası:

```json
"smart_money_flag_file": "output/flags/LINK_smart.json"
```

## Çıkış

- Peak trail: `trail_pct` (varsayılan **0.020%**)
- Entry stop: `stop_pct` (varsayılan **0.50%**)
- Yükselişte sabit +1 satışı yok

## Latency

Canlıda botu Binance’e yakın çalıştır (AWS `ap-northeast-1` Tokyo veya `eu-central-1` Frankfurt). Bu kod colocated sunucu sağlamaz; deploy senin VPS’in.
