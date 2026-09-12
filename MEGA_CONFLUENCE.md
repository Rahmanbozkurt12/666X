# Mega Confluence Scanner

128 kripto analiz metodunu tarar, confluence skoru üretir, paper trader ile **AL / SAT** yapar.

## Tek dosya (önerilen)

Tüm kod birleştirildi:

```bash
python mega_confluence_all_in_one.py --symbol BTCUSDT
python mega_confluence_all_in_one.py --symbol BTCUSDT --execute --loop --poll 60
```

## Risk kuralları

1. Confluence **BUY** ise al (paper)
2. Peak’ten `trail` (varsayılan **%0.45**) geri çekilince sat
3. Girişten **%-1** olunca hard stop sat
4. Confluence **SELL** ve kârdayken de satabilir

> Hard stop kaybı sınırlar; sıfır zarar garantisi değildir (slippage / gap olabilir).

## Güçlü stack (yüksek ağırlık)

Price Action + Wyckoff + Market Structure + Volume/Footprint + CVD/Delta + OI/Liquidations + Liquidity + On-chain proxy + SMC/ICT + Risk Management

API’si olmayan metodlar (MEV, mempool, ETF flow, NVT, …) neutral / düşük ağırlık döner.

## Diğer komutlar

```bash
python mega_confluence_all_in_one.py --symbol BTCUSDT --symbol ETHUSDT --execute --json
python mega_confluence_all_in_one.py --top 10 --execute
python mega_confluence_scanner.py --symbol BTCUSDT   # aynı tek dosyayı çağırır
```

Ayarlar: `config/mega_confluence.json`

## Çıktı

- `output/mega_confluence_last.json` — son tarama
- `output/mega_confluence_state.json` — paper pozisyon / trades

Eğitim / araştırma amaçlıdır; yatırım tavsiyesi değildir.
