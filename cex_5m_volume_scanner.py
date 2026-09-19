#!/usr/bin/env python3
"""
Çoklu CEX 5 dakikalık dipten hacim / 0→+ dönüş tarayıcısı.

Binance, Coinbase, Upbit, OKX, Bybit, Bitget, Gate, KuCoin üzerinde
5m mumları tarar; dipten hacim yükselişi veya negatiften pozitife dönüş
sinyali olan coinleri bulur. En çok ortak (confluence) coin'i yüzdelik
skorla işaretler:

  🟢 AL  → yeterli borsada aynı sinyal
  🔴 BEKLE → zayıf / tekil sinyal

Kullanım:
  python cex_5m_volume_scanner.py
  python cex_5m_volume_scanner.py --once
  python cex_5m_volume_scanner.py --once --dry-run
  python cex_5m_volume_scanner.py --top 15
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ccxt
import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "cex_5m_scanner.json"
OUTPUT_PATH = ROOT / "output" / "cex_5m_volume_signals.json"
STATE_PATH = ROOT / "output" / "cex_5m_alert_state.json"

# Geo-restricted ortamlarda Binance public data mirror
BINANCE_REST = "https://data-api.binance.vision"
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "cex-5m-volume-scanner/1.0"})


@dataclass
class ExchangeSignal:
    exchange: str
    symbol: str
    base: str
    price: float
    change_pct: float
    volume_rise: float
    near_bottom: bool
    zero_to_pos: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class CoinConfluence:
    base: str
    signal_exchanges: list[str]
    listed_exchanges: list[str]
    confluence_pct: float
    avg_volume_rise: float
    avg_change_pct: float
    reasons: list[str]
    action: str  # AL | BEKLE
    signals: list[ExchangeSignal]


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) and value.strip() else default


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def make_exchange(ex_id: str) -> ccxt.Exchange:
    klass = getattr(ccxt, ex_id)
    opts: dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": 25000,
        "options": {"defaultType": "spot"},
    }
    # Binance: restricted location → public data API
    if ex_id == "binance":
        opts["urls"] = {
            "api": {
                "public": BINANCE_REST + "/api/v3",
                "private": BINANCE_REST + "/api/v3",
                "v1": BINANCE_REST + "/api/v1",
            }
        }
    return klass(opts)


def scan_binance_vision(cfg: dict[str, Any]) -> tuple[list[ExchangeSignal], set[str], str | None]:
    """CCXT Binance 451 verirse data-api.binance.vision ile tara."""
    max_sym = int(cfg.get("max_symbols_per_exchange") or 120)
    min_qv = float(cfg.get("min_quote_volume_usdt") or 0)
    limit = int(cfg.get("ohlcv_limit") or 36)
    workers = int(cfg.get("workers") or 12)
    vol_mult = float(cfg.get("volume_rise_mult") or 1.45)
    near_pct = float(cfg.get("near_bottom_pct") or 2.5)
    stable = {s.upper() for s in (cfg.get("stable_bases") or [])}

    try:
        info = HTTP.get(f"{BINANCE_REST}/api/v3/exchangeInfo", timeout=30).json()
        tickers = HTTP.get(f"{BINANCE_REST}/api/v3/ticker/24hr", timeout=40).json()
    except Exception as exc:  # noqa: BLE001
        return [], set(), f"binance-vision: {exc}"

    usdt_syms = {
        s["symbol"]
        for s in info.get("symbols", [])
        if s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
        and s.get("isSpotTradingAllowed", True)
        and normalize_base(s.get("baseAsset", "")) not in stable
        and not str(s.get("symbol", "")).endswith(("UPUSDT", "DOWNUSDT"))
    }
    ranked: list[tuple[float, str, str]] = []
    listed: set[str] = set()
    for t in tickers:
        sym = t.get("symbol") or ""
        if sym not in usdt_syms:
            continue
        base = normalize_base(sym.replace("USDT", ""))
        if base in stable:
            continue
        listed.add(base)
        try:
            qv = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            qv = 0.0
        if min_qv and qv < min_qv:
            continue
        ranked.append((qv, base, sym))
    ranked.sort(key=lambda x: x[0], reverse=True)
    ranked = ranked[:max_sym]

    signals: list[ExchangeSignal] = []

    def _job(item: tuple[float, str, str]) -> ExchangeSignal | None:
        _qv, base, market = item
        try:
            rows = HTTP.get(
                f"{BINANCE_REST}/api/v3/klines",
                params={"symbol": market, "interval": "5m", "limit": limit},
                timeout=25,
            ).json()
            ohlcv = [
                [int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
                for r in rows
            ]
        except Exception:  # noqa: BLE001
            return None
        hit = analyze_ohlcv(ohlcv, volume_rise_mult=vol_mult, near_bottom_pct=near_pct)
        if not hit:
            return None
        return ExchangeSignal(
            exchange="binance",
            symbol=f"{base}/USDT",
            base=base,
            price=float(hit["price"]),
            change_pct=float(hit["change_pct"]),
            volume_rise=float(hit["volume_rise"]),
            near_bottom=bool(hit["near_bottom"]),
            zero_to_pos=bool(hit["zero_to_pos"]),
            reasons=list(hit["reasons"]),
        )

    with ThreadPoolExecutor(max_workers=max(2, workers)) as pool:
        futs = [pool.submit(_job, row) for row in ranked]
        for fut in as_completed(futs):
            try:
                sig = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if sig:
                signals.append(sig)
    return signals, listed, None


def normalize_base(base: str) -> str:
    b = (base or "").upper().strip()
    # 1000PEPE → PEPE, 1000SHIB → SHIB gibi kaldıraçlı tickers
    if b.startswith("1000") and len(b) > 4:
        b = b[4:]
    if b.startswith("10000") and len(b) > 5:
        b = b[5:]
    return b


def pick_quote_markets(
    markets: dict[str, Any],
    quote_priority: list[str],
    stable_bases: set[str],
) -> dict[str, str]:
    """base -> best market symbol (örn. BTC → BTC/USDT)."""
    by_base: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for sym, m in markets.items():
        if not m.get("active", True):
            continue
        if m.get("spot") is False:
            continue
        base = normalize_base(str(m.get("base") or ""))
        quote = str(m.get("quote") or "").upper()
        if not base or base in stable_bases:
            continue
        if quote not in quote_priority:
            continue
        # sadece spot benzeri
        if m.get("contract") or m.get("swap") or m.get("future"):
            continue
        prio = quote_priority.index(quote)
        by_base[base].append((prio, sym))

    chosen: dict[str, str] = {}
    for base, rows in by_base.items():
        rows.sort(key=lambda x: x[0])
        chosen[base] = rows[0][1]
    return chosen


def ticker_quote_volume(t: dict[str, Any]) -> float:
    for key in ("quoteVolume", "baseVolume"):
        v = t.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    info = t.get("info") or {}
    for key in ("quoteVolume", "volCcy24h", "turnover24h", "acc_trade_price_24h"):
        v = info.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def analyze_ohlcv(
    ohlcv: list[list[float]],
    *,
    volume_rise_mult: float,
    near_bottom_pct: float,
) -> dict[str, Any] | None:
    """Kapalı 5m mumlarında dipten hacim + 0→+ dönüş tespiti."""
    if not ohlcv or len(ohlcv) < 10:
        return None

    # son mum oluşuyorsa hariç tut
    candles = ohlcv[:-1] if len(ohlcv) >= 11 else ohlcv
    if len(candles) < 9:
        return None

    opens = [float(c[1]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    vols = [float(c[5]) for c in candles]

    lookback = min(12, len(candles))
    window = slice(-lookback, None)
    w_lows = lows[window]
    w_vols = vols[window]
    w_closes = closes[window]

    last_c, prev_c, prev2_c = closes[-1], closes[-2], closes[-3]
    last_o, prev_o = opens[-1], opens[-2]
    last_v, prev_v, prev2_v = vols[-1], vols[-2], vols[-3]

    if prev_c <= 0 or prev2_c <= 0 or last_c <= 0:
        return None

    # Hacim dibi: son 2-3 mum öncesindeki pencere min'inden yükseliş
    trough_vols = w_vols[:-1]
    vol_trough = min(trough_vols) if trough_vols else last_v
    vol_rising_chain = last_v > prev_v >= prev2_v * 0.95
    vol_from_bottom = last_v >= max(vol_trough * volume_rise_mult, 1e-12) and last_v > prev_v
    volume_ok = vol_from_bottom or (vol_rising_chain and last_v >= vol_trough * 1.2)
    volume_rise = (last_v / vol_trough) if vol_trough > 0 else 0.0

    # Fiyat dipten / dipe yakın
    recent_low = min(w_lows)
    near_bottom = last_c <= recent_low * (1.0 + near_bottom_pct / 100.0)

    # 0 → + (önceki getiri ≤0, şimdi >0)
    ret_prev = (prev_c - prev2_c) / prev2_c
    ret_now = (last_c - prev_c) / prev_c
    zero_to_pos = ret_prev <= 0.0 and ret_now > 0.0

    green_after_red = last_c > last_o and prev_c <= prev_o
    local_bounce = last_c > recent_low and last_c > prev_c and near_bottom

    reasons: list[str] = []
    if vol_from_bottom:
        reasons.append("HACIM_DIP")
    elif vol_rising_chain:
        reasons.append("HACIM_YUKSELIS")
    if zero_to_pos:
        reasons.append("0→+")
    if green_after_red:
        reasons.append("YESIL_MUM")
    if near_bottom:
        reasons.append("DIP_YAKIN")
    if local_bounce and volume_ok:
        reasons.append("DIP_DONUS")

    # Ana kural: hacim canlanıyor VE (0→+ veya dipten dönüş)
    price_trigger = zero_to_pos or green_after_red or local_bounce
    if not (volume_ok and price_trigger):
        return None

    change_pct = ret_now * 100.0
    return {
        "price": last_c,
        "change_pct": change_pct,
        "volume_rise": volume_rise,
        "near_bottom": near_bottom,
        "zero_to_pos": zero_to_pos,
        "reasons": reasons,
        "high": highs[-1],
        "low": lows[-1],
    }


def scan_exchange(
    name: str,
    ex_id: str,
    cfg: dict[str, Any],
) -> tuple[list[ExchangeSignal], set[str], str | None]:
    """Bir borsayı tara. Dönüş: signals, listed_bases, error."""
    quote_priority = list(cfg.get("quote_priority") or ["USDT", "USD"])
    stable = {s.upper() for s in (cfg.get("stable_bases") or [])}
    max_sym = int(cfg.get("max_symbols_per_exchange") or 120)
    min_qv = float(cfg.get("min_quote_volume_usdt") or 0)
    limit = int(cfg.get("ohlcv_limit") or 36)
    timeframe = str(cfg.get("timeframe") or "5m")
    workers = int(cfg.get("workers") or 12)
    vol_mult = float(cfg.get("volume_rise_mult") or 1.45)
    near_pct = float(cfg.get("near_bottom_pct") or 2.5)

    try:
        ex = make_exchange(ex_id)
        markets = ex.load_markets()
    except Exception as exc:  # noqa: BLE001
        if name == "binance" or ex_id == "binance":
            print(f"  … binance CCXT başarısız, vision API deneniyor ({exc.__class__.__name__})")
            return scan_binance_vision(cfg)
        return [], set(), f"{name}: load_markets {exc}"

    base_to_sym = pick_quote_markets(markets, quote_priority, stable)
    listed = set(base_to_sym.keys())

    try:
        tickers = ex.fetch_tickers(list(base_to_sym.values()))
    except Exception:
        # bazı borsalar toplu ticker desteklemez
        tickers = {}
        for sym in list(base_to_sym.values())[: max_sym * 2]:
            try:
                tickers[sym] = ex.fetch_ticker(sym)
                time.sleep(getattr(ex, "rateLimit", 200) / 1000.0)
            except Exception:  # noqa: BLE001
                continue

    ranked: list[tuple[float, str, str]] = []
    for base, sym in base_to_sym.items():
        t = tickers.get(sym) or {}
        qv = ticker_quote_volume(t)
        # Upbit KRW hacmi kabaca; eşiği gevşek tut
        if min_qv > 0 and qv < min_qv and str(markets.get(sym, {}).get("quote") or "").upper() == "USDT":
            continue
        ranked.append((qv, base, sym))
    ranked.sort(key=lambda x: x[0], reverse=True)
    ranked = ranked[:max_sym]

    signals: list[ExchangeSignal] = []

    def _job(item: tuple[float, str, str]) -> ExchangeSignal | None:
        _qv, base, sym = item
        try:
            ohlcv = ex.fetch_ohlcv(sym, timeframe=timeframe, limit=limit)
        except Exception:  # noqa: BLE001
            return None
        hit = analyze_ohlcv(ohlcv, volume_rise_mult=vol_mult, near_bottom_pct=near_pct)
        if not hit:
            return None
        return ExchangeSignal(
            exchange=name,
            symbol=sym,
            base=base,
            price=float(hit["price"]),
            change_pct=float(hit["change_pct"]),
            volume_rise=float(hit["volume_rise"]),
            near_bottom=bool(hit["near_bottom"]),
            zero_to_pos=bool(hit["zero_to_pos"]),
            reasons=list(hit["reasons"]),
        )

    with ThreadPoolExecutor(max_workers=max(2, workers)) as pool:
        futs = [pool.submit(_job, row) for row in ranked]
        for fut in as_completed(futs):
            try:
                sig = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if sig:
                signals.append(sig)

    return signals, listed, None


def build_confluence(
    all_signals: list[ExchangeSignal],
    listings: dict[str, set[str]],
    *,
    buy_min_exchanges: int,
    buy_min_confluence_pct: float,
) -> list[CoinConfluence]:
    by_base: dict[str, list[ExchangeSignal]] = defaultdict(list)
    for s in all_signals:
        by_base[s.base].append(s)

    # coin → hangi borsalarda listeli
    base_listed: dict[str, set[str]] = defaultdict(set)
    for ex_name, bases in listings.items():
        for b in bases:
            base_listed[b].add(ex_name)

    out: list[CoinConfluence] = []
    for base, sigs in by_base.items():
        sig_ex = sorted({s.exchange for s in sigs})
        listed = sorted(base_listed.get(base) or {s.exchange for s in sigs})
        n_sig = len(sig_ex)
        n_list = max(len(listed), 1)
        # Confluence: sinyal veren / listelendiği borsa sayısı
        pct = 100.0 * n_sig / n_list
        # Ayrıca "kaç borsada ortak" mutlak gücü
        avg_vol = sum(s.volume_rise for s in sigs) / len(sigs)
        avg_chg = sum(s.change_pct for s in sigs) / len(sigs)
        reasons = sorted({r for s in sigs for r in s.reasons})
        action = (
            "AL"
            if n_sig >= buy_min_exchanges and pct >= buy_min_confluence_pct
            else "BEKLE"
        )
        out.append(
            CoinConfluence(
                base=base,
                signal_exchanges=sig_ex,
                listed_exchanges=listed,
                confluence_pct=pct,
                avg_volume_rise=avg_vol,
                avg_change_pct=avg_chg,
                reasons=reasons,
                action=action,
                signals=sigs,
            )
        )

    # Tek borsada görünen gürültülü token'ları (ör. Bitget R* hisse wrapper) alta it
    out.sort(
        key=lambda c: (
            len(c.signal_exchanges),
            len(c.listed_exchanges),
            c.confluence_pct,
            c.avg_volume_rise,
        ),
        reverse=True,
    )
    return out


def format_report(coins: list[CoinConfluence], top: int, scanned_ex: list[str]) -> str:
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"CEX 5m DİPTEN HACİM TARAMA · {now}")
    lines.append(f"Borsalar: {', '.join(scanned_ex)}")
    lines.append("")

    if not coins:
        lines.append("🔴 BEKLE — şu an ortak dipten hacim sinyali yok")
        return "\n".join(lines)

    best = coins[0]
    badge = "🟢 AL" if best.action == "AL" else "🔴 BEKLE"
    lines.append("═══ EN GÜÇLÜ ORTAK COİN ═══")
    lines.append(
        f"{badge}  {best.base}  ·  ortak %{best.confluence_pct:.0f}  "
        f"({len(best.signal_exchanges)}/{len(best.listed_exchanges)} borsa)"
    )
    lines.append(
        f"Borsalar: {', '.join(best.signal_exchanges)}  |  "
        f"hacim×{best.avg_volume_rise:.2f}  |  Δ%{best.avg_change_pct:+.2f}"
    )
    lines.append(f"Neden: {', '.join(best.reasons) or '-'}")
    lines.append("")
    lines.append(f"── TOP {min(top, len(coins))} ──")
    for i, c in enumerate(coins[:top], 1):
        mark = "🟢 AL" if c.action == "AL" else "🔴 BEKLE"
        lines.append(
            f"{i:2d}. {mark} {c.base:<10} "
            f"%{c.confluence_pct:5.1f}  "
            f"{len(c.signal_exchanges)}/{len(c.listed_exchanges)} cex  "
            f"vol×{c.avg_volume_rise:.2f}  "
            f"[{', '.join(c.signal_exchanges)}]"
        )
    return "\n".join(lines)


def format_telegram(coins: list[CoinConfluence], scanned_ex: list[str]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not coins:
        return (
            f"<b>🔴 BEKLE</b> · 5m dipten hacim\n"
            f"<i>{now}</i>\n"
            f"Ortak sinyal yok.\n"
            f"CEX: <code>{', '.join(scanned_ex)}</code>"
        )
    best = coins[0]
    if best.action == "AL":
        head = f"<b>🟢 AL FIRSATI</b> · <b>{best.base}</b>"
    else:
        head = f"<b>🔴 BEKLE</b> · <b>{best.base}</b> (zayıf confluence)"
    body = (
        f"{head}\n"
        f"Ortaklık: <b>%{best.confluence_pct:.0f}</b> "
        f"({len(best.signal_exchanges)}/{len(best.listed_exchanges)} borsa)\n"
        f"Borsalar: <code>{', '.join(best.signal_exchanges)}</code>\n"
        f"Hacim yükseliş: ×{best.avg_volume_rise:.2f}\n"
        f"5m Δ: %{best.avg_change_pct:+.2f}\n"
        f"Sinyal: {', '.join(best.reasons)}\n"
        f"<i>{now}</i>"
    )
    # AL olan diğerleri kısaca
    als = [c for c in coins[1:6] if c.action == "AL"]
    if als:
        body += "\n\nDiğer 🟢 AL:\n" + "\n".join(
            f"• <b>{c.base}</b> %{c.confluence_pct:.0f} ({len(c.signal_exchanges)} cex)"
            for c in als
        )
    return body


def telegram_send(token: str, chat_id: str, text: str, dry_run: bool = False) -> bool:
    if dry_run:
        print("--- DRY-RUN TELEGRAM ---\n" + text + "\n------------------------")
        return True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if r.status_code != 200:
            print(f"[telegram] HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return False
        return True
    except requests.RequestException as exc:
        print(f"[telegram] error: {exc}", file=sys.stderr)
        return False


def run_scan(cfg: dict[str, Any]) -> tuple[list[CoinConfluence], list[str], list[str]]:
    ex_cfg = cfg.get("exchanges") or {}
    enabled: list[tuple[str, str]] = []
    skipped: list[str] = []
    for name, meta in ex_cfg.items():
        if not meta.get("enabled"):
            note = meta.get("note") or "disabled"
            skipped.append(f"{name} ({note})")
            continue
        ex_id = meta.get("id")
        if not ex_id:
            skipped.append(f"{name} (id yok)")
            continue
        enabled.append((name, str(ex_id)))

    all_signals: list[ExchangeSignal] = []
    listings: dict[str, set[str]] = {}
    errors: list[str] = []
    scanned: list[str] = []

    # Borsaları sırayla (her biri kendi thread havuzunu kullanır)
    for name, ex_id in enabled:
        print(f"[scan] {name}…", flush=True)
        t0 = time.time()
        sigs, listed, err = scan_exchange(name, ex_id, cfg)
        dt = time.time() - t0
        if err:
            errors.append(err)
            print(f"  ! {err}", file=sys.stderr)
            continue
        scanned.append(name)
        listings[name] = listed
        all_signals.extend(sigs)
        print(f"  → {len(sigs)} sinyal / {len(listed)} coin ({dt:.1f}s)")

    buy_n = int(cfg.get("buy_min_exchanges") or 3)
    buy_pct = float(cfg.get("buy_min_confluence_pct") or 50)
    coins = build_confluence(
        all_signals,
        listings,
        buy_min_exchanges=buy_n,
        buy_min_confluence_pct=buy_pct,
    )
    if skipped:
        print("[skip] " + "; ".join(skipped))
    if errors:
        print("[errors] " + " | ".join(errors), file=sys.stderr)
    return coins, scanned, skipped


def serialize(coins: list[CoinConfluence], scanned: list[str], skipped: list[str]) -> dict[str, Any]:
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scanned_exchanges": scanned,
        "skipped": skipped,
        "coins": [
            {
                "base": c.base,
                "action": c.action,
                "confluence_pct": round(c.confluence_pct, 2),
                "signal_exchanges": c.signal_exchanges,
                "listed_exchanges": c.listed_exchanges,
                "avg_volume_rise": round(c.avg_volume_rise, 3),
                "avg_change_pct": round(c.avg_change_pct, 3),
                "reasons": c.reasons,
                "details": [
                    {
                        "exchange": s.exchange,
                        "symbol": s.symbol,
                        "price": s.price,
                        "change_pct": round(s.change_pct, 3),
                        "volume_rise": round(s.volume_rise, 3),
                        "reasons": s.reasons,
                    }
                    for s in c.signals
                ],
            }
            for c in coins
        ],
    }


def should_alert(state: dict[str, Any], best: CoinConfluence | None, cooldown_sec: int = 900) -> bool:
    if not best or best.action != "AL":
        return False
    last = state.get("last_alert") or {}
    if last.get("base") == best.base:
        try:
            prev = datetime.fromisoformat(last["at"])
            if (datetime.now(timezone.utc) - prev).total_seconds() < cooldown_sec:
                return False
        except (KeyError, TypeError, ValueError):
            pass
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Çoklu CEX 5m dipten hacim tarayıcısı")
    parser.add_argument("--once", action="store_true", help="Tek tarama")
    parser.add_argument("--dry-run", action="store_true", help="Telegram gönderme")
    parser.add_argument("--top", type=int, default=12, help="Konsolda gösterilecek satır")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--no-telegram", action="store_true")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        raise SystemExit(f"config yok: {cfg_path}")
    cfg = load_json(cfg_path)

    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    dry = bool(args.dry_run) or args.no_telegram or not (token and chat_id)
    if dry and not args.dry_run and not args.no_telegram:
        print("[info] Telegram env yok → dry-run", file=sys.stderr)

    poll = int(cfg.get("poll_seconds") or 300)
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {}

    while True:
        coins, scanned, skipped = run_scan(cfg)
        report = format_report(coins, args.top, scanned)
        print("\n" + report + "\n")

        payload = serialize(coins, scanned, skipped)
        save_json(OUTPUT_PATH, payload)

        best = coins[0] if coins else None
        if not args.no_telegram and should_alert(state, best):
            msg = format_telegram(coins, scanned)
            if telegram_send(token or "", chat_id or "", msg, dry_run=dry):
                state["last_alert"] = {
                    "base": best.base if best else None,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "pct": best.confluence_pct if best else 0,
                }
                save_json(STATE_PATH, state)

        if args.once:
            break
        print(f"[sleep] {poll}s…")
        time.sleep(poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
