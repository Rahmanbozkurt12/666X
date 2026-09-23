# Phantom key → Jupiter AL/SAT bot

Phantom’da bot API key **yok**. Bot, Phantom’dan export ettiğin **private key** ile Jupiter swap imzalar. Sen sadece scripti açık bırakırsın.

## 1) Ayrı trading cüzdanı (zorunlu)

1. Phantom’da **yeni hesap** oluştur (ana seed’i bota verme).
2. O hesaba biraz **SOL** at (gas + alım).
3. **Settings → Security & Privacy → Export Private Key** → base58 kopyala.

## 2) Kurulum

```bash
pip install requests solders base58
export SOLANA_PRIVATE_KEY='phantom_export_base58'
# önerilir (RPC ban azalsın):
export HELIUS_API_KEY='...'
```

`phantom_jupiter_bot.py` içinde:

- `DRY_RUN = True` → önce sadece quote/log (zincire gitmez)
- `TOKENS` listesine mint ekle, `"enabled": True` yap
- `buy_sol`, `take_profit_pct`, `stop_loss_pct` ayarla

## 3) Çalıştır

```bash
python phantom_jupiter_bot.py
```

Bot döngüde:

1. Jupiter’dan yol (route) alır  
2. AL: SOL → token  
3. Fiyat `take_profit` / `stop_loss`’a gelince SAT: token → SOL  

## 4) Canlı

1. `DRY_RUN = False`
2. Küçük `buy_sol` ile dene
3. Terminali açık bırak (veya `tmux` / `screen`)

## Güvenlik

- Ana Phantom seed’ini asla scripte koyma
- Key’i Git’e commit etme
- Sadece o trading cüzdanına koyduğun SOL risk altında
