#!/usr/bin/env python3
"""
BTC / ETH 10 dakikalık yön skoru.

Kaynaklar (API anahtarı gerekmez):
  - OKX USDT perpetual: fiyat, funding, OI, emir defteri, L/S oranı, likidasyon, mum
  - Binance Vision (spot): mum + derinlik — OKX ile çapraz kontrol

Dürüst sınır:
  10 dakikalık yön %100 bilinemez. Motor varsayılanı 50/50'dir.
  Sinyaller çelişirse BEKLE der. En fazla ~68/32 verir; "kesin" demez.
  3 bağımsız onay yoksa oran 55/45 bandını geçmez.

Kullanım:
  python btc_eth_pulse.py --once
  python btc_eth_pulse.py --loop
  python btc_eth_pulse.py --serve --port 8080
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output"
LATEST_PATH = OUT_DIR / "pulse_latest.json"
HISTORY_PATH = OUT_DIR / "pulse_history.jsonl"
HTML_PATH = OUT_DIR / "pulse.html"

OKX = "https://www.okx.com"
VISION = "https://data-api.binance.vision"
UA = "btc-eth-pulse/1.0 (+local research; not financial advice)"

SYMBOLS = {
    "BTC": {
        "okx_swap": "BTC-USDT-SWAP",
        "okx_ccy": "BTC",
        "okx_uly": "BTC-USDT",
        "binance_spot": "BTCUSDT",
    },
    "ETH": {
        "okx_swap": "ETH-USDT-SWAP",
        "okx_ccy": "ETH",
        "okx_uly": "ETH-USDT",
        "binance_spot": "ETHUSDT",
    },
}

# Oran tavanı: daha yüksek iddia etmek veri kalitesini aşar.
MAX_TILT = 18.0  # 50±18 → 32/68 bandı
WEAK_TILT = 5.0  # 3 onay yoksa 50±5


@dataclass
class Signal:
    name: str
    score: float  # -1 down ... +1 up
    note: str
    weight: float = 1.0


@dataclass
class Pulse:
    symbol: str
    price: float
    up_pct: float
    down_pct: float
    action: str
    confidence: str
    confirms: int
    reasons: list[str]
    metrics: dict[str, Any] = field(default_factory=dict)
    signals: list[dict[str, Any]] = field(default_factory=list)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat()


def http_get_json(url: str, timeout: float = 15.0, retries: int = 3) -> Any:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(
                url,
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last}") from last


def okx_data(path: str) -> list[Any]:
    payload = http_get_json(f"{OKX}{path}")
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"OKX {path}: {payload.get('msg') or payload}")
    return payload.get("data") or []


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def signed_from_ratio(ratio: float, dead: float = 0.04) -> float:
    """ratio>1 alış baskısı. 1.0 etrafında ölü bölge."""
    if ratio <= 0:
        return -1.0
    edge = ratio - 1.0
    if abs(edge) < dead:
        return 0.0
    return clamp(edge / 0.35)


def fetch_okx_snapshot(spec: dict[str, str]) -> dict[str, Any]:
    inst = spec["okx_swap"]
    ccy = spec["okx_ccy"]
    uly = spec["okx_uly"]

    ticker = (okx_data(f"/api/v5/market/ticker?instId={inst}") or [{}])[0]
    funding = (okx_data(f"/api/v5/public/funding-rate?instId={inst}") or [{}])[0]
    oi_now = (okx_data(f"/api/v5/public/open-interest?instId={inst}") or [{}])[0]
    books = (okx_data(f"/api/v5/market/books?instId={inst}&sz=20") or [{}])[0]
    candles = okx_data(f"/api/v5/market/candles?instId={inst}&bar=5m&limit=8")
    trades = okx_data(f"/api/v5/market/trades?instId={inst}&limit=100")
    ls_hist = okx_data(
        f"/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={ccy}&period=5m"
    )
    oi_hist = okx_data(
        f"/api/v5/rubik/stat/contracts/open-interest-volume?ccy={ccy}&period=5m"
    )
    liqs = okx_data(
        f"/api/v5/public/liquidation-orders?instType=SWAP&uly={uly}&state=filled"
    )

    return {
        "ticker": ticker,
        "funding": funding,
        "oi_now": oi_now,
        "books": books,
        "candles": candles,
        "trades": trades,
        "ls_hist": ls_hist,
        "oi_hist": oi_hist,
        "liqs": liqs,
    }


def fetch_spot_cross(symbol: str) -> dict[str, Any] | None:
    try:
        klines = http_get_json(
            f"{VISION}/api/v3/klines?symbol={symbol}&interval=5m&limit=6"
        )
        depth = http_get_json(
            f"{VISION}/api/v3/depth?symbol={symbol}&limit=20"
        )
        return {"klines": klines, "depth": depth}
    except Exception:
        return None


def book_imbalance(books: dict[str, Any]) -> tuple[float, float, float]:
    bids = books.get("bids") or []
    asks = books.get("asks") or []
    bid_sz = sum(fnum(row[1]) * fnum(row[0]) for row in bids[:20])
    ask_sz = sum(fnum(row[1]) * fnum(row[0]) for row in asks[:20])
    total = bid_sz + ask_sz
    if total <= 0:
        return 0.0, 0.0, 0.0
    return bid_sz / total, bid_sz, ask_sz


def trade_pressure(trades: list[dict[str, Any]]) -> tuple[float, float, float]:
    buy = 0.0
    sell = 0.0
    for t in trades:
        notional = fnum(t.get("sz")) * fnum(t.get("px"))
        if (t.get("side") or "").lower() == "buy":
            buy += notional
        else:
            sell += notional
    total = buy + sell
    if total < 25_000:
        # Çok ince örnek → yanıltmasın
        return 1.0, buy, sell
    if sell <= 0:
        return 2.0, buy, sell
    if buy <= 0:
        return 0.5, buy, sell
    ratio = buy / sell
    # Aşırı uçları yumuşat (mikro işlem gürültüsü)
    return max(0.25, min(4.0, ratio)), buy, sell


def candle_return(candles: list[list[Any]], bars: int = 2) -> float:
    """OKX candles newest-first: [ts,o,h,l,c,...]."""
    if len(candles) < bars:
        return 0.0
    newest_close = fnum(candles[0][4])
    oldest_open = fnum(candles[bars - 1][1])
    if oldest_open <= 0:
        return 0.0
    return (newest_close - oldest_open) / oldest_open


def liq_split(liqs: list[Any], lookback_ms: int = 15 * 60 * 1000) -> dict[str, float]:
    now_ms = int(time.time() * 1000)
    long_usd = 0.0
    short_usd = 0.0
    rows = []
    if liqs and isinstance(liqs[0], dict):
        rows = liqs[0].get("details") or []
    for row in rows:
        ts = int(fnum(row.get("ts") or row.get("time")))
        if ts and now_ms - ts > lookback_ms:
            continue
        usd = fnum(row.get("sz")) * fnum(row.get("bkPx") or row.get("px"))
        side = (row.get("posSide") or "").lower()
        if side == "long":
            long_usd += usd
        elif side == "short":
            short_usd += usd
        else:
            # net: buy covers short, sell dumps long
            if (row.get("side") or "").lower() == "buy":
                short_usd += usd
            else:
                long_usd += usd
    return {"long_usd": long_usd, "short_usd": short_usd}


def score_snapshot(coin: str, snap: dict[str, Any], spot: dict[str, Any] | None) -> Pulse:
    ticker = snap["ticker"]
    price = fnum(ticker.get("last"))
    funding = fnum(snap["funding"].get("fundingRate"))
    oi_usd = fnum(snap["oi_now"].get("oiUsd"))

    imb, bid_usd, ask_usd = book_imbalance(snap["books"])
    taker_ratio, buy_usd, sell_usd = trade_pressure(snap["trades"])
    ret_10m = candle_return(snap["candles"], 2)
    ret_30m = candle_return(snap["candles"], 6)

    ls_now = 1.0
    if snap["ls_hist"]:
        ls_now = fnum(snap["ls_hist"][0][1], 1.0)

    oi_chg = 0.0
    if len(snap["oi_hist"]) >= 3:
        oi_now_v = fnum(snap["oi_hist"][0][1])
        oi_ago = fnum(snap["oi_hist"][2][1])
        if oi_ago:
            oi_chg = (oi_now_v - oi_ago) / oi_ago

    liq = liq_split(snap["liqs"])
    long_liq, short_liq = liq["long_usd"], liq["short_usd"]

    signals: list[Signal] = []

    # 1) Gerçekleşen işlem baskısı (en az yanıltan kısa vadeli sinyal)
    tr_score = signed_from_ratio(taker_ratio, dead=0.06)
    signals.append(
        Signal(
            "islem_baskisi",
            tr_score,
            f"son işlemler alış ${buy_usd:,.0f} / satış ${sell_usd:,.0f} (oran {taker_ratio:.2f})",
            weight=1.4,
        )
    )

    # 2) Emir defteri (spoof riski var → düşük ağırlık)
    book_score = 0.0
    book_note = f"bid payı={imb:.1%}"
    if imb:
        book_score = clamp((imb - 0.5) / 0.18)
        if abs(imb - 0.5) < 0.04:
            book_score = 0.0
        # Aşırı tek taraflı defter çoğu zaman spoof / ince likidite
        if imb < 0.15 or imb > 0.85:
            book_score *= 0.2
            book_note += " (aşırı dengesiz, spoof şüphesi — zayıflatıldı)"
        else:
            book_note += " (spoof olabilir)"
    signals.append(
        Signal(
            "emir_defteri",
            book_score,
            book_note,
            weight=0.7,
        )
    )

    # 3) 10dk mum — tek başına yetmez
    mom = 0.0
    if abs(ret_10m) >= 0.0008:
        mom = clamp(ret_10m / 0.006)
    signals.append(
        Signal("10dk_momentum", mom, f"10dk getiri={ret_10m*100:.2f}%", weight=0.8)
    )

    # 4) Funding: kalabalık tarafın tersi (aşırılıkta)
    fund_score = 0.0
    if abs(funding) >= 0.00025:
        fund_score = clamp(-funding / 0.0008)
    signals.append(
        Signal(
            "funding",
            fund_score,
            f"funding={funding*100:.4f}% (aşırı long→inme eğilimi)",
            weight=0.8,
        )
    )

    # 5) L/S hesabı: aşırı kalabalık = ters
    ls_score = 0.0
    if ls_now >= 1.35 or ls_now <= 0.75:
        ls_score = clamp((1.0 - ls_now) / 0.8)
    signals.append(
        Signal("long_short", ls_score, f"hesap L/S={ls_now:.2f}", weight=0.8)
    )

    # 6) OI + fiyat (klasik yapı)
    oi_score = 0.0
    oi_note = "OI/fiyat nötr"
    if abs(ret_10m) >= 0.0006 and abs(oi_chg) >= 0.0015:
        if ret_10m > 0 and oi_chg > 0:
            oi_score, oi_note = 0.55, "fiyat+OI yukarı: yeni long, devam eğilimi"
        elif ret_10m < 0 and oi_chg > 0:
            oi_score, oi_note = -0.55, "fiyat↓ OI↑: yeni short, düşüş eğilimi"
        elif ret_10m > 0 and oi_chg < 0:
            oi_score, oi_note = 0.45, "fiyat↑ OI↓: short kapanışı (squeeze)"
        elif ret_10m < 0 and oi_chg < 0:
            # cascade veya long kapanış — yön belirsiz, hafif devam
            oi_score, oi_note = -0.20, "fiyat↓ OI↓: long kapanış/cascade — erken long yok"
    signals.append(Signal("oi_fiyat", oi_score, oi_note, weight=1.2))

    # 7) Gerçekleşen likidasyon (tahmin haritası değil)
    liq_score = 0.0
    liq_note = "likidasyon dengeli/az"
    liq_tot = long_liq + short_liq
    if liq_tot >= 50_000:
        if long_liq > short_liq * 2.2:
            # long patlıyor: cascade devamı, bounce iddiası yok
            liq_score = -0.7 if ret_10m < 0 else 0.15
            liq_note = (
                f"long likidasyon baskın (${long_liq:,.0f} vs ${short_liq:,.0f})"
            )
        elif short_liq > long_liq * 2.2:
            liq_score = 0.7 if ret_10m > 0 else -0.15
            liq_note = (
                f"short likidasyon baskın (${short_liq:,.0f} vs ${long_liq:,.0f})"
            )
    signals.append(Signal("likidasyon", liq_score, liq_note, weight=1.3))

    # 8) Spot çapraz kontrol — OKX swap ile ters düşerse skoru zayıflat
    spot_ret = 0.0
    if spot and spot.get("klines") and len(spot["klines"]) >= 3:
        k = spot["klines"]
        o = fnum(k[-3][1])
        c = fnum(k[-1][4])
        if o:
            spot_ret = (c - o) / o
    agree_spot = True
    if abs(ret_10m) >= 0.001 and abs(spot_ret) >= 0.001:
        agree_spot = (ret_10m > 0) == (spot_ret > 0)

    # Ağırlıklı skor
    num = sum(s.score * s.weight for s in signals if abs(s.score) > 0.04)
    den = sum(s.weight for s in signals if abs(s.score) > 0.04) or 1.0
    raw = num / den
    if not agree_spot:
        raw *= 0.55

    up_votes = sum(1 for s in signals if s.score > 0.18)
    down_votes = sum(1 for s in signals if s.score < -0.18)
    confirms = max(up_votes, down_votes)

    tilt = MAX_TILT * math.tanh(abs(raw) * 1.6)
    if confirms < 3:
        tilt = min(tilt, WEAK_TILT)
        confidence = "DUSUK"
    elif abs(raw) >= 0.42 and confirms >= 4 and agree_spot:
        confidence = "ORTA"
    else:
        confidence = "DUSUK-ORTA"
        tilt = min(tilt, 12.0)

    if abs(raw) < 0.12 or confirms < 2:
        up_pct = 50.0
        down_pct = 50.0
        action = "BEKLE"
        confidence = "NOUTR"
    elif raw > 0:
        up_pct = round(50.0 + tilt, 1)
        down_pct = round(100.0 - up_pct, 1)
        action = "CIKIS_EGILIMI" if confirms >= 3 else "BEKLE"
    else:
        down_pct = round(50.0 + tilt, 1)
        up_pct = round(100.0 - down_pct, 1)
        action = "INIS_EGILIMI" if confirms >= 3 else "BEKLE"

    reasons = [s.note for s in signals if abs(s.score) >= 0.18]
    if not agree_spot:
        reasons.append("spot ve perpetual 10dk yönü uyuşmuyor → skor kısıldı")
    if confirms < 3:
        reasons.append("3 bağımsız onay yok → güçlü çağrı yok")

    return Pulse(
        symbol=coin,
        price=price,
        up_pct=up_pct,
        down_pct=down_pct,
        action=action,
        confidence=confidence,
        confirms=confirms,
        reasons=reasons[:6],
        metrics={
            "funding_pct": round(funding * 100, 5),
            "oi_usd": round(oi_usd),
            "oi_chg_10m_pct": round(oi_chg * 100, 3),
            "ls_ratio": round(ls_now, 3),
            "ret_10m_pct": round(ret_10m * 100, 3),
            "ret_30m_pct": round(ret_30m * 100, 3),
            "taker_buy_sell": round(taker_ratio, 3),
            "book_bid_share": round(imb, 3),
            "book_bid_usd": round(bid_usd),
            "book_ask_usd": round(ask_usd),
            "trade_buy_usd": round(buy_usd),
            "trade_sell_usd": round(sell_usd),
            "long_liq_usd_15m": round(long_liq),
            "short_liq_usd_15m": round(short_liq),
            "spot_ret_10m_pct": round(spot_ret * 100, 3),
            "spot_perp_agree": agree_spot,
        },
        signals=[
            {
                "name": s.name,
                "score": round(s.score, 3),
                "weight": s.weight,
                "note": s.note,
            }
            for s in signals
        ],
    )


def build_report(interval_min: int = 10) -> dict[str, Any]:
    coins: list[dict[str, Any]] = []
    errors: list[str] = []
    for coin, spec in SYMBOLS.items():
        try:
            snap = fetch_okx_snapshot(spec)
            spot = fetch_spot_cross(spec["binance_spot"])
            pulse = score_snapshot(coin, snap, spot)
            coins.append(asdict(pulse))
        except Exception as exc:
            errors.append(f"{coin}: {exc}")
            coins.append(
                {
                    "symbol": coin,
                    "price": None,
                    "up_pct": 50.0,
                    "down_pct": 50.0,
                    "action": "HATA_BEKLE",
                    "confidence": "YOK",
                    "confirms": 0,
                    "reasons": [str(exc)],
                    "metrics": {},
                    "signals": [],
                }
            )
    return {
        "as_of_utc": iso(),
        "interval_min": interval_min,
        "disclaimer": (
            "Bu bir olasılık skoru, kehanet değil. 10dk yön hatasız bilinemez. "
            "3 onay yoksa BEKLE. Yatırım tavsiyesi değildir."
        ),
        "sources": ["OKX SWAP public API", "Binance Vision spot (çapraz)"],
        "coins": coins,
        "errors": errors,
    }


def format_console(report: dict[str, Any]) -> str:
    lines = [
        "",
        "=" * 64,
        f"BTC/ETH PULSE  {report['as_of_utc']}",
        report["disclaimer"],
        "=" * 64,
    ]
    for c in report["coins"]:
        lines.append(
            f"\n{c['symbol']}  ${c['price']:,.2f}"
            if c.get("price")
            else f"\n{c['symbol']}  (veri yok)"
        )
        lines.append(
            f"  CIKACAK (yukarı): %{c['up_pct']:.1f}   "
            f"INECEK (aşağı): %{c['down_pct']:.1f}"
        )
        lines.append(
            f"  Aksiyon: {c['action']}   Güven: {c['confidence']}   "
            f"Onay: {c['confirms']}/7"
        )
        for r in c.get("reasons") or []:
            lines.append(f"   - {r}")
    if report.get("errors"):
        lines.append("\nHatalar:")
        for e in report["errors"]:
            lines.append(f"  ! {e}")
    lines.append("")
    return "\n".join(lines)


def format_telegram(report: dict[str, Any]) -> str:
    """Kısa saatlik özet — telefonda okunur."""
    lines = [
        f"BTC/ETH PULSE ({report.get('interval_min', 60)}dk)",
        f"UTC: {report['as_of_utc'][:19]}",
        "",
    ]
    for c in report["coins"]:
        price = f"${c['price']:,.2f}" if c.get("price") else "—"
        lines.append(f"{c['symbol']} {price}")
        lines.append(f"  ↑%{c['up_pct']:.0f}  ↓%{c['down_pct']:.0f}  → {c['action']}")
        lines.append(f"  güven {c['confidence']} | onay {c['confirms']}/7")
        for r in (c.get("reasons") or [])[:2]:
            lines.append(f"  • {r}")
        lines.append("")
    lines.append("Not: olasılık skoru, kehanet değil.")
    return "\n".join(lines)


def write_outputs(report: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False) + "\n")
    HTML_PATH.write_text(render_html(report), encoding="utf-8")


def render_html(report: dict[str, Any]) -> str:
    cards = []
    for c in report["coins"]:
        up = c["up_pct"]
        down = c["down_pct"]
        price = f"${c['price']:,.2f}" if c.get("price") else "—"
        reasons = "".join(f"<li>{r}</li>" for r in (c.get("reasons") or []))
        cards.append(
            f"""
            <article class="card">
              <header>
                <h2>{c['symbol']}</h2>
                <div class="price">{price}</div>
              </header>
              <div class="bar">
                <div class="up" style="width:{up}%">↑ {up:.1f}%</div>
                <div class="down" style="width:{down}%">↓ {down:.1f}%</div>
              </div>
              <p class="meta">{c['action']} · güven {c['confidence']} · onay {c['confirms']}/7</p>
              <ul>{reasons}</ul>
            </article>
            """
        )
    joined = "\n".join(cards)
    return f"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8"/>
  <meta http-equiv="refresh" content="60"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>BTC/ETH Pulse</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; background:#0b1220; color:#e8eefc; margin:0; padding:24px; }}
    h1 {{ margin:0 0 8px; }}
    .sub {{ color:#9bb0d3; max-width:720px; }}
    .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); margin-top:20px; }}
    .card {{ background:#121a2b; border:1px solid #24324d; border-radius:14px; padding:16px; }}
    .price {{ font-size:22px; font-weight:700; }}
    .bar {{ display:flex; height:28px; border-radius:8px; overflow:hidden; margin:12px 0; font-size:13px; }}
    .up {{ background:#147a4b; display:flex; align-items:center; justify-content:center; }}
    .down {{ background:#8b2d3b; display:flex; align-items:center; justify-content:center; }}
    ul {{ margin:8px 0 0; padding-left:18px; color:#c5d3ea; }}
    .meta {{ color:#9bb0d3; }}
  </style>
</head>
<body>
  <h1>BTC / ETH 10dk yön skoru</h1>
  <p class="sub">{report['disclaimer']}</p>
  <p class="sub">Güncelleme (UTC): {report['as_of_utc']} · kaynak: OKX SWAP + Binance Vision</p>
  <div class="grid">{joined}</div>
</body>
</html>
"""


def maybe_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps(
        {"chat_id": chat, "text": text[:3500], "disable_web_page_preview": True}
    ).encode()
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as resp:
            resp.read()
    except Exception as exc:
        print(f"Telegram gönderilemedi: {exc}", file=sys.stderr)


class PulseHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/pulse.html"}:
            target, ctype = HTML_PATH, "text/html; charset=utf-8"
        elif path in {"/latest.json", "/api/latest.json"}:
            target, ctype = LATEST_PATH, "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        if not target.exists():
            self.send_error(503, "Henüz veri yok")
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def load_dotenv(path: Path | None = None) -> None:
    """Basit .env yükleyici — python-dotenv şart değil."""
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def run_cycle(
    send_tg: bool,
    *,
    compact_tg: bool = False,
    interval_min: int = 10,
) -> dict[str, Any]:
    report = build_report(interval_min=interval_min)
    write_outputs(report)
    text = format_console(report)
    print(text, flush=True)
    if send_tg:
        maybe_telegram(format_telegram(report) if compact_tg else text)
    return report


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="BTC/ETH yön skoru")
    parser.add_argument("--once", action="store_true", help="Tek ölçüm al, çık")
    parser.add_argument("--loop", action="store_true", help="Belirli aralıkla tekrarla")
    parser.add_argument("--hourly", action="store_true", help="Her 1 saatte Telegram özeti")
    parser.add_argument("--serve", action="store_true", help="Loop + yerel web paneli")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Saniye (varsayılan: 600; --hourly ile 3600)",
    )
    parser.add_argument("--telegram", action="store_true", help="Varsa Telegram'a da at")
    args = parser.parse_args()

    if args.hourly:
        args.loop = True
        args.telegram = True
        if args.interval is None:
            args.interval = 3600
    if args.interval is None:
        args.interval = 600

    compact = bool(args.hourly or args.telegram)
    interval_min = max(1, int(round(args.interval / 60)))

    if args.serve:
        run_cycle(args.telegram, compact_tg=compact, interval_min=interval_min)
        server = ThreadingHTTPServer(("0.0.0.0", args.port), PulseHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(
            f"Panel: http://127.0.0.1:{args.port}/  (her {args.interval}s)",
            flush=True,
        )
        try:
            while True:
                time.sleep(args.interval)
                run_cycle(args.telegram, compact_tg=compact, interval_min=interval_min)
        except KeyboardInterrupt:
            server.shutdown()
            return 0

    if args.loop or args.hourly:
        while True:
            run_cycle(args.telegram, compact_tg=compact, interval_min=interval_min)
            time.sleep(args.interval)

    run_cycle(args.telegram, compact_tg=compact, interval_min=interval_min)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
