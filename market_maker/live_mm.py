#!/usr/bin/env python3
"""
Binance Spot — TOP 20 hareketli coin · dipte AL / yükselişte SAT · CANLI

1) API KEY / SECRET yaz
2) pip install "ccxt[pro]"
3) python live_mm.py

Ne yapar:
  • Binance 24h hacmine göre en hareketli 20 USDT pair seçer (periyodik yeniler)
  • Serbest USDT'yi 20 eşit slota böler
  • Fiyat DÜŞÜNCE slot ile ALır
  • Aldığı coin YÜKSELINCE SATar
  • Satıştan sonra aynı coine 1 dk ara verir, sonra tekrar devam
  • LIMIT_MAKER + ban koruması (418/-1003 → bekler)
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import ccxt

try:
    import ccxt.pro as ccxtpro
except ImportError:
    ccxtpro = None  # type: ignore

# =============================================================================
BINANCE_API_KEY = "BURAYA_API_KEY"
BINANCE_API_SECRET = "BURAYA_SECRET_KEY"
# =============================================================================

TOP_N = 20                      # en hareketli N coin
BALANCE_SLOTS = 20              # USDT / 20
QUOTE = "USDT"

# Strateji
DIP_BPS = 25.0                  # son mid'e göre bu kadar düştüyse AL sinyali
RISE_BPS = 35.0                 # girişe göre bu kadar yükseldiyse SAT
COOLDOWN_SEC = 60.0             # satınca aynı coine 1 dk ara
TOP_REFRESH_SEC = 300.0         # top-20 listesini 5 dk'da bir yenile
MIN_QUOTE_VOL_USDT = 5_000_000  # çok ölü pair alma
MIN_PRICE = 0.00001

# Emir / ban koruma
REPLACE_SEC = 8.0               # sinyal kontrol aralığı (pozisyon varken daha sık)
BALANCE_CACHE_SEC = 45.0
FILL_POLL_SEC = 45.0
MIN_QUOTE_FREE = 5.0
POST_ONLY = True
MAKER_FEE = 0.001
FEE_SAFETY = 1.5
MIN_EDGE_BPS = 6.0
MAX_DRAWDOWN_RATIO = 0.08       # slot başına DD kill

SKIP_BASES = {
    "USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDE", "USD1",
    "EUR", "TRY", "BRL", "AEUR",
}

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
    return ("x-T20DIP" + uuid.uuid4().hex)[:32]


def ban_until_ms(err: Exception | str) -> Optional[int]:
    msg = str(err)
    m = re.search(r"banned until (\d+)", msg, re.I)
    if m:
        return int(m.group(1))
    if "418" in msg or "-1003" in msg or "DDoSProtection" in msg or "teapot" in msg.lower():
        return int(time.time() * 1000) + 15 * 60 * 1000
    return None


async def sleep_ban(err: Exception | str) -> None:
    until = ban_until_ms(err)
    if not until:
        await asyncio.sleep(30)
        return
    now = int(time.time() * 1000)
    wait = max(5.0, (until - now) / 1000.0 + 5.0)
    human = datetime.fromtimestamp(until / 1000, tz=timezone.utc).strftime("%H:%M:%S UTC")
    log.error("IP BAN — %s kadar bekleniyor (≈%.0f sn)", human, wait)
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
                return self._bal
            except Exception as e:
                if ban_until_ms(e):
                    await sleep_ban(e)
                    return self._bal
                log.error("balance: %s", e)
                return self._bal

    async def tickers_24h(self) -> List[dict]:
        try:
            data = await self.run(self.rest.fetch_tickers)
            return list(data.values()) if isinstance(data, dict) else []
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            log.error("tickers: %s", e)
            return []

    async def watch_book(self, symbol: str):
        if not self.ws:
            return None
        try:
            return await self.ws.watch_order_book(symbol, 5)
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            else:
                await asyncio.sleep(1.5)
            return None

    async def book_rest(self, symbol: str):
        try:
            return await self.run(self.rest.fetch_order_book, symbol, 5)
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return None

    async def trades(self, symbol: str, limit: int = 8):
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
            try:
                opens = await self.run(self.rest.fetch_open_orders, symbol) or []
            except Exception:
                opens = []
            for o in opens:
                oid = o.get("id")
                if oid:
                    try:
                        await self.run(self.rest.cancel_order, oid, symbol)
                    except Exception:
                        pass


def pick_top_symbols(ex: Exchange, tickers: List[dict], n: int = TOP_N) -> List[str]:
    rows: List[Tuple[float, str]] = []
    for t in tickers:
        sym = str(t.get("symbol") or "")
        if not sym.endswith("/USDT"):
            continue
        if sym not in ex.rest.markets:
            continue
        m = ex.rest.markets[sym]
        # spot only (futures ayrı sembol /USDT:USDT olur)
        if ":USDT" in sym or m.get("contract"):
            continue
        if m.get("spot") is False:
            continue
        base = sym.split("/")[0].upper()
        if base in SKIP_BASES:
            continue
        if m.get("active") is False:
            continue
        qv = t.get("quoteVolume")
        if qv is None:
            info = t.get("info") or {}
            qv = info.get("quoteVolume")
        try:
            qv_f = float(qv or 0)
        except Exception:
            qv_f = 0.0
        if qv_f < MIN_QUOTE_VOL_USDT:
            continue
        last = float(t.get("last") or t.get("close") or 0)
        if last < MIN_PRICE:
            continue
        rows.append((qv_f, sym))
    rows.sort(key=lambda x: -x[0])
    out: List[str] = []
    for _, sym in rows:
        if sym not in out:
            out.append(sym)
        if len(out) >= n:
            break
    return out


@dataclass
class Book:
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0


@dataclass
class Worker:
    ex: Exchange
    symbol: str
    slot: float
    book: Book = field(default_factory=Book)
    hist: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=120))
    base_free: float = 0.0
    quote_free: float = 0.0
    base_total: float = 0.0
    entry_px: float = 0.0
    realized: float = 0.0
    cooldown_until: float = 0.0
    last_action: float = 0.0
    last_fill_poll: float = 0.0
    seen: Set[str] = field(default_factory=set)
    last_tid: Optional[str] = None
    kill: bool = False
    running: bool = True
    has_pos: bool = False

    @property
    def base(self) -> str:
        return self.symbol.split("/")[0]

    def on_book(self, ob: dict) -> None:
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        if not bids or not asks:
            return
        bid = float(bids[0][0])
        ask = float(asks[0][0])
        mid = (bid + ask) / 2.0
        self.book = Book(bid, ask, mid)
        self.hist.append((time.time(), mid))

    def recent_high(self, window_sec: float = 90.0) -> float:
        now = time.time()
        vals = [p for t, p in self.hist if now - t <= window_sec]
        return max(vals) if vals else self.book.mid

    def dip_bps(self) -> float:
        hi = self.recent_high(90.0)
        mid = self.book.mid
        if hi <= 0 or mid <= 0:
            return 0.0
        return (hi - mid) / hi * 10000.0

    def rise_bps(self) -> float:
        if self.entry_px <= 0 or self.book.mid <= 0:
            return 0.0
        return (self.book.mid - self.entry_px) / self.entry_px * 10000.0

    def min_spread(self) -> float:
        mid = self.book.mid
        tick = self.ex.tick(self.symbol)
        fee = max(self.ex.maker(self.symbol), 0.0) * FEE_SAFETY * 2.0
        return max(mid * fee + mid * (MIN_EDGE_BPS / 10000.0), tick * 2)

    async def bal(self) -> None:
        b = await self.ex.balance()
        if not b:
            return
        self.base_free = float((b.get("free") or {}).get(self.base, 0) or 0)
        self.quote_free = float((b.get("free") or {}).get(QUOTE, 0) or 0)
        self.base_total = float((b.get("total") or {}).get(self.base, 0) or 0)
        min_cost = self.ex.limits(self.symbol)[1]
        mid = self.book.mid or 0
        self.has_pos = mid > 0 and self.base_total * mid >= min_cost * 0.8
        if self.has_pos and self.entry_px <= 0:
            self.entry_px = mid

    async def fills(self) -> None:
        now = time.time()
        if now - self.last_fill_poll < FILL_POLL_SEC:
            return
        self.last_fill_poll = now
        for t in await self.ex.trades(self.symbol, 8):
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
                self.entry_px = px
                self.has_pos = True
            else:
                self.realized += amt * px - fee
                self.entry_px = 0.0
                self.has_pos = False
                self.cooldown_until = time.time() + COOLDOWN_SEC
                log.info(
                    "%s SAT fill → %.0fs cooldown (rise/take)",
                    self.symbol,
                    COOLDOWN_SEC,
                )
            log.info("FILL %s %s %.6f @ %.6f", self.symbol, side, amt, px)
            await self.ex.balance(force=True)

    def risk_ok(self) -> bool:
        dd = -min(0.0, self.realized)
        if self.slot > 0 and dd > self.slot * MAX_DRAWDOWN_RATIO:
            log.error("%s KILL DD $%.2f", self.symbol, dd)
            self.kill = True
            return False
        return True

    def buy_quote_px(self) -> Optional[Tuple[float, float]]:
        """Dipte LIMIT_MAKER alış fiyatı + miktar."""
        mid = self.book.mid
        bid = self.book.bid
        if mid <= 0 or bid <= 0:
            return None
        tick = self.ex.tick(self.symbol)
        price = self.ex.px(self.symbol, min(bid, mid - self.min_spread() / 2))
        if price >= bid:
            price = self.ex.px(self.symbol, bid - tick)
        if price <= 0:
            return None
        min_qty, min_cost = self.ex.limits(self.symbol)
        usdt = min(self.slot * 0.95, self.quote_free * 0.90)
        if usdt < max(MIN_QUOTE_FREE, min_cost):
            return None
        qty = self.ex.amt(self.symbol, usdt / price)
        if qty < min_qty or qty * price < min_cost:
            return None
        return price, qty

    def sell_quote_px(self) -> Optional[Tuple[float, float]]:
        mid = self.book.mid
        ask = self.book.ask
        if mid <= 0 or ask <= 0 or self.base_free <= 0:
            return None
        tick = self.ex.tick(self.symbol)
        price = self.ex.px(self.symbol, max(ask, mid + self.min_spread() / 2))
        if price <= ask:
            price = self.ex.px(self.symbol, ask + tick)
        min_qty, min_cost = self.ex.limits(self.symbol)
        qty = self.ex.amt(self.symbol, self.base_free * 0.98)
        if qty < min_qty or qty * price < min_cost:
            return None
        return price, qty

    async def act(self) -> None:
        now = time.time()
        if now - self.last_action < REPLACE_SEC:
            return
        if self.kill:
            await self.ex.cancel_all(self.symbol)
            return

        # Pozisyon var → yükseldiyse SAT
        if self.has_pos and self.base_free > 0:
            rise = self.rise_bps()
            if rise >= RISE_BPS:
                await self.ex.cancel_all(self.symbol)
                await asyncio.sleep(0.2)
                q = self.sell_quote_px()
                if q:
                    px, qty = q
                    log.info("%s YÜKSELİŞ +%.1f bps → SAT @ %.8f", self.symbol, rise, px)
                    await self.ex.place(self.symbol, "sell", qty, px)
                    self.last_action = time.time()
            return

        # Pozisyon yok → cooldown veya dipte AL
        if now < self.cooldown_until:
            return

        dip = self.dip_bps()
        if dip >= DIP_BPS and self.quote_free >= MIN_QUOTE_FREE:
            await self.ex.cancel_all(self.symbol)
            await asyncio.sleep(0.2)
            q = self.buy_quote_px()
            if q:
                px, qty = q
                log.info("%s DÜŞÜŞ −%.1f bps → AL @ %.8f ($%.2f slot)", self.symbol, dip, px, self.slot)
                await self.ex.place(self.symbol, "buy", qty, px)
                self.last_action = time.time()

    async def run(self, delay: float = 0.0) -> None:
        await asyncio.sleep(delay)
        if self.symbol not in self.ex.rest.markets:
            log.error("market yok: %s", self.symbol)
            return

        log.info("%s START | slot=$%.2f | dip≥%.0fbps rise≥%.0fbps cool=%.0fs",
                 self.symbol, self.slot, DIP_BPS, RISE_BPS, COOLDOWN_SEC)

        await self.bal()
        tr = await self.ex.trades(self.symbol, 1)
        if tr:
            self.last_tid = str(tr[-1]["id"])

        use_ws = self.ex.ws is not None
        last_rest = 0.0
        last_print = 0.0

        if use_ws:
            ob = await self.ex.watch_book(self.symbol)
        else:
            ob = await self.ex.book_rest(self.symbol)
        if ob:
            self.on_book(ob)

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
                    if time.time() - last_rest >= 12:
                        ob = await self.ex.book_rest(self.symbol)
                        if ob:
                            self.on_book(ob)
                        last_rest = time.time()

                await self.bal()
                await self.fills()
                self.risk_ok()
                await self.act()

                if time.time() - last_print > 40:
                    cd = max(0.0, self.cooldown_until - time.time())
                    print(
                        f"{self.symbol:12} mid={self.book.mid:.6g} "
                        f"dip={self.dip_bps():.0f}bps rise={self.rise_bps():.0f}bps "
                        f"pos={'Y' if self.has_pos else 'N'} "
                        f"cd={cd:.0f}s rpnl=${self.realized:.2f}"
                    )
                    last_print = time.time()
                await asyncio.sleep(0.25 if use_ws else 2.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("%s loop: %s", self.symbol, e)
                if ban_until_ms(e):
                    await sleep_ban(e)
                else:
                    await asyncio.sleep(4)

        await self.ex.cancel_all(self.symbol)
        self.running = False


async def main_async() -> None:
    print("=" * 64)
    print("Binance TOP-20 hareketli · dip AL / yükseliş SAT · 1dk cooldown")
    print(f"CCXT {ccxt.__version__} | Pro={'var' if ccxtpro else 'YOK — pip install ccxt[pro]'}")
    print("=" * 64)

    key, secret = resolve_keys()
    ex = Exchange(key, secret)
    await ex.init()

    bal = await ex.balance(force=True)
    if not bal:
        await ex.close()
        raise SystemExit("Bakiye yok / ban — sonra tekrar dene")

    free = float((bal.get("free") or {}).get(QUOTE, 0) or 0)
    tickers = await ex.tickers_24h()
    symbols = pick_top_symbols(ex, tickers, TOP_N)
    if len(symbols) < 5:
        await ex.close()
        raise SystemExit(f"Yeterli hareketli pair yok ({len(symbols)})")

    n = min(BALANCE_SLOTS, len(symbols))
    symbols = symbols[:n]
    slot = free / n if n else 0.0
    print(f"USDT: ${free:,.2f} | {n}×${slot:,.2f}")
    print("TOP:", ", ".join(s.replace("/USDT", "") for s in symbols))
    print(f"dip≥{DIP_BPS:.0f}bps | rise≥{RISE_BPS:.0f}bps | cooldown={COOLDOWN_SEC:.0f}s")
    print("Ctrl+C ile dur")
    print("=" * 64)

    if slot < 6:
        await ex.close()
        raise SystemExit(f"Slot ${slot:.2f} küçük — USDT ekle veya TOP_N düşür")

    if not ex.ws:
        log.warning("ccxt.pro yok → REST (ban riski). pip install 'ccxt[pro]'")

    workers = [Worker(ex, s, slot) for s in symbols]
    tasks = [asyncio.create_task(w.run(i * 2.0)) for i, w in enumerate(workers)]
    refresh_task = asyncio.create_task(refresh_loop(ex, workers, tasks))

    try:
        await asyncio.gather(*tasks, refresh_task)
    except asyncio.CancelledError:
        pass
    finally:
        refresh_task.cancel()
        for w in workers:
            w.running = False
            try:
                await ex.cancel_all(w.symbol)
            except Exception:
                pass
        await ex.close()
        print("Kapandı")


async def refresh_loop(ex: Exchange, workers: List[Worker], tasks: List[asyncio.Task]) -> None:
    """Her TOP_REFRESH_SEC yeni top-20; düşenleri kapat, yenileri ekle (pozisyonsuz)."""
    while True:
        try:
            await asyncio.sleep(TOP_REFRESH_SEC)
            tickers = await ex.tickers_24h()
            new_syms = pick_top_symbols(ex, tickers, TOP_N)
            if not new_syms:
                continue
            current = {w.symbol: w for w in workers if w.running}
            # yeni ekle
            free_bal = await ex.balance()
            free = float(((free_bal or {}).get("free") or {}).get(QUOTE, 0) or 0)
            active_n = max(1, len([w for w in workers if w.running]))
            slot = free / max(TOP_N, active_n)

            for sym in new_syms:
                if sym in current:
                    continue
                # sadece pozisyonsuz yer varsa ve TOP_N altındaysak
                if sum(1 for w in workers if w.running) >= TOP_N:
                    break
                w = Worker(ex, sym, slot)
                workers.append(w)
                tasks.append(asyncio.create_task(w.run(1.0)))
                log.info("YENİ pair eklendi: %s", sym)

            # listeden düşen + pozisyonsuz → kapat
            keep = set(new_syms)
            for w in list(workers):
                if not w.running:
                    continue
                if w.symbol in keep:
                    continue
                if w.has_pos:
                    continue  # pozisyon bitene kadar tut
                w.running = False
                await ex.cancel_all(w.symbol)
                log.info("listeden çıktı (kapatıldı): %s", w.symbol)

            print("TOP yenilendi:", ", ".join(s.replace("/USDT", "") for s in new_syms))
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error("refresh: %s", e)
            await asyncio.sleep(30)


def main() -> None:
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
