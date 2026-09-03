#!/usr/bin/env python3
"""
CEX bot-sürüsü / erken pump hazırlığı tarayıcısı (Binance Spot public API).

Yakalamaya çalıştığı pattern:
  - Saatlik binlerce işlem (bot swarm)
  - Benzer lot boyutları (makine gibi tekrar)
  - Agresif taker alış baskısı
  - Hacim ivmesi yüksek ama fiyat henüz %30–80'e patlamamış

Kullanım:
  python3 cex_bot_swarm_detector.py --once --dry-run
  python3 cex_bot_swarm_detector.py              # sürekli + Telegram
  python3 cex_bot_swarm_detector.py --symbol PROMUSDT --once --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "bot_swarm.json"
STATE_PATH = ROOT / "output" / "bot_swarm_state.json"
OUT_PATH = ROOT / "output" / "bot_swarm_alerts.jsonl"

# data-api.binance.vision: geo-restricted api.binance.com yerine public mirror
SPOT_BASE = "https://data-api.binance.vision"


@dataclass
class SwarmHit:
    symbol: str
    score: float
    price: float
    change_pct_24h: float
    quote_volume_24h: float
    trades_24h: int
    trades_per_hour_est: float
    recent_trades: int
    recent_window_sec: float
    trades_per_min: float
    taker_buy_ratio: float
    size_cv: float
    top_size_share: float
    vol_accel_1h: float
    avg_trade_usd: float
    reasons: list[str]


def env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if isinstance(v, str) and v.strip() else None


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_config() -> dict[str, Any]:
    defaults = {
        "poll_seconds": 90,
        "min_quote_volume_24h": 2_000_000,
        "max_change_pct": 25.0,
        "min_change_pct": -5.0,
        "min_trades_24h": 80_000,
        "min_trades_per_min": 40.0,
        "min_taker_buy_ratio": 0.55,
        "max_size_cv": 1.2,
        "min_top_size_share": 0.12,
        "min_vol_accel_1h": 1.8,
        "min_score": 55.0,
        "agg_trade_limit": 1000,
        "exclude_bases": [
            "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX",
            "LINK", "AVAX", "DOT", "LTC", "BCH", "NEAR", "APT", "SUI",
            "TON", "SHIB", "PEPE", "WIF", "USDC", "FDUSD", "USD1", "TUSD",
            "EUR", "TRY", "BUSD",
        ],
        "exclude_suffixes": ["UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"],
        "cooldown_sec": 1800,
        "top_n_candidates": 40,
        "telegram_min_score": 65.0,
    }
    if CONFIG_PATH.exists():
        raw = load_json(CONFIG_PATH)
        defaults.update(raw.get("settings") or raw)
    return defaults


def http_get(path: str, params: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    url = f"{SPOT_BASE}{path}"
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def telegram_send(token: str, chat_id: str, text: str, *, dry_run: bool) -> bool:
    if dry_run:
        print("--- DRY-RUN TELEGRAM ---\n" + text + "\n------------------------")
        return True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            print(f"[telegram] HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return False
        return True
    except requests.RequestException as exc:
        print(f"[telegram] {exc}", file=sys.stderr)
        return False


def is_excluded(symbol: str, cfg: dict[str, Any]) -> bool:
    if not symbol.endswith("USDT"):
        return True
    for suf in cfg.get("exclude_suffixes") or []:
        if symbol.endswith(suf):
            return True
    base = symbol[:-4]
    return base in set(cfg.get("exclude_bases") or [])


def fetch_tickers() -> list[dict[str, Any]]:
    data = http_get("/api/v3/ticker/24hr")
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected ticker payload: {type(data)}")
    return data


def fetch_agg_trades(symbol: str, limit: int) -> list[dict[str, Any]]:
    return http_get("/api/v3/aggTrades", {"symbol": symbol, "limit": limit})


def fetch_klines_1m(symbol: str, limit: int = 120) -> list[list[Any]]:
    return http_get("/api/v3/klines", {"symbol": symbol, "interval": "1m", "limit": limit})


def coeff_var(values: list[float]) -> float:
    if len(values) < 5:
        return 999.0
    mean = statistics.fmean(values)
    if mean <= 0:
        return 999.0
    return statistics.pstdev(values) / mean


def analyze_agg_trades(trades: list[dict[str, Any]]) -> dict[str, float]:
    if not trades:
        return {
            "recent_trades": 0,
            "window_sec": 0.0,
            "trades_per_min": 0.0,
            "taker_buy_ratio": 0.0,
            "size_cv": 999.0,
            "top_size_share": 0.0,
            "quote_usd": 0.0,
            "avg_trade_usd": 0.0,
        }

    ts0 = int(trades[0]["T"])
    ts1 = int(trades[-1]["T"])
    window_sec = max((ts1 - ts0) / 1000.0, 1.0)

    buy_quote = 0.0
    sell_quote = 0.0
    sizes: list[float] = []
    size_bucket = Counter()

    for t in trades:
        px = float(t["p"])
        qty = float(t["q"])
        quote = px * qty
        sizes.append(qty)
        # Binance: m=True → buyer is maker → agresif satıcı (taker sell)
        if t.get("m"):
            sell_quote += quote
        else:
            buy_quote += quote
        # boyutları kaba kovalara yuvarla (bot lot tekrarı)
        if qty > 0:
            bucket = round(qty, 4 if qty < 1 else 2)
            size_bucket[bucket] += 1

    total_q = buy_quote + sell_quote
    taker_buy = (buy_quote / total_q) if total_q > 0 else 0.5
    top_share = (size_bucket.most_common(1)[0][1] / len(trades)) if trades else 0.0
    tpm = len(trades) / (window_sec / 60.0)

    return {
        "recent_trades": float(len(trades)),
        "window_sec": window_sec,
        "trades_per_min": tpm,
        "taker_buy_ratio": taker_buy,
        "size_cv": coeff_var(sizes),
        "top_size_share": top_share,
        "quote_usd": total_q,
        "avg_trade_usd": (total_q / len(trades)) if trades else 0.0,
    }


def volume_accel_1h(klines: list[list[Any]]) -> float:
    """Son 60dk quote volume / önceki 60dk. >1 = ivme."""
    if len(klines) < 120:
        return 1.0
    # kline: [open_time, o, h, l, c, vol, close_time, quote_vol, trades, ...]
    recent = sum(float(k[7]) for k in klines[-60:])
    prev = sum(float(k[7]) for k in klines[-120:-60])
    if prev <= 0:
        return 99.0 if recent > 0 else 1.0
    return recent / prev


def score_candidate(metrics: dict[str, float], ticker: dict[str, Any], cfg: dict[str, Any]) -> SwarmHit:
    change = float(ticker["priceChangePercent"])
    qv = float(ticker["quoteVolume"])
    trades_24h = int(ticker["count"])
    price = float(ticker["lastPrice"])
    tph_est = trades_24h / 24.0

    reasons: list[str] = []
    score = 0.0

    tpm = metrics["trades_per_min"]
    if tpm >= cfg["min_trades_per_min"]:
        score += min(25.0, 10.0 + (tpm / cfg["min_trades_per_min"]) * 8.0)
        reasons.append(f"yoğun işlem {tpm:.0f}/dk")
    elif tpm >= cfg["min_trades_per_min"] * 0.6:
        score += 8.0
        reasons.append(f"orta yoğunluk {tpm:.0f}/dk")

    if trades_24h >= cfg["min_trades_24h"]:
        score += 10.0
        reasons.append(f"24s işlem {trades_24h:,}")

    tbr = metrics["taker_buy_ratio"]
    if tbr >= cfg["min_taker_buy_ratio"]:
        score += min(20.0, (tbr - 0.5) * 60.0)
        reasons.append(f"agresif alış %{tbr*100:.0f}")

    if metrics["size_cv"] <= cfg["max_size_cv"]:
        score += 12.0
        reasons.append(f"lot tekdüzeliği cv={metrics['size_cv']:.2f}")

    if metrics["top_size_share"] >= cfg["min_top_size_share"]:
        score += 10.0
        reasons.append(f"aynı lot tekrarı %{metrics['top_size_share']*100:.0f}")

    accel = metrics["vol_accel_1h"]
    if accel >= cfg["min_vol_accel_1h"]:
        score += min(18.0, accel * 5.0)
        reasons.append(f"1s hacim ivmesi x{accel:.1f}")

    # henüz patlamamış — erken yakalama bonusu
    if change < cfg["max_change_pct"]:
        score += 8.0
        reasons.append(f"fiyat erken %{change:.1f}")
    if 3.0 <= change <= 15.0:
        score += 5.0
        reasons.append("ısınma bandı %3–15")

    # çok küçük ortalama işlem + yüksek frekans = bot makinesi
    avg_usd = metrics["avg_trade_usd"]
    if avg_usd > 0 and avg_usd < 80 and tpm >= cfg["min_trades_per_min"] * 0.7:
        score += 8.0
        reasons.append(f"mikro lot ~${avg_usd:.0f}")

    return SwarmHit(
        symbol=ticker["symbol"],
        score=round(score, 1),
        price=price,
        change_pct_24h=round(change, 2),
        quote_volume_24h=round(qv, 0),
        trades_24h=trades_24h,
        trades_per_hour_est=round(tph_est, 0),
        recent_trades=int(metrics["recent_trades"]),
        recent_window_sec=round(metrics["window_sec"], 1),
        trades_per_min=round(tpm, 1),
        taker_buy_ratio=round(tbr, 3),
        size_cv=round(metrics["size_cv"], 3),
        top_size_share=round(metrics["top_size_share"], 3),
        vol_accel_1h=round(accel, 2),
        avg_trade_usd=round(avg_usd, 2),
        reasons=reasons,
    )


def pick_candidates(tickers: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tickers:
        sym = t["symbol"]
        if is_excluded(sym, cfg):
            continue
        try:
            change = float(t["priceChangePercent"])
            qv = float(t["quoteVolume"])
            count = int(t["count"])
        except (TypeError, ValueError, KeyError):
            continue
        if qv < cfg["min_quote_volume_24h"]:
            continue
        if change > cfg["max_change_pct"] or change < cfg["min_change_pct"]:
            continue
        if count < cfg["min_trades_24h"] * 0.35:
            # tamamen ölü çiftleri ele; ama erken swarm için eşiği düşük tut
            continue
        # yoğunluk skoru: işlem sayısı / sqrt(hacim) — bot swarm için proxy
        intensity = count / math.sqrt(max(qv, 1.0))
        out.append({**t, "_intensity": intensity})

    out.sort(key=lambda x: x["_intensity"], reverse=True)
    return out[: int(cfg["top_n_candidates"])]


def format_alert(hit: SwarmHit) -> str:
    reasons = " · ".join(hit.reasons[:5])
    return (
        f"🤖 <b>CEX Bot Sürüsü?</b>\n"
        f"<b>{hit.symbol}</b> skor <b>{hit.score:.0f}</b>\n"
        f"Fiyat: {hit.price} | 24s: <b>{hit.change_pct_24h:+.1f}%</b>\n"
        f"İşlem: ~{hit.trades_per_hour_est:,.0f}/saat | son pencere {hit.trades_per_min:.0f}/dk\n"
        f"Taker alış: %{hit.taker_buy_ratio*100:.0f} | lot cv={hit.size_cv:.2f}\n"
        f"1s hacim ivmesi: x{hit.vol_accel_1h:.1f} | ort işlem ~${hit.avg_trade_usd:.0f}\n"
        f"{reasons}\n"
        f"<i>Erken uyarı — %30–80 öncesi imza adayı; teyit gerekir.</i>"
    )


def append_alert(hit: SwarmHit) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(), **asdict(hit)}
    with OUT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def scan_symbol(symbol: str, ticker: dict[str, Any], cfg: dict[str, Any]) -> SwarmHit | None:
    trades = fetch_agg_trades(symbol, int(cfg["agg_trade_limit"]))
    metrics = analyze_agg_trades(trades)
    try:
        klines = fetch_klines_1m(symbol, 120)
        metrics["vol_accel_1h"] = volume_accel_1h(klines)
    except requests.RequestException:
        metrics["vol_accel_1h"] = 1.0
    hit = score_candidate(metrics, ticker, cfg)
    if hit.score < float(cfg["min_score"]):
        return None
    return hit


def poll_once(
    cfg: dict[str, Any],
    *,
    state: dict[str, Any],
    dry_run: bool,
    token: str | None,
    chat_id: str | None,
    only_symbol: str | None = None,
) -> list[SwarmHit]:
    tickers = fetch_tickers()
    by_sym = {t["symbol"]: t for t in tickers}

    if only_symbol:
        t = by_sym.get(only_symbol)
        if not t:
            print(f"[warn] symbol yok: {only_symbol}", file=sys.stderr)
            return []
        candidates = [t]
    else:
        candidates = pick_candidates(tickers, cfg)

    print(f"scanning {len(candidates)} candidates…")
    hits: list[SwarmHit] = []
    last_alert: dict[str, float] = dict(state.get("last_alert") or {})
    now = time.time()
    cooldown = float(cfg.get("cooldown_sec") or 1800)

    for i, t in enumerate(candidates):
        sym = t["symbol"]
        try:
            hit = scan_symbol(sym, t, cfg)
        except requests.RequestException as exc:
            print(f"[warn] {sym}: {exc}", file=sys.stderr)
            continue
        if hit is None:
            continue
        hits.append(hit)
        print(
            f"  HIT {hit.symbol} score={hit.score} chg={hit.change_pct_24h}% "
            f"tpm={hit.trades_per_min} buy={hit.taker_buy_ratio} accel=x{hit.vol_accel_1h}"
        )
        append_alert(hit)

        if hit.score >= float(cfg.get("telegram_min_score") or cfg["min_score"]):
            prev = last_alert.get(sym, 0.0)
            if now - prev >= cooldown:
                msg = format_alert(hit)
                if token and chat_id:
                    telegram_send(token, chat_id, msg, dry_run=dry_run)
                elif dry_run:
                    telegram_send("", "", msg, dry_run=True)
                last_alert[sym] = now

        # rate-limit nazikçe
        if i < len(candidates) - 1:
            time.sleep(0.12)

    hits.sort(key=lambda h: h.score, reverse=True)
    state["last_alert"] = last_alert
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    state["last_hits"] = [asdict(h) for h in hits[:20]]
    save_json(STATE_PATH, state)
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="Binance CEX bot-swarm / early-pump detector")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--symbol", help="Tek sembol tara (örn. PROMUSDT)")
    args = parser.parse_args()

    cfg = load_config()
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    dry_run = bool(args.dry_run) or not (token and chat_id)
    if dry_run and not args.dry_run:
        print("[info] Telegram env yok → dry-run", file=sys.stderr)

    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"last_alert": {}, "last_hits": []}
    poll = int(cfg.get("poll_seconds") or 90)

    print(f"bot-swarm detector | poll={poll}s | dry_run={dry_run} | min_score={cfg['min_score']}")
    while True:
        hits = poll_once(
            cfg,
            state=state,
            dry_run=dry_run,
            token=token,
            chat_id=chat_id,
            only_symbol=args.symbol.upper() if args.symbol else None,
        )
        print(f"[{datetime.now(timezone.utc).isoformat()}] hits={len(hits)}")
        if hits:
            top = hits[0]
            print(f"top: {top.symbol} score={top.score} → {', '.join(top.reasons[:3])}")
        if args.once:
            break
        time.sleep(poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
