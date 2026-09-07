#!/usr/bin/env python3
"""
Binance yükseliş adayı tarayıcı (Telegram YOK).

Her çalıştırdığında konsola en güçlü adayları yazar.
API key gerekmez. Kesin yükseliş / %70 garanti yoktur.

Kullanım:
  python3 binance_momentum_scanner.py
  python3 binance_momentum_scanner.py --top 3
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

BINANCE_API_BASES = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
)

_active_base: str | None = None


@dataclass(frozen=True)
class Candidate:
    symbol: str
    price: float
    change_30m_pct: float
    change_1h_pct: float
    change_24h_pct: float
    volume_ratio_30m: float
    quote_volume_24h: float
    score: float
    risk: str
    reason: str


def get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    global _active_base
    bases = list(BINANCE_API_BASES)
    if _active_base:
        bases = [_active_base] + [b for b in bases if b != _active_base]

    last_err: Exception | None = None
    for base in bases:
        url = f"{base}{path}"
        try:
            r = requests.get(url, params=params or {}, timeout=30)
            if r.status_code in {418, 429, 451, 403}:
                last_err = requests.HTTPError(f"{r.status_code} {base}")
                continue
            r.raise_for_status()
            _active_base = base
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            continue
    raise RuntimeError(f"Binance API unreachable: {last_err}")


def pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return ((a - b) / b) * 100.0


def risk_label(change_30m: float, vol_ratio: float) -> str:
    if change_30m >= 6 or vol_ratio >= 4:
        return "yüksek"
    if change_30m >= 3 or vol_ratio >= 2.5:
        return "orta"
    return "düşük"


def analyze_symbol(symbol: str, last_price: float, change_24h: float, quote_vol_24h: float) -> Candidate | None:
    klines = get_json(
        "/api/v3/klines",
        {"symbol": symbol, "interval": "15m", "limit": 8},
    )
    if not isinstance(klines, list) or len(klines) < 6:
        return None

    closes = [float(k[4]) for k in klines]
    quote_vols = [float(k[7]) for k in klines]

    price_now = closes[-1] if closes[-1] > 0 else last_price
    price_30m = closes[-3]
    price_1h = closes[-5]

    change_30m = pct(price_now, price_30m)
    change_1h = pct(price_now, price_1h)

    recent_quote = sum(quote_vols[-2:])
    prior_quote = sum(quote_vols[-6:-2]) / 2
    vol_ratio = (recent_quote / prior_quote) if prior_quote > 0 else 0.0

    if not (2.0 <= change_30m <= 8.0):
        return None
    if vol_ratio < 1.5:
        return None
    if change_24h > 25:
        return None
    if change_1h < 1.0:
        return None

    score = (
        change_30m * 1.4
        + min(vol_ratio, 6.0) * 2.0
        + max(min(change_1h, 12.0), 0) * 0.6
        + min(change_24h, 15.0) * 0.2
    )
    reason = (
        f"30dk %{change_30m:.1f}, 1s %{change_1h:.1f}, "
        f"hacim x{vol_ratio:.1f}, 24s %{change_24h:.1f}"
    )
    return Candidate(
        symbol=symbol,
        price=price_now,
        change_30m_pct=change_30m,
        change_1h_pct=change_1h,
        change_24h_pct=change_24h,
        volume_ratio_30m=vol_ratio,
        quote_volume_24h=quote_vol_24h,
        score=score,
        risk=risk_label(change_30m, vol_ratio),
        reason=reason,
    )


def scan(
    *,
    min_quote_volume: float,
    max_symbols: int,
    sleep_between: float,
) -> list[Candidate]:
    tickers = get_json("/api/v3/ticker/24hr")
    usdt = []
    for t in tickers:
        symbol = t.get("symbol") or ""
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        if any(x in base for x in ("UP", "DOWN", "BULL", "BEAR")):
            continue
        if base in {"USDC", "FDUSD", "TUSD", "DAI", "EUR", "TRY", "BUSD"}:
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
            last = float(t.get("lastPrice") or 0)
            ch24 = float(t.get("priceChangePercent") or 0)
        except (TypeError, ValueError):
            continue
        if qv < min_quote_volume or last <= 0:
            continue
        if ch24 < -3 or ch24 > 25:
            continue
        usdt.append((symbol, last, ch24, qv))

    usdt.sort(key=lambda x: x[3], reverse=True)
    usdt = usdt[:max_symbols]

    found: list[Candidate] = []
    for i, (symbol, last, ch24, qv) in enumerate(usdt):
        try:
            c = analyze_symbol(symbol, last, ch24, qv)
        except (requests.RequestException, ValueError, KeyError, IndexError, RuntimeError) as exc:
            print(f"[skip] {symbol}: {exc}", file=sys.stderr)
            continue
        if c:
            found.append(c)
        if sleep_between > 0 and i + 1 < len(usdt):
            time.sleep(sleep_between)

    found.sort(key=lambda c: c.score, reverse=True)
    return found


def format_console(candidates: list[Candidate], top: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"YUKSELIS ADAYLARI ({now})",
        "Filtre: 30dk %+2…+8 | hacim ≥1.5x | aşırı uzamamış",
        "-" * 64,
    ]
    rows = candidates[:top]
    if not rows:
        lines.append("Şu an filtreye uyan aday yok. Biraz sonra tekrar çalıştır.")
        return "\n".join(lines)

    lines.append("EN GUCLU ADAY:")
    best = rows[0]
    best_coin = best.symbol.replace("USDT", "")
    lines.append(
        f"  >>> {best_coin}  ${best.price:.6g}  |  30dk %{best.change_30m_pct:+.1f}  "
        f"|  hacim x{best.volume_ratio_30m:.1f}  |  risk {best.risk}"
    )
    lines.append(f"      {best.reason}")
    lines.append("")
    lines.append(f"{'#':<3} {'COIN':<12} {'PRICE':>12} {'30m%':>7} {'1h%':>7} {'VOLx':>6} {'RISK':<7}")
    for i, c in enumerate(rows, 1):
        coin = c.symbol.replace("USDT", "")
        lines.append(
            f"{i:<3} {coin:<12} {c.price:>12.6g} "
            f"{c.change_30m_pct:>+6.1f}% {c.change_1h_pct:>+6.1f}% "
            f"{c.volume_ratio_30m:>5.1f}x {c.risk:<7}"
        )
    lines.append("-" * 64)
    lines.append("Not: Bu kesin yükseliş değil, momentum taramasıdır.")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Binance yükseliş adayı tarayıcı (konsol)")
    p.add_argument("--top", type=int, default=5, help="Kaç aday (default 5)")
    p.add_argument("--min-volume", type=float, default=2_000_000, help="Min 24s USDT hacmi")
    p.add_argument("--universe", type=int, default=120, help="İncelenecek sembol sayısı")
    p.add_argument("--sleep", type=float, default=0.05, help="Sembol arası bekleme")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    print("Taranıyor… her çalıştırmada güncel adayları bulur.\n", flush=True)
    candidates = scan(
        min_quote_volume=args.min_volume,
        max_symbols=args.universe,
        sleep_between=args.sleep,
    )
    print(format_console(candidates, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
