#!/usr/bin/env python3
"""
PRO CEX RADAR — yüksek ikna / az sinyal

Felsefe (garanti yok; yüksek olasılık filtresi):
  CORE (üçü de şart):
    1) Hacim sessiz → 0→+ (5m)
    2) Fiyat erken: 14g dibe yakın + 24s ∈ [-15%, +5%]
    3) ≥3 kaliteli CEX'te aynı hacim uyanışı

  TEYİT (AL için):
    4) 15m higher-low veya kısa direnç kırılımı
    5) RSI(1h) 30–55
    6) BTC rejim düşüş değil
    7) Funding aşırı pozitif değil (varsa)

  🟢 YÜKSEK = CORE + TEYİT
  🟡 ORTA   = sadece CORE
  🔴 GEÇ    = zaten +%12+ / dipten uzak

Borsalar (8 kaliteli): Binance, OKX, Bybit, Bitget, Gate, KuCoin, MEXC, Coinbase

Kullanım:
  python3 pro_cex_radar.py --once --dry-run
  python3 pro_cex_radar.py --backtest
  python3 pro_cex_radar.py --missed
  python3 pro_cex_radar.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import ccxt
except ImportError:
    ccxt = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

QUALITY_CEX = [
    "binance",
    "okx",
    "bybit",
    "bitget",
    "gate",
    "kucoin",
    "mexc",
    "coinbase",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "rest_base": "https://data-api.binance.vision",
    "futures_base": "https://fapi.binance.com",
    "quote": "USDT",
    "workers": 16,
    "poll_seconds": 180,
    "min_quote_volume_usdt": 500_000,
    "max_binance_symbols": 250,
    "cex_ids": QUALITY_CEX,
    "cex_max_symbols": 50,
    "cex_ohlcv_limit": 28,
    "cex_workers": 8,
    "min_cex_confluence": 3,
    "core": {
        "near_low_days": 14,
        "near_low_max_pct": 8.0,
        "min_24h_pct": -15.0,
        "max_24h_pct": 5.0,
        "vol_quiet_bars": 14,
        "vol_turn_mult": 1.7,
        "vol_rise_mult": 1.4,
    },
    "confirm": {
        "rsi_min": 30.0,
        "rsi_max": 55.0,
        "btc_dump_pct": -3.0,
        "max_funding": 0.0003,
    },
    "late": {
        "max_24h_pct": 12.0,
        "max_from_low_pct": 20.0,
    },
    "risk": {
        "stop_buffer_pct": 1.5,
        "tp1_pct": 8.0,
        "tp2_pct": 25.0,
        "account_risk_pct": 1.0,
    },
    "skip_bases": [
        "WBTC", "WETH", "BTCB", "WBETH", "BETH", "RLUSD", "USDC", "FDUSD",
        "TUSD", "DAI", "BUSD", "USDP", "EUR", "U", "USD0", "USDD", "XAUT", "PAXG",
    ],
}

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_NAME = "pro_cex_radar.json"


def find_root() -> Path:
    here = SCRIPT_DIR
    for cand in [here, here.parent, Path.cwd(), Path.cwd().parent]:
        if (cand / "config" / CONFIG_NAME).exists() or (cand / CONFIG_NAME).exists():
            return cand
    return here.parent if here.name.lower() == "output" else here


ROOT = find_root()
OUTPUT_DIR = ROOT / "output"
OUTPUT_PATH = OUTPUT_DIR / "pro_cex_radar_signals.json"
STATE_PATH = OUTPUT_DIR / "pro_cex_radar_state.json"
BACKTEST_PATH = OUTPUT_DIR / "pro_cex_radar_backtest.json"
MISSED_PATH = OUTPUT_DIR / "pro_cex_radar_missed.json"

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "pro-cex-radar/1.0"})


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------

def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) and v.strip() else default


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_config(cli: str | None = None) -> tuple[dict[str, Any], str]:
    cands = []
    if cli:
        cands.append(Path(cli))
    cands += [
        ROOT / "config" / CONFIG_NAME,
        ROOT / CONFIG_NAME,
        SCRIPT_DIR / "config" / CONFIG_NAME,
        Path.cwd() / "config" / CONFIG_NAME,
    ]
    for p in cands:
        if p.is_file():
            raw = load_json(p)
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(raw)
            for k in ("core", "confirm", "late", "risk"):
                if isinstance(raw.get(k), dict):
                    m = dict(DEFAULT_CONFIG[k])
                    m.update(raw[k])
                    cfg[k] = m
            return cfg, str(p)
    print("[uyarı] config yok → gömülü profesyonel varsayılan", file=sys.stderr)
    return dict(DEFAULT_CONFIG), "(embedded)"


def get_json(url: str, params: dict[str, Any] | None = None, timeout: int = 25) -> Any:
    r = HTTP.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def ema(vals: list[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains[-n:]) / n, sum(losses[-n:]) / n
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))


def atr(h: list[float], l: list[float], c: list[float], n: int = 14) -> float | None:
    if len(c) < n + 1:
        return None
    trs = []
    for i in range(1, len(c)):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return sum(trs[-n:]) / n


# ---------------------------------------------------------------------------
# Core detectors
# ---------------------------------------------------------------------------

def vol_zero_to_pos(vols: list[float], quiet_bars: int, turn_mult: float) -> dict[str, Any]:
    n = max(8, quiet_bars)
    if len(vols) < n + 3:
        return {"ok": False, "ratio": 0.0}
    quiet, recent = vols[-(n + 3) : -3], vols[-3:]
    q = sum(quiet) / len(quiet)
    r = sum(recent) / len(recent)
    if q <= 0:
        return {"ok": False, "ratio": 0.0}
    ratio = r / q
    slope = recent[-1] > recent[-2] >= recent[0] * 0.95
    quiet_ok = max(quiet[-5:]) <= q * 1.4
    ok = quiet_ok and slope and ratio >= turn_mult and recent[-1] > q * turn_mult
    return {"ok": ok, "ratio": round(ratio, 2)}


def vol_from_bottom(vols: list[float], mult: float) -> tuple[bool, float]:
    if len(vols) < 8 or vols[-1] <= 0:
        return False, 0.0
    trough = min(v for v in vols[-12:-1] if v > 0) if any(v > 0 for v in vols[-12:-1]) else 0
    if trough <= 0:
        return False, 0.0
    rise = vols[-1] / trough
    return (vols[-1] >= trough * mult and vols[-1] > vols[-2]), rise


@dataclass
class Signal:
    base: str
    symbol: str
    price: float
    change_24h: float
    quote_vol: float
    conviction: str  # YUKSEK | ORTA | GEC | YOK
    score: float
    reasons: list[str] = field(default_factory=list)
    cex_count: int = 0
    cex_list: list[str] = field(default_factory=list)
    from_low_pct: float = 0.0
    vol_ratio: float = 0.0
    rsi_1h: float | None = None
    stop: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    risk_reward: float | None = None
    upside_room_pct: float = 0.0
    layers: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def binance_symbols(cfg: dict[str, Any]) -> list[tuple[str, str, float]]:
    """[(symbol, base, quoteVol)] likit USDT spot."""
    base_url = cfg["rest_base"]
    info = get_json(f"{base_url}/api/v3/exchangeInfo")
    tickers = get_json(f"{base_url}/api/v3/ticker/24hr")
    skip = {s.upper() for s in (cfg.get("skip_bases") or [])}
    usdt = {
        s["symbol"]: s["baseAsset"]
        for s in info.get("symbols", [])
        if s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
        and s.get("isSpotTradingAllowed", True)
        and not str(s.get("baseAsset", "")).endswith(("UP", "DOWN"))
    }
    out: list[tuple[str, str, float]] = []
    min_qv = float(cfg.get("min_quote_volume_usdt") or 0)
    for t in tickers:
        sym = t.get("symbol") or ""
        if sym not in usdt:
            continue
        base = usdt[sym].upper()
        if base in skip:
            continue
        # tokenized stock *B
        if len(base) >= 4 and base.endswith("B") and base not in {"BNB", "BB"} and base[:-1].isalpha():
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
            chg = float(t.get("priceChangePercent") or 0)
            px = float(t.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        if qv < min_qv or px <= 0:
            continue
        out.append((sym, base, qv))
    out.sort(key=lambda x: x[2], reverse=True)
    cap = int(cfg.get("max_binance_symbols") or 250)
    return out[:cap]


def fetch_klines(cfg: dict[str, Any], symbol: str, interval: str, limit: int) -> dict[str, list[float]] | None:
    try:
        rows = get_json(
            f"{cfg['rest_base']}/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        if not isinstance(rows, list) or len(rows) < 12:
            return None
        # drop forming candle
        rows = rows[:-1]
        return {
            "o": [float(r[1]) for r in rows],
            "h": [float(r[2]) for r in rows],
            "l": [float(r[3]) for r in rows],
            "c": [float(r[4]) for r in rows],
            "v": [float(r[5]) for r in rows],
        }
    except Exception:
        return None


def fetch_funding(cfg: dict[str, Any]) -> dict[str, float]:
    try:
        rows = get_json(f"{cfg['futures_base']}/fapi/v1/premiumIndex", timeout=15)
        out = {}
        for r in rows if isinstance(rows, list) else []:
            try:
                out[str(r["symbol"])] = float(r.get("lastFundingRate") or 0)
            except (KeyError, TypeError, ValueError):
                pass
        return out
    except Exception:
        return {}


def btc_ok(cfg: dict[str, Any]) -> tuple[bool, float]:
    try:
        t = get_json(f"{cfg['rest_base']}/api/v3/ticker/24hr", {"symbol": "BTCUSDT"})
        chg = float(t.get("priceChangePercent") or 0)
    except Exception:
        return True, 0.0
    dump = float((cfg.get("confirm") or {}).get("btc_dump_pct") or -3)
    if chg > dump:
        return True, chg
    d1h = fetch_klines(cfg, "BTCUSDT", "1h", 40)
    if d1h:
        e = ema(d1h["c"], 25)
        if e and d1h["c"][-1] < e and chg <= dump:
            return False, chg
    return chg > dump, chg


# ---------------------------------------------------------------------------
# Multi-CEX confluence (quality 8)
# ---------------------------------------------------------------------------

def _norm_base(b: str) -> str:
    b = (b or "").upper()
    return b[4:] if b.startswith("1000") and len(b) > 4 else b


def cex_volume_wakes(cfg: dict[str, Any]) -> dict[str, Any]:
    """base -> [cex,...] hacim uyanışı."""
    ids = list(cfg.get("cex_ids") or QUALITY_CEX)
    max_sym = int(cfg.get("cex_max_symbols") or 50)
    limit = int(cfg.get("cex_ohlcv_limit") or 28)
    workers = int(cfg.get("cex_workers") or 8)
    core = cfg.get("core") or {}
    quiet_bars = int(core.get("vol_quiet_bars") or 14)
    turn_mult = float(core.get("vol_turn_mult") or 1.7)
    rise_mult = float(core.get("vol_rise_mult") or 1.4)

    by_base: dict[str, list[str]] = {}
    scanned: list[str] = []
    errors: list[str] = []

    print(f"[cex] {len(ids)} kaliteli borsa: {', '.join(ids)}", flush=True)

    for ex_id in ids:
        t0 = time.time()
        try:
            wakes = _one_cex_wakes(ex_id, cfg, max_sym, limit, workers, quiet_bars, turn_mult, rise_mult)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{ex_id}:{exc.__class__.__name__}")
            print(f"  ! {ex_id} {exc.__class__.__name__}", flush=True)
            continue
        scanned.append(ex_id)
        for b in wakes:
            by_base.setdefault(b, []).append(ex_id)
        print(f"  → {ex_id}: {len(wakes)} uyanış ({time.time()-t0:.1f}s)", flush=True)

    print(f"[cex] OK {len(scanned)}/{len(ids)} · {len(by_base)} base", flush=True)
    return {"by_base": by_base, "scanned": scanned, "errors": errors, "requested": len(ids)}


def _one_cex_wakes(
    ex_id: str,
    cfg: dict[str, Any],
    max_sym: int,
    limit: int,
    workers: int,
    quiet_bars: int,
    turn_mult: float,
    rise_mult: float,
) -> set[str]:
    if ex_id == "binance":
        return _binance_vision_wakes(cfg, max_sym, limit, workers, quiet_bars, turn_mult, rise_mult)
    if ccxt is None:
        raise RuntimeError("ccxt missing")
    ex = getattr(ccxt, ex_id)({"enableRateLimit": True, "timeout": 20000, "options": {"defaultType": "spot"}})
    markets = ex.load_markets()
    pairs: list[tuple[str, str]] = []
    for sym, m in markets.items():
        if not m.get("active", True) or m.get("spot") is False:
            continue
        if m.get("contract") or m.get("swap"):
            continue
        if str(m.get("quote") or "").upper() not in {"USDT", "USD", "USDC"}:
            continue
        base = _norm_base(str(m.get("base") or ""))
        if base:
            pairs.append((base, sym))
    try:
        tickers = ex.fetch_tickers()
    except Exception:
        tickers = {}
    ranked = []
    for base, sym in pairs:
        t = tickers.get(sym) or {}
        try:
            qv = float(t.get("quoteVolume") or t.get("baseVolume") or 0)
        except (TypeError, ValueError):
            qv = 0.0
        ranked.append((qv, base, sym))
    ranked.sort(reverse=True)
    ranked = ranked[:max_sym]
    wakes: set[str] = set()

    def job(item: tuple[float, str, str]) -> str | None:
        _, base, sym = item
        try:
            rows = ex.fetch_ohlcv(sym, "5m", limit=limit)
        except Exception:
            return None
        vols = [float(r[5]) for r in rows[:-1]]
        vz = vol_zero_to_pos(vols, quiet_bars, turn_mult)
        ok, _ = vol_from_bottom(vols, rise_mult)
        return base if (vz["ok"] or ok) else None

    with ThreadPoolExecutor(max_workers=max(2, workers)) as pool:
        for fut in as_completed([pool.submit(job, r) for r in ranked]):
            try:
                b = fut.result()
            except Exception:
                continue
            if b:
                wakes.add(b)
    return wakes


def _binance_vision_wakes(
    cfg: dict[str, Any],
    max_sym: int,
    limit: int,
    workers: int,
    quiet_bars: int,
    turn_mult: float,
    rise_mult: float,
) -> set[str]:
    ranked = binance_symbols(cfg)[:max_sym]
    wakes: set[str] = set()

    def job(item: tuple[str, str, float]) -> str | None:
        sym, base, _ = item
        d = fetch_klines(cfg, sym, "5m", limit)
        if not d:
            return None
        vz = vol_zero_to_pos(d["v"], quiet_bars, turn_mult)
        ok, _ = vol_from_bottom(d["v"], rise_mult)
        return base if (vz["ok"] or ok) else None

    with ThreadPoolExecutor(max_workers=max(2, workers)) as pool:
        for fut in as_completed([pool.submit(job, r) for r in ranked]):
            try:
                b = fut.result()
            except Exception:
                continue
            if b:
                wakes.add(b)
    return wakes


# ---------------------------------------------------------------------------
# Analyze (Binance deep + confluence)
# ---------------------------------------------------------------------------

def analyze(
    symbol: str,
    base: str,
    ticker: dict[str, Any],
    cfg: dict[str, Any],
    cex_map: dict[str, list[str]],
    funding: dict[str, float],
    btc_friendly: bool,
    btc_chg: float,
) -> Signal | None:
    core = cfg["core"]
    conf = cfg["confirm"]
    late = cfg["late"]
    risk = cfg["risk"]

    try:
        price = float(ticker["lastPrice"])
        chg24 = float(ticker["priceChangePercent"])
        qv = float(ticker["quoteVolume"])
    except (KeyError, TypeError, ValueError):
        return None

    d5 = fetch_klines(cfg, symbol, "5m", 48)
    d15 = fetch_klines(cfg, symbol, "15m", 64)
    d1h = fetch_klines(cfg, symbol, "1h", 72)
    d1d = fetch_klines(cfg, symbol, "1d", 40)
    if not d5 or not d1h or not d1d:
        return None

    # --- CORE 1: volume ---
    vz = vol_zero_to_pos(
        d5["v"],
        int(core["vol_quiet_bars"]),
        float(core["vol_turn_mult"]),
    )
    vb, vrise = vol_from_bottom(d5["v"], float(core["vol_rise_mult"]))
    vol_ok = vz["ok"] or vb

    # --- CORE 2: early price ---
    days = int(core["near_low_days"])
    low = min(d1d["l"][-days:])
    from_low = ((price / low) - 1) * 100 if low > 0 else 999
    early_px = (
        float(core["min_24h_pct"]) <= chg24 <= float(core["max_24h_pct"])
        and from_low <= float(core["near_low_max_pct"])
    )

    # --- CORE 3: CEX confluence ---
    cexes = sorted(set(cex_map.get(base) or []))
    # Binance wake'i kendinden say
    if vol_ok and "binance" not in cexes:
        cexes = sorted(cexes + ["binance"])
    cex_n = len(cexes)
    cex_ok = cex_n >= int(cfg.get("min_cex_confluence") or 3)

    reasons: list[str] = []
    score = 0.0

    # Late reject
    if chg24 >= float(late["max_24h_pct"]) or from_low >= float(late["max_from_low_pct"]):
        return Signal(
            base=base,
            symbol=symbol,
            price=price,
            change_24h=chg24,
            quote_vol=qv,
            conviction="GEC",
            score=0,
            reasons=[f"GEC_24s%{chg24:+.1f}", f"dip+{from_low:.1f}%"],
            cex_count=cex_n,
            cex_list=cexes,
            from_low_pct=round(from_low, 2),
            vol_ratio=vz["ratio"],
        )

    if not vol_ok:
        return None  # core fail → sessizce atla (gürültü yok)
    reasons.append(f"HACIM_0→+×{vz['ratio'] or round(vrise, 2)}")
    score += 30

    if not early_px:
        # erken değilse yüksek ikna yok
        if chg24 > float(core["max_24h_pct"]):
            return Signal(
                base=base, symbol=symbol, price=price, change_24h=chg24, quote_vol=qv,
                conviction="GEC", score=10, reasons=["FIYAT_KACMIS"] + reasons,
                cex_count=cex_n, cex_list=cexes, from_low_pct=round(from_low, 2), vol_ratio=vz["ratio"],
            )
        return None

    reasons.append(f"ERKEN_FIYAT(%{chg24:+.1f})")
    reasons.append(f"DIP_YAKIN(%{from_low:.1f})")
    score += 30

    if not cex_ok:
        # tek borsa → yayınlama (profesyonel filtre: gürültüyü kes)
        return None
    reasons.append(f"CEX×{cex_n}[{','.join(cexes)}]")
    score += 10 + min(20, cex_n * 4)

    # --- CONFIRM ---
    confirms = 0
    rsi_1h = rsi(d1h["c"], 14)
    if rsi_1h is not None and float(conf["rsi_min"]) <= rsi_1h <= float(conf["rsi_max"]):
        confirms += 1
        score += 12
        reasons.append(f"RSI_OK({rsi_1h:.0f})")
    elif rsi_1h is not None and rsi_1h > 60:
        score -= 10
        reasons.append(f"RSI_SICAK({rsi_1h:.0f})")

    # higher-low / micro break 15m
    hl = False
    brk = False
    if d15 and len(d15["l"]) >= 20:
        hl = min(d15["l"][-6:]) > min(d15["l"][-18:-6]) * 1.001
        resist = max(d15["h"][-12:-2])
        brk = d15["c"][-1] > resist and d15["v"][-1] > sum(d15["v"][-12:-1]) / 11
    if hl:
        confirms += 1
        score += 10
        reasons.append("HIGHER_LOW")
    if brk:
        confirms += 1
        score += 10
        reasons.append("15m_KIRILIM")

    if btc_friendly:
        confirms += 1
        score += 6
        reasons.append(f"BTC_OK(%{btc_chg:+.1f})")
    else:
        score -= 15
        reasons.append("BTC_DUSUS_REJIM")

    fr = funding.get(symbol)
    if fr is not None:
        if fr <= 0:
            confirms += 1
            score += 6
            reasons.append(f"FUNDING_NEG({fr:.4%})")
        elif fr > float(conf["max_funding"]):
            score -= 8
            reasons.append(f"FUNDING_SICAK({fr:.4%})")

    # room to 30d high
    hi30 = max(d1d["h"][-30:]) if len(d1d["h"]) >= 30 else max(d1d["h"])
    room = ((hi30 / price) - 1) * 100 if price > 0 else 0
    if room >= 40:
        score += 8
        reasons.append(f"UC_ALANI(+%{room:.0f})")

    # risk
    swing = min(d1h["l"][-12:])
    atr_v = atr(d1h["h"], d1h["l"], d1h["c"], 14)
    buf = float(risk["stop_buffer_pct"]) / 100
    stop = min(swing * (1 - buf), price * (1 - buf))
    if atr_v:
        stop = min(stop, price - 1.2 * atr_v)
    stop = max(stop, price * 0.88)
    tp1 = price * (1 + float(risk["tp1_pct"]) / 100)
    tp2_pct = max(float(risk["tp2_pct"]), min(60.0, room * 0.7)) if room > 20 else float(risk["tp2_pct"])
    tp2 = price * (1 + tp2_pct / 100)
    rr = ((tp1 - price) / (price - stop)) if price > stop else 0

    score = max(0, min(100, score))

    # Conviction
    if btc_friendly and confirms >= 3 and score >= 70 and cex_n >= int(cfg["min_cex_confluence"]):
        conv = "YUKSEK"
    elif confirms >= 2 and score >= 58:
        conv = "ORTA"
    else:
        conv = "ORTA" if score >= 50 else "YOK"

    if conv == "YOK":
        return None

    return Signal(
        base=base,
        symbol=symbol,
        price=price,
        change_24h=chg24,
        quote_vol=qv,
        conviction=conv,
        score=round(score, 1),
        reasons=reasons,
        cex_count=cex_n,
        cex_list=cexes,
        from_low_pct=round(from_low, 2),
        vol_ratio=float(vz["ratio"] or round(vrise, 2)),
        rsi_1h=round(rsi_1h, 1) if rsi_1h is not None else None,
        stop=round(stop, 8),
        tp1=round(tp1, 8),
        tp2=round(tp2, 8),
        risk_reward=round(rr, 2),
        upside_room_pct=round(room, 1),
        layers={
            "confirms": confirms,
            "btc_chg": btc_chg,
            "funding": fr,
            "account_risk_pct": risk.get("account_risk_pct"),
        },
    )


# ---------------------------------------------------------------------------
# Scan / report
# ---------------------------------------------------------------------------

def run_scan(cfg: dict[str, Any]) -> tuple[list[Signal], dict[str, Any]]:
    print("[1/4] Binance likit evren…", flush=True)
    ranked = binance_symbols(cfg)
    tickers = {t["symbol"]: t for t in get_json(f"{cfg['rest_base']}/api/v3/ticker/24hr")}
    print(f"  → {len(ranked)} sembol")

    print("[2/4] BTC rejim + funding…", flush=True)
    btc_friendly, btc_chg = btc_ok(cfg)
    funding = fetch_funding(cfg)
    print(f"  BTC %{btc_chg:+.2f} · {'OK' if btc_friendly else 'DÜŞÜŞ'} · funding={len(funding)}")

    print("[3/4] 8 CEX confluence…", flush=True)
    cex = cex_volume_wakes(cfg)

    print("[4/4] derin analiz (sadece confluence adayları)…", flush=True)
    # Sadece CEX map'te olan veya Binance wake potansiyeli yüksek olanları derin incele
    hot_bases = set(cex["by_base"])
    # ayrıca Binance top hacimde erken olanları da dene
    targets = [(s, b, q) for s, b, q in ranked if b in hot_bases]
    # hot boşsa (ağ hatası) yine de top 80 tara
    if len(targets) < 15:
        targets = ranked[:80]
        print("  [uyarı] az confluence — top 80 fallback")

    meta = {
        "btc_chg": btc_chg,
        "btc_ok": btc_friendly,
        "cex": {"scanned": cex["scanned"], "requested": cex["requested"], "errors": cex["errors"]},
    }
    results: list[Signal] = []
    workers = int(cfg.get("workers") or 16)

    def job(item: tuple[str, str, float]) -> Signal | None:
        sym, base, _ = item
        t = tickers.get(sym)
        if not t:
            return None
        return analyze(sym, base, t, cfg, cex["by_base"], funding, btc_friendly, btc_chg)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(job, it) for it in targets]
        done = 0
        for fut in as_completed(futs):
            done += 1
            if done % 40 == 0:
                print(f"  … {done}/{len(targets)}")
            try:
                sig = fut.result()
            except Exception:
                continue
            if sig:
                results.append(sig)

    order = {"YUKSEK": 0, "ORTA": 1, "GEC": 2, "YOK": 3}
    results.sort(key=lambda s: (order.get(s.conviction, 9), -s.score, -s.cex_count))
    print(f"  → {sum(1 for s in results if s.conviction=='YUKSEK')} YÜKSEK · "
          f"{sum(1 for s in results if s.conviction=='ORTA')} ORTA · "
          f"{sum(1 for s in results if s.conviction=='GEC')} GEÇ")
    return results, meta


def format_report(rows: list[Signal], meta: dict[str, Any], top: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hi = [r for r in rows if r.conviction == "YUKSEK"]
    mid = [r for r in rows if r.conviction == "ORTA"]
    late = [r for r in rows if r.conviction == "GEC"]
    lines = [
        f"PRO CEX RADAR · {now}",
        "Kural: hacim0→+  +  erken fiyat  +  ≥3 CEX  +  teyit",
        f"BTC %{meta.get('btc_chg', 0):+.2f} · "
        f"CEX {(meta.get('cex') or {}).get('scanned')}",
        f"🟢YÜKSEK={len(hi)} 🟡ORTA={len(mid)} 🔴GEÇ={len(late)}",
        "",
    ]
    if hi:
        lines.append("═══ 🟢 YÜKSEK İKNA (AL adayı) ═══")
        for i, r in enumerate(hi[:top], 1):
            lines.append(
                f"{i:2d}. {r.base:<8} skor={r.score:5.1f}  "
                f"%{r.change_24h:+.1f}  CEX×{r.cex_count}  "
                f"dip+{r.from_low_pct}%  RSI{r.rsi_1h}  "
                f"SL {r.stop} TP1 {r.tp1} TP2 {r.tp2} RR{r.risk_reward}"
            )
            lines.append(f"    {', '.join(r.reasons[:6])}")
        lines.append("")
    else:
        lines.append("🟢 YÜKSEK yok — bugün zorla AL yok (doğru davranış)")
        lines.append("")

    if mid:
        lines.append("── 🟡 ORTA (izle / teyit bekle) ──")
        for i, r in enumerate(mid[: min(8, top)], 1):
            lines.append(
                f"{i:2d}. {r.base:<8} skor={r.score:5.1f}  %{r.change_24h:+.1f}  "
                f"CEX×{r.cex_count}  | {', '.join(r.reasons[:4])}"
            )
        lines.append("")

    hot = sorted(late, key=lambda x: -x.change_24h)[:5]
    if hot:
        lines.append("── 🔴 GEÇ (kaçmış — ders) ──")
        for r in hot:
            lines.append(f"   {r.base:<8} %{r.change_24h:+.1f}")
    return "\n".join(lines)


def format_telegram(hi: list[Signal], meta: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not hi:
        return f"<b>PRO RADAR</b>\n🟢 YÜKSEK yok.\n<i>{now}</i>"
    lines = [f"<b>🟢 PRO CEX RADAR · {len(hi)} YÜKSEK</b>", f"<i>{now}</i>", ""]
    for r in hi[:6]:
        lines.append(
            f"<b>{r.base}</b> skor {r.score:.0f} · CEX×{r.cex_count}\n"
            f"%{r.change_24h:+.1f} · <code>{r.price}</code>\n"
            f"🛑 <code>{r.stop}</code> 🎯 <code>{r.tp1}</code> / <code>{r.tp2}</code> RR{r.risk_reward}\n"
            f"Dip+{r.from_low_pct}% · RSI {r.rsi_1h} · Vol×{r.vol_ratio}\n"
            f"{', '.join(r.reasons[:5])}\n"
        )
    return "\n".join(lines)


def telegram_send(token: str, chat_id: str, text: str, dry: bool) -> bool:
    if dry:
        print("--- DRY-RUN TELEGRAM ---\n" + text + "\n------------------------")
        return True
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30,
        )
        return r.status_code == 200
    except requests.RequestException as exc:
        print(f"[telegram] {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Backtest + missed logger
# ---------------------------------------------------------------------------

def run_backtest(cfg: dict[str, Any], n: int = 30, hold: int = 12) -> dict[str, Any]:
    """CORE kurallarının 1h üzerinde ileri getirisi."""
    print(f"[backtest] n={n} hold={hold}h")
    ranked = binance_symbols(cfg)[:n]
    core = cfg["core"]
    trades = []
    for sym, base, _ in ranked:
        d = fetch_klines(cfg, sym, "1h", 220)
        if not d or len(d["c"]) < 80:
            continue
        for i in range(48, len(d["c"]) - hold - 1):
            vols = d["v"][: i + 1]
            closes = d["c"][: i + 1]
            lows = d["l"][: i + 1]
            px = closes[-1]
            low = min(lows[- min(14 * 24, len(lows)) :])
            from_low = ((px / low) - 1) * 100
            if from_low > float(core["near_low_max_pct"]):
                continue
            vz = vol_zero_to_pos(vols[-40:], int(core["vol_quiet_bars"]), float(core["vol_turn_mult"]))
            if not vz["ok"]:
                continue
            r_1h = rsi(closes, 14)
            if r_1h is None or not (30 <= r_1h <= 55):
                continue
            if len(closes) < 3:
                continue
            if not ((closes[-2] - closes[-3]) / closes[-3] <= 0 and (closes[-1] - closes[-2]) / closes[-2] > 0):
                continue
            ret = (d["c"][i + hold] / px - 1) * 100
            trades.append({"base": base, "ret": round(ret, 3), "from_low": round(from_low, 2)})
        time.sleep(0.04)

    if not trades:
        summary = {"trades": 0, "winrate": 0, "avg": 0, "median": 0}
    else:
        rets = sorted(t["ret"] for t in trades)
        summary = {
            "trades": len(trades),
            "winrate": round(100 * sum(1 for r in rets if r > 0) / len(rets), 1),
            "avg": round(sum(rets) / len(rets), 3),
            "median": rets[len(rets) // 2],
            "best": rets[-1],
            "worst": rets[0],
            "hold_h": hold,
        }
    out = {"updated_at": datetime.now(timezone.utc).isoformat(), "summary": summary, "sample": trades[:25]}
    save_json(BACKTEST_PATH, out)
    print(json.dumps(summary, indent=2))
    return out


def run_missed(cfg: dict[str, Any]) -> dict[str, Any]:
    """Son 24s +%20 üstü coinler — radar erken yakalar mıydı? (ders logu)."""
    print("[missed] 24s şişenleri incele…")
    tickers = get_json(f"{cfg['rest_base']}/api/v3/ticker/24hr")
    big = []
    for t in tickers:
        try:
            chg = float(t["priceChangePercent"])
            if chg < 20 or not str(t["symbol"]).endswith("USDT"):
                continue
            big.append((chg, t["symbol"], float(t["lastPrice"])))
        except (KeyError, TypeError, ValueError):
            continue
    big.sort(reverse=True)
    lessons = []
    for chg, sym, px in big[:25]:
        d1d = fetch_klines(cfg, sym, "1d", 20)
        d5 = fetch_klines(cfg, sym, "5m", 60)
        note = "veri_yok"
        if d1d and d5:
            # 24s önce kabaca: fiyat şimdi / (1+chg/100)
            approx_early = px / (1 + chg / 100)
            low14 = min(d1d["l"][-14:])
            from_low_then = ((approx_early / low14) - 1) * 100 if low14 else 999
            # hacim uyanışı var mıydı son 12 saatte?
            woke = False
            for i in range(20, len(d5["v"]) - 5):
                vz = vol_zero_to_pos(d5["v"][i - 18 : i + 1], 14, 1.7)
                if vz["ok"]:
                    woke = True
                    break
            if from_low_then <= 10 and woke:
                note = "YAKALANABILIRDI_erken_setup"
            elif woke:
                note = "hacim_vardi_fiyat_uzakta"
            else:
                note = "ani_gap_veya_haber"
        lessons.append({"symbol": sym, "chg_24h": chg, "note": note})
        time.sleep(0.05)
    out = {"updated_at": datetime.now(timezone.utc).isoformat(), "lessons": lessons}
    save_json(MISSED_PATH, out)
    for L in lessons[:12]:
        print(f"  {L['symbol']:<12} %{L['chg_24h']:+.1f}  → {L['note']}")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="PRO CEX Radar — yüksek ikna")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--config", default=None)
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--missed", action="store_true")
    ap.add_argument("--fast", action="store_true", help="CEX başına az sembol")
    args = ap.parse_args()

    cfg, src = load_config(args.config)
    if args.fast:
        cfg["cex_max_symbols"] = 30
        cfg["max_binance_symbols"] = 120
    print(f"[config] {src}")
    print(f"[cex] {cfg.get('cex_ids')}")

    if args.backtest:
        run_backtest(cfg)
        return 0
    if args.missed:
        run_missed(cfg)
        return 0

    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    dry = args.dry_run or args.no_telegram or not (token and chat)
    poll = int(cfg.get("poll_seconds") or 180)
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"alerted": {}}

    while True:
        t0 = time.time()
        rows, meta = run_scan(cfg)
        print("\n" + format_report(rows, meta, args.top) + "\n")
        save_json(
            OUTPUT_PATH,
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "elapsed": round(time.time() - t0, 1),
                "meta": meta,
                "signals": [asdict(r) for r in rows if r.conviction != "YOK"][:60],
            },
        )

        hi = [r for r in rows if r.conviction == "YUKSEK"]
        # cooldown
        alerted = state.setdefault("alerted", {})
        fresh = []
        now = datetime.now(timezone.utc)
        for r in hi:
            prev = alerted.get(r.base)
            if prev:
                try:
                    if (now - datetime.fromisoformat(prev)).total_seconds() < 1800:
                        continue
                except ValueError:
                    pass
            fresh.append(r)
        if fresh and not args.no_telegram:
            if telegram_send(token or "", chat or "", format_telegram(fresh, meta), dry):
                ts = now.isoformat()
                for r in fresh:
                    alerted[r.base] = ts
                save_json(STATE_PATH, state)

        if args.once:
            break
        print(f"[sleep] {poll}s")
        time.sleep(poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
