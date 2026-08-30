#!/usr/bin/env python3
"""
Market Radar — CEX FOMO / short-squeeze / dump-risk sinyal tarayıcısı.

ÖNEMLİ:
  - Bu araç OTOMATİK AL-SAT YAPMAZ.
  - Sadece public market verisinden UYARI üretir.
  - Para kaybını engelleyen bir sistem değildir; yanlış sinyal olabilir.
  - Yatırım tavsiyesi değildir.

Kaynaklar:
  - Binance spot + USDT-M futures (www.binance.com)
  - Upbit KRW, Bithumb KRW, BtcTurk TRY

Kullanım:
  python market_radar.py --once --dry-run
  python market_radar.py --once            # Telegram env varsa gönderir
  python market_radar.py                  # loop
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "market_radar.json"
STATE_PATH = ROOT / "output" / "market_radar_state.json"
OUT_PATH = ROOT / "output" / "market_radar_last.json"

BINANCE = "https://www.binance.com"
UA = {"User-Agent": "RATIO-MarketRadar/1.0 (+research; no-trading)"}


# ---------------- HTTP (retry, timeout, safe JSON) ----------------

def http_get(url: str, params: dict[str, Any] | None = None, timeout: int = 25) -> Any:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if not raw:
                    raise ValueError("empty body")
                return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last_err}")


def env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


# ---------------- Data models ----------------

@dataclass
class SpotRow:
    symbol: str  # e.g. ZKCUSDT
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
    severity: int  # 1..5
    title: str
    detail: str
    metrics: dict[str, Any]


# ---------------- Exchange adapters ----------------

def binance_spot_tickers() -> list[SpotRow]:
    data = http_get(f"{BINANCE}/api/v3/ticker/24hr")
    if not isinstance(data, list):
        raise RuntimeError("binance spot ticker unexpected")
    out: list[SpotRow] = []
    for t in data:
        sym = t.get("symbol") or ""
        if not sym.endswith("USDT"):
            continue
        if any(x in sym for x in ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")):
            continue
        try:
            out.append(
                SpotRow(
                    symbol=sym,
                    base=sym[:-4],
                    last=float(t["lastPrice"]),
                    chg_pct=float(t["priceChangePercent"]),
                    quote_vol=float(t["quoteVolume"]),
                    high=float(t["highPrice"]),
                    low=float(t["lowPrice"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def binance_futures_metrics(symbol: str) -> dict[str, Any] | None:
    """Return funding, LS, OI change. None if futures market missing."""
    try:
        prem = http_get(f"{BINANCE}/fapi/v1/premiumIndex", {"symbol": symbol})
        funding = float(prem.get("lastFundingRate") or 0)
    except Exception:
        return None
    try:
        ls = http_get(
            f"{BINANCE}/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol, "period": "1h", "limit": 1},
        )
        ls_ratio = float(ls[0]["longShortRatio"]) if isinstance(ls, list) and ls else None
    except Exception:
        ls_ratio = None
    try:
        oi = http_get(
            f"{BINANCE}/futures/data/openInterestHist",
            {"symbol": symbol, "period": "1h", "limit": 3},
        )
        if not isinstance(oi, list) or len(oi) < 2:
            oi_chg = None
            oi_usd = None
        else:
            now = float(oi[-1]["sumOpenInterestValue"])
            prev = float(oi[-2]["sumOpenInterestValue"])
            oi_usd = now
            oi_chg = ((now - prev) / prev * 100) if prev else None
    except Exception:
        oi_chg = None
        oi_usd = None
    try:
        tk = http_get(
            f"{BINANCE}/futures/data/takerlongshortRatio",
            {"symbol": symbol, "period": "1h", "limit": 1},
        )
        taker = float(tk[0]["buySellRatio"]) if isinstance(tk, list) and tk else None
    except Exception:
        taker = None
    return {
        "funding": funding,
        "ls_ratio": ls_ratio,
        "oi_usd": oi_usd,
        "oi_1h_chg_pct": oi_chg,
        "taker_bs": taker,
    }


def upbit_krw_map() -> dict[str, dict[str, float]]:
    """base -> {last, chg, quote_krw}"""
    markets = http_get("https://api.upbit.com/v1/market/all", {"isDetails": "false"})
    krw = [m["market"] for m in markets if isinstance(m, dict) and str(m.get("market", "")).startswith("KRW-")]
    out: dict[str, dict[str, float]] = {}
    # batch tickers in chunks of 80
    for i in range(0, len(krw), 80):
        chunk = krw[i : i + 80]
        tick = http_get("https://api.upbit.com/v1/ticker", {"markets": ",".join(chunk)})
        if not isinstance(tick, list):
            continue
        for t in tick:
            market = t.get("market") or ""
            base = market.replace("KRW-", "")
            try:
                out[base] = {
                    "last": float(t["trade_price"]),
                    "chg_pct": float(t["signed_change_rate"]) * 100,
                    "quote_krw": float(t["acc_trade_price_24h"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
        time.sleep(0.05)
    return out


def bithumb_krw_map() -> dict[str, dict[str, float]]:
    data = http_get("https://api.bithumb.com/public/ticker/ALL_KRW")
    rows = (data or {}).get("data") or {}
    out: dict[str, dict[str, float]] = {}
    if not isinstance(rows, dict):
        return out
    for base, t in rows.items():
        if base == "date" or not isinstance(t, dict):
            continue
        try:
            prev = float(t.get("prev_closing_price") or 0)
            last = float(t.get("closing_price") or 0)
            chg = ((last - prev) / prev * 100) if prev else 0.0
            # acc_trade_value_24H is KRW quote volume
            q = float(t.get("acc_trade_value_24H") or t.get("acc_trade_value") or 0)
            out[base] = {"last": last, "chg_pct": chg, "quote_krw": q}
        except (TypeError, ValueError):
            continue
    return out


def btcturk_try_map() -> dict[str, dict[str, float]]:
    data = http_get("https://api.btcturk.com/api/v2/ticker")
    rows = (data or {}).get("data") or []
    out: dict[str, dict[str, float]] = {}
    if not isinstance(rows, list):
        return out
    for t in rows:
        pair = str(t.get("pairNormalized") or "")
        if not pair.endswith("_TRY"):
            continue
        base = pair.replace("_TRY", "")
        try:
            last = float(t["last"])
            # dailyPercent from API if present
            if "dailyPercent" in t:
                chg = float(t["dailyPercent"])
            else:
                # approximate from numerator/denominator fields when available
                chg = 0.0
            q = float(t.get("volume") or 0) * last  # base vol * last ≈ TRY quote
            out[base] = {"last": last, "chg_pct": chg, "quote_try": q}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def approx_fx() -> dict[str, float]:
    """Rough FX for volume compare. Fail-soft to defaults."""
    usdt_try = 48.0
    usdt_krw = 1350.0
    try:
        # Binance USDTTRY if exists
        t = http_get(f"{BINANCE}/api/v3/ticker/price", {"symbol": "USDTTRY"})
        usdt_try = float(t["price"])
    except Exception:
        pass
    try:
        # Upbit USDT-KRW
        t = http_get("https://api.upbit.com/v1/ticker", {"markets": "KRW-USDT"})
        if isinstance(t, list) and t:
            usdt_krw = float(t[0]["trade_price"])
    except Exception:
        pass
    return {"USDT_TRY": usdt_try, "USDT_KRW": usdt_krw}


# ---------------- Signal engine ----------------

def pick_universe(spots: list[SpotRow], cfg: dict[str, Any]) -> list[SpotRow]:
    s = cfg["settings"]
    exclude = set(s.get("majors_exclude") or [])
    watch = {w.upper() for w in cfg.get("watchlist") or []}
    min_vol = float(s["min_quote_volume_usdt"])
    ranked = sorted(
        [x for x in spots if x.symbol not in exclude and x.quote_vol >= min_vol],
        key=lambda x: x.quote_vol,
        reverse=True,
    )
    top_n = int(s.get("always_scan_top_n") or 40)
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
    # FOMO: strong spot move + meaningful volume
    if chg >= float(s["fomo_chg_pct"]) and row.quote_vol >= float(s["min_quote_volume_usdt"]):
        sev = 3
        if chg >= 20:
            sev = 4
        if chg >= 40:
            sev = 5
        out.append(
            Signal(
                kind="CEX_FOMO",
                symbol=row.symbol,
                severity=sev,
                title=f"CEX FOMO {row.base}",
                detail=f"Binance spot {chg:+.1f}% · 24h vol ${row.quote_vol/1e6:.1f}M",
                metrics={"chg_pct": chg, "binance_vol_usdt": row.quote_vol},
            )
        )

    # Korea lead FOMO
    if korea_quote_usdt > 0 and row.quote_vol > 0:
        ratio = korea_quote_usdt / row.quote_vol
        if ratio >= float(s["korea_lead_ratio"]) and chg >= float(s["fomo_chg_pct"]) * 0.6:
            out.append(
                Signal(
                    kind="KOREA_FOMO",
                    symbol=row.symbol,
                    severity=5 if ratio >= 2 else 4,
                    title=f"Korea lead FOMO {row.base}",
                    detail=(
                        f"Korea vol ${korea_quote_usdt/1e6:.1f}M vs Binance ${row.quote_vol/1e6:.1f}M "
                        f"(x{ratio:.2f}) · chg {chg:+.1f}%"
                    ),
                    metrics={
                        "korea_vol_usdt": korea_quote_usdt,
                        "binance_vol_usdt": row.quote_vol,
                        "korea_lead_x": ratio,
                        "chg_pct": chg,
                    },
                )
            )

    # Turkey participation note (not always lead)
    if turk_quote_usdt > row.quote_vol * 0.35 and chg >= float(s["fomo_chg_pct"]) * 0.7:
        out.append(
            Signal(
                kind="TURKEY_FOMO",
                symbol=row.symbol,
                severity=3,
                title=f"TR CEX hot {row.base}",
                detail=f"BtcTurk≈${turk_quote_usdt/1e6:.1f}M · Binance ${row.quote_vol/1e6:.1f}M · {chg:+.1f}%",
                metrics={"btcturk_vol_usdt": turk_quote_usdt, "chg_pct": chg},
            )
        )

    # Short squeeze heuristic
    if fut and fut.get("oi_1h_chg_pct") is not None and fut.get("funding") is not None:
        funding = float(fut["funding"])
        oi_chg = float(fut["oi_1h_chg_pct"])
        # use recent hour proxy: if 24h strong and last hour still up hard — use chg as weak proxy
        # Better: require funding negative + OI dropping + daily up
        if (
            funding <= float(s["squeeze_funding_lt"])
            and oi_chg <= -float(s["squeeze_oi_drop_pct"])
            and chg >= float(s["squeeze_price_up_pct"])
        ):
            out.append(
                Signal(
                    kind="SHORT_SQUEEZE",
                    symbol=row.symbol,
                    severity=5,
                    title=f"Short squeeze risk {row.base}",
                    detail=(
                        f"funding {funding:.5f} · OI 1h {oi_chg:+.2f}% · spot {chg:+.1f}% "
                        f"· L/S {fut.get('ls_ratio')}"
                    ),
                    metrics={
                        "funding": funding,
                        "oi_1h_chg_pct": oi_chg,
                        "ls_ratio": fut.get("ls_ratio"),
                        "taker_bs": fut.get("taker_bs"),
                        "chg_pct": chg,
                    },
                )
            )

    # Dump risk after extended pump (informative — not a short entry)
    if chg >= float(s["dump_risk_chg_pct"]):
        # if funding very positive while pumped = late long crowded
        if fut and fut.get("funding") is not None and float(fut["funding"]) >= 0.0005:
            out.append(
                Signal(
                    kind="DUMP_RISK",
                    symbol=row.symbol,
                    severity=4,
                    title=f"Dump risk {row.base}",
                    detail=f"Şişmiş move {chg:+.1f}% + funding +{float(fut['funding']):.5f} (kalabalık long)",
                    metrics={"chg_pct": chg, "funding": fut.get("funding")},
                )
            )

    # CEX-only hint when Korea/Binance huge (DEX not checked here — flagged in detail)
    if chg >= 15 and row.quote_vol >= 10_000_000:
        out.append(
            Signal(
                kind="CEX_ORDERBOOK",
                symbol=row.symbol,
                severity=2,
                title=f"CEX orderbook move {row.base}",
                detail="Büyük CEX hacmi — on-chain whale varsayma; önce funding/OI/Kore teyidi",
                metrics={"chg_pct": chg, "binance_vol_usdt": row.quote_vol},
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
        url, data=payload, headers={**UA, "Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[telegram] {e}", file=sys.stderr)
        return False


def format_signal(sig: Signal) -> str:
    return (
        f"{sig.kind} | {sig.symbol}\n"
        f"{sig.title}\n"
        f"{sig.detail}\n"
        f"severity={sig.severity}/5\n"
        f"⚠️ Uyarıdır, otomatik emir değildir."
    )


def signal_key(sig: Signal) -> str:
    # dedupe by kind+symbol for several hours
    return f"{sig.kind}:{sig.symbol}"


# ---------------- Main cycle ----------------

def run_cycle(cfg: dict[str, Any], state: dict[str, Any], dry_run: bool) -> list[Signal]:
    settings = cfg["settings"]
    print(f"[{datetime.now(timezone.utc).isoformat()}] scanning…")

    spots = binance_spot_tickers()
    universe = pick_universe(spots, cfg)
    print(f"universe={len(universe)} symbols")

    fx = approx_fx()
    usdt_krw = fx["USDT_KRW"] or 1350.0
    usdt_try = fx["USDT_TRY"] or 48.0

    # Korea / TR maps (fail-soft)
    upbit: dict[str, dict[str, float]] = {}
    bithumb: dict[str, dict[str, float]] = {}
    btcturk: dict[str, dict[str, float]] = {}
    try:
        upbit = upbit_krw_map()
        print(f"upbit markets={len(upbit)}")
    except Exception as e:
        print(f"[warn] upbit: {e}", file=sys.stderr)
    try:
        bithumb = bithumb_krw_map()
        print(f"bithumb markets={len(bithumb)}")
    except Exception as e:
        print(f"[warn] bithumb: {e}", file=sys.stderr)
    try:
        btcturk = btcturk_try_map()
        print(f"btcturk markets={len(btcturk)}")
    except Exception as e:
        print(f"[warn] btcturk: {e}", file=sys.stderr)

    signals: list[Signal] = []
    snapshots: list[dict[str, Any]] = []

    for row in universe:
        fut = None
        try:
            fut = binance_futures_metrics(row.symbol)
        except Exception as e:
            print(f"[warn] futures {row.symbol}: {e}", file=sys.stderr)

        k_quote = 0.0
        if row.base in upbit:
            k_quote += upbit[row.base].get("quote_krw", 0.0) / usdt_krw
        if row.base in bithumb:
            k_quote += bithumb[row.base].get("quote_krw", 0.0) / usdt_krw
        t_quote = 0.0
        if row.base in btcturk:
            t_quote += btcturk[row.base].get("quote_try", 0.0) / usdt_try

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
        time.sleep(0.05)  # be gentle on futures endpoints

    # rank & cap alerts
    signals.sort(key=lambda s: (-s.severity, -s.metrics.get("chg_pct", 0)))
    max_alerts = int(settings.get("max_alerts_per_cycle") or 12)
    seen = set(state.get("alerted") or [])
    # expire keys older: store map key->ts
    alerted_map: dict[str, float] = dict(state.get("alerted_map") or {})
    now = time.time()
    # drop > 6h
    alerted_map = {k: ts for k, ts in alerted_map.items() if now - float(ts) < 6 * 3600}

    fresh: list[Signal] = []
    for sig in signals:
        key = signal_key(sig)
        if key in alerted_map:
            continue
        fresh.append(sig)
        if len(fresh) >= max_alerts:
            break

    token = env("TELEGRAM_BOT_TOKEN")
    chat = env("TELEGRAM_CHAT_ID")
    send = dry_run or bool(token and chat)
    if not dry_run and not (token and chat):
        print("[info] Telegram env yok → dry-run çıktı", file=sys.stderr)
        dry_run = True

    for sig in fresh:
        ok = telegram_send(token or "", chat or "", format_signal(sig), dry_run=dry_run)
        if ok:
            alerted_map[signal_key(sig)] = now
            print(f"[alert] {sig.kind} {sig.symbol} sev={sig.severity}")
        time.sleep(0.2)

    payload = {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "disclaimer": "Research alerts only. Not financial advice. No auto-trading.",
        "fx": fx,
        "signal_count": len(signals),
        "fresh_alerts": [asdict(s) for s in fresh],
        "all_signals": [asdict(s) for s in signals[:50]],
        "snapshots_top": sorted(snapshots, key=lambda x: -x["chg_pct"])[:30],
    }
    save_json(OUT_PATH, payload)
    state["alerted_map"] = alerted_map
    state["updated_at"] = payload["as_of_utc"]
    save_json(STATE_PATH, state)
    print(f"signals={len(signals)} fresh={len(fresh)} → {OUT_PATH}")
    return fresh


def main() -> int:
    parser = argparse.ArgumentParser(description="CEX FOMO / squeeze market radar (alerts only)")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Telegram gönderme")
    parser.add_argument("--live-telegram", action="store_true", help="Dry-run kapalı (env gerekir)")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"config yok: {cfg_path}", file=sys.stderr)
        return 1
    cfg = load_json(cfg_path)
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {}

    dry = True
    if args.live_telegram:
        dry = False
    if args.dry_run:
        dry = True
    if cfg.get("settings", {}).get("dry_run_default", True) and not args.live_telegram:
        dry = True

    print("=" * 60)
    print("MARKET RADAR — uyarı sistemi (otomatik al-sat YOK)")
    print("Yanlış sinyal olabilir. Para kaybı riski size aittir.")
    print("=" * 60)
    print(f"dry_run={dry}")

    poll = int(cfg.get("settings", {}).get("poll_seconds") or 120)
    while True:
        try:
            run_cycle(cfg, state, dry_run=dry)
        except Exception as e:
            print(f"[error] cycle failed: {e}", file=sys.stderr)
            # never crash-loop trade; just wait
        if args.once:
            break
        time.sleep(poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
