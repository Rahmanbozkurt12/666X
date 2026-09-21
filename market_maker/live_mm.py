#!/usr/bin/env python3
"""
Binance Spot Market Maker — TEK DOSYA · CANLI AL/SAT (testnet YOK)

1) Aşağıya API KEY / SECRET yaz
2) Kaydet:  live_mm.py
3) Çalıştır:  python live_mm.py

Düzeltmeler:
  • Spot Post-Only = LIMIT_MAKER (GTX değil — -1115 hatası giderildi)
  • Ortak bakiye cache + yavaş poll (429 rate-limit giderildi)
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

import ccxt

try:
    import ccxt.pro as ccxtpro
except ImportError:
    ccxtpro = None  # type: ignore

# =============================================================================
#  >>> API KEY BURAYA (tırnak içinde, boşluksuz) <<<
# =============================================================================
BINANCE_API_KEY = "BURAYA_API_KEY"
BINANCE_API_SECRET = "BURAYA_SECRET_KEY"
# =============================================================================

SYMBOLS: List[str] = [
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "DOGE/USDT",
    "ADA/USDT",
    "AVAX/USDT",
]
BALANCE_SLOTS = 8
QUOTE = "USDT"

MAX_INVENTORY_RATIO = 0.90
MAX_DRAWDOWN_RATIO = 0.05
BASE_SPREAD_TICKS = 4.0
VOL_MULT = 4.0
REPLACE_SEC = 25.0          # emir yenileme (yavaş = az API)
BOOK_REST_SEC = 3.0         # REST book aralığı (WS yoksa)
BALANCE_CACHE_SEC = 20.0    # ortak bakiye cache
MIN_QUOTE_FREE = 5.0

MAKER_FEE = 0.001
TAKER_FEE = 0.001
FEE_SAFETY = 1.5
MIN_EDGE_BPS = 8.0
POST_ONLY = True            # LIMIT_MAKER (spot)

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
    return ("x-LIVE8MM" + uuid.uuid4().hex)[:32]


class Exchange:
    def __init__(self, key: str, secret: str):
        opts = {
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "rateLimit": 120,
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
                log.warning("WS yok, REST: %s", e)
                self.ws = None

        self._bal: Optional[dict] = None
        self._bal_ts = 0.0
        self._bal_lock = asyncio.Lock()
        self._fee_cache: Dict[str, Tuple[float, float]] = {}
        self._order_lock = asyncio.Lock()

    async def run(self, fn, *a, **kw):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*a, **kw))

    async def init(self):
        await self.run(self.rest.load_markets)
        log.info("CANLI Binance spot | markets=%d", len(self.rest.markets))

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
                return self._bal
            except Exception as e:
                msg = str(e)
                if "429" in msg or "-1003" in msg:
                    log.warning("rate-limit balance — cache kullan / 30s bekle")
                    await asyncio.sleep(5)
                    return self._bal
                log.error("balance: %s", e)
                return self._bal

    async def book(self, symbol: str):
        try:
            return await self.run(self.rest.fetch_order_book, symbol, 10)
        except Exception as e:
            if "429" in str(e) or "-1003" in str(e):
                log.warning("rate-limit book %s", symbol)
                await asyncio.sleep(3)
                return None
            log.error("book %s: %s", symbol, e)
            return None

    async def watch_book(self, symbol: str):
        if not self.ws:
            return None
        try:
            return await self.ws.watch_order_book(symbol, 10)
        except Exception as e:
            log.error("ws %s: %s", symbol, e)
            return None

    async def open_orders(self, symbol: str):
        try:
            return await self.run(self.rest.fetch_open_orders, symbol) or []
        except Exception as e:
            if "429" in str(e):
                await asyncio.sleep(3)
            return []

    async def trades(self, symbol: str, limit: int = 20):
        try:
            return await self.run(self.rest.fetch_my_trades, symbol, None, limit) or []
        except Exception:
            return []

    async def fees(self, symbol: str) -> Tuple[float, float]:
        if symbol in self._fee_cache:
            return self._fee_cache[symbol]
        m, t = MAKER_FEE, TAKER_FEE
        mk = self.rest.markets.get(symbol) or {}
        m = float(mk.get("maker", m))
        t = float(mk.get("taker", t))
        # fetch_trading_fees her sembolde çağırma — market default yeterli
        self._fee_cache[symbol] = (m, t)
        return m, t

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
        min_qty = float((lim.get("amount") or {}).get("min") or 0)
        min_cost = float((lim.get("cost") or {}).get("min") or 5.0)
        return min_qty, min_cost

    def tick(self, symbol: str) -> float:
        m = self.rest.markets.get(symbol) or {}
        p = (m.get("precision") or {}).get("price")
        if isinstance(p, int):
            return 10 ** (-p)
        if p:
            return float(p)
        return 0.01

    async def place(self, symbol: str, side: str, amount: float, price: float):
        """Spot Post-Only: LIMIT_MAKER (GTX futures-only → -1115)."""
        amount = self.amt(symbol, amount)
        price = self.px(symbol, price)
        if amount <= 0 or price <= 0:
            return None
        min_qty, min_cost = self.limits(symbol)
        if amount < min_qty or amount * price < min_cost:
            return None

        params: Dict[str, Any] = {"newClientOrderId": coid()}
        # Binance SPOT post-only = LIMIT_MAKER (GTX sadece futures → -1115)
        order_type = "LIMIT_MAKER" if POST_ONLY else "limit"

        async with self._order_lock:
            try:
                o = await self.run(
                    self.rest.create_order,
                    symbol,
                    order_type,
                    side,
                    amount,
                    price,
                    params,
                )
                log.info(
                    "EMİR %s %s %.8f @ %.8f id=%s",
                    side.upper(),
                    symbol,
                    amount,
                    price,
                    o.get("id"),
                )
                return o
            except Exception as e:
                msg = str(e)
                if any(
                    x in msg
                    for x in ("Post Only", "-5022", "would immediately", "Order would")
                ):
                    log.warning("post-only reddedildi %s %s", side, symbol)
                    return None
                # ccxt sürüm farkı: limit + postOnly
                if POST_ONLY and ("Invalid" in msg or "-1115" in msg or "type" in msg.lower()):
                    try:
                        o = await self.run(
                            self.rest.create_order,
                            symbol,
                            "limit",
                            side,
                            amount,
                            price,
                            {"newClientOrderId": coid(), "postOnly": True},
                        )
                        log.info(
                            "EMİR(PO) %s %s %.8f @ %.8f id=%s",
                            side.upper(),
                            symbol,
                            amount,
                            price,
                            o.get("id"),
                        )
                        return o
                    except Exception as e2:
                        log.error("order %s %s: %s", side, symbol, e2)
                        return None
                if "429" in msg or "-1003" in msg:
                    log.warning("rate-limit order — 10s bekle")
                    await asyncio.sleep(10)
                    return None
                log.error("order %s %s: %s", side, symbol, e)
                return None

    async def cancel_all(self, symbol: str):
        async with self._order_lock:
            # tek çağrı tercih
            try:
                if hasattr(self.rest, "cancel_all_orders"):
                    await self.run(self.rest.cancel_all_orders, symbol)
                    return
            except Exception:
                pass
            for o in await self.open_orders(symbol):
                oid = o.get("id")
                if not oid:
                    continue
                try:
                    await self.run(self.rest.cancel_order, oid, symbol)
                except Exception as e:
                    if "429" not in str(e):
                        log.error("cancel %s: %s", oid, e)


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
        self.hist: Deque[Tuple[float, float]] = deque(maxlen=400)
        self.base_free = 0.0
        self.quote_free = 0.0
        self.base_total = 0.0
        self.realized = 0.0
        self.maker = MAKER_FEE
        self.taker = TAKER_FEE
        self.last_q = 0.0
        self.last_tid: Optional[str] = None
        self.seen: set = set()
        self.kill = False
        self.running = True
        self._last_fill_poll = 0.0

    def rt_fee(self) -> float:
        return max(self.maker, 0.0) * FEE_SAFETY * 2.0

    def min_spread(self, mid: float) -> float:
        tick = self.ex.tick(self.symbol)
        edge = mid * (MIN_EDGE_BPS / 10000.0)
        fee = mid * self.rt_fee()
        return max(fee + edge, tick * max(2.0, BASE_SPREAD_TICKS))

    def on_book(self, ob: dict):
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        if not bids or not asks:
            return
        bid, bq = float(bids[0][0]), float(bids[0][1])
        ask, aq = float(asks[0][0]), float(asks[0][1])
        mid = (bid + ask) / 2.0
        tot = bq + aq
        imb = (bq - aq) / tot if tot else 0.0
        self.book = Book(bid, ask, mid, imb)
        self.hist.append((time.time(), mid))
        if len(self.hist) >= 5:
            recent = list(self.hist)[-60:]
            rets = []
            for i in range(1, len(recent)):
                a, b = recent[i - 1][1], recent[i][1]
                if a > 0:
                    rets.append(math.log(b / a))
            if rets:
                self.book.vol = math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(600)

    async def bal(self):
        b = await self.ex.balance()
        if not b:
            return
        self.base_free = float((b.get("free") or {}).get(self.base, 0) or 0)
        self.quote_free = float((b.get("free") or {}).get(self.quote, 0) or 0)
        self.base_total = float((b.get("total") or {}).get(self.base, 0) or 0)

    async def sync_fees(self):
        self.maker, self.taker = await self.ex.fees(self.symbol)
        log.info(
            "%s fees maker=%.4f%% floor≈%.3f%% slot=$%.2f",
            self.symbol,
            self.maker * 100,
            self.rt_fee() * 100 + MIN_EDGE_BPS / 100,
            self.slot,
        )

    async def fills(self):
        now = time.time()
        if now - self._last_fill_poll < 30:
            return
        self._last_fill_poll = now
        rows = await self.ex.trades(self.symbol, 20)
        for t in rows:
            tid = str(t["id"])
            if tid in self.seen:
                continue
            if self.last_tid and tid.isdigit() and self.last_tid.isdigit():
                if int(tid) <= int(self.last_tid):
                    continue
            self.seen.add(tid)
            self.last_tid = tid
            side = t.get("side")
            amt = float(t.get("amount") or 0)
            px = float(t.get("price") or 0)
            fee = float((t.get("fee") or {}).get("cost") or 0)
            if side == "buy":
                self.realized -= amt * px + fee
            else:
                self.realized += amt * px - fee
            log.info("FILL %s %s %.6f @ %.6f", self.symbol, side, amt, px)
            await self.ex.balance(force=True)

    def risk_ok(self) -> bool:
        mid = self.book.mid
        inv = abs(self.base_total) * mid if mid > 0 else 0
        if mid > 0 and inv > self.slot * MAX_INVENTORY_RATIO * 1.05:
            return False
        dd = -min(0.0, self.realized)
        if self.slot > 0 and dd > self.slot * MAX_DRAWDOWN_RATIO:
            log.error("%s KILL drawdown $%.2f", self.symbol, dd)
            self.kill = True
            return False
        return True

    def inv_pos(self) -> float:
        mid = self.book.mid
        if mid <= 0 or self.slot <= 0:
            return 0.0
        max_b = (self.slot * MAX_INVENTORY_RATIO) / mid
        if max_b <= 0:
            return 0.0
        return max(-1.0, min(1.0, self.base_total / max_b))

    def quotes(self) -> Optional[Tuple[float, float, float, float]]:
        mid = self.book.mid
        if mid <= 0:
            return None
        tick = self.ex.tick(self.symbol)
        fee_half = self.min_spread(mid) / 2.0
        vol = max(self.book.vol, 0.0005)
        half = max(
            BASE_SPREAD_TICKS * tick / 2.0,
            mid * 0.0003,
            fee_half,
            min(VOL_MULT * vol * mid * 0.05, mid * 0.008),
        )
        skew = -self.inv_pos() * 2.0 * tick
        skew = max(-half * 0.25, min(half * 0.25, skew))
        imb = max(-half * 0.2, min(half * 0.2, self.book.imb * tick))

        bid = mid - half + skew - imb
        ask = mid + half + skew + imb
        min_full = self.min_spread(mid)
        if ask - bid < min_full:
            half = min_full / 2.0
            bid, ask = mid - half, mid + half

        bid = self.ex.px(self.symbol, bid)
        ask = self.ex.px(self.symbol, ask)
        if ask <= bid:
            ask = self.ex.px(self.symbol, bid + max(tick, min_full))
        if ask - bid < min_full * 0.98:
            return None

        # Post-only için mid'den en az 1 tick uzak kal (hemen match olmasın)
        if bid >= self.book.bid:
            bid = self.ex.px(self.symbol, min(bid, self.book.bid - tick))
        if ask <= self.book.ask:
            ask = self.ex.px(self.symbol, max(ask, self.book.ask + tick))
        if ask <= bid or ask - bid < min_full * 0.95:
            half = min_full / 2.0
            bid = self.ex.px(self.symbol, mid - half)
            ask = self.ex.px(self.symbol, mid + half)

        min_qty, min_cost = self.ex.limits(self.symbol)
        target = max(min_cost * 1.05, self.slot * 0.90)
        sz = target / mid
        ip = self.inv_pos()
        bid_sz = sz * max(0.5, 1.0 + ip * 0.15)
        ask_sz = sz * max(0.5, 1.0 - ip * 0.15)

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
        now = time.time()
        if now - self.last_q < REPLACE_SEC:
            return
        if self.kill:
            await self.ex.cancel_all(self.symbol)
            return
        q = self.quotes()
        if not q:
            return
        bid, ask, bid_sz, ask_sz = q
        await self.ex.cancel_all(self.symbol)
        await asyncio.sleep(0.25)
        if bid_sz > 0 and self.quote_free >= MIN_QUOTE_FREE:
            await self.ex.place(self.symbol, "buy", bid_sz, bid)
            await asyncio.sleep(0.2)
        if ask_sz > 0 and self.base_free > 0:
            await self.ex.place(self.symbol, "sell", ask_sz, ask)
        self.last_q = time.time()
        log.info(
            "%s quote bid=%.6f×%.6f ask=%.6f×%.6f spread=%.3f%%",
            self.symbol,
            bid,
            bid_sz,
            ask,
            ask_sz,
            (ask - bid) / self.book.mid * 100 if self.book.mid else 0,
        )

    async def run(self, delay: float = 0.0):
        await asyncio.sleep(delay)
        if self.symbol not in self.ex.rest.markets:
            log.error("market yok: %s", self.symbol)
            return
        await self.sync_fees()
        await self.bal()
        tr = await self.ex.trades(self.symbol, 1)
        if tr:
            self.last_tid = str(tr[-1]["id"])
        ob = await self.ex.book(self.symbol)
        if ob:
            self.on_book(ob)
            await self.replace()

        use_ws = self.ex.ws is not None
        last_rest = 0.0
        last_print = 0.0
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
                    if time.time() - last_rest >= BOOK_REST_SEC:
                        ob = await self.ex.book(self.symbol)
                        if ob:
                            self.on_book(ob)
                        last_rest = time.time()
                await self.bal()
                await self.fills()
                self.risk_ok()
                await self.replace()
                if time.time() - last_print > 30:
                    print(
                        f"{self.symbol:10} mid={self.book.mid:.6f} "
                        f"base={self.base_total:.6f} rpnl=${self.realized:.2f} "
                        f"{'KILL' if self.kill else 'OK'}"
                    )
                    last_print = time.time()
                await asyncio.sleep(0.2 if use_ws else 1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("%s loop: %s", self.symbol, e)
                await asyncio.sleep(5)
        await self.ex.cancel_all(self.symbol)
        self.running = False


async def main_async():
    print("=" * 60)
    print("Binance Market Maker — CANLI AL/SAT (testnet YOK)")
    print(f"CCXT {ccxt.__version__} | Pro={'var' if ccxtpro else 'yok'}")
    print("Post-Only: LIMIT_MAKER | bakiye cache | yavaş poll")
    print("=" * 60)

    key, secret = resolve_keys()
    ex = Exchange(key, secret)
    await ex.init()

    bal = await ex.balance(force=True)
    if not bal:
        await ex.close()
        raise SystemExit("Bakiye alınamadı — key/izin kontrol et (Spot Trade)")

    free = float((bal.get("free") or {}).get(QUOTE, 0) or 0)
    symbols = [s for s in SYMBOLS[:BALANCE_SLOTS] if s in ex.rest.markets]
    if not symbols:
        await ex.close()
        raise SystemExit("Hiçbir pair market'te yok")
    n = len(symbols)
    slot = free / n
    print(f"USDT serbest : ${free:,.2f}")
    print(f"Slotlar      : {n} × ${slot:,.2f}")
    print(f"Pairler      : {', '.join(symbols)}")
    print(f"Post-only    : LIMIT_MAKER | kill DD%{MAX_DRAWDOWN_RATIO*100:.0f}")
    print("Durdur       : Ctrl+C")
    print("=" * 60)

    if slot < 6:
        await ex.close()
        raise SystemExit(f"Slot ${slot:.2f} çok küçük — USDT ekle veya SYMBOLS azalt")

    # 429 önlemi: slotları kademeli başlat
    slots = [Slot(ex, s, slot) for s in symbols]
    tasks = [asyncio.create_task(s.run(i * 2.0)) for i, s in enumerate(slots)]
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
        print("Kapandı — emirler iptal")


def main():
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nCtrl+C — durdu")


if __name__ == "__main__":
    main()
