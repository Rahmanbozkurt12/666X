#!/usr/bin/env python3
"""
Binance Spot Market Maker — TEK DOSYA · CANLI (testnet YOK)

>>> ŞU AN BANLIYSAN: botu KAPAT, ban bitene kadar BEKLE, sonra YENİ dosyayı çalıştır.

1) API KEY / SECRET yaz
2) python live_mm.py

Bu sürüm:
  • LIMIT_MAKER (spot post-only)
  • Varsayılan 4 pair (az API)
  • Order book = WebSocket (REST yok)
  • Ortak bakiye 45s cache
  • Emir yenileme 45s
  • Ban (-1003/418) → otomatik bekler
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

import ccxt

try:
    import ccxt.pro as ccxtpro
except ImportError:
    ccxtpro = None  # type: ignore

# =============================================================================
BINANCE_API_KEY = "BURAYA_API_KEY"
BINANCE_API_SECRET = "BURAYA_SECRET_KEY"
# =============================================================================

# Az pair = az ban riski (100 USDT için 4 yeterli)
SYMBOLS: List[str] = [
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
]
BALANCE_SLOTS = 4
QUOTE = "USDT"

MAX_INVENTORY_RATIO = 0.90
MAX_DRAWDOWN_RATIO = 0.05
BASE_SPREAD_TICKS = 4.0
VOL_MULT = 4.0
REPLACE_SEC = 45.0
BALANCE_CACHE_SEC = 45.0
FILL_POLL_SEC = 60.0
MIN_QUOTE_FREE = 5.0

MAKER_FEE = 0.001
FEE_SAFETY = 1.5
MIN_EDGE_BPS = 8.0
POST_ONLY = True

# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("live_mm")


def clean(s: str) -> str:
    s = (s or "").strip()
    for q in ('"', "'", "\u201c", "\u201d", "\u2018", "\u2019"):
        if len(s) >= 2 and s[0] == q and s[-1] == q:
            s = s[1:-1].strip()
    for ch in (" ", "\t", "\r", "\n", "\u200b", "\ufeff"):
        s = s.replace(ch, "")
    return s


def resolve_keys() -> Tuple[str, str]:
    k = clean(BINANCE_API_KEY) or clean(os.getenv("BINANCE_API_KEY") or "")
    s = clean(BINANCE_API_SECRET) or clean(os.getenv("BINANCE_API_SECRET") or "")
    bad = {"", "BURAYA_API_KEY", "BURAYA_SECRET_KEY", "YOUR_KEY", "YOUR_SECRET"}
    if k in bad or s in bad:
        raise SystemExit(
            "\nAPI KEY BOŞ!\n"
            '  BINANCE_API_KEY = "gerçek_key"\n'
            '  BINANCE_API_SECRET = "gerçek_secret"\n'
        )
    return k, s


def coid() -> str:
    return ("x-LIVE4MM" + uuid.uuid4().hex)[:32]


def ban_until_ms(err: Exception | str) -> Optional[int]:
    msg = str(err)
    m = re.search(r"banned until (\d+)", msg, re.I)
    if m:
        return int(m.group(1))
    if "418" in msg or "-1003" in msg or "DDoSProtection" in msg or "teapot" in msg.lower():
        return int(time.time() * 1000) + 15 * 60 * 1000  # bilinmiyorsa 15 dk
    return None


async def sleep_ban(err: Exception | str) -> None:
    until = ban_until_ms(err)
    if not until:
        await asyncio.sleep(30)
        return
    now = int(time.time() * 1000)
    wait = max(5.0, (until - now) / 1000.0 + 5.0)
    human = datetime.fromtimestamp(until / 1000, tz=timezone.utc).strftime("%H:%M:%S UTC")
    log.error("IP BAN — %s kadar bekleniyor (≈%.0f sn). Botu kapatma, bekliyor…", human, wait)
    # parçalı sleep (Ctrl+C çalışsın)
    end = time.time() + wait
    while time.time() < end:
        left = end - time.time()
        log.info("ban bekleme… %.0f sn kaldı", left)
        await asyncio.sleep(min(30.0, left))


class Exchange:
    def __init__(self, key: str, secret: str):
        opts = {
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "rateLimit": 200,
            "options": {"defaultType": "spot", "adjustForTimeDifference": True},
        }
        self.rest = ccxt.binance(opts)
        self.rest.set_sandbox_mode(False)
        self.ws = None
        if ccxtpro is not None:
            try:
                self.ws = ccxtpro.binance(opts)
                self.ws.set_sandbox_mode(False)
            except Exception as e:
                log.warning("WS açılamadı: %s", e)

        self._bal: Optional[dict] = None
        self._bal_ts = 0.0
        self._bal_lock = asyncio.Lock()
        self._fee: Dict[str, float] = {}
        self._order_lock = asyncio.Lock()
        self.banned = False

    async def run(self, fn, *a, **kw):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*a, **kw))

    async def init(self):
        while True:
            try:
                await self.run(self.rest.load_markets)
                log.info("CANLI Binance | markets=%d | WS=%s", len(self.rest.markets), bool(self.ws))
                return
            except Exception as e:
                if ban_until_ms(e):
                    await sleep_ban(e)
                    continue
                raise

    async def close(self):
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass

    async def balance(self, force: bool = False) -> Optional[dict]:
        async with self._bal_lock:
            now = time.time()
            if (not force) and self._bal is not None and (now - self._bal_ts) < BALANCE_CACHE_SEC:
                return self._bal
            try:
                self._bal = await self.run(self.rest.fetch_balance)
                self._bal_ts = time.time()
                self.banned = False
                return self._bal
            except Exception as e:
                if ban_until_ms(e):
                    self.banned = True
                    await sleep_ban(e)
                    self.banned = False
                    return self._bal
                log.error("balance: %s", e)
                return self._bal

    async def watch_book(self, symbol: str):
        if not self.ws:
            return None
        try:
            return await self.ws.watch_order_book(symbol, 5)
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            else:
                log.error("ws %s: %s", symbol, e)
                await asyncio.sleep(2)
            return None

    async def book_rest_rare(self, symbol: str):
        """Sadece WS yoksa — nadir."""
        try:
            return await self.run(self.rest.fetch_order_book, symbol, 5)
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return None

    async def open_orders(self, symbol: str):
        try:
            return await self.run(self.rest.fetch_open_orders, symbol) or []
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return []

    async def trades(self, symbol: str, limit: int = 10):
        try:
            return await self.run(self.rest.fetch_my_trades, symbol, None, limit) or []
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return []

    def maker(self, symbol: str) -> float:
        if symbol in self._fee:
            return self._fee[symbol]
        mk = self.rest.markets.get(symbol) or {}
        m = float(mk.get("maker", MAKER_FEE))
        self._fee[symbol] = m
        return m

    def amt(self, symbol: str, x: float) -> float:
        if x <= 0:
            return 0.0
        try:
            return float(self.rest.amount_to_precision(symbol, x))
        except Exception:
            return 0.0

    def px(self, symbol: str, x: float) -> float:
        if x <= 0:
            return 0.0
        try:
            return float(self.rest.price_to_precision(symbol, x))
        except Exception:
            return x

    def limits(self, symbol: str) -> Tuple[float, float]:
        m = self.rest.markets.get(symbol) or {}
        lim = m.get("limits") or {}
        return (
            float((lim.get("amount") or {}).get("min") or 0),
            float((lim.get("cost") or {}).get("min") or 5.0),
        )

    def tick(self, symbol: str) -> float:
        m = self.rest.markets.get(symbol) or {}
        p = (m.get("precision") or {}).get("price")
        if isinstance(p, int):
            return 10 ** (-p)
        if p:
            return float(p)
        return 0.01

    async def place(self, symbol: str, side: str, amount: float, price: float):
        amount = self.amt(symbol, amount)
        price = self.px(symbol, price)
        if amount <= 0 or price <= 0:
            return None
        min_qty, min_cost = self.limits(symbol)
        if amount < min_qty or amount * price < min_cost:
            return None

        async with self._order_lock:
            for attempt in range(2):
                try:
                    otype = "LIMIT_MAKER" if POST_ONLY else "limit"
                    params: Dict[str, Any] = {"newClientOrderId": coid()}
                    if attempt == 1 and POST_ONLY:
                        otype = "limit"
                        params["postOnly"] = True
                    o = await self.run(
                        self.rest.create_order, symbol, otype, side, amount, price, params
                    )
                    log.info("EMİR %s %s %.8f @ %.8f id=%s", side.upper(), symbol, amount, price, o.get("id"))
                    return o
                except Exception as e:
                    msg = str(e)
                    if ban_until_ms(e):
                        await sleep_ban(e)
                        continue
                    if any(x in msg for x in ("Post Only", "-5022", "would immediately", "Order would")):
                        log.warning("post-only reddedildi %s %s", side, symbol)
                        return None
                    if attempt == 0:
                        continue
                    log.error("order %s %s: %s", side, symbol, e)
                    return None
            return None

    async def cancel_all(self, symbol: str):
        async with self._order_lock:
            try:
                if hasattr(self.rest, "cancel_all_orders"):
                    await self.run(self.rest.cancel_all_orders, symbol)
                    return
            except Exception as e:
                if ban_until_ms(e):
                    await sleep_ban(e)
                    return
            for o in await self.open_orders(symbol):
                oid = o.get("id")
                if oid:
                    try:
                        await self.run(self.rest.cancel_order, oid, symbol)
                    except Exception:
                        pass


@dataclass
class Book:
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    imb: float = 0.0
    vol: float = 0.0


class Slot:
    def __init__(self, ex: Exchange, symbol: str, slot_usdt: float):
        self.ex = ex
        self.symbol = symbol
        self.base = symbol.split("/")[0]
        self.quote = symbol.split("/")[1]
        self.slot = float(slot_usdt)
        self.book = Book()
        self.hist: Deque[Tuple[float, float]] = deque(maxlen=300)
        self.base_free = 0.0
        self.quote_free = 0.0
        self.base_total = 0.0
        self.realized = 0.0
        self.last_q = 0.0
        self.last_tid: Optional[str] = None
        self.seen: set = set()
        self.kill = False
        self.running = True
        self._last_fill = 0.0

    @property
    def maker(self) -> float:
        return self.ex.maker(self.symbol)

    def rt_fee(self) -> float:
        return max(self.maker, 0.0) * FEE_SAFETY * 2.0

    def min_spread(self, mid: float) -> float:
        tick = self.ex.tick(self.symbol)
        return max(
            mid * self.rt_fee() + mid * (MIN_EDGE_BPS / 10000.0),
            tick * max(2.0, BASE_SPREAD_TICKS),
        )

    def on_book(self, ob: dict):
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        if not bids or not asks:
            return
        bid, bq = float(bids[0][0]), float(bids[0][1])
        ask, aq = float(asks[0][0]), float(asks[0][1])
        mid = (bid + ask) / 2.0
        tot = bq + aq
        self.book = Book(bid, ask, mid, (bq - aq) / tot if tot else 0.0)
        self.hist.append((time.time(), mid))
        if len(self.hist) >= 5:
            recent = list(self.hist)[-40:]
            rets = [
                math.log(recent[i][1] / recent[i - 1][1])
                for i in range(1, len(recent))
                if recent[i - 1][1] > 0
            ]
            if rets:
                self.book.vol = math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(400)

    async def bal(self):
        b = await self.ex.balance()
        if not b:
            return
        self.base_free = float((b.get("free") or {}).get(self.base, 0) or 0)
        self.quote_free = float((b.get("free") or {}).get(self.quote, 0) or 0)
        self.base_total = float((b.get("total") or {}).get(self.base, 0) or 0)

    async def fills(self):
        now = time.time()
        if now - self._last_fill < FILL_POLL_SEC:
            return
        self._last_fill = now
        for t in await self.ex.trades(self.symbol, 10):
            tid = str(t["id"])
            if tid in self.seen:
                continue
            if self.last_tid and tid.isdigit() and self.last_tid.isdigit():
                if int(tid) <= int(self.last_tid):
                    continue
            self.seen.add(tid)
            self.last_tid = tid
            side, amt, px = t.get("side"), float(t.get("amount") or 0), float(t.get("price") or 0)
            fee = float((t.get("fee") or {}).get("cost") or 0)
            if side == "buy":
                self.realized -= amt * px + fee
            else:
                self.realized += amt * px - fee
            log.info("FILL %s %s %.6f @ %.6f", self.symbol, side, amt, px)
            await self.ex.balance(force=True)

    def risk_ok(self) -> bool:
        mid = self.book.mid
        if mid > 0 and abs(self.base_total) * mid > self.slot * MAX_INVENTORY_RATIO * 1.05:
            return False
        dd = -min(0.0, self.realized)
        if self.slot > 0 and dd > self.slot * MAX_DRAWDOWN_RATIO:
            log.error("%s KILL DD $%.2f", self.symbol, dd)
            self.kill = True
            return False
        return True

    def inv_pos(self) -> float:
        mid = self.book.mid
        if mid <= 0 or self.slot <= 0:
            return 0.0
        max_b = (self.slot * MAX_INVENTORY_RATIO) / mid
        return max(-1.0, min(1.0, self.base_total / max_b)) if max_b else 0.0

    def quotes(self) -> Optional[Tuple[float, float, float, float]]:
        mid = self.book.mid
        if mid <= 0:
            return None
        tick = self.ex.tick(self.symbol)
        fee_half = self.min_spread(mid) / 2.0
        half = max(
            BASE_SPREAD_TICKS * tick / 2.0,
            mid * 0.0003,
            fee_half,
            min(VOL_MULT * max(self.book.vol, 0.0005) * mid * 0.05, mid * 0.008),
        )
        skew = max(-half * 0.25, min(half * 0.25, -self.inv_pos() * 2 * tick))
        bid = mid - half + skew
        ask = mid + half + skew
        min_full = self.min_spread(mid)
        if ask - bid < min_full:
            half = min_full / 2.0
            bid, ask = mid - half, mid + half

        bid = self.ex.px(self.symbol, bid)
        ask = self.ex.px(self.symbol, ask)
        # book içinde kalma (LIMIT_MAKER reject olmasın)
        if self.book.bid > 0 and bid >= self.book.bid:
            bid = self.ex.px(self.symbol, self.book.bid - tick)
        if self.book.ask > 0 and ask <= self.book.ask:
            ask = self.ex.px(self.symbol, self.book.ask + tick)
        if ask <= bid:
            return None
        if ask - bid < min_full * 0.9:
            return None

        min_qty, min_cost = self.ex.limits(self.symbol)
        target = max(min_cost * 1.05, self.slot * 0.90)
        sz = target / mid
        ip = self.inv_pos()
        bid_sz = sz * max(0.5, 1 + ip * 0.15)
        ask_sz = sz * max(0.5, 1 - ip * 0.15)
        max_buy = min(self.quote_free * 0.95, self.slot)
        bid_sz = min(bid_sz, max_buy / mid) if max_buy >= min_cost else 0.0
        max_sell = min(self.base_free * 0.95, self.slot / mid)
        ask_sz = min(ask_sz, max_sell) if max_sell * mid >= min_cost else 0.0
        if abs(self.base_total) * mid >= self.slot * MAX_INVENTORY_RATIO:
            bid_sz = 0.0
        bid_sz = self.ex.amt(self.symbol, bid_sz)
        ask_sz = self.ex.amt(self.symbol, ask_sz)
        if bid_sz > 0 and bid_sz * bid < min_cost:
            bid_sz = 0.0
        if ask_sz > 0 and ask_sz * ask < min_cost:
            ask_sz = 0.0
        return bid, ask, bid_sz, ask_sz

    async def replace(self):
        if time.time() - self.last_q < REPLACE_SEC:
            return
        if self.kill:
            await self.ex.cancel_all(self.symbol)
            return
        q = self.quotes()
        if not q:
            return
        bid, ask, bid_sz, ask_sz = q
        await self.ex.cancel_all(self.symbol)
        await asyncio.sleep(0.4)
        if bid_sz > 0 and self.quote_free >= MIN_QUOTE_FREE:
            await self.ex.place(self.symbol, "buy", bid_sz, bid)
            await asyncio.sleep(0.4)
        if ask_sz > 0 and self.base_free > 0:
            await self.ex.place(self.symbol, "sell", ask_sz, ask)
        self.last_q = time.time()
        log.info(
            "%s quote bid=%.6f×%.6f ask=%.6f×%.6f spr=%.3f%%",
            self.symbol,
            bid,
            bid_sz,
            ask,
            ask_sz,
            (ask - bid) / mid * 100 if (mid := self.book.mid) else 0,
        )

    async def run(self, delay: float = 0.0):
        await asyncio.sleep(delay)
        if self.symbol not in self.ex.rest.markets:
            log.error("market yok: %s", self.symbol)
            return

        log.info(
            "%s start | maker=%.4f%% floor≈%.3f%% slot=$%.2f",
            self.symbol,
            self.maker * 100,
            self.rt_fee() * 100 + MIN_EDGE_BPS / 100,
            self.slot,
        )
        await self.bal()
        tr = await self.ex.trades(self.symbol, 1)
        if tr:
            self.last_tid = str(tr[-1]["id"])

        use_ws = self.ex.ws is not None
        if not use_ws:
            log.warning("%s WS yok — REST book 20s (yavaş)", self.symbol)

        last_rest = 0.0
        last_print = 0.0
        # ilk book
        if use_ws:
            ob = await self.ex.watch_book(self.symbol)
        else:
            ob = await self.ex.book_rest_rare(self.symbol)
        if ob:
            self.on_book(ob)
            await self.replace()

        while self.running:
            try:
                if self.kill:
                    await self.ex.cancel_all(self.symbol)
                    break
                if use_ws:
                    ob = await self.ex.watch_book(self.symbol)
                    if ob:
                        self.on_book(ob)
                else:
                    if time.time() - last_rest >= 20:
                        ob = await self.ex.book_rest_rare(self.symbol)
                        if ob:
                            self.on_book(ob)
                        last_rest = time.time()
                await self.bal()
                await self.fills()
                self.risk_ok()
                await self.replace()
                if time.time() - last_print > 45:
                    print(
                        f"{self.symbol:10} mid={self.book.mid:.6f} "
                        f"base={self.base_total:.6f} rpnl=${self.realized:.2f} "
                        f"{'KILL' if self.kill else 'OK'}"
                    )
                    last_print = time.time()
                await asyncio.sleep(0.3 if use_ws else 2.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("%s loop: %s", self.symbol, e)
                if ban_until_ms(e):
                    await sleep_ban(e)
                else:
                    await asyncio.sleep(5)
        await self.ex.cancel_all(self.symbol)
        self.running = False


async def main_async():
    print("=" * 60)
    print("Binance MM — CANLI | LIMIT_MAKER | 4 pair | ban-safe")
    print(f"CCXT {ccxt.__version__} | Pro={'var' if ccxtpro else 'YOK — pip install ccxt[pro]'}")
    print("=" * 60)
    print("UYARI: IP banlıysan bot BAN BİTENE KADAR bekler.")
    print("Ban bitmeden tekrar tekrar başlatma (daha kötü olur).")
    print("=" * 60)

    key, secret = resolve_keys()
    ex = Exchange(key, secret)
    await ex.init()

    bal = await ex.balance(force=True)
    if not bal:
        await ex.close()
        raise SystemExit("Bakiye yok / ban — sonra tekrar dene")

    free = float((bal.get("free") or {}).get(QUOTE, 0) or 0)
    symbols = [s for s in SYMBOLS[:BALANCE_SLOTS] if s in ex.rest.markets]
    n = len(symbols)
    slot = free / n if n else 0
    print(f"USDT: ${free:,.2f} | {n}×${slot:,.2f} | {', '.join(symbols)}")
    print(f"replace={REPLACE_SEC}s balance_cache={BALANCE_CACHE_SEC}s")
    print("Ctrl+C ile dur")
    print("=" * 60)

    if slot < 6:
        await ex.close()
        raise SystemExit(f"Slot ${slot:.2f} küçük — USDT ekle")

    if not ex.ws:
        log.warning("ccxt.pro yok → REST kullanır (ban riski). pip install 'ccxt[pro]'")

    slots = [Slot(ex, s, slot) for s in symbols]
    # 5 sn arayla başlat
    tasks = [asyncio.create_task(s.run(i * 5.0)) for i, s in enumerate(slots)]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for s in slots:
            s.running = False
            try:
                await ex.cancel_all(s.symbol)
            except Exception:
                pass
        await ex.close()
        print("Kapandı")


def main():
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nDurdu")


if __name__ == "__main__":
    main()
