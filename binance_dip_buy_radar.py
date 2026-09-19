#!/usr/bin/env python3
"""
Binance TÜM USDT spot — dipten erken AL radarı (gelişmiş v2).

Katmanlar:
  1) Teknik dip (14g)
  2) 5m hacim dipten yükseliş
  3) 0→+ / yeşil mum / higher-low
  4) RSI toparlanma
  5) EMA dönüş
  6) BTC relative strength + BTC rejim filtresi
  7) Futures funding / OI (varsa)
  8) Stop / TP risk önerisi
  9) Geç kalma filtresi (AR +%50 tipi → GEÇ)

Kullanım:
  python3 binance_dip_buy_radar.py --once --dry-run
  python3 binance_dip_buy_radar.py --once --top 20
  python3 binance_dip_buy_radar.py --backtest --backtest-symbols 40
  python3 binance_dip_buy_radar.py
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

DEFAULT_CONFIG: dict[str, Any] = {
    "rest_base": "https://data-api.binance.vision",
    "futures_base": "https://fapi.binance.com",
    "quote": "USDT",
    "workers": 20,
    "poll_seconds": 180,
    "max_symbols": 0,
    "min_quote_volume_usdt": 200000,
    "ohlcv": {"5m": 48, "15m": 96, "1h": 72, "1d": 90},
    "early_buy": {
        "max_24h_change_pct": 8.0,
        "min_24h_change_pct": -25.0,
        "near_low_lookback_days": 14,
        "near_low_max_pct": 8.0,
        "max_rsi_1h": 55.0,
        "min_rsi_1h": 25.0,
        "volume_rise_mult_5m": 1.4,
        "min_score_al": 62,
        "min_score_izle": 48,
    },
    "late_reject": {
        "max_already_up_24h_pct": 15.0,
        "max_rsi_1h": 70.0,
        "max_from_14d_low_pct": 25.0,
    },
    "regime": {
        "enabled": True,
        "btc_dump_pct": -3.0,
        "block_al_on_btc_dump": True,
        "soft_penalty": 12,
    },
    "futures": {
        "enabled": True,
        "neg_funding_bonus": 6,
        "funding_threshold": -0.0001,
        "oi_rise_bonus": 5,
    },
    "risk": {
        "stop_buffer_pct": 1.2,
        "tp1_pct": 6.0,
        "tp2_pct": 14.0,
    },
    "stable_bases": [
        "USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDP", "EUR", "AEUR",
        "USD1", "BFUSD", "RLUSD", "USDE", "XUSD", "U", "USD0", "USDD",
    ],
    "skip_bases": [
        "WBTC", "WETH", "BTCB", "WBETH", "BETH", "STETH", "WSTETH", "RLUSD", "XAUT", "PAXG",
    ],
    "skip_suffixes": ["UP", "DOWN", "BULL", "BEAR"],
    "skip_tokenized_stocks": True,
}

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_NAME = "binance_dip_buy_radar.json"


def find_project_root() -> Path:
    here = SCRIPT_DIR
    for cand in [here, here.parent, Path.cwd(), Path.cwd().parent]:
        if (cand / "config" / CONFIG_NAME).exists() or (cand / CONFIG_NAME).exists():
            return cand
    if here.name.lower() == "output":
        return here.parent
    return here


ROOT = find_project_root()
CONFIG_PATH = ROOT / "config" / CONFIG_NAME
OUTPUT_DIR = ROOT / "output"
OUTPUT_PATH = OUTPUT_DIR / "binance_dip_buy_signals.json"
STATE_PATH = OUTPUT_DIR / "binance_dip_buy_state.json"
BACKTEST_PATH = OUTPUT_DIR / "binance_dip_buy_backtest.json"

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "binance-dip-buy-radar/2.0"})


def resolve_config_path(cli_path: str | None = None) -> Path | None:
    candidates: list[Path] = []
    if cli_path:
        candidates.append(Path(cli_path))
    candidates.extend(
        [
            CONFIG_PATH,
            ROOT / CONFIG_NAME,
            SCRIPT_DIR / "config" / CONFIG_NAME,
            SCRIPT_DIR / CONFIG_NAME,
            SCRIPT_DIR.parent / "config" / CONFIG_NAME,
            Path.cwd() / "config" / CONFIG_NAME,
            Path.cwd() / CONFIG_NAME,
        ]
    )
    seen: set[str] = set()
    for p in candidates:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        if p.is_file():
            return p
    return None


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) and v.strip() else default


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_config(cli_path: str | None = None) -> tuple[dict[str, Any], str]:
    path = resolve_config_path(cli_path)
    if path is not None:
        raw = load_json(path)
        # gömülü defaults ile birleştir (eksik anahtarlar)
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(raw)
        for k in ("early_buy", "late_reject", "regime", "futures", "risk", "ohlcv"):
            if isinstance(raw.get(k), dict):
                merged = dict(DEFAULT_CONFIG.get(k) or {})
                merged.update(raw[k])
                cfg[k] = merged
        return cfg, str(path)
    print(
        "[uyarı] config/binance_dip_buy_radar.json bulunamadı → gömülü varsayılan",
        file=sys.stderr,
    )
    return dict(DEFAULT_CONFIG), "(embedded-default)"


def get_json(url: str, params: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    r = HTTP.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


@dataclass
class Analysis:
    symbol: str
    base: str
    price: float
    change_24h_pct: float
    quote_volume_24h: float
    score: float
    action: str
    phase: str
    reasons: list[str] = field(default_factory=list)
    layers: dict[str, Any] = field(default_factory=dict)
    stop: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    risk_reward: float | None = None


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
    return e


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs[-period:]) / period


def parse_klines(rows: list[list[Any]]) -> dict[str, list[float]]:
    return {
        "o": [float(r[1]) for r in rows],
        "h": [float(r[2]) for r in rows],
        "l": [float(r[3]) for r in rows],
        "c": [float(r[4]) for r in rows],
        "v": [float(r[5]) for r in rows],
        "qv": [float(r[7]) for r in rows],
    }


def closed_slice(d: dict[str, list[float]]) -> dict[str, list[float]]:
    if len(d["c"]) < 3:
        return d
    return {k: v[:-1] for k, v in d.items()}


def is_tokenized_stock(base: str) -> bool:
    b = base.upper()
    if b in {"BNB", "BB", "OMNI"}:
        return False
    return len(b) >= 4 and b.endswith("B") and b[:-1].isalpha()


def list_usdt_symbols(cfg: dict[str, Any]) -> list[str]:
    info = get_json(f"{cfg['rest_base']}/api/v3/exchangeInfo")
    quote = cfg.get("quote") or "USDT"
    stables = {s.upper() for s in (cfg.get("stable_bases") or [])}
    skip_bases = {s.upper() for s in (cfg.get("skip_bases") or [])}
    skip_suf = tuple(cfg.get("skip_suffixes") or [])
    skip_stocks = bool(cfg.get("skip_tokenized_stocks", True))
    out: list[str] = []
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("quoteAsset") != quote:
            continue
        if not s.get("isSpotTradingAllowed", True):
            continue
        b = str(s.get("baseAsset") or "").upper()
        if b in stables or b in skip_bases:
            continue
        if any(b.endswith(suf) for suf in skip_suf):
            continue
        if skip_stocks and is_tokenized_stock(b):
            continue
        sym = str(s["symbol"])
        if any(sym.endswith(f"{suf}{quote}") for suf in skip_suf):
            continue
        out.append(sym)
    return out


def fetch_tickers(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = get_json(f"{cfg['rest_base']}/api/v3/ticker/24hr")
    return {r["symbol"]: r for r in rows if "symbol" in r}


def fetch_ohlcv(cfg: dict[str, Any], symbol: str, interval: str, limit: int) -> dict[str, list[float]] | None:
    try:
        rows = get_json(
            f"{cfg['rest_base']}/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
            timeout=25,
        )
        if not isinstance(rows, list) or len(rows) < 10:
            return None
        return parse_klines(rows)
    except Exception:  # noqa: BLE001
        return None


def fetch_funding_map(cfg: dict[str, Any]) -> dict[str, float]:
    """symbol -> lastFundingRate. Geo-block'ta boş döner."""
    if not (cfg.get("futures") or {}).get("enabled", True):
        return {}
    base = cfg.get("futures_base") or "https://fapi.binance.com"
    try:
        rows = get_json(f"{base}/fapi/v1/premiumIndex", timeout=20)
        out: dict[str, float] = {}
        if not isinstance(rows, list):
            return {}
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            try:
                out[str(sym)] = float(r.get("lastFundingRate") or 0)
            except (TypeError, ValueError):
                continue
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"[futures] funding alınamadı ({exc.__class__.__name__}) — atlandı", file=sys.stderr)
        return {}


def fetch_open_interest(cfg: dict[str, Any], symbol: str) -> float | None:
    base = cfg.get("futures_base") or "https://fapi.binance.com"
    try:
        row = get_json(f"{base}/fapi/v1/openInterest", {"symbol": symbol}, timeout=15)
        return float(row.get("openInterest") or 0)
    except Exception:  # noqa: BLE001
        return None


def analyze_volume_bottom(vols: list[float], mult: float) -> tuple[bool, float]:
    if len(vols) < 8:
        return False, 0.0
    last, prev, prev2 = vols[-1], vols[-2], vols[-3]
    if last <= 0:
        return False, 0.0
    trough = min(vols[-12:-1]) if len(vols) >= 12 else min(vols[:-1])
    if trough <= 0:
        avg = sum(vols[-12:-1]) / max(1, len(vols[-12:-1]))
        trough = max(avg * 0.25, last * 0.05)
        if trough <= 0:
            return False, 0.0
    rise = last / trough
    chain = last > prev >= prev2 * 0.95
    ok = (last >= trough * mult and last > prev) or (
        chain and last >= trough * 1.2 and rise >= 1.15
    )
    return ok, rise


def btc_regime(cfg: dict[str, Any], tickers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """BTC dump ise AL'yi yumuşat / engelle."""
    reg = cfg.get("regime") or {}
    btc = tickers.get("BTCUSDT") or {}
    try:
        chg = float(btc.get("priceChangePercent") or 0)
    except (TypeError, ValueError):
        chg = 0.0
    dump_th = float(reg.get("btc_dump_pct") or -3.0)
    is_dump = chg <= dump_th
    # EMA teyidi
    d1h = fetch_ohlcv(cfg, "BTCUSDT", "1h", 48)
    below_ema = False
    if d1h:
        c = closed_slice(d1h)
        e25 = ema(c["c"], 25)
        if e25 and c["c"][-1] < e25:
            below_ema = True
    hostile = bool(reg.get("enabled", True)) and is_dump and below_ema
    return {
        "btc_change_24h": chg,
        "btc_dump": is_dump,
        "btc_below_ema25": below_ema,
        "hostile": hostile,
        "block_al": hostile and bool(reg.get("block_al_on_btc_dump", True)),
        "penalty": float(reg.get("soft_penalty") or 12),
    }


def calc_risk_levels(
    price: float,
    swing_low: float,
    atr_v: float | None,
    cfg: dict[str, Any],
) -> tuple[float, float, float, float]:
    risk = cfg.get("risk") or {}
    buf = float(risk.get("stop_buffer_pct") or 1.2) / 100.0
    tp1p = float(risk.get("tp1_pct") or 6.0) / 100.0
    tp2p = float(risk.get("tp2_pct") or 14.0) / 100.0
    stop = min(swing_low * (1.0 - buf), price * (1.0 - buf))
    if atr_v and atr_v > 0:
        stop = min(stop, price - 1.2 * atr_v)
    stop = max(stop, price * 0.85)  # aşırı geniş stop engeli
    tp1 = price * (1.0 + tp1p)
    tp2 = price * (1.0 + tp2p)
    risk_amt = price - stop
    rr = ((tp1 - price) / risk_amt) if risk_amt > 0 else 0.0
    return round(stop, 8), round(tp1, 8), round(tp2, 8), round(rr, 2)


def analyze_symbol(
    symbol: str,
    ticker: dict[str, Any],
    cfg: dict[str, Any],
    regime: dict[str, Any],
    funding_map: dict[str, float],
) -> Analysis | None:
    early = cfg.get("early_buy") or {}
    late = cfg.get("late_reject") or {}
    fut = cfg.get("futures") or {}
    ohlcv_cfg = cfg.get("ohlcv") or {}
    btc_change_24h = float(regime.get("btc_change_24h") or 0)

    try:
        price = float(ticker.get("lastPrice") or 0)
        chg24 = float(ticker.get("priceChangePercent") or 0)
        qv24 = float(ticker.get("quoteVolume") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None

    base = symbol.replace(cfg.get("quote") or "USDT", "")
    min_qv = float(cfg.get("min_quote_volume_usdt") or 0)
    if min_qv and qv24 < min_qv:
        return None

    already_late = (
        chg24 >= float(late.get("max_already_up_24h_pct") or 15)
        or chg24 >= float(early.get("max_24h_change_pct") or 8) + 7
    )

    d5 = fetch_ohlcv(cfg, symbol, "5m", int(ohlcv_cfg.get("5m") or 48))
    d15 = fetch_ohlcv(cfg, symbol, "15m", int(ohlcv_cfg.get("15m") or 96))
    d1h = fetch_ohlcv(cfg, symbol, "1h", int(ohlcv_cfg.get("1h") or 72))
    d1d = fetch_ohlcv(cfg, symbol, "1d", int(ohlcv_cfg.get("1d") or 90))
    if not d5 or not d1h or not d1d:
        return None

    c5 = closed_slice(d5)
    c15 = closed_slice(d15) if d15 else None
    c1h = closed_slice(d1h)
    c1d = closed_slice(d1d)

    score = 0.0
    reasons: list[str] = []
    layers: dict[str, Any] = {"change_24h_pct": chg24, "quote_volume_24h": qv24}

    # 1) Dip
    lookback_d = int(early.get("near_low_lookback_days") or 14)
    day_lows = c1d["l"][-lookback_d:] if len(c1d["l"]) >= lookback_d else c1d["l"]
    day_low = min(day_lows) if day_lows else price
    from_low_pct = ((price / day_low) - 1.0) * 100.0 if day_low > 0 else 999.0
    near_max = float(early.get("near_low_max_pct") or 8.0)
    layers["from_nd_low_pct"] = round(from_low_pct, 2)

    if from_low_pct <= near_max:
        score += 18
        reasons.append(f"DIP_YAKIN_{lookback_d}g(%{from_low_pct:.1f})")
    elif from_low_pct <= near_max * 1.5:
        score += 10
        reasons.append(f"DIP_BOLGE(%{from_low_pct:.1f})")
    elif from_low_pct <= float(late.get("max_from_14d_low_pct") or 25):
        score += 3
    else:
        score -= 12
        reasons.append(f"DIP_UZAK(%{from_low_pct:.1f})")

    # 2) Hacim
    vol_ok, vol_rise = analyze_volume_bottom(
        c5["v"], float(early.get("volume_rise_mult_5m") or 1.4)
    )
    layers["vol_rise_5m"] = round(vol_rise, 2)
    if vol_ok:
        score += 16
        reasons.append(f"HACIM_DIP×{vol_rise:.2f}")
        if vol_rise >= 2.5:
            score += 4
            reasons.append("HACIM_PATLAMA")

    if len(c1d["v"]) >= 20:
        avg30 = sum(c1d["v"][-30:]) / min(30, len(c1d["v"]))
        vol_vs_30 = c1d["v"][-1] / avg30 if avg30 > 0 else 0
        layers["vol_vs_30d"] = round(vol_vs_30, 2)
        if 1.2 <= vol_vs_30 <= 4.0:
            score += 8
            reasons.append(f"HACIM_UYANIS×{vol_vs_30:.1f}")
        elif vol_vs_30 > 4.0 and chg24 > 10:
            score -= 6
            reasons.append("HACIM_COK_GEC")

    # 3) Momentum
    closes5, opens5 = c5["c"], c5["o"]
    if len(closes5) >= 3:
        ret_prev = (closes5[-2] - closes5[-3]) / closes5[-3] if closes5[-3] else 0
        ret_now = (closes5[-1] - closes5[-2]) / closes5[-2] if closes5[-2] else 0
        layers["ret_5m_pct"] = round(ret_now * 100, 3)
        if ret_prev <= 0 and ret_now > 0:
            score += 12
            reasons.append("0→+")
        if closes5[-1] > opens5[-1] and closes5[-2] <= opens5[-2]:
            score += 6
            reasons.append("YESIL_MUM")

    if c15 and len(c15["l"]) >= 20:
        recent = min(c15["l"][-6:])
        prior = min(c15["l"][-18:-6])
        if recent > prior * 1.001 and closes5[-1] > recent:
            score += 8
            reasons.append("HIGHER_LOW")
            layers["higher_low"] = True

    # 4) RSI
    rsi_1h = rsi(c1h["c"], 14)
    rsi_1d = rsi(c1d["c"], 14)
    layers["rsi_1h"] = round(rsi_1h, 1) if rsi_1h is not None else None
    layers["rsi_1d"] = round(rsi_1d, 1) if rsi_1d is not None else None
    min_rsi = float(early.get("min_rsi_1h") or 25)
    max_rsi = float(early.get("max_rsi_1h") or 55)
    if rsi_1h is not None:
        if min_rsi <= rsi_1h <= max_rsi:
            score += 12
            reasons.append(f"RSI_TOPARLANMA({rsi_1h:.0f})")
        elif rsi_1h < min_rsi:
            score += 4
            reasons.append(f"RSI_ASIRI_SATIM({rsi_1h:.0f})")
        elif rsi_1h >= float(late.get("max_rsi_1h") or 70):
            score -= 15
            reasons.append(f"RSI_ASIRI_ALIM({rsi_1h:.0f})")
            already_late = True

    # 5) EMA
    ema7 = ema(c1h["c"], 7)
    ema25 = ema(c1h["c"], 25)
    layers["ema7_1h"] = round(ema7, 6) if ema7 else None
    layers["ema25_1h"] = round(ema25, 6) if ema25 else None
    if ema7 and ema25:
        if price > ema7 > ema25:
            if chg24 < 5 and from_low_pct <= 12:
                score += 6
                reasons.append("EMA_STACK_ERKEN")
            else:
                score -= 4
                reasons.append("EMA_STACK_GEC")
        elif price > ema7 and ema7 < ema25:
            score += 10
            reasons.append("EMA7_KIRILIM")

    # 6) RS + BTC rejim
    rel = chg24 - btc_change_24h
    layers["rel_vs_btc_pct"] = round(rel, 2)
    if -2 <= chg24 <= 8 and rel > 1.5:
        score += 8
        reasons.append(f"RS_BTC(+{rel:.1f})")
    elif chg24 < -3 and rel > 0:
        score += 5
        reasons.append("BTC_ALTI_GUC")

    if regime.get("hostile"):
        score -= float(regime.get("penalty") or 12)
        reasons.append("BTC_REJIM_DUSUS")
        layers["btc_regime"] = "hostile"
    else:
        layers["btc_regime"] = "ok"

    # 7) Futures funding
    fr = funding_map.get(symbol)
    layers["funding"] = fr
    if fr is not None:
        th = float(fut.get("funding_threshold") or -0.0001)
        if fr <= th:
            score += float(fut.get("neg_funding_bonus") or 6)
            reasons.append(f"FUNDING_NEG({fr:.4%})")
        elif fr >= abs(th) * 3:
            score -= 4
            reasons.append(f"FUNDING_POZ({fr:.4%})")

    # 8) 24s erken pencere
    min24 = float(early.get("min_24h_change_pct") or -25)
    max24 = float(early.get("max_24h_change_pct") or 8)
    if min24 <= chg24 <= max24:
        score += 10
        reasons.append(f"24s_ERKEN(%{chg24:+.1f})")
    elif chg24 > max24:
        score -= 10
        reasons.append(f"24s_KACMIS(%{chg24:+.1f})")
        already_late = True

    # 9) Likidite
    if qv24 >= 1_000_000:
        score += 5
        reasons.append("LIKIT")
    elif qv24 >= 300_000:
        score += 2

    # Risk seviyeleri
    swing = min(c1h["l"][-12:]) if len(c1h["l"]) >= 12 else min(c1h["l"])
    atr_v = atr(c1h["h"], c1h["l"], c1h["c"], 14)
    stop, tp1, tp2, rr = calc_risk_levels(price, swing, atr_v, cfg)
    layers["atr_1h"] = round(atr_v, 8) if atr_v else None

    score = max(0.0, min(100.0, score))
    min_al = float(early.get("min_score_al") or 62)
    min_izle = float(early.get("min_score_izle") or 48)

    if already_late or from_low_pct > float(late.get("max_from_14d_low_pct") or 25):
        action, phase = ("GEÇ", "rally_olmus") if score >= min_izle else ("GEÇ", "asiri_uzama")
    elif score >= min_al and vol_ok and from_low_pct <= near_max * 1.25:
        action, phase = "AL", "dip_erken"
    elif score >= min_izle and (vol_ok or from_low_pct <= near_max):
        action, phase = "İZLE", "gelisiyor"
    else:
        action, phase = "YOK", "sinyal_yok"

    if action == "AL":
        if not (vol_ok and from_low_pct <= near_max * 1.35):
            action, phase = "İZLE", "eksik_onay"
        elif rsi_1h is not None and rsi_1h > 58:
            action, phase = "İZLE", "rsi_sicak"
        elif chg24 > float(early.get("max_24h_change_pct") or 8):
            action, phase = "GEÇ", "24s_kacmis"
        elif regime.get("block_al"):
            action, phase = "İZLE", "btc_rejim_bekle"
            reasons.append("AL_ENGEL_BTC")

    return Analysis(
        symbol=symbol,
        base=base,
        price=price,
        change_24h_pct=chg24,
        quote_volume_24h=qv24,
        score=round(score, 1),
        action=action,
        phase=phase,
        reasons=reasons,
        layers=layers,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        risk_reward=rr,
    )


def telegram_send(token: str, chat_id: str, text: str, dry_run: bool = False) -> bool:
    if dry_run:
        print("--- DRY-RUN TELEGRAM ---\n" + text + "\n------------------------")
        return True
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        return r.status_code == 200
    except requests.RequestException as exc:
        print(f"[telegram] {exc}", file=sys.stderr)
        return False


def format_report(rows: list[Analysis], top: int, regime: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    als = [r for r in rows if r.action == "AL"]
    izles = [r for r in rows if r.action == "İZLE"]
    gec = [r for r in rows if r.action == "GEÇ"]
    lines = [
        f"BINANCE DİP AL RADARI v2 · {now}",
        f"Tarama: {len(rows)} aday | 🟢AL={len(als)} 🟡İZLE={len(izles)} 🔴GEÇ={len(gec)}",
    ]
    if regime:
        lines.append(
            f"BTC rejim: %{regime.get('btc_change_24h', 0):+.2f} · "
            f"{'⚠️ DÜŞÜŞ' if regime.get('hostile') else 'OK'}"
        )
    lines.append("")

    if als:
        lines.append("═══ 🟢 AL (dipten erken) ═══")
        for i, r in enumerate(als[:top], 1):
            lines.append(
                f"{i:2d}. {r.base:<8} skor={r.score:5.1f}  "
                f"%{r.change_24h_pct:+.1f}  ${r.price}  "
                f"SL {r.stop}  TP1 {r.tp1}  RR {r.risk_reward}  "
                f"| {', '.join(r.reasons[:4])}"
            )
        lines.append("")
    else:
        lines.append("🟢 AL yok — dipte erken sinyal şu an yok")
        lines.append("")

    if izles:
        lines.append("── 🟡 İZLE ──")
        for i, r in enumerate(izles[: min(8, top)], 1):
            lines.append(
                f"{i:2d}. {r.base:<8} skor={r.score:5.1f}  %{r.change_24h_pct:+.1f}  "
                f"| {', '.join(r.reasons[:4])}"
            )
        lines.append("")

    hot = sorted(gec, key=lambda x: x.change_24h_pct, reverse=True)[:5]
    if hot:
        lines.append("── 🔴 GEÇ (yükselmiş — AL değil) ──")
        for r in hot:
            lines.append(f"   {r.base:<8} %{r.change_24h_pct:+.1f}  skor={r.score}")
    return "\n".join(lines)


def format_telegram(als: list[Analysis], regime: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not als:
        return f"<b>⚪ Binance dip radar</b>\nAL yok.\n<i>{now}</i>"
    lines = [f"<b>🟢 BINANCE DİP AL v2</b> · {len(als)} coin", f"<i>{now}</i>"]
    if regime:
        lines.append(
            f"BTC: %{regime.get('btc_change_24h', 0):+.2f} "
            f"({'düşüş rejimi' if regime.get('hostile') else 'OK'})"
        )
    lines.append("")
    for r in als[:8]:
        fr = r.layers.get("funding")
        fr_s = f"{fr:.4%}" if isinstance(fr, float) else "-"
        lines.append(
            f"<b>{r.base}</b> skor <b>{r.score:.0f}</b> · %{r.change_24h_pct:+.1f}\n"
            f"Fiyat: <code>{r.price}</code>\n"
            f"🛑 SL <code>{r.stop}</code> · "
            f"🎯 TP1 <code>{r.tp1}</code> · TP2 <code>{r.tp2}</code> · RR {r.risk_reward}\n"
            f"Dip+{r.layers.get('from_nd_low_pct')}% · "
            f"Vol×{r.layers.get('vol_rise_5m')} · RSI {r.layers.get('rsi_1h')} · "
            f"Fund {fr_s}\n"
            f"{', '.join(r.reasons[:6])}\n"
        )
    return "\n".join(lines)


def should_alert(state: dict[str, Any], bases: list[str], cooldown: int = 1800) -> list[str]:
    last_map: dict[str, str] = state.get("alerted") or {}
    now = datetime.now(timezone.utc)
    fresh: list[str] = []
    for b in bases:
        prev = last_map.get(b)
        if prev:
            try:
                if (now - datetime.fromisoformat(prev)).total_seconds() < cooldown:
                    continue
            except ValueError:
                pass
        fresh.append(b)
    return fresh


def run_scan(cfg: dict[str, Any], workers: int) -> tuple[list[Analysis], dict[str, Any]]:
    print("[1/4] sembol + ticker…", flush=True)
    symbols = list_usdt_symbols(cfg)
    tickers = fetch_tickers(cfg)
    max_sym = int(cfg.get("max_symbols") or 0)
    min_qv = float(cfg.get("min_quote_volume_usdt") or 0)

    print("[2/4] BTC rejim + funding…", flush=True)
    regime = btc_regime(cfg, tickers)
    funding_map = fetch_funding_map(cfg)
    print(
        f"  BTC %{regime['btc_change_24h']:+.2f} · "
        f"rejim={'HOSTILE' if regime['hostile'] else 'OK'} · "
        f"funding={len(funding_map)} sembol",
        flush=True,
    )

    ranked: list[str] = []
    for sym in symbols:
        t = tickers.get(sym)
        if not t:
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if min_qv and qv < min_qv:
            continue
        ranked.append(sym)
    ranked.sort(key=lambda s: float((tickers.get(s) or {}).get("quoteVolume") or 0), reverse=True)
    if max_sym > 0:
        ranked = ranked[:max_sym]

    print(f"[3/4] {len(ranked)} coin analiz…", flush=True)
    results: list[Analysis] = []

    def job(sym: str) -> Analysis | None:
        return analyze_symbol(sym, tickers[sym], cfg, regime, funding_map)

    with ThreadPoolExecutor(max_workers=max(4, workers)) as pool:
        futs = {pool.submit(job, s): s for s in ranked}
        done = 0
        for fut in as_completed(futs):
            done += 1
            if done % 50 == 0:
                print(f"  … {done}/{len(ranked)}", flush=True)
            try:
                row = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if row:
                results.append(row)

    print(f"[4/4] {len(results)} sonuç", flush=True)
    order = {"AL": 0, "İZLE": 1, "GEÇ": 2, "YOK": 3}
    results.sort(key=lambda r: (order.get(r.action, 9), -r.score, -r.quote_volume_24h))
    return results, regime


def run_backtest(cfg: dict[str, Any], n_symbols: int = 40, hold_hours: int = 12) -> dict[str, Any]:
    """
    Basit ileriye dönük test: son ~10 günde 1h mumlarda
    'erken dip + hacim' şartı sağlandığında sonraki hold_hours getirisi.
    """
    print(f"[backtest] top {n_symbols} coin, hold={hold_hours}h…", flush=True)
    tickers = fetch_tickers(cfg)
    symbols = list_usdt_symbols(cfg)
    ranked = sorted(
        symbols,
        key=lambda s: float((tickers.get(s) or {}).get("quoteVolume") or 0),
        reverse=True,
    )[:n_symbols]

    early = cfg.get("early_buy") or {}
    vol_mult = float(early.get("volume_rise_mult_5m") or 1.4)
    near_max = float(early.get("near_low_max_pct") or 8.0)
    trades: list[dict[str, Any]] = []

    for sym in ranked:
        d1h = fetch_ohlcv(cfg, sym, "1h", 240)
        if not d1h or len(d1h["c"]) < 80:
            continue
        for i in range(48, len(d1h["c"]) - hold_hours - 1):
            window_c = d1h["c"][: i + 1]
            window_v = d1h["v"][: i + 1]
            window_l = d1h["l"][: i + 1]
            price = window_c[-1]
            # 14*24 ~ 14g low approx from 1h
            look = window_l[- min(14 * 24, len(window_l)) :]
            day_low = min(look)
            from_low = ((price / day_low) - 1.0) * 100 if day_low > 0 else 999
            if from_low > near_max:
                continue
            vol_ok, rise = analyze_volume_bottom(window_v[-48:], vol_mult)
            if not vol_ok:
                continue
            r_1h = rsi(window_c, 14)
            if r_1h is None or r_1h > 55:
                continue
            # 0→+ on 1h
            if len(window_c) < 3:
                continue
            if not (
                (window_c[-2] - window_c[-3]) / window_c[-3] <= 0
                and (window_c[-1] - window_c[-2]) / window_c[-2] > 0
            ):
                continue
            exit_px = d1h["c"][i + hold_hours]
            ret = (exit_px / price - 1.0) * 100.0
            trades.append(
                {
                    "symbol": sym,
                    "ret_pct": round(ret, 3),
                    "from_low_pct": round(from_low, 2),
                    "vol_rise": round(rise, 2),
                    "rsi": round(r_1h, 1),
                }
            )
        time.sleep(0.05)

    if not trades:
        summary = {"trades": 0, "winrate": 0, "avg_ret": 0, "median_ret": 0}
    else:
        rets = sorted(t["ret_pct"] for t in trades)
        wins = sum(1 for r in rets if r > 0)
        mid = rets[len(rets) // 2]
        summary = {
            "trades": len(trades),
            "winrate": round(100.0 * wins / len(rets), 1),
            "avg_ret": round(sum(rets) / len(rets), 3),
            "median_ret": mid,
            "best": rets[-1],
            "worst": rets[0],
            "hold_hours": hold_hours,
            "symbols_tested": n_symbols,
        }
    out = {"updated_at": datetime.now(timezone.utc).isoformat(), "summary": summary, "sample": trades[:30]}
    save_json(BACKTEST_PATH, out)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Binance dipten erken AL radarı v2")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--config", default=None)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--backtest-symbols", type=int, default=40)
    parser.add_argument("--backtest-hold", type=int, default=12)
    args = parser.parse_args()

    cfg, cfg_src = load_config(args.config)
    print(f"[config] {cfg_src}")
    print(f"[output] {OUTPUT_PATH}")
    workers = int(args.workers or cfg.get("workers") or 16)

    if args.backtest:
        run_backtest(cfg, n_symbols=args.backtest_symbols, hold_hours=args.backtest_hold)
        return 0

    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    dry = bool(args.dry_run) or args.no_telegram or not (token and chat_id)
    if dry and not args.dry_run and not args.no_telegram:
        print("[info] Telegram env yok → dry-run", file=sys.stderr)

    poll = int(cfg.get("poll_seconds") or 180)
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"alerted": {}}

    while True:
        t0 = time.time()
        rows, regime = run_scan(cfg, workers)
        visible = [r for r in rows if r.action != "YOK"]
        print("\n" + format_report(visible, args.top, regime) + "\n")

        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_sec": round(time.time() - t0, 1),
            "regime": regime,
            "counts": {
                a: sum(1 for r in rows if r.action == a) for a in ("AL", "İZLE", "GEÇ", "YOK")
            },
            "signals": [asdict(r) for r in visible[:80]],
        }
        save_json(OUTPUT_PATH, payload)

        als = [r for r in rows if r.action == "AL"]
        fresh = should_alert(state, [r.base for r in als])
        alert_rows = [r for r in als if r.base in fresh]
        if alert_rows and not args.no_telegram:
            msg = format_telegram(alert_rows, regime)
            if telegram_send(token or "", chat_id or "", msg, dry_run=dry):
                alerted = state.setdefault("alerted", {})
                now = datetime.now(timezone.utc).isoformat()
                for r in alert_rows:
                    alerted[r.base] = now
                if len(alerted) > 500:
                    items = sorted(alerted.items(), key=lambda x: x[1], reverse=True)[:300]
                    state["alerted"] = dict(items)
                save_json(STATE_PATH, state)

        if args.once:
            break
        print(f"[sleep] {poll}s…")
        time.sleep(poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
