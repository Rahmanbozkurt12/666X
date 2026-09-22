#!/usr/bin/env python3
"""
Binance Spot Market Maker — BNB · HIZLI · CANLI

Gerçek MM gibi: her pair'de aynı anda AL (bid) + SAT (ask) LIMIT_MAKER.
50 coin zorunluluğu YOK — varsayılan 6 likit pair (hız + az ban).

1) API KEY yaz  (+ BNB bakiye, Pay fees with BNB AÇIK)
2) pip install "ccxt[pro]"
3) python live_mm.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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

QUOTE = "BNB"
# Az pair = her slota daha çok BNB + gerçek AL/SAT (0.06 BNB için 3 ideal)
NUM_PAIRS = 3
MIN_QUOTE_VOL = 5.0
REPLACE_SEC = 14.0              # emir dinlensin — sık iptal = alım yok
BALANCE_CACHE_SEC = 8.0         # iptal sonrası taze bakiye
FILL_POLL_SEC = 12.0
BOOK_REST_SEC = 8.0
WORKER_STAGGER_SEC = 2.0
MAX_BOOK_SPREAD_BPS = 55.0      # OPEN gibi 80bps pair'leri alma
QUOTE_MOVE_BPS = 15.0           # mid bu kadar oynamazsa emir yenileme
JOIN_BID = True                 # best bid'e yapış — 1 tick geride kalma

# Spread: komisyonu geçsin
MAKER_FEE = 0.00075
FEE_SAFETY = 1.5
MIN_EDGE_BPS = 12.0             # fee üstü
BASE_SPREAD_TICKS = 3.0
MAX_INVENTORY_RATIO = 0.99
TARGET_INVENTORY_RATIO = 0.50   # iki yön MM hedefi
MIN_QUOTE_FREE = 0.003
RESERVE_BNB = 0.0003            # sadece fee tozu — gerisini deploy
USE_QUOTE_FRAC = 0.998          # serbest BNB'nin neredeyse tamamı
POST_ONLY = True
MAX_DRAWDOWN_RATIO = 0.18       # mark-to-market (nakit alış ≠ zarar)

SKIP_BASES = {
    "BNB", "USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDE", "USD1",
    "EUR", "TRY", "BRL", "AEUR",
}

STATS_PATH = Path(__file__).resolve().parent.parent / "output" / "live_mm_day_stats.json"

# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("live_mm")

_RESET = "\033[0m"
_GREEN = "\033[92m"
_RED = "\033[91m"
_ORANGE = "\033[38;5;208m"
_BOLD = "\033[1m"
_DIM = "\033[2m"


def _c(color: str, text: str) -> str:
    return f"{color}{text}{_RESET}"


def say_buy(msg: str) -> None:
    print(_c(_GREEN, f"🟢 AL  | {msg}"), flush=True)


def say_sell(msg: str) -> None:
    print(_c(_RED, f"🔴 SAT | {msg}"), flush=True)


def say_order(msg: str) -> None:
    print(_c(_ORANGE, f"🟠 EMİR | {msg}"), flush=True)


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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
        raise SystemExit("API KEY / SECRET doldur")
    return k, s


def coid() -> str:
    return ("x-MMBNB" + uuid.uuid4().hex)[:32]


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
        await asyncio.sleep(20)
        return
    wait = max(5.0, (until - int(time.time() * 1000)) / 1000.0 + 5.0)
    human = datetime.fromtimestamp(until / 1000, tz=timezone.utc).strftime("%H:%M:%S UTC")
    log.error("IP BAN — %s kadar bekleniyor (≈%.0fs)", human, wait)
    end = time.time() + wait
    while time.time() < end:
        log.info("ban… %.0fs", end - time.time())
        await asyncio.sleep(min(30.0, end - time.time()))


def roundtrip_fee_bps() -> float:
    return MAKER_FEE * 2 * FEE_SAFETY * 10000.0


def min_spread_bps() -> float:
    return roundtrip_fee_bps() + MIN_EDGE_BPS


@dataclass
class DayStats:
    day: str = field(default_factory=utc_day)
    orders: int = 0
    buys: int = 0
    sells: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    won: float = 0.0
    lost: float = 0.0
    fees: float = 0.0
    started_at: float = field(default_factory=time.time)

    def ensure_today(self) -> None:
        d = utc_day()
        if d != self.day:
            self.day = d
            self.orders = self.buys = self.sells = 0
            self.win_trades = self.loss_trades = 0
            self.won = self.lost = self.fees = 0.0
            self.started_at = time.time()

    def net(self) -> float:
        return self.won - self.lost - self.fees

    def print_live(self) -> None:
        self.ensure_today()
        net = self.net()
        print(
            _c(_DIM, "── bilanço ── ")
            + f"emir={self.orders} "
            + _c(_GREEN, f"AL={self.buys}")
            + " "
            + _c(_RED, f"SAT={self.sells}")
            + " "
            + _c(_ORANGE, f"fee={self.fees:.5f}")
            + " "
            + _c(_GREEN if net >= 0 else _RED, f"net={net:+.5f} {QUOTE}"),
            flush=True,
        )

    def print_summary(self) -> None:
        self.ensure_today()
        net = self.net()
        print("=" * 64, flush=True)
        print(_c(_BOLD, f"GÜNLÜK ÖZET ({self.day} UTC)"), flush=True)
        print(f"  Emir={self.orders} AL={self.buys} SAT={self.sells}", flush=True)
        print(f"  Kazanç=+{self.won:.6f} Kayıp=-{self.lost:.6f} Fee={self.fees:.6f} {QUOTE}", flush=True)
        print(_c(_GREEN if net >= 0 else _RED, f"  Net={net:+.6f} {QUOTE}"), flush=True)
        print("=" * 64, flush=True)

    def save(self) -> None:
        try:
            STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATS_PATH.write_text(json.dumps({**asdict(self), "net": self.net()}, indent=2), encoding="utf-8")
        except Exception:
            pass

    @classmethod
    def load(cls) -> "DayStats":
        try:
            if STATS_PATH.exists():
                raw = json.loads(STATS_PATH.read_text(encoding="utf-8"))
                st = cls(**{k: raw[k] for k in cls.__dataclass_fields__ if k in raw})
                st.ensure_today()
                return st
        except Exception:
            pass
        return cls()


class Exchange:
    def __init__(self, key: str, secret: str):
        opts = {
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "rateLimit": 250,
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
                log.warning("WS yok: %s", e)
        self._bal: Optional[dict] = None
        self._bal_ts = 0.0
        self._bal_lock = asyncio.Lock()
        self._order_lock = asyncio.Lock()
        self._fee: Dict[str, float] = {}
        self.n_pairs = 1
        self.stats = DayStats.load()

    def deployable_bnb(self, bal: Optional[dict] = None) -> float:
        """Fee tozu hariç tüm serbest BNB."""
        b = bal if bal is not None else self._bal
        if not b:
            return 0.0
        return max(0.0, self.free(b, QUOTE) - RESERVE_BNB)

    def slot_budget(self, bal: Optional[dict] = None) -> float:
        n = max(1, self.n_pairs)
        return self.deployable_bnb(bal) / n

    async def run(self, fn, *a, **kw):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*a, **kw))

    async def init(self) -> None:
        while True:
            try:
                await self.run(self.rest.load_markets)
                log.info(
                    "CANLI MM | markets=%d WS=%s min_spread≈%.1fbps pairs≤%d",
                    len(self.rest.markets),
                    bool(self.ws),
                    min_spread_bps(),
                    NUM_PAIRS,
                )
                return
            except Exception as e:
                if ban_until_ms(e):
                    await sleep_ban(e)
                    continue
                raise

    async def close(self) -> None:
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass

    async def balance(self, force: bool = False) -> Optional[dict]:
        async with self._bal_lock:
            now = time.time()
            if (not force) and self._bal is not None and now - self._bal_ts < BALANCE_CACHE_SEC:
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

    async def tickers(self) -> Dict[str, dict]:
        try:
            data = await self.run(self.rest.fetch_tickers)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return {}

    async def watch_book(self, symbol: str):
        if not self.ws:
            return None
        try:
            return await self.ws.watch_order_book(symbol, 5)
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            await asyncio.sleep(1)
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

    def free(self, bal: dict, asset: str) -> float:
        return float((bal.get("free") or {}).get(asset, 0) or 0)

    def total(self, bal: dict, asset: str) -> float:
        return float((bal.get("total") or {}).get(asset, 0) or 0)

    def maker(self, symbol: str) -> float:
        if symbol in self._fee:
            return self._fee[symbol]
        m = float((self.rest.markets.get(symbol) or {}).get("maker", MAKER_FEE))
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
        lim = (self.rest.markets.get(symbol) or {}).get("limits") or {}
        return (
            float((lim.get("amount") or {}).get("min") or 0),
            float((lim.get("cost") or {}).get("min") or 0.001),
        )

    def tick(self, symbol: str) -> float:
        p = ((self.rest.markets.get(symbol) or {}).get("precision") or {}).get("price")
        if isinstance(p, int):
            return 10 ** (-p)
        if p:
            return float(p)
        return 1e-6

    async def place(self, symbol: str, side: str, amount: float, price: float) -> Optional[dict]:
        amount = self.amt(symbol, amount)
        price = self.px(symbol, price)
        if amount <= 0 or price <= 0:
            return None
        min_qty, min_cost = self.limits(symbol)
        if amount < min_qty or amount * price < min_cost:
            return None
        async with self._order_lock:
            try:
                say_order(f"{side.upper()} {symbol} qty={amount:.8g} @ {price:.8g}")
                otype = "LIMIT_MAKER" if POST_ONLY else "limit"
                o = await self.run(
                    self.rest.create_order,
                    symbol,
                    otype,
                    side,
                    amount,
                    price,
                    {"newClientOrderId": coid()},
                )
                self.stats.ensure_today()
                self.stats.orders += 1
                # fee sadece gerçek fill'de (iptal edilen emir komisyon yemez)
                self.stats.save()
                return o
            except Exception as e:
                msg = str(e)
                if ban_until_ms(e):
                    await sleep_ban(e)
                    return None
                if any(x in msg for x in ("Post Only", "-5022", "would immediately", "Order would")):
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
                        self.stats.ensure_today()
                        self.stats.orders += 1
                        return o
                    except Exception as e2:
                        log.warning("post-only fail %s %s: %s", side, symbol, e2)
                        return None
                log.error("order %s %s: %s", side, symbol, e)
                return None

    async def cancel_all(self, symbol: str) -> None:
        async with self._order_lock:
            try:
                if hasattr(self.rest, "cancel_all_orders"):
                    await self.run(self.rest.cancel_all_orders, symbol)
            except Exception as e:
                if ban_until_ms(e):
                    await sleep_ban(e)


def pick_liquid_pairs(ex: Exchange, tickers: Dict[str, dict], n: int = NUM_PAIRS) -> List[str]:
    rows: List[Tuple[float, str]] = []
    for sym, t in tickers.items():
        if not sym.endswith(f"/{QUOTE}") or ":" in sym:
            continue
        m = ex.rest.markets.get(sym) or {}
        if m.get("contract") or m.get("spot") is False or m.get("active") is False:
            continue
        base = sym.split("/")[0].upper()
        if base in SKIP_BASES:
            continue
        qv = float(t.get("quoteVolume") or 0)
        if qv < MIN_QUOTE_VOL:
            continue
        # spread kaba filtre (ticker)
        bid = float(t.get("bid") or 0)
        ask = float(t.get("ask") or 0)
        if bid > 0 and ask > bid:
            spr = (ask - bid) / ((ask + bid) / 2) * 10000.0
            if spr > MAX_BOOK_SPREAD_BPS:
                continue
        rows.append((qv, sym))
    rows.sort(key=lambda x: -x[0])
    out = [s for _, s in rows[:n]]
    log.info("MM pairs (%d): %s", len(out), ", ".join(x.replace(f"/{QUOTE}", "") for x in out))
    return out


@dataclass
class Book:
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0


@dataclass
class Slot:
    ex: Exchange
    symbol: str
    slot_bnb: float
    book: Book = field(default_factory=Book)
    hist: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=120))
    base_free: float = 0.0
    quote_free: float = 0.0
    base_total: float = 0.0
    realized: float = 0.0          # kapanmış round-trip PnL (BNB)
    inv_qty: float = 0.0           # maliyet takibi (adet)
    inv_cost: float = 0.0          # bu envanterin BNB maliyeti
    peak_equity: float = 0.0
    seeded_inv: bool = False
    last_quote: float = 0.0
    last_mid_quoted: float = 0.0
    last_bid: float = 0.0
    last_ask: float = 0.0
    last_bid_sz: float = 0.0
    last_ask_sz: float = 0.0
    last_fill_poll: float = 0.0
    seen: Set[str] = field(default_factory=set)
    last_tid: Optional[str] = None
    kill: bool = False
    running: bool = True

    @property
    def base(self) -> str:
        return self.symbol.split("/")[0]

    def on_book(self, ob: dict) -> None:
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        if not bids or not asks:
            return
        bid, ask = float(bids[0][0]), float(asks[0][0])
        mid = (bid + ask) / 2.0
        self.book = Book(bid, ask, mid)
        self.hist.append((time.time(), mid))

    def vol(self) -> float:
        if len(self.hist) < 5:
            return 0.0
        recent = list(self.hist)[-40:]
        rets = [
            math.log(recent[i][1] / recent[i - 1][1])
            for i in range(1, len(recent))
            if recent[i - 1][1] > 0
        ]
        if not rets:
            return 0.0
        return math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(400)

    def inv_pos(self) -> float:
        """+1 = fazla base (sat), -1 = az base (al). Hedef ~TARGET_INVENTORY_RATIO."""
        mid = self.book.mid
        if mid <= 0 or self.slot_bnb <= 0:
            return 0.0
        target = (self.slot_bnb * TARGET_INVENTORY_RATIO) / mid
        if target <= 0:
            return 0.0
        return max(-1.0, min(1.0, (self.base_total - target) / target))

    def min_full_spread(self) -> float:
        mid = self.book.mid
        tick = self.ex.tick(self.symbol)
        fee = max(self.ex.maker(self.symbol), MAKER_FEE) * FEE_SAFETY * 2.0
        return max(mid * fee + mid * (MIN_EDGE_BPS / 10000.0), tick * max(2.0, BASE_SPREAD_TICKS))

    def equity(self) -> float:
        mid = self.book.mid
        open_mtm = 0.0
        if self.inv_qty > 0 and mid > 0:
            open_mtm = self.inv_qty * mid - self.inv_cost
        elif self.base_total > 0 and mid > 0 and self.inv_qty <= 0:
            # seed öncesi kaba MTM
            open_mtm = 0.0
        return self.realized + open_mtm

    def seed_inventory(self) -> None:
        """Başlangıç bakiyesini maliyet olarak mid'den işaretle — alış ≠ DD."""
        if self.seeded_inv:
            return
        mid = self.book.mid
        if mid <= 0:
            return
        qty = max(0.0, self.base_total)
        self.inv_qty = qty
        self.inv_cost = qty * mid
        self.seeded_inv = True
        self.peak_equity = max(self.peak_equity, self.equity())

    async def bal(self) -> None:
        b = await self.ex.balance()
        if not b:
            return
        self.base_free = self.ex.free(b, self.base)
        self.quote_free = self.ex.free(b, QUOTE)
        self.base_total = self.ex.total(b, self.base)

    async def fills(self) -> None:
        now = time.time()
        if now - self.last_fill_poll < FILL_POLL_SEC:
            return
        self.last_fill_poll = now
        for t in await self.ex.trades(self.symbol, 8):
            tid = str(t["id"])
            if tid in self.seen:
                continue
            if self.last_tid and tid.isdigit() and self.last_tid.isdigit() and int(tid) <= int(self.last_tid):
                continue
            self.seen.add(tid)
            self.last_tid = tid
            side = t.get("side")
            amt = float(t.get("amount") or 0)
            px = float(t.get("price") or 0)
            fee = float((t.get("fee") or {}).get("cost") or 0)
            if amt <= 0 or px <= 0:
                continue
            st = self.ex.stats
            st.ensure_today()
            if side == "buy":
                # alış: envanter maliyeti — realized düşmez (nakit çıkışı ≠ zarar)
                self.inv_cost += amt * px + fee
                self.inv_qty += amt
                st.buys += 1
                say_buy(f"{self.symbol} FILL {amt:.6g} @ {px:.8g}")
            else:
                # satış: maliyet düş, fark = realized PnL
                if self.inv_qty > 1e-12:
                    avg = self.inv_cost / self.inv_qty
                    used = min(amt, self.inv_qty)
                    cost = avg * used
                    self.inv_cost = max(0.0, self.inv_cost - cost)
                    self.inv_qty = max(0.0, self.inv_qty - used)
                    # fazla satım (seed dışı) — kalanı mid maliyet say
                    extra = amt - used
                    if extra > 1e-12:
                        mid = self.book.mid or px
                        cost += extra * mid
                else:
                    mid = self.book.mid or px
                    cost = amt * mid
                pnl = amt * px - fee - cost
                self.realized += pnl
                if pnl >= 0:
                    st.won += pnl
                    st.win_trades += 1
                else:
                    st.lost += -pnl
                    st.loss_trades += 1
                st.sells += 1
                say_sell(f"{self.symbol} FILL {amt:.6g} @ {px:.8g} pnl={pnl:+.6f}")
            st.fees += abs(fee)
            st.save()
            self.peak_equity = max(self.peak_equity, self.equity())
            await self.ex.balance(force=True)
            await self.bal()

    def risk_ok(self) -> bool:
        # Mark-to-market: alış maliyeti DD sayılmaz; açık pozisyon + kapalı PnL
        eq = self.equity()
        self.peak_equity = max(self.peak_equity, eq, 0.0)
        dd = self.peak_equity - eq
        if self.slot_bnb > 0 and dd > self.slot_bnb * MAX_DRAWDOWN_RATIO:
            log.error("%s KILL DD %.5f (mtm eq=%.5f)", self.symbol, dd, eq)
            self.kill = True
            return False
        return True

    def quotes(self) -> Optional[Tuple[float, float, float, float]]:
        mid = self.book.mid
        if mid <= 0:
            return None
        # book zaten çok genişse MM etme
        if self.book.bid > 0 and self.book.ask > self.book.bid:
            nat = (self.book.ask - self.book.bid) / mid * 10000.0
            if nat > MAX_BOOK_SPREAD_BPS * 1.2:
                return None
        tick = self.ex.tick(self.symbol)
        half = max(
            self.min_full_spread() / 2.0,
            BASE_SPREAD_TICKS * tick / 2.0,
            mid * 0.00025,
            min(3.0 * max(self.vol(), 0.0004) * mid * 0.04, mid * 0.006),
        )
        ip = self.inv_pos()
        # fazla base → bid düşür / ask yaklaştır; az base → bid yükselt
        skew = max(-half * 0.45, min(half * 0.45, -ip * half * 0.55))
        bid = mid - half + skew
        ask = mid + half + skew
        if ask - bid < self.min_full_spread():
            half = self.min_full_spread() / 2.0
            bid, ask = mid - half + skew * 0.5, mid + half + skew * 0.5

        bid = self.ex.px(self.symbol, bid)
        ask = self.ex.px(self.symbol, ask)

        # Best bid'e yapış (LIMIT_MAKER) — 1 tick geride kalınca alım neredeyse yok
        if JOIN_BID and self.book.bid > 0:
            join = self.book.bid
            if ask - join >= self.min_full_spread() * 0.85 and join < ask:
                bid = self.ex.px(self.symbol, join)
            elif bid >= self.book.bid:
                bid = self.ex.px(self.symbol, self.book.bid - tick)
        elif self.book.bid > 0 and bid >= self.book.bid:
            bid = self.ex.px(self.symbol, self.book.bid - tick)

        if self.book.ask > 0 and ask <= self.book.ask:
            # ask tarafında da best ask'a join (post-only çaprazlama)
            join_ask = self.book.ask
            if join_ask - bid >= self.min_full_spread() * 0.85 and join_ask > bid:
                ask = self.ex.px(self.symbol, join_ask)
            else:
                ask = self.ex.px(self.symbol, self.book.ask + tick)

        if ask <= bid or ask - bid < self.min_full_spread() * 0.85:
            return None

        min_qty, min_cost = self.ex.limits(self.symbol)
        # Adil pay: tüm serbest BNB / pair (fee tozu hariç) — idle bırakma
        fair = max(self.slot_bnb, self.ex.slot_budget())
        self.slot_bnb = fair
        n = max(1, self.ex.n_pairs)
        # Bu pair en fazla serbest/n alır; hepsi birlikte ≈ %100 deploy
        share = max(fair, max(0.0, self.quote_free - RESERVE_BNB) * USE_QUOTE_FRAC / n)

        inv_bnb = self.base_total * mid
        room = max(0.0, share * MAX_INVENTORY_RATIO - inv_bnb)
        q_avail = max(0.0, self.quote_free - RESERVE_BNB) * USE_QUOTE_FRAC
        buy_budget = min(q_avail, share, room if room >= min_cost else share)
        if buy_budget < min_cost or inv_bnb >= share * MAX_INVENTORY_RATIO:
            buy_budget = 0.0
        sell_base = max(self.base_free, 0.0)

        # Emir boyutu ≈ slot/pay'ın tamamı
        target = max(min_cost * 1.05, buy_budget if buy_budget > 0 else share)
        sz = target / mid
        bid_sz = sz * max(0.9, 1.0 - ip * 0.2)
        ask_sz = sz * max(0.9, 1.0 + ip * 0.2)

        if buy_budget >= min_cost:
            bid_sz = min(bid_sz, buy_budget / mid)
        else:
            bid_sz = 0.0

        if sell_base * mid >= min_cost * 0.95:
            ask_sz = min(ask_sz, sell_base * USE_QUOTE_FRAC, share / mid * 1.2)
        else:
            ask_sz = 0.0

        if inv_bnb >= share * MAX_INVENTORY_RATIO:
            bid_sz = 0.0

        # hard cap: notional asla payı aşmasın
        if bid_sz > 0 and mid > 0:
            bid_sz = min(bid_sz, buy_budget / mid)

        bid_sz = self.ex.amt(self.symbol, bid_sz)
        ask_sz = self.ex.amt(self.symbol, ask_sz)
        if bid_sz > 0 and bid > 0 and bid_sz * bid > buy_budget * 1.001:
            bid_sz = self.ex.amt(self.symbol, buy_budget / bid)
        if bid_sz > 0 and bid_sz * bid < min_cost:
            # min notional'a büyüt (bütçe yetiyorsa)
            need = min_cost / bid * 1.02
            if need * bid <= buy_budget + 1e-12:
                bid_sz = self.ex.amt(self.symbol, need)
            if bid_sz * bid < min_cost:
                bid_sz = 0.0
        if ask_sz > 0 and ask_sz * ask < min_cost:
            need = min_cost / ask
            if sell_base >= need:
                ask_sz = self.ex.amt(self.symbol, need * 1.02)
            if ask_sz * ask < min_cost:
                ask_sz = 0.0
        return bid, ask, bid_sz, ask_sz

    async def replace(self) -> None:
        if time.time() - self.last_quote < REPLACE_SEC:
            return
        if self.kill:
            await self.ex.cancel_all(self.symbol)
            return
        # mid az oynadıysa dinlenen emri bozma (alım şansı kaçmasın)
        if self.last_mid_quoted > 0 and self.book.mid > 0:
            moved = abs(self.book.mid - self.last_mid_quoted) / self.last_mid_quoted * 10000.0
            hold = REPLACE_SEC * 3.0
            if moved < QUOTE_MOVE_BPS and time.time() - self.last_quote < hold:
                return

        q = self.quotes()
        if not q:
            self.last_quote = time.time()
            return
        bid, ask, bid_sz, ask_sz = q

        # Yeni fiyat/adet neredeyse aynıysa iptal-spam yok
        same_bid = (
            self.last_bid > 0
            and abs(bid - self.last_bid) / self.last_bid * 10000.0 < 3.0
            and abs(bid_sz - self.last_bid_sz) <= max(1e-8, self.last_bid_sz * 0.08)
        )
        same_ask = (
            self.last_ask > 0
            and abs(ask - self.last_ask) / self.last_ask * 10000.0 < 3.0
            and abs(ask_sz - self.last_ask_sz) <= max(1e-8, self.last_ask_sz * 0.08)
        )
        if (bid_sz <= 0 or same_bid) and (ask_sz <= 0 or same_ask) and (self.last_bid > 0 or self.last_ask > 0):
            self.last_quote = time.time()
            return

        # Önce iptal → bakiye serbest kalsın → sonra iki yön emir
        await self.ex.cancel_all(self.symbol)
        await asyncio.sleep(0.25)
        await self.ex.balance(force=True)
        await self.bal()

        q = self.quotes()
        if not q:
            self.last_quote = time.time()
            self.last_bid = self.last_ask = 0.0
            self.last_bid_sz = self.last_ask_sz = 0.0
            return
        bid, ask, bid_sz, ask_sz = q

        if bid_sz > 0:
            await self.ex.place(self.symbol, "buy", bid_sz, bid)
            await asyncio.sleep(0.12)
        if ask_sz > 0:
            await self.ex.place(self.symbol, "sell", ask_sz, ask)

        self.last_quote = time.time()
        self.last_mid_quoted = self.book.mid
        self.last_bid, self.last_ask = bid, ask
        self.last_bid_sz, self.last_ask_sz = bid_sz, ask_sz
        spr = (ask - bid) / mid * 10000 if (mid := self.book.mid) else 0
        log.info(
            "%s quote BUY %.8g×%.6g | SELL %.8g×%.6g | spr=%.1fbps | qFree=%.4f baseFree=%.6g",
            self.symbol, bid, bid_sz, ask, ask_sz, spr, self.quote_free, self.base_free,
        )

    async def run(self, delay: float = 0.0) -> None:
        await asyncio.sleep(delay)
        if self.symbol not in self.ex.rest.markets:
            log.error("market yok: %s", self.symbol)
            return
        log.info("%s START slot=%.4f %s | maker MM", self.symbol, self.slot_bnb, QUOTE)
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
            self.seed_inventory()
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
                elif time.time() - last_rest >= BOOK_REST_SEC:
                    ob = await self.ex.book_rest(self.symbol)
                    if ob:
                        self.on_book(ob)
                    last_rest = time.time()
                await self.bal()
                if not self.seeded_inv:
                    self.seed_inventory()
                await self.fills()
                self.risk_ok()
                await self.replace()
                if time.time() - last_print > 30:
                    print(
                        f"{self.symbol:12} mid={self.book.mid:.6g} "
                        f"base={self.base_total:.6g} rpnl={self.realized:.5f} "
                        f"eq={self.equity():+.5f} "
                        f"{'KILL' if self.kill else 'OK'}",
                        flush=True,
                    )
                    self.ex.stats.print_live()
                    last_print = time.time()
                await asyncio.sleep(0.2 if use_ws else 1.2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("%s: %s", self.symbol, e)
                if ban_until_ms(e):
                    await sleep_ban(e)
                else:
                    await asyncio.sleep(3)
        await self.ex.cancel_all(self.symbol)
        self.running = False


async def main_async() -> None:
    print("=" * 64)
    print("Binance REAL MM · BNB · LIMIT_MAKER bid+ask · hızlı")
    print(f"pairs≤{NUM_PAIRS} | replace={REPLACE_SEC:.0f}s | min_spread≈{min_spread_bps():.1f}bps")
    print(f"CCXT {ccxt.__version__} | Pro={'var' if ccxtpro else 'YOK — pip install ccxt[pro]'}")
    print("Pay fees with BNB AÇIK olsun")
    print("=" * 64)

    key, secret = resolve_keys()
    ex = Exchange(key, secret)
    await ex.init()

    bal = await ex.balance(force=True)
    if not bal:
        raise SystemExit("Bakiye yok / ban")
    free = ex.free(bal, QUOTE)
    spend = ex.deployable_bnb(bal)
    print(f"{QUOTE} free≈{free:.6f} | deploy≈{spend:.6f} (fee tozu {RESERVE_BNB})")

    tickers = await ex.tickers()
    symbols = pick_liquid_pairs(ex, tickers, NUM_PAIRS)
    if len(symbols) < 2:
        raise SystemExit("Yeterli likit BNB pair yok")

    n = len(symbols)
    ex.n_pairs = n
    slot = spend / n if n else 0.0
    if slot < MIN_QUOTE_FREE:
        raise SystemExit(f"Slot {slot:.5f} {QUOTE} küçük — BNB ekle veya NUM_PAIRS düşür")

    print(f"{n}×{slot:.6f} {QUOTE} | TÜM serbest BNB deploy | Ctrl+C dur")
    print(_c(_GREEN, "AL=yeşil"), "|", _c(_RED, "SAT=kırmızı"), "|", _c(_ORANGE, "EMİR=turuncu"))
    print("=" * 64)

    slots = [Slot(ex, s, slot) for s in symbols]
    tasks = [asyncio.create_task(s.run(i * WORKER_STAGGER_SEC)) for i, s in enumerate(slots)]
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
        ex.stats.print_summary()
        ex.stats.save()
        print("Kapandı")


def main() -> None:
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nDurdu")


if __name__ == "__main__":
    main()
