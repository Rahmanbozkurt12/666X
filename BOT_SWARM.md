# CEX Bot Sürüsü / Erken Pump Tespiti

`cex_bot_swarm_detector.py` — Binance Spot public veriden, **%30–80 patlamadan önce** bot/MM hazırlık imzasını yakalamaya çalışır.

## Ne arıyoruz?

Tipik senaryo:

1. Botlar saatte binlerce küçük işlem açar (makine-gun)
2. Agresif **taker alış** baskısı oluşur
3. Lot boyutları birbirine benzer (aynı bot / aynı MM şablonu)
4. 1 saatlik hacim önceki saate göre hızlanır
5. Fiyat henüz büyük yeşil mum atmamıştır (erken pencere)

## Sinyaller (skor bileşenleri)

| Sinyal | Anlamı |
|--------|--------|
| İşlem / dk | Swarm yoğunluğu |
| 24s işlem sayısı | Sürekli bot aktivitesi |
| Taker buy ratio | Agresif alış (`m=false`) |
| Size CV + aynı lot payı | Tekrarlayan bot lotları |
| 1s hacim ivmesi | Ani likidite / ilgi |
| Erken fiyat bandı | Henüz %25+ olmamış |

## Çalıştırma

```bash
pip install -r requirements.txt

# tek tur, Telegram yok
python3 cex_bot_swarm_detector.py --once --dry-run

# tek coin
python3 cex_bot_swarm_detector.py --symbol PROMUSDT --once --dry-run

# canlı (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)
set -a; source .env; set +a
python3 cex_bot_swarm_detector.py
```

Ayarlar: `config/bot_swarm.json`  
Çıktı: `output/bot_swarm_alerts.jsonl`

## API

Geo kısıtı olan ortamlarda `api.binance.com` yerine `data-api.binance.vision` kullanılır (spot).

## Sınırlar

- Wash trade / spoof’u %100 ispatlamaz; **istatistiksel imza** üretir
- Listing, haber, airdrop günlerinde false positive artar
- Futures OI / funding bu sürümde yok (spot odaklı); Bookmap duvar uyarısı ile birleştirilebilir
- Bu bir emir botu değil — sadece erken uyarı

## Bookmap ile birlikte

1. Swarm detector → aday coin listesi
2. Bookmap add-on → o coinde likidite duvarı / alış duvarı
3. Telegram köprüsü → birleşik alert
