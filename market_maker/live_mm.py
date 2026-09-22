#!/usr/bin/env python3
"""
Binance Spot Market Maker — SADECE USDT · 5m YEŞİL

Sadece */USDT. 189sn tarama. ≥15 coin kesin.
Giriş: -20 dip+yeşil+hacim↑ | yüksek hacim+yükselen | hacim dibinden yükselen.
Aynı coine 1dk AL yok. Geniş fee+edge (komisyon tamponu).

1) API KEY yaz  (USDT bakiye + Pay fees with BNB isteğe bağlı)
2) pip install "ccxt[pro]"
3) python live_mm.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import signal
import sys
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

import ccxt

try:
    import ccxt.pro as ccxtpro
except ImportError:
    ccxtpro = None  # type: ignore

# =============================================================================
BINANCE_API_KEY = "BURAYA_API_KEY"
BINANCE_API_SECRET = "BURAYA_SECRET_KEY"
# =============================================================================

QUOTE = "USDT"                  # SADECE USDT
SCAN_ALL = True
FORCE_MIN_OPEN = True           # her taramada ≥15 kesin
MAX_OPEN = 20
MIN_OPEN = 15
CANDIDATE_POOL = 200
SCAN_SEC = 189.0                # 189 sn'de bir tarama
REPLACE_SEC = 75.0
BALANCE_CACHE_SEC = 8.0
FILL_POLL_SEC = 20.0
BOOK_REST_SEC = 12.0
WORKER_STAGGER_SEC = 0.6
HOLD_QUOTE_MULT = 5.0
LOOP_SLEEP_SEC = 1.5
USE_WS = False
API_RATE_MS = 400
ROTATE_COOLDOWN_SEC = 5 * 60
KEEP_GRACE_SEC = 45.0
SAME_COIN_BUY_SEC = 60.0        # aynı coine 1 dk ara
KLINE_TF = "5m"
KLINE_LIMIT = 12
KLINE_TOP_N = 80                # daha çok coine 5m yeşil bak

MIN_USDT_VOL = 400.0
SOFT_USDT_VOL = 1_000.0
FLOOR_USDT_VOL = 400.0
HIGH_USDT_VOL = 100_000.0       # yüksek hacim eşiği
MAX_BOOK_SPREAD_BPS = 140.0
MIN_BOOK_SPREAD_BPS = 6.0
QUOTE_MOVE_BPS = 40.0
JOIN_TOUCH = False
MIN_VOL_RISE_PCT = 0.02         # hacim yükselmeye başladı
MIN_VOL_RISE_USDT = 800.0
DIP_PCT_LO = -28.0              # ~-20 dip bandı
DIP_PCT_HI = -8.0
MAX_ABS_24H_PCT = 35.0
MIN_24H_PCT = 0.0

MAKER_FEE = 0.00075
FEE_SAFETY = 2.8                # komisyon tamponu ↑ — paramız eksilmesin
MIN_EDGE_BPS = 90.0             # net edge yüksek
MIN_SELL_EDGE_BPS = 80.0
BASE_SPREAD_TICKS = 5.0
BEHIND_TICKS = 2.0
MAX_HALF_SPREAD_BPS = 150.0
MAX_INVENTORY_RATIO = 0.45
TARGET_INVENTORY_RATIO = 0.15
MIN_QUOTE_FREE = 5.0            # USDT slot min (≥15 için)
RESERVE_USDT = 2.0
USE_QUOTE_FRAC = 0.999
MIN_USDT_PER_SLOT = 5.0
POST_ONLY = True
MAX_DRAWDOWN_RATIO = 0.08
MAX_PAIR_HOLD_SEC = 15 * 60
MAX_BUY_LEAD = 2
MIN_WR_TO_BUY = 0.42
MIN_TRADES_FOR_WR = 8
TOXIC_LOSS_STREAK = 2
TOXIC_PAUSE_SEC = 10 * 60
MOMENTUM_BUY_BPS = -12.0
POST_FILL_COOLDOWN_SEC = SAME_COIN_BUY_SEC
VOL_WIDEN_MULT = 2.4
SKEW_STRENGTH = 0.85

# Tüm giriş metodları (skor ağırlıkları)
W_VOL_RISE = 5.0                # hacim yükseliyor
W_VOL_RISE_PCT = 3.5
W_VOLUME = 0.60
W_HIGH_VOL_RISE = 3.0           # yüksek hacim + yükselmeye devam
W_DIP_REBOUND = 4.5             # -20 dip rebound
W_GREEN_5M = 4.0                # 5m yeşile dönüş
W_VOL_5M = 3.0
W_VOL_RECOVER = 3.2             # hacim -lerde ama yükselmeye başlamış
W_VOLATILITY = 0.9
W_RANGE = 0.60
W_SPREAD_FIT = 1.20
W_MOMENTUM = 0.35
MIN_METHODS_PASS = 0            # metodlar skorlar; elemez — pad ≥15
FALLBACK_METHODS_PASS = 0

SKIP_BASES = {
    "BNB", "USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDE", "USD1",
    "EUR", "TRY", "BRL", "AEUR", "TRX",
}
SKIP_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
SKIP_CONTAINS = ("3L", "3S", "2L", "2S", "LEVERAGED")

_OUT = Path(__file__).resolve().parent.parent / "output"
STATS_PATH = _OUT / "live_mm_day_stats.json"
STATE_PATH = _OUT / "live_mm_state.json"
VOL_SNAP_PATH = _OUT / "live_mm_vol_snap.json"

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

_ban_until_ms_shared = 0
_ban_log_ts = 0.0
_ban_lock: Optional[asyncio.Lock] = None


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
        raise SystemExit("API KEY / SECRET doldur (dosya başı veya env)")
    return k, s


def coid() -> str:
    return ("x-MMUSDT" + uuid.uuid4().hex)[:32]


def skip_base(base: str) -> bool:
    b = base.upper()
    if b in SKIP_BASES:
        return True
    if any(b.endswith(suf) or b.startswith(suf) for suf in SKIP_SUFFIXES):
        return True
    if any(x in b for x in SKIP_CONTAINS):
        return True
    return False


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
    global _ban_until_ms_shared, _ban_log_ts
    until = ban_until_ms(err) or (int(time.time() * 1000) + 60_000)
    async with _get_ban_lock():
        _ban_until_ms_shared = max(_ban_until_ms_shared, until)
        target = _ban_until_ms_shared
    wait = max(5.0, (target - int(time.time() * 1000)) / 1000.0 + 3.0)
    human = datetime.fromtimestamp(target / 1000, tz=timezone.utc).strftime("%H:%M:%S UTC")
    now = time.time()
    if now - _ban_log_ts > 25.0:
        log.error("IP BAN — %s kadar bekleniyor (≈%.0fs)", human, wait)
        _ban_log_ts = now
    end = time.time() + wait
    while time.time() < end:
        if time.time() - _ban_log_ts > 60.0:
            log.info("ban… %.0fs", end - time.time())
            _ban_log_ts = time.time()
        await asyncio.sleep(min(60.0, max(1.0, end - time.time())))


async def wait_if_banned() -> None:
    if _ban_until_ms_shared > int(time.time() * 1000):
        await sleep_ban(f"banned until {_ban_until_ms_shared}")


def roundtrip_fee_bps() -> float:
    return MAKER_FEE * 2 * FEE_SAFETY * 10000.0


def min_spread_bps() -> float:
    return roundtrip_fee_bps() + MIN_EDGE_BPS


@dataclass
class DayStats:
    day: str = field(default_factory=utc_day)
    orders: int = 0
    cancels: int = 0
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
            self.orders = self.cancels = self.buys = self.sells = 0
            self.win_trades = self.loss_trades = 0
            self.won = self.lost = self.fees = 0.0
            self.started_at = time.time()

    def net(self) -> float:
        # won/lost fee düşülmüş; fee bilanço bilgi satırı
        return self.won - self.lost

    def win_rate(self) -> float:
        n = self.win_trades + self.loss_trades
        return (self.win_trades / n) if n else 0.0

    def allow_buys(self, flat: bool = False) -> bool:
        """
        flat=True → envanter yok; SADECE-SAT kilidi Binance'te sıfır emir bırakır.
        Bu durumda AL serbest (aksi halde hiç order görünmez).
        """
        self.ensure_today()
        if flat:
            return True
        if self.buys > self.sells + MAX_BUY_LEAD:
            return False
        n = self.win_trades + self.loss_trades
        if n >= MIN_TRADES_FOR_WR and self.win_rate() < MIN_WR_TO_BUY:
            return False
        return True

    def unlock_stale_gates(self) -> None:
        """Önceki oturum AL>>SAT / düşük WR kilidini kır — flat'te emir görünsün."""
        self.ensure_today()
        if self.buys > self.sells + MAX_BUY_LEAD:
            log.warning(
                "stats unlock: AL lead %d→%d (eski oturum kilidi kırıldı)",
                self.buys, self.sells,
            )
            self.buys = self.sells
            self.save()
        n = self.win_trades + self.loss_trades
        if n >= MIN_TRADES_FOR_WR and self.win_rate() < MIN_WR_TO_BUY:
            log.warning(
                "stats: WR=%.0f%% düşük — envanter yokken AL yine açılır (flat unlock)",
                self.win_rate() * 100.0,
            )

    def print_live(self) -> None:
        self.ensure_today()
        net = self.net()
        wr = self.win_rate()
        mode = "AL+SAT" if self.allow_buys() else "SADECE-SAT (flat→AL açık)"
        print(
            _c(_DIM, "── bilanço ── ")
            + f"emir={self.orders} iptal={self.cancels} "
            + _c(_GREEN, f"AL={self.buys}")
            + " "
            + _c(_RED, f"SAT={self.sells}")
            + " "
            + _c(_ORANGE, f"fee={self.fees:.5f}")
            + f" wr={wr:.0%} [{mode}] "
            + _c(_GREEN if net >= 0 else _RED, f"net={net:+.5f} {QUOTE}"),
            flush=True,
        )

    def print_summary(self) -> None:
        self.ensure_today()
        net = self.net()
        print("=" * 64, flush=True)
        print(_c(_BOLD, f"GÜNLÜK ÖZET ({self.day} UTC)"), flush=True)
        print(
            f"  Emir={self.orders} İptal={self.cancels} AL={self.buys} SAT={self.sells} "
            f"WR={self.win_rate():.0%}",
            flush=True,
        )
        print(
            f"  Kazanç=+{self.won:.6f} Kayıp=-{self.lost:.6f} Fee(bilgi)={self.fees:.6f} {QUOTE}",
            flush=True,
        )
        print(_c(_GREEN if net >= 0 else _RED, f"  Net={net:+.6f} {QUOTE}"), flush=True)
        print("=" * 64, flush=True)

    def save(self) -> None:
        try:
            STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATS_PATH.write_text(
                json.dumps({**asdict(self), "net": self.net()}, indent=2),
                encoding="utf-8",
            )
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
            log.info("WS kapalı — REST (ban riski ↓)")
        self._bal: Optional[dict] = None
        self._bal_ts = 0.0
        self._bal_lock = asyncio.Lock()
        self._order_lock = asyncio.Lock()
        self._fee: Dict[str, float] = {}
        self._buy_reserved: Dict[str, float] = {}  # symbol -> resting AL in USDT
        self.n_pairs = 1
        self._bnb_usd = 0.0  # sadece "Pay fees with BNB" → USDT çevirisi
        self.stats = DayStats.load()
        self.stats.unlock_stale_gates()
        self.stopped = False
        self._quote_skip_log: Dict[str, float] = {}

    def quote_of(self, symbol: str) -> str:
        return symbol.split("/")[1].upper() if "/" in symbol else QUOTE

    def deployable_usdt(self, bal: Optional[dict] = None) -> float:
        b = bal if bal is not None else self._bal
        if not b:
            return 0.0
        return max(0.0, self.free(b, "USDT") - RESERVE_USDT) * USE_QUOTE_FRAC

    def slot_budget(self, bal: Optional[dict] = None, symbol: str = "") -> float:
        return self.deployable_usdt(bal) / max(1, self.n_pairs)

    def reserved_for_quote(self, quote: str = "USDT", exclude: str = "") -> float:
        s = 0.0
        for sym, v in self._buy_reserved.items():
            if sym == exclude:
                continue
            if self.quote_of(sym) == quote:
                s += v
        return s

    def set_buy_reserve(self, symbol: str, amt: float) -> None:
        if amt <= 0:
            self._buy_reserved.pop(symbol, None)
        else:
            self._buy_reserved[symbol] = amt

    def free_quote_for(self, symbol: str, bal: Optional[dict] = None) -> float:
        b = bal if bal is not None else self._bal
        if not b:
            return 0.0
        raw = max(0.0, self.free(b, "USDT") - RESERVE_USDT) * USE_QUOTE_FRAC
        return max(0.0, raw - self.reserved_for_quote("USDT", exclude=symbol))

    def fee_to_usdt(self, fee_cost: float, fee_ccy: str, px: float, base: str) -> float:
        """Fee'yi USDT cinsine çevir (Pay fees with BNB dahil)."""
        if fee_cost <= 0:
            return 0.0
        ccy = (fee_ccy or "USDT").upper()
        if ccy == "USDT":
            return fee_cost
        if ccy == base:
            return fee_cost * px
        if ccy == "BNB" and self._bnb_usd > 1e-9:
            return fee_cost * self._bnb_usd
        return fee_cost * px

    async def run(self, fn, *a, **kw):
        await wait_if_banned()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*a, **kw))

    async def init(self) -> None:
        while True:
            try:
                await self.run(self.rest.load_markets)
                log.info(
                    "CANLI MM | markets=%d WS=%s min_spread≈%.1fbps open=%d force=%s KÂR-KİLİT",
                    len(self.rest.markets),
                    bool(self.ws),
                    min_spread_bps(),
                    MAX_OPEN,
                    FORCE_MIN_OPEN,
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

    async def ohlcv(self, symbol: str, timeframe: str = KLINE_TF, limit: int = KLINE_LIMIT) -> List[list]:
        try:
            data = await self.run(self.rest.fetch_ohlcv, symbol, timeframe, None, limit)
            return data if isinstance(data, list) else []
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return []

    async def trades(self, symbol: str, limit: int = 12):
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
        m = float((self.rest.markets.get(symbol) or {}).get("maker", MAKER_FEE) or MAKER_FEE)
        self._fee[symbol] = max(m, 0.0)
        return self._fee[symbol]

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
                self.stats.save()
                self._bal_ts = 0.0  # bakiye stale olmasın — sonraki slot doğru free görsün
                return o
            except Exception as e:
                msg = str(e)
                if ban_until_ms(e):
                    await sleep_ban(e)
                    return None
                if any(x in msg for x in ("Post Only", "-5022", "would immediately", "Order would")):
                    return None
                if any(x in msg for x in ("insufficient", "-2010", "MIN_NOTIONAL", "-1013")):
                    log.warning("order skip %s %s: %s", side, symbol, e)
                    self._bal_ts = 0.0
                    return None
                log.error("order %s %s: %s", side, symbol, e)
                return None

    async def cancel_all(self, symbol: str) -> None:
        async with self._order_lock:
            try:
                if hasattr(self.rest, "cancel_all_orders"):
                    await self.run(self.rest.cancel_all_orders, symbol)
                    self.stats.ensure_today()
                    self.stats.cancels += 1
                    self.stats.save()
                    self.set_buy_reserve(symbol, 0.0)
                    self._bal_ts = 0.0
            except Exception as e:
                if ban_until_ms(e):
                    await sleep_ban(e)


def method_scores(
    t: dict,
    spr_bps: float,
    usdt_vol: float,
    vol_rise_pct: float = 0.0,
    vol_rise_abs: float = 0.0,
    green_5m: float = 0.0,
    vol_5m_rise: float = 0.0,
) -> Tuple[float, int, Dict[str, float]]:
    """
    Tüm giriş metodları (skor; elemez):
    1) Hacim yükseliyor
    2) Yüksek hacim + yükselmeye devam
    3) ~-20 dip + rebound
    4) 5m yeşile dönüş + 5m hacim↑
    5) Hacim -lerde ama yükselmeye başlamış (recover)
    """
    pct_signed = float(t.get("percentage") or 0)
    pct = abs(pct_signed)
    last = float(t.get("last") or t.get("close") or 0)
    high = float(t.get("high") or 0)
    low = float(t.get("low") or 0)
    vol_abs = pct

    # 1) Hacim yükseliyor
    s_rise = 0.0
    if vol_rise_abs > 0:
        s_rise += math.log1p(vol_rise_abs) * W_VOL_RISE
    if vol_rise_pct > 0:
        s_rise += min(vol_rise_pct, 3.0) * 100.0 * W_VOL_RISE_PCT
    pass_rise = vol_rise_pct > 0 or vol_rise_abs > 0 or vol_rise_pct >= MIN_VOL_RISE_PCT

    # 2) Yüksek hacim + yükselmeye devam
    s_high = 0.0
    if usdt_vol >= HIGH_USDT_VOL and vol_rise_pct > 0:
        s_high = (
            math.log1p(usdt_vol / HIGH_USDT_VOL) * 40.0 * W_HIGH_VOL_RISE
            + min(vol_rise_pct, 1.0) * 55.0
        )
        pass_rise = True

    # 3) ~-20 dip rebound
    s_dip = 0.0
    if DIP_PCT_LO <= pct_signed <= DIP_PCT_HI:
        depth = min(abs(pct_signed), 25.0) / 25.0
        rise_boost = 1.0 + min(max(vol_rise_pct, 0.0), 1.5)
        s_dip = (40.0 + depth * 80.0) * W_DIP_REBOUND * rise_boost
        pass_rise = True

    # 4) 5m yeşil + 5m hacim
    s_green = max(0.0, green_5m) * W_GREEN_5M
    s_v5 = max(0.0, vol_5m_rise) * W_VOL_5M
    pass_5m = green_5m > 0.2 or vol_5m_rise > 0.05
    # dip + yeşil mum → ekstra boost (kullanıcı kuralı)
    if DIP_PCT_LO <= pct_signed <= DIP_PCT_HI and green_5m >= 0.6 and vol_rise_pct > 0:
        s_dip += 60.0 * W_DIP_REBOUND
        s_green += 25.0 * W_GREEN_5M

    # 5) Hacim -lerde / düşük ama yükselmeye başlamış
    s_recover = 0.0
    soft_vol = usdt_vol < HIGH_USDT_VOL
    if soft_vol and vol_rise_pct >= MIN_VOL_RISE_PCT:
        s_recover = (
            min(vol_rise_pct, 2.0) * 70.0 * W_VOL_RECOVER
            + math.log1p(max(0.0, vol_rise_abs)) * 0.8 * W_VOL_RECOVER
        )
        pass_rise = True

    s_vol = min(vol_abs, 18.0) * W_VOLATILITY
    pass_vol = vol_abs <= MAX_ABS_24H_PCT or (DIP_PCT_LO <= pct_signed <= DIP_PCT_HI)
    if last > 0 and high > low > 0:
        rng = (high - low) / last * 100.0
    else:
        rng = max(vol_abs * 0.8, 0.5)
    s_range = min(rng, 25.0) * W_RANGE
    pass_range = rng <= 50.0
    s_vol_amt = math.log1p(max(0.0, usdt_vol)) * W_VOLUME
    pass_qv = usdt_vol >= FLOOR_USDT_VOL
    need = min_spread_bps()
    if spr_bps <= 0:
        s_spread, pass_spr = 0.0, True
    elif spr_bps <= MAX_BOOK_SPREAD_BPS * 1.25:
        s_spread = max(0.0, (MAX_BOOK_SPREAD_BPS * 1.25 - abs(spr_bps - need))) * W_SPREAD_FIT * 0.08
        pass_spr = True
    else:
        s_spread = 0.0
        pass_spr = spr_bps <= MAX_BOOK_SPREAD_BPS * 1.6
    s_mom = max(0.0, pct_signed) * W_MOMENTUM
    pass_mom = pct_signed <= MAX_ABS_24H_PCT and pct_signed >= -abs(MAX_ABS_24H_PCT)
    parts = {
        "rise": s_rise,
        "high": s_high,
        "dip": s_dip,
        "g5": s_green,
        "v5": s_v5,
        "recover": s_recover,
        "vol": s_vol,
        "range": s_range,
        "qv": s_vol_amt,
        "spread": s_spread,
        "mom": s_mom,
    }
    passed = sum([pass_rise or pass_5m, pass_vol, pass_range, pass_qv, pass_spr, pass_mom])
    return sum(parts.values()), passed, parts


def analyze_5m_ohlcv(ohlcv: List[list]) -> Tuple[float, float]:
    """
    5m mum: yeşil skor (0..1+) + hacim yükseliş oranı.
    Yeşil mumları erken yakala → başarı ↑
    """
    if not ohlcv or len(ohlcv) < 4:
        return 0.0, 0.0
    # [ts, o, h, l, c, vol]
    greens = 0
    body_sum = 0.0
    for candle in ohlcv[-6:]:
        o, h, l, c, v = float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4]), float(candle[5])
        if c > o and o > 0:
            greens += 1
            body_sum += (c - o) / o
    last = ohlcv[-1]
    o, c = float(last[1]), float(last[4])
    last_green = 1.0 if c > o else (0.35 if c >= o * 0.999 else 0.0)
    # son 3 vs önceki 3 hacim
    vols = [float(x[5] or 0) for x in ohlcv]
    prev = sum(vols[-6:-3]) / 3.0 if len(vols) >= 6 else sum(vols[:-3]) / max(1, len(vols) - 3)
    cur = sum(vols[-3:]) / 3.0
    vol_rise = ((cur - prev) / prev) if prev > 1e-12 else (0.5 if cur > 0 else 0.0)
    green_score = last_green * 1.2 + greens / 6.0 + min(body_sum * 50.0, 1.5)
    return green_score, max(0.0, vol_rise)


def load_vol_snap() -> Dict[str, float]:
    try:
        if VOL_SNAP_PATH.exists():
            raw = json.loads(VOL_SNAP_PATH.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in (raw.get("vols") or raw).items()}
    except Exception:
        pass
    return {}


def save_vol_snap(vols: Dict[str, float]) -> None:
    try:
        VOL_SNAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        VOL_SNAP_PATH.write_text(
            json.dumps({"ts": time.time(), "vols": vols}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _ticker(tickers: Dict[str, dict], sym: str) -> dict:
    t = tickers.get(sym)
    if t:
        return t
    return tickers.get(sym.replace("/", "")) or {}


def _px(tickers: Dict[str, dict], sym: str) -> float:
    t = _ticker(tickers, sym)
    for k in ("last", "close", "bid", "ask"):
        v = float(t.get(k) or 0)
        if v > 0:
            return v
    return 0.0


def usdt_fx(tickers: Dict[str, dict]) -> Dict[str, float]:
    fx = {"USDT": 1.0, "USDC": 1.0, "FDUSD": 1.0, "BUSD": 1.0, "TUSD": 1.0, "USD1": 1.0, "USDE": 1.0}
    for q in ("BNB", "BTC", "ETH", "TRY", "EUR"):
        px = _px(tickers, f"{q}/USDT") or _px(tickers, f"{q}/USDC")
        if px > 0:
            fx[q] = px
    return fx


def quote_vol_usdt(t: dict, quote: str, fx: Dict[str, float]) -> float:
    qv = float(t.get("quoteVolume") or 0)
    bv = float(t.get("baseVolume") or 0)
    last = float(t.get("last") or t.get("close") or 0)
    rate = fx.get(quote, 0.0)
    if qv > 0 and rate > 0:
        return qv * rate
    if bv > 0 and last > 0 and rate > 0:
        return bv * last * rate
    return 0.0


def book_spread_bps(t: dict) -> Optional[float]:
    bid = float(t.get("bid") or 0)
    ask = float(t.get("ask") or 0)
    last = float(t.get("last") or t.get("close") or 0)
    if bid > 0 and ask > bid:
        return (ask - bid) / ((ask + bid) / 2.0) * 10000.0
    if last > 0:
        return 40.0
    return None


def iter_spot(ex: Exchange):
    for sym, m in (ex.rest.markets or {}).items():
        if ":" in sym or "/" not in sym:
            continue
        if m.get("contract") or m.get("spot") is False or m.get("active") is False:
            continue
        base, quote = sym.split("/")[0].upper(), sym.split("/")[1].upper()
        yield sym, base, quote, m


def scan_all_binance(
    ex: Exchange,
    tickers: Dict[str, dict],
    min_usdt_vol: float,
    min_pass: int,
    max_spread: float,
    require_rise: bool = True,
) -> Tuple[List[Tuple[float, str, str, float]], int, int]:
    """
    Skor önceliği (sadece */USDT):
    1) Hacim YÜKSELİYOR
    2) Hacim zaten yüksek + yükselmeye devam
    3) ~-20 dip + hacim artışı (rebound / yeşil mum öncesi)
    """
    fx = usdt_fx(tickers)
    prev = load_vol_snap()
    now_vols: Dict[str, float] = {}
    per_base: Dict[str, dict] = {}
    scanned = spot_n = 0
    for sym, base, quote, _m in iter_spot(ex):
        spot_n += 1
        if quote != QUOTE:
            continue
        if skip_base(base):
            continue
        scanned += 1
        t = _ticker(tickers, sym)
        if not t:
            continue
        spr = book_spread_bps(t)
        if spr is None:
            continue
        usdt_vol = quote_vol_usdt(t, quote, fx)
        pct_signed = float(t.get("percentage") or 0)
        pct = abs(pct_signed)
        row = per_base.get(base)
        if row is None or usdt_vol >= float(row.get("usdt_vol") or 0):
            per_base[base] = {
                "base": base,
                "usdt_vol": usdt_vol,
                "pct": pct,
                "pct_signed": pct_signed,
                "t": t,
                "spr": spr,
                "trade_sym": sym,
            }

    ranked: List[Tuple[float, str, str, float]] = []
    for base, row in per_base.items():
        trade_sym = row.get("trade_sym") or ""
        if not trade_sym:
            continue
        usdt_vol = float(row["usdt_vol"])
        now_vols[base] = usdt_vol
        if usdt_vol < min_usdt_vol:
            continue
        eff_spr = float(row.get("spr") or 9e9)
        if eff_spr > max_spread:
            continue
        pct_signed = float(row.get("pct_signed") or row["t"].get("percentage") or 0)
        if pct_signed > MAX_ABS_24H_PCT or pct_signed < -abs(MAX_ABS_24H_PCT):
            continue
        prev_v = float(prev.get(base) or 0.0)
        if prev_v > 1e-9:
            rise_abs = usdt_vol - prev_v
            rise_pct = rise_abs / prev_v
        else:
            rise_abs = 0.0
            rise_pct = 0.0
        is_dip = DIP_PCT_LO <= pct_signed <= DIP_PCT_HI
        is_high_rising = usdt_vol >= HIGH_USDT_VOL and rise_pct > 0
        is_vol_recover = rise_pct >= MIN_VOL_RISE_PCT or rise_abs >= MIN_VOL_RISE_USDT
        rising_ok = is_vol_recover or rise_pct > 0
        if require_rise and prev and not (rising_ok or is_dip or is_high_rising):
            continue
        score, npass, _ = method_scores(row["t"], eff_spr, usdt_vol, rise_pct, rise_abs)
        if min_pass > 0 and npass < min_pass and not (is_dip or is_high_rising or rising_ok):
            continue
        # Öncelik boost: dip / yüksek-hacim-yükselen / recover
        if is_dip and rising_ok:
            score *= 1.25
        if is_high_rising:
            score *= 1.20
        if is_vol_recover and usdt_vol < HIGH_USDT_VOL:
            score *= 1.12
        ranked.append((score, base, trade_sym, usdt_vol))
    ranked.sort(key=lambda x: -x[0])
    save_vol_snap(now_vols)
    return ranked, scanned, spot_n


async def enrich_ranked_5m(
    ex: Exchange,
    ranked: List[Tuple[float, str, str, float]],
) -> List[Tuple[float, str, str, float]]:
    """5m yeşil + hacim↑ skoru; dip+yeşil coinleri öne alır."""
    if not ranked:
        return ranked
    enriched: List[Tuple[float, str, str, float]] = []
    n_check = min(len(ranked), max(KLINE_TOP_N, 80))
    top = ranked[:n_check]
    rest = ranked[n_check:]
    green_n = dip_green_n = 0
    for sc, base, trade_sym, usdt_vol in top:
        g5 = v5 = 0.0
        if trade_sym:
            ohlcv = await ex.ohlcv(trade_sym, KLINE_TF, KLINE_LIMIT)
            g5, v5 = analyze_5m_ohlcv(ohlcv)
            await asyncio.sleep(0.05)
        bonus = g5 * W_GREEN_5M * 10.0 + v5 * 120.0 * W_VOL_5M
        # -20 dip + yeşile dönüş + hacim↑ → kesin öncelik
        if g5 >= 0.7 and v5 > 0:
            bonus += 80.0 * W_DIP_REBOUND
            green_n += 1
        if g5 >= 0.8:
            dip_green_n += 1
        enriched.append((sc + bonus, base, trade_sym, usdt_vol))
    enriched.extend(rest)
    enriched.sort(key=lambda x: -x[0])
    log.info(
        "5m kontrol | aday=%d yeşil=%d güçlü=%d tf=%s",
        len(top), green_n, dip_green_n, KLINE_TF,
    )
    return enriched


def all_trade_pairs(ex: Exchange, tickers: Dict[str, dict]) -> List[Tuple[float, str]]:
    """Pad listesi: sadece likit */USDT."""
    out: List[Tuple[float, str]] = []
    fx = usdt_fx(tickers)
    for sym, base, quote, _m in iter_spot(ex):
        if quote != QUOTE or skip_base(base):
            continue
        t = _ticker(tickers, sym)
        last = float(t.get("last") or t.get("close") or t.get("bid") or t.get("ask") or 0)
        if last <= 0:
            continue
        usdt_vol = quote_vol_usdt(t, quote, fx)
        if usdt_vol < FLOOR_USDT_VOL:
            continue
        pct = abs(float(t.get("percentage") or 0))
        out.append((usdt_vol * (1.0 + min(pct, 15.0) / 30.0), sym))
    out.sort(key=lambda x: -x[0])
    return out


async def pick_open_pairs(
    ex: Exchange,
    tickers: Dict[str, dict],
    n: int,
    keep: Optional[Set[str]] = None,
    cooldown: Optional[Dict[str, float]] = None,
) -> Tuple[List[str], int, int]:
    """
    1) Hacim yükselen / yüksek+yükselen / -20 dip+yeşil / hacim recover
    2) 5m yeşil mum + 5m hacim artışı
    3) Rotasyon: soğuk coinleri ele
    4) Pad: likit */USDT — ≥15 kesin
    """
    n = max(n, MIN_OPEN if FORCE_MIN_OPEN else n)
    keep = keep or set()
    cooldown = cooldown or {}
    now = time.time()
    cold = {s for s, ts in cooldown.items() if now - ts < ROTATE_COOLDOWN_SEC}

    ranked, scanned, spot_n = scan_all_binance(
        ex, tickers, MIN_USDT_VOL, MIN_METHODS_PASS, MAX_BOOK_SPREAD_BPS, require_rise=False
    )
    ranked = await enrich_ranked_5m(ex, ranked)
    pool: List[str] = []

    def push(sym: str, ignore_cold: bool = False) -> None:
        if not sym or sym in pool:
            return
        if not sym.endswith("/USDT"):
            return
        if (not ignore_cold) and sym in cold and sym not in keep:
            return
        pool.append(sym)

    for s in keep:
        push(s)
    for _sc, _b, trade_sym, _v in ranked:
        if trade_sym:
            push(trade_sym)
        if len(pool) >= CANDIDATE_POOL:
            break
    if len(pool) < CANDIDATE_POOL:
        loose, sc2, _ = scan_all_binance(
            ex, tickers, SOFT_USDT_VOL, FALLBACK_METHODS_PASS, MAX_BOOK_SPREAD_BPS * 1.6, require_rise=False
        )
        if len(loose) > KLINE_TOP_N:
            loose = await enrich_ranked_5m(ex, loose[: max(KLINE_TOP_N, 60)])
        scanned = max(scanned, sc2)
        for _sc, _b, trade_sym, _v in loose:
            if trade_sym:
                push(trade_sym)
            if len(pool) >= CANDIDATE_POOL:
                break

    pad = all_trade_pairs(ex, tickers)
    for _qv, sym in pad:
        push(sym)
        if len(pool) >= max(n, CANDIDATE_POOL):
            break
    if FORCE_MIN_OPEN and len(pool) < n:
        for _qv, sym in pad:
            push(sym, ignore_cold=True)
            if len(pool) >= n:
                break

    out: List[str] = []
    for s in keep:
        if s.endswith("/USDT") and s not in out:
            out.append(s)
    rest = [s for s in pool if s not in out]
    top = rest[: max(n * 2, 40)]
    mid = rest[len(top) :]
    head, tail = top[: max(n, 10)], top[max(n, 10) :]
    random.shuffle(tail)
    for s in head + tail + mid:
        if len(out) >= n:
            break
        out.append(s)

    if FORCE_MIN_OPEN and len(out) < n:
        for _qv, sym in pad:
            if sym not in out:
                out.append(sym)
            if len(out) >= n:
                break

    names = ", ".join(s.replace("/USDT", "") for s in out)
    log.info(
        "TARAMA | spot=%d taranan=%d pad=%d havuz=%d odak=%d (≥%d) USDT → %s",
        spot_n,
        scanned,
        len(pad),
        len(pool),
        len(out),
        MIN_OPEN,
        names or "-",
    )
    return out, scanned, len(pool)


def max_open_for_balance(spend: float) -> int:
    if spend <= RESERVE_USDT:
        return 0
    if FORCE_MIN_OPEN:
        return MAX_OPEN
    return min(MAX_OPEN, max(1, int(spend / MIN_QUOTE_FREE)))


@dataclass
class Book:
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0


@dataclass
class Slot:
    ex: Exchange
    symbol: str
    slot_quote: float
    book: Book = field(default_factory=Book)
    hist: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=160))
    base_free: float = 0.0
    quote_free: float = 0.0
    base_total: float = 0.0
    realized: float = 0.0
    inv_qty: float = 0.0
    inv_cost: float = 0.0
    peak_equity: float = 0.0
    seeded_inv: bool = False
    last_quote: float = 0.0
    last_mid_quoted: float = 0.0
    last_bid: float = 0.0
    last_ask: float = 0.0
    last_bid_sz: float = 0.0
    last_ask_sz: float = 0.0
    last_fill_poll: float = 0.0
    last_bal_poll: float = 0.0
    last_buy_fill_ts: float = 0.0
    loss_streak: int = 0
    toxic_until: float = 0.0
    seen: Set[str] = field(default_factory=set)
    last_tid: Optional[str] = None
    kill: bool = False
    running: bool = True
    opened_at: float = field(default_factory=time.time)

    @property
    def base(self) -> str:
        return self.symbol.split("/")[0]

    @property
    def quote_asset(self) -> str:
        return "USDT"

    def on_book(self, ob: dict) -> None:
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        if not bids or not asks:
            return
        bid, ask = float(bids[0][0]), float(asks[0][0])
        if bid <= 0 or ask <= bid:
            return
        mid = (bid + ask) / 2.0
        self.book = Book(bid, ask, mid)
        self.hist.append((time.time(), mid))

    def vol(self) -> float:
        if len(self.hist) < 6:
            return 0.0
        recent = list(self.hist)[-50:]
        rets = [
            math.log(recent[i][1] / recent[i - 1][1])
            for i in range(1, len(recent))
            if recent[i - 1][1] > 0 and recent[i][1] > 0
        ]
        if not rets:
            return 0.0
        return math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(400)

    def inv_pos(self) -> float:
        mid = self.book.mid
        if mid <= 0 or self.slot_quote <= 0:
            return 0.0
        target = (self.slot_quote * TARGET_INVENTORY_RATIO) / mid
        if target <= 0:
            return 0.0
        return max(-1.0, min(1.0, (self.base_total - target) / target))

    def mid_momentum_bps(self, lookback: int = 12) -> float:
        """Kısa vade mid değişimi (bps). Negatif = düşüş → AL toksik."""
        if len(self.hist) < max(4, lookback):
            return 0.0
        recent = list(self.hist)[-lookback:]
        a, b = recent[0][1], recent[-1][1]
        if a <= 0 or b <= 0:
            return 0.0
        return (b - a) / a * 10000.0

    def is_toxic(self) -> bool:
        return time.time() < self.toxic_until

    def min_full_spread(self) -> float:
        mid = self.book.mid
        tick = self.ex.tick(self.symbol)
        fee = max(self.ex.maker(self.symbol), MAKER_FEE) * FEE_SAFETY * 2.0
        # Vol genişletmesi — profesyonel MM: risk ↑ → spread ↑
        vol_extra = min(max(self.vol(), 0.0) * VOL_WIDEN_MULT, MAX_HALF_SPREAD_BPS / 10000.0) * mid
        edge = mid * (MIN_EDGE_BPS / 10000.0)
        return max(mid * fee + edge + vol_extra, tick * max(2.0, BASE_SPREAD_TICKS))

    def sell_floor(self) -> float:
        """Maliyet + round-trip fee + net edge — altında SAT yok."""
        if self.inv_qty <= 1e-12:
            return 0.0
        avg = self.inv_cost / self.inv_qty
        fee = max(self.ex.maker(self.symbol), MAKER_FEE) * FEE_SAFETY * 2.0
        return avg * (1.0 + fee + MIN_SELL_EDGE_BPS / 10000.0)

    def equity(self) -> float:
        mid = self.book.mid
        open_mtm = (self.inv_qty * mid - self.inv_cost) if self.inv_qty > 0 and mid > 0 else 0.0
        return self.realized + open_mtm

    def has_inventory(self) -> bool:
        mid = self.book.mid or 0.0
        _, min_cost = self.ex.limits(self.symbol)
        return self.base_total * mid >= min_cost * 0.9 or self.inv_qty > 0

    def seed_inventory(self) -> None:
        if self.seeded_inv or self.book.mid <= 0:
            return
        qty = max(0.0, self.base_total)
        self.inv_qty = qty
        self.inv_cost = qty * self.book.mid
        self.seeded_inv = True
        self.peak_equity = max(self.peak_equity, self.equity())

    async def bal(self) -> None:
        now = time.time()
        if now - self.last_bal_poll < 5.0:
            return
        self.last_bal_poll = now
        b = await self.ex.balance()
        if not b:
            return
        self.base_free = self.ex.free(b, self.base)
        self.quote_free = self.ex.free(b, self.quote_asset)
        self.base_total = self.ex.total(b, self.base)

    async def prime_fills(self) -> None:
        """Eski trade'leri saymadan işaretle — açılışta sahte AL/SAT + WR çökmesi olmasın."""
        for t in await self.ex.trades(self.symbol, 20):
            tid = str(t.get("id") or "")
            if not tid:
                continue
            self.seen.add(tid)
            self.last_tid = tid
        self.last_fill_poll = time.time()

    async def fills(self) -> None:
        now = time.time()
        if now - self.last_fill_poll < FILL_POLL_SEC:
            return
        self.last_fill_poll = now
        for t in await self.ex.trades(self.symbol, 12):
            tid = str(t.get("id") or "")
            if not tid or tid in self.seen:
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
            fee_ccy = str(fee_raw.get("currency") or "USDT").upper()
            fee = self.ex.fee_to_usdt(fee_cost, fee_ccy, px, self.base)
            if amt <= 0 or px <= 0:
                continue
            st = self.ex.stats
            st.ensure_today()
            if side == "buy":
                self.inv_cost += amt * px + fee
                self.inv_qty += amt
                st.buys += 1
                self.last_buy_fill_ts = time.time()
                say_buy(f"{self.symbol} FILL {amt:.6g} @ {px:.8g}")
            else:
                if self.inv_qty > 1e-12:
                    avg = self.inv_cost / self.inv_qty
                    used = min(amt, self.inv_qty)
                    cost = avg * used
                    self.inv_cost = max(0.0, self.inv_cost - cost)
                    self.inv_qty = max(0.0, self.inv_qty - used)
                    extra = amt - used
                    if extra > 1e-12:
                        cost += extra * (self.book.mid or px)
                else:
                    cost = amt * (self.book.mid or px)
                pnl = amt * px - fee - cost
                self.realized += pnl
                if pnl >= 0:
                    st.won += abs(pnl)
                    st.win_trades += 1
                    self.loss_streak = 0
                else:
                    st.lost += abs(pnl)
                    st.loss_trades += 1
                    self.loss_streak += 1
                    if self.loss_streak >= TOXIC_LOSS_STREAK:
                        self.toxic_until = time.time() + TOXIC_PAUSE_SEC
                        log.warning(
                            "%s TOXIC pause %ds (loss_streak=%d)",
                            self.symbol, int(TOXIC_PAUSE_SEC), self.loss_streak,
                        )
                st.sells += 1
                say_sell(f"{self.symbol} FILL {amt:.6g} @ {px:.8g} pnl={pnl:+.6f} USDT")
            st.fees += abs(fee)
            st.save()
            self.peak_equity = max(self.peak_equity, self.equity())
            await self.ex.balance(force=False)
            self.last_bal_poll = 0.0
            await self.bal()

    def risk_ok(self) -> bool:
        eq = self.equity()
        self.peak_equity = max(self.peak_equity, eq, 0.0)
        dd = self.peak_equity - eq
        if self.slot_quote > 0 and dd > self.slot_quote * MAX_DRAWDOWN_RATIO:
            log.error("%s KILL DD %.5f (eq=%.5f)", self.symbol, dd, eq)
            self.kill = True
            return False
        return True

    def quotes(self) -> Optional[Tuple[float, float, float, float]]:
        mid = self.book.mid
        if mid <= 0:
            return None
        nat_bps = 0.0
        if self.book.bid > 0 and self.book.ask > self.book.bid:
            nat_bps = (self.book.ask - self.book.bid) / mid * 10000.0
            # Sadece aşırı geniş book'u atla — dar book'a izin (MIN_BOOK gevşek)
            if nat_bps > MAX_BOOK_SPREAD_BPS * 1.35:
                return None

        tick = self.ex.tick(self.symbol)
        half = self.min_full_spread() / 2.0
        half = min(half, mid * (MAX_HALF_SPREAD_BPS / 10000.0))
        ip = self.inv_pos()
        # Inventory skew (pro MM): fazla base → bid↓ ask↓ (satmaya zorla)
        skew = max(-half * 0.55, min(half * 0.55, -ip * half * SKEW_STRENGTH))
        bid = mid - half + skew
        ask = mid + half + skew
        if ask - bid < self.min_full_spread():
            half = self.min_full_spread() / 2.0
            bid, ask = mid - half + skew * 0.4, mid + half + skew * 0.4
        bid = self.ex.px(self.symbol, bid)
        ask = self.ex.px(self.symbol, ask)

        # Best'e yapışma — BEHIND_TICKS geride (adverse selection ↓)
        behind = max(1.0, BEHIND_TICKS) * tick
        if self.book.bid > 0 and bid >= self.book.bid - behind + tick * 0.5:
            bid = self.ex.px(self.symbol, self.book.bid - behind)
        if self.book.ask > 0 and ask <= self.book.ask + behind - tick * 0.5:
            ask = self.ex.px(self.symbol, self.book.ask + behind)

        min_qty, min_cost = self.ex.limits(self.symbol)
        sell_base = max(self.base_free, 0.0)
        floor = self.sell_floor()
        if floor > 0:
            ask = self.ex.px(self.symbol, max(ask, floor))
            if self.book.ask > 0 and ask <= self.book.ask:
                ask = self.ex.px(self.symbol, max(floor, self.book.ask + behind))

        if ask <= bid or ask - bid < self.min_full_spread() * 0.95:
            if sell_base * mid < min_cost * 0.95 and floor <= 0:
                return None
            ask = self.ex.px(self.symbol, max(ask, floor, (self.book.ask + behind) if self.book.ask else mid * 1.002))
            bid = self.ex.px(self.symbol, ask - self.min_full_spread())
            if bid >= ask:
                return None

        # Slot bütçesi — USDT
        alloc = max(self.slot_quote, MIN_QUOTE_FREE)
        inv_usdt = self.base_total * mid
        buy_budget = max(0.0, alloc * MAX_INVENTORY_RATIO - inv_usdt)
        free_left = self.ex.free_quote_for(self.symbol)
        buy_budget = min(buy_budget, free_left, max(0.0, alloc - inv_usdt))
        min_need = min_cost if min_cost > 0 else MIN_USDT_PER_SLOT
        if buy_budget < min_need:
            buy_budget = 0.0

        # Envanter yok = flat → AL kilidi uygulanmaz (yoksa Binance'te emir görünmez)
        flat = self.inv_qty <= 1e-12 and inv_usdt < min_need * 0.9
        now = time.time()
        mom = self.mid_momentum_bps()
        skip_reason = ""
        if not self.ex.stats.allow_buys(flat=flat):
            buy_budget = 0.0
            skip_reason = "WR/lead SADECE-SAT"
        if self.is_toxic():
            buy_budget = 0.0
            skip_reason = skip_reason or "toxic"
        if mom < MOMENTUM_BUY_BPS:
            buy_budget = 0.0
            skip_reason = skip_reason or "momentum"
        if self.last_buy_fill_ts > 0 and now - self.last_buy_fill_ts < SAME_COIN_BUY_SEC:
            buy_budget = 0.0
            skip_reason = skip_reason or "1dk aynı coin"
        if inv_usdt >= alloc * MAX_INVENTORY_RATIO or ip > 0.35:
            buy_budget = 0.0
            skip_reason = skip_reason or "inv full"
        if floor > 0 and ask <= bid:
            buy_budget = 0.0
            bid = self.ex.px(self.symbol, max(tick, ask - self.min_full_spread()))

        bid_sz = (buy_budget / bid) if (buy_budget > 0 and bid > 0) else 0.0
        ask_sz = sell_base * 0.995 if sell_base * (ask or mid) >= min_cost else 0.0
        if floor > 0 and ask + 1e-15 < floor:
            ask_sz = 0.0

        bid_sz = self.ex.amt(self.symbol, bid_sz)
        ask_sz = self.ex.amt(self.symbol, ask_sz)
        if bid <= 0 or bid_sz * bid < min_cost or bid_sz < min_qty:
            if buy_budget > 0 and not skip_reason:
                skip_reason = f"min_notional(need≈{min_need:.2f} budget≈{buy_budget:.2f})"
            bid_sz = 0.0
        if ask <= 0 or ask_sz * ask < min_cost or ask_sz < min_qty:
            ask_sz = 0.0
        self.ex.set_buy_reserve(self.symbol, (bid_sz * bid) if bid_sz > 0 and bid > 0 else 0.0)
        if bid_sz <= 0 and ask_sz <= 0:
            last = self.ex._quote_skip_log.get(self.symbol, 0.0)
            if now - last > 90.0:
                self.ex._quote_skip_log[self.symbol] = now
                log.info(
                    "%s emir yok | flat=%s alloc=%.2f free=%.2f | %s",
                    self.symbol, flat, alloc, free_left, skip_reason or "bütçe/min yetersiz",
                )
            return None
        return bid, ask, bid_sz, ask_sz

    def needs_replace(self, bid: float, ask: float, bsz: float, asz: float) -> bool:
        now = time.time()
        if self.last_quote <= 0:
            return True
        age = now - self.last_quote
        mid = self.book.mid
        moved = 0.0
        if self.last_mid_quoted > 0 and mid > 0:
            moved = abs(mid - self.last_mid_quoted) / self.last_mid_quoted * 10000.0
        px_drift = False
        if self.last_bid > 0 and bid > 0:
            px_drift = abs(bid - self.last_bid) / self.last_bid * 10000.0 >= QUOTE_MOVE_BPS
        if self.last_ask > 0 and ask > 0:
            px_drift = px_drift or abs(ask - self.last_ask) / self.last_ask * 10000.0 >= QUOTE_MOVE_BPS
        side_flip = (self.last_bid_sz > 0) != (bsz > 0) or (self.last_ask_sz > 0) != (asz > 0)
        if age < REPLACE_SEC and moved < QUOTE_MOVE_BPS and not side_flip:
            return False
        if age >= REPLACE_SEC * HOLD_QUOTE_MULT:
            return True
        return moved >= QUOTE_MOVE_BPS or px_drift or side_flip

    async def sync_orders(self, bid: float, ask: float, bsz: float, asz: float) -> None:
        if not self.needs_replace(bid, ask, bsz, asz):
            return
        await self.ex.cancel_all(self.symbol)
        await asyncio.sleep(0.2)
        if bsz > 0 and bid > 0:
            await self.ex.place(self.symbol, "buy", bsz, bid)
            await asyncio.sleep(0.12)
        if asz > 0 and ask > 0:
            await self.ex.place(self.symbol, "sell", asz, ask)
        self.last_quote = time.time()
        self.last_mid_quoted = self.book.mid
        self.last_bid, self.last_ask = bid, ask
        self.last_bid_sz, self.last_ask_sz = bsz, asz

    async def flatten_and_stop(self) -> None:
        self.running = False
        await self.ex.cancel_all(self.symbol)
        await self.bal()
        # Kill'de zararına market yok — sadece kârlı maker ask
        mid = self.book.mid
        tick = self.ex.tick(self.symbol)
        min_qty, min_cost = self.ex.limits(self.symbol)
        avg = (self.inv_cost / self.inv_qty) if self.inv_qty > 1e-12 else mid
        floor = self.sell_floor() or (
            (avg or mid or 0) * (1.0 + MAKER_FEE * FEE_SAFETY * 2.0 + MIN_SELL_EDGE_BPS / 10000.0)
        )
        if self.base_free > 0 and mid > 0:
            behind = max(1.0, BEHIND_TICKS) * tick
            px = self.ex.px(
                self.symbol,
                max(floor, (self.book.ask + behind) if self.book.ask > 0 else mid * 1.002),
            )
            amt = self.ex.amt(self.symbol, self.base_free * 0.995)
            if amt >= min_qty and amt * px >= min_cost and px >= floor:
                await self.ex.place(self.symbol, "sell", amt, px)
        log.info("%s durdu eq=%.6f realized=%.6f", self.symbol, self.equity(), self.realized)

    async def loop(self) -> None:
        last_rest = 0.0
        last_print = 0.0
        try:
            await self.bal()
            await self.prime_fills()  # eski fill'leri yok say
            while self.running and not self.kill and not self.ex.stopped:
                await wait_if_banned()
                ob = None
                now = time.time()
                if self.ex.ws and USE_WS:
                    try:
                        ob = await asyncio.wait_for(self.ex.watch_book(self.symbol), timeout=8.0)
                    except Exception:
                        ob = None
                if ob is None and now - last_rest >= BOOK_REST_SEC:
                    ob = await self.ex.book_rest(self.symbol)
                    last_rest = now
                if ob:
                    self.on_book(ob)
                if self.book.mid <= 0:
                    await asyncio.sleep(1.0)
                    continue
                self.seed_inventory()
                await self.bal()
                await self.fills()
                if not self.risk_ok():
                    break
                q = self.quotes()
                if q:
                    await self.sync_orders(*q)
                if now - last_print > 90:
                    last_print = now
                    log.info(
                        "%s mid=%.8g inv=%.4fUSDT eq=%+.5f",
                        self.symbol, self.book.mid, self.base_total * self.book.mid, self.equity(),
                    )
                await asyncio.sleep(LOOP_SLEEP_SEC)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("%s loop: %s", self.symbol, e)
            if ban_until_ms(e):
                await sleep_ban(e)
        finally:
            await self.flatten_and_stop()


class Engine:
    def __init__(self, ex: Exchange):
        self.ex = ex
        self.slots: Dict[str, Slot] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.banned: Set[str] = set()
        self.cooldown: Dict[str, float] = {}  # sym -> exit time

    async def refresh_universe(self) -> None:
        tickers = await self.ex.tickers()
        if not tickers:
            log.warning("ticker boş")
            return
        fx = usdt_fx(tickers)
        self.ex._bnb_usd = float(fx.get("BNB") or 0.0)  # fee çevirisi
        bal = await self.ex.balance(force=True)
        spend_usdt = self.ex.deployable_usdt(bal)
        if spend_usdt < MIN_USDT_PER_SLOT:
            log.error("bakiye yetersiz USDT≈%.2f (min slot≈%.1f)", spend_usdt, MIN_USDT_PER_SLOT)
            return

        now = time.time()
        keep: Set[str] = set()
        force_out: Set[str] = set()
        for sym, sl in list(self.slots.items()):
            age = now - sl.opened_at
            if sl.has_inventory() and not sl.kill:
                keep.add(sym)
            elif age < KEEP_GRACE_SEC:
                keep.add(sym)
            elif age >= MAX_PAIR_HOLD_SEC and not sl.has_inventory():
                force_out.add(sym)

        # Kaç slot gerçekten emir koyabilir? (Binance min notional ≈ MIN_USDT_PER_SLOT)
        fundable = max(0, int(spend_usdt / MIN_USDT_PER_SLOT))
        if FORCE_MIN_OPEN:
            if fundable < MIN_OPEN:
                log.error(
                    "USDT≈%.2f → fonlanabilir %d <15 — emir görünsün diye %d slot "
                    "(≥15 için ≈%.0f USDT lazım)",
                    spend_usdt, fundable, max(1, fundable), MIN_OPEN * MIN_USDT_PER_SLOT,
                )
                want_n = max(1, fundable)
            else:
                want_n = min(MAX_OPEN, max(MIN_OPEN, fundable))
        else:
            want_n = min(MAX_OPEN, max(1, fundable))
        log.info("bakiye USDT≈%.2f | hedef_slot=%d (fundable=%d)", spend_usdt, want_n, fundable)

        picked, scanned, pool_n = await pick_open_pairs(
            self.ex, tickers, want_n, keep=keep, cooldown=self.cooldown
        )
        picked = [s for s in picked if s.endswith("/USDT")]
        picked = [s for s in picked if s not in force_out or s in keep]
        if len(picked) < want_n:
            more, _, _ = await pick_open_pairs(self.ex, tickers, want_n + 30, keep=keep, cooldown=self.cooldown)
            for s in more:
                if not s.endswith("/USDT"):
                    continue
                if s not in picked and s not in force_out and s not in self.banned:
                    picked.append(s)
                if len(picked) >= want_n:
                    break
        # Pad: tüm likit */USDT — ≥15 kesin
        if len(picked) < MIN_OPEN:
            for _qv, sym in all_trade_pairs(self.ex, tickers):
                if sym in picked or sym in self.banned or (sym in force_out and sym not in keep):
                    continue
                picked.append(sym)
                if len(picked) >= MIN_OPEN:
                    break

        picked = [s for s in picked if s not in self.banned and s.endswith("/USDT")][:MAX_OPEN]
        # Bakiye az olsa bile MIN_OPEN'ı koru (slot_budget otomatik incelir)
        if FORCE_MIN_OPEN and len(picked) < MIN_OPEN:
            log.error("odak %d < MIN_OPEN=%d — USDT market/pad yetersiz", len(picked), MIN_OPEN)
        elif not FORCE_MIN_OPEN and fundable > 0 and len(picked) > fundable:
            keep_syms = [s for s in picked if s in keep]
            extra = [s for s in picked if s not in keep]
            picked = (keep_syms + extra)[: max(fundable, len(keep_syms))]

        self.ex.n_pairs = max(1, len(picked))
        self.ex._buy_reserved = {s: v for s, v in self.ex._buy_reserved.items() if s in picked}
        current, target = set(self.slots), set(picked)

        for sym in (current - target) | force_out:
            if sym in keep and sym not in force_out:
                continue
            sl = self.slots.get(sym)
            if sl and sl.has_inventory() and sym not in force_out:
                continue
            log.info("rotasyon çıkış %s (hold/skor)", sym)
            self.cooldown[sym] = now
            if sl:
                sl.running = False
            t = self.tasks.pop(sym, None)
            if t:
                t.cancel()
            self.slots.pop(sym, None)

        for i, sym in enumerate(picked):
            budget = self.ex.slot_budget(bal, sym)
            if sym in self.slots:
                self.slots[sym].slot_quote = budget
                continue
            sl = Slot(ex=self.ex, symbol=sym, slot_quote=budget)
            self.slots[sym] = sl
            self.tasks[sym] = asyncio.create_task(self._boot(sl, i * WORKER_STAGGER_SEC))

        live = ", ".join(s.replace("/USDT", "") for s in self.slots)
        log.info(
            "ODAK %d/%d | taranan≈%d havuz=%d | USDT_slot=%d → %s",
            len(self.slots),
            MAX_OPEN,
            scanned,
            pool_n,
            self.ex.n_pairs,
            live,
        )

    async def _boot(self, sl: Slot, delay: float) -> None:
        try:
            if delay:
                await asyncio.sleep(delay)
            await sl.loop()
        except asyncio.CancelledError:
            sl.running = False
            try:
                await sl.ex.cancel_all(sl.symbol)
            except Exception:
                pass
        finally:
            if sl.kill:
                self.banned.add(sl.symbol)
            self.slots.pop(sl.symbol, None)
            self.tasks.pop(sl.symbol, None)

    async def harvest(self) -> None:
        for s in [s for s, t in self.tasks.items() if t.done()]:
            self.tasks.pop(s, None)
            sl = self.slots.pop(s, None)
            if sl and sl.kill:
                self.banned.add(s)

    async def shutdown(self) -> None:
        self.ex.stopped = True
        for sl in self.slots.values():
            sl.running = False
        for t in self.tasks.values():
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for sl in list(self.slots.values()):
            try:
                await sl.ex.cancel_all(sl.symbol)
            except Exception:
                pass
        self.ex.stats.print_summary()
        self.ex.stats.save()
        await self.ex.close()


async def main() -> None:
    key, secret = resolve_keys()
    ex = Exchange(key, secret)
    await ex.init()
    eng = Engine(ex)
    stop = asyncio.Event()

    def _stop(*_a):
        stop.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                pass
    except Exception:
        pass

    last_scan = last_stat = 0.0
    try:
        while not stop.is_set() and not ex.stopped:
            now = time.time()
            if now - last_scan >= SCAN_SEC or not eng.slots:
                await eng.refresh_universe()
                last_scan = now
            await eng.harvest()
            if now - last_stat >= 45:
                ex.stats.print_live()
                last_stat = now
            try:
                await asyncio.wait_for(stop.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("kapanış…")
        await eng.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
