# Bilgisayar yeniden açılınca — Bookmap köprüsü

Auto load açıksa jar genelde gelir; yine de bu sırayı izleyin.

## 1) Bookmap

1. Bookmap'i açın
2. BTC-USDT (veya izlediğiniz) enstrümana bağlanın
3. **Settings → Configure add-ons**
4. **Python API** mavi tik
5. **yenibot** mavi tik (yoksa **Add...** → `C:\Users\Rahman\OneDrive\Desktop\bot\yenibot.jar` veya `C:\Bookmap\Python\build\yenibot.jar`)
6. İsterseniz **Auto enable** + **Auto load** tikli bırakın
7. Ayar: Min wall size `200`–`500`, Near wall % `0.2` → **Apply** → **CLOSE**

## 2) VS Code köprü

```powershell
cd C:\Users\Rahman\OneDrive\Desktop\bot
python bookmap_telegram_bridge.py --dry-run
```

`watching ...bookmap_events.jsonl` görünce hazır.

Telegram için (opsiyonel):

```powershell
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHAT_ID="..."
python bookmap_telegram_bridge.py
```

## 3) Çalışıyor mu?

- `C:\Users\Rahman\OneDrive\Desktop\bot\output\bookmap_events.jsonl` büyüyorsa OK
- Çok satır geliyorsa Min wall size'ı yükseltin

## Notlar

- `yenibot.py` / `bot.py` VS Code'dan çalıştırılmaz
- Jar yeniden Build etmeye gerek yok (kod değişmedikçe)
- Kod güncellerseniz: Bookmap editor → Build → eski yenibot Remove → yeni jar Add
