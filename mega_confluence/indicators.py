#!/usr/bin/env python3
"""Technical indicator helpers (pure Python, no numpy required)."""

from __future__ import annotations

from typing import Sequence


def sma(vals: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    if period <= 0 or len(vals) < period:
        return out
    s = sum(vals[:period])
    out[period - 1] = s / period
    for i in range(period, len(vals)):
        s += vals[i] - vals[i - period]
        out[i] = s / period
    return out


def ema(vals: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    if len(vals) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(vals[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_g, avg_l = gains / period, losses / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def atr(candles: Sequence[dict], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(candles)
    if len(candles) < period + 1:
        return out
    trs: list[float] = [0.0]
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        tr = max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
        trs.append(tr)
    s = sum(trs[1 : period + 1])
    out[period] = s / period
    for i in range(period + 1, len(candles)):
        s = out[i - 1] * (period - 1) + trs[i]  # type: ignore[operator]
        out[i] = s / period
    return out


def macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    ef = ema(closes, fast)
    es = ema(closes, slow)
    line: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if ef[i] is not None and es[i] is not None:
            line[i] = ef[i] - es[i]  # type: ignore[operator]
    # signal on non-None macd values
    filled = [x if x is not None else 0.0 for x in line]
    # only start after slow period
    sig_raw = ema(filled, signal)
    hist: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if line[i] is not None and sig_raw[i] is not None and i >= slow + signal - 2:
            hist[i] = line[i] - sig_raw[i]  # type: ignore[operator]
        else:
            sig_raw[i] = None
            if i < slow + signal - 2:
                line[i] = line[i]  # keep
    return line, sig_raw, hist


def stochastic(
    candles: Sequence[dict], k_period: int = 14, d_period: int = 3
) -> tuple[list[float | None], list[float | None]]:
    k: list[float | None] = [None] * len(candles)
    for i in range(k_period - 1, len(candles)):
        window = candles[i - k_period + 1 : i + 1]
        hi = max(c["high"] for c in window)
        lo = min(c["low"] for c in window)
        rng = hi - lo or 1e-12
        k[i] = 100 * (candles[i]["close"] - lo) / rng
    d = sma([x if x is not None else 0.0 for x in k], d_period)
    for i in range(len(d)):
        if k[i] is None:
            d[i] = None
    return k, d


def bollinger(
    closes: Sequence[float], period: int = 20, mult: float = 2.0
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    mid = sma(closes, period)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        if mid[i] is None:
            continue
        window = closes[i - period + 1 : i + 1]
        mean = mid[i]
        var = sum((x - mean) ** 2 for x in window) / period  # type: ignore[operator]
        std = var**0.5
        upper[i] = mean + mult * std  # type: ignore[operator]
        lower[i] = mean - mult * std  # type: ignore[operator]
    return lower, mid, upper


def adx(candles: Sequence[dict], period: int = 14) -> list[float | None]:
    n = len(candles)
    out: list[float | None] = [None] * n
    if n < period * 2:
        return out
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = candles[i]["high"] - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        tr[i] = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i - 1]["close"]),
            abs(candles[i]["low"] - candles[i - 1]["close"]),
        )
    atr_s = sum(tr[1 : period + 1])
    pdm_s = sum(plus_dm[1 : period + 1])
    mdm_s = sum(minus_dm[1 : period + 1])
    dx_vals: list[float] = []
    for i in range(period, n):
        if i > period:
            atr_s = atr_s - atr_s / period + tr[i]
            pdm_s = pdm_s - pdm_s / period + plus_dm[i]
            mdm_s = mdm_s - mdm_s / period + minus_dm[i]
        if atr_s == 0:
            dx_vals.append(0.0)
            continue
        pdi = 100 * pdm_s / atr_s
        mdi = 100 * mdm_s / atr_s
        denom = pdi + mdi or 1e-12
        dx = 100 * abs(pdi - mdi) / denom
        dx_vals.append(dx)
        if len(dx_vals) >= period:
            out[i] = sum(dx_vals[-period:]) / period
    return out


def obv(candles: Sequence[dict]) -> list[float]:
    out = [0.0]
    for i in range(1, len(candles)):
        if candles[i]["close"] > candles[i - 1]["close"]:
            out.append(out[-1] + candles[i]["volume"])
        elif candles[i]["close"] < candles[i - 1]["close"]:
            out.append(out[-1] - candles[i]["volume"])
        else:
            out.append(out[-1])
    return out


def mfi(candles: Sequence[dict], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(candles)
    tp = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles]
    raw_mf = [tp[i] * candles[i]["volume"] for i in range(len(candles))]
    for i in range(period, len(candles)):
        pos = neg = 0.0
        for j in range(i - period + 1, i + 1):
            if tp[j] > tp[j - 1]:
                pos += raw_mf[j]
            elif tp[j] < tp[j - 1]:
                neg += raw_mf[j]
        if neg == 0:
            out[i] = 100.0
        else:
            out[i] = 100 - 100 / (1 + pos / neg)
    return out


def vwap_series(candles: Sequence[dict]) -> list[float | None]:
    out: list[float | None] = [None] * len(candles)
    cum_pv = 0.0
    cum_v = 0.0
    for i, c in enumerate(candles):
        typical = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv += typical * c["volume"]
        cum_v += c["volume"]
        out[i] = cum_pv / cum_v if cum_v else None
    return out


def anchored_vwap(candles: Sequence[dict], anchor_idx: int) -> float | None:
    if anchor_idx < 0 or anchor_idx >= len(candles):
        return None
    cum_pv = 0.0
    cum_v = 0.0
    for c in candles[anchor_idx:]:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv += typical * c["volume"]
        cum_v += c["volume"]
    return cum_pv / cum_v if cum_v else None


def linear_reg_slope(vals: Sequence[float]) -> float:
    n = len(vals)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = (n - 1) / 2
    my = sum(vals) / n
    num = sum((xs[i] - mx) * (vals[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n)) or 1e-12
    return num / den


def pct_change(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def last_valid(series: Sequence[float | None]) -> float | None:
    for x in reversed(series):
        if x is not None:
            return float(x)
    return None


def swing_highs_lows(
    candles: Sequence[dict], left: int = 2, right: int = 2
) -> tuple[list[int], list[int]]:
    highs: list[int] = []
    lows: list[int] = []
    for i in range(left, len(candles) - right):
        h = candles[i]["high"]
        l = candles[i]["low"]
        if all(h >= candles[i - j]["high"] for j in range(1, left + 1)) and all(
            h >= candles[i + j]["high"] for j in range(1, right + 1)
        ):
            highs.append(i)
        if all(l <= candles[i - j]["low"] for j in range(1, left + 1)) and all(
            l <= candles[i + j]["low"] for j in range(1, right + 1)
        ):
            lows.append(i)
    return highs, lows
