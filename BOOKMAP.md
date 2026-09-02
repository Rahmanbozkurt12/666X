# Bookmap Entegrasyonu

Bookmap'i mevcut Python kod tabanına bağlamak için iki parçalı bir yapı kullanılır:

1. **Bookmap add-on** (`bookmap/wall_alert_addon.py`) — Bookmap içinde çalışır, order book'u izler
2. **Telegram köprüsü** (`bookmap_telegram_bridge.py`) — Olayları JSONL dosyasından okuyup Telegram'a gönderir

---

## Tıkandığınız nokta: `book.jar` / Configure add-ons

**Bu projede `book.jar` (veya herhangi bir add-on `.jar`) üretilmez.** Kod Python'dır; Bookmap'e jar seçmeniz gerekmez.

| Yaptığınız (yanlış) | Doğrusu |
|---------------------|---------|
| `Settings → Configure add-ons → Add...` ile `.jar` / `.lnk` seçmek | **Settings → Manage plugins → Bookmap Add-ons (L1) → Python API** kurmak |
| Masaüstü kısayolu (`.lnk`) seçmek | Kısayol asla seçilmez; bizim add-on zaten `.py` |
| `C:\Program Files\Bookmap\Bookmap.jar` seçmek | Bu Bookmap'in **kendi uygulaması**; add-on değil, buraya eklenmez |
| VS Code'dan `wall_alert_addon.py` çalıştırmak | Add-on **yalnızca Bookmap içinden** çalışır |

Configure add-ons ekranı **Java** eklentileri içindir. Bizim köprü **Python API** kullanır; o ekranda mavi tik aramayın.

### Windows — doğru 6 adım (tik burada değil)

1. Bookmap'i açın (canlı veri / BTC vb. grafik açık olsun).
2. **Settings → Manage plugins → Bookmap Add-ons (L1)** → **Python API** → **Install** (veya zaten kurulu görünsün).
3. Bookmap içinde **Python API / Scripts** editörünü açın (eklenti kurulunca menüde çıkar).
4. Şu dosyayı açın / içeriğini yapıştırın (kısayol değil, gerçek `.py`):
   - Repo: `bookmap\wall_alert_addon.py`
   - OneDrive kopyanız varsa: `C:\Users\Rahman\OneDrive\bookmap çalıştırma.py`
5. Grafikte add-on'u **Enable** edin. Konsolda şunu görmelisiniz:
   `[wall_alert] depth subscribed: ...`
6. Ayrı bir VS Code / PowerShell penceresinde köprüyü çalıştırın:

```powershell
cd <666X-repo-klasörü>
python bookmap_telegram_bridge.py --dry-run
```

Add-on yazmaya başlayınca `output\bookmap_events.jsonl` oluşur; köprü o dosyayı dinleyip satırları ekrana / Telegram'a basar.

### Yol bulucu (Windows)

PowerShell'de repo kökünden:

```powershell
powershell -ExecutionPolicy Bypass -File bookmap\find_paths.ps1
```

Script şunları yazar: gerçek `.py` yolu, beklenen JSONL yolu, varsa `Bookmap.jar` (uygulama — add-on değil).

---

## Gereksinimler

- [Bookmap](https://bookmap.com/) 7.4 veya üzeri
- Python 3.7.14+ (Bookmap'in Python API eklentisi ile)
- Bookmap → **Settings → Manage plugins → Bookmap Add-ons (L1)** → Python API kurulu olmalı

> `bookmap` paketi normal `pip install` ile çalışmaz; yalnızca Bookmap uygulaması içinden yüklenir.

## Sık hata: `Import "bookmap" could not be resolved`

VS Code veya terminalde `python wall_alert_addon.py` çalıştırırsanız bu hatayı alırsınız. **Bu beklenen davranıştır.**

| Dosya | Nerede çalışır? |
|-------|-----------------|
| `bookmap/wall_alert_addon.py` | **Yalnızca Bookmap içinde** (Python API editörü) |
| `bookmap_telegram_bridge.py` | VS Code / terminal (bookmap import yok) |

Pylance uyarısını görmezden gelebilirsiniz; script Bookmap'ten çalıştırıldığında modül orada vardır.

### VS Code'da çalıştırılacak script

Telegram köprüsü normal Python ile çalışır:

```powershell
cd C:\Users\Rahman\...\666X
pip install requests
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHAT_ID="..."
python bookmap_telegram_bridge.py --dry-run
```

### Python sürümü

Bookmap resmi olarak **Python 3.7.14+** ister; **3.13** henüz desteklenmeyebilir. Bookmap kendi Python yolunu kullanır — VS Code'daki 3.13 seçimi add-on için önemli değildir, script zaten Bookmap içinden koşar.

## Kurulum

### 1. Bookmap add-on'u yükle

Bookmap'te **Scripts** veya **Python API** editöründen `bookmap/wall_alert_addon.py` dosyasını açın.

Alternatif: dosyayı Bookmap'in script klasörüne kopyalayın ve oradan çalıştırın.

### 2. Enstrümanda etkinleştir

- BTC futures gibi bir enstrüman açın
- Add-on listesinden **wall_alert_addon**'u etkinleştirin
- Bookmap ayar panelinden **Min wall size** ve **Near wall %** değerlerini ayarlayın

### 3. Telegram köprüsünü başlat

```bash
pip install -r requirements.txt
set -a; source .env; set +a   # TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID

python bookmap_telegram_bridge.py
python bookmap_telegram_bridge.py --dry-run   # test
```

## Nasıl çalışır?

```
Bookmap (canlı veri)
    │
    ▼
wall_alert_addon.py  ──►  output/bookmap_events.jsonl
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

`config/bookmap_alerts.json`:

```json
{
  "settings": {
    "min_wall_size": 50000,
    "near_wall_pct": 5.0,
    "cooldown_sec": 120
  }
}
```

## Kendi kodunuzla kullanım

JSONL dosyasını doğrudan okuyabilirsiniz:

```python
import json
from pathlib import Path

for line in Path("output/bookmap_events.jsonl").open():
    event = json.loads(line)
    if event["type"] == "wall_detected" and event["side"] == "ask":
        print(f"Direnç: {event['price']} — hacim {event['size']}")
```

Mevcut `telegram_cex_alert.py` ile aynı `.env` dosyasını kullanır; iki script paralel çalışabilir.

## Sınırlamalar

- Python API şu an **beta**; replay modu desteklenmiyor
- BookmapData / dxFeed için Java API gerekir (o durumda gerçekten `.jar` derlenir — bu repo o yolu kullanmaz)
- Add-on Bookmap dışında çalıştırılamaz; köprü scripti bağımsız çalışır

## Kaynaklar

- [Bookmap Python API](https://github.com/BookmapAPI/python-api)
- [Bookmap Knowledge Base — Python API](https://bookmap.com/knowledgebase/docs/Addons-Python-API)
- [Python API Quick Guide](https://docs.google.com/document/d/178YRno3iKKdbuvVjVh380ayR-VsSUlQGZt2tDFjjD3A)
