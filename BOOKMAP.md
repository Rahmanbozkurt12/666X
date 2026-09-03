# Bookmap Entegrasyonu

Bookmap'i mevcut Python kod tabanına bağlamak için iki parçalı bir yapı kullanılır:

1. **Bookmap add-on** (`bookmap/wall_alert_addon.py`) — Bookmap içinde çalışır, order book'u izler
2. **Telegram köprüsü** (`bookmap_telegram_bridge.py`) — Olayları JSONL dosyasından okuyup Telegram'a gönderir

## Gereksinimler

- [Bookmap](https://bookmap.com/) 7.4 veya üzeri
- Python 3.7.14+ (Bookmap'in Python API eklentisi ile)
- Bookmap → **Settings → Manage plugins → Bookmap Add-ons (L1)** → Python API kurulu olmalı

> `bookmap` paketi normal `pip install` ile çalışmaz; yalnızca Bookmap uygulaması içinden yüklenir.

---

## Bookmap'te doğru çalıştırma (Windows)

1. Bookmap'i açın  
2. **Settings → Manage plugins → Bookmap Add-ons (L1)** → **Python API** kurulu olsun  
3. Bookmap menüsünden **Python API / Scripts** editörünü açın  
4. `bookmap/wall_alert_addon.py` dosyasını yükleyin (veya içeriği yapıştırın)  
5. Canlı bir enstrüman grafiği açın (BTC futures vb.) — **Data: Live** olmalı  
6. Add-on listesinden **wall_alert_addon**'u **Enable** edin  
7. Bookmap mesaj / konsol logunda şunları görmelisiniz:
   - `[wall_alert] depth subscribed: ...`
   - `[wall_alert] ready — events -> C:\Users\...\Documents\666X\output\bookmap_events.jsonl`

### Telegram köprüsü (VS Code / PowerShell)

```powershell
cd C:\Users\Rahman\...\666X
pip install requests
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHAT_ID="..."
# Bookmap logundaki yolu aynen verin:
python bookmap_telegram_bridge.py --dry-run --events "$env:USERPROFILE\Documents\666X\output\bookmap_events.jsonl"
```

---

## Desktop\\bot kurulumu (ekran görüntünüz)

Sizde iki pencere var:

| Pencere | Dosya | Ne yapmalı |
|---------|--------|------------|
| VS Code | `C:\Users\Rahman\OneDrive\Desktop\bot\bookmap_bridge.py` | Köprü — düzeltilmiş kopya: `bot/bookmap_bridge.py` |
| Bookmap code editor | `book.py` | Add-on — düzeltilmiş kopya: `bot/book.py` |

### Ekrandaki hata

```text
SyntaxError: unterminated string literal (detected at line 65)
output = Path(r"C:\Users\Rahman\OneDrive\desk...
```

Yol satırı yarım kalmış (tırnak kapanmamış). `bot/bookmap_bridge.py` ile değiştirin; `main()` bittikten sonra ekstra `output = Path(...)` satırı olmamalı.

Adım adım: **`bot/README.md`**

### Alış yeşil / satış kırmızı

Ham `bookmap_events.jsonl` VS Code’da tek renk görünür. Renkli bakmak için:

```powershell
cd C:\Users\Rahman\OneDrive\Desktop\bot
.\venv\Scripts\python.exe bookmap_bridge.py --boya
# output\bookmap_events.diff → VS Code’da aç (alış yeşil, satış kırmızı)

.\venv\Scripts\python.exe bookmap_bridge.py --viewer
# tarayıcıda canlı yeşil/kırmızı liste
```

Telegram ve terminal uyarılarında da 🟢 ALIŞ / 🔴 SATIŞ kullanılır.

Senin Telegram chat ID: **5555764362** (`@rbozkurt`) — config’e yazıldı. Eksik olan sadece bot token:

```powershell
# .env
TELEGRAM_BOT_TOKEN=BotFather_token_buraya
TELEGRAM_CHAT_ID=5555764362
```

Bot’a bir kez `/start` yazmayı unutma.

---

## Sık hatalar ve çözümleri

### 1) VS Code: `Import "bookmap" could not be resolved`

**Beklenen.** Add-on'u VS Code'dan çalıştırmayın. Yalnızca Bookmap Python API editöründen Enable edin.

| Dosya | Nerede çalışır? |
|-------|-----------------|
| `bookmap/wall_alert_addon.py` | **Yalnızca Bookmap içinde** |
| `bookmap_telegram_bridge.py` | VS Code / terminal |

### 2) Bookmap içinde Enable → hata / addon kapanıyor

Önceki sürümde boş order book üzerinde `get_bbos` açılırken şu hata oluşabiliyordu:

```text
TypeError: cannot unpack non-iterable NoneType object
```

Bu sürümde BBO okuma güvenli hale getirildi; handler'lar try/except ile sarıldı.  
Hâlâ kapanıyorsa Bookmap **Messages / Python console** çıktısının tamamını kopyalayıp gönderin.

Kontrol listesi:

- Python API eklentisi kurulu mu?
- Enstrüman **Live** mi? (Simulated trading OK; feed Live olmalı)
- BookmapData / dxFeed kullanıyorsanız Python API **desteklemez** — Java API gerekir
- Aynı addon'u aynı anda iki enstrümanda Enable etmeyin (bilinen Bookmap bug'ı)

### 3) Add-on çalışıyor ama Telegram'a bir şey gelmiyor

Bookmap script'i genelde `C:\Bookmap\Python\tmp\...` altına kopyalanır; eski kod olayları yanlış klasöre yazıyordu.

Bu sürüm varsayılan olarak şuraya yazar:

```text
%USERPROFILE%\Documents\666X\output\bookmap_events.jsonl
```

1. Bookmap logunda `events -> ...` satırındaki **mutlak yolu** bulun  
2. Köprüyü o yolla başlatın:  
   `python bookmap_telegram_bridge.py --events "O_YOL"`  
3. Bookmap add-on ayarından **Export path** alanını da aynı mutlak yola çekebilirsiniz  
4. `Min wall size` değerini düşürün (ör. `10000`) — duvar eşiği yüksekse olay üretmez

### 4) Python sürümü

Bookmap kendi Python yolunu kullanır. VS Code'daki 3.13 seçimi add-on için önemli değildir.

---

## Nasıl çalışır?

```
Bookmap (canlı veri)
    │
    ▼
wall_alert_addon.py  ──►  Documents/666X/output/bookmap_events.jsonl
    │                              │
    │                              ▼
    │                    bookmap_telegram_bridge.py
    │                              │
    ▼                              ▼
  DOM / ladder              Telegram uyarıları
```

### Tespit edilen olaylar

| Olay | Açıklama |
|------|----------|
| `wall_detected` | Büyük likidite duvarı oluştu (ör. 80.000'de 850K satış) |
| `wall_removed` | Duvar emildi veya kaldırıldı |
| `price_near_wall` | Fiyat duvara belirlenen % mesafesine girdi |

### Yapılandırma

`config/bookmap_alerts.json` — `export_path` boş bırakılırsa Documents yolu kullanılır.

Bookmap UI ayarları: **Min wall size**, **Near wall %**, **Export path**.

---

## Hızlı test (Telegram köprüsü)

```powershell
# örnek olay yaz
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\Documents\666X\output" | Out-Null
'{"type":"wall_detected","alias":"TEST","side":"ask","price":80000,"size":850000,"mid_price":76880,"ts":"2026-09-02T12:00:00Z"}' |
  Out-File -Encoding utf8 -Append "$env:USERPROFILE\Documents\666X\output\bookmap_events.jsonl"

python bookmap_telegram_bridge.py --dry-run --once --replay --events "$env:USERPROFILE\Documents\666X\output\bookmap_events.jsonl"
```

---

## Sınırlamalar

- Python API şu an **beta**; replay modu desteklenmiyor
- BookmapData / dxFeed için Java API gerekir
- Add-on Bookmap dışında çalıştırılamaz; köprü scripti bağımsız çalışır

## Kaynaklar

- [Bookmap Python API](https://github.com/BookmapAPI/python-api)
- [Bookmap Knowledge Base — Python API](https://bookmap.com/knowledgebase/docs/Addons-Python-API)
