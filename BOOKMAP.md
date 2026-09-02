# Bookmap Entegrasyonu

Bookmap'i mevcut Python kod tabanına bağlamak için iki parçalı bir yapı kullanılır:

1. **Bookmap add-on** (`bookmap/wall_alert_addon.py`) — Bookmap Python editöründe yazılır, **Build** ile `.jar` olur
2. **Telegram köprüsü** (`bookmap_telegram_bridge.py`) — Olayları JSONL dosyasından okuyup Telegram'a / ekrana basar

---

## Şu an neredeyiz? (Python API kurulu)

Plugins manager'da **Python API v0.2.0** görünüyorsa 1. adım bitti. **OK** ile kapatın.

Jar henüz yok; bir sonraki adımlarda Bookmap editöründe **Build** deyince oluşur.

### Adım 2 — Python API'yi etkinleştir + editörü aç

1. Bookmap ana pencerede: **Settings → Configure add-ons**  
   (veya araç çubuğundaki Configure add-ons ikonu)
2. Listede **Python API** satırını bulun → soldaki **mavi tik / checkbox**'ı işaretleyin
3. Yanındaki ayar / düğmeden **Open embedded editor** (veya benzeri) seçin

Editör açılmazsa: bir enstrüman (BTC) abone olduğunuzdan emin olun, Python API tikini kapatıp tekrar açın.

### Adım 3 — Scripti koy + Build (jar burada oluşur)

1. Editör sol panelde sağ tık → **New Python file** → örn. `wall_alert`
2. `bookmap/wall_alert_addon.py` içeriğinin **tamamını** yapıştırın (VS Code'daki dosyadan kopyalayın)
3. **Save**
4. Gerekirse **Set custom runtime** → sisteminizdeki `python.exe` (3.7–3.12)
5. **Build**'e basın
6. Başarılı olunca: editör menüsü **File → Open build folder**

Bu klasördeki `.jar` dosyası aradığınız add-on jar'ıdır (kısayol `.lnk` değil, gerçek dosya).

Yaygın konum örneği:

`C:\Bookmap\Python\...` veya build folder penceresinin gösterdiği yol

### Adım 4 — Build edilen jar'ı Configure add-ons'a ekle

1. Ana Bookmap: yine **Settings → Configure add-ons → Add...**
2. Az önce açılan **build folder** içindeki `.jar` dosyasını seçin (`.lnk` seçmeyin)
3. Popup'ta add-on'u **Load** edin
4. Listede çıkan satırın yanına **mavi tik** koyun (Enable)

Konsolda / log'da şunu arayın: `[wall_alert] depth subscribed: ...`  
(Log: Bookmap **File → Show log file**, `[PYTHON-CLIENT]` satırları)

### Adım 5 — VS Code köprüsü

```powershell
cd <666X-repo-klasörü>
python bookmap_telegram_bridge.py --dry-run
```

Add-on yazınca `output\bookmap_events.jsonl` oluşur; köprü canlı satır basar.

### Yol bulucu

```powershell
powershell -ExecutionPolicy Bypass -File bookmap\find_paths.ps1
```

---

## Sık karışıklıklar

| Yanlış | Doğru |
|--------|--------|
| Plugins manager'da jar aramak | Orada sadece **Python API Install** yapılır |
| Masaüstü `.lnk` seçmek | Build folder'daki gerçek `.jar` |
| `C:\Program Files\Bookmap\Bookmap.jar` eklemek | Bu uygulama jar'ı; add-on değil |
| Repo'dan hazır `book.jar` beklemek | Jar **Build** butonuyla üretilir |
| VS Code'dan `wall_alert_addon.py` çalıştırmak | Add-on Bookmap editöründe Build + Enable ile çalışır |

---

## Gereksinimler

- Bookmap 7.4+
- Python 3.7.14+ (3.13 sorun çıkarabilir)
- Plugins manager → **Python API** kurulu

> `bookmap` paketi normal pip ile Bookmap dışında çalışmaz; script Bookmap'in Python ortamında koşar.

## Sık hata: `Import "bookmap" could not be resolved`

VS Code'da Pylance uyarısı **normaldir**. Dosyayı VS Code'dan `python ...` ile çalıştırmayın.

| Dosya | Nerede? |
|-------|---------|
| `bookmap/wall_alert_addon.py` | Bookmap embedded editor → Build → Enable |
| `bookmap_telegram_bridge.py` | VS Code / PowerShell |

## Nasıl çalışır?

```
Bookmap (canlı veri)
    │
    ▼
wall_alert_addon.py  --Build-->  *.jar  --Enable-->  output/bookmap_events.jsonl
                                                         │
                                                         ▼
                                               bookmap_telegram_bridge.py
                                                         │
                                                         ▼
                                                   ekran / Telegram
```

### Olaylar

| Olay | Açıklama |
|------|----------|
| `wall_detected` | Büyük likidite duvarı |
| `wall_removed` | Duvar kalktı / emildi |
| `price_near_wall` | Fiyat duvara yaklaştı |

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

Add-on dosya yolunu bulamazsa Bookmap'ten `BOOKMAP_ROOT` ortam değişkenini repo köküne ayarlayın; yoksa olaylar `bookmap/` yanına yazılabilir — köprüyü `--events` ile o yola yönlendirin.

## Sınırlamalar

- Python API beta; replay desteklenmiyor
- BookmapData / dxFeed için Java API gerekir
- Her Build sonrası genelde yeni jar'ı Configure add-ons'a yeniden eklemeniz gerekebilir

## Kaynaklar

- [Python API Quick Guide](https://docs.google.com/document/d/178YRno3iKKdbuvVjVh380ayR-VsSUlQGZt2tDFjjD3A)
- [Bookmap Python API GitHub](https://github.com/BookmapAPI/python-api)
- [KB — Python API](https://bookmap.com/knowledgebase/docs/Addons-Python-API)
