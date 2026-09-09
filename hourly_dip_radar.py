#!/usr/bin/env python3
"""
Binance USDT spot saatlik dip / dönüş radarı.

Orijinal 1s websocket kodundaki hatalar:
  - Birleşik stream /ws/ altında açılıyordu (doğrusu /stream?streams=)
  - 200 sembolle sınırlıydı, 1 saniyelik mum saatlik izleme için uygun değildi
  - Combined mesaj sarmalayıcısı ({stream, data}) okunmuyordu

Bu sürüm:
  - Tüm USDT spot paritelerini tarar (stable / kaldıraçlı token hariç)
  - 1 saatlik mum kullanır
  - Birden fazla dönüş metodunu aynı mumda skorlar
  - Önce son kapanmış saati REST ile tarar, sonra canlı 1h websocket ile izler

Kullanım:
  python hourly_dip_radar.py
  python hourly_dip_radar.py --once
  python hourly_dip_radar.py --all-symbols
  python hourly_dip_radar.py --min-score 2
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

try:
    import websocket
except ImportError:
    websocket = None  # type: ignore[assignment]


BINANCE_REST = "https://data-api.binance.vision"
BINANCE_WS = "wss://data-stream.binance.vision/stream"
KLINE_INTERVAL = "1h"
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDP", "TUSD", "EUR", "AEUR", "USD1"}
LEVERAGE_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
HTTP_TIMEOUT = 20
MAX_STREAMS_PER_SOCKET = 80


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "hourly-dip-radar/1.0"})


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    taker_buy_vol: float
    closed: bool = True

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def delta(self) -> float:
        sell = self.volume - self.taker_buy_vol
        return self.taker_buy_vol - sell

    @property
    def buy_ratio(self) -> float:
        if self.volume <= 0:
            return 0.0
        return min(self.taker_buy_vol / self.volume, 1.0)

    @property
    def rejection(self) -> float:
        if self.range <= 0:
            return 0.0
        return self.lower_wick / self.range


def parse_kline_rest(row: list[Any]) -> Candle:
    close_time = int(row[6])
    now_ms = int(time.time() * 1000)
    return Candle(
        open_time=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        close_time=close_time,
        taker_buy_vol=float(row[10]),
        closed=close_time <= now_ms,
    )


def parse_kline_ws(k: dict[str, Any]) -> Candle:
    return Candle(
        open_time=int(k["t"]),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k["v"]),
        close_time=int(k["T"]),
        taker_buy_vol=float(k["V"]),
        closed=bool(k.get("x")),
    )


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def detect_methods(candles: list[Candle]) -> list[str]:
    """Son mum üzerinde birden fazla dönüş metodunu çalıştırır."""
    if len(candles) < 2:
        return []
    cur = candles[-1]
    prev = candles[-2]
    methods: list[str] = []

    if cur.range <= 0 or cur.volume <= 0:
        return methods

    # 1) Dipten iğne reddi (GVR)
    if cur.rejection >= 0.20 and cur.close >= cur.open:
        methods.append("IGNE_REDDI")

    # 2) Pozitif delta / net alıcı
    if cur.delta > 0 and cur.buy_ratio >= 0.52:
        methods.append("POZITIF_DELTA")

    # 3) Hammer / pin bar
    if (
        cur.body > 0
        and cur.lower_wick >= 2.0 * cur.body
        and cur.upper_wick <= 0.4 * cur.body
        and cur.close >= cur.open
    ):
        methods.append("HAMMER")

    # 4) Yutan boğa (bullish engulfing)
    prev_bear = prev.close < prev.open
    cur_bull = cur.close > cur.open
    if prev_bear and cur_bull and cur.open <= prev.close and cur.close >= prev.open:
        methods.append("YUTAN_BOGA")

    # 5) Hacim patlaması + yeşil kapanış
    vols = [c.volume for c in candles[:-1]]
    vol_avg = sma(vols, 20) if len(vols) >= 20 else sma(vols, min(len(vols), 10))
    if vol_avg and cur.volume >= 1.5 * vol_avg and cur.close > cur.open:
        methods.append("HACIM_PATLAMASI")

    # 6) RSI toparlanması
    closes = [c.close for c in candles]
    cur_rsi = rsi(closes)
    prev_rsi = rsi(closes[:-1]) if len(closes) > 15 else None
    if cur_rsi is not None and prev_rsi is not None and prev_rsi <= 35 and cur_rsi > prev_rsi:
        methods.append("RSI_TOPARLAMA")

    # 7) Dip + net alıcı (orijinal kombine filtre)
    if cur.delta > 0 and cur.rejection >= 0.20:
        methods.append("DIP_TOPLAMA")

    return methods


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    r = SESSION.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def is_tradeable_usdt(symbol: str) -> bool:
    if not symbol.endswith("USDT"):
        return False
    base = symbol[:-4]
    if base in STABLE_BASES:
        return False
    if any(symbol.endswith(suf) for suf in LEVERAGE_SUFFIXES):
        return False
    return True


def get_universe(dip_only: bool, min_change: float, max_drop: float, min_quote_vol: float) -> list[dict[str, Any]]:
    print("Tum Binance USDT spot taranıyor...")
    rows = get_json(f"{BINANCE_REST}/api/v3/ticker/24hr")
    universe: list[dict[str, Any]] = []
    for item in rows:
        symbol = item.get("symbol") or ""
        if not is_tradeable_usdt(symbol):
            continue
        try:
            change = float(item["priceChangePercent"])
            last = float(item["lastPrice"])
            quote_vol = float(item.get("quoteVolume") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if last <= 0 or quote_vol < min_quote_vol:
            continue
        in_dip = max_drop <= change <= min_change
        if dip_only and not in_dip:
            continue
        universe.append(
            {
                "symbol": symbol,
                "change": change,
                "last": last,
                "quote_vol": quote_vol,
                "in_dip": in_dip,
            }
        )
    universe.sort(key=lambda x: x["quote_vol"], reverse=True)
    print(f"Takibe alınan parite: {len(universe)}")
    return universe


def fetch_klines(symbol: str, limit: int = 50) -> list[Candle]:
    rows = get_json(
        f"{BINANCE_REST}/api/v3/klines",
        params={"symbol": symbol, "interval": KLINE_INTERVAL, "limit": limit},
    )
    if not isinstance(rows, list):
        return []
    return [parse_kline_rest(row) for row in rows]


def scan_symbol(meta: dict[str, Any], min_score: int) -> dict[str, Any] | None:
    symbol = meta["symbol"]
    try:
        candles = fetch_klines(symbol, limit=50)
    except (requests.RequestException, ValueError) as exc:
        print(f"[warn] {symbol} kline alınamadı: {exc}", file=sys.stderr)
        return None
    if len(candles) < 3:
        return None
    # REST son mumu henüz kapanmamış olabilir; kapanmış son saati kullan
    closed = candles[:-1] if not candles[-1].closed else candles
    if len(closed) < 3:
        return None
    candle = closed[-1]
    now_ms = int(time.time() * 1000)
    # Ölü / delist paritelerin yıllar önceki son mumunu alma
    if now_ms - candle.close_time > 3 * 60 * 60 * 1000:
        return None
    if candle.taker_buy_vol < 0 or candle.taker_buy_vol > candle.volume * 1.01:
        return None
    methods = detect_methods(closed)
    if len(methods) < min_score:
        return None
    return {
        "symbol": symbol,
        "change_24h": meta["change"],
        "in_dip": meta["in_dip"],
        "price": candle.close,
        "delta": candle.delta,
        "rejection": candle.rejection,
        "buy_ratio": candle.buy_ratio,
        "volume": candle.volume,
        "open_time": candle.open_time,
        "methods": methods,
        "score": len(methods),
    }


def fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def print_hit(hit: dict[str, Any], live: bool = False) -> None:
    tag = "CANLI" if live else "KAPANMIS SAAT"
    dip = "DIP" if hit.get("in_dip") else "SPOT"
    methods = ",".join(hit["methods"])
    print(
        f"[{tag}] {hit['symbol']:<12} {dip:<4} "
        f"Fiyat: ${hit['price']:<12.6g} "
        f"24s: {hit['change_24h']:+6.2f}% "
        f"Delta: {hit['delta']:+10.2f} "
        f"Igne: %{hit['rejection']*100:5.1f} "
        f"Alis: %{hit['buy_ratio']*100:5.1f} "
        f"Skor: {hit['score']} "
        f"| {methods} "
        f"| {fmt_time(hit['open_time'])} UTC"
    )


def has_buy_pressure(hit: dict[str, Any]) -> bool:
    return "POZITIF_DELTA" in hit["methods"] or "DIP_TOPLAMA" in hit["methods"]


def rest_scan(universe: list[dict[str, Any]], min_score: int, workers: int, require_delta: bool) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    print(f"Saatlik mumlar çekiliyor ({len(universe)} parite, {workers} işçi)...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(scan_symbol, meta, min_score): meta["symbol"] for meta in universe}
        for fut in as_completed(futs):
            hit = fut.result()
            if not hit:
                continue
            if require_delta and not has_buy_pressure(hit):
                continue
            hits.append(hit)
            print_hit(hit)
    hits.sort(key=lambda x: (x["score"], x["buy_ratio"], -abs(x["change_24h"])), reverse=True)
    return hits


def chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class HourlySocket:
    def __init__(
        self,
        symbols: list[str],
        min_score: int,
        cache: dict[str, list[Candle]],
        lock: threading.Lock,
        require_delta: bool,
    ):
        self.symbols = [s.lower() for s in symbols]
        self.min_score = min_score
        self.cache = cache
        self.lock = lock
        self.require_delta = require_delta
        self.seen: set[str] = set()
        self.ws: Any = None

    def _url(self) -> str:
        streams = "/".join(f"{s}@kline_{KLINE_INTERVAL}" for s in self.symbols)
        return f"{BINANCE_WS}?streams={streams}"

    def on_message(self, _ws: Any, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or "k" not in data:
            return
        k = data["k"]
        symbol = str(k.get("s") or "")
        candle = parse_kline_ws(k)
        if not candle.closed:
            return
        key = f"{symbol}:{candle.open_time}"
        if key in self.seen:
            return
        self.seen.add(key)

        with self.lock:
            hist = list(self.cache.get(symbol, []))
            if hist and hist[-1].open_time == candle.open_time:
                hist[-1] = candle
            else:
                hist.append(candle)
            if len(hist) > 60:
                hist = hist[-60:]
            self.cache[symbol] = hist
            methods = detect_methods(hist)

        if len(methods) < self.min_score:
            return
        hit = {
            "symbol": symbol,
            "change_24h": 0.0,
            "in_dip": True,
            "price": candle.close,
            "delta": candle.delta,
            "rejection": candle.rejection,
            "buy_ratio": candle.buy_ratio,
            "volume": candle.volume,
            "open_time": candle.open_time,
            "methods": methods,
            "score": len(methods),
        }
        if self.require_delta and not has_buy_pressure(hit):
            return
        print_hit(hit, live=True)

    def run(self) -> None:
        if websocket is None:
            raise SystemExit("websocket-client yüklü değil. pip install websocket-client")
        url = self._url()

        def on_error(_ws: Any, err: Any) -> None:
            print(f"[ws] hata: {err}", file=sys.stderr)

        def on_close(_ws: Any, status: Any, msg: Any) -> None:
            print(f"[ws] kapandı status={status} msg={msg}")

        def on_open(_ws: Any) -> None:
            print(f"[ws] bağlandı ({len(self.symbols)} stream)")

        while True:
            self.ws = websocket.WebSocketApp(
                url,
                on_message=self.on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )
            self.ws.run_forever(ping_interval=20, ping_timeout=10)
            print("[ws] 5 sn sonra yeniden bağlanılacak...")
            time.sleep(5)


def seed_cache(universe: list[dict[str, Any]], workers: int) -> dict[str, list[Candle]]:
    cache: dict[str, list[Candle]] = {}
    print("Websocket için saatlik geçmiş yükleniyor...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_klines, meta["symbol"], 50): meta["symbol"] for meta in universe}
        for fut in as_completed(futs):
            symbol = futs[fut]
            try:
                cache[symbol] = fut.result()
            except (requests.RequestException, ValueError):
                continue
    return cache


def print_summary(hits: list[dict[str, Any]]) -> None:
    print("\n========== SAATLIK TARAMA OZETI ==========")
    if not hits:
        print("Bu saatte skor eşiğini geçen sinyal yok.")
        return
    print(f"Toplam sinyal: {len(hits)}")
    print(f"{'SKOR':<6}{'SEMBOL':<12}{'24S':>8}  METODLAR")
    for hit in hits[:40]:
        print(
            f"{hit['score']:<6}{hit['symbol']:<12}{hit['change_24h']:+7.2f}%  "
            f"{','.join(hit['methods'])}"
        )
    print("=========================================\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Binance saatlik dip / dönüş radarı")
    p.add_argument("--once", action="store_true", help="Tek REST taraması, websocket yok")
    p.add_argument("--all-symbols", action="store_true", help="Dip filtresini kapat, tüm USDT'yi tara")
    p.add_argument("--min-change", type=float, default=-3.0, help="Dip üst eşiği (varsayılan -3)")
    p.add_argument("--max-drop", type=float, default=-30.0, help="Dip alt eşiği (varsayılan -30)")
    p.add_argument("--min-score", type=int, default=2, help="Kaç metod aynı anda tutmalı")
    p.add_argument("--loose", action="store_true", help="Net alıcı şartını kapat (sadece iğne/RSI de yeter)")
    p.add_argument("--workers", type=int, default=8, help="REST paralel işçi sayısı")
    p.add_argument("--refresh-min", type=int, default=60, help="REST taramasını kaç dakikada bir tekrarla")
    p.add_argument("--no-ws", action="store_true", help="Sadece REST döngüsü")
    p.add_argument("--min-quote-vol", type=float, default=200_000, help="24s min USDT hacim")
    p.add_argument("--max-ws", type=int, default=400, help="Websocket'e alınacak en likit parite sayısı")
    p.add_argument("--max-scan", type=int, default=0, help="REST taramasını ilk N pariteyle sınırla (0=hepsi)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    print("=" * 58)
    print("  DIPTEKI KOINLERIN SAATLIK DONUS RADARI")
    print("  Metodlar: IGNE_REDDI, POZITIF_DELTA, HAMMER, YUTAN_BOGA,")
    print("            HACIM_PATLAMASI, RSI_TOPARLAMA, DIP_TOPLAMA")
    print("=" * 58)

    dip_only = not args.all_symbols
    universe = get_universe(dip_only, args.min_change, args.max_drop, args.min_quote_vol)
    if args.max_scan and args.max_scan > 0:
        universe = universe[: args.max_scan]
        print(f"Tarama ilk {len(universe)} pariteyle sınırlandı")
    if not universe:
        print("Taranacak parite yok.")
        return 1

    require_delta = not args.loose
    hits = rest_scan(universe, args.min_score, args.workers, require_delta)
    print_summary(hits)

    if args.once:
        return 0

    if args.no_ws:
        while True:
            time.sleep(max(60, args.refresh_min * 60))
            universe = get_universe(dip_only, args.min_change, args.max_drop, args.min_quote_vol)
            hits = rest_scan(universe, args.min_score, args.workers, require_delta)
            print_summary(hits)
        return 0

    if websocket is None:
        print("websocket-client yok; pip install websocket-client", file=sys.stderr)
        print("REST döngüsüne düşülüyor.")
        while True:
            time.sleep(max(60, args.refresh_min * 60))
            universe = get_universe(dip_only, args.min_change, args.max_drop, args.min_quote_vol)
            hits = rest_scan(universe, args.min_score, args.workers, require_delta)
            print_summary(hits)
        return 0

    ws_universe = universe[: args.max_ws]
    cache = seed_cache(ws_universe, args.workers)
    lock = threading.Lock()
    batches = chunks([m["symbol"] for m in ws_universe], MAX_STREAMS_PER_SOCKET)
    print(f"Canlı 1h websocket: {len(ws_universe)} parite, {len(batches)} bağlantı")
    for batch in batches:
        sock = HourlySocket(batch, args.min_score, cache, lock, require_delta)
        threading.Thread(target=sock.run, daemon=True).start()
        time.sleep(0.4)

    print("Saat kapanışında sinyaller yazılacak. Durdurmak için Ctrl+C.")
    try:
        while True:
            time.sleep(max(60, args.refresh_min * 60))
            universe = get_universe(dip_only, args.min_change, args.max_drop, args.min_quote_vol)
            hits = rest_scan(universe, args.min_score, args.workers, require_delta)
            print_summary(hits)
    except KeyboardInterrupt:
        print("\nDurduruldu.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
