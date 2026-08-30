#!/usr/bin/env python3
"""Headless all-in-one TA engine (Binance klines → full verdict)."""

from __future__ import annotations

import argparse
import json
import math
import urllib.request
from dataclasses import dataclass
from typing import Any


BASES = [
    "https://www.binance.com/api/v3/klines",
    "https://data-api.binance.vision/api/v3/klines",
]


def fetch_klines(symbol: str, interval: str, limit: int = 500) -> list[dict]:
    last_err: Exception | None = None
    for base in BASES:
        try:
            url = f"{base}?symbol={symbol}&interval={interval}&limit={limit}"
            with urllib.request.urlopen(url, timeout=30) as r:
                raw = json.loads(r.read().decode())
            return [
                {
                    "time": int(c[0]) // 1000,
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                }
                for c in raw
            ]
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(last_err)


def ema(vals: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    if len(vals) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(vals[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = (d if d > 0 else 0.0), (-d if d < 0 else 0.0)
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(candles: list[dict], period: int = 14) -> float:
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])))
    slice_ = trs[-period:]
    return sum(slice_) / len(slice_) if slice_ else 0.0


def analyze(symbol: str, interval: str, candles: list[dict]) -> dict[str, Any]:
    closes = [c["close"] for c in candles]
    last = closes[-1]
    hi = max(c["high"] for c in candles[-120:])
    lo = min(c["low"] for c in candles[-120:])
    rng = hi - lo or 1e-9
    fib618 = lo + rng * 0.618
    prev = candles[-2]
    pp = (prev["high"] + prev["low"] + prev["close"]) / 3
    e7, e25 = ema(closes, 7)[-1], ema(closes, 25)[-1]
    r = rsi(closes)
    a = atr(candles)
    chg40 = (closes[-1] - closes[-40]) / closes[-40] if len(closes) > 40 else 0

    votes = []
    votes.append(("Trend", "bull" if chg40 > 0.02 else "bear" if chg40 < -0.02 else "neutral", 18))
    votes.append(("Fib618", "bull" if last > fib618 else "bear", 14))
    votes.append(("Pivot", "bull" if last > pp else "bear", 10))
    if e7 is not None and e25 is not None:
        votes.append(("EMA7/25", "bull" if e7 > e25 else "bear", 12))
    if r is not None:
        votes.append(("RSI", "bear" if r >= 70 else "bull" if r <= 30 else "neutral", 12))

    score = sum((1 if b == "bull" else -1 if b == "bear" else 0) * w for _, b, w in votes)
    tw = sum(w for _, _, w in votes)
    norm = (score / tw) * 100 if tw else 0
    bias = "bull" if norm >= 15 else "bear" if norm <= -15 else "neutral"

    hourly = []
    center = last
    drift = 0.15 if bias == "bull" else -0.15 if bias == "bear" else 0
    for h in range(1, 11):
        center *= 1 + (drift * a) / last
        half = a * (0.55 + h * 0.04)
        hourly.append({"hour": h, "low": center - half, "high": center + half})

    return {
        "symbol": symbol,
        "interval": interval,
        "last": last,
        "bias": bias,
        "score": round(norm, 1),
        "rsi": None if r is None else round(r, 2),
        "atr": a,
        "fib618": fib618,
        "pivot": pp,
        "targets": {"down5": last * 0.95, "up10": last * 1.1},
        "votes": [{"method": m, "bias": b, "weight": w} for m, b, w in votes],
        "hourly": hourly,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="All-in-one TA engine")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", default="1h")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    candles = fetch_klines(args.symbol.upper(), args.interval)
    out = analyze(args.symbol.upper(), args.interval, candles)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"{out['symbol']} {out['interval']} last={out['last']:.4f}")
        print(f"VERDICT: {out['bias'].upper()} score={out['score']} RSI={out['rsi']}")
        print(f"Fib618={out['fib618']:.4f} PP={out['pivot']:.4f}")
        print("10h bands:")
        for h in out["hourly"]:
            print(f"  +{h['hour']}h  {h['low']:.4f} – {h['high']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
