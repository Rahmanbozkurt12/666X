# Mega Confluence Scanner

128 kripto analiz metodunu tarar, confluence skoru üretir, paper trader ile **AL / SAT** yapar.

## Risk kuralları (istenildiği gibi)

1. Confluence **BUY** ise al (paper)
2. Peak’ten `trail_pct` (varsayılan **%0.45**) geri çekilince sat → yükselişi olabildiğince sür, dönüşte çık
3. Girişten **%-1** olunca hard stop sat
4. Confluence **SELL** ve kârdayken de satabilir

> Hard stop kaybı sınırlar; sıfır zarar garantisi değildir (slippage / gap olabilir).

## Güçlü stack (yüksek ağırlık)

Price Action + Wyckoff + Market Structure + Volume/Footprint + CVD/Delta + OI/Liquidations + Liquidity + On-chain proxy + SMC/ICT + Risk Management

API’si olmayan metodlar (MEV, mempool, ETF flow, NVT, …) **neutral / düşük ağırlık** döner; tarama listesinde yine görünür.

## Kullanım

```bash
# Tek tarama
python mega_confluence_scanner.py --symbol BTCUSDT

# Birkaç coin
python mega_confluence_scanner.py --symbol BTCUSDT --symbol ETHUSDT --symbol SOLUSDT

# JSON + paper trade (al/sat state: output/mega_confluence_state.json)
python mega_confluence_scanner.py --symbol BTCUSDT --execute --json

# Canlı döngü (paper)
python mega_confluence_scanner.py --symbol BTCUSDT --execute --loop --poll 60

# Top hacimli USDT çiftlerini de tara
python mega_confluence_scanner.py --top 10 --execute
```

Ayarlar: `config/mega_confluence.json`

```json
{
  "hard_stop_pct": 1.0,
  "trail_pct": 0.45,
  "starting_cash": 10000
}
```

## Çıktı

- Konsol: verdict, top bull/bear metodlar, trade event
- `output/mega_confluence_last.json` — son tarama
- `output/mega_confluence_state.json` — paper pozisyon / trades

Eğitim / araştırma amaçlıdır; yatırım tavsiyesi değildir.
