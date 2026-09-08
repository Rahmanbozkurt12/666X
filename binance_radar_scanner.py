#!/usr/bin/env python3
"""
Binance Momentum / Likidite Radar Tarayıcısı

Ölçtükleri:
- 5dk, 30dk, 1s fiyat momentumu
- Kademeli hacim artışı
- Order-book derinliği ve derinlik değişimi
- Alış / satış tarafı dengesizliği
- Büyük işlem akışı (aggTrades)
- Binance resmî duyuru eşleşmesi
- Opsiyonel on-chain holder ve DEX havuz likiditesi modülü (placeholder)

Kurulum:
    pip install requests

Kullanım:
    python3 binance_radar_scanner.py --top 10
    python3 binance_radar_scanner.py --loop --interval 300 --top 10
    python3 binance_radar_scanner.py --telegram --loop --interval 300

Ortam değişkenleri (sadece --telegram için):
    export TELEGRAM_BOT_TOKEN="..."
    export TELEGRAM_CHAT_ID="..."

Not:
- Bu yazılım alarm/radar aracıdır; kesin fiyat yönü veya getiri garantisi vermez.
- "Balina" / "bot" ifadeleri davranışsal tahmindir; kimlik tespiti değildir.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent

BINANCE_BASES = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
)

CMS_BASE = "https://www.binance.com"
QUOTE_ASSET = "USDT"

MIN_24H_QUOTE_VOLUME = 2_000_000
MIN_DEPTH_USDT = 80_000
MAX_SYMBOLS_TO_SCAN = 80
BIG_TRADE_USDT = 25_000
STATE_FILE = ROOT / "output" / "binance_radar_state.json"

STABLE_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "TRY"}
LEVERAGED_MARKERS = ("UP", "DOWN", "BULL", "BEAR")

_active_base: str | None = None


@dataclass
class Candidate:
    symbol: str
    price: float
    change_5m_pct: float
    change_30m_pct: float
    change_1h_pct: float
    change_24h_pct: float
    quote_volume_24h: float
    volume_ratio_5m: float
    volume_ratio_30m: float
    volume_steps_up: int
    depth_usdt: float
    depth_change_pct: float | None
    bid_ask_ratio: float
    spread_pct: float
    big_buy_usdt: float
    big_sell_usdt: float
    big_trade_ratio: float
    announcement_match: bool
    announcement_titles: list[str]
    holder_change_pct: float | None
    pool_liquidity_change_pct: float | None
    score: float
    risk: str
    reasons: list[str]


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) and value.strip() else default


def get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    global _active_base

    bases = list(BINANCE_BASES)
    if _active_base:
        bases = [_active_base] + [base for base in bases if base != _active_base]

    last_error: Exception | None = None
    for base in bases:
        try:
            response = requests.get(
                f"{base}{path}",
                params=params or {},
                timeout=20,
                headers={"User-Agent": "BinanceRadarScanner/1.0"},
            )
            if response.status_code in {403, 418, 429, 451}:
                last_error = requests.HTTPError(f"{response.status_code} from {base}")
                continue
            response.raise_for_status()
            _active_base = base
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc

    raise RuntimeError(f"Binance market API erişilemedi: {last_error}")


def get_cms_json(path: str, params: dict[str, Any]) -> Any:
    response = requests.get(
        f"{CMS_BASE}{path}",
        params=params,
        timeout=20,
        headers={"User-Agent": "BinanceRadarScanner/1.0", "lang": "en"},
    )
    response.raise_for_status()
    return response.json()


def pct_change(old: float, new: float) -> float:
    if old <= 0:
        return 0.0
    return (new - old) / old * 100


def fmt_usdt(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def valid_symbol(symbol: str) -> bool:
    if not symbol.endswith(QUOTE_ASSET):
        return False
    base = symbol[: -len(QUOTE_ASSET)]
    if base in STABLE_BASES:
        return False
    if any(marker in base for marker in LEVERAGED_MARKERS):
        return False
    return True


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"depth": {}, "seen_announcements": []}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"depth": {}, "seen_announcements": []}
        data.setdefault("depth", {})
        data.setdefault("seen_announcements", [])
        return data
    except (json.JSONDecodeError, OSError):
        return {"depth": {}, "seen_announcements": []}


def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError as exc:
        print(f"[state] kayıt hatası: {exc}", file=sys.stderr)


def get_klines(symbol: str, interval: str = "5m", limit: int = 50) -> list[list[Any]]:
    data = get_json(
        "/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    if not isinstance(data, list):
        return []
    return data


def get_momentum_and_volume(
    symbol: str,
) -> tuple[float, float, float, float, float, int]:
    candles = get_klines(symbol, "5m", 49)
    if len(candles) < 25:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0

    closes = [float(candle[4]) for candle in candles]
    quote_volumes = [float(candle[7]) for candle in candles]

    change_5m = pct_change(closes[-2], closes[-1])
    change_30m = pct_change(closes[-7], closes[-1]) if len(closes) >= 7 else 0.0
    change_1h = pct_change(closes[-13], closes[-1]) if len(closes) >= 13 else 0.0

    recent_5m = quote_volumes[-1]
    baseline_5m = mean(quote_volumes[-25:-1])
    volume_ratio_5m = recent_5m / baseline_5m if baseline_5m > 0 else 0.0

    recent_30m = sum(quote_volumes[-6:])
    prior_30m_blocks = [sum(quote_volumes[i : i + 6]) for i in range(0, 36, 6)]
    baseline_30m = mean(prior_30m_blocks) if prior_30m_blocks else 0.0
    volume_ratio_30m = recent_30m / baseline_30m if baseline_30m > 0 else 0.0

    recent_steps = quote_volumes[-5:]
    volume_steps_up = sum(
        1 for previous, current in zip(recent_steps, recent_steps[1:]) if current > previous
    )

    return (
        change_5m,
        change_30m,
        change_1h,
        volume_ratio_5m,
        volume_ratio_30m,
        volume_steps_up,
    )


def get_orderbook_metrics(symbol: str) -> tuple[float, float, float]:
    data = get_json("/api/v3/depth", {"symbol": symbol, "limit": 100})
    bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in data.get("asks", [])]

    bid_value = sum(price * qty for price, qty in bids)
    ask_value = sum(price * qty for price, qty in asks)
    depth = bid_value + ask_value
    bid_ask_ratio = bid_value / ask_value if ask_value > 0 else 0.0

    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 0.0
    midpoint = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
    spread_pct = ((best_ask - best_bid) / midpoint * 100) if midpoint else 99.0

    return depth, bid_ask_ratio, spread_pct


def get_big_trade_metrics(symbol: str) -> tuple[float, float, float]:
    trades = get_json("/api/v3/aggTrades", {"symbol": symbol, "limit": 1000})
    big_buy = 0.0
    big_sell = 0.0

    for trade in trades:
        price = float(trade["p"])
        qty = float(trade["q"])
        value = price * qty
        if value < BIG_TRADE_USDT:
            continue
        if trade["m"]:
            big_sell += value
        else:
            big_buy += value

    ratio = big_buy / big_sell if big_sell > 0 else (99.0 if big_buy > 0 else 1.0)
    return big_buy, big_sell, ratio


def fetch_recent_announcements() -> list[dict[str, Any]]:
    all_articles: list[dict[str, Any]] = []
    for catalog_id in (48, 49, 93):
        try:
            payload = get_cms_json(
                "/bapi/composite/v1/public/cms/article/list/query",
                {
                    "type": 1,
                    "catalogId": catalog_id,
                    "pageNo": 1,
                    "pageSize": 20,
                },
            )
            catalogs = payload.get("data", {}).get("catalogs", [])
            for catalog in catalogs:
                all_articles.extend(catalog.get("articles", []))
        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"[announcement] alınamadı catalog={catalog_id}: {exc}", file=sys.stderr)

    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - 72 * 60 * 60 * 1000
    recent = [
        article
        for article in all_articles
        if int(article.get("releaseDate", 0) or 0) >= cutoff_ms
    ]

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for article in recent:
        code = str(article.get("code") or "")
        if code and code not in seen:
            unique.append(article)
            seen.add(code)
    return unique


def match_announcements(symbol: str, announcements: list[dict[str, Any]]) -> list[str]:
    base = symbol[: -len(QUOTE_ASSET)].upper()
    matches: list[str] = []
    for item in announcements:
        title = str(item.get("title", ""))
        upper = f" {title.upper()} "
        patterns = (
            f"({base})",
            f" {base} ",
            f" {base}:",
            f" {base},",
            f" {base}.",
            f" {base}/",
            f"/{base} ",
        )
        if any(pattern in upper for pattern in patterns):
            matches.append(title)
    return matches[:3]


def get_onchain_metrics_optional(symbol: str) -> tuple[float | None, float | None]:
    """
    On-chain holder / DEX pool likiditesi için güvenli placeholder.

    Gerçek entegrasyon için:
    1) Binance sembolü -> doğrulanmış kontrat + ağ
    2) Holder geçmişi sağlayan kaynak
    3) DEX pool likidite geçmişi

    Sembol tek başına kontrat adresi değildir; varsayılan N/A.
    """
    _ = symbol
    return None, None


def classify_risk(
    price: float,
    quote_volume_24h: float,
    depth_usdt: float,
    spread_pct: float,
    volume_ratio_30m: float,
) -> str:
    if (
        price < 0.01
        or quote_volume_24h < 5_000_000
        or depth_usdt < 150_000
        or spread_pct > 0.25
        or volume_ratio_30m > 10
    ):
        return "Yüksek"
    if quote_volume_24h < 20_000_000 or depth_usdt < 500_000 or spread_pct > 0.10:
        return "Orta"
    return "Düşük-Orta"


def score_candidate(
    change_5m: float,
    change_30m: float,
    change_1h: float,
    volume_ratio_5m: float,
    volume_ratio_30m: float,
    volume_steps_up: int,
    depth_usdt: float,
    depth_change_pct: float | None,
    bid_ask_ratio: float,
    spread_pct: float,
    big_trade_ratio: float,
    has_announcement: bool,
    holder_change_pct: float | None,
    pool_change_pct: float | None,
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    if 0 < change_5m <= 5:
        score += change_5m * 1.5

    if 0 < change_30m <= 12:
        score += change_30m * 2.0
        reasons.append(f"30dk momentum %+{change_30m:.2f}")

    if 0 < change_1h <= 20:
        score += change_1h * 1.0
        reasons.append(f"1s momentum %+{change_1h:.2f}")

    if volume_steps_up >= 3:
        score += volume_steps_up * 4
        reasons.append(f"5dk hacmi {volume_steps_up}/4 basamakta arttı")

    if 1.3 <= volume_ratio_5m <= 8:
        score += min(volume_ratio_5m - 1, 5) * 3

    if 1.5 <= volume_ratio_30m <= 8:
        score += min(volume_ratio_30m - 1, 6) * 4
        reasons.append(f"30dk hacim ortalamanın {volume_ratio_30m:.2f}x üzerinde")

    if depth_usdt >= MIN_DEPTH_USDT:
        score += min(math.log10(depth_usdt / MIN_DEPTH_USDT + 1) * 8, 12)

    if depth_change_pct is not None and depth_change_pct > 5:
        score += min(depth_change_pct / 5, 10)
        reasons.append(f"order-book derinliği %{depth_change_pct:+.1f}")

    if 1.15 <= bid_ask_ratio <= 4:
        score += min((bid_ask_ratio - 1) * 5, 12)
        reasons.append(f"alış baskısı bid/ask={bid_ask_ratio:.2f}")

    if spread_pct <= 0.08:
        score += 4
    elif spread_pct > 0.25:
        score -= 8
        reasons.append(f"geniş spread %{spread_pct:.2f}")

    if 1.3 <= big_trade_ratio <= 8:
        score += min((big_trade_ratio - 1) * 3, 10)
        reasons.append(f"büyük işlem akışı buy/sell={big_trade_ratio:.2f}")
    elif big_trade_ratio < 0.7:
        score -= 6

    if has_announcement:
        score += 8
        reasons.append("Binance duyuru eşleşmesi")

    if holder_change_pct is not None and holder_change_pct > 0:
        score += min(holder_change_pct / 2, 6)
        reasons.append(f"holder değişimi %{holder_change_pct:+.1f}")

    if pool_change_pct is not None and pool_change_pct > 0:
        score += min(pool_change_pct / 3, 6)
        reasons.append(f"havuz likiditesi %{pool_change_pct:+.1f}")

    # Aşırı uzamış kısa mumları cezalandır
    if change_5m > 8 or change_30m > 20:
        score -= 10
        reasons.append("kısa vadede aşırı uzamış")

    return score, reasons


def analyze_symbol(
    symbol: str,
    last_price: float,
    change_24h: float,
    quote_volume_24h: float,
    announcements: list[dict[str, Any]],
    state: dict[str, Any],
) -> Candidate | None:
    (
        change_5m,
        change_30m,
        change_1h,
        volume_ratio_5m,
        volume_ratio_30m,
        volume_steps_up,
    ) = get_momentum_and_volume(symbol)

    # Erken radar: hafif-orta yeşil + hacim artışı
    if change_30m <= 0.5 and volume_ratio_30m < 1.4:
        return None
    if change_30m > 18 or change_5m > 10:
        return None

    depth_usdt, bid_ask_ratio, spread_pct = get_orderbook_metrics(symbol)
    if depth_usdt < MIN_DEPTH_USDT:
        return None

    prev_depth = state.get("depth", {}).get(symbol)
    depth_change_pct: float | None = None
    if isinstance(prev_depth, (int, float)) and prev_depth > 0:
        depth_change_pct = pct_change(float(prev_depth), depth_usdt)
    state.setdefault("depth", {})[symbol] = depth_usdt

    big_buy, big_sell, big_ratio = get_big_trade_metrics(symbol)
    titles = match_announcements(symbol, announcements)
    holder_change, pool_change = get_onchain_metrics_optional(symbol)

    score, reasons = score_candidate(
        change_5m,
        change_30m,
        change_1h,
        volume_ratio_5m,
        volume_ratio_30m,
        volume_steps_up,
        depth_usdt,
        depth_change_pct,
        bid_ask_ratio,
        spread_pct,
        big_ratio,
        bool(titles),
        holder_change,
        pool_change,
    )

    if score < 12:
        return None

    risk = classify_risk(last_price, quote_volume_24h, depth_usdt, spread_pct, volume_ratio_30m)
    return Candidate(
        symbol=symbol,
        price=last_price,
        change_5m_pct=change_5m,
        change_30m_pct=change_30m,
        change_1h_pct=change_1h,
        change_24h_pct=change_24h,
        quote_volume_24h=quote_volume_24h,
        volume_ratio_5m=volume_ratio_5m,
        volume_ratio_30m=volume_ratio_30m,
        volume_steps_up=volume_steps_up,
        depth_usdt=depth_usdt,
        depth_change_pct=depth_change_pct,
        bid_ask_ratio=bid_ask_ratio,
        spread_pct=spread_pct,
        big_buy_usdt=big_buy,
        big_sell_usdt=big_sell,
        big_trade_ratio=big_ratio,
        announcement_match=bool(titles),
        announcement_titles=titles,
        holder_change_pct=holder_change,
        pool_liquidity_change_pct=pool_change,
        score=score,
        risk=risk,
        reasons=reasons,
    )


def scan_universe(max_symbols: int) -> list[tuple[str, float, float, float]]:
    tickers = get_json("/api/v3/ticker/24hr")
    rows: list[tuple[str, float, float, float]] = []
    for t in tickers:
        symbol = str(t.get("symbol") or "")
        if not valid_symbol(symbol):
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
            last = float(t.get("lastPrice") or 0)
            ch24 = float(t.get("priceChangePercent") or 0)
        except (TypeError, ValueError):
            continue
        if qv < MIN_24H_QUOTE_VOLUME or last <= 0:
            continue
        if ch24 < -5 or ch24 > 40:
            continue
        rows.append((symbol, last, ch24, qv))

    rows.sort(key=lambda x: x[3], reverse=True)
    return rows[:max_symbols]


def format_console(candidates: list[Candidate], top: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"BINANCE RADAR ({now})",
        "Metrikler: momentum + kademeli hacim + order book + büyük işlem + duyuru",
        "-" * 72,
    ]
    rows = candidates[:top]
    if not rows:
        lines.append("Şu an filtreye uyan aday yok.")
        return "\n".join(lines)

    best = rows[0]
    coin = best.symbol.replace(QUOTE_ASSET, "")
    lines.append("EN GUCLU ADAY:")
    lines.append(
        f"  >>> {coin}  ${best.price:.6g}  |  skor {best.score:.1f}  |  risk {best.risk}"
    )
    lines.append(
        f"      5dk %{best.change_5m_pct:+.2f} | 30dk %{best.change_30m_pct:+.2f} | "
        f"1s %{best.change_1h_pct:+.2f} | hacim30 {best.volume_ratio_30m:.2f}x"
    )
    lines.append(
        f"      derinlik {fmt_usdt(best.depth_usdt)} | bid/ask {best.bid_ask_ratio:.2f} | "
        f"big buy/sell {best.big_trade_ratio:.2f}"
    )
    if best.reasons:
        lines.append("      neden: " + " · ".join(best.reasons[:4]))
    lines.append("")
    lines.append(
        f"{'#':<3} {'COIN':<10} {'SCORE':>6} {'30m%':>7} {'VOLx':>6} "
        f"{'DEPTH':>8} {'B/A':>5} {'RISK':<10}"
    )
    for i, c in enumerate(rows, 1):
        name = c.symbol.replace(QUOTE_ASSET, "")
        lines.append(
            f"{i:<3} {name:<10} {c.score:>6.1f} {c.change_30m_pct:>+6.2f}% "
            f"{c.volume_ratio_30m:>5.2f}x {fmt_usdt(c.depth_usdt):>8} "
            f"{c.bid_ask_ratio:>5.2f} {c.risk:<10}"
        )
    lines.append("-" * 72)
    lines.append("Not: radar/alarm aracıdır; kesin yükseliş garantisi yoktur.")
    return "\n".join(lines)


def format_telegram(candidates: list[Candidate], top: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"<b>Binance Radar</b> ({now})", ""]
    rows = candidates[:top]
    if not rows:
        lines.append("Şu an filtreye uyan aday yok.")
        return "\n".join(lines)

    for i, c in enumerate(rows, 1):
        coin = c.symbol.replace(QUOTE_ASSET, "")
        lines.append(
            f"{i}) <b>{coin}</b> skor {c.score:.1f} | 30dk %{c.change_30m_pct:+.2f} | "
            f"hacim {c.volume_ratio_30m:.2f}x | risk {c.risk}"
        )
        if c.reasons:
            lines.append("   " + " · ".join(c.reasons[:3]))
    lines.append("")
    lines.append("Uyarı: sinyal değil, radar listesi.")
    return "\n".join(lines)


def telegram_send(token: str, chat_id: str, text: str) -> bool:
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
        print(f"[telegram] error: {exc}", file=sys.stderr)
        return False


def run_once(args: argparse.Namespace) -> int:
    print("Radar taranıyor…", flush=True)
    state = load_state()
    announcements = fetch_recent_announcements()
    universe = scan_universe(args.universe)

    found: list[Candidate] = []
    for i, (symbol, last, ch24, qv) in enumerate(universe):
        try:
            c = analyze_symbol(symbol, last, ch24, qv, announcements, state)
        except (requests.RequestException, RuntimeError, ValueError, KeyError, IndexError) as exc:
            print(f"[skip] {symbol}: {exc}", file=sys.stderr)
            continue
        if c:
            found.append(c)
        if args.sleep > 0 and i + 1 < len(universe):
            time.sleep(args.sleep)

    found.sort(key=lambda c: c.score, reverse=True)
    save_state(state)
    print(format_console(found, args.top))

    if args.telegram:
        token = env("TELEGRAM_BOT_TOKEN")
        chat_id = env("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID eksik", file=sys.stderr)
            return 1
        if not telegram_send(token, chat_id, format_telegram(found, args.top)):
            return 1
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Binance momentum/likidite radar tarayıcısı")
    p.add_argument("--top", type=int, default=10, help="Kaç aday gösterilsin")
    p.add_argument("--universe", type=int, default=MAX_SYMBOLS_TO_SCAN, help="İncelenecek sembol sayısı")
    p.add_argument("--sleep", type=float, default=0.08, help="Sembol arası bekleme (sn)")
    p.add_argument("--loop", action="store_true", help="Sürekli tara")
    p.add_argument("--interval", type=int, default=300, help="Loop aralığı sn (default 300)")
    p.add_argument("--telegram", action="store_true", help="Sonucu Telegram'a gönder (opsiyonel)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.loop:
        return run_once(args)

    while True:
        run_once(args)
        print(f"\nSonraki tarama ~{args.interval}s sonra…\n", flush=True)
        time.sleep(max(args.interval, 60))


if __name__ == "__main__":
    raise SystemExit(main())
