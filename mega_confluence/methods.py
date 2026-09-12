#!/usr/bin/env python3
"""
128 crypto analysis methods → each returns a Vote in [-1, +1].

Methods that need private/on-chain APIs use public proxies when possible,
otherwise return neutral (0) with low weight so confluence stays honest.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Callable

from . import indicators as ind


@dataclass
class Vote:
    id: int
    name: str
    score: float  # -1 bear … +1 bull
    weight: float
    detail: str
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


MethodFn = Callable[[dict[str, Any]], Vote]


def _v(i: int, name: str, score: float, weight: float, detail: str, ok: bool = True) -> Vote:
    return Vote(i, name, ind.clamp(score), weight, detail, ok)


def _closes(c: list[dict]) -> list[float]:
    return [x["close"] for x in c]


def _neutral(i: int, name: str, reason: str, weight: float = 0.3) -> Vote:
    return _v(i, name, 0.0, weight, reason, ok=False)


# ─── Individual methods ─────────────────────────────────────────────────────


def m01_price_action(b: dict) -> Vote:
    c = b["candles"]
    last3 = c[-3:]
    body = last3[-1]["close"] - last3[-1]["open"]
    rng = last3[-1]["high"] - last3[-1]["low"] or 1e-12
    close_loc = (last3[-1]["close"] - last3[-1]["low"]) / rng
    bull_engulf = (
        last3[-2]["close"] < last3[-2]["open"]
        and last3[-1]["close"] > last3[-1]["open"]
        and last3[-1]["close"] >= last3[-2]["open"]
        and last3[-1]["open"] <= last3[-2]["close"]
    )
    score = (close_loc - 0.5) * 1.4 + (body / rng) * 0.6
    if bull_engulf:
        score += 0.35
    return _v(1, "Price Action", score, 1.4, f"close_loc={close_loc:.2f} body_ratio={body/rng:.2f}")


def m02_market_structure(b: dict) -> Vote:
    c = b["candles"]
    highs, lows = ind.swing_highs_lows(c)
    if len(highs) < 2 or len(lows) < 2:
        return _v(2, "Market Structure", 0.0, 1.3, "insufficient swings")
    hh = c[highs[-1]]["high"] > c[highs[-2]]["high"]
    hl = c[lows[-1]]["low"] > c[lows[-2]]["low"]
    lh = c[highs[-1]]["high"] < c[highs[-2]]["high"]
    ll = c[lows[-1]]["low"] < c[lows[-2]]["low"]
    if hh and hl:
        return _v(2, "Market Structure", 0.85, 1.5, "HH+HL uptrend structure")
    if lh and ll:
        return _v(2, "Market Structure", -0.85, 1.5, "LH+LL downtrend structure")
    return _v(2, "Market Structure", 0.1 if hh or hl else -0.1, 1.0, "mixed structure")


def m03_wyckoff(b: dict) -> Vote:
    c = b["candles"][-80:]
    closes = _closes(c)
    vols = [x["volume"] for x in c]
    mid = len(c) // 2
    range_early = max(closes[:mid]) - min(closes[:mid])
    range_late = max(closes[mid:]) - min(closes[mid:])
    vol_early = sum(vols[:mid]) / mid
    vol_late = sum(vols[mid:]) / (len(vols) - mid)
    spring = closes[-1] > min(closes[-20:-1]) and min(closes[-5:]) <= min(closes[-40:-5]) * 1.002
    markup = closes[-1] > max(closes[:-5]) and vol_late > vol_early * 1.1
    if spring and vol_late > vol_early:
        return _v(3, "Wyckoff", 0.8, 1.4, "spring + volume → accumulation bias")
    if markup:
        return _v(3, "Wyckoff", 0.65, 1.2, "markup phase bias")
    if range_late < range_early * 0.7 and closes[-1] < closes[0]:
        return _v(3, "Wyckoff", -0.55, 1.1, "distribution / range contraction down")
    return _v(3, "Wyckoff", 0.05, 0.8, "neutral phase")


def m04_support_resistance(b: dict) -> Vote:
    c = b["candles"]
    last = c[-1]["close"]
    window = c[-120:]
    hi = max(x["high"] for x in window)
    lo = min(x["low"] for x in window)
    # distance to nearest S/R
    levels = [lo, hi, (hi + lo) / 2]
    # recent swing levels
    highs, lows = ind.swing_highs_lows(window)
    levels += [window[i]["high"] for i in highs[-5:]]
    levels += [window[i]["low"] for i in lows[-5:]]
    near = min(levels, key=lambda lv: abs(lv - last))
    dist = (last - near) / last
    # bounce from support
    if last > near and abs(dist) < 0.008:
        return _v(4, "Support / Resistance", 0.55, 1.1, f"holding near support {near:.6g}")
    if last < near and abs(dist) < 0.008:
        return _v(4, "Support / Resistance", -0.55, 1.1, f"rejecting near resistance {near:.6g}")
    pos = (last - lo) / (hi - lo or 1e-12)
    return _v(4, "Support / Resistance", (pos - 0.5) * 0.6, 0.8, f"range_pos={pos:.2f}")


def m05_trend_analysis(b: dict) -> Vote:
    closes = _closes(b["candles"])
    slope = ind.linear_reg_slope(closes[-40:])
    norm = slope / (closes[-1] or 1e-12) * 40
    return _v(5, "Trend Analysis", ind.clamp(norm * 8), 1.2, f"slope_norm={norm:.4f}")


def m06_trendline(b: dict) -> Vote:
    c = b["candles"]
    _, lows = ind.swing_highs_lows(c)
    if len(lows) < 2:
        return _v(6, "Trendline", 0.0, 0.7, "not enough lows")
    i0, i1 = lows[-2], lows[-1]
    y0, y1 = c[i0]["low"], c[i1]["low"]
    # project to now
    if i1 == i0:
        return _v(6, "Trendline", 0.0, 0.5, "degenerate")
    slope = (y1 - y0) / (i1 - i0)
    proj = y1 + slope * (len(c) - 1 - i1)
    last = c[-1]["close"]
    dist = (last - proj) / last
    return _v(6, "Trendline", ind.clamp(dist * 40 + (0.3 if slope > 0 else -0.3)), 1.0, f"vs_tl={dist*100:.2f}%")


def m07_chart_patterns(b: dict) -> Vote:
    c = b["candles"][-60:]
    closes = _closes(c)
    # simple double bottom / top
    lo1 = min(closes[:30])
    lo2 = min(closes[30:])
    hi1 = max(closes[:30])
    hi2 = max(closes[30:])
    if abs(lo1 - lo2) / (closes[-1] or 1) < 0.012 and closes[-1] > (lo1 + hi1) / 2:
        return _v(7, "Chart Patterns", 0.7, 1.0, "double-bottom-like")
    if abs(hi1 - hi2) / (closes[-1] or 1) < 0.012 and closes[-1] < (lo1 + hi1) / 2:
        return _v(7, "Chart Patterns", -0.7, 1.0, "double-top-like")
    # triangle compression
    w1 = max(closes[:20]) - min(closes[:20])
    w2 = max(closes[-20:]) - min(closes[-20:])
    if w2 < w1 * 0.55:
        return _v(7, "Chart Patterns", 0.15 if closes[-1] > closes[0] else -0.15, 0.7, "compression")
    return _v(7, "Chart Patterns", 0.0, 0.5, "no clear pattern")


def m08_candlestick(b: dict) -> Vote:
    c = b["candles"][-1]
    p = b["candles"][-2]
    body = abs(c["close"] - c["open"])
    rng = c["high"] - c["low"] or 1e-12
    upper = c["high"] - max(c["close"], c["open"])
    lower = min(c["close"], c["open"]) - c["low"]
    score = 0.0
    detail = []
    if lower > body * 2 and upper < body:
        score += 0.55
        detail.append("hammer")
    if upper > body * 2 and lower < body:
        score -= 0.55
        detail.append("shooting_star")
    if c["close"] > c["open"] and p["close"] < p["open"] and c["close"] > p["open"]:
        score += 0.4
        detail.append("bull_engulf")
    if c["close"] < c["open"] and p["close"] > p["open"] and c["close"] < p["open"]:
        score -= 0.4
        detail.append("bear_engulf")
    if body / rng < 0.1:
        detail.append("doji")
    return _v(8, "Candlestick Patterns", score, 1.0, ",".join(detail) or "plain")


def m09_fib_retracement(b: dict) -> Vote:
    c = b["candles"][-120:]
    hi = max(x["high"] for x in c)
    lo = min(x["low"] for x in c)
    last = c[-1]["close"]
    rng = hi - lo or 1e-12
    # assume upswing then retest
    pos = (last - lo) / rng
    # bullish near 0.618 / 0.5 from high
    ret_from_hi = (hi - last) / rng
    if 0.45 <= ret_from_hi <= 0.65:
        return _v(9, "Fibonacci Retracement", 0.65, 1.1, f"at 50-61.8% pullback ({ret_from_hi:.2f})")
    if ret_from_hi < 0.236:
        return _v(9, "Fibonacci Retracement", 0.35, 0.8, "shallow retrace / strength")
    if pos < 0.236:
        return _v(9, "Fibonacci Retracement", -0.5, 0.9, "deep weakness")
    return _v(9, "Fibonacci Retracement", (pos - 0.5) * 0.5, 0.6, f"pos={pos:.2f}")


def m10_fib_extension(b: dict) -> Vote:
    c = b["candles"][-100:]
    lo = min(x["low"] for x in c[:50])
    hi = max(x["high"] for x in c[30:])
    last = c[-1]["close"]
    swing = hi - lo or 1e-12
    ext = (last - hi) / swing
    if 0.9 <= (last - lo) / swing <= 1.1:
        return _v(10, "Fibonacci Extension", 0.2, 0.7, "near 100% extension — caution")
    if ext > 0.272:
        return _v(10, "Fibonacci Extension", -0.25, 0.8, "overextended above swing")
    if last < hi and last > lo + swing * 0.618:
        return _v(10, "Fibonacci Extension", 0.45, 0.9, "room to 100-127 extension")
    return _v(10, "Fibonacci Extension", 0.0, 0.5, f"ext={ext:.2f}")


def m11_elliott(b: dict) -> Vote:
    closes = _closes(b["candles"][-80:])
    # crude 5-wave impulse detection via alternating swings
    highs, lows = ind.swing_highs_lows(b["candles"][-80:], 1, 1)
    n = len(highs) + len(lows)
    slope = ind.linear_reg_slope(closes)
    if n >= 5 and slope > 0:
        return _v(11, "Elliott Wave", 0.45, 0.7, f"impulse-like swings={n}")
    if n >= 5 and slope < 0:
        return _v(11, "Elliott Wave", -0.45, 0.7, f"bear impulse-like swings={n}")
    return _v(11, "Elliott Wave", 0.0, 0.4, "unclear wave count")


def m12_dow(b: dict) -> Vote:
    c = b["candles"]
    highs, lows = ind.swing_highs_lows(c)
    if len(highs) < 2 or len(lows) < 2:
        return _v(12, "Dow Theory", 0.0, 0.6, "insufficient")
    primary_up = c[highs[-1]]["high"] > c[highs[-2]]["high"] and c[lows[-1]]["low"] > c[lows[-2]]["low"]
    primary_dn = c[highs[-1]]["high"] < c[highs[-2]]["high"] and c[lows[-1]]["low"] < c[lows[-2]]["low"]
    vol_up = b["candles"][-1]["volume"] > sum(x["volume"] for x in c[-20:-1]) / 19
    if primary_up:
        return _v(12, "Dow Theory", 0.7 if vol_up else 0.45, 1.1, "primary up confirmed" if vol_up else "primary up")
    if primary_dn:
        return _v(12, "Dow Theory", -0.7 if vol_up else -0.45, 1.1, "primary down")
    return _v(12, "Dow Theory", 0.0, 0.5, "secondary / unclear")


def m13_volume(b: dict) -> Vote:
    c = b["candles"]
    vols = [x["volume"] for x in c]
    avg = sum(vols[-21:-1]) / 20
    last_v = vols[-1]
    up = c[-1]["close"] >= c[-1]["open"]
    ratio = last_v / (avg or 1e-12)
    score = (0.5 if up else -0.5) * min(ratio / 2, 1.5)
    return _v(13, "Volume Analysis", score, 1.2, f"vol_ratio={ratio:.2f} {'up' if up else 'down'} bar")


def m14_volume_profile(b: dict) -> Vote:
    c = b["candles"][-100:]
    # POC via volume-weighted price bins
    lo = min(x["low"] for x in c)
    hi = max(x["high"] for x in c)
    bins = 24
    step = (hi - lo) / bins or 1e-12
    hist = [0.0] * bins
    for x in c:
        idx = int(((x["high"] + x["low"] + x["close"]) / 3 - lo) / step)
        idx = max(0, min(bins - 1, idx))
        hist[idx] += x["volume"]
    poc_i = max(range(bins), key=lambda i: hist[i])
    poc = lo + (poc_i + 0.5) * step
    last = c[-1]["close"]
    dist = (last - poc) / last
    return _v(14, "Volume Profile", ind.clamp(dist * 25), 1.0, f"poc={poc:.6g} dist={dist*100:.2f}%")


def m15_vwap(b: dict) -> Vote:
    series = ind.vwap_series(b["candles"][-96:])
    v = series[-1]
    last = b["candles"][-1]["close"]
    if v is None:
        return _v(15, "VWAP", 0.0, 0.5, "n/a")
    dist = (last - v) / last
    return _v(15, "VWAP", ind.clamp(dist * 30), 1.1, f"vs_vwap={dist*100:.2f}%")


def m16_anchored_vwap(b: dict) -> Vote:
    c = b["candles"]
    # anchor at lowest low of last 80 bars
    window = c[-80:]
    anchor_local = min(range(len(window)), key=lambda i: window[i]["low"])
    av = ind.anchored_vwap(window, anchor_local)
    last = window[-1]["close"]
    if av is None:
        return _v(16, "Anchored VWAP", 0.0, 0.5, "n/a")
    dist = (last - av) / last
    return _v(16, "Anchored VWAP", ind.clamp(0.2 + dist * 25), 1.0, f"vs_avwap={dist*100:.2f}%")


def m17_footprint(b: dict) -> Vote:
    # proxy: taker buy ratio per candle as footprint imbalance
    c = b["candles"][-20:]
    buy = sum(x.get("taker_buy_base", 0) for x in c)
    vol = sum(x["volume"] for x in c) or 1e-12
    ratio = buy / vol
    return _v(17, "Footprint Chart", ind.clamp((ratio - 0.5) * 4), 1.2, f"taker_buy_share={ratio:.2f}")


def m18_order_flow(b: dict) -> Vote:
    trades = b.get("agg_trades") or []
    if len(trades) < 20:
        return _v(18, "Order Flow", 0.0, 0.4, "few trades", ok=False)
    buy_q = sum(float(t["q"]) for t in trades if not t.get("m"))
    sell_q = sum(float(t["q"]) for t in trades if t.get("m"))
    total = buy_q + sell_q or 1e-12
    imb = (buy_q - sell_q) / total
    return _v(18, "Order Flow", ind.clamp(imb * 2), 1.3, f"buy_sell_imb={imb:.2f}")


def m19_cvd(b: dict) -> Vote:
    trades = b.get("agg_trades") or []
    if not trades:
        # candle proxy
        c = b["candles"][-40:]
        cvd = 0.0
        for x in c:
            delta = 2 * x.get("taker_buy_base", x["volume"] / 2) - x["volume"]
            cvd += delta
        slope = cvd / (sum(x["volume"] for x in c) or 1)
        return _v(19, "CVD", ind.clamp(slope * 3), 1.2, f"candle_cvd_norm={slope:.3f}")
    cvd = 0.0
    series = []
    for t in trades:
        q = float(t["q"])
        cvd += -q if t.get("m") else q
        series.append(cvd)
    slope = ind.linear_reg_slope(series[-100:] if len(series) > 100 else series)
    return _v(19, "CVD", ind.clamp(slope * 50), 1.4, f"cvd_slope={slope:.4f}")


def m20_delta(b: dict) -> Vote:
    c = b["candles"][-10:]
    deltas = [2 * x.get("taker_buy_base", 0) - x["volume"] for x in c]
    avg = sum(deltas) / len(deltas)
    norm = avg / (sum(x["volume"] for x in c) / len(c) or 1)
    return _v(20, "Delta Analysis", ind.clamp(norm * 3), 1.2, f"delta_norm={norm:.3f}")


def m21_bid_ask_imbalance(b: dict) -> Vote:
    depth = b.get("depth") or {}
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    if not bids or not asks:
        return _v(21, "Bid/Ask Imbalance", 0.0, 0.3, "no depth", ok=False)
    bid_sz = sum(float(x[1]) for x in bids[:20])
    ask_sz = sum(float(x[1]) for x in asks[:20])
    imb = (bid_sz - ask_sz) / (bid_sz + ask_sz or 1)
    return _v(21, "Bid/Ask Imbalance", ind.clamp(imb * 2), 1.1, f"book_imb={imb:.2f}")


def m22_order_book(b: dict) -> Vote:
    depth = b.get("depth") or {}
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    if not bids or not asks:
        return _neutral(22, "Order Book Analysis", "no depth")
    mid = (float(bids[0][0]) + float(asks[0][0])) / 2
    # wall detection
    bid_wall = max(bids[:50], key=lambda x: float(x[1]))
    ask_wall = max(asks[:50], key=lambda x: float(x[1]))
    score = 0.0
    if float(bid_wall[1]) > float(ask_wall[1]) * 1.4:
        score += 0.45
    elif float(ask_wall[1]) > float(bid_wall[1]) * 1.4:
        score -= 0.45
    spread = (float(asks[0][0]) - float(bids[0][0])) / mid
    if spread > 0.0015:
        score *= 0.5
    return _v(22, "Order Book Analysis", score, 1.0, f"spread={spread*10000:.1f}bps")


def m23_liquidity(b: dict) -> Vote:
    depth = b.get("depth") or {}
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    if not bids or not asks:
        return _neutral(23, "Liquidity Analysis", "no depth")
    mid = (float(bids[0][0]) + float(asks[0][0])) / 2
    # liquidity within 0.5%
    bid_liq = sum(float(p) * float(q) for p, q in bids if float(p) >= mid * 0.995)
    ask_liq = sum(float(p) * float(q) for p, q in asks if float(p) <= mid * 1.005)
    ratio = bid_liq / (ask_liq or 1)
    score = ind.clamp((ratio - 1) * 0.8)
    return _v(23, "Liquidity Analysis", score, 1.0, f"bid/ask_liq={ratio:.2f}")


def m24_heatmap(b: dict) -> Vote:
    # proxy heatmap: stacked depth density near price
    book = m22_order_book(b)
    return _v(24, "Heatmap Analysis", book.score * 0.8, 0.7, "depth-density proxy (no Bookmap feed)", book.available)


def m25_oi(b: dict) -> Vote:
    hist = b.get("oi_hist") or []
    if len(hist) < 3:
        oi = b.get("open_interest")
        if oi is None:
            return _neutral(25, "Open Interest (OI)", "no OI data")
        return _v(25, "Open Interest (OI)", 0.0, 0.5, f"oi={oi:.2f} (no hist)")
    vals = [float(x.get("sumOpenInterest", x.get("sumOpenInterestValue", 0))) for x in hist]
    chg = (vals[-1] - vals[0]) / (vals[0] or 1)
    price_up = b["candles"][-1]["close"] >= b["candles"][-5]["close"]
    # OI↑ + price↑ = long build; OI↑ + price↓ = short build
    if chg > 0.02 and price_up:
        score = 0.55
    elif chg > 0.02 and not price_up:
        score = -0.45
    elif chg < -0.02 and price_up:
        score = 0.35  # short cover
    elif chg < -0.02 and not price_up:
        score = -0.35
    else:
        score = 0.0
    return _v(25, "Open Interest (OI)", score, 1.3, f"oi_chg={chg*100:.2f}%")


def m26_funding(b: dict) -> Vote:
    prem = b.get("premium") or {}
    fr = prem.get("lastFundingRate")
    if fr is None:
        hist = b.get("funding_hist") or []
        if not hist:
            return _neutral(26, "Funding Rate", "no funding")
        fr = float(hist[-1].get("fundingRate", 0))
    else:
        fr = float(fr)
    # extreme positive funding → crowded long (bearish squeeze risk)
    if fr > 0.0005:
        score = -0.5
    elif fr < -0.0003:
        score = 0.55  # shorts pay → squeeze upside
    else:
        score = -fr * 200
    return _v(26, "Funding Rate", ind.clamp(score), 1.2, f"funding={fr:.6f}")


def m27_long_short(b: dict) -> Vote:
    ls = b.get("long_short") or []
    if not ls:
        return _neutral(27, "Long/Short Ratio", "no L/S data")
    ratio = float(ls[-1].get("longShortRatio", 1))
    # crowded longs = risk; low ratio = contrarian long
    if ratio > 1.8:
        score = -0.55
    elif ratio < 0.85:
        score = 0.55
    else:
        score = (1.2 - ratio) * 0.5
    return _v(27, "Long/Short Ratio", ind.clamp(score), 1.0, f"ls={ratio:.2f}")


def m28_liquidation(b: dict) -> Vote:
    # proxy: sharp price move + OI drop ≈ liquidation cascade aftermath
    hist = b.get("oi_hist") or []
    c = b["candles"]
    if len(hist) < 2:
        return _neutral(28, "Liquidation Analysis", "no liquidation feed; OI proxy weak")
    oi0 = float(hist[-2].get("sumOpenInterest", 1))
    oi1 = float(hist[-1].get("sumOpenInterest", 1))
    oi_chg = (oi1 - oi0) / (oi0 or 1)
    px_chg = (c[-1]["close"] - c[-4]["close"]) / c[-4]["close"]
    if oi_chg < -0.03 and px_chg > 0.01:
        return _v(28, "Liquidation Analysis", 0.6, 1.1, "short-liq cascade proxy")
    if oi_chg < -0.03 and px_chg < -0.01:
        return _v(28, "Liquidation Analysis", -0.6, 1.1, "long-liq cascade proxy")
    return _v(28, "Liquidation Analysis", 0.0, 0.6, "no cascade proxy")


def m29_basis(b: dict) -> Vote:
    prem = b.get("premium") or {}
    mark = prem.get("markPrice")
    idx = prem.get("indexPrice")
    if not mark or not idx:
        return _neutral(29, "Basis / Futures Premium", "no premium index")
    basis = (float(mark) - float(idx)) / float(idx)
    # rich premium → caution; discount → bullish
    return _v(29, "Basis / Futures Premium", ind.clamp(-basis * 80), 0.9, f"basis={basis*100:.3f}%")


def m30_perp(b: dict) -> Vote:
    # combine funding + basis + OI
    votes = [m25_oi(b), m26_funding(b), m29_basis(b)]
    avail = [v for v in votes if v.available]
    if not avail:
        return _neutral(30, "Perpetual Futures Analysis", "no perp data")
    score = sum(v.score * v.weight for v in avail) / sum(v.weight for v in avail)
    return _v(30, "Perpetual Futures Analysis", score, 1.2, "composite funding/OI/basis")


def m31_rsi(b: dict) -> Vote:
    r = ind.last_valid(ind.rsi(_closes(b["candles"])))
    if r is None:
        return _neutral(31, "RSI", "n/a")
    if r >= 70:
        score = -0.7
    elif r <= 30:
        score = 0.7
    else:
        score = (50 - r) / 50
    return _v(31, "RSI", score, 1.0, f"rsi={r:.1f}")


def m32_macd(b: dict) -> Vote:
    line, sig, hist = ind.macd(_closes(b["candles"]))
    h = ind.last_valid(hist)
    prev = None
    for x in reversed(hist[:-1]):
        if x is not None:
            prev = x
            break
    if h is None:
        return _neutral(32, "MACD", "n/a")
    score = ind.clamp(h / (abs(b["candles"][-1]["close"]) * 0.001 + 1e-12) * 0.15)
    if prev is not None and prev < 0 <= h:
        score = max(score, 0.65)
    if prev is not None and prev > 0 >= h:
        score = min(score, -0.65)
    return _v(32, "MACD", score, 1.0, f"hist={h:.6g}")


def m33_stochastic(b: dict) -> Vote:
    k, d = ind.stochastic(b["candles"])
    kv, dv = ind.last_valid(k), ind.last_valid(d)
    if kv is None or dv is None:
        return _neutral(33, "Stochastic", "n/a")
    score = 0.0
    if kv < 20 and kv > dv:
        score = 0.7
    elif kv > 80 and kv < dv:
        score = -0.7
    else:
        score = (50 - kv) / 80
    return _v(33, "Stochastic", score, 0.9, f"k={kv:.1f} d={dv:.1f}")


def m34_bollinger(b: dict) -> Vote:
    lo, mid, hi = ind.bollinger(_closes(b["candles"]))
    last = b["candles"][-1]["close"]
    l, m, h = ind.last_valid(lo), ind.last_valid(mid), ind.last_valid(hi)
    if None in (l, m, h):
        return _neutral(34, "Bollinger Bands", "n/a")
    assert l is not None and m is not None and h is not None
    width = (h - l) / m
    if last <= l:
        score = 0.65
    elif last >= h:
        score = -0.65
    else:
        score = (m - last) / (h - l) * 0.8
    return _v(34, "Bollinger Bands", score, 0.9, f"width={width:.3f}")


def m35_atr(b: dict) -> Vote:
    a = ind.last_valid(ind.atr(b["candles"]))
    last = b["candles"][-1]["close"]
    if a is None:
        return _neutral(35, "ATR", "n/a")
    # rising ATR with up-close = trend fuel; used as volatility regime filter
    atr_s = ind.atr(b["candles"])
    prev = atr_s[-15] if len(atr_s) > 15 else None
    expanding = prev is not None and a > prev
    up = b["candles"][-1]["close"] > b["candles"][-5]["close"]
    score = 0.35 if expanding and up else -0.35 if expanding and not up else 0.0
    return _v(35, "ATR", score, 0.7, f"atr_pct={a/last*100:.2f}% expand={expanding}")


def m36_adx(b: dict) -> Vote:
    a = ind.last_valid(ind.adx(b["candles"]))
    if a is None:
        return _neutral(36, "ADX", "n/a")
    slope = ind.linear_reg_slope(_closes(b["candles"][-20:]))
    trend = 1 if slope > 0 else -1
    if a < 20:
        return _v(36, "ADX", 0.0, 0.6, f"adx={a:.1f} choppy")
    return _v(36, "ADX", trend * min(a / 50, 1.0), 1.0, f"adx={a:.1f}")


def m37_ma(b: dict) -> Vote:
    closes = _closes(b["candles"])
    s20 = ind.last_valid(ind.sma(closes, 20))
    s50 = ind.last_valid(ind.sma(closes, 50))
    last = closes[-1]
    if s20 is None or s50 is None:
        return _neutral(37, "Moving Averages", "n/a")
    score = 0.4 if last > s20 else -0.4
    score += 0.4 if s20 > s50 else -0.4
    return _v(37, "Moving Averages", score, 1.0, f"px vs SMA20/50")


def m38_ema_sma(b: dict) -> Vote:
    closes = _closes(b["candles"])
    e9 = ind.last_valid(ind.ema(closes, 9))
    e21 = ind.last_valid(ind.ema(closes, 21))
    s50 = ind.last_valid(ind.sma(closes, 50))
    if None in (e9, e21, s50):
        return _neutral(38, "EMA / SMA", "n/a")
    score = (0.45 if e9 > e21 else -0.45) + (0.35 if e21 > s50 else -0.35)
    return _v(38, "EMA / SMA", score, 1.1, "EMA9/21 + SMA50 stack")


def m39_ichimoku(b: dict) -> Vote:
    c = b["candles"]
    if len(c) < 60:
        return _neutral(39, "Ichimoku", "short history")

    def mid_high_low(period: int, end: int) -> float:
        w = c[end - period + 1 : end + 1]
        return (max(x["high"] for x in w) + min(x["low"] for x in w)) / 2

    i = len(c) - 1
    tenkan = mid_high_low(9, i)
    kijun = mid_high_low(26, i)
    span_a = (tenkan + kijun) / 2
    span_b = mid_high_low(52, i)
    last = c[i]["close"]
    cloud_top = max(span_a, span_b)
    cloud_bot = min(span_a, span_b)
    score = 0.0
    if last > cloud_top:
        score += 0.5
    elif last < cloud_bot:
        score -= 0.5
    score += 0.35 if tenkan > kijun else -0.35
    return _v(39, "Ichimoku", score, 1.0, f"tenkan={tenkan:.6g} kijun={kijun:.6g}")


def m40_obv(b: dict) -> Vote:
    series = ind.obv(b["candles"])
    slope = ind.linear_reg_slope(series[-30:])
    norm = slope / (abs(series[-1]) + 1e-9)
    return _v(40, "OBV", ind.clamp(norm * 200), 0.9, f"obv_slope_norm={norm:.4f}")


def m41_mfi(b: dict) -> Vote:
    v = ind.last_valid(ind.mfi(b["candles"]))
    if v is None:
        return _neutral(41, "MFI", "n/a")
    if v >= 80:
        score = -0.65
    elif v <= 20:
        score = 0.65
    else:
        score = (50 - v) / 60
    return _v(41, "MFI", score, 0.9, f"mfi={v:.1f}")


def m42_stoch_rsi(b: dict) -> Vote:
    r = ind.rsi(_closes(b["candles"]))
    vals = [x for x in r if x is not None]
    if len(vals) < 14:
        return _neutral(42, "Stochastic RSI", "n/a")
    window = vals[-14:]
    lo, hi = min(window), max(window)
    stoch = (window[-1] - lo) / (hi - lo or 1e-12)
    if stoch < 0.2:
        score = 0.7
    elif stoch > 0.8:
        score = -0.7
    else:
        score = (0.5 - stoch) * 1.2
    return _v(42, "Stochastic RSI", score, 0.9, f"stoch_rsi={stoch:.2f}")


def m43_divergence(b: dict) -> Vote:
    closes = _closes(b["candles"][-40:])
    rsis = [x for x in ind.rsi(_closes(b["candles"]))[-40:] if x is not None]
    if len(rsis) < 10:
        return _neutral(43, "Divergence Analysis", "n/a")
    price_hl = closes[-1] < min(closes[:-5]) * 1.01 and closes[-1] > closes[-2]
    rsi_hl = rsis[-1] > min(rsis[:-5])
    price_lh = closes[-1] > max(closes[:-5]) * 0.99
    rsi_lh = rsis[-1] < max(rsis[:-5])
    if price_hl and rsi_hl:
        return _v(43, "Divergence Analysis", 0.75, 1.2, "bullish RSI divergence")
    if price_lh and rsi_lh:
        return _v(43, "Divergence Analysis", -0.75, 1.2, "bearish RSI divergence")
    return _v(43, "Divergence Analysis", 0.0, 0.5, "no divergence")


def m44_momentum(b: dict) -> Vote:
    closes = _closes(b["candles"])
    mom = (closes[-1] - closes[-10]) / closes[-10]
    return _v(44, "Momentum Trading", ind.clamp(mom * 15), 1.1, f"mom10={mom*100:.2f}%")


def m45_breakout(b: dict) -> Vote:
    c = b["candles"]
    hi = max(x["high"] for x in c[-40:-1])
    last = c[-1]
    vol_avg = sum(x["volume"] for x in c[-21:-1]) / 20
    if last["close"] > hi and last["volume"] > vol_avg * 1.3:
        return _v(45, "Breakout Trading", 0.85, 1.3, "volume breakout above range high")
    if last["close"] > hi:
        return _v(45, "Breakout Trading", 0.4, 1.0, "breakout weak volume")
    return _v(45, "Breakout Trading", -0.1, 0.6, "no breakout")


def m46_breakdown(b: dict) -> Vote:
    c = b["candles"]
    lo = min(x["low"] for x in c[-40:-1])
    last = c[-1]
    vol_avg = sum(x["volume"] for x in c[-21:-1]) / 20
    if last["close"] < lo and last["volume"] > vol_avg * 1.3:
        return _v(46, "Breakdown Trading", -0.85, 1.3, "volume breakdown")
    if last["close"] < lo:
        return _v(46, "Breakdown Trading", -0.4, 1.0, "breakdown weak volume")
    return _v(46, "Breakdown Trading", 0.1, 0.6, "no breakdown")


def m47_mean_reversion(b: dict) -> Vote:
    closes = _closes(b["candles"])
    s = ind.last_valid(ind.sma(closes, 20))
    if s is None:
        return _neutral(47, "Mean Reversion", "n/a")
    z = (closes[-1] - s) / s
    return _v(47, "Mean Reversion", ind.clamp(-z * 12), 0.9, f"vs_sma20={z*100:.2f}%")


def m48_scalping(b: dict) -> Vote:
    # micro momentum + book imbalance
    of = m18_order_flow(b)
    pa = m01_price_action(b)
    score = of.score * 0.6 + pa.score * 0.4
    return _v(48, "Scalping", score, 0.8, "micro flow + PA")


def m49_day_trading(b: dict) -> Vote:
    t = b.get("ticker") or {}
    chg = float(t.get("priceChangePercent") or 0)
    vwap = m15_vwap(b)
    score = ind.clamp(chg / 8) * 0.5 + vwap.score * 0.5
    return _v(49, "Day Trading", score, 0.9, f"24h={chg:.2f}% + VWAP")


def m50_swing(b: dict) -> Vote:
    htf = b.get("htf_candles") or b["candles"]
    slope = ind.linear_reg_slope(_closes(htf[-48:]))
    norm = slope / (htf[-1]["close"] or 1) * 20
    return _v(50, "Swing Trading", ind.clamp(norm * 10), 1.0, f"htf_slope={norm:.4f}")


def m51_position(b: dict) -> Vote:
    htf = b.get("htf_candles") or b["candles"]
    closes = _closes(htf)
    e50 = ind.last_valid(ind.ema(closes, 50))
    e200 = ind.last_valid(ind.ema(closes, 200)) if len(closes) >= 200 else ind.last_valid(ind.ema(closes, 100))
    if e50 is None or e200 is None:
        return _neutral(51, "Position Trading", "n/a")
    score = 0.6 if e50 > e200 and closes[-1] > e50 else -0.6 if e50 < e200 else 0.0
    return _v(51, "Position Trading", score, 0.8, "HTF EMA stack")


def m52_grid(b: dict) -> Vote:
    a = ind.last_valid(ind.atr(b["candles"]))
    last = b["candles"][-1]["close"]
    if a is None:
        return _neutral(52, "Grid Trading", "n/a")
    # grids prefer range; ADX low
    adx_v = ind.last_valid(ind.adx(b["candles"]))
    ranging = adx_v is not None and adx_v < 22
    # for directional confluence, ranging → neutral-to-slight mean revert
    score = 0.15 if ranging else -0.1
    return _v(52, "Grid Trading", score, 0.4, f"range_mode={ranging} atr%={a/last*100:.2f}")


def m53_dca(b: dict) -> Vote:
    # DCA bias: buy weakness in higher-timeframe uptrend
    pos = m51_position(b)
    mr = m47_mean_reversion(b)
    score = 0.0
    if pos.score > 0.2 and mr.score > 0.2:
        score = 0.55
    elif pos.score < -0.2:
        score = -0.4
    return _v(53, "DCA", score, 0.5, "HTF up + dip")


def m54_arbitrage(b: dict) -> Vote:
    return _neutral(54, "Arbitrage", "needs multi-exchange quotes", 0.2)


def m55_stat_arb(b: dict) -> Vote:
    btc = b.get("btc_candles")
    if not btc:
        return _neutral(55, "Statistical Arbitrage", "needs BTC pair series")
    # residual mean reversion vs BTC
    a = _closes(b["candles"][-80:])
    bb = _closes(btc[-80:])
    n = min(len(a), len(bb))
    a, bb = a[-n:], bb[-n:]
    # hedge ratio = std proxy
    ratio = [a[i] / bb[i] for i in range(n)]
    mean = sum(ratio) / n
    std = (sum((x - mean) ** 2 for x in ratio) / n) ** 0.5 or 1e-12
    z = (ratio[-1] - mean) / std
    return _v(55, "Statistical Arbitrage", ind.clamp(-z / 2), 0.7, f"z_vs_btc={z:.2f}")


def m56_pairs(b: dict) -> Vote:
    arb = m55_stat_arb(b)
    return _v(56, "Pairs Trading", arb.score, 0.6, "BTC pair z-score", arb.available)


def m57_mm(b: dict) -> Vote:
    return _neutral(57, "Market Making", "inventory strategy — not directional", 0.1)


def m58_algo(b: dict) -> Vote:
    # meta: average of core directional engines
    core = [m01_price_action(b), m02_market_structure(b), m13_volume(b), m31_rsi(b), m45_breakout(b)]
    score = sum(v.score * v.weight for v in core) / sum(v.weight for v in core)
    return _v(58, "Algorithmic Trading", score, 0.8, "core-engine ensemble")


def m59_quant(b: dict) -> Vote:
    # z-score momentum + vol normalize
    closes = _closes(b["candles"][-60:])
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    std = (sum((x - mean) ** 2 for x in rets) / len(rets)) ** 0.5 or 1e-12
    z = rets[-1] / std
    return _v(59, "Quantitative Trading", ind.clamp(z / 2), 0.9, f"ret_z={z:.2f}")


def m60_hft(b: dict) -> Vote:
    return _neutral(60, "High-Frequency Trading", "needs colocated tick feed", 0.1)


def m61_smc(b: dict) -> Vote:
    # BOS + liquidity sweep combo
    ms = m02_market_structure(b)
    sweep = m63_liquidity_sweep(b)
    score = ms.score * 0.55 + sweep.score * 0.45
    return _v(61, "Smart Money Concept (SMC)", score, 1.4, "structure + sweep")


def m62_ict(b: dict) -> Vote:
    fvg = m64_fvg(b)
    ob = m65_order_block(b)
    score = fvg.score * 0.5 + ob.score * 0.5
    return _v(62, "ICT Methodology", score, 1.3, "FVG + order block")


def m63_liquidity_sweep(b: dict) -> Vote:
    c = b["candles"]
    prior_low = min(x["low"] for x in c[-30:-2])
    prior_high = max(x["high"] for x in c[-30:-2])
    last = c[-1]
    # sweep low then close back above
    if last["low"] < prior_low and last["close"] > prior_low:
        return _v(63, "Liquidity Sweep", 0.8, 1.4, "sell-side liquidity swept")
    if last["high"] > prior_high and last["close"] < prior_high:
        return _v(63, "Liquidity Sweep", -0.8, 1.4, "buy-side liquidity swept")
    return _v(63, "Liquidity Sweep", 0.0, 0.6, "no sweep")


def m64_fvg(b: dict) -> Vote:
    c = b["candles"]
    # 3-candle fair value gap
    a, mid, d = c[-3], c[-2], c[-1]
    bull_fvg = a["high"] < d["low"]
    bear_fvg = a["low"] > d["high"]
    last = d["close"]
    if bull_fvg:
        gap_mid = (a["high"] + d["low"]) / 2
        score = 0.55 if last >= gap_mid else 0.25
        return _v(64, "Fair Value Gap (FVG)", score, 1.2, "bullish FVG")
    if bear_fvg:
        gap_mid = (a["low"] + d["high"]) / 2
        score = -0.55 if last <= gap_mid else -0.25
        return _v(64, "Fair Value Gap (FVG)", score, 1.2, "bearish FVG")
    return _v(64, "Fair Value Gap (FVG)", 0.0, 0.5, "no FVG")


def m65_order_block(b: dict) -> Vote:
    c = b["candles"]
    # last opposite candle before impulsive move
    impulse = c[-1]["close"] - c[-4]["close"]
    if impulse > 0:
        # find last bearish candle in prior 8
        for x in reversed(c[-12:-1]):
            if x["close"] < x["open"]:
                if c[-1]["close"] > x["high"]:
                    return _v(65, "Order Block", 0.7, 1.3, "bullish OB broken/held")
                if x["low"] <= c[-1]["low"] <= x["high"]:
                    return _v(65, "Order Block", 0.5, 1.1, "price in bullish OB")
                break
    elif impulse < 0:
        for x in reversed(c[-12:-1]):
            if x["close"] > x["open"]:
                if c[-1]["close"] < x["low"]:
                    return _v(65, "Order Block", -0.7, 1.3, "bearish OB broken/held")
                break
    return _v(65, "Order Block", 0.0, 0.5, "no clear OB")


def m66_breaker(b: dict) -> Vote:
    # failed order block → breaker
    ob = m65_order_block(b)
    ms = m02_market_structure(b)
    if ob.score > 0.4 and ms.score < -0.3:
        return _v(66, "Breaker Block", -0.6, 1.0, "bullish OB failed → breaker short")
    if ob.score < -0.4 and ms.score > 0.3:
        return _v(66, "Breaker Block", 0.6, 1.0, "bearish OB failed → breaker long")
    return _v(66, "Breaker Block", 0.0, 0.5, "no breaker")


def m67_imbalance(b: dict) -> Vote:
    fvg = m64_fvg(b)
    return _v(67, "Imbalance", fvg.score, 1.0, fvg.detail, fvg.available)


def m68_sm_flow(b: dict) -> Vote:
    of = m18_order_flow(b)
    oi = m25_oi(b)
    score = of.score * 0.6 + oi.score * 0.4
    return _v(68, "Smart Money Flow", score, 1.1, "flow + OI proxy")


def m69_whale_tracking(b: dict) -> Vote:
    trades = b.get("agg_trades") or []
    if not trades:
        return _neutral(69, "Whale Tracking", "no agg trades")
    sizes = [float(t["q"]) for t in trades]
    avg = sum(sizes) / len(sizes)
    whales = [t for t in trades if float(t["q"]) > avg * 5]
    if not whales:
        return _v(69, "Whale Tracking", 0.0, 0.5, "no whale prints")
    buy = sum(float(t["q"]) for t in whales if not t.get("m"))
    sell = sum(float(t["q"]) for t in whales if t.get("m"))
    imb = (buy - sell) / (buy + sell or 1)
    return _v(69, "Whale Tracking", ind.clamp(imb * 1.5), 1.0, f"whale_imb={imb:.2f} n={len(whales)}")


def m70_sm_wallet(b: dict) -> Vote:
    return _neutral(70, "Smart Money Wallet Tracking", "needs labeled wallet API", 0.2)


def m71_wallet_cluster(b: dict) -> Vote:
    return _neutral(71, "Wallet Clustering", "needs on-chain graph", 0.2)


def m72_onchain(b: dict) -> Vote:
    # soft proxy from exchange-like aggressive flow
    whale = m69_whale_tracking(b)
    return _v(72, "On-Chain Analysis", whale.score * 0.5, 0.5, "trade-size proxy only", whale.available)


def m73_ex_inflow(b: dict) -> Vote:
    return _neutral(73, "Exchange Inflow", "needs on-chain exchange labels", 0.2)


def m74_ex_outflow(b: dict) -> Vote:
    return _neutral(74, "Exchange Outflow", "needs on-chain exchange labels", 0.2)


def m75_ex_wallet(b: dict) -> Vote:
    return _neutral(75, "Exchange Wallet Tracking", "see telegram_cex_alert.py", 0.2)


def m76_whale_tx(b: dict) -> Vote:
    whale = m69_whale_tracking(b)
    return _v(76, "Whale Transaction Analysis", whale.score, 0.8, "CEX whale prints proxy", whale.available)


def m77_token_holder(b: dict) -> Vote:
    return _neutral(77, "Token Holder Analysis", "needs explorer API", 0.2)


def m78_token_dist(b: dict) -> Vote:
    return _neutral(78, "Token Distribution Analysis", "needs holder snapshot", 0.2)


def m79_lp(b: dict) -> Vote:
    return _neutral(79, "Liquidity Pool Analysis", "needs DEX pool API", 0.2)


def m80_dex_flow(b: dict) -> Vote:
    return _neutral(80, "DEX Flow Analysis", "needs DEX subgraph", 0.2)


def m81_unlock(b: dict) -> Vote:
    return _neutral(81, "Token Unlock Analysis", "needs vesting calendar", 0.2)


def m82_vesting(b: dict) -> Vote:
    return _neutral(82, "Token Vesting Analysis", "needs vesting calendar", 0.2)


def m83_supply(b: dict) -> Vote:
    return _neutral(83, "Token Supply Analysis", "needs tokenomics API", 0.2)


def m84_burn_mint(b: dict) -> Vote:
    return _neutral(84, "Token Burn / Mint Analysis", "needs token events", 0.2)


def m85_staking(b: dict) -> Vote:
    return _neutral(85, "Staking Flow Analysis", "needs staking contract feed", 0.2)


def m86_bridge(b: dict) -> Vote:
    return _neutral(86, "Bridge Flow Analysis", "needs bridge indexer", 0.2)


def m87_stablecoin(b: dict) -> Vote:
    # proxy: USDT pair quote volume expansion from ticker
    t = b.get("ticker") or {}
    qv = float(t.get("quoteVolume") or 0)
    # can't get global stable flow; use local activity intensity vs median assumption
    return _v(87, "Stablecoin Flow Analysis", 0.0, 0.3, f"local_quote_vol={qv:.0f} (proxy weak)", ok=False)


def m88_gas(b: dict) -> Vote:
    return _neutral(88, "Gas / Network Activity Analysis", "needs RPC gas oracle", 0.2)


def m89_mempool(b: dict) -> Vote:
    return _neutral(89, "Mempool Analysis", "needs mempool stream", 0.1)


def m90_mev(b: dict) -> Vote:
    return _neutral(90, "MEV Analysis", "needs MEV relay data", 0.1)


def m91_sentiment(b: dict) -> Vote:
    # funding + LS as crowding sentiment proxy
    f = m26_funding(b)
    ls = m27_long_short(b)
    # contrarian
    score = -(f.score * 0.4 + (-ls.score) * 0.3) + m44_momentum(b).score * 0.3
    return _v(91, "Sentiment Analysis", ind.clamp(score), 0.8, "crowding proxies")


def m92_social(b: dict) -> Vote:
    return _neutral(92, "Social Media Analysis", "needs social API keys", 0.2)


def m93_gtrends(b: dict) -> Vote:
    return _neutral(93, "Google Trends Analysis", "needs trends API", 0.2)


def m94_news(b: dict) -> Vote:
    return _neutral(94, "News Trading", "needs news feed", 0.2)


def m95_event(b: dict) -> Vote:
    return _neutral(95, "Event Trading", "needs event calendar", 0.2)


def m96_narrative(b: dict) -> Vote:
    t = b.get("ticker") or {}
    chg = float(t.get("priceChangePercent") or 0)
    # relative strength narrative heat
    return _v(96, "Narrative Analysis", ind.clamp(chg / 15), 0.5, f"24h heat={chg:.1f}%")


def m97_fear_greed(b: dict) -> Vote:
    # proxy from RSI + funding extremes
    r = ind.last_valid(ind.rsi(_closes(b["candles"]))) or 50
    fr = 0.0
    prem = b.get("premium") or {}
    if prem.get("lastFundingRate") is not None:
        fr = float(prem["lastFundingRate"])
    greed = (r - 50) / 50 + fr * 500
    # contrarian
    return _v(97, "Fear & Greed Analysis", ind.clamp(-greed), 0.7, f"greed_proxy={greed:.2f}")


def m98_onchain_val(b: dict) -> Vote:
    return _neutral(98, "On-chain Valuation", "needs realized cap / NVT feeds", 0.2)


def m99_nvt(b: dict) -> Vote:
    return _neutral(99, "NVT Analysis", "needs on-chain volume", 0.2)


def m100_mvrv(b: dict) -> Vote:
    return _neutral(100, "MVRV Analysis", "needs realized price", 0.2)


def m101_sopr(b: dict) -> Vote:
    return _neutral(101, "SOPR Analysis", "needs spent output data", 0.2)


def m102_realized(b: dict) -> Vote:
    return _neutral(102, "Realized Cap Analysis", "needs realized cap", 0.2)


def m103_ex_reserve(b: dict) -> Vote:
    return _neutral(103, "Exchange Reserve Analysis", "needs exchange reserve feed", 0.2)


def m104_miner(b: dict) -> Vote:
    return _neutral(104, "Miner Flow Analysis", "BTC miner API only", 0.2)


def m105_etf(b: dict) -> Vote:
    return _neutral(105, "ETF Flow Analysis", "needs ETF flow feed", 0.2)


def m106_macro(b: dict) -> Vote:
    # weak proxy: BTC strength as risk-on
    btc = b.get("btc_candles") or (b["candles"] if b["symbol"] == "BTCUSDT" else None)
    if not btc:
        return _neutral(106, "Macro Analysis", "no BTC context")
    chg = (btc[-1]["close"] - btc[-20]["close"]) / btc[-20]["close"]
    return _v(106, "Macro Analysis", ind.clamp(chg * 8), 0.7, f"btc_proxy={chg*100:.2f}%")


def m107_dxy(b: dict) -> Vote:
    return _neutral(107, "DXY Analysis", "needs DXY feed", 0.2)


def m108_rates(b: dict) -> Vote:
    return _neutral(108, "Interest Rate Analysis", "needs rates feed", 0.2)


def m109_money_supply(b: dict) -> Vote:
    return _neutral(109, "Liquidity / Money Supply Analysis", "needs macro liquidity feed", 0.2)


def m110_corr(b: dict) -> Vote:
    btc = b.get("btc_candles")
    if not btc:
        return _v(110, "Correlation Analysis", 0.0, 0.4, "self/BTC n/a")
    a = _closes(b["candles"][-80:])
    bb = _closes(btc[-80:])
    n = min(len(a), len(bb))
    a, bb = a[-n:], bb[-n:]
    ma, mb = sum(a) / n, sum(bb) / n
    num = sum((a[i] - ma) * (bb[i] - mb) for i in range(n))
    den = (sum((a[i] - ma) ** 2 for i in range(n)) * sum((bb[i] - mb) ** 2 for i in range(n))) ** 0.5 or 1
    corr = num / den
    # high corr → follow BTC bias
    btc_score = m05_trend_analysis({"candles": btc}).score
    return _v(110, "Correlation Analysis", ind.clamp(corr * btc_score), 0.8, f"corr_btc={corr:.2f}")


def m111_btc_dom(b: dict) -> Vote:
    dom = b.get("btc_dominance") or {}
    share = dom.get("btc_vol_share")
    if share is None:
        return _neutral(111, "BTC Dominance Analysis", "no dominance proxy")
    # high BTC vol share → alts weak
    if b["symbol"] == "BTCUSDT":
        score = (share - 0.25) * 2
    else:
        score = (0.25 - share) * 2
    return _v(111, "BTC Dominance Analysis", ind.clamp(score), 0.7, f"btc_vol_share={share:.3f}")


def m112_alt_season(b: dict) -> Vote:
    dom = m111_btc_dom(b)
    # inverse of dominance for alts
    if b["symbol"] == "BTCUSDT":
        return _v(112, "Altcoin Season Analysis", -dom.score * 0.5, 0.6, "BTC focus")
    return _v(112, "Altcoin Season Analysis", -dom.score, 0.7, "alt season proxy")


def m113_cross_ex(b: dict) -> Vote:
    return _neutral(113, "Cross-Exchange Analysis", "single-venue data only", 0.2)


def m114_rel_strength(b: dict) -> Vote:
    btc = b.get("btc_candles")
    t = b.get("ticker") or {}
    chg = float(t.get("priceChangePercent") or 0)
    if not btc:
        return _v(114, "Relative Strength Analysis", ind.clamp(chg / 10), 0.8, f"24h={chg:.1f}%")
    btc_chg = (btc[-1]["close"] - btc[-96]["close"]) / btc[-96]["close"] * 100 if len(btc) > 96 else 0
    rs = chg - btc_chg
    return _v(114, "Relative Strength Analysis", ind.clamp(rs / 8), 1.0, f"rs_vs_btc={rs:.2f}%")


def m115_rr(b: dict) -> Vote:
    c = b["candles"]
    last = c[-1]["close"]
    a = ind.last_valid(ind.atr(c)) or last * 0.01
    stop = last - a
    target = last + 2 * a
    rr = (target - last) / (last - stop or 1e-12)
    score = 0.4 if rr >= 2 else -0.2
    return _v(115, "Risk/Reward Analysis", score, 0.8, f"rr≈{rr:.2f} (ATR-based)")


def m116_position_sizing(b: dict) -> Vote:
    # not directional — quality of setup for sizing
    conf = abs(m02_market_structure(b).score) + abs(m13_volume(b).score)
    return _v(116, "Position Sizing", ind.clamp(conf / 2 - 0.3), 0.4, "setup clarity for size")


def m117_stop_loss(b: dict) -> Vote:
    return _v(117, "Stop-Loss Management", 0.0, 0.2, "managed by trader (-1% hard stop)", ok=True)


def m118_take_profit(b: dict) -> Vote:
    return _v(118, "Take-Profit Management", 0.0, 0.2, "managed by trailing peak exit", ok=True)


def m119_portfolio_risk(b: dict) -> Vote:
    return _v(119, "Portfolio Risk Management", 0.0, 0.2, "single-symbol paper mode", ok=True)


def m120_kelly(b: dict) -> Vote:
    # rough edge from recent win-rate proxy of direction consistency
    closes = _closes(b["candles"][-50:])
    ups = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    p = ups / (len(closes) - 1)
    # Kelly f = 2p-1 for even money
    f = 2 * p - 1
    return _v(120, "Kelly Criterion", ind.clamp(f), 0.5, f"edge_proxy={f:.2f}")


def m121_backtest(b: dict) -> Vote:
    # quick walk-forward of EMA cross last 80 bars
    closes = _closes(b["candles"][-120:])
    e_fast = ind.ema(closes, 9)
    e_slow = ind.ema(closes, 21)
    pnl = 0.0
    pos = 0
    entry = 0.0
    trades = 0
    wins = 0
    for i in range(25, len(closes)):
        if e_fast[i] is None or e_slow[i] is None or e_fast[i - 1] is None or e_slow[i - 1] is None:
            continue
        if e_fast[i - 1] <= e_slow[i - 1] and e_fast[i] > e_slow[i] and pos == 0:
            pos = 1
            entry = closes[i]
        elif e_fast[i - 1] >= e_slow[i - 1] and e_fast[i] < e_slow[i] and pos == 1:
            ret = (closes[i] - entry) / entry
            pnl += ret
            trades += 1
            if ret > 0:
                wins += 1
            pos = 0
    if trades < 3:
        return _v(121, "Backtesting", 0.0, 0.5, f"few trades={trades}")
    score = ind.clamp(pnl * 5)
    return _v(121, "Backtesting", score, 0.8, f"ema_cross pnl={pnl*100:.1f}% wr={wins/trades:.0%} n={trades}")


def m122_forward(b: dict) -> Vote:
    return _v(122, "Forward Testing", 0.0, 0.2, "use --loop paper trader", ok=True)


def m123_monte_carlo(b: dict) -> Vote:
    closes = _closes(b["candles"][-80:])
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    # simple: probability of positive path = share of positive returns
    p_up = sum(1 for r in rets if r > 0) / len(rets)
    return _v(123, "Monte Carlo Analysis", ind.clamp((p_up - 0.5) * 3 + mean * 50), 0.6, f"p_up={p_up:.2f}")


def m124_stat_prob(b: dict) -> Vote:
    mc = m123_monte_carlo(b)
    return _v(124, "Statistical Probability Analysis", mc.score, 0.6, mc.detail, mc.available)


def m125_ml(b: dict) -> Vote:
    # lightweight feature ensemble standing in for ML score
    feats = [
        m05_trend_analysis(b).score,
        m31_rsi(b).score,
        m19_cvd(b).score,
        m25_oi(b).score,
        m45_breakout(b).score,
    ]
    score = sum(feats) / len(feats)
    return _v(125, "Machine Learning / AI Trading", score, 0.9, "feature ensemble (no trained model)")


def m126_signal_scoring(b: dict) -> Vote:
    algo = m58_algo(b)
    return _v(126, "Signal Scoring Systems", algo.score, 0.7, "meta score", algo.available)


def m127_mtf(b: dict) -> Vote:
    ltf = m05_trend_analysis(b)
    htf_bundle = {"candles": b.get("htf_candles") or b["candles"], "symbol": b["symbol"]}
    htf = m05_trend_analysis(htf_bundle)
    if ltf.score > 0 and htf.score > 0:
        score = 0.75
    elif ltf.score < 0 and htf.score < 0:
        score = -0.75
    else:
        score = (ltf.score + htf.score * 1.5) / 2.5
    return _v(127, "Multi-Timeframe Analysis", ind.clamp(score), 1.3, f"ltf={ltf.score:.2f} htf={htf.score:.2f}")


def m128_confluence(b: dict) -> Vote:
    # placeholder — filled after all votes in run_all
    return _v(128, "Confluence Analysis", 0.0, 0.1, "aggregate computed separately")


METHODS: list[MethodFn] = [
    m01_price_action,
    m02_market_structure,
    m03_wyckoff,
    m04_support_resistance,
    m05_trend_analysis,
    m06_trendline,
    m07_chart_patterns,
    m08_candlestick,
    m09_fib_retracement,
    m10_fib_extension,
    m11_elliott,
    m12_dow,
    m13_volume,
    m14_volume_profile,
    m15_vwap,
    m16_anchored_vwap,
    m17_footprint,
    m18_order_flow,
    m19_cvd,
    m20_delta,
    m21_bid_ask_imbalance,
    m22_order_book,
    m23_liquidity,
    m24_heatmap,
    m25_oi,
    m26_funding,
    m27_long_short,
    m28_liquidation,
    m29_basis,
    m30_perp,
    m31_rsi,
    m32_macd,
    m33_stochastic,
    m34_bollinger,
    m35_atr,
    m36_adx,
    m37_ma,
    m38_ema_sma,
    m39_ichimoku,
    m40_obv,
    m41_mfi,
    m42_stoch_rsi,
    m43_divergence,
    m44_momentum,
    m45_breakout,
    m46_breakdown,
    m47_mean_reversion,
    m48_scalping,
    m49_day_trading,
    m50_swing,
    m51_position,
    m52_grid,
    m53_dca,
    m54_arbitrage,
    m55_stat_arb,
    m56_pairs,
    m57_mm,
    m58_algo,
    m59_quant,
    m60_hft,
    m61_smc,
    m62_ict,
    m63_liquidity_sweep,
    m64_fvg,
    m65_order_block,
    m66_breaker,
    m67_imbalance,
    m68_sm_flow,
    m69_whale_tracking,
    m70_sm_wallet,
    m71_wallet_cluster,
    m72_onchain,
    m73_ex_inflow,
    m74_ex_outflow,
    m75_ex_wallet,
    m76_whale_tx,
    m77_token_holder,
    m78_token_dist,
    m79_lp,
    m80_dex_flow,
    m81_unlock,
    m82_vesting,
    m83_supply,
    m84_burn_mint,
    m85_staking,
    m86_bridge,
    m87_stablecoin,
    m88_gas,
    m89_mempool,
    m90_mev,
    m91_sentiment,
    m92_social,
    m93_gtrends,
    m94_news,
    m95_event,
    m96_narrative,
    m97_fear_greed,
    m98_onchain_val,
    m99_nvt,
    m100_mvrv,
    m101_sopr,
    m102_realized,
    m103_ex_reserve,
    m104_miner,
    m105_etf,
    m106_macro,
    m107_dxy,
    m108_rates,
    m109_money_supply,
    m110_corr,
    m111_btc_dom,
    m112_alt_season,
    m113_cross_ex,
    m114_rel_strength,
    m115_rr,
    m116_position_sizing,
    m117_stop_loss,
    m118_take_profit,
    m119_portfolio_risk,
    m120_kelly,
    m121_backtest,
    m122_forward,
    m123_monte_carlo,
    m124_stat_prob,
    m125_ml,
    m126_signal_scoring,
    m127_mtf,
    m128_confluence,
]


# Strong-stack weights boost (user's preferred confluence stack)
STRONG_STACK = {
    "Price Action",
    "Wyckoff",
    "Market Structure",
    "Volume Analysis",
    "Footprint Chart",
    "CVD",
    "Delta Analysis",
    "Open Interest (OI)",
    "Liquidation Analysis",
    "Liquidity Analysis",
    "On-Chain Analysis",
    "Risk/Reward Analysis",
    "Stop-Loss Management",
    "Take-Profit Management",
    "Smart Money Concept (SMC)",
    "Order Flow",
}


def run_all_methods(bundle: dict[str, Any]) -> list[Vote]:
    votes: list[Vote] = []
    for fn in METHODS:
        try:
            vote = fn(bundle)
        except Exception as e:  # noqa: BLE001
            vote = _neutral(0, getattr(fn, "__name__", "method"), f"error: {e}", 0.1)
        if vote.name in STRONG_STACK and vote.available:
            vote = Vote(vote.id, vote.name, vote.score, vote.weight * 1.35, vote.detail, vote.available)
        votes.append(vote)

    # fill confluence (#128) from available directional votes
    usable = [v for v in votes if v.id != 128 and v.available and v.weight > 0.25]
    if usable:
        score = sum(v.score * v.weight for v in usable) / sum(v.weight for v in usable)
        votes = [
            v
            if v.id != 128
            else Vote(128, "Confluence Analysis", ind.clamp(score), 1.5, f"n={len(usable)} methods", True)
            for v in votes
        ]
    return votes
