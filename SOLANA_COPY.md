# Solana cüzdan → Binance kopya (örnek)

Dosya: `solana_binance_copy.py`

İzlenen: `HxjwdF326ZunmUwC1iXhfgL3ku78YsksN6n7Rfxzwr6b`

```powershell
pip install requests
# dosyada BINANCE_API_KEY / SECRET yaz
# önerilir: HELIUS_API_KEY yaz (public RPC ban yer)
python solana_binance_copy.py
```

- USDT ÷ 8 = slot; her AL max 1 slot
- Token Binance’te yoksa `SKIP`
- İlk açılışta geçmiş tx kopyalanmaz
