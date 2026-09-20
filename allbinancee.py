#!/usr/bin/env python3
"""
Binance Dip AL Radar + GERÇEK al/sat (tek dosya).

1) Aşağıdaki API KEY / SECRET satırlarını doldur
2) Kaydet
3) Çalıştır:  python allbinancee.py --once

Kurallar:
  • 16 büyük CEX hacim taraması + Binance derin analiz
  • Onaylanan coinlere USDT EŞİT bölünür (max 10 / turda max 5)
  • Komisyon koruması: net kâr eşiği altında TP yok
  • Entry -%0.5 → STOP sat | Zirveden -%0.5 → TRAIL sat | +%1.2 → TP sat
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import ccxt
except ImportError:  # pragma: no cover
    ccxt = None  # type: ignore[assignment]

# =============================================================================
#  >>> API KEY BURAYA  (tırnak içinde, boşluksuz)  <<<
#  Binance → API Management → Create API
#  İzin: Enable Spot & Margin Trading  AÇIK olsun
# =============================================================================
BINANCE_API_KEY_HARDCODE = "BURAYA_API_KEY"
BINANCE_API_SECRET_HARDCODE = "BURAYA_SECRET_KEY"
# True = gerçek para ile al/sat  |  False = sadece kağıt (paper)
LIVE_HARDCODE = True
TRADE_HARDCODE = True
# =============================================================================

# En büyük 16 CEX — Binance derin analiz + diğerlerinde 5m hacim 0→+ onay
MULTI_CEX_IDS: list[str] = [
    "binance",   # 1
    "okx",       # 2
    "bybit",     # 3
    "bitget",    # 4
    "gate",      # 5  (Gate.io)
    "kucoin",    # 6
    "mexc",      # 7
    "htx",       # 8  (eski Huobi)
    "coinbase",  # 9
    "upbit",     # 10
    "kraken",    # 11
    "bingx",     # 12
    "cryptocom", # 13
    "whitebit",  # 14
    "coinex",    # 15
    "bitstamp",  # 16
]

DEFAULT_CONFIG: dict[str, Any] = {
    "rest_base": "https://data-api.binance.vision",
    "futures_base": "https://fapi.binance.com",
    "quote": "USDT",
    "workers": 20,
    "poll_seconds": 120,  # sık kontrol: -0.5% stop / zirve trail için
    "max_symbols": 0,
    "min_quote_volume_usdt": 500000,  # ince coin / kayma koruması
    "ohlcv": {"5m": 48, "15m": 96, "1h": 72, "1d": 90},
    "early_buy": {
        "max_24h_change_pct": 5.0,
        "min_24h_change_pct": -25.0,
        "early_rally_max_pct": 4.0,
        "near_low_lookback_days": 14,
        "near_low_max_pct": 10.0,
        "max_rsi_1h": 55.0,
        "min_rsi_1h": 25.0,
        "volume_rise_mult_5m": 1.35,
        "min_score_al": 62,
        "min_score_izle": 50,
        "min_pump_score_al": 58,
    },
    "late_reject": {
        "max_already_up_24h_pct": 10.0,
        "max_rsi_1h": 68.0,
        "max_from_14d_low_pct": 25.0,
    },
    "pump_upside": {
        "enabled": True,
        "target_low_pct": 40.0,
        "target_high_pct": 70.0,
        "min_room_to_30d_high_pct": 25.0,
        "quiet_vol_bars": 18,
        "vol_turn_mult": 1.8,
        "compression_atr_ratio": 0.75,
    },
    "regime": {
        "enabled": True,
        "btc_dump_pct": -2.0,
        "block_al_on_btc_dump": True,
        "hard_block_on_btc_dump": True,  # dump tek başına AL kapar (EMA beklemez)
        "soft_penalty": 15,
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
        "use_pump_tp": True,
    },
    "trade": {
        "enabled": True,
        "max_positions": 10,
        "deploy_pct": 0.95,
        "min_order_usdt": 12.0,
        "tp1_sell_pct": 1.0,
        "prefer_uc": True,
        "also_buy_izle": False,  # sadece kaliteli AL
        "require_uc": True,  # UÇ şart
        "require_cex_min": 2,  # en az N CEX onay (0=kapalı; multi-cex kapalıysa esneklik)
        "izle_min_score": 65.0,
        "izle_min_pump": 55.0,
        "izle_max_24h_pct": 4.0,
        "trade_base": "https://api.binance.com",
        "recv_window": 60000,
        "fee_rate_pct": 0.10,
        "bnb_fee_discount": True,  # BNB varsa fee ~%25 indirim
        "fee_buffer_pct": 0.25,
        "hard_stop_pct": 0.50,
        "peak_trail_pct": 0.50,
        "peak_trail_tight_pct": 0.30,  # kâr büyüyünce sıkı trail
        "trail_tighten_after_pct": 1.0,  # peak kâr ≥1% olunca tight trail
        "quick_tp_pct": 1.20,
        "min_net_tp_pct": 0.55,
        "time_stop_minutes": 30,  # 30 dk kâr yoksa çık
        "time_stop_min_pnl_pct": 0.0,  # zaman stop'ta min brüt (0=fee üstü veya küçük zarar)
        "max_buy_per_cycle": 3,
        "max_per_sector": 2,  # korelasyon: aynı sektör max 2
        "use_limit_orders": True,  # maker dene → dolmazsa market
        "limit_wait_sec": 3.0,
        "spread_tp_boost": True,  # geniş spread → daha yüksek TP eşiği
    },
    "multi_cex": {
        "enabled": True,  # 15+ büyük CEX hacim taraması AÇIK
        "ids": list(MULTI_CEX_IDS),
        "max_symbols_per_exchange": 60,
        "ohlcv_limit": 30,
        "workers_per_exchange": 8,
        "min_confluence_boost": 2,
        "confluence_score_bonus": 8,
        "uc_min_cex": 3,
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
POS_PATH = OUTPUT_DIR / "binance_dip_buy_positions.json"
TRADE_LOG = OUTPUT_DIR / "binance_dip_buy_trades.jsonl"
PNL_PATH = OUTPUT_DIR / "binance_dip_buy_pnl_daily.json"

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "binance-dip-buy-radar/2.1"})


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
        for k in (
            "early_buy",
            "late_reject",
            "regime",
            "futures",
            "risk",
            "ohlcv",
            "pump_upside",
            "multi_cex",
            "trade",
        ):
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
    pump_score: float = 0.0
    upside_est_pct: float = 0.0
    is_uc: bool = False


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


def volume_zero_to_pos(
    vols: list[float],
    *,
    quiet_bars: int = 18,
    turn_mult: float = 1.8,
) -> dict[str, Any]:
    """Sessiz/düşen hacim → artıya geçiş (AR tipi erken uyanış)."""
    n = max(8, quiet_bars)
    if len(vols) < n + 3:
        return {"ok": False, "quiet": False, "turn": False, "ratio": 0.0}
    quiet = vols[-(n + 3) : -3]
    recent = vols[-3:]
    q_avg = sum(quiet) / len(quiet) if quiet else 0.0
    r_avg = sum(recent) / len(recent)
    quiet_ok = q_avg > 0 and max(quiet[-6:]) <= q_avg * 1.35
    slope_up = recent[-1] > recent[0] and recent[-1] > recent[-2]
    ratio = (r_avg / q_avg) if q_avg > 0 else 0.0
    turn = ratio >= turn_mult and slope_up and recent[-1] > q_avg * turn_mult
    return {
        "ok": bool(quiet_ok and turn),
        "quiet": quiet_ok,
        "turn": turn,
        "ratio": round(ratio, 2),
        "q_avg": q_avg,
        "r_avg": r_avg,
    }


def analyze_pump_upside(
    *,
    price: float,
    chg24: float,
    c5: dict[str, list[float]],
    c15: dict[str, list[float]] | None,
    c1h: dict[str, list[float]],
    c1d: dict[str, list[float]],
    from_low_pct: float,
    vol_ok: bool,
    vol_rise: float,
    rsi_1h: float | None,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """
    +3/+5 erken ralli iken +50/+70'e gidebilecek 'uç' potansiyeli.
    AR benzeri: sessiz taban + hacim 0→+ + sıkışma + yukarıda boşluk.
    """
    pu = cfg.get("pump_upside") or {}
    if not pu.get("enabled", True):
        return {"pump_score": 0.0, "upside_est_pct": 0.0, "is_uc": False, "reasons": []}

    early = cfg.get("early_buy") or {}
    early_max = float(early.get("early_rally_max_pct") or 5.0)
    score = 0.0
    reasons: list[str] = []

    vz = volume_zero_to_pos(
        c5["v"],
        quiet_bars=int(pu.get("quiet_vol_bars") or 18),
        turn_mult=float(pu.get("vol_turn_mult") or 1.8),
    )
    vz_1h = volume_zero_to_pos(
        c1h["v"],
        quiet_bars=min(24, max(10, len(c1h["v"]) - 4)),
        turn_mult=1.5,
    )
    if vz["ok"]:
        score += 28
        reasons.append(f"HACIM_0→+×{vz['ratio']:.1f}")
    elif vz["turn"] and vol_ok:
        score += 14
        reasons.append(f"HACIM_DONUS×{vz['ratio']:.1f}")
    if vz_1h["ok"] or (vz_1h["turn"] and vz_1h["ratio"] >= 1.5):
        score += 10
        reasons.append(f"HACIM_1h_0→+×{vz_1h['ratio']:.1f}")
    if vol_ok and vol_rise >= 2.0:
        score += 6

    if chg24 <= 1.0:
        score += 16
        reasons.append("FIYAT_HENUZ_YATAY")
    elif chg24 <= early_max:
        score += 18
        reasons.append(f"ERKEN_RALLI(+%{chg24:.1f})")
    elif chg24 <= early_max + 3:
        score += 8
        reasons.append(f"ERKEN_SINIR(+%{chg24:.1f})")
    else:
        score -= 20
        reasons.append("FIYAT_COK_GITMIS")

    if from_low_pct <= 8:
        score += 14
        reasons.append(f"DIPTE_KALIYOR(%{from_low_pct:.1f})")
    elif from_low_pct <= 12:
        score += 8
        reasons.append(f"DIP_YAKIN(%{from_low_pct:.1f})")
    elif from_low_pct > 22:
        score -= 15

    high_30 = max(c1d["h"][-30:]) if len(c1d["h"]) >= 10 else max(c1d["h"])
    room_pct = ((high_30 / price) - 1.0) * 100.0 if price > 0 else 0.0
    min_room = float(pu.get("min_room_to_30d_high_pct") or 25.0)
    if room_pct >= 60:
        score += 16
        reasons.append(f"UC_ALANI_30g(+%{room_pct:.0f})")
    elif room_pct >= min_room:
        score += 10
        reasons.append(f"BOSLUK_30g(+%{room_pct:.0f})")
    elif room_pct < 12:
        score -= 12
        reasons.append("TAVANA_YAKIN")

    atr_now = atr(c1h["h"], c1h["l"], c1h["c"], 14)
    atr_prev = None
    if len(c1h["c"]) >= 40:
        atr_prev = atr(c1h["h"][:-14], c1h["l"][:-14], c1h["c"][:-14], 14)
    if atr_now and atr_prev and atr_prev > 0:
        ratio = atr_now / atr_prev
        if ratio >= 1.15:
            score += 10
            reasons.append("SIKISMA_KIRILIM")
        elif ratio >= 1.05:
            score += 5
            reasons.append("VOL_GENISLIYOR")

    if len(c1d["h"]) >= 30:
        r7 = max(c1d["h"][-7:]) - min(c1d["l"][-7:])
        r30 = max(c1d["h"][-30:]) - min(c1d["l"][-30:])
        if r30 > 0 and (r7 / r30) <= 0.35 and vol_ok:
            score += 8
            reasons.append("TABAN_SIKISMA")

    if c15 and len(c15["l"]) >= 20:
        if min(c15["l"][-6:]) > min(c15["l"][-18:-6]) * 1.001:
            score += 6
            reasons.append("HL_YAPISI")
    if rsi_1h is not None:
        if 32 <= rsi_1h <= 55:
            score += 8
            reasons.append(f"RSI_UC_PENCERE({rsi_1h:.0f})")
        elif rsi_1h > 65:
            score -= 12

    if len(c1d["v"]) >= 10:
        quiet_days = c1d["v"][-8:-1]
        today = c1d["v"][-1]
        qd = sum(quiet_days) / len(quiet_days)
        if qd > 0 and today >= qd * 1.4 and chg24 <= early_max + 2:
            score += 8
            reasons.append("GUNLUK_HACIM_UYANDI")

    score = max(0.0, min(100.0, score))
    t_lo = float(pu.get("target_low_pct") or 40.0)
    t_hi = float(pu.get("target_high_pct") or 70.0)
    upside = t_lo + (t_hi - t_lo) * (score / 100.0)
    upside = min(max(upside, t_lo * 0.7), t_hi)
    if room_pct < 20:
        upside = min(upside, max(15.0, room_pct * 0.9))

    is_uc = (
        score >= 58
        and chg24 <= early_max + 1.5
        and from_low_pct <= 14
        and (vz["ok"] or (vol_ok and vz["ratio"] >= 1.5) or vz_1h["ok"])
        and room_pct >= min_room * 0.8
    )
    if is_uc:
        reasons.insert(0, f"UC_POTANSIYEL(~%{upside:.0f})")

    return {
        "pump_score": round(score, 1),
        "upside_est_pct": round(upside, 1),
        "is_uc": is_uc,
        "reasons": reasons,
        "room_30d_pct": round(room_pct, 1),
        "vol_zero_to_pos": vz,
    }


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
    hard = bool(reg.get("hard_block_on_btc_dump", True))
    soft_hostile = is_dump and below_ema
    hostile = bool(reg.get("enabled", True)) and soft_hostile
    block_al = bool(reg.get("enabled", True)) and bool(reg.get("block_al_on_btc_dump", True)) and (
        (hard and is_dump) or ((not hard) and soft_hostile)
    )
    return {
        "btc_change_24h": chg,
        "btc_dump": is_dump,
        "btc_below_ema25": below_ema,
        "hostile": hostile,
        "block_al": block_al,
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

    # 10) UÇ POTANSİYEL (+50/+70 öngörü) — hacim 0→+ & erken ralli
    pump = analyze_pump_upside(
        price=price,
        chg24=chg24,
        c5=c5,
        c15=c15,
        c1h=c1h,
        c1d=c1d,
        from_low_pct=from_low_pct,
        vol_ok=vol_ok,
        vol_rise=vol_rise,
        rsi_1h=rsi_1h,
        cfg=cfg,
    )
    pump_score = float(pump["pump_score"])
    upside_est = float(pump["upside_est_pct"])
    is_uc = bool(pump["is_uc"])
    layers["pump_score"] = pump_score
    layers["upside_est_pct"] = upside_est
    layers["room_30d_pct"] = pump.get("room_30d_pct")
    layers["vol_0_to_pos"] = (pump.get("vol_zero_to_pos") or {}).get("ratio")
    # Ana skora uç katkısı
    score += min(18.0, pump_score * 0.18)
    for pr in pump.get("reasons") or []:
        if pr not in reasons:
            reasons.append(pr)

    # Risk seviyeleri — UC ise TP2 = tahmini uç
    swing = min(c1h["l"][-12:]) if len(c1h["l"]) >= 12 else min(c1h["l"])
    atr_v = atr(c1h["h"], c1h["l"], c1h["c"], 14)
    stop, tp1, tp2, rr = calc_risk_levels(price, swing, atr_v, cfg)
    risk_cfg = cfg.get("risk") or {}
    if risk_cfg.get("use_pump_tp", True) and upside_est >= 25:
        tp2 = round(price * (1.0 + upside_est / 100.0), 8)
        risk_amt = price - stop if stop else 0
        if risk_amt > 0:
            rr = round((tp1 - price) / risk_amt, 2)
    layers["atr_1h"] = round(atr_v, 8) if atr_v else None

    score = max(0.0, min(100.0, score))
    min_al = float(early.get("min_score_al") or 60)
    min_izle = float(early.get("min_score_izle") or 46)
    min_pump = float(early.get("min_pump_score_al") or 55)
    early_rally_max = float(early.get("early_rally_max_pct") or 5.0)

    if already_late or from_low_pct > float(late.get("max_from_14d_low_pct") or 28):
        action, phase = ("GEÇ", "rally_olmus") if score >= min_izle else ("GEÇ", "asiri_uzama")
    elif (
        score >= min_al
        and vol_ok
        and from_low_pct <= near_max * 1.35
        and chg24 <= float(early.get("max_24h_change_pct") or 6) + 0.5
        and (is_uc or pump_score >= min_pump * 0.85)
    ):
        action, phase = "AL", "uc_erken" if is_uc else "dip_erken"
    elif score >= min_al and vol_ok and from_low_pct <= near_max * 1.25:
        action, phase = "AL", "dip_erken"
    elif is_uc and pump_score >= min_pump and chg24 <= early_rally_max + 1:
        # klasik skor biraz düşük olsa bile uç setup AL
        action, phase = "AL", "uc_setup"
        score = max(score, min_al)
    elif score >= min_izle and (vol_ok or from_low_pct <= near_max or pump_score >= 50):
        action, phase = "İZLE", "gelisiyor"
    else:
        action, phase = "YOK", "sinyal_yok"

    if action == "AL":
        if not (vol_ok or (pump.get("vol_zero_to_pos") or {}).get("ok")):
            action, phase = "İZLE", "eksik_hacim"
        elif from_low_pct > near_max * 1.5:
            action, phase = "İZLE", "dip_uzak"
        elif rsi_1h is not None and rsi_1h > 60:
            action, phase = "İZLE", "rsi_sicak"
        elif chg24 > float(early.get("max_24h_change_pct") or 6) + 2:
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
        pump_score=pump_score,
        upside_est_pct=upside_est,
        is_uc=is_uc and action == "AL",
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
    ucs = [r for r in als if r.is_uc]
    izles = [r for r in rows if r.action == "İZLE"]
    gec = [r for r in rows if r.action == "GEÇ"]
    lines = [
        f"BINANCE DİP AL RADARI v2.1 · {now}",
        f"Tarama: {len(rows)} aday | 🟢AL={len(als)} 🚀UÇ={len(ucs)} "
        f"🟡İZLE={len(izles)} 🔴GEÇ={len(gec)}",
    ]
    if regime:
        lines.append(
            f"BTC rejim: %{regime.get('btc_change_24h', 0):+.2f} · "
            f"{'⚠️ DÜŞÜŞ' if regime.get('hostile') else 'OK'}"
        )
        mc = regime.get("multi_cex") or {}
        if mc:
            scanned = mc.get("scanned") or []
            lines.append(
                f"CEX tarama: {len(scanned)}/{mc.get('requested', 0)} borsa · "
                f"{', '.join(scanned)}"
            )
    lines.append("")

    if ucs:
        lines.append("═══ 🚀 UÇ POTANSİYEL (+50/+70 adayı) ═══")
        for i, r in enumerate(sorted(ucs, key=lambda x: -x.pump_score)[:top], 1):
            lines.append(
                f"{i:2d}. {r.base:<8} uç={r.pump_score:5.1f} →~%{r.upside_est_pct:.0f}  "
                f"24s%{r.change_24h_pct:+.1f}  CEX×{r.layers.get('cex_count', 0)}  "
                f"SL {r.stop}  TP2 {r.tp2}  "
                f"| {', '.join(r.reasons[:5])}"
            )
        lines.append("")

    if als:
        lines.append("═══ 🟢 AL (dipten / erken) ═══")
        for i, r in enumerate(als[:top], 1):
            tag = "🚀" if r.is_uc else "  "
            lines.append(
                f"{i:2d}.{tag}{r.base:<8} skor={r.score:5.1f} uç={r.pump_score:4.0f}  "
                f"%{r.change_24h_pct:+.1f}  ~%{r.upside_est_pct:.0f}  "
                f"SL {r.stop}  TP1 {r.tp1}  "
                f"| {', '.join(r.reasons[:4])}"
            )
        lines.append("")
    else:
        lines.append("🟢 AL yok — dipte erken sinyal şu an yok")
        lines.append("")

    if izles:
        lines.append("── 🟡 İZLE ──")
        for i, r in enumerate(
            sorted(izles, key=lambda x: (-x.pump_score, -x.score))[: min(8, top)], 1
        ):
            lines.append(
                f"{i:2d}. {r.base:<8} skor={r.score:5.1f} uç={r.pump_score:4.0f}  "
                f"%{r.change_24h_pct:+.1f}  | {', '.join(r.reasons[:4])}"
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
    ucs = [r for r in als if r.is_uc]
    lines = [
        f"<b>🟢 BINANCE DİP AL v2.1</b> · {len(als)} coin"
        + (f" · 🚀{len(ucs)} uç" if ucs else ""),
        f"<i>{now}</i>",
    ]
    if regime:
        lines.append(
            f"BTC: %{regime.get('btc_change_24h', 0):+.2f} "
            f"({'düşüş rejimi' if regime.get('hostile') else 'OK'})"
        )
    lines.append("")
    ordered = sorted(als, key=lambda r: (not r.is_uc, -r.pump_score, -r.score))
    for r in ordered[:8]:
        fr = r.layers.get("funding")
        fr_s = f"{fr:.4%}" if isinstance(fr, float) else "-"
        head = f"🚀 <b>{r.base}</b>" if r.is_uc else f"<b>{r.base}</b>"
        lines.append(
            f"{head} skor {r.score:.0f} · uç {r.pump_score:.0f} → ~%{r.upside_est_pct:.0f}\n"
            f"24s %{r.change_24h_pct:+.1f} · <code>{r.price}</code>\n"
            f"🛑 SL <code>{r.stop}</code> · "
            f"🎯 TP1 <code>{r.tp1}</code> · TP2 <code>{r.tp2}</code>\n"
            f"Dip+{r.layers.get('from_nd_low_pct')}% · "
            f"Vol0→+ {r.layers.get('vol_0_to_pos')} · "
            f"RSI {r.layers.get('rsi_1h')} · Fund {fr_s}\n"
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


def _ccxt_exchange(ex_id: str) -> Any:
    assert ccxt is not None
    klass = getattr(ccxt, ex_id)
    return klass({"enableRateLimit": True, "timeout": 20000, "options": {"defaultType": "spot"}})


def _scan_binance_vision_wake(
    *, max_symbols: int, ohlcv_limit: int, workers: int
) -> tuple[str, set[str], str | None]:
    try:
        info = get_json(f"{DEFAULT_CONFIG['rest_base']}/api/v3/exchangeInfo")
        tickers = get_json(f"{DEFAULT_CONFIG['rest_base']}/api/v3/ticker/24hr")
    except Exception as exc:  # noqa: BLE001
        return "binance", set(), f"{exc.__class__.__name__}"
    usdt = {
        s["symbol"]
        for s in info.get("symbols", [])
        if s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
        and s.get("isSpotTradingAllowed", True)
    }
    ranked: list[tuple[float, str, str]] = []
    for t in tickers:
        sym = t.get("symbol") or ""
        if sym not in usdt:
            continue
        base = _normalize_base(sym.replace("USDT", ""))
        try:
            qv = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            qv = 0.0
        ranked.append((qv, base, sym))
    ranked.sort(key=lambda x: x[0], reverse=True)
    ranked = ranked[:max_symbols]
    wakes: set[str] = set()

    def job(item: tuple[float, str, str]) -> str | None:
        _q, base, market = item
        try:
            rows = get_json(
                f"{DEFAULT_CONFIG['rest_base']}/api/v3/klines",
                {"symbol": market, "interval": "5m", "limit": ohlcv_limit},
                timeout=20,
            )
            vols = [float(r[5]) for r in rows[:-1]]
        except Exception:  # noqa: BLE001
            return None
        vz = volume_zero_to_pos(vols, quiet_bars=12, turn_mult=1.6)
        ok, _ = analyze_volume_bottom(vols, 1.35)
        return base if (vz.get("ok") or ok) else None

    with ThreadPoolExecutor(max_workers=max(2, workers)) as pool:
        for fut in as_completed([pool.submit(job, row) for row in ranked]):
            try:
                b = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if b:
                wakes.add(b)
    return "binance", wakes, None


def _normalize_base(base: str) -> str:
    b = (base or "").upper().strip()
    if b.startswith("1000") and len(b) > 4:
        b = b[4:]
    return b


def scan_one_cex_volume_wake(
    ex_id: str,
    *,
    max_symbols: int,
    ohlcv_limit: int,
    workers: int,
) -> tuple[str, set[str], str | None]:
    """Bir CEX'te 5m hacim 0→+ / dipten yükseliş olan base seti."""
    # Binance geo-block → vision API
    if ex_id == "binance":
        return _scan_binance_vision_wake(max_symbols=max_symbols, ohlcv_limit=ohlcv_limit, workers=workers)

    if ccxt is None:
        return ex_id, set(), "ccxt yok"
    try:
        ex = _ccxt_exchange(ex_id)
        markets = ex.load_markets()
    except Exception as exc:  # noqa: BLE001
        return ex_id, set(), f"{exc.__class__.__name__}"

    # USDT/USD pariteleri
    candidates: list[tuple[float, str, str]] = []
    for sym, m in markets.items():
        if not m.get("active", True):
            continue
        if m.get("spot") is False or m.get("contract") or m.get("swap"):
            continue
        quote = str(m.get("quote") or "").upper()
        if quote not in {"USDT", "USD", "USDC"}:
            continue
        base = _normalize_base(str(m.get("base") or ""))
        if not base or base in {"USDT", "USDC", "USD"}:
            continue
        candidates.append((0.0, base, sym))

    # ticker ile hacme göre sırala
    try:
        tickers = ex.fetch_tickers([c[2] for c in candidates[: max_symbols * 3]])
    except Exception:
        try:
            tickers = ex.fetch_tickers()
        except Exception:  # noqa: BLE001
            tickers = {}

    ranked: list[tuple[float, str, str]] = []
    for _qv, base, sym in candidates:
        t = tickers.get(sym) or {}
        try:
            qv = float(t.get("quoteVolume") or t.get("baseVolume") or 0)
        except (TypeError, ValueError):
            qv = 0.0
        ranked.append((qv, base, sym))
    ranked.sort(key=lambda x: x[0], reverse=True)
    ranked = ranked[:max_symbols]

    wakes: set[str] = set()

    def job(item: tuple[float, str, str]) -> str | None:
        _q, base, sym = item
        try:
            rows = ex.fetch_ohlcv(sym, timeframe="5m", limit=ohlcv_limit)
        except Exception:  # noqa: BLE001
            return None
        if not rows or len(rows) < 12:
            return None
        vols = [float(r[5]) for r in rows[:-1]]
        vz = volume_zero_to_pos(vols, quiet_bars=12, turn_mult=1.6)
        ok, _rise = analyze_volume_bottom(vols, 1.35)
        if vz.get("ok") or ok:
            return base
        return None

    with ThreadPoolExecutor(max_workers=max(2, workers)) as pool:
        futs = [pool.submit(job, row) for row in ranked]
        for fut in as_completed(futs):
            try:
                b = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if b:
                wakes.add(b)
    return ex_id, wakes, None


def scan_multi_cex_confluence(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    15+ CEX'te hacim uyanışı tara.
    Dönüş: { base: [cex,...], scanned: [...], errors: [...] }
    """
    mc = cfg.get("multi_cex") or {}
    if not mc.get("enabled", True):
        return {"by_base": {}, "scanned": [], "errors": [], "requested": 0}
    if ccxt is None:
        print("[multi-cex] ccxt kurulu değil → pip install ccxt", file=sys.stderr)
        return {"by_base": {}, "scanned": [], "errors": ["ccxt missing"], "requested": 0}

    ids = list(mc.get("ids") or MULTI_CEX_IDS)
    max_sym = int(mc.get("max_symbols_per_exchange") or 60)
    limit = int(mc.get("ohlcv_limit") or 30)
    workers = int(mc.get("workers_per_exchange") or 8)

    print(f"[multi-cex] {len(ids)} borsa taranıyor: {', '.join(ids)}", flush=True)
    by_base: dict[str, list[str]] = {}
    scanned: list[str] = []
    errors: list[str] = []

    # Borsaları paralel değil sırayla (rate limit); her biri kendi pool'unu kullanır
    for ex_id in ids:
        t0 = time.time()
        name, wakes, err = scan_one_cex_volume_wake(
            ex_id, max_symbols=max_sym, ohlcv_limit=limit, workers=workers
        )
        dt = time.time() - t0
        if err:
            errors.append(f"{name}: {err}")
            print(f"  ! {name} atlandı ({err}) {dt:.1f}s", flush=True)
            continue
        scanned.append(name)
        for b in wakes:
            by_base.setdefault(b, []).append(name)
        print(f"  → {name}: {len(wakes)} hacim-uyanış ({dt:.1f}s)", flush=True)

    print(
        f"[multi-cex] başarılı {len(scanned)}/{len(ids)} borsa · "
        f"{len(by_base)} ortak base adayı",
        flush=True,
    )
    return {
        "by_base": by_base,
        "scanned": scanned,
        "errors": errors,
        "requested": len(ids),
    }


def apply_cex_confluence(
    rows: list[Analysis],
    confluence: dict[str, Any],
    cfg: dict[str, Any],
) -> list[Analysis]:
    """Çoklu CEX onayını skora / UÇ bayrağına yedir."""
    mc = cfg.get("multi_cex") or {}
    by_base: dict[str, list[str]] = confluence.get("by_base") or {}
    min_boost = int(mc.get("min_confluence_boost") or 2)
    bonus = float(mc.get("confluence_score_bonus") or 8)
    uc_min = int(mc.get("uc_min_cex") or 3)
    majors = {"BTC", "ETH", "BNB", "SOL", "XRP"}
    out: list[Analysis] = []
    for r in rows:
        cexes = sorted(set(by_base.get(r.base) or []))
        n = len(cexes)
        r.layers["cex_count"] = n
        r.layers["cex_list"] = cexes
        if n >= min_boost:
            r.score = min(100.0, round(r.score + bonus * min(n, 6) / 2, 1))
            r.pump_score = min(100.0, round(r.pump_score + 5 * min(n, 5), 1))
            tag = f"CEX×{n}"
            if tag not in r.reasons:
                r.reasons.append(tag)
            # Major'larda çoklu CEX her zaman var — UÇ için altcoin şartı
            if (
                n >= uc_min
                and r.base not in majors
                and r.action in {"AL", "İZLE"}
                and r.change_24h_pct <= 6.5
                and r.pump_score >= 55
            ):
                if r.action == "İZLE":
                    r.action = "AL"
                    r.phase = "cex_confluence"
                if r.action == "AL":
                    r.is_uc = True
                    r.upside_est_pct = max(r.upside_est_pct, 50.0)
                    if not any(x.startswith("UC_POTANSIYEL") for x in r.reasons):
                        r.reasons.insert(0, f"UC_POTANSIYEL(~%{r.upside_est_pct:.0f})")
                    if "COKLU_CEX_ONAY" not in r.reasons:
                        r.reasons.append("COKLU_CEX_ONAY")
        out.append(r)
    order = {"AL": 0, "İZLE": 1, "GEÇ": 2, "YOK": 3}
    out.sort(
        key=lambda r: (
            order.get(r.action, 9),
            0 if r.is_uc else 1,
            -int(r.layers.get("cex_count") or 0),
            -r.pump_score,
            -r.score,
        )
    )
    return out


def run_scan(cfg: dict[str, Any], workers: int) -> tuple[list[Analysis], dict[str, Any]]:
    print("[1/5] sembol + ticker…", flush=True)
    symbols = list_usdt_symbols(cfg)
    tickers = fetch_tickers(cfg)
    max_sym = int(cfg.get("max_symbols") or 0)
    min_qv = float(cfg.get("min_quote_volume_usdt") or 0)

    print("[2/5] BTC rejim + funding…", flush=True)
    regime = btc_regime(cfg, tickers)
    funding_map = fetch_funding_map(cfg)
    print(
        f"  BTC %{regime['btc_change_24h']:+.2f} · "
        f"rejim={'HOSTILE' if regime['hostile'] else 'OK'} · "
        f"funding={len(funding_map)} sembol",
        flush=True,
    )

    print("[3/5] çoklu CEX hacim taraması (15+)…", flush=True)
    confluence = scan_multi_cex_confluence(cfg)
    regime["multi_cex"] = {
        "requested": confluence.get("requested"),
        "scanned": confluence.get("scanned"),
        "errors": confluence.get("errors"),
        "wake_bases": len(confluence.get("by_base") or {}),
    }

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

    print(f"[4/5] Binance derin analiz · {len(ranked)} coin…", flush=True)
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

    print("[5/5] CEX confluence birleştir…", flush=True)
    results = apply_cex_confluence(results, confluence, cfg)
    print(f"  → {len(results)} sonuç · CEX OK {len(confluence.get('scanned') or [])}", flush=True)
    return results, regime


# ---------------------------------------------------------------------------
# Binance spot al/sat (signed) — max 10 pozisyon, USDT eşit bölünür
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_trade(row: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with TRADE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def clean_api_credential(raw: str) -> str:
    """Boşluk / tırnak / gizli karakter temizle (Windows yapıştırma hataları)."""
    s = (raw or "").strip()
    # akıllı tırnaklar
    s = (
        s.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        s = s[1:-1].strip()
    for ch in (" ", "\t", "\r", "\n", "\u200b", "\u200c", "\u200d", "\ufeff"):
        s = s.replace(ch, "")
    return s


def resolve_api_keys() -> tuple[str, str]:
    """Dosyadaki HARDCODE öncelikli (ENV eski secret -1022 yapmasın)."""
    hard_key = clean_api_credential(BINANCE_API_KEY_HARDCODE or "")
    hard_sec = clean_api_credential(BINANCE_API_SECRET_HARDCODE or "")
    env_key = clean_api_credential(env("BINANCE_API_KEY") or "")
    env_sec = clean_api_credential(env("BINANCE_API_SECRET") or "")

    # placeholder metinleri boş say
    placeholders = {
        "",
        "BURAYA_API_KEY",
        "BURAYA_SECRET_KEY",
        "YOUR_KEY",
        "YOUR_SECRET",
        "api_key",
        "secret_key",
    }
    if hard_key in placeholders:
        hard_key = ""
    if hard_sec in placeholders:
        hard_sec = ""

    if hard_key and hard_sec:
        print("[keys] kaynak=DOSYA (HARDCODE)")
        return hard_key, hard_sec
    if env_key and env_sec:
        print("[keys] kaynak=ENV")
        return env_key, env_sec
    print("[keys] kaynak=EKSIK — BURAYA_API_KEY / BURAYA_SECRET_KEY doldur")
    return hard_key or env_key, hard_sec or env_sec


def binance_server_time_ms(trade_base: str) -> int:
    """PC saati kaymışsa imza/timestamp bozulmasın diye Binance saatini kullan."""
    try:
        r = HTTP.get(f"{trade_base}/api/v3/time", timeout=10)
        r.raise_for_status()
        return int(r.json()["serverTime"])
    except Exception:  # noqa: BLE001
        return int(time.time() * 1000)


def signed_request(
    method: str,
    trade_base: str,
    path: str,
    api_key: str,
    api_secret: str,
    params: dict[str, Any] | None = None,
    recv_window: int = 5000,
) -> Any:
    api_key = clean_api_credential(api_key)
    api_secret = clean_api_credential(api_secret)
    params = dict(params or {})
    params["timestamp"] = binance_server_time_ms(trade_base)
    params["recvWindow"] = int(recv_window)
    # Binance: sabit sıralı query + HMAC-SHA256
    query = urllib.parse.urlencode(params, doseq=True)
    sig = hmac.new(
        api_secret.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers = {"X-MBX-APIKEY": api_key}
    url = f"{trade_base}{path}"
    if method == "GET":
        r = HTTP.get(f"{url}?{query}&signature={sig}", headers=headers, timeout=30)
    elif method == "POST":
        # POST: body olarak imzalı form (imza hatalarını azaltır)
        body = f"{query}&signature={sig}"
        headers = {
            **headers,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        r = HTTP.post(url, data=body, headers=headers, timeout=30)
    elif method == "DELETE":
        r = HTTP.delete(f"{url}?{query}&signature={sig}", headers=headers, timeout=30)
    else:
        raise ValueError(method)
    if r.status_code >= 400:
        msg = r.text[:400]
        if "-1022" in msg or "Signature" in msg:
            raise RuntimeError(
                f"Binance {r.status_code}: {msg}\n"
                "→ İmza geçersiz (-1022). Çoğu zaman SECRET KEY yanlış.\n"
                "  1) Binance → API Management → Secret Key'i YENİDEN kopyala\n"
                "  2) API Key ile Secret Key yer değiştirmiş olmasın\n"
                "  3) Tırnak/boşluk olmasın: SECRET = \"abc...\"  (tek çift tırnak)\n"
                "  4) Enable Spot & Margin Trading açık olsun\n"
                "  5) Gerekirse yeni API key oluştur"
            )
        raise RuntimeError(f"Binance {r.status_code}: {msg}")
    return r.json()


def load_lot_filters(rest_base: str) -> dict[str, dict[str, float]]:
    info = get_json(f"{rest_base}/api/v3/exchangeInfo")
    out: dict[str, dict[str, float]] = {}
    for s in info.get("symbols", []):
        sym = s["symbol"]
        step = min_qty = min_notional = 0.0
        for f in s.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step = float(f["stepSize"])
                min_qty = float(f["minQty"])
            elif f.get("filterType") in {"MIN_NOTIONAL", "NOTIONAL"}:
                min_notional = float(f.get("minNotional") or f.get("notional") or 0)
        out[sym] = {"stepSize": step, "minQty": min_qty, "minNotional": min_notional}
    return out


def round_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    precision = max(0, int(round(-math.log10(step)))) if step < 1 else 0
    floored = math.floor(qty / step) * step
    return float(f"{floored:.{precision}f}")


def get_spot_price(rest_base: str, symbol: str) -> float:
    t = get_json(f"{rest_base}/api/v3/ticker/price", {"symbol": symbol})
    return float(t["price"])


class BinanceAccount:
    def __init__(self, cfg: dict[str, Any], live: bool):
        trade_cfg = cfg.get("trade") or {}
        self.cfg = cfg
        self.live = live
        self.api_key, self.api_secret = resolve_api_keys()
        self.trade_base = trade_cfg.get("trade_base") or "https://api.binance.com"
        self.rest_base = cfg.get("rest_base") or "https://data-api.binance.vision"
        self.recv = int(trade_cfg.get("recv_window") or 60000)
        self.filters = load_lot_filters(self.rest_base)
        self._paper_usdt = float(env("PAPER_USDT") or 1000)
        self._paper_balances: dict[str, float] = {"USDT": self._paper_usdt}

        if self.live and not (self.api_key and self.api_secret):
            raise SystemExit(
                "LIVE için BINANCE_API_KEY + BINANCE_API_SECRET gerekli "
                "(env veya dosyadaki HARDCODE alanları)"
            )
        if self.live:
            if len(self.api_secret) < 20:
                raise SystemExit(
                    f"[HATA] Secret Key çok kısa ({len(self.api_secret)} karakter). "
                    "API Key değil, Secret Key yapıştırdığından emin ol."
                )
            print(
                f"[keys] len key={len(self.api_key)} secret={len(self.api_secret)} "
                f"(secret son 4: …{self.api_secret[-4:]})"
            )

    def free_usdt(self) -> float:
        if not self.live:
            return float(self._paper_balances.get("USDT", 0))
        acc = signed_request(
            "GET",
            self.trade_base,
            "/api/v3/account",
            self.api_key,
            self.api_secret,
            recv_window=self.recv,
        )
        for b in acc.get("balances", []):
            if b.get("asset") == "USDT":
                return float(b.get("free") or 0)
        return 0.0

    def free_asset(self, asset: str) -> float:
        asset = (asset or "").upper()
        if not self.live:
            return float(self._paper_balances.get(asset, 0))
        acc = signed_request(
            "GET",
            self.trade_base,
            "/api/v3/account",
            self.api_key,
            self.api_secret,
            recv_window=self.recv,
        )
        for b in acc.get("balances", []):
            if b.get("asset") == asset:
                return float(b.get("free") or 0)
        return 0.0

    def market_buy_quote(self, symbol: str, quote_usdt: float) -> dict[str, Any]:
        price = get_spot_price(self.rest_base, symbol)
        if not self.live:
            qty = quote_usdt / price if price > 0 else 0
            base = symbol.replace("USDT", "")
            self._paper_balances["USDT"] = self._paper_balances.get("USDT", 0) - quote_usdt
            self._paper_balances[base] = self._paper_balances.get(base, 0) + qty
            return {
                "symbol": symbol,
                "side": "BUY",
                "status": "FILLED",
                "price": price,
                "executedQty": str(qty),
                "cummulativeQuoteQty": str(quote_usdt),
                "paper": True,
            }
        return signed_request(
            "POST",
            self.trade_base,
            "/api/v3/order",
            self.api_key,
            self.api_secret,
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": f"{quote_usdt:.2f}",
            },
            recv_window=self.recv,
        )

    def book_ticker(self, symbol: str) -> tuple[float, float]:
        try:
            t = get_json(f"{self.rest_base}/api/v3/ticker/bookTicker", {"symbol": symbol})
            return float(t["bidPrice"]), float(t["askPrice"])
        except Exception:  # noqa: BLE001
            px = get_spot_price(self.rest_base, symbol)
            return px, px

    def spread_pct(self, symbol: str) -> float:
        bid, ask = self.book_ticker(symbol)
        if bid <= 0 or ask <= 0:
            return 0.2
        return max(0.0, (ask / bid - 1.0) * 100.0)

    def smart_buy_quote(self, symbol: str, quote_usdt: float, *, use_limit: bool, wait_sec: float) -> dict[str, Any]:
        """Önce maker limit dene; dolmazsa market (komisyon/kayma azaltma)."""
        if not use_limit or not self.live:
            return self.market_buy_quote(symbol, quote_usdt)
        bid, ask = self.book_ticker(symbol)
        px = bid if bid > 0 else ask
        if px <= 0:
            return self.market_buy_quote(symbol, quote_usdt)
        qty = quote_usdt / px
        filt = self.filters.get(symbol) or {}
        step = float(filt.get("stepSize") or 0)
        qty = round_step(qty, step) if step else qty
        if qty <= 0:
            return self.market_buy_quote(symbol, quote_usdt)
        precision = max(0, int(round(-math.log10(step)))) if 0 < step < 1 else 0
        qstr = f"{qty:.{precision}f}"
        p_prec = max(0, len(str(bid).split(".")[-1]) if "." in str(bid) else 4)
        pstr = f"{px:.{min(8, p_prec)}f}"
        try:
            order = signed_request(
                "POST",
                self.trade_base,
                "/api/v3/order",
                self.api_key,
                self.api_secret,
                {
                    "symbol": symbol,
                    "side": "BUY",
                    "type": "LIMIT",
                    "timeInForce": "GTC",
                    "quantity": qstr,
                    "price": pstr,
                },
                recv_window=self.recv,
            )
            oid = order.get("orderId")
            time.sleep(max(0.5, wait_sec))
            if oid:
                st = signed_request(
                    "GET",
                    self.trade_base,
                    "/api/v3/order",
                    self.api_key,
                    self.api_secret,
                    {"symbol": symbol, "orderId": oid},
                    recv_window=self.recv,
                )
                status = str(st.get("status") or "")
                filled = float(st.get("executedQty") or 0)
                if status == "FILLED" or filled > 0:
                    # kalanı iptal
                    if status != "FILLED":
                        try:
                            signed_request(
                                "DELETE",
                                self.trade_base,
                                "/api/v3/order",
                                self.api_key,
                                self.api_secret,
                                {"symbol": symbol, "orderId": oid},
                                recv_window=self.recv,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    quote_filled = float(st.get("cummulativeQuoteQty") or filled * px)
                    if status == "FILLED":
                        return st
                    # kısmi → kalan market
                    remain_quote = max(0.0, quote_usdt - quote_filled)
                    if remain_quote >= 11:
                        mkt = self.market_buy_quote(symbol, remain_quote)
                        return {
                            "symbol": symbol,
                            "side": "BUY",
                            "status": "FILLED",
                            "executedQty": str(filled + float(mkt.get("executedQty") or 0)),
                            "cummulativeQuoteQty": str(
                                quote_filled + float(mkt.get("cummulativeQuoteQty") or 0)
                            ),
                            "price": px,
                            "limit_partial": True,
                        }
                    return st
                # hiç dolmadı → iptal + market
                try:
                    signed_request(
                        "DELETE",
                        self.trade_base,
                        "/api/v3/order",
                        self.api_key,
                        self.api_secret,
                        {"symbol": symbol, "orderId": oid},
                        recv_window=self.recv,
                    )
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        return self.market_buy_quote(symbol, quote_usdt)

    def market_sell_qty(self, symbol: str, qty: float) -> dict[str, Any]:
        filt = self.filters.get(symbol) or {}
        step = float(filt.get("stepSize") or 0)
        qty = round_step(qty, step) if step else qty
        if qty <= 0:
            raise RuntimeError(f"qty=0 {symbol}")
        price = get_spot_price(self.rest_base, symbol)
        if not self.live:
            base = symbol.replace("USDT", "")
            have = self._paper_balances.get(base, 0)
            qty = min(qty, have)
            self._paper_balances[base] = have - qty
            self._paper_balances["USDT"] = self._paper_balances.get("USDT", 0) + qty * price
            return {
                "symbol": symbol,
                "side": "SELL",
                "status": "FILLED",
                "price": price,
                "executedQty": str(qty),
                "cummulativeQuoteQty": str(qty * price),
                "paper": True,
            }
        step = float(filt.get("stepSize") or 0.0001)
        precision = max(0, int(round(-math.log10(step)))) if step < 1 else 0
        qstr = f"{qty:.{precision}f}"
        return signed_request(
            "POST",
            self.trade_base,
            "/api/v3/order",
            self.api_key,
            self.api_secret,
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": qstr,
            },
            recv_window=self.recv,
        )


def load_positions() -> dict[str, Any]:
    if POS_PATH.exists():
        return load_json(POS_PATH)
    return {"positions": {}, "updated_at": None}


def save_positions(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    save_json(POS_PATH, state)


def round_trip_fee_pct(trade_cfg: dict[str, Any], *, bnb_discount: bool = False) -> float:
    side = float(trade_cfg.get("fee_rate_pct") or 0.10)
    if bnb_discount:
        side *= 0.75  # ~%25 BNB indirimi
    return side * 2.0


def min_profit_after_fees_pct(
    trade_cfg: dict[str, Any],
    *,
    bnb_discount: bool = False,
    spread_pct: float = 0.0,
) -> float:
    buf = float(trade_cfg.get("fee_buffer_pct") or 0.25)
    floor = float(trade_cfg.get("min_net_tp_pct") or 0.55)
    need = round_trip_fee_pct(trade_cfg, bnb_discount=bnb_discount) + buf
    if trade_cfg.get("spread_tp_boost", True) and spread_pct > 0:
        need += min(0.8, spread_pct * 0.5)
    return max(floor, need)


def dynamic_trail_pct(trade_cfg: dict[str, Any], peak_gain_pct: float) -> float:
    base = float(trade_cfg.get("peak_trail_pct") or 0.50)
    tight = float(trade_cfg.get("peak_trail_tight_pct") or 0.30)
    after = float(trade_cfg.get("trail_tighten_after_pct") or 1.0)
    return tight if peak_gain_pct >= after else base


def max_positions_for_balance(usdt: float, trade_cfg: dict[str, Any]) -> int:
    hard = int(trade_cfg.get("max_positions") or 10)
    min_order = float(trade_cfg.get("min_order_usdt") or 12)
    deploy = float(trade_cfg.get("deploy_pct") or 0.95)
    n = int((usdt * deploy) // max(min_order, 1))
    return max(1, min(hard, max(n, 1 if usdt >= min_order else 0)))


SECTOR_MAP: dict[str, str] = {
    "PEPE": "meme", "DOGE": "meme", "SHIB": "meme", "FLOKI": "meme", "BONK": "meme",
    "WIF": "meme", "BOME": "meme", "TRUMP": "meme", "NEIRO": "meme", "MEME": "meme",
    "DOGS": "meme", "MEW": "meme", "PNUT": "meme", "GOAT": "meme",
    "SOL": "l1", "ADA": "l1", "AVAX": "l1", "NEAR": "l1", "SUI": "l1", "APT": "l1",
    "SEI": "l1", "TIA": "l1", "INJ": "l1", "TON": "l1", "TRX": "l1", "XRP": "l1",
    "FET": "ai", "RENDER": "ai", "RNDR": "ai", "TAO": "ai", "WLD": "ai", "AI": "ai",
    "ARKM": "ai", "AIXBT": "ai",
    "UNI": "defi", "AAVE": "defi", "CRV": "defi", "MKR": "defi", "SNX": "defi",
    "COMP": "defi", "DYDX": "defi", "JUP": "defi", "CAKE": "defi",
    "LINK": "oracle", "PYTH": "oracle", "API3": "oracle",
    "FIL": "storage", "AR": "storage",
    "LTC": "payment", "BCH": "payment", "XLM": "payment",
}


def coin_sector(base: str) -> str:
    return SECTOR_MAP.get((base or "").upper(), "other")


def sync_positions_with_exchange(account: BinanceAccount, state: dict[str, Any]) -> list[str]:
    """Hesapta olmayan hayalet pozisyonları temizle; qty'yi free bakiyeye çek."""
    notes: list[str] = []
    if not account.live:
        return notes
    positions = state.get("positions") or {}
    drop: list[str] = []
    for base, pos in list(positions.items()):
        try:
            free = account.free_asset(base)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"sync fail {base}: {exc}")
            continue
        if free <= 0:
            notes.append(f"🧹 sync: {base} hesapta yok → silindi")
            drop.append(base)
            continue
        q = float(pos.get("qty") or 0)
        if free < q * 0.98:
            pos["qty"] = free
            notes.append(f"🧹 sync: {base} qty {q:.6g}→{free:.6g}")
    for b in drop:
        positions.pop(b, None)
    state["positions"] = positions
    return notes


def build_daily_pnl_report(trade_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """trades.jsonl → bugünün winrate / net PnL (fee düşülmüş tahmini)."""
    trade_cfg = trade_cfg or {}
    fee = round_trip_fee_pct(trade_cfg) / 100.0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sells: list[float] = []
    buys = 0
    if TRADE_LOG.exists():
        with TRADE_LOG.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = str(row.get("ts") or "")
                if not ts.startswith(today):
                    continue
                act = str(row.get("action") or "")
                if act == "BUY":
                    buys += 1
                elif act.startswith("SELL"):
                    pnl = float(row.get("pnl_pct") or 0) / 100.0
                    sells.append(pnl - fee)
    wins = sum(1 for x in sells if x > 0)
    summary = {
        "date": today,
        "buys": buys,
        "sells": len(sells),
        "wins": wins,
        "winrate_pct": round(100.0 * wins / len(sells), 1) if sells else 0.0,
        "avg_net_pnl_pct": round(100.0 * (sum(sells) / len(sells)), 3) if sells else 0.0,
        "sum_net_pnl_pct": round(100.0 * sum(sells), 3) if sells else 0.0,
        "fee_roundtrip_pct": round(fee * 100, 3),
    }
    save_json(PNL_PATH, {"updated_at": now_iso(), "summary": summary})
    return summary


def manage_exits(account: BinanceAccount, state: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    """
    1) Entry -%0.5 hard stop
    2) Dinamik trail (kâr büyüdükçe sıkılaşır)
    3) +TP (komisyon+spread üstü)
    4) Zaman stop (~30 dk)
    """
    notes: list[str] = []
    positions: dict[str, Any] = state.get("positions") or {}
    trade_cfg = cfg.get("trade") or {}
    hard_stop = float(trade_cfg.get("hard_stop_pct") or 0.50)
    quick_tp = float(trade_cfg.get("quick_tp_pct") or 1.20)
    time_stop_m = float(trade_cfg.get("time_stop_minutes") or 30)
    bnb_ok = bool(trade_cfg.get("bnb_fee_discount", True)) and account.free_asset("BNB") >= 0.01
    min_gross = min_profit_after_fees_pct(trade_cfg, bnb_discount=bnb_ok)
    rest = account.rest_base
    closed: list[str] = []
    now = datetime.now(timezone.utc)

    def do_sell(base: str, pos: dict[str, Any], price: float, reason: str, action: str) -> bool:
        symbol = pos["symbol"]
        qty = float(pos["qty"])
        entry = float(pos["entry"])
        try:
            if account.live:
                free = account.free_asset(base)
                if free > 0:
                    qty = min(qty, free)
            if qty <= 0:
                notes.append(f"⚠ {base} bakiye 0 → silindi ({reason})")
                closed.append(base)
                return True
            order = account.market_sell_qty(symbol, qty)
            fill_qty = float(order.get("executedQty") or qty)
            pnl = (price / entry - 1) * 100 if entry else 0
            notes.append(f"{reason} {base} @ {price} PnL%{pnl:+.2f}")
            log_trade(
                {
                    "ts": now_iso(),
                    "action": action,
                    "base": base,
                    "symbol": symbol,
                    "price": price,
                    "qty": fill_qty,
                    "entry": entry,
                    "peak": pos.get("peak"),
                    "pnl_pct": round(pnl, 3),
                    "live": account.live,
                    "order": order,
                }
            )
            closed.append(base)
            return True
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            notes.append(f"{action} fail {base}: {exc}")
            if "-2010" in err or "insufficient" in err.lower():
                notes.append(f"⚠ {base} hayalet → silindi")
                closed.append(base)
                return True
            return False

    for base, pos in list(positions.items()):
        symbol = pos["symbol"]
        try:
            price = get_spot_price(rest, symbol)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"price fail {symbol}: {exc}")
            continue
        entry = float(pos["entry"])
        if entry <= 0 or price <= 0:
            continue
        peak = float(pos.get("peak") or entry)
        if price > peak:
            peak = price
            pos["peak"] = peak
        pnl_pct = (price / entry - 1.0) * 100.0
        peak_gain = (peak / entry - 1.0) * 100.0
        trail = dynamic_trail_pct(trade_cfg, peak_gain)
        from_peak_pct = (price / peak - 1.0) * 100.0 if peak > 0 else 0.0

        # spread'e göre TP eşiği
        try:
            sp = account.spread_pct(symbol)
        except Exception:  # noqa: BLE001
            sp = 0.0
        tp_need = max(quick_tp, min_profit_after_fees_pct(trade_cfg, bnb_discount=bnb_ok, spread_pct=sp))

        if pnl_pct <= -hard_stop:
            do_sell(base, pos, price, f"🛑 STOP%-{hard_stop}", "SELL_STOP")
            continue

        if peak_gain >= min_gross and from_peak_pct <= -trail:
            do_sell(
                base,
                pos,
                price,
                f"📉 TRAIL%{trail:.2f} zirve%{peak_gain:.1f}→%{from_peak_pct:.1f}",
                "SELL_TRAIL",
            )
            continue

        if pnl_pct >= tp_need:
            do_sell(base, pos, price, f"🎯 TP%+{pnl_pct:.2f} (≥{tp_need:.2f})", "SELL_TP")
            continue

        # zaman stop
        opened = pos.get("opened_at")
        if opened and time_stop_m > 0:
            try:
                age_m = (now - datetime.fromisoformat(str(opened))).total_seconds() / 60.0
            except ValueError:
                age_m = 0.0
            if age_m >= time_stop_m:
                # kâr komisyon üstündeyse veya küçük zarar → çık (büyük zararda hard stop zaten)
                if pnl_pct >= min_gross or (-hard_stop < pnl_pct <= 0.15):
                    do_sell(base, pos, price, f"⏱ TIME {age_m:.0f}dk PnL%{pnl_pct:+.2f}", "SELL_TIME")
                    continue

    for b in closed:
        positions.pop(b, None)
    state["positions"] = positions
    return notes


def manage_entries(
    account: BinanceAccount,
    state: dict[str, Any],
    rows: list[Analysis],
    cfg: dict[str, Any],
    regime: dict[str, Any] | None = None,
) -> list[str]:
    """UÇ + CEX onay + sektör çeşitliliği + bakiyeye göre slot + eşit bölüşüm."""
    notes: list[str] = []
    positions: dict[str, Any] = state.get("positions") or {}
    trade_cfg = cfg.get("trade") or {}
    regime = regime or {}
    min_order = float(trade_cfg.get("min_order_usdt") or 12)
    deploy = float(trade_cfg.get("deploy_pct") or 0.95)
    require_uc = bool(trade_cfg.get("require_uc", True))
    require_cex = int(trade_cfg.get("require_cex_min") or 2)
    also_izle = bool(trade_cfg.get("also_buy_izle", False))
    max_buy = int(trade_cfg.get("max_buy_per_cycle") or 3)
    max_sector = int(trade_cfg.get("max_per_sector") or 2)
    hard_stop = float(trade_cfg.get("hard_stop_pct") or 0.50)
    quick_tp = float(trade_cfg.get("quick_tp_pct") or 1.20)
    use_limit = bool(trade_cfg.get("use_limit_orders", True))
    limit_wait = float(trade_cfg.get("limit_wait_sec") or 3.0)

    if regime.get("block_al"):
        notes.append("⛔ BTC dump — yeni AL kapalı (hard block)")
        return notes

    free = account.free_usdt()
    bnb_ok = bool(trade_cfg.get("bnb_fee_discount", True)) and account.free_asset("BNB") >= 0.01
    if bnb_ok:
        notes.append("💎 BNB fee indirimi aktif (~%25)")
    min_gross = min_profit_after_fees_pct(trade_cfg, bnb_discount=bnb_ok)
    max_pos = max_positions_for_balance(free, trade_cfg)
    # multi-cex kapalıysa CEX şartını gevşet
    mc_on = bool((cfg.get("multi_cex") or {}).get("enabled", True))
    cex_need = require_cex if mc_on else 0
    scanned_n = len(((regime or {}).get("multi_cex") or {}).get("scanned") or [])
    if mc_on and scanned_n == 0 and cex_need > 0:
        notes.append("CEX tarama başarısız/boş → CEX şartı bu tur gevşetildi")
        cex_need = 0

    notes.append(
        f"sinyal: AL={sum(1 for r in rows if r.action=='AL')} · "
        f"USDT={free:.2f} · slot_max={max_pos} · "
        f"UÇ_zorunlu={require_uc} CEX≥{cex_need} · fee≥%{min_gross:.2f}"
    )

    slots = max_pos - len(positions)
    if slots <= 0:
        notes.append(f"pozisyon dolu ({len(positions)}/{max_pos}) — bakiye-slot kuralı")
        return notes

    sector_count: dict[str, int] = {}
    for b in positions:
        s = coin_sector(b)
        sector_count[s] = sector_count.get(s, 0) + 1

    def is_buyable(r: Analysis) -> bool:
        if r.base in positions or not r.price:
            return False
        if r.action != "AL" and not (also_izle and r.action == "İZLE"):
            return False
        if require_uc and not r.is_uc:
            return False
        cex_n = int((r.layers or {}).get("cex_count") or 0)
        if cex_need > 0 and cex_n < cex_need:
            return False
        qv = float(r.quote_volume_24h or 0)
        if qv < float(cfg.get("min_quote_volume_usdt") or 0):
            return False
        sec = coin_sector(r.base)
        if sector_count.get(sec, 0) >= max_sector:
            return False
        return True

    cands = [r for r in rows if is_buyable(r)]
    cands.sort(key=lambda r: (0 if r.is_uc else 1, -int((r.layers or {}).get("cex_count") or 0), -r.pump_score, -r.score))
    # sektör çeşitliliği seçerken de uygula
    picked: list[Analysis] = []
    trial_sec = dict(sector_count)
    for r in cands:
        if len(picked) >= min(slots, max_buy):
            break
        sec = coin_sector(r.base)
        if trial_sec.get(sec, 0) >= max_sector:
            continue
        picked.append(r)
        trial_sec[sec] = trial_sec.get(sec, 0) + 1
    cands = picked

    if not cands:
        notes.append("alım yok — UÇ+CEX+sektör filtresinden geçen aday yok")
        near = sorted(
            [r for r in rows if r.action == "AL"],
            key=lambda r: (-int((r.layers or {}).get("cex_count") or 0), -r.pump_score),
        )[:6]
        for r in near:
            notes.append(
                f"  elendi: {r.base} UÇ={r.is_uc} CEX×{(r.layers or {}).get('cex_count', 0)} "
                f"uç={r.pump_score:.0f} sektör={coin_sector(r.base)}"
            )
        return notes

    budget = free * deploy
    fee_reserve = budget * (round_trip_fee_pct(trade_cfg, bnb_discount=bnb_ok) / 100.0)
    budget = max(0.0, budget - fee_reserve)
    per = budget / len(cands)
    if per < min_order:
        n = int(budget // min_order)
        if n <= 0:
            notes.append(f"USDT yetersiz free={free:.2f} (min {min_order})")
            return notes
        cands = cands[:n]
        per = budget / len(cands)

    notes.append(f"AL planı: {len(cands)} coin × ~{per:.2f} USDT EŞİT (komisyon payı ayrıldı)")

    for sig in cands:
        symbol = sig.symbol
        filt = account.filters.get(symbol) or {}
        min_notional = float(filt.get("minNotional") or min_order)
        quote = max(per, min_notional)
        if account.free_usdt() < quote:
            notes.append(f"bakiye bitti, {sig.base} atlandı")
            break
        try:
            sp = account.spread_pct(symbol)
            tp_need = max(quick_tp, min_profit_after_fees_pct(trade_cfg, bnb_discount=bnb_ok, spread_pct=sp))
            order = account.smart_buy_quote(
                symbol, quote, use_limit=use_limit, wait_sec=limit_wait
            )
            fill_quote = float(order.get("cummulativeQuoteQty") or quote)
            fill_qty = float(order.get("executedQty") or 0)
            px = float(order.get("price") or 0)
            if fill_qty <= 0 and px > 0:
                fill_qty = fill_quote / px
            if fill_qty <= 0:
                px = get_spot_price(account.rest_base, symbol)
                fill_qty = fill_quote / px
            entry = fill_quote / fill_qty if fill_qty else get_spot_price(account.rest_base, symbol)
            stop = round(entry * (1.0 - hard_stop / 100.0), 10)
            tp1 = round(entry * (1.0 + tp_need / 100.0), 10)
            tp2 = round(entry * (1.0 + tp_need * 1.6 / 100.0), 10)
            positions[sig.base] = {
                "symbol": symbol,
                "entry": entry,
                "peak": entry,
                "qty": fill_qty,
                "stop": stop,
                "tp1": tp1,
                "tp2": tp2,
                "score": sig.score,
                "pump_score": sig.pump_score,
                "is_uc": sig.is_uc,
                "cex_count": int((sig.layers or {}).get("cex_count") or 0),
                "sector": coin_sector(sig.base),
                "spread_pct": round(sp, 3),
                "sold_tp1": False,
                "opened_at": now_iso(),
                "reasons": sig.reasons[:8],
                "signal_action": sig.action,
            }
            sector_count[coin_sector(sig.base)] = sector_count.get(coin_sector(sig.base), 0) + 1
            tag = "🚀" if sig.is_uc else "🟢"
            notes.append(
                f"{tag} AL {sig.base} ~{fill_quote:.2f} USDT @ {entry:.8g} "
                f"CEX×{positions[sig.base]['cex_count']} "
                f"sektör={positions[sig.base]['sector']} "
                f"SL%-{hard_stop} TP%+{tp_need:.2f}"
            )
            log_trade(
                {
                    "ts": now_iso(),
                    "action": "BUY",
                    "base": sig.base,
                    "symbol": symbol,
                    "price": entry,
                    "qty": fill_qty,
                    "quote": fill_quote,
                    "stop": stop,
                    "tp1": tp1,
                    "score": sig.score,
                    "pump_score": sig.pump_score,
                    "is_uc": sig.is_uc,
                    "cex_count": positions[sig.base]["cex_count"],
                    "live": account.live,
                }
            )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"AL fail {sig.base}: {exc}")

    state["positions"] = positions
    return notes


def print_portfolio(account: BinanceAccount, state: dict[str, Any], max_pos: int = 10) -> None:
    positions = state.get("positions") or {}
    print(f"\n═══ PORTFÖY ({len(positions)}/{max_pos}) · USDT free={account.free_usdt():.2f} ═══")
    if not positions:
        print("  (boş)")
        return
    for base, pos in positions.items():
        try:
            px = get_spot_price(account.rest_base, pos["symbol"])
        except Exception:  # noqa: BLE001
            px = float(pos["entry"])
        entry = float(pos["entry"])
        peak = float(pos.get("peak") or entry)
        pnl = (px / entry - 1) * 100
        uc = "🚀" if pos.get("is_uc") else "  "
        print(
            f"  {uc}{base:<8} qty={float(pos['qty']):.6g}  entry={entry:.6g}  "
            f"now={px:.6g} peak={peak:.6g} PnL%{pnl:+.2f}  "
            f"sektör={pos.get('sector', '?')} CEX×{pos.get('cex_count', 0)}"
        )


def run_trade_cycle(
    account: BinanceAccount,
    rows: list[Analysis],
    cfg: dict[str, Any],
    regime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = load_positions()
    print("\n[sync] hesap ↔ pozisyon…", flush=True)
    for note in sync_positions_with_exchange(account, state):
        print(" ", note)
    save_positions(state)

    print("\n[exit] stop / trail / tp / time…", flush=True)
    for note in manage_exits(account, state, cfg):
        print(" ", note)
    save_positions(state)

    print("\n[entry] UÇ+CEX alımlar…", flush=True)
    for note in manage_entries(account, state, rows, cfg, regime=regime):
        print(" ", note)
    save_positions(state)

    free = account.free_usdt()
    max_pos = max_positions_for_balance(free, cfg.get("trade") or {})
    print_portfolio(account, state, max_pos=max(max_pos, len(state.get("positions") or {})))

    pnl = build_daily_pnl_report(cfg.get("trade") or {})
    print(
        f"\n[pnl bugün] satiş={pnl['sells']} winrate%{pnl['winrate_pct']} "
        f"ort_net%{pnl['avg_net_pnl_pct']} toplam_net%{pnl['sum_net_pnl_pct']}",
        flush=True,
    )
    return state


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
    parser = argparse.ArgumentParser(description="Binance dipten erken AL radarı v2 + al/sat")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Emir atma (paper) / telegram dry")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--config", default=None)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--backtest-symbols", type=int, default=40)
    parser.add_argument("--backtest-hold", type=int, default=12)
    parser.add_argument("--skip-multi-cex", action="store_true", help="Sadece Binance (hızlı)")
    parser.add_argument("--fast", action="store_true", help="Her CEX'te az sembol (test)")
    parser.add_argument(
        "--trade",
        action="store_true",
        help="AL sinyallerinde Binance spot al/sat (varsayılan paper; --live ile gerçek)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Gerçek Binance emri at (API key gerekir). LIVE=1 ile aynı.",
    )
    parser.add_argument("--no-trade", action="store_true", help="Al/sat kapalı (sadece radar)")
    args = parser.parse_args()

    cfg, cfg_src = load_config(args.config)
    if args.skip_multi_cex:
        cfg.setdefault("multi_cex", {})["enabled"] = False
    if args.fast:
        cfg.setdefault("multi_cex", {})["max_symbols_per_exchange"] = 25
        cfg.setdefault("multi_cex", {})["ohlcv_limit"] = 24
    print(f"[config] {cfg_src}")
    print(f"[output] {OUTPUT_PATH}")
    mc = cfg.get("multi_cex") or {}
    if mc.get("enabled", True):
        ids = mc.get("ids") or MULTI_CEX_IDS
        print(f"[cex] {len(ids)} borsa hedefleniyor")
    workers = int(args.workers or cfg.get("workers") or 16)

    if args.backtest:
        run_backtest(cfg, n_symbols=args.backtest_symbols, hold_hours=args.backtest_hold)
        return 0

    # --- trade mode ---
    trade_cfg = cfg.setdefault("trade", dict(DEFAULT_CONFIG["trade"]))
    api_key, api_secret = resolve_api_keys()
    keys_ok = bool(api_key and api_secret)
    want_trade = (
        bool(
            args.trade
            or trade_cfg.get("enabled")
            or TRADE_HARDCODE
            or keys_ok
        )
        and not args.no_trade
    )
    live_env = (env("LIVE") or "0") == "1"
    # Key dolu + trade açık → otomatik LIVE (--dry-run hariç)
    want_live = (
        bool(args.live or live_env or LIVE_HARDCODE or (keys_ok and want_trade))
        and not args.dry_run
    )
    trade_live = bool(want_trade and want_live)
    account: BinanceAccount | None = None

    print("=" * 60)
    if keys_ok:
        print(f"[keys] API key OK (…{api_key[-4:]})")
    else:
        print("[keys] API key BOŞ — BINANCE_API_KEY_HARDCODE doldur!")
    print(
        f"[flags] trade={want_trade} live={want_live} "
        f"(LIVE_HARDCODE={LIVE_HARDCODE} TRADE_HARDCODE={TRADE_HARDCODE})"
    )
    print("=" * 60)

    if want_trade and not keys_ok and not args.dry_run:
        raise SystemExit(
            "\n"
            "╔════════════════════════════════════════════════════════╗\n"
            "║  API KEY BOŞ — alım yapılamaz                          ║\n"
            "║                                                        ║\n"
            "║  Dosyanın ÜSTÜNDE şu 2 satırı doldur:                  ║\n"
            "║    BINANCE_API_KEY_HARDCODE = \"gerçek_api_key\"         ║\n"
            "║    BINANCE_API_SECRET_HARDCODE = \"gerçek_secret\"       ║\n"
            "║                                                        ║\n"
            "║  BURAYA_API_KEY yazısını sil, kendi key'ini yaz.       ║\n"
            "║  Kaydet →  python allbinancee.py --once                ║\n"
            "╚════════════════════════════════════════════════════════╝\n"
        )

    if want_trade:
        if trade_live:
            print("[MODE] ⚠️  LIVE Binance spot — GERÇEK PARA / GERÇEK EMRİ")
        else:
            print("[MODE] PAPER — Binance hesabında alım GÖRÜNMEZ")
            print("       --dry-run kapalı mı? Key dolu mu?")
        account = BinanceAccount(cfg, live=trade_live)
        try:
            bal = account.free_usdt()
            print(f"[account] USDT free ≈ {bal:.2f}")
            if trade_live and bal < float(trade_cfg.get("min_order_usdt") or 11):
                print(
                    f"[uyarı] USDT bakiyesi düşük ({bal:.2f}) — "
                    "min ~11 USDT spot serbest bakiye gerekir",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[account HATA] bakiye okunamadı: {exc}", file=sys.stderr)
            if trade_live:
                raise SystemExit(
                    "\nAPI bağlanamadı. Kontrol et:\n"
                    "  1) Key/Secret doğru mu?\n"
                    "  2) Binance → API → Enable Spot & Margin Trading AÇIK mı?\n"
                    "  3) IP kısıtı varsa PC IP ekle (veya kısıtı kapat)\n"
                ) from exc
        print(f"[positions] {POS_PATH}")
    else:
        print("[MODE] sadece radar — trade kapalı")

    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    tg_dry = bool(args.dry_run) or args.no_telegram or not (token and chat_id)
    if tg_dry and not args.dry_run and not args.no_telegram:
        print("[info] Telegram env yok → telegram dry-run", file=sys.stderr)

    poll = int(cfg.get("poll_seconds") or 180)
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"alerted": {}}

    while True:
        t0 = time.time()
        rows, regime = run_scan(cfg, workers)
        visible = [r for r in rows if r.action != "YOK"]
        print("\n" + format_report(visible, args.top, regime) + "\n")

        pos_state: dict[str, Any] | None = None
        if want_trade and account is not None:
            try:
                pos_state = run_trade_cycle(account, rows, cfg)
            except Exception as exc:  # noqa: BLE001
                print(f"[trade HATA] {exc}", file=sys.stderr)

        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_sec": round(time.time() - t0, 1),
            "regime": regime,
            "trade": {
                "enabled": want_trade,
                "live": trade_live,
                "open_positions": len((pos_state or {}).get("positions") or {}) if pos_state else None,
            },
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
            if telegram_send(token or "", chat_id or "", msg, dry_run=tg_dry):
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
