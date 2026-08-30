# RATIO — TA Lab

Tüm trader analizlerini **otomatik çalıştıran** motor + ekran.

## Ne yapar?
1. Binance mum çeker  
2. Price action, Fib/Gann/Pivot, formasyon, Elliot/Wyckoff, mumlar, indikatörler, VPVR çalıştırır  
3. Oyları birleştirir → **tek skor / bias / 10 saatlik bant**

## Web
```bash
cd ta-lab
npm install
npm run dev
```

## CLI (headless)
```bash
python ta-lab/engine.py --symbol BTCUSDT --interval 1h
python ta-lab/engine.py --symbol SUSDT --json
```

Eğitim amaçlıdır; yatırım tavsiyesi değildir.
