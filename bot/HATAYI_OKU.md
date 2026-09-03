# Bu hatayı neden alıyorsun?

Ekrandaki hata:

```text
ModuleNotFoundError: No module named 'bookmap'
```

**Normal.** `book.py` / `bot tr.py` dosyasını VS Code veya PowerShell’den çalıştırıyorsun.
`bookmap` paketi pip ile kurulmaz; **sadece Bookmap uygulamasının içinde** vardır.

Ayrıca:

```text
ModuleNotFoundError: No module named 'side_color'
```

`bookmap_bridge.py` çalıştırıyorsan yanında `side_color.py` olmalı.

---

## Doğru kurulum (2 parça)

### A) Bookmap (VS Code değil!)

1. Bookmap’i aç  
2. **Bookmap code editor**’ü aç  
3. Repo’daki `bot/book.py` içeriğini yapıştır → **Save**  
4. Canlı grafikte add-on’u **Enable** et  
5. Logda şunu ara: `ready — events -> ...\bookmap_events.jsonl`

`bot tr.py` diye VS Code’dan ▶ Run yapma.

### B) VS Code köprüsü

Klasör şöyle olsun (isim önemli):

```text
C:\Users\Rahman\OneDrive\Desktop\bot\
  book.py
  bookmap_bridge.py
  side_color.py
  events_viewer.html
  bookmap_alerts.json
  output\
```

Şu an sen `Desktop\Yeni klasör\bot tr.py` kullanıyorsun — yanlış yer / yanlış dosya adı.

PowerShell:

```powershell
mkdir C:\Users\Rahman\OneDrive\Desktop\bot -Force
cd C:\Users\Rahman\OneDrive\Desktop\bot
# buraya bookmap_bridge.py + side_color.py + events_viewer.html kopyala
python bookmap_bridge.py
```

Telegram için:

```powershell
copy .env.example .env
# TELEGRAM_BOT_TOKEN doldur (CHAT_ID=5555764362 hazır)
python bookmap_bridge.py --telegram
```

---

## Kısa özet

| Dosya | Nerede çalışır? |
|--------|------------------|
| `book.py` | **Sadece Bookmap** code editor |
| `bookmap_bridge.py` | VS Code / PowerShell |
| `side_color.py` | Bridge ile aynı klasörde durur, tek başına çalıştırılmaz |
