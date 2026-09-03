# Bookmap + bot klasörü (Desktop)

## Alış yeşil / satış kırmızı

Ekrandaki `bookmap_events.jsonl` dosyasında tüm satırlar aynı renkte görünür. Artık üç yerde renk var:

| Yer | Nasıl |
|-----|--------|
| **VS Code** | `python bookmap_bridge.py --boya` → `bookmap_events.diff` dosyasını aç (alış yeşil, satış kırmızı) |
| **Tarayıcı** | `python bookmap_bridge.py --viewer` |
| **Terminal / Telegram** | 🟢 ALIŞ · 🔴 SATIŞ |

Bookmap add-on yeni olaylarda `bookmap_events.diff` dosyasını da otomatik yazar.

## Kurulum (Desktop\\bot)

Hedef klasör:

`C:\Users\Rahman\OneDrive\Desktop\bot`  
(Türkçe Windows’ta `Masaüstü` olabilir — script ikisini de dener.)

Kopyalanacaklar: `book.py`, `bookmap_bridge.py`, `side_color.py`, `events_viewer.html`, `bookmap_alerts.json`

### 1) Bookmap code editor

1. Sağdaki **Bookmap code editor**’de mevcut `book.py` / `book.py.py` içeriğini silin  
2. Repo’daki `bot/book.py` içeriğini yapıştırın → **Save**  
3. Canlı grafikte add-on’u **Enable** edin  
4. Logda şunu görün:
   - `[book] depth subscribed: ...`
   - `[book] ready — events -> C:\Users\Rahman\OneDrive\Desktop\bot\output\bookmap_events.jsonl`
   - `[book] 🟢 ALIŞ ...` veya `[book] 🔴 SATIŞ ...`

### 2) VS Code köprüsü

```powershell
cd C:\Users\Rahman\OneDrive\Desktop\bot
.\venv\Scripts\python.exe bookmap_bridge.py
```

Mevcut uzun JSONL’i VS Code’da boyamak:

```powershell
.\venv\Scripts\python.exe bookmap_bridge.py --boya
# sonra output\bookmap_events.diff dosyasını VS Code’da açın
```

Canlı renkli görünüm (tarayıcı):

```powershell
.\venv\Scripts\python.exe bookmap_bridge.py --viewer
```

## Hızlı test (Bookmap olmadan)

```powershell
New-Item -ItemType Directory -Force -Path .\output | Out-Null
'{"type":"wall_detected","alias":"TEST","side":"ask","price":80000,"size":850000,"mid_price":76880,"ts":"2026-09-02T12:00:00Z"}' |
  Out-File -Encoding utf8 -Append .\output\bookmap_events.jsonl
'{"type":"wall_detected","alias":"TEST","side":"bid","price":79000,"size":500000,"mid_price":76880,"ts":"2026-09-02T12:00:01Z"}' |
  Out-File -Encoding utf8 -Append .\output\bookmap_events.jsonl
.\venv\Scripts\python.exe bookmap_bridge.py --once --replay
```

Terminalde 🔴 SATIŞ ve 🟢 ALIŞ satırları görünmeli.
