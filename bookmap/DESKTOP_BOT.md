# Masaüstü `bot` klasörü — 2 dakikalık düzeltme

Klasörünüz: `C:\Users\Rahman\OneDrive\Desktop\bot`  
Add-on: `New Folder\bot.py`  
Köprü: henüz yoktu → aşağıdaki komut indirir.

## Terminal hataları

| Hata | Anlamı |
|------|--------|
| `No module named 'bookmap'` | **Normal.** `bot.py` VS Code'dan çalışmaz. Bookmap editöründe Build edilir. |
| `can't open file ... bookmap_telegram_bridge.py` | Dosya klasörde yoktu. Aşağıdaki indirme komutunu çalıştırın. |

## VS Code terminalinde (Desktop\bot)

```powershell
cd C:\Users\Rahman\OneDrive\Desktop\bot

Invoke-WebRequest -Uri "https://raw.githubusercontent.com/Rahmanbozkurt12/666X/cursor/bookmap-jar-path-fix-7c26/bookmap_telegram_bridge.py" -OutFile "bookmap_telegram_bridge.py"

pip install requests

python bookmap_telegram_bridge.py --dry-run
```

Beklenen çıktı: `watching ...bookmap_events.jsonl` ve dosya yoksa `[bekle]`. Bu nöbetçi doğru durumdur — Bookmap tarafı yazınca satırlar akar.

`bot.py` hâlâ `New Folder` altındaysa:

```powershell
python bookmap_telegram_bridge.py --dry-run --events "New Folder\output\bookmap_events.jsonl"
```

## Bookmap tarafı (jar burada oluşur)

1. `bot.py`yi VS Code'da **çalıştırmayın**
2. Bookmap → **Settings → Configure add-ons** → **Python API** mavi tik → **Open embedded editor**
3. `New Folder\bot.py` içeriğini yapıştır → **Build**
4. **File → Open build folder** → `.jar` yolunu kopyalayın
5. Configure add-ons → **Add...** → o `.jar` (`.lnk` değil) → **mavi tik**

Jar hazır olunca bir foto atın; bir sonraki adımı ona göre söyleriz.
