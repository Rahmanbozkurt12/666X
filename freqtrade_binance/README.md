# Freqtrade + NostalgiaForInfinityX7 — Binance spot (dry-run)

Bu klasör GitHub’daki **en başarılı açık kaynak Binance stack**’inin kurulumu:

| Parça | Kaynak |
|--------|--------|
| Motor | [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) (~54k⭐) |
| Strateji | [iterativv/NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity) X7 (5m) |

## Kurulum

```bash
cd freqtrade_binance
chmod +x setup.sh
./setup.sh          # NostalgiaForInfinityX7.py indirir (~3.5MB)
docker compose up -d
```

API UI: http://127.0.0.1:8080  (user/pass: `freqtrade` / `freqtrade`)

## Önemli

- Varsayılan **`dry_run: true`** — gerçek para yok.
- Live için `configs/exampleconfig_secret.json` içine Binance API key/secret yaz, `dry_run` → `false`.
- Withdrawal izni **kapalı** tut.
- NFI timeframe **5m** (strategy içinde sabit).
- Lisans: her iki proje de GPL-3.0.

## Dosyalar

- `docker-compose.yml` — Freqtrade stable image
- `user_data/config.json` — config birleştirici
- `configs/*` — Binance spot + volume pairlist + blacklist
- `setup.sh` — resmi NFI X7 stratejisini indirir
