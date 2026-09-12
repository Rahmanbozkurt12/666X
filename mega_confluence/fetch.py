#!/usr/bin/env python3
"""Binance spot + futures data fetchers (no API key required for public endpoints)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

UA = {"User-Agent": "666X-MegaConfluence/1.0 (+research; paper-trading)"}

SPOT_BASES = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://www.binance.com",
)
FAPI_BASES = (
    "https://fapi.binance.com",
    "https://www.binance.com",
)

_active_spot: str | None = None
_active_fapi: str | None = None


def _http_get(url: str, *, timeout: float = 25.0, retries: int = 4) -> Any:
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if not raw:
                    raise ValueError("empty body")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (418, 429, 500, 502, 503, 504):
                time.sleep(min(8.0, 0.4 * (2**attempt)))
                continue
            raise RuntimeError(f"HTTP {e.code} for {url}") from e
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(min(8.0, 0.4 * (2**attempt)))
    raise RuntimeError(f"GET failed {url}: {last_err}")


def spot_get(path: str, params: dict[str, Any] | None = None, *, timeout: float = 25.0) -> Any:
    global _active_spot
    bases = list(SPOT_BASES)
    if _active_spot:
        bases = [_active_spot] + [b for b in bases if b != _active_spot]
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    last_err: Exception | None = None
    for base in bases:
        try:
            data = _http_get(f"{base}{path}{qs}", timeout=timeout)
            _active_spot = base
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(f"spot unreachable {path}: {last_err}")


def fapi_get(path: str, params: dict[str, Any] | None = None, *, timeout: float = 25.0) -> Any:
    global _active_fapi
    bases = list(FAPI_BASES)
    if _active_fapi:
        bases = [_active_fapi] + [b for b in bases if b != _active_fapi]
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    last_err: Exception | None = None
    for base in bases:
        if "fapi.binance.com" in base:
            url = f"{base}{path}{qs}"
        else:
            # www.binance.com mirrors /fapi and /futures
            url = f"{base}{path}{qs}"
        try:
            data = _http_get(url, timeout=timeout)
            _active_fapi = base
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(f"fapi unreachable {path}: {last_err}")


def parse_klines(raw: list) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for c in raw:
        out.append(
            {
                "time": float(c[0]) / 1000.0,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "quote_volume": float(c[7]),
                "trades": float(c[8]),
                "taker_buy_base": float(c[9]),
                "taker_buy_quote": float(c[10]),
            }
        )
    return out


def fetch_klines(symbol: str, interval: str = "15m", limit: int = 500) -> list[dict[str, float]]:
    raw = spot_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not isinstance(raw, list) or len(raw) < 50:
        raise RuntimeError(f"bad klines for {symbol}")
    return parse_klines(raw)


def fetch_ticker_24h(symbol: str | None = None) -> Any:
    params = {"symbol": symbol} if symbol else None
    return spot_get("/api/v3/ticker/24hr", params)


def fetch_depth(symbol: str, limit: int = 100) -> dict[str, Any]:
    return spot_get("/api/v3/depth", {"symbol": symbol, "limit": limit})


def fetch_agg_trades(symbol: str, limit: int = 500) -> list[dict[str, Any]]:
    raw = spot_get("/api/v3/aggTrades", {"symbol": symbol, "limit": limit})
    return raw if isinstance(raw, list) else []


def fetch_premium_index(symbol: str) -> dict[str, Any] | None:
    try:
        data = fapi_get("/fapi/v1/premiumIndex", {"symbol": symbol})
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def fetch_open_interest(symbol: str) -> float | None:
    try:
        data = fapi_get("/fapi/v1/openInterest", {"symbol": symbol})
        if isinstance(data, dict) and "openInterest" in data:
            return float(data["openInterest"])
    except Exception:
        return None
    return None


def fetch_oi_hist(symbol: str, period: str = "1h", limit: int = 24) -> list[dict[str, Any]]:
    try:
        data = fapi_get(
            "/futures/data/openInterestHist",
            {"symbol": symbol, "period": period, "limit": limit},
        )
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fetch_long_short_ratio(symbol: str, period: str = "1h", limit: int = 24) -> list[dict[str, Any]]:
    try:
        data = fapi_get(
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol, "period": period, "limit": limit},
        )
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fetch_taker_ratio(symbol: str, period: str = "1h", limit: int = 24) -> list[dict[str, Any]]:
    try:
        data = fapi_get(
            "/futures/data/takerlongshortRatio",
            {"symbol": symbol, "period": period, "limit": limit},
        )
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fetch_funding_hist(symbol: str, limit: int = 50) -> list[dict[str, Any]]:
    try:
        data = fapi_get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fetch_btc_dominance_proxy() -> dict[str, float] | None:
    """Approximate BTC dominance from Binance USDT quote volumes."""
    try:
        tickers = fetch_ticker_24h()
        if not isinstance(tickers, list):
            return None
        btc = 0.0
        total = 0.0
        for t in tickers:
            sym = str(t.get("symbol") or "")
            if not sym.endswith("USDT"):
                continue
            qv = float(t.get("quoteVolume") or 0)
            total += qv
            if sym == "BTCUSDT":
                btc = qv
        if total <= 0:
            return None
        return {"btc_vol_share": btc / total, "total_usdt_quote_vol": total}
    except Exception:
        return None


def gather_market_bundle(symbol: str, interval: str = "15m") -> dict[str, Any]:
    """Collect all public market datasets used by method scorers."""
    candles = fetch_klines(symbol, interval=interval, limit=500)
    htf = fetch_klines(symbol, interval="1h", limit=300)
    ticker = fetch_ticker_24h(symbol)
    depth = fetch_depth(symbol, limit=100)
    trades = fetch_agg_trades(symbol, limit=500)
    premium = fetch_premium_index(symbol)
    oi = fetch_open_interest(symbol)
    oi_hist = fetch_oi_hist(symbol)
    ls = fetch_long_short_ratio(symbol)
    taker = fetch_taker_ratio(symbol)
    funding_hist = fetch_funding_hist(symbol)
    btc_dom = fetch_btc_dominance_proxy()

    # BTC context for correlation / relative strength
    btc_candles = None
    try:
        if symbol != "BTCUSDT":
            btc_candles = fetch_klines("BTCUSDT", interval=interval, limit=200)
    except Exception:
        btc_candles = None

    return {
        "symbol": symbol,
        "interval": interval,
        "candles": candles,
        "htf_candles": htf,
        "ticker": ticker if isinstance(ticker, dict) else {},
        "depth": depth if isinstance(depth, dict) else {},
        "agg_trades": trades,
        "premium": premium or {},
        "open_interest": oi,
        "oi_hist": oi_hist,
        "long_short": ls,
        "taker_ratio": taker,
        "funding_hist": funding_hist,
        "btc_dominance": btc_dom,
        "btc_candles": btc_candles,
        "fetched_at": time.time(),
    }
