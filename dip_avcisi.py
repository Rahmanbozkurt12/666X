#!/usr/bin/env python3
"""
0.10$ altı düşen USDT paritelerinde 1h Bollinger alt bant dip avcısı.
Varsayılan dry-run: emir atmaz. Canlı için LIVE=1.

  export BINANCE_API_KEY=...
  export BINANCE_API_SECRET=...
  python dip_avcisi.py
"""

import os
import time

import pandas as pd
from binance.client import Client

LIVE = os.environ.get("LIVE") == "1"
client = Client(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])

MAX_PRICE, TP, SL, WAIT = 0.10, 0.05, 0.03, 30
pos, entry = None, 0.0


def usdt() -> float:
    return float(client.get_asset_balance(asset="USDT")["free"])


def losers() -> list[tuple[str, float, float]]:
    out = []
    for t in client.get_ticker():
        if not t["symbol"].endswith("USDT"):
            continue
        price, chg = float(t["lastPrice"]), float(t["priceChangePercent"])
        if price < MAX_PRICE and chg < 0:
            out.append((t["symbol"], price, chg))
    return sorted(out, key=lambda x: x[2])


def is_dip(symbol: str) -> bool:
    df = pd.DataFrame(client.get_klines(symbol=symbol, interval="1h", limit=50))
    close = df[4].astype(float)
    bb = close.rolling(20).mean() - 2 * close.rolling(20).std()
    return bool(close.iloc[-1] <= bb.iloc[-1] * 1.01)


print("[DİP AVCISI] 0.10$ altı | dry-run" if not LIVE else "[DİP AVCISI] CANLI")

while True:
    try:
        if pos:
            price = float(client.get_symbol_ticker(symbol=pos)["price"])
            pnl = (price - entry) / entry
            print(f"[TAKİP] {pos} alış={entry} anlık={price} %{pnl*100:.2f}")
            if pnl >= TP or pnl <= -SL:
                qty = float(client.get_asset_balance(asset=pos.replace("USDT", ""))["free"])
                print(f"[SATIŞ] {pos} qty={qty}")
                if LIVE:
                    client.order_market_sell(symbol=pos, quantity=qty)
                pos, entry = None, 0.0
            time.sleep(WAIT)
            continue

        bal = usdt()
        if bal < 10:
            print(f"[BEKLEME] USDT={bal:.2f} (min 10)")
            time.sleep(60)
            continue

        found = False
        coins = losers()
        print(f"[TARAMA] {len(coins)} düşen coin")
        for symbol, price, chg in coins:
            if not is_dip(symbol):
                continue
            print(f"[DİP] {symbol} {price} %{chg} → {bal:.2f} USDT")
            if LIVE:
                client.order_market_buy(symbol=symbol, quoteOrderQty=bal)
            pos, entry, found = symbol, price, True
            break
        if not found:
            print("[BEKLEME] dip yok")
        time.sleep(WAIT)
    except Exception as e:
        print(f"[HATA] {e}")
        time.sleep(30)
