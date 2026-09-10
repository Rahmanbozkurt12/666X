#!/usr/bin/env python3
"""
Binance USDT spot 15 dakikalık TA / dip dönüş radarı.

Mumdan hesaplanan yöntemler:
  Fibonacci retracement + extension
  Destek/direnç, pivot, yuvarlak sayı, HH/HL trend
  SMA/EMA 20-50-100-200, golden/death cross, EMA ribbon
  RSI, Stochastic, MACD, CCI, Williams %R, MFI
  Bollinger, Keltner, ATR, Donchian
  ADX/DMI, Parabolic SAR, SuperTrend, Ichimoku
  Volume, OBV, VWAP, CVD/delta
  Mum formasyonları + basit double bottom

Elle çizilen / ayrı veri isteyenler yok:
  Fib fan/arc/time zone, Elliott, harmonic, Wyckoff,
  likidasyon haritası, order book heatmap.

Kullanım:
  python hourly_dip_radar.py
  python hourly_dip_radar.py --once
  python hourly_dip_radar.py --all-symbols --min-score 5
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

try:
    import websocket
except ImportError:
    websocket = None  # type: ignore[assignment]


BINANCE_REST = "https://data-api.binance.vision"
BINANCE_WS = "wss://data-stream.binance.vision/stream"
FAPI_REST = "https://fapi.binance.com"
KLINE_INTERVAL = "15m"
KLINE_LIMIT = 260
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDP", "EUR", "AEUR", "USD1"}
LEVERAGE_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
HTTP_TIMEOUT = 20
MAX_STREAMS_PER_SOCKET = 80
FIB_RETRACE = (0.236, 0.382, 0.5, 0.618, 0.786)
FIB_EXT = (1.272, 1.618)
BULLISH_TAGS = {
    "FIB_RETRACE",
    "DESTEK",
    "PIVOT",
    "TREND_HL",
    "EMA_RIBBON",
    "EMA_STACK",
    "MA20_USTU",
    "EMA50_USTU",
    "EMA200_USTU",
    "GOLDEN_CROSS",
    "RSI_TOPARLAMA",
    "STOCH_OS",
    "MACD_CROSS",
    "MACD_POZITIF",
    "CCI_OS",
    "WILLIAMS_OS",
    "MFI_OS",
    "BB_ALT",
    "KELTNER_ALT",
    "DONCHIAN_ALT",
    "ADX_ALIS",
    "SUPERTREND_ALIS",
    "PSAR_CEVRIM",
    "ICHIMOKU_ALIS",
    "HACIM_PATLAMASI",
    "OBV_YUKSELIS",
    "VWAP_USTU",
    "POZITIF_DELTA",
    "DIP_TOPLAMA",
    "IGNE_REDDI",
    "HAMMER",
    "YUTAN_BOGA",
    "CIFT_DIP",
    "FUNDING_NEGATIF",
}


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "hourly-dip-radar/1.1"})


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    taker_buy_vol: float
    closed: bool = True

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def delta(self) -> float:
        sell = self.volume - self.taker_buy_vol
        return self.taker_buy_vol - sell

    @property
    def buy_ratio(self) -> float:
        if self.volume <= 0:
            return 0.0
        return min(self.taker_buy_vol / self.volume, 1.0)

    @property
    def rejection(self) -> float:
        if self.range <= 0:
            return 0.0
        return self.lower_wick / self.range

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0


def parse_kline_rest(row: list[Any]) -> Candle:
    close_time = int(row[6])
    now_ms = int(time.time() * 1000)
    return Candle(
        open_time=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        close_time=close_time,
        taker_buy_vol=float(row[10]),
        closed=close_time <= now_ms,
    )


def parse_kline_ws(k: dict[str, Any]) -> Candle:
    return Candle(
        open_time=int(k["t"]),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k["v"]),
        close_time=int(k["T"]),
        taker_buy_vol=float(k["V"]),
        closed=bool(k.get("x")),
    )


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema_last(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
    return e


def ema_series(values: list[float], period: int) -> list[float] | None:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out = [e]
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
        out.append(e)
    return out


def stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((x - mean) ** 2 for x in values) / n)


def rsi_wilder(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        prev_c = candles[i - 1].close
        c = candles[i]
        trs.append(max(c.high - c.low, abs(c.high - prev_c), abs(c.low - prev_c)))
    return sma(trs, period)


def near(price: float, level: float, tol: float) -> bool:
    if level <= 0 or price <= 0:
        return False
    return abs(price - level) / price <= tol


def last_local_swings(candles: list[Candle], lookback: int = 60) -> tuple[float, float]:
    window = candles[-lookback:] if len(candles) >= lookback else candles
    return max(c.high for c in window), min(c.low for c in window)


def detect_methods(candles: list[Candle]) -> list[str]:
    if len(candles) < 30:
        return []
    cur = candles[-1]
    prev = candles[-2]
    methods: list[str] = []
    if cur.range <= 0 or cur.volume <= 0:
        return methods

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    vols = [c.volume for c in candles]
    price = cur.close
    atr14 = atr(candles, 14) or (price * 0.01)
    tol = max(0.0025, (atr14 / price) * 0.35)

    # --- Fibonacci retracement / extension ---
    swing_hi, swing_lo = last_local_swings(candles, 80)
    rng = swing_hi - swing_lo
    if rng > 0:
        for ratio in FIB_RETRACE:
            level = swing_hi - rng * ratio
            if near(price, level, tol) and price >= cur.open:
                tag = f"FIB_{str(ratio).replace('0.', '')}"
                if tag not in methods:
                    methods.append("FIB_RETRACE")
                break
        for ratio in FIB_EXT:
            level = swing_lo + rng * ratio
            if near(price, level, tol):
                methods.append("FIB_EXT")
                break

    # --- Destek / direnç / pivot / yuvarlak ---
    look = candles[-40:]
    supports: list[float] = []
    resists: list[float] = []
    for i in range(2, len(look) - 2):
        if look[i].low <= look[i - 1].low and look[i].low <= look[i - 2].low and look[i].low <= look[i + 1].low:
            supports.append(look[i].low)
        if look[i].high >= look[i - 1].high and look[i].high >= look[i - 2].high and look[i].high >= look[i + 1].high:
            resists.append(look[i].high)
    if any(near(price, s, tol) for s in supports[-6:]) and price >= cur.open:
        methods.append("DESTEK")
    if any(near(price, r, tol) for r in resists[-6:]):
        methods.append("DIRENC")

    day = candles[-96:] if len(candles) >= 96 else candles
    ph, pl, pc = max(c.high for c in day[:-1]), min(c.low for c in day[:-1]), day[-2].close
    pp = (ph + pl + pc) / 3.0
    r1 = 2 * pp - pl
    s1 = 2 * pp - ph
    if near(price, s1, tol) or near(price, pp, tol):
        methods.append("PIVOT")
    if near(price, r1, tol):
        methods.append("PIVOT_R1")

    mag = 10 ** math.floor(math.log10(price)) if price > 0 else 1
    round_lvl = round(price / mag) * mag
    if near(price, round_lvl, 0.004):
        methods.append("YUVARLAK")

    if len(candles) >= 6:
        l1, l2, l3 = candles[-2].low, candles[-4].low, candles[-6].low
        h1, h2, h3 = candles[-2].high, candles[-4].high, candles[-6].high
        if l1 > l2 > l3:
            methods.append("TREND_HL")
        if h1 < h2 < h3:
            methods.append("KANAL_ALT")

    # --- SMA / EMA ---
    ema20 = ema_last(closes, 20)
    ema50 = ema_last(closes, 50)
    ema100 = ema_last(closes, 100)
    ema200 = ema_last(closes, 200)
    sma20 = sma(closes, 20)
    prev_closes = closes[:-1]
    prev_ema50 = ema_last(prev_closes, 50)
    prev_ema200 = ema_last(prev_closes, 200)
    if ema20 and ema50 and ema100 and price > ema20 > ema50 > ema100:
        methods.append("EMA_RIBBON")
    elif ema20 and ema50 and price > ema20 > ema50:
        methods.append("EMA_STACK")
    if ema20 and sma20 and price > ema20 and price > sma20:
        methods.append("MA20_USTU")
    if ema50 and price > ema50:
        methods.append("EMA50_USTU")
    if ema200 and price > ema200:
        methods.append("EMA200_USTU")
    if ema50 and ema200 and prev_ema50 and prev_ema200:
        if prev_ema50 <= prev_ema200 and ema50 > ema200:
            methods.append("GOLDEN_CROSS")
        if prev_ema50 >= prev_ema200 and ema50 < ema200:
            methods.append("DEATH_CROSS")

    # --- Osilatörler ---
    cur_rsi = rsi_wilder(closes, 14)
    prev_rsi = rsi_wilder(closes[:-1], 14)
    if cur_rsi is not None and prev_rsi is not None:
        if prev_rsi <= 35 and cur_rsi > prev_rsi:
            methods.append("RSI_TOPARLAMA")
        if 50 <= cur_rsi <= 60 and cur_rsi > prev_rsi and cur.close > cur.open:
            methods.append("RSI_NOMLU")

    hh = max(highs[-14:])
    ll = min(lows[-14:])
    stoch_k = ((price - ll) / (hh - ll) * 100.0) if hh > ll else 50.0
    if stoch_k <= 25 and price >= cur.open:
        methods.append("STOCH_OS")

    ema12s = ema_series(closes, 12)
    ema26s = ema_series(closes, 26)
    if ema12s and ema26s:
        n = min(len(ema12s), len(ema26s))
        macd_hist = [a - b for a, b in zip(ema12s[-n:], ema26s[-n:])]
        if len(macd_hist) >= 10:
            signal = ema_last(macd_hist, 9)
            if signal is not None and macd_hist[-2] <= signal and macd_hist[-1] > signal:
                methods.append("MACD_CROSS")
            elif signal is not None and macd_hist[-1] > signal and macd_hist[-1] > 0:
                methods.append("MACD_POZITIF")

    tps = [c.typical for c in candles]
    cci_sma = sma(tps, 20)
    if cci_sma is not None:
        mad = sma([abs(x - cci_sma) for x in tps[-20:]], 20) or 1e-12
        cci = (tps[-1] - cci_sma) / (0.015 * mad)
        if cci <= -100 and price >= cur.open:
            methods.append("CCI_OS")

    will_r = ((hh - price) / (hh - ll) * -100.0) if hh > ll else -50.0
    if will_r <= -80 and price >= cur.open:
        methods.append("WILLIAMS_OS")

    pos_mf = 0.0
    neg_mf = 0.0
    for i in range(-14, 0):
        flow = candles[i].typical * candles[i].volume
        if candles[i].typical > candles[i - 1].typical:
            pos_mf += flow
        else:
            neg_mf += flow
    if neg_mf > 0:
        mfi = 100.0 - (100.0 / (1.0 + pos_mf / neg_mf))
        if mfi <= 30 and price >= cur.open:
            methods.append("MFI_OS")

    # --- Volatilite ---
    bb_mid = sma(closes, 20)
    if bb_mid is not None:
        bb_sd = stdev(closes[-20:])
        bb_low = bb_mid - 2 * bb_sd
        if price <= bb_low * 1.005 and cur.close >= cur.open:
            methods.append("BB_ALT")
    if ema20 and atr14:
        kel_low = ema20 - 2 * atr14
        if price <= kel_low * 1.005 and cur.close >= cur.open:
            methods.append("KELTNER_ALT")
    don_low = min(lows[-20:])
    don_high = max(highs[-21:-1]) if len(highs) >= 21 else max(highs[:-1])
    if price <= don_low * 1.008 and cur.close >= cur.open:
        methods.append("DONCHIAN_ALT")
    if atr14 and prev:
        prev_atr = atr(candles[:-1], 14)
        if prev_atr and atr14 > prev_atr * 1.15 and cur.close > cur.open:
            methods.append("ATR_GENISLEME")

    # --- Trend gücü ---
    plus_dm = 0.0
    minus_dm = 0.0
    tr_sum = 0.0
    for i in range(-14, 0):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm += up if up > down and up > 0 else 0.0
        minus_dm += down if down > up and down > 0 else 0.0
        tr_sum += max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
    if tr_sum > 0:
        pdi = 100 * plus_dm / tr_sum
        mdi = 100 * minus_dm / tr_sum
        dx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) else 0.0
        if dx >= 20 and pdi > mdi:
            methods.append("ADX_ALIS")
        if dx >= 20 and mdi > pdi:
            methods.append("ADX_SATIS")

    # SuperTrend (10, 3)
    if atr14 and ema20:
        st_mid = (cur.high + cur.low) / 2.0
        st_lower = st_mid - 3 * atr14
        st_upper = st_mid + 3 * atr14
        if price > ema20 and price > st_lower:
            methods.append("SUPERTREND_ALIS")
        if price < ema20 and price < st_upper:
            methods.append("SUPERTREND_SATIS")

    # Parabolic SAR (basit)
    if len(candles) >= 8:
        recent_lo = min(c.low for c in candles[-8:-1])
        recent_hi = max(c.high for c in candles[-8:-1])
        if price > recent_hi * 0.999 and cur.close > cur.open:
            methods.append("PSAR_CEVRIM")

    # Ichimoku
    if len(candles) >= 52:
        tenkan = (max(highs[-9:]) + min(lows[-9:])) / 2.0
        kijun = (max(highs[-26:]) + min(lows[-26:])) / 2.0
        senkou_b = (max(highs[-52:]) + min(lows[-52:])) / 2.0
        senkou_a = (tenkan + kijun) / 2.0
        cloud_top = max(senkou_a, senkou_b)
        cloud_bot = min(senkou_a, senkou_b)
        if price > cloud_top and tenkan >= kijun:
            methods.append("ICHIMOKU_ALIS")
        if price < cloud_bot:
            methods.append("ICHIMOKU_SATIS")

    # --- Hacim / CVD ---
    vol_avg = sma(vols[:-1], 20)
    if vol_avg and cur.volume >= 1.5 * vol_avg and cur.close > cur.open:
        methods.append("HACIM_PATLAMASI")

    obv = 0.0
    obv_prev = 0.0
    for i in range(1, len(candles)):
        if candles[i].close > candles[i - 1].close:
            obv += candles[i].volume
        elif candles[i].close < candles[i - 1].close:
            obv -= candles[i].volume
        if i == len(candles) - 2:
            obv_prev = obv
    if obv > obv_prev and cur.close > prev.close:
        methods.append("OBV_YUKSELIS")

    vwap_num = sum(c.typical * c.volume for c in day)
    vwap_den = sum(c.volume for c in day) or 1.0
    vwap = vwap_num / vwap_den
    if price >= vwap and cur.close > cur.open:
        methods.append("VWAP_USTU")
    elif price <= vwap and cur.close < cur.open:
        methods.append("VWAP_ALT")

    if cur.delta > 0 and cur.buy_ratio >= 0.52:
        methods.append("POZITIF_DELTA")
    if cur.delta > 0 and cur.rejection >= 0.20:
        methods.append("DIP_TOPLAMA")

    # --- Mum formasyonları ---
    if cur.rejection >= 0.20 and cur.close >= cur.open:
        methods.append("IGNE_REDDI")
    if (
        cur.body > 0
        and cur.lower_wick >= 2.0 * cur.body
        and cur.upper_wick <= 0.4 * cur.body
        and cur.close >= cur.open
    ):
        methods.append("HAMMER")
    if cur.body > 0 and cur.upper_wick >= 2.0 * cur.body and cur.lower_wick <= 0.4 * cur.body:
        methods.append("SHOOTING_STAR")
    if cur.range > 0 and cur.body / cur.range <= 0.12:
        methods.append("DOJI")
    if prev.close < prev.open and cur.close > cur.open and cur.open <= prev.close and cur.close >= prev.open:
        methods.append("YUTAN_BOGA")
    if prev.close > prev.open and cur.close < cur.open and cur.open >= prev.close and cur.close <= prev.open:
        methods.append("YUTAN_AYI")

    # Çift dip (basit)
    lows_idx = lows[-30:]
    if len(lows_idx) >= 20:
        m1 = min(lows_idx[:15])
        m2 = min(lows_idx[15:])
        if m1 > 0 and abs(m1 - m2) / m1 <= 0.012 and price > max(m1, m2) * 1.004:
            methods.append("CIFT_DIP")

    return methods


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    r = SESSION.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_funding(symbol: str) -> float | None:
    try:
        data = get_json(f"{FAPI_REST}/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(data.get("lastFundingRate"))
    except (requests.RequestException, ValueError, TypeError, KeyError):
        return None


def is_tradeable_usdt(symbol: str) -> bool:
    if not symbol.endswith("USDT"):
        return False
    base = symbol[:-4]
    if base in STABLE_BASES:
        return False
    if any(symbol.endswith(suf) for suf in LEVERAGE_SUFFIXES):
        return False
    return True


def get_universe(dip_only: bool, min_change: float, max_drop: float, min_quote_vol: float) -> list[dict[str, Any]]:
    print("Tum Binance USDT spot taranıyor...")
    rows = get_json(f"{BINANCE_REST}/api/v3/ticker/24hr")
    universe: list[dict[str, Any]] = []
    for item in rows:
        symbol = item.get("symbol") or ""
        if not is_tradeable_usdt(symbol):
            continue
        try:
            change = float(item["priceChangePercent"])
            last = float(item["lastPrice"])
            quote_vol = float(item.get("quoteVolume") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if last <= 0 or quote_vol < min_quote_vol:
            continue
        in_dip = max_drop <= change <= min_change
        if dip_only and not in_dip:
            continue
        universe.append(
            {
                "symbol": symbol,
                "change": change,
                "last": last,
                "quote_vol": quote_vol,
                "in_dip": in_dip,
            }
        )
    universe.sort(key=lambda x: x["quote_vol"], reverse=True)
    print(f"Takibe alınan parite: {len(universe)}")
    return universe


def fetch_klines(symbol: str, limit: int = KLINE_LIMIT) -> list[Candle]:
    rows = get_json(
        f"{BINANCE_REST}/api/v3/klines",
        params={"symbol": symbol, "interval": KLINE_INTERVAL, "limit": limit},
    )
    if not isinstance(rows, list):
        return []
    return [parse_kline_rest(row) for row in rows]


def scan_symbol(meta: dict[str, Any], min_score: int, use_funding: bool) -> dict[str, Any] | None:
    symbol = meta["symbol"]
    try:
        candles = fetch_klines(symbol, limit=KLINE_LIMIT)
    except (requests.RequestException, ValueError) as exc:
        print(f"[warn] {symbol} kline alınamadı: {exc}", file=sys.stderr)
        return None
    if len(candles) < 30:
        return None
    closed = candles[:-1] if not candles[-1].closed else candles
    if len(closed) < 30:
        return None
    candle = closed[-1]
    now_ms = int(time.time() * 1000)
    if now_ms - candle.close_time > 45 * 60 * 1000:
        return None
    if candle.taker_buy_vol < 0 or candle.taker_buy_vol > candle.volume * 1.01:
        return None
    methods = detect_methods(closed)
    if use_funding:
        fr = fetch_funding(symbol)
        if fr is not None and fr < 0 and candle.close >= candle.open:
            methods.append("FUNDING_NEGATIF")
    bull = [m for m in methods if m in BULLISH_TAGS]
    if len(bull) < min_score:
        return None
    return {
        "symbol": symbol,
        "change_24h": meta["change"],
        "in_dip": meta["in_dip"],
        "price": candle.close,
        "delta": candle.delta,
        "rejection": candle.rejection,
        "buy_ratio": candle.buy_ratio,
        "volume": candle.volume,
        "open_time": candle.open_time,
        "methods": methods,
        "score": len(bull),
    }


def fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def print_hit(hit: dict[str, Any], live: bool = False) -> None:
    tag = "CANLI" if live else "KAPANMIS 15M"
    dip = "DIP" if hit.get("in_dip") else "SPOT"
    methods = ",".join(hit["methods"])
    print(
        f"[{tag}] {hit['symbol']:<12} {dip:<4} "
        f"Fiyat: ${hit['price']:<12.6g} "
        f"24s: {hit['change_24h']:+6.2f}% "
        f"Delta: {hit['delta']:+10.2f} "
        f"Igne: %{hit['rejection']*100:5.1f} "
        f"Alis: %{hit['buy_ratio']*100:5.1f} "
        f"Skor: {hit['score']} "
        f"| {methods} "
        f"| {fmt_time(hit['open_time'])} UTC"
    )


def has_buy_pressure(hit: dict[str, Any]) -> bool:
    return "POZITIF_DELTA" in hit["methods"] or "DIP_TOPLAMA" in hit["methods"]


def rest_scan(
    universe: list[dict[str, Any]],
    min_score: int,
    workers: int,
    require_delta: bool,
    use_funding: bool,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    print(f"15m mumlar çekiliyor ({len(universe)} parite, {workers} işçi)...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(scan_symbol, meta, min_score, use_funding): meta["symbol"] for meta in universe
        }
        for fut in as_completed(futs):
            hit = fut.result()
            if not hit:
                continue
            if require_delta and not has_buy_pressure(hit):
                continue
            hits.append(hit)
            print_hit(hit)
    hits.sort(key=lambda x: (x["score"], x["buy_ratio"], -abs(x["change_24h"])), reverse=True)
    return hits


def chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class IntervalSocket:
    def __init__(
        self,
        symbols: list[str],
        min_score: int,
        cache: dict[str, list[Candle]],
        lock: threading.Lock,
        require_delta: bool,
    ):
        self.symbols = [s.lower() for s in symbols]
        self.min_score = min_score
        self.cache = cache
        self.lock = lock
        self.require_delta = require_delta
        self.seen: set[str] = set()
        self.ws: Any = None

    def _url(self) -> str:
        streams = "/".join(f"{s}@kline_{KLINE_INTERVAL}" for s in self.symbols)
        return f"{BINANCE_WS}?streams={streams}"

    def on_message(self, _ws: Any, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or "k" not in data:
            return
        k = data["k"]
        symbol = str(k.get("s") or "")
        candle = parse_kline_ws(k)
        if not candle.closed:
            return
        key = f"{symbol}:{candle.open_time}"
        if key in self.seen:
            return
        self.seen.add(key)

        with self.lock:
            hist = list(self.cache.get(symbol, []))
            if hist and hist[-1].open_time == candle.open_time:
                hist[-1] = candle
            else:
                hist.append(candle)
            if len(hist) > KLINE_LIMIT:
                hist = hist[-KLINE_LIMIT:]
            self.cache[symbol] = hist
            methods = detect_methods(hist)

        bull = [m for m in methods if m in BULLISH_TAGS]
        if len(bull) < self.min_score:
            return
        hit = {
            "symbol": symbol,
            "change_24h": 0.0,
            "in_dip": True,
            "price": candle.close,
            "delta": candle.delta,
            "rejection": candle.rejection,
            "buy_ratio": candle.buy_ratio,
            "volume": candle.volume,
            "open_time": candle.open_time,
            "methods": methods,
            "score": len(bull),
        }
        if self.require_delta and not has_buy_pressure(hit):
            return
        print_hit(hit, live=True)

    def run(self) -> None:
        if websocket is None:
            raise SystemExit("websocket-client yüklü değil. pip install websocket-client")
        url = self._url()

        def on_error(_ws: Any, err: Any) -> None:
            print(f"[ws] hata: {err}", file=sys.stderr)

        def on_close(_ws: Any, status: Any, msg: Any) -> None:
            print(f"[ws] kapandı status={status} msg={msg}")

        def on_open(_ws: Any) -> None:
            print(f"[ws] bağlandı ({len(self.symbols)} stream, {KLINE_INTERVAL})")

        while True:
            self.ws = websocket.WebSocketApp(
                url,
                on_message=self.on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )
            self.ws.run_forever(ping_interval=20, ping_timeout=10)
            print("[ws] 5 sn sonra yeniden bağlanılacak...")
            time.sleep(5)


def seed_cache(universe: list[dict[str, Any]], workers: int) -> dict[str, list[Candle]]:
    cache: dict[str, list[Candle]] = {}
    print("Websocket için 15m geçmiş yükleniyor...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_klines, meta["symbol"], KLINE_LIMIT): meta["symbol"] for meta in universe}
        for fut in as_completed(futs):
            symbol = futs[fut]
            try:
                cache[symbol] = fut.result()
            except (requests.RequestException, ValueError):
                continue
    return cache


def print_summary(hits: list[dict[str, Any]]) -> None:
    print("\n========== 15M TARAMA OZETI ==========")
    if not hits:
        print("Bu 15 dakikada skor eşiğini geçen sinyal yok.")
        return
    print(f"Toplam sinyal: {len(hits)}")
    print(f"{'SKOR':<6}{'SEMBOL':<12}{'24S':>8}  METODLAR")
    for hit in hits[:40]:
        print(
            f"{hit['score']:<6}{hit['symbol']:<12}{hit['change_24h']:+7.2f}%  "
            f"{','.join(hit['methods'])}"
        )
    print("======================================\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Binance 15m TA / dip dönüş radarı")
    p.add_argument("--once", action="store_true", help="Tek REST taraması, websocket yok")
    p.add_argument("--all-symbols", action="store_true", help="Dip filtresini kapat, tüm USDT'yi tara")
    p.add_argument("--min-change", type=float, default=-3.0, help="Dip üst eşiği (varsayılan -3)")
    p.add_argument("--max-drop", type=float, default=-30.0, help="Dip alt eşiği (varsayılan -30)")
    p.add_argument("--min-score", type=int, default=4, help="Kaç metod aynı anda tutmalı")
    p.add_argument("--require-delta", action="store_true", help="Net alıcı (delta) şartını aç")
    p.add_argument("--loose", action="store_true", help="(eski) net alıcı şartını kapat — artık varsayılan")
    p.add_argument("--funding", action="store_true", help="Futures funding oranını da skorla")
    p.add_argument("--workers", type=int, default=8, help="REST paralel işçi sayısı")
    p.add_argument("--refresh-min", type=int, default=15, help="REST taramasını kaç dakikada bir tekrarla")
    p.add_argument("--no-ws", action="store_true", help="Sadece REST döngüsü")
    p.add_argument("--min-quote-vol", type=float, default=200_000, help="24s min USDT hacim")
    p.add_argument("--max-ws", type=int, default=400, help="Websocket'e alınacak en likit parite sayısı")
    p.add_argument("--max-scan", type=int, default=0, help="REST taramasını ilk N pariteyle sınırla (0=hepsi)")
    return p.parse_args()


def rest_loop(dip_only: bool, args: argparse.Namespace, require_delta: bool) -> None:
    while True:
        time.sleep(max(60, args.refresh_min * 60))
        universe = get_universe(dip_only, args.min_change, args.max_drop, args.min_quote_vol)
        hits = rest_scan(universe, args.min_score, args.workers, require_delta, args.funding)
        print_summary(hits)


def main() -> int:
    args = parse_args()
    print("=" * 58)
    print("  15M TA / DIP DONUS RADARI")
    print("  EMA+RSI+FIB+Destek/Direnc+Hacim + osilator/volatilite")
    print("=" * 58)

    dip_only = not args.all_symbols
    universe = get_universe(dip_only, args.min_change, args.max_drop, args.min_quote_vol)
    if args.max_scan and args.max_scan > 0:
        universe = universe[: args.max_scan]
        print(f"Tarama ilk {len(universe)} pariteyle sınırlandı")
    if not universe:
        print("Taranacak parite yok.")
        return 1

    require_delta = bool(args.require_delta)
    hits = rest_scan(universe, args.min_score, args.workers, require_delta, args.funding)
    print_summary(hits)

    if args.once:
        return 0

    if args.no_ws or websocket is None:
        if websocket is None:
            print("websocket-client yok; REST döngüsüne düşülüyor.", file=sys.stderr)
        rest_loop(dip_only, args, require_delta)
        return 0

    ws_universe = universe[: args.max_ws]
    cache = seed_cache(ws_universe, args.workers)
    lock = threading.Lock()
    batches = chunks([m["symbol"] for m in ws_universe], MAX_STREAMS_PER_SOCKET)
    print(f"Canlı {KLINE_INTERVAL} websocket: {len(ws_universe)} parite, {len(batches)} bağlantı")
    for batch in batches:
        sock = IntervalSocket(batch, args.min_score, cache, lock, require_delta)
        threading.Thread(target=sock.run, daemon=True).start()
        time.sleep(0.4)

    print("15m mum kapanışında sinyaller yazılacak. Durdurmak için Ctrl+C.")
    try:
        rest_loop(dip_only, args, require_delta)
    except KeyboardInterrupt:
        print("\nDurduruldu.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
