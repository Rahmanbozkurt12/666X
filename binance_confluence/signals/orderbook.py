#!/usr/bin/env python3
"""Order-book imbalance via Binance depth (REST snapshot + optional WS cache)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from binance_confluence.signals.base import Signal

try:
    import websockets
except ImportError:  # optional
    websockets = None  # type: ignore


class OrderBookCache:
    """Keeps top-of-book depth for selected symbols (WS if available)."""

    def __init__(self, top_levels: int = 10) -> None:
        self.top_levels = top_levels
        self._books: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def update_rest(self, symbol: str, depth: dict[str, Any]) -> None:
        with self._lock:
            self._books[symbol] = {
                "bids": depth.get("bids") or [],
                "asks": depth.get("asks") or [],
                "ts": time.time(),
            }

    def get(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            return self._books.get(symbol)

    def start_ws(self, symbols: list[str]) -> None:
        if not websockets or not symbols:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._ws_loop(symbols)),
            name="binance-depth-ws",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    async def _ws_loop(self, symbols: list[str]) -> None:
        assert websockets is not None
        streams = "/".join(f"{s.lower()}@depth{self.top_levels}@100ms" for s in symbols)
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        msg = json.loads(raw)
                        data = msg.get("data") or msg
                        sym = (data.get("s") or "").upper()
                        if not sym:
                            # combined stream may omit s in some payloads
                            stream = msg.get("stream") or ""
                            sym = stream.split("@")[0].upper() if stream else ""
                        bids = data.get("bids") or data.get("b") or []
                        asks = data.get("asks") or data.get("a") or []
                        if sym and (bids or asks):
                            with self._lock:
                                self._books[sym] = {"bids": bids, "asks": asks, "ts": time.time()}
            except Exception:
                await asyncio.sleep(3)


def imbalance_ratio(book: dict[str, Any], top_levels: int = 10) -> float | None:
    bids = (book.get("bids") or [])[:top_levels]
    asks = (book.get("asks") or [])[:top_levels]
    if not bids or not asks:
        return None
    bid_vol = sum(float(x[1]) for x in bids)
    ask_vol = sum(float(x[1]) for x in asks)
    if ask_vol <= 0:
        return None
    return bid_vol / ask_vol


def scan_orderbook_imbalance(
    client: Any,
    symbols: list[str],
    cache: OrderBookCache | None,
    *,
    top_levels: int = 10,
    min_ratio: float = 3.0,
    weight: float = 1.2,
) -> list[Signal]:
    out: list[Signal] = []
    for sym in symbols:
        book = cache.get(sym) if cache else None
        if not book:
            try:
                depth = client.get_order_book(symbol=sym, limit=top_levels)
                if cache:
                    cache.update_rest(sym, depth)
                book = depth
            except Exception:
                continue
        ratio = imbalance_ratio(book, top_levels)
        if ratio is None or ratio < min_ratio:
            continue
        score = weight * min(ratio / min_ratio, 3.0)
        out.append(
            Signal(
                name="orderbook_imbalance",
                symbol=sym,
                score=score,
                reason=f"bid/ask={ratio:.2f}x (top{top_levels})",
                meta={"bid_ask_ratio": ratio},
            )
        )
    out.sort(key=lambda s: s.score, reverse=True)
    return out
