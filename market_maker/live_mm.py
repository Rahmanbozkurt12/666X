#!/usr/bin/env python3
"""
Binance Spot Market Maker — BNB · KÂR ÖNCELİKLİ · 20 COİN · CANLI

Tüm Binance BNB spot'u tarar (oynaklık + multi-method).
En az 20 pair açar. Spread fee+edge üstü; az cancel = az komisyon.

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
# TÜM Binance BNB spot — volatilite öncelikli, EN AZ 20 coin
SCAN_ALL = True
MAX_OPEN = 20
MIN_OPEN = 20                   # zorunlu minimum açık coin
SCAN_SEC = 300.0                # seyrek rescan — ban riski ↓
MIN_QUOTE_VOL = 0.05
REPLACE_SEC = 60.0              # emir uzun dinlensin
BALANCE_CACHE_SEC = 25.0
FILL_POLL_SEC = 40.0
BOOK_REST_SEC = 15.0
WORKER_STAGGER_SEC = 1.5
LOOP_SLEEP_SEC = 2.5            # 20 pair × 0.2s döngü = ban
MAX_BOOK_SPREAD_BPS = 200.0
QUOTE_MOVE_BPS = 40.0
JOIN_BID = True
HOLD_QUOTE_MULT = 3.0
USE_WS = False                  # 20 WS kitap = IP ban; REST yeter
API_RATE_MS = 500               # ccxt rateLimit

# KÂR > KOMİSYON: round-trip fee üstüne net edge
MAKER_FEE = 0.00075
FEE_SAFETY = 2.0                # fee'yi abartılı varsay → daha geniş spread
MIN_EDGE_BPS = 40.0             # fee üstü net kâr hedefi
BASE_SPREAD_TICKS = 4.0
MAX_INVENTORY_RATIO = 0.95
TARGET_INVENTORY_RATIO = 0.40
MIN_QUOTE_FREE = 0.0020         # 20 slot × ~0.002 = 0.04 BNB ile çalışır
RESERVE_BNB = 0.0003
USE_QUOTE_FRAC = 0.995
POST_ONLY = True
MAX_DRAWDOWN_RATIO = 0.22
MIN_SELL_EDGE_BPS = 45.0        # avg maliyet + fee + kâr — zararlı satışı kes
# Skor ağırlıkları (multi-method)
W_VOLATILITY = 3.0
W_RANGE = 1.5
W_VOLUME = 0.35                 # hacim düşük ağırlık
W_SPREAD_FIT = 1.2              # fee'yi geçen ama aşırı geniş olmayan spread
W_MOMENTUM = 1.0
MIN_METHODS_PASS = 2            # en az 2 yöntem yeşil olmadan açma

SKIP_BASES = {
    "BNB", "USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDE", "USD1",
    "EUR", "TRY", "BRL", "AEUR",
}

STATS_PATH = Path(__file__).resolve().parent.parent / "output" / "live_mm_day_stats.json"
NUM_PAIRS = MAX_OPEN  # geriye uyum

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


_ban_until_ms_shared = 0
_ban_log_ts = 0.0
_ban_lock: Optional[asyncio.Lock] = None


def _get_ban_lock() -> asyncio.Lock:
    global _ban_lock
    if _ban_lock is None:
        _ban_lock = asyncio.Lock()
    return _ban_lock


def ban_until_ms(err: Exception | str) -> Optional[int]:
    msg = str(err)
    m = re.search(r"banned until (\d+)", msg, re.I)
    if m:
        return int(m.group(1))
    if "418" in msg or "-1003" in msg or "DDoSProtection" in msg or "teapot" in msg.lower():
        return int(time.time() * 1000) + 15 * 60 * 1000
    return None


async def sleep_ban(err: Exception | str) -> None:
    """Tek paylaşımlı ban beklemesi — 20 worker aynı anda spam log basmasın."""
    global _ban_until_ms_shared, _ban_log_ts
    until = ban_until_ms(err) or (int(time.time() * 1000) + 60_000)
    async with _get_ban_lock():
        _ban_until_ms_shared = max(_ban_until_ms_shared, until)
        target = _ban_until_ms_shared
    wait = max(5.0, (target - int(time.time() * 1000)) / 1000.0 + 3.0)
    human = datetime.fromtimestamp(target / 1000, tz=timezone.utc).strftime("%H:%M:%S UTC")
    now = time.time()
    if now - _ban_log_ts > 25.0:
        log.error("IP BAN — %s kadar tek sırada bekleniyor (≈%.0fs)", human, wait)
        _ban_log_ts = now
    end = time.time() + wait
    while time.time() < end:
        left = end - time.time()
        if time.time() - _ban_log_ts > 60.0:
            log.info("ban… %.0fs", left)
            _ban_log_ts = time.time()
        await asyncio.sleep(min(60.0, max(1.0, left)))


async def wait_if_banned() -> None:
    now_ms = int(time.time() * 1000)
    if _ban_until_ms_shared > now_ms:
        await sleep_ban(f"banned until {_ban_until_ms_shared}")


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
            "rateLimit": API_RATE_MS,
            "options": {"defaultType": "spot", "adjustForTimeDifference": True},
        }
        self.rest = ccxt.binance(opts)
        self.rest.set_sandbox_mode(False)
        self.ws = None
        if USE_WS and ccxtpro is not None:
            try:
                self.ws = ccxtpro.binance(opts)
                self.ws.set_sandbox_mode(False)
            except Exception as e:
                log.warning("WS yok: %s", e)
        elif not USE_WS:
            log.info("WS kapalı (USE_WS=False) — REST kitap, ban riski düşük")
        self._bal: Optional[dict] = None
        self._bal_ts = 0.0
        self._bal_lock = asyncio.Lock()
        self._order_lock = asyncio.Lock()
        self._fee: Dict[str, float] = {}
        self.n_pairs = 1
        self._last_stats_print = 0.0
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
        await wait_if_banned()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*a, **kw))

    async def init(self) -> None:
        while True:
            try:
                await self.run(self.rest.load_markets)
                log.info(
                    "CANLI MM | markets=%d WS=%s min_spread≈%.1fbps max_open=%d SCAN_ALL=%s",
                    len(self.rest.markets),
                    bool(self.ws),
                    min_spread_bps(),
                    MAX_OPEN,
                    SCAN_ALL,
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


def method_scores(t: dict, spr_bps: float) -> Tuple[float, int, Dict[str, float]]:
    """
    Multi-method skor: volatilite / range / hacim / spread-fit / momentum.
    Dönüş: (toplam skor, geçen yöntem sayısı, kırılım)
    """
    qv = float(t.get("quoteVolume") or 0)
    pct = float(t.get("percentage") or 0)
    last = float(t.get("last") or t.get("close") or 0)
    high = float(t.get("high") or 0)
    low = float(t.get("low") or 0)

    vol_abs = abs(pct)
    s_vol = vol_abs * W_VOLATILITY
    pass_vol = vol_abs >= 1.5

    if last > 0 and high > low > 0:
        rng = (high - low) / last * 100.0
    else:
        rng = vol_abs * 0.8
    s_range = rng * W_RANGE
    pass_range = rng >= 2.0

    s_vol_amt = math.log1p(max(0.0, qv)) * W_VOLUME * 10.0
    pass_qv = qv >= MIN_QUOTE_VOL

    need = min_spread_bps()
    if spr_bps <= 0:
        s_spread = 0.0
        pass_spr = False
    elif spr_bps < need * 0.5:
        s_spread = spr_bps * 0.05
        pass_spr = False
    elif spr_bps <= need * 2.5:
        s_spread = (need * 2.5 - abs(spr_bps - need)) * W_SPREAD_FIT * 0.1
        pass_spr = True
    else:
        s_spread = max(0.0, (MAX_BOOK_SPREAD_BPS - spr_bps)) * 0.02
        pass_spr = spr_bps <= MAX_BOOK_SPREAD_BPS

    if pct <= -2.0:
        s_mom = (abs(pct) * 0.6) * W_MOMENTUM
        pass_mom = True
    elif pct >= 3.0:
        s_mom = pct * 0.25 * W_MOMENTUM
        pass_mom = True
    else:
        s_mom = abs(pct) * 0.1
        pass_mom = vol_abs >= 2.0

    parts = {
        "vol": s_vol,
        "range": s_range,
        "qv": s_vol_amt,
        "spread": s_spread,
        "mom": s_mom,
    }
    passed = sum([pass_vol, pass_range, pass_qv, pass_spr, pass_mom])
    return sum(parts.values()), passed, parts


def scan_all_bnb_pairs(ex: Exchange, tickers: Dict[str, dict]) -> Tuple[List[Tuple[float, str]], int]:
    """Tüm spot BNB pair — oynaklık + multi-method öncelikli."""
    rows: List[Tuple[float, str]] = []
    scanned = 0
    markets = ex.rest.markets or {}
    for sym, m in markets.items():
        if not sym.endswith(f"/{QUOTE}") or ":" in sym:
            continue
        if m.get("contract") or m.get("spot") is False or m.get("active") is False:
            continue
        base = sym.split("/")[0].upper()
        if base in SKIP_BASES:
            continue
        scanned += 1
        t = tickers.get(sym) or {}
        qv = float(t.get("quoteVolume") or 0)
        if qv < MIN_QUOTE_VOL:
            continue
        bid = float(t.get("bid") or 0)
        ask = float(t.get("ask") or 0)
        last = float(t.get("last") or t.get("close") or 0)
        if bid <= 0 or ask <= bid:
            if last <= 0:
                continue
            mid = last
            spr = 40.0
        else:
            mid = (bid + ask) / 2.0
            spr = (ask - bid) / mid * 10000.0
            if spr > MAX_BOOK_SPREAD_BPS:
                continue
        score, npass, _ = method_scores(t, spr)
        if npass < MIN_METHODS_PASS:
            continue
        pct = abs(float(t.get("percentage") or 0))
        score *= 1.0 + min(pct, 40.0) / 50.0
        rows.append((score, sym))
    rows.sort(key=lambda x: -x[0])
    return rows, scanned


def pick_open_pairs(
    ex: Exchange,
    tickers: Dict[str, dict],
    n: int,
    keep: Optional[Set[str]] = None,
) -> Tuple[List[str], int, int]:
    """Tüm CEX tara → en oynak n pair (hedef ≥ MIN_OPEN=20)."""
    ranked, scanned = scan_all_bnb_pairs(ex, tickers)
    qualified = len(ranked)
    keep = keep or set()
    out: List[str] = []
    ranked_syms = [s for _, s in ranked]
    ranked_set = set(ranked_syms)
    for s in keep:
        if s in ranked_set and len(out) < n:
            out.append(s)
    for s in ranked_syms:
        if s not in out and len(out) < n:
            out.append(s)
    if len(out) < n:
        for _, sym in ranked:
            if sym not in out:
                out.append(sym)
            if len(out) >= n:
                break
    if len(out) < n:
        for sym, m in (ex.rest.markets or {}).items():
            if not sym.endswith(f"/{QUOTE}") or ":" in sym:
                continue
            if m.get("active") is False or m.get("contract"):
                continue
            base = sym.split("/")[0].upper()
            if base in SKIP_BASES or sym in out:
                continue
            t = tickers.get(sym) or {}
            if float(t.get("quoteVolume") or 0) < MIN_QUOTE_VOL:
                continue
            out.append(sym)
            if len(out) >= n:
                break
    names = ", ".join(x.replace(f"/{QUOTE}", "") for x in out)
    log.info(
        "CEX TARAMA | taranan=%d aday=%d açılacak=%d (hedef≥%d) → %s",
        scanned,
        qualified,
        len(out),
        MIN_OPEN,
        names or "-",
    )
    return out, scanned, qualified


def max_open_for_balance(spend: float) -> int:
    """EN AZ 20 coin — bakiye ~0.04+ BNB ise zorla MAX_OPEN."""
    if spend < MIN_QUOTE_FREE * 2:
        return 0
    if spend >= MIN_QUOTE_FREE * MIN_OPEN * 0.85:
        return MAX_OPEN
    n = max(2, int(spend / MIN_QUOTE_FREE))
    return min(MAX_OPEN, n)


def pick_liquid_pairs(ex: Exchange, tickers: Dict[str, dict], n: int = MAX_OPEN) -> List[str]:
    out, _, _ = pick_open_pairs(ex, tickers, n)
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
            fee_raw = t.get("fee") or {}
            fee_cost = float(fee_raw.get("cost") or 0)
            fee_ccy = str(fee_raw.get("currency") or QUOTE).upper()
            # fee'yi BNB'ye çevir (baz asset fee'si abartılı net yazmasın)
            if fee_cost and fee_ccy == QUOTE:
                fee = fee_cost
            elif fee_cost and fee_ccy == self.base:
                fee = fee_cost * px
            else:
                fee = fee_cost * px if fee_ccy != QUOTE else fee_cost
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
            await self.ex.balance(force=False)
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

        # Best bid'e yapış — ama fee+edge spread'i ezme
        if JOIN_BID and self.book.bid > 0:
            join = self.book.bid
            if ask - join >= self.min_full_spread() and join < ask:
                bid = self.ex.px(self.symbol, join)
            elif bid >= self.book.bid:
                bid = self.ex.px(self.symbol, self.book.bid - tick)
        elif self.book.bid > 0 and bid >= self.book.bid:
            bid = self.ex.px(self.symbol, self.book.bid - tick)

        if self.book.ask > 0 and ask <= self.book.ask:
            join_ask = self.book.ask
            if join_ask - bid >= self.min_full_spread() and join_ask > bid:
                ask = self.ex.px(self.symbol, join_ask)
            else:
                ask = self.ex.px(self.symbol, self.book.ask + tick)

        min_qty, min_cost = self.ex.limits(self.symbol)
        sell_base_early = max(self.base_free, 0.0)
        # Spread fee+edge altındaysa: envanter yoksa çık; varsa sadece kârlı sat
        if ask <= bid or ask - bid < self.min_full_spread():
            if sell_base_early * mid < min_cost * 0.95:
                return None
            ask = self.ex.px(
                self.symbol,
                max(ask, (self.book.ask + tick) if self.book.ask > 0 else mid * 1.001),
            )
            bid = self.ex.px(self.symbol, ask - self.min_full_spread())

        # Adil pay: tüm serbest BNB / pair (fee tozu hariç)
        fair = max(self.slot_bnb, self.ex.slot_budget())
        self.slot_bnb = max(fair, MIN_QUOTE_FREE)
        n = max(1, self.ex.n_pairs)
        share = max(self.slot_bnb, max(0.0, self.quote_free - RESERVE_BNB) * USE_QUOTE_FRAC / n)

        inv_bnb = self.base_total * mid
        room = max(0.0, share * MAX_INVENTORY_RATIO - inv_bnb)
        q_avail = max(0.0, self.quote_free - RESERVE_BNB) * USE_QUOTE_FRAC
        buy_budget = min(q_avail, share, room if room >= min_cost else 0.0)
        if buy_budget < min_cost or inv_bnb >= share * MAX_INVENTORY_RATIO:
            buy_budget = 0.0
        sell_base = sell_base_early

        # Ortalama maliyet tabanı — zararlı maker satışı engelle
        avg_cost = (self.inv_cost / self.inv_qty) if self.inv_qty > 1e-12 else 0.0
        bid_sz_force_zero = False
        if avg_cost > 0:
            floor = avg_cost * (1.0 + MAKER_FEE * FEE_SAFETY * 2.0 + MIN_SELL_EDGE_BPS / 10000.0)
            if ask < floor:
                ask = self.ex.px(self.symbol, floor)
            # post-only: ask kitap ask'ının altında kalmasın
            if self.book.ask > 0 and ask < self.book.ask:
                ask = self.ex.px(self.symbol, max(floor, self.book.ask))
            if self.book.ask > 0 and ask <= self.book.ask:
                ask = self.ex.px(self.symbol, max(floor, self.book.ask + tick))
            if ask <= bid:
                bid_sz_force_zero = True
                buy_budget = 0.0
                bid = self.ex.px(self.symbol, max(tick, ask - self.min_full_spread()))

        if ask <= bid:
            if sell_base * mid >= min_cost * 0.95:
                ask = self.ex.px(
                    self.symbol,
                    max(
                        avg_cost * (1.0 + MIN_SELL_EDGE_BPS / 10000.0) if avg_cost > 0 else mid * 1.001,
                        (self.book.ask + tick) if self.book.ask > 0 else mid * 1.001,
                    ),
                )
                bid = self.ex.px(self.symbol, ask - self.min_full_spread())
                buy_budget = 0.0
                bid_sz_force_zero = True
            else:
                return None

        # Alım boyutu
        if buy_budget >= min_cost and not bid_sz_force_zero:
            bid_sz = self.ex.amt(self.symbol, buy_budget / mid)
            if bid_sz * bid < min_cost:
                need = min_cost / bid * 1.02
                bid_sz = self.ex.amt(self.symbol, need) if need * bid <= buy_budget else 0.0
            if bid_sz * bid < min_cost:
                bid_sz = 0.0
        else:
            bid_sz = 0.0

        # SATIM: envanteri share ile KISITLAMA — stuck HBAR BUY×0 SELL×0 buna bağlıydı
        if sell_base * mid >= min_cost * 0.95:
            if inv_bnb >= share * TARGET_INVENTORY_RATIO or buy_budget <= 0 or bid_sz_force_zero:
                ask_sz = sell_base * USE_QUOTE_FRAC
            else:
                ask_sz = min(sell_base * USE_QUOTE_FRAC, max(share / mid, min_cost / mid * 1.05))
            ask_sz = self.ex.amt(self.symbol, ask_sz)
            if ask_sz * ask < min_cost:
                need = min_cost / ask * 1.02
                ask_sz = self.ex.amt(self.symbol, need) if sell_base >= need else 0.0
            if ask_sz * ask < min_cost:
                ask_sz = 0.0
        else:
            ask_sz = 0.0

        if inv_bnb >= share * MAX_INVENTORY_RATIO:
            bid_sz = 0.0

        return bid, ask, bid_sz, ask_sz

    async def replace(self) -> None:
        if time.time() - self.last_quote < REPLACE_SEC:
            return
        if self.kill:
            await self.ex.cancel_all(self.symbol)
            return
        # mid az oynadıysa dinlenen emri bozma (cancel spam = fee/fırsat kaybı)
        if self.last_mid_quoted > 0 and self.book.mid > 0:
            moved = abs(self.book.mid - self.last_mid_quoted) / self.last_mid_quoted * 10000.0
            hold = REPLACE_SEC * HOLD_QUOTE_MULT
            if moved < QUOTE_MOVE_BPS and time.time() - self.last_quote < hold:
                return

        q = self.quotes()
        if not q:
            self.last_quote = time.time()
            return
        bid, ask, bid_sz, ask_sz = q

        # Yeni fiyat/adet neredeyse aynıysa iptal-spam yok
        same_bid = (
            bid_sz > 0
            and self.last_bid > 0
            and abs(bid - self.last_bid) / self.last_bid * 10000.0 < 3.0
            and abs(bid_sz - self.last_bid_sz) <= max(1e-8, self.last_bid_sz * 0.08)
        )
        same_ask = (
            ask_sz > 0
            and self.last_ask > 0
            and abs(ask - self.last_ask) / self.last_ask * 10000.0 < 3.0
            and abs(ask_sz - self.last_ask_sz) <= max(1e-8, self.last_ask_sz * 0.08)
        )
        # iki yön de 0 ise eski emri temizle (stuck BUY×0 SELL×0)
        if bid_sz <= 0 and ask_sz <= 0:
            if self.last_bid > 0 or self.last_ask > 0:
                await self.ex.cancel_all(self.symbol)
                self.last_bid = self.last_ask = 0.0
                self.last_bid_sz = self.last_ask_sz = 0.0
            self.last_quote = time.time()
            return
        if (bid_sz <= 0 or same_bid) and (ask_sz <= 0 or same_ask) and (self.last_bid > 0 or self.last_ask > 0):
            self.last_quote = time.time()
            return

        # Önce iptal → bakiye (cache) → iki yön emir
        await self.ex.cancel_all(self.symbol)
        await asyncio.sleep(0.35)
        await self.ex.balance(force=False)
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

        use_ws = USE_WS and self.ex.ws is not None
        last_rest = 0.0
        last_print = 0.0
        last_bal = 0.0
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
                await wait_if_banned()
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
                if time.time() - last_bal >= BALANCE_CACHE_SEC:
                    await self.bal()
                    last_bal = time.time()
                if not self.seeded_inv:
                    self.seed_inventory()
                await self.fills()
                self.risk_ok()
                await self.replace()
                if time.time() - last_print > 120:
                    print(
                        f"{self.symbol:12} mid={self.book.mid:.6g} "
                        f"base={self.base_total:.6g} rpnl={self.realized:.5f} "
                        f"eq={self.equity():+.5f} "
                        f"{'KILL' if self.kill else 'OK'}",
                        flush=True,
                    )
                    # bilanço spam yok — en fazla 2 dk'da bir global
                    if time.time() - self.ex._last_stats_print > 120:
                        self.ex.stats.print_live()
                        self.ex._last_stats_print = time.time()
                    last_print = time.time()
                await asyncio.sleep(LOOP_SLEEP_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("%s: %s", self.symbol, e)
                if ban_until_ms(e):
                    await sleep_ban(e)
                else:
                    await asyncio.sleep(5)
        await self.ex.cancel_all(self.symbol)
        self.running = False


async def main_async() -> None:
    print("=" * 64)
    print("Binance REAL MM · BNB · KÂR>FEE · ≥20 COİN · OYNAK TARAMA")
    print(
        f"SCAN_ALL | open={MIN_OPEN}..{MAX_OPEN} | rescan={SCAN_SEC:.0f}s | "
        f"replace≥{REPLACE_SEC:.0f}s | min_spread≈{min_spread_bps():.1f}bps "
        f"(fee≈{roundtrip_fee_bps():.1f}+edge{MIN_EDGE_BPS:.0f})"
    )
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
    want = max_open_for_balance(spend)
    if want < 2:
        raise SystemExit(f"BNB yetersiz (deploy={spend:.5f}) — en az {MIN_QUOTE_FREE * 2:.4f} lazım")

    symbols, scanned, qualified = pick_open_pairs(ex, tickers, want)
    if len(symbols) < 2:
        raise SystemExit(
            f"Yeterli likit BNB pair yok (taranan={scanned} aday={qualified})"
        )

    n = len(symbols)
    ex.n_pairs = n
    slot = spend / n if n else 0.0
    print(
        f"TARAMA OK | Binance spot BNB: {scanned} pair tarandı, {qualified} aday, "
        f"{n} açık × {slot:.6f} {QUOTE}"
    )
    print(_c(_GREEN, "AL=yeşil"), "|", _c(_RED, "SAT=kırmızı"), "|", _c(_ORANGE, "EMİR=turuncu"))
    print("=" * 64)

    active: Dict[str, Tuple[Slot, asyncio.Task]] = {}
    stop_all = asyncio.Event()

    async def start_slot(sym: str, delay: float, slot_bnb: float) -> None:
        if sym in active:
            return
        s = Slot(ex, sym, slot_bnb)
        t = asyncio.create_task(s.run(delay))
        active[sym] = (s, t)

    async def stop_slot(sym: str) -> None:
        pair = active.pop(sym, None)
        if not pair:
            return
        s, t = pair
        s.running = False
        s.kill = True
        try:
            await ex.cancel_all(sym)
        except Exception:
            pass
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    for i, sym in enumerate(symbols):
        await start_slot(sym, i * WORKER_STAGGER_SEC, slot)

    async def rotator() -> None:
        while not stop_all.is_set():
            try:
                await asyncio.wait_for(stop_all.wait(), timeout=SCAN_SEC)
                break
            except asyncio.TimeoutError:
                pass
            try:
                bal2 = await ex.balance(force=True)
                spend2 = ex.deployable_bnb(bal2) if bal2 else spend
                want2 = max_open_for_balance(spend2)
                # envanteri olan pair'leri tut
                keep: Set[str] = set()
                for sym, (s, _) in list(active.items()):
                    if s.base_total * max(s.book.mid, 0) >= MIN_QUOTE_FREE * 0.5:
                        keep.add(sym)
                    if s.kill:
                        await stop_slot(sym)
                tickers2 = await ex.tickers()
                new_syms, sc, qu = pick_open_pairs(ex, tickers2, want2, keep=keep)
                if len(new_syms) < 2:
                    log.warning("rescan zayıf: taranan=%d aday=%d", sc, qu)
                    continue
                new_set = set(new_syms)
                # fazla/kötü slotları kapat (envanterli keep hariç mümkün olduğunca)
                for sym in list(active.keys()):
                    if sym not in new_set:
                        if sym in keep and len(active) <= want2:
                            continue
                        log.info("ROTasyon OUT %s", sym)
                        await stop_slot(sym)
                n_now = max(1, len(new_set | set(active.keys())))
                ex.n_pairs = max(len(new_syms), len(active), 1)
                slot2 = spend2 / ex.n_pairs if ex.n_pairs else slot
                for i, sym in enumerate(new_syms):
                    if sym not in active:
                        log.info("ROTasyon IN %s slot=%.5f", sym, slot2)
                        await start_slot(sym, i * 0.4, slot2)
                    else:
                        active[sym][0].slot_bnb = slot2
                log.info(
                    "RESCAN | taranan=%d aday=%d açık=%d slot≈%.5f",
                    sc,
                    qu,
                    len(active),
                    slot2,
                )
            except Exception as e:
                log.error("rotator: %s", e)
                if ban_until_ms(e):
                    await sleep_ban(e)

    rot_task = asyncio.create_task(rotator())
    try:
        while active and not stop_all.is_set():
            done = [sym for sym, (s, t) in active.items() if t.done() or not s.running]
            for sym in done:
                await stop_slot(sym)
            if not active:
                break
            await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        pass
    finally:
        stop_all.set()
        rot_task.cancel()
        try:
            await rot_task
        except (asyncio.CancelledError, Exception):
            pass
        for sym in list(active.keys()):
            await stop_slot(sym)
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
