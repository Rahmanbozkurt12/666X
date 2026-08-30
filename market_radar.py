#!/usr/bin/env python3
"""
Market Radar — CEX FOMO / short-squeeze / dump-risk UYARI tarayıcısı.

GÜVENLİK:
  - Otomatik al-sat YOK
  - Varsayılan dry-run
  - Yüksek severity + cooldown + mute noisy kinds
  - Kaynak hata olursa fail-soft (crash trade yok)
  - Yatırım tavsiyesi değildir; yanlış sinyal olabilir
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "market_radar.json"
STATE_PATH = ROOT / "output" / "market_radar_state.json"
OUT_PATH = ROOT / "output" / "market_radar_last.json"
LOG_PATH = ROOT / "output" / "market_radar.log"
HEALTH_PATH = ROOT / "output" / "market_radar_health.json"

BINANCE = "https://www.binance.com"
UA = {"User-Agent": "RATIO-MarketRadar/1.1 (+research; no-trading)"}

REQUIRED_SETTINGS = [
    "poll_seconds",
    "dry_run_default",
    "max_alerts_per_cycle",
    "min_severity_to_alert",
    "min_quote_volume_usdt",
    "fomo_chg_pct",
    "korea_lead_ratio",
    "squeeze_funding_lt",
    "squeeze_oi_drop_pct",
    "squeeze_price_up_pct",
    "dump_risk_chg_pct",
]


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be object")
    return data


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def safe_float(x: Any, default: float | None = None) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):  # NaN/inf
        return default
    return v


def validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    if "settings" not in cfg or not isinstance(cfg["settings"], dict):
        raise ValueError("config.settings missing")
    s = cfg["settings"]
    missing = [k for k in REQUIRED_SETTINGS if k not in s]
    if missing:
        raise ValueError(f"config missing settings: {missing}")
    # type/range checks
    if float(s["min_quote_volume_usdt"]) < 100000:
        raise ValueError("min_quote_volume_usdt too low (<100k) — noise risk")
    if float(s["fomo_chg_pct"]) < 5:
        raise ValueError("fomo_chg_pct too low (<5)")
    if int(s["min_severity_to_alert"]) < 3:
        raise ValueError("min_severity_to_alert should be >= 3 for lower false positives")
    if int(s["poll_seconds"]) < 60:
        raise ValueError("poll_seconds < 60 may rate-limit")
    cfg.setdefault("watchlist", [])
    cfg.setdefault("always_scan_top_n", 30)
    s.setdefault("mute_kinds", ["CEX_ORDERBOOK"])
    s.setdefault("korea_min_vol_usdt", 5_000_000)
    s.setdefault("alert_cooldown_hours", 6)
    s.setdefault("http_retries", 4)
    s.setdefault("http_timeout_sec", 25)
    s.setdefault("futures_sleep_sec", 0.08)
    s.setdefault("squeeze_require_taker_buy", True)
    s.setdefault("dump_risk_funding_gt", 0.0008)
    return cfg


def http_get(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: int = 25,
    retries: int = 4,
) -> Any:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
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
            # backoff more on 418/429/5xx
            wait = min(8.0, 0.5 * (2**attempt))
            if e.code in (418, 429, 500, 502, 503, 504):
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code} for {url}") from e
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(min(8.0, 0.5 * (2**attempt)))
    raise RuntimeError(f"GET failed {url}: {last_err}")


@dataclass
class SpotRow:
    symbol: str
    base: str
    last: float
    chg_pct: float
    quote_vol: float
    high: float
    low: float


@dataclass
class Signal:
    kind: str
    symbol: str
    severity: int
    title: str
    detail: str
    metrics: dict[str, Any]
    confirmations: int = 1


def binance_spot_tickers(timeout: int, retries: int) -> list[SpotRow]:
    data = http_get(f"{BINANCE}/api/v3/ticker/24hr", timeout=timeout, retries=retries)
    if not isinstance(data, list):
        raise RuntimeError("binance spot ticker unexpected")
    out: list[SpotRow] = []
    for t in data:
        sym = str(t.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        if any(x in sym for x in ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")):
            continue
        last = safe_float(t.get("lastPrice"))
        chg = safe_float(t.get("priceChangePercent"))
        vol = safe_float(t.get("quoteVolume"))
        high = safe_float(t.get("highPrice"))
        low = safe_float(t.get("lowPrice"))
        if None in (last, chg, vol, high, low):
            continue
        assert last is not None and chg is not None and vol is not None
        assert high is not None and low is not None
        if last <= 0 or vol < 0 or high < low:
            continue
        out.append(
            SpotRow(
                symbol=sym,
                base=sym[:-4],
                last=last,
                chg_pct=chg,
                quote_vol=vol,
                high=high,
                low=low,
            )
        )
    if len(out) < 50:
        raise RuntimeError(f"binance spot too few rows: {len(out)}")
    return out


def binance_futures_metrics(symbol: str, timeout: int, retries: int) -> dict[str, Any] | None:
    try:
        prem = http_get(
            f"{BINANCE}/fapi/v1/premiumIndex",
            {"symbol": symbol},
            timeout=timeout,
            retries=max(2, retries - 1),
        )
        funding = safe_float(prem.get("lastFundingRate"))
        if funding is None:
            return None
    except Exception:
        return None

    ls_ratio = None
    try:
        ls = http_get(
            f"{BINANCE}/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol, "period": "1h", "limit": 1},
            timeout=timeout,
            retries=2,
        )
        if isinstance(ls, list) and ls:
            ls_ratio = safe_float(ls[0].get("longShortRatio"))
    except Exception:
        pass

    oi_chg = None
    oi_usd = None
    try:
        oi = http_get(
            f"{BINANCE}/futures/data/openInterestHist",
            {"symbol": symbol, "period": "1h", "limit": 3},
            timeout=timeout,
            retries=2,
        )
        if isinstance(oi, list) and len(oi) >= 2:
            now = safe_float(oi[-1].get("sumOpenInterestValue"))
            prev = safe_float(oi[-2].get("sumOpenInterestValue"))
            if now is not None and prev is not None and prev > 0:
                oi_usd = now
                oi_chg = (now - prev) / prev * 100
    except Exception:
        pass

    taker = None
    try:
        tk = http_get(
            f"{BINANCE}/futures/data/takerlongshortRatio",
            {"symbol": symbol, "period": "1h", "limit": 1},
            timeout=timeout,
            retries=2,
        )
        if isinstance(tk, list) and tk:
            taker = safe_float(tk[0].get("buySellRatio"))
    except Exception:
        pass

    return {
        "funding": funding,
        "ls_ratio": ls_ratio,
        "oi_usd": oi_usd,
        "oi_1h_chg_pct": oi_chg,
        "taker_bs": taker,
    }


def upbit_krw_map(timeout: int, retries: int) -> dict[str, dict[str, float]]:
    markets = http_get(
        "https://api.upbit.com/v1/market/all",
        {"isDetails": "false"},
        timeout=timeout,
        retries=retries,
    )
    krw = [
        m["market"]
        for m in markets
        if isinstance(m, dict) and str(m.get("market", "")).startswith("KRW-")
    ]
    out: dict[str, dict[str, float]] = {}
    for i in range(0, len(krw), 80):
        chunk = krw[i : i + 80]
        tick = http_get(
            "https://api.upbit.com/v1/ticker",
            {"markets": ",".join(chunk)},
            timeout=timeout,
            retries=retries,
        )
        if not isinstance(tick, list):
            continue
        for t in tick:
            market = str(t.get("market") or "")
            base = market.replace("KRW-", "")
            last = safe_float(t.get("trade_price"))
            chg = safe_float(t.get("signed_change_rate"))
            q = safe_float(t.get("acc_trade_price_24h"))
            if None in (last, chg, q) or last is None or chg is None or q is None:
                continue
            if last <= 0 or q < 0:
                continue
            out[base] = {"last": last, "chg_pct": chg * 100, "quote_krw": q}
        time.sleep(0.05)
    return out


def bithumb_krw_map(timeout: int, retries: int) -> dict[str, dict[str, float]]:
    data = http_get(
        "https://api.bithumb.com/public/ticker/ALL_KRW",
        timeout=timeout,
        retries=retries,
    )
    rows = (data or {}).get("data") or {}
    out: dict[str, dict[str, float]] = {}
    if not isinstance(rows, dict):
        return out
    for base, t in rows.items():
        if base == "date" or not isinstance(t, dict):
            continue
        prev = safe_float(t.get("prev_closing_price"), 0.0) or 0.0
        last = safe_float(t.get("closing_price"))
        q = safe_float(t.get("acc_trade_value_24H") or t.get("acc_trade_value"), 0.0) or 0.0
        if last is None or last <= 0:
            continue
        chg = ((last - prev) / prev * 100) if prev > 0 else 0.0
        out[base] = {"last": last, "chg_pct": chg, "quote_krw": q}
    return out


def btcturk_try_map(timeout: int, retries: int) -> dict[str, dict[str, float]]:
    data = http_get("https://api.btcturk.com/api/v2/ticker", timeout=timeout, retries=retries)
    rows = (data or {}).get("data") or []
    out: dict[str, dict[str, float]] = {}
    if not isinstance(rows, list):
        return out
    for t in rows:
        pair = str(t.get("pairNormalized") or "")
        if not pair.endswith("_TRY"):
            continue
        base = pair.replace("_TRY", "")
        last = safe_float(t.get("last"))
        vol = safe_float(t.get("volume"), 0.0) or 0.0
        chg = safe_float(t.get("dailyPercent"), 0.0) or 0.0
        if last is None or last <= 0:
            continue
        out[base] = {"last": last, "chg_pct": chg, "quote_try": vol * last}
    return out


def approx_fx(timeout: int, retries: int) -> dict[str, float]:
    usdt_try = 48.0
    usdt_krw = 1350.0
    try:
        t = http_get(
            f"{BINANCE}/api/v3/ticker/price",
            {"symbol": "USDTTRY"},
            timeout=timeout,
            retries=2,
        )
        v = safe_float(t.get("price"))
        if v and 10 < v < 200:
            usdt_try = v
    except Exception as e:
        logging.warning("USDTTRY fx fallback: %s", e)
    try:
        t = http_get(
            "https://api.upbit.com/v1/ticker",
            {"markets": "KRW-USDT"},
            timeout=timeout,
            retries=2,
        )
        if isinstance(t, list) and t:
            v = safe_float(t[0].get("trade_price"))
            if v and 500 < v < 5000:
                usdt_krw = v
    except Exception as e:
        logging.warning("USDTKRW fx fallback: %s", e)
    return {"USDT_TRY": usdt_try, "USDT_KRW": usdt_krw}


def pick_universe(spots: list[SpotRow], cfg: dict[str, Any]) -> list[SpotRow]:
    s = cfg["settings"]
    exclude = set(s.get("majors_exclude") or [])
    watch = {str(w).upper() for w in cfg.get("watchlist") or []}
    min_vol = float(s["min_quote_volume_usdt"])
    ranked = sorted(
        [x for x in spots if x.symbol not in exclude and x.quote_vol >= min_vol],
        key=lambda x: x.quote_vol,
        reverse=True,
    )
    top_n = int(cfg.get("always_scan_top_n") or 30)
    chosen = {x.symbol: x for x in ranked[:top_n]}
    by_sym = {x.symbol: x for x in spots}
    for w in watch:
        if w in by_sym:
            chosen[w] = by_sym[w]
    return list(chosen.values())


def detect_signals(
    row: SpotRow,
    fut: dict[str, Any] | None,
    korea_quote_usdt: float,
    turk_quote_usdt: float,
    cfg: dict[str, Any],
) -> list[Signal]:
    s = cfg["settings"]
    out: list[Signal] = []
    chg = row.chg_pct
    min_vol = float(s["min_quote_volume_usdt"])

    if chg >= float(s["fomo_chg_pct"]) and row.quote_vol >= min_vol:
        sev = 4 if chg >= 20 else 3
        if chg >= 40:
            sev = 5
        conf = 1 + (1 if row.quote_vol >= min_vol * 2 else 0) + (1 if chg >= 20 else 0)
        out.append(
            Signal(
                kind="CEX_FOMO",
                symbol=row.symbol,
                severity=sev,
                title=f"CEX FOMO {row.base}",
                detail=f"Binance spot {chg:+.1f}% · 24h vol ${row.quote_vol/1e6:.1f}M",
                metrics={"chg_pct": chg, "binance_vol_usdt": row.quote_vol},
                confirmations=conf,
            )
        )

    korea_min = float(s.get("korea_min_vol_usdt") or 5_000_000)
    if korea_quote_usdt >= korea_min and row.quote_vol > 0:
        ratio = korea_quote_usdt / row.quote_vol
        if ratio >= float(s["korea_lead_ratio"]) and chg >= float(s["fomo_chg_pct"]) * 0.7:
            out.append(
                Signal(
                    kind="KOREA_FOMO",
                    symbol=row.symbol,
                    severity=5 if ratio >= 2 else 4,
                    title=f"Korea lead FOMO {row.base}",
                    detail=(
                        f"Korea ${korea_quote_usdt/1e6:.1f}M vs Binance ${row.quote_vol/1e6:.1f}M "
                        f"(x{ratio:.2f}) · chg {chg:+.1f}%"
                    ),
                    metrics={
                        "korea_vol_usdt": korea_quote_usdt,
                        "binance_vol_usdt": row.quote_vol,
                        "korea_lead_x": ratio,
                        "chg_pct": chg,
                    },
                    confirmations=2 + (1 if ratio >= 2 else 0),
                )
            )

    if turk_quote_usdt > row.quote_vol * 0.4 and chg >= float(s["fomo_chg_pct"]) * 0.8:
        out.append(
            Signal(
                kind="TURKEY_FOMO",
                symbol=row.symbol,
                severity=3,
                title=f"TR CEX hot {row.base}",
                detail=(
                    f"BtcTurk≈${turk_quote_usdt/1e6:.1f}M · Binance ${row.quote_vol/1e6:.1f}M · {chg:+.1f}%"
                ),
                metrics={"btcturk_vol_usdt": turk_quote_usdt, "chg_pct": chg},
                confirmations=2,
            )
        )

    if fut and fut.get("oi_1h_chg_pct") is not None and fut.get("funding") is not None:
        funding = float(fut["funding"])
        oi_chg = float(fut["oi_1h_chg_pct"])
        taker = fut.get("taker_bs")
        taker_ok = True
        if s.get("squeeze_require_taker_buy", True):
            taker_ok = taker is not None and float(taker) >= 1.0
        if (
            funding <= float(s["squeeze_funding_lt"])
            and oi_chg <= -float(s["squeeze_oi_drop_pct"])
            and chg >= float(s["squeeze_price_up_pct"])
            and taker_ok
        ):
            out.append(
                Signal(
                    kind="SHORT_SQUEEZE",
                    symbol=row.symbol,
                    severity=5,
                    title=f"Short squeeze risk {row.base}",
                    detail=(
                        f"funding {funding:.5f} · OI1h {oi_chg:+.2f}% · spot {chg:+.1f}% "
                        f"· taker {taker} · L/S {fut.get('ls_ratio')}"
                    ),
                    metrics={
                        "funding": funding,
                        "oi_1h_chg_pct": oi_chg,
                        "ls_ratio": fut.get("ls_ratio"),
                        "taker_bs": taker,
                        "chg_pct": chg,
                    },
                    confirmations=3,
                )
            )

    if chg >= float(s["dump_risk_chg_pct"]) and fut and fut.get("funding") is not None:
        if float(fut["funding"]) >= float(s.get("dump_risk_funding_gt") or 0.0008):
            out.append(
                Signal(
                    kind="DUMP_RISK",
                    symbol=row.symbol,
                    severity=4,
                    title=f"Dump risk {row.base}",
                    detail=(
                        f"Move {chg:+.1f}% + funding +{float(fut['funding']):.5f} (kalabalık long)"
                    ),
                    metrics={"chg_pct": chg, "funding": fut.get("funding")},
                    confirmations=2,
                )
            )

    if chg >= 20 and row.quote_vol >= 15_000_000:
        out.append(
            Signal(
                kind="CEX_ORDERBOOK",
                symbol=row.symbol,
                severity=2,
                title=f"CEX orderbook move {row.base}",
                detail="Büyük CEX hacmi — on-chain whale varsayma",
                metrics={"chg_pct": chg, "binance_vol_usdt": row.quote_vol},
                confirmations=1,
            )
        )

    return out


def telegram_send(token: str, chat_id: str, text: str, dry_run: bool) -> bool:
    if dry_run:
        print("--- DRY-RUN TELEGRAM ---\n" + text + "\n------------------------")
        return True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={**UA, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            return bool(body.get("ok"))
    except Exception as e:
        logging.error("telegram send failed: %s", e)
        return False


def format_signal(sig: Signal) -> str:
    return (
        f"{sig.kind} | {sig.symbol}\n"
        f"{sig.title}\n"
        f"{sig.detail}\n"
        f"severity={sig.severity}/5 · confirmations={sig.confirmations}\n"
        f"⚠️ UYARI ONLY — otomatik emir YOK. Kendi stop'unu kullan."
    )


def signal_key(sig: Signal) -> str:
    return f"{sig.kind}:{sig.symbol}"


def run_health(cfg: dict[str, Any]) -> dict[str, Any]:
    s = cfg["settings"]
    timeout = int(s["http_timeout_sec"])
    retries = int(s["http_retries"])
    health: dict[str, Any] = {"as_of_utc": datetime.now(timezone.utc).isoformat(), "ok": True, "checks": {}}
    checks = health["checks"]

    def check(name: str, fn: Any) -> None:
        t0 = time.time()
        try:
            fn()
            checks[name] = {"ok": True, "ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            health["ok"] = False
            checks[name] = {"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)}

    check("binance_spot", lambda: binance_spot_tickers(timeout, retries)[:1])
    check("binance_futures_btc", lambda: binance_futures_metrics("BTCUSDT", timeout, retries))
    check("upbit", lambda: upbit_krw_map(timeout, max(2, retries - 1)))
    check("bithumb", lambda: bithumb_krw_map(timeout, retries))
    check("btcturk", lambda: btcturk_try_map(timeout, retries))
    check("fx", lambda: approx_fx(timeout, retries))
    save_json(HEALTH_PATH, health)
    return health


def run_cycle(cfg: dict[str, Any], state: dict[str, Any], dry_run: bool) -> list[Signal]:
    settings = cfg["settings"]
    timeout = int(settings["http_timeout_sec"])
    retries = int(settings["http_retries"])
    logging.info("scan start dry_run=%s", dry_run)

    spots = binance_spot_tickers(timeout, retries)
    universe = pick_universe(spots, cfg)
    logging.info("universe=%s", len(universe))

    fx = approx_fx(timeout, retries)
    usdt_krw = fx["USDT_KRW"] or 1350.0
    usdt_try = fx["USDT_TRY"] or 48.0

    upbit: dict[str, dict[str, float]] = {}
    bithumb: dict[str, dict[str, float]] = {}
    btcturk: dict[str, dict[str, float]] = {}
    try:
        upbit = upbit_krw_map(timeout, retries)
        logging.info("upbit=%s", len(upbit))
    except Exception as e:
        logging.warning("upbit failed: %s", e)
    try:
        bithumb = bithumb_krw_map(timeout, retries)
        logging.info("bithumb=%s", len(bithumb))
    except Exception as e:
        logging.warning("bithumb failed: %s", e)
    try:
        btcturk = btcturk_try_map(timeout, retries)
        logging.info("btcturk=%s", len(btcturk))
    except Exception as e:
        logging.warning("btcturk failed: %s", e)

    # if ALL secondary sources fail, still continue with Binance-only but mark degraded
    degraded = not upbit and not bithumb and not btcturk
    if degraded:
        logging.warning("degraded mode: only Binance sources available")

    signals: list[Signal] = []
    snapshots: list[dict[str, Any]] = []
    sleep_f = float(settings.get("futures_sleep_sec") or 0.08)

    for row in universe:
        fut = binance_futures_metrics(row.symbol, timeout, retries)
        k_quote = 0.0
        if row.base in upbit:
            k_quote += float(upbit[row.base].get("quote_krw") or 0) / usdt_krw
        if row.base in bithumb:
            k_quote += float(bithumb[row.base].get("quote_krw") or 0) / usdt_krw
        t_quote = 0.0
        if row.base in btcturk:
            t_quote += float(btcturk[row.base].get("quote_try") or 0) / usdt_try

        sigs = detect_signals(row, fut, k_quote, t_quote, cfg)
        signals.extend(sigs)
        snapshots.append(
            {
                "symbol": row.symbol,
                "last": row.last,
                "chg_pct": row.chg_pct,
                "binance_vol_usdt": row.quote_vol,
                "korea_vol_usdt": round(k_quote, 2),
                "btcturk_vol_usdt": round(t_quote, 2),
                "futures": fut,
                "signals": [s.kind for s in sigs],
            }
        )
        time.sleep(sleep_f)

    mute = set(settings.get("mute_kinds") or [])
    min_sev = int(settings["min_severity_to_alert"])
    # Prefer multi-confirmed signals when degraded is False
    filtered = [
        s
        for s in signals
        if s.kind not in mute
        and s.severity >= min_sev
        and s.confirmations >= (2 if s.kind in {"KOREA_FOMO", "SHORT_SQUEEZE", "DUMP_RISK"} else 1)
    ]
    filtered.sort(key=lambda s: (-s.severity, -s.confirmations, -s.metrics.get("chg_pct", 0)))

    cooldown_h = float(settings.get("alert_cooldown_hours") or 6)
    alerted_map: dict[str, float] = {
        k: float(ts)
        for k, ts in dict(state.get("alerted_map") or {}).items()
        if time.time() - float(ts) < cooldown_h * 3600
    }
    max_alerts = int(settings.get("max_alerts_per_cycle") or 8)
    fresh: list[Signal] = []
    for sig in filtered:
        key = signal_key(sig)
        if key in alerted_map:
            continue
        fresh.append(sig)
        if len(fresh) >= max_alerts:
            break

    token = env("TELEGRAM_BOT_TOKEN")
    chat = env("TELEGRAM_CHAT_ID")
    if not dry_run and not (token and chat):
        logging.info("Telegram env missing → forcing dry-run")
        dry_run = True

    now = time.time()
    for sig in fresh:
        ok = telegram_send(token or "", chat or "", format_signal(sig), dry_run=dry_run)
        if ok:
            alerted_map[signal_key(sig)] = now
            logging.info("alert %s %s sev=%s conf=%s", sig.kind, sig.symbol, sig.severity, sig.confirmations)
        time.sleep(0.25)

    payload = {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "disclaimer": "Research alerts only. Not financial advice. No auto-trading.",
        "degraded": degraded,
        "dry_run": dry_run,
        "fx": fx,
        "signal_count_raw": len(signals),
        "signal_count_filtered": len(filtered),
        "fresh_alerts": [asdict(s) for s in fresh],
        "all_signals_filtered": [asdict(s) for s in filtered[:50]],
        "snapshots_top": sorted(snapshots, key=lambda x: -abs(x["chg_pct"]))[:30],
    }
    save_json(OUT_PATH, payload)
    state["alerted_map"] = alerted_map
    state["updated_at"] = payload["as_of_utc"]
    save_json(STATE_PATH, state)
    logging.info(
        "done raw=%s filtered=%s fresh=%s → %s",
        len(signals),
        len(filtered),
        len(fresh),
        OUT_PATH,
    )
    return fresh


def main() -> int:
    parser = argparse.ArgumentParser(description="Hardened CEX market radar (alerts only)")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live-telegram", action="store_true")
    parser.add_argument("--health", action="store_true", help="API health check only")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()

    setup_logging()
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        logging.error("config missing: %s", cfg_path)
        return 1
    try:
        cfg = validate_config(load_json(cfg_path))
    except Exception as e:
        logging.error("invalid config: %s", e)
        return 2

    if args.health:
        health = run_health(cfg)
        print(json.dumps(health, indent=2))
        return 0 if health["ok"] else 3

    state = load_json(STATE_PATH) if STATE_PATH.exists() else {}
    dry = True
    if args.live_telegram:
        dry = False
    if args.dry_run or cfg["settings"].get("dry_run_default", True) and not args.live_telegram:
        dry = True

    print("=" * 60)
    print("MARKET RADAR v1.1 — uyarı only / otomatik al-sat YOK")
    print("False-positive azaltıldı; yine de para kaybı riski sende.")
    print("=" * 60)
    logging.info("dry_run=%s", dry)

    poll = int(cfg["settings"]["poll_seconds"])
    failures = 0
    while True:
        try:
            run_cycle(cfg, state, dry_run=dry)
            failures = 0
        except Exception as e:
            failures += 1
            logging.exception("cycle failed (%s): %s", failures, e)
            # exponential backoff on repeated failures; never place orders
            time.sleep(min(300, 15 * failures))
        if args.once:
            break
        time.sleep(poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
