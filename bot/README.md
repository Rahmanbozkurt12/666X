# Bookmap + bot klasörü (Desktop)

Ekran görüntünüzdeki hata:

```text
SyntaxError: unterminated string literal (detected at line 65)
output = Path(r"C:\Users\Rahman\OneDrive\desk...
```

`bookmap_bridge.py` içinde yol satırı yarım kalmıştı (tırnak kapanmamış). Bu klasördeki dosyalar düzeltilmiş tam kopyadır.

## Kurulum (2 dosya)

Hedef klasör:

`C:\Users\Rahman\OneDrive\Desktop\bot`  
(Türkçe Windows’ta `Masaüstü` olabilir — script ikisini de dener.)

### 1) Bookmap code editor

1. Sağdaki **Bookmap code editor**’de mevcut `book.py` / `book.py.py` içeriğini silin  
2. Repo’daki `bot/book.py` içeriğini yapıştırın → **Save**  
3. Canlı grafikte add-on’u **Enable** edin  
4. Logda şunu görün:
   - `[book] depth subscribed: ...`
   - `[book] ready — events -> C:\Users\Rahman\OneDrive\Desktop\bot\output\bookmap_events.jsonl`

**Düzeltmeler (eski book.py’de bozulanlar):**
- `bm.on_mbo(...)` diye bir API **yok** → `on_new_order` / `on_replace_order` / `on_remove_order`
- `bm.add_on_interval_handler(addon, handler, ms)` **yanlış** → sadece 2 argüman: `(addon, handler)`
- `get_bbos` None dönünce çökme engellendi

### 2) VS Code köprüsü

1. `bot/bookmap_bridge.py` dosyasını `Desktop\bot\bookmap_bridge.py` olarak kopyalayın (eski bozuk dosyanın üstüne)  
2. Terminal:

```powershell
cd C:\Users\Rahman\OneDrive\Desktop\bot
.\venv\Scripts\python.exe bookmap_bridge.py
```

Beklenen çıktı:

```text
Bookmap Canlı Likidite Analizörü Başlatıldı...
Dosya izleniyor: C:\Users\Rahman\OneDrive\Desktop\bot\output\bookmap_events.jsonl
```

Dosya yoksa Bookmap Enable edilince oluşur. Hâlâ yoksa Bookmap logundaki `events ->` yolunu `--events` ile verin:

```powershell
.\venv\Scripts\python.exe bookmap_bridge.py --events "C:\Users\Rahman\OneDrive\Desktop\bot\output\bookmap_events.jsonl"
```

## Hızlı test (Bookmap olmadan)

```powershell
New-Item -ItemType Directory -Force -Path .\output | Out-Null
'{"type":"wall_detected","alias":"TEST","side":"ask","price":80000,"size":850000,"mid_price":76880,"ts":"2026-09-02T12:00:00Z"}' |
  Out-File -Encoding utf8 -Append .\output\bookmap_events.jsonl
.\venv\Scripts\python.exe bookmap_bridge.py --once --replay
```

`DUVAR  TEST  SATIŞ ... @ 80000` görmelisiniz.
