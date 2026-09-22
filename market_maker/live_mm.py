#!/usr/bin/env python3
"""
Binance Spot — BNB · 50 coin tarama · min 15 AL/SAT · CANLI

Her tur: yükselen / düşen / hacim oynaklığı → 50 arama → en az 15 coine dağıt.
SCAN=65s FAST=3s | Pay fees with BNB AÇIK
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import ccxt

# =============================================================================
BINANCE_API_KEY = "BURAYA_API_KEY"
BINANCE_API_SECRET = "BURAYA_SECRET_KEY"
# =============================================================================

QUOTE = "BNB"
SCAN_POOL = 50                  # her turda 50 coin ara (iniş/çıkış/hacim)
TOP_N = 15                      # en az 15 coine al/sat dağıt
MIN_OPEN_COINS = 15
SCAN_SEC = 65.0
FAST_SEC = 3.0
BUY_COOLDOWN_SEC = 480.0
SELL_COOLDOWN_SEC = 25.0

MAKER_FEE = 0.00075
TAKER_FEE = 0.00075
FEE_SAFETY = 1.6
MIN_EDGE_BPS = 40.0

DIP_BPS = 18.0
MIN_QUOTE_VOL = 0.5
CANDIDATE_POOL = 200
MIN_QUOTE_FREE = 0.003
RESERVE_BNB = 0.002
MAX_DRAWDOWN_QUOTE = 0.35
MAX_SPREAD_BPS = 35.0
MAX_SPREAD_FORCE_BPS = 55.0
ALLOW_TAKER_TO_FILL = True
PENDING_MAX_SEC = 12.0
FORCE_MARKET_UNDER_MIN = True
DEPLOY_ALL_BNB = True
SELL_USE_MARKET = False

RSI_PERIOD = 14
RSI_OVERSOLD = 48.0
RSI_MAX_BUY = 58.0
RSI_EXIT = 62.0
EMA_FAST = 7
EMA_SLOW = 21
MIN_METHODS_PASS = 2
MIN_METHODS_FORCE = 1
KLINE_TF = "1m"
KLINE_LIMIT = 60

BALANCE_CACHE_SEC = 20.0
POST_ONLY = True

SKIP_BASES = {
    "BNB", "USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDE", "USD1",
    "EUR", "TRY", "BRL", "AEUR",
}

# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("live_mm")

# ANSI renkler (Windows VT destekli terminallerde de çalışır)
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


def est_fee(notional: float) -> float:
    """Quote cinsinden tahmini maker komisyon."""
    return abs(notional) * MAKER_FEE


STATS_PATH = Path(__file__).resolve().parent.parent / "output" / "live_mm_day_stats.json"


@dataclass
class DayStats:
    day: str = field(default_factory=utc_day)
    orders: int = 0
    buys: int = 0
    sells: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    won_bnb: float = 0.0
    lost_bnb: float = 0.0
    fees_bnb: float = 0.0
    started_at: float = field(default_factory=time.time)

    def ensure_today(self) -> None:
        d = utc_day()
        if d != self.day:
            self.day = d
            self.orders = self.buys = self.sells = 0
            self.win_trades = self.loss_trades = 0
            self.won_bnb = self.lost_bnb = self.fees_bnb = 0.0
            self.started_at = time.time()

    def net(self) -> float:
        return self.won_bnb - self.lost_bnb - self.fees_bnb

    def summary_lines(self) -> List[str]:
        self.ensure_today()
        mins = max(0.1, (time.time() - self.started_at) / 60.0)
        net = self.net()
        net_c = _GREEN if net >= 0 else _RED
        lines = [
            "=" * 64,
            _c(_BOLD, f"GÜNLÜK ÖZET  ({self.day} UTC)"),
            "-" * 64,
            f"  İşlem (roundtrip SAT): {_c(_BOLD, str(self.sells))}",
            f"  Emir sayısı          : {_c(_ORANGE, str(self.orders))}",
            f"  Alış                 : {_c(_GREEN, str(self.buys))}",
            f"  Satış                : {_c(_RED, str(self.sells))}",
            f"  Kazanan işlem        : {_c(_GREEN, str(self.win_trades))}",
            f"  Kaybeden işlem       : {_c(_RED, str(self.loss_trades))}",
            f"  Kazanç (brüt)        : {_c(_GREEN, f'+{self.won_bnb:.6f} {QUOTE}')}",
            f"  Kayıp (brüt)         : {_c(_RED, f'-{self.lost_bnb:.6f} {QUOTE}')}",
            f"  Komisyon (tahmini)   : {_c(_ORANGE, f'{self.fees_bnb:.6f} {QUOTE}')}  (maker≈{MAKER_FEE*100:.3f}%)",
            f"  Net (kazanç-kayıp-fee): {_c(net_c, f'{net:+.6f} {QUOTE}')}",
            f"  Süre                 : {mins:.1f} dk",
            "=" * 64,
        ]
        return lines

    def print_summary(self) -> None:
        for line in self.summary_lines():
            print(line, flush=True)

    def print_live(self) -> None:
        """Her turda tek satır canlı bilanço."""
        self.ensure_today()
        net = self.net()
        net_c = _GREEN if net >= 0 else _RED
        print(
            _c(_DIM, "── bilanço ── ")
            + f"işlem={self.sells} "
            + _c(_GREEN, f"kazanç=+{self.won_bnb:.5f}")
            + " "
            + _c(_RED, f"kayıp=-{self.lost_bnb:.5f}")
            + " "
            + _c(_ORANGE, f"fee={self.fees_bnb:.5f}")
            + " "
            + _c(net_c, f"net={net:+.5f} {QUOTE}")
            + f" | AL={self.buys} SAT={self.sells} emir={self.orders}",
            flush=True,
        )

    def save(self) -> None:
        try:
            STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = asdict(self)
            payload["net_bnb"] = self.net()
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            STATS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("stats kaydedilemedi: %s", e)

    @classmethod
    def load(cls) -> "DayStats":
        try:
            if STATS_PATH.exists():
                raw = json.loads(STATS_PATH.read_text(encoding="utf-8"))
                st = cls(
                    day=str(raw.get("day") or utc_day()),
                    orders=int(raw.get("orders") or 0),
                    buys=int(raw.get("buys") or 0),
                    sells=int(raw.get("sells") or 0),
                    win_trades=int(raw.get("win_trades") or 0),
                    loss_trades=int(raw.get("loss_trades") or 0),
                    won_bnb=float(raw.get("won_bnb") or 0),
                    lost_bnb=float(raw.get("lost_bnb") or 0),
                    fees_bnb=float(raw.get("fees_bnb") or 0),
                    started_at=float(raw.get("started_at") or time.time()),
                )
                st.ensure_today()
                return st
        except Exception as e:
            log.warning("stats okunamadı: %s", e)
        return cls()


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
    return ("x-BNB20M" + uuid.uuid4().hex)[:32]


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


def roundtrip_fee_bps() -> float:
    return (MAKER_FEE + MAKER_FEE) * FEE_SAFETY * 10000.0


def min_rise_bps() -> float:
    """Komisyona ezilmeden SAT eşiği."""
    return roundtrip_fee_bps() + MIN_EDGE_BPS


def rsi_wilder(closes: List[float], period: int = RSI_PERIOD) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l <= 1e-12:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def ema(series: List[float], period: int) -> Optional[float]:
    if len(series) < period:
        return None
    k = 2.0 / (period + 1)
    v = sum(series[:period]) / period
    for x in series[period:]:
        v = x * k + v * (1 - k)
    return v


# ---- State ----

@dataclass
class Pos:
    symbol: str
    entry: float
    qty: float
    bought_at: float
    methods: str = ""


@dataclass
class SignalReport:
    symbol: str
    score: float
    passed: int
    total: int
    reasons: List[str]
    qv: float
    last: float
    bid: float
    ask: float
    mid: float
    rsi: Optional[float] = None


@dataclass
class PendingBuy:
    symbol: str
    order_id: Optional[str]
    price: float
    qty: float
    budget: float
    placed_at: float
    methods: str = ""


@dataclass
class State:
    positions: Dict[str, Pos] = field(default_factory=dict)
    pending: Dict[str, PendingBuy] = field(default_factory=dict)
    buy_block_until: Dict[str, float] = field(default_factory=dict)
    sell_block_until: Dict[str, float] = field(default_factory=dict)
    recently_scanned: List[str] = field(default_factory=list)
    realized_bnb: float = 0.0
    kill: bool = False
    watchlist: List[str] = field(default_factory=list)
    stats: DayStats = field(default_factory=DayStats)


class Exchange:
    def __init__(self, key: str, secret: str):
        opts = {
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "rateLimit": 280,
            "options": {"defaultType": "spot", "adjustForTimeDifference": True},
        }
        self.rest = ccxt.binance(opts)
        self.rest.set_sandbox_mode(False)
        self._bal: Optional[dict] = None
        self._bal_ts = 0.0
        self._bal_lock = asyncio.Lock()
        self._order_lock = asyncio.Lock()
        self._kl_cache: Dict[str, Tuple[float, list]] = {}

    async def run(self, fn, *a, **kw):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*a, **kw))

    async def init(self) -> None:
        while True:
            try:
                await self.run(self.rest.load_markets)
                log.info("CANLI | markets=%d | quote=%s | min_rise=%.1fbps",
                         len(self.rest.markets), QUOTE, min_rise_bps())
                return
            except Exception as e:
                if ban_until_ms(e):
                    await sleep_ban(e)
                    continue
                raise

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

    async def tickers(self) -> Dict[str, dict]:
        try:
            data = await self.run(self.rest.fetch_tickers)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            log.error("tickers: %s", e)
            return {}

    async def book(self, symbol: str) -> Optional[dict]:
        try:
            return await self.run(self.rest.fetch_order_book, symbol, 5)
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return None

    async def book_ticker(self, symbol: str) -> Optional[Tuple[float, float]]:
        """Binance bookTicker — en hızlı bid/ask."""
        try:
            m = self.rest.market(symbol)
            raw = await self.run(
                self.rest.publicGetTickerBookTicker,
                {"symbol": m["id"]},
            )
            return float(raw["bidPrice"]), float(raw["askPrice"])
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return None

    async def avg_price(self, symbol: str) -> Optional[float]:
        try:
            m = self.rest.market(symbol)
            raw = await self.run(self.rest.publicGetAvgPrice, {"symbol": m["id"]})
            return float(raw.get("price") or 0) or None
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return None

    async def klines(self, symbol: str, tf: str = KLINE_TF, limit: int = KLINE_LIMIT) -> list:
        now = time.time()
        hit = self._kl_cache.get(symbol)
        if hit and now - hit[0] < 50:
            return hit[1]
        try:
            rows = await self.run(self.rest.fetch_ohlcv, symbol, tf, None, limit)
            self._kl_cache[symbol] = (now, rows or [])
            return rows or []
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return []

    async def recent_trades(self, symbol: str, limit: int = 30) -> list:
        try:
            return await self.run(self.rest.fetch_trades, symbol, None, limit) or []
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return []

    def free(self, bal: dict, asset: str) -> float:
        return float((bal.get("free") or {}).get(asset, 0) or 0)

    def total(self, bal: dict, asset: str) -> float:
        return float((bal.get("total") or {}).get(asset, 0) or 0)

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
            float((lim.get("cost") or {}).get("min") or 0.001),
        )

    def tick(self, symbol: str) -> float:
        m = self.rest.markets.get(symbol) or {}
        p = (m.get("precision") or {}).get("price")
        if isinstance(p, int):
            return 10 ** (-p)
        if p:
            return float(p)
        return 1e-6

    async def place_fast(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        stats: Optional[DayStats] = None,
    ) -> Optional[dict]:
        """Emir hızı için kısa yol — gereksiz bekleme yok."""
        amount = self.amt(symbol, amount)
        price = self.px(symbol, price)
        if amount <= 0 or price <= 0:
            return None
        min_qty, min_cost = self.limits(symbol)
        if amount < min_qty or amount * price < min_cost:
            return None

        async with self._order_lock:
            try:
                otype = "LIMIT_MAKER" if POST_ONLY else "limit"
                params: Dict[str, Any] = {"newClientOrderId": coid()}
                say_order(f"{side.upper()} {symbol} qty={amount:.8f} @ {price:.8f}")
                o = await self.run(
                    self.rest.create_order, symbol, otype, side, amount, price, params
                )
                if stats is not None:
                    stats.ensure_today()
                    stats.orders += 1
                    stats.fees_bnb += est_fee(amount * price)
                log.info("EMİR %s %s qty=%.8f @ %.8f id=%s", side.upper(), symbol, amount, price, o.get("id"))
                return o
            except Exception as e:
                msg = str(e)
                if ban_until_ms(e):
                    await sleep_ban(e)
                    return None
                if any(x in msg for x in ("Post Only", "-5022", "would immediately", "Order would")):
                    try:
                        say_order(f"RETRY {side.upper()} {symbol} @ {price:.8f}")
                        o = await self.run(
                            self.rest.create_order,
                            symbol,
                            "limit",
                            side,
                            amount,
                            price,
                            {"newClientOrderId": coid(), "postOnly": True},
                        )
                        if stats is not None:
                            stats.ensure_today()
                            stats.orders += 1
                            stats.fees_bnb += est_fee(amount * price)
                        log.info("EMİR(retry) %s %s @ %.8f id=%s", side.upper(), symbol, price, o.get("id"))
                        return o
                    except Exception as e2:
                        log.warning("post-only fail %s %s: %s", side, symbol, e2)
                        return None
                log.error("order %s %s: %s", side, symbol, e)
                return None

    async def place_market_buy(
        self,
        symbol: str,
        quote_bnb: float,
        stats: Optional[DayStats] = None,
    ) -> Optional[dict]:
        """Hızlı doldurma — min 5 için taker AL (quoteOrderQty)."""
        quote_bnb = float(f"{quote_bnb:.8f}")
        min_qty, min_cost = self.limits(symbol)
        if quote_bnb < max(min_cost, MIN_QUOTE_FREE * 0.5):
            return None
        async with self._order_lock:
            try:
                say_order(f"MARKET BUY {symbol} ~{quote_bnb:.4f} USDT")
                o = await self.run(
                    self.rest.create_order,
                    symbol,
                    "market",
                    "buy",
                    None,
                    None,
                    {"quoteOrderQty": quote_bnb, "newClientOrderId": coid()},
                )
                if stats is not None:
                    stats.ensure_today()
                    stats.orders += 1
                    stats.fees_bnb += est_fee(quote_bnb) * (TAKER_FEE / MAKER_FEE)
                log.info("MARKET BUY %s quote=%.4f id=%s", symbol, quote_bnb, o.get("id"))
                return o
            except Exception as e:
                # bazı sembollerde quoteOrderQty olmayabilir → amount ile dene
                try:
                    bt = await self.book_ticker(symbol)
                    if not bt:
                        raise e
                    ask = bt[1]
                    qty = self.amt(symbol, (quote_bnb * 0.98) / ask)
                    if qty < min_qty:
                        raise e
                    say_order(f"MARKET BUY {symbol} qty={qty}")
                    o = await self.run(
                        self.rest.create_order,
                        symbol,
                        "market",
                        "buy",
                        qty,
                        None,
                        {"newClientOrderId": coid()},
                    )
                    if stats is not None:
                        stats.ensure_today()
                        stats.orders += 1
                        stats.fees_bnb += est_fee(qty * ask) * (TAKER_FEE / MAKER_FEE)
                    return o
                except Exception as e2:
                    if ban_until_ms(e2):
                        await sleep_ban(e2)
                    log.error("market buy %s: %s", symbol, e2)
                    return None

    async def fetch_order(self, symbol: str, order_id: str) -> Optional[dict]:
        try:
            return await self.run(self.rest.fetch_order, order_id, symbol)
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return None

    async def cancel_all(self, symbol: str) -> None:
        async with self._order_lock:
            try:
                if hasattr(self.rest, "cancel_all_orders"):
                    await self.run(self.rest.cancel_all_orders, symbol)
            except Exception as e:
                if ban_until_ms(e):
                    await sleep_ban(e)


def score_ticker(t: dict) -> float:
    """Genel oynaklık skoru: |%| × log(hacim)."""
    try:
        ch = abs(float(t.get("percentage") or 0))
    except Exception:
        ch = 0.0
    qv = float(t.get("quoteVolume") or 0)
    if qv <= 0:
        return 0.0
    return ch * math.log10(qv + 10.0) + (ch ** 1.2) * 0.5


def vol_volatility_score(t: dict) -> float:
    """Hacim oynaklığı: yüksek hacim + geniş high-low aralığı."""
    qv = float(t.get("quoteVolume") or 0)
    last = float(t.get("last") or t.get("close") or 0)
    hi = float(t.get("high") or 0)
    lo = float(t.get("low") or 0)
    if qv <= 0 or last <= 0:
        return 0.0
    rng = 0.0
    if hi > 0 and lo > 0 and hi >= lo:
        rng = (hi - lo) / last * 100.0
    try:
        pct = abs(float(t.get("percentage") or 0))
    except Exception:
        pct = 0.0
    return math.log10(qv + 10.0) * (1.0 + rng) * (1.0 + pct * 0.15)


def _bnb_spot_rows(
    ex: Exchange, tickers: Dict[str, dict]
) -> List[Tuple[str, dict, float, float, float]]:
    """(sym, ticker, qv, last, signed_pct)"""
    out = []
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
        last = float(t.get("last") or t.get("close") or 0)
        if last <= 0:
            continue
        try:
            pct = float(t.get("percentage") or 0)
        except Exception:
            pct = 0.0
        out.append((sym, t, qv, last, pct))
    return out


def pick_movers(
    ex: Exchange,
    tickers: Dict[str, dict],
    state: State,
    n: int = SCAN_POOL,
) -> List[Tuple[str, float, float]]:
    """
    50 arama: yükselen + düşen + hacim oynaklığı + hacim.
    """
    rows = _bnb_spot_rows(ex, tickers)
    if not rows:
        return []

    gainers = sorted(rows, key=lambda r: -r[4])
    losers = sorted(rows, key=lambda r: r[4])
    by_vol = sorted(rows, key=lambda r: -r[2])
    by_vol_vol = sorted(rows, key=lambda r: -vol_volatility_score(r[1]))
    recent = set(state.recently_scanned[-40:])

    picked: List[str] = []
    meta: Dict[str, Tuple[float, float]] = {}
    tags: Dict[str, str] = {}

    def _add(sym: str, qv: float, last: float, tag: str) -> None:
        if sym in picked:
            return
        picked.append(sym)
        meta[sym] = (qv, last)
        tags[sym] = tag

    chunk = max(8, n // 4)
    for sym, _t, qv, last, _p in gainers[:chunk]:
        _add(sym, qv, last, "UP")
    for sym, _t, qv, last, _p in losers[:chunk]:
        _add(sym, qv, last, "DOWN")
    for sym, _t, qv, last, _p in by_vol_vol[:chunk]:
        _add(sym, qv, last, "VOLAT")
    for sym, _t, qv, last, _p in by_vol[:chunk]:
        _add(sym, qv, last, "VOL")

    scored = sorted(
        (
            (score_ticker(t) * (0.6 if sym in recent else 1.0), sym, qv, last)
            for sym, t, qv, last, _ in rows
        ),
        key=lambda x: -x[0],
    )
    for _sc, sym, qv, last in scored:
        if len(picked) >= n:
            break
        _add(sym, qv, last, "SCORE")

    for sym in list(state.positions.keys()) + list(state.pending.keys()):
        if sym not in meta:
            t = tickers.get(sym) or {}
            _add(sym, float(t.get("quoteVolume") or 1), float(t.get("last") or 0), "POS")

    result = [(s, meta[s][0], meta[s][1]) for s in picked[: max(n, len(state.positions))]]
    up_n = sum(1 for s, _, _ in result if tags.get(s) == "UP")
    dn_n = sum(1 for s, _, _ in result if tags.get(s) == "DOWN")
    vv_n = sum(1 for s, _, _ in result if tags.get(s) == "VOLAT")
    log.info(
        "50-arama aday=%d → seçilen=%d | UP=%d DOWN=%d VOLAT=%d",
        len(rows), len(result), up_n, dn_n, vv_n,
    )
    print(
        f"TARAMA{len(result)}: "
        + ", ".join(f"{s.replace(f'/{QUOTE}', '')}:{tags.get(s, '?')}" for s, _, _ in result[:24])
        + (" …" if len(result) > 24 else ""),
        flush=True,
    )
    return result


def volume_weights(items: List[Tuple[str, float, float]]) -> Dict[str, float]:
    logs = {s: math.log10(max(qv, 1.0) + 10.0) for s, qv, _ in items}
    tot = sum(logs.values()) or 1.0
    return {s: logs[s] / tot for s in logs}


async def analyze_symbol(
    ex: Exchange,
    symbol: str,
    ticker: dict,
    qv: float,
) -> Optional[SignalReport]:
    """
    Binance metodları (hepsi):
      1) ticker 24h % / hacim
      2) bookTicker spread + dip
      3) klines → RSI
      4) klines → EMA fast/slow
      5) klines → momentum / pullback from high
      6) avgPrice sapma
      7) recent trades alım baskısı
    """
    reasons: List[str] = []
    passed = 0
    total = 7

    # --- bookTicker (hızlı) ---
    bt = await ex.book_ticker(symbol)
    if not bt:
        ob = await ex.book(symbol)
        if not ob or not ob.get("bids") or not ob.get("asks"):
            return None
        bid, ask = float(ob["bids"][0][0]), float(ob["asks"][0][0])
    else:
        bid, ask = bt
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10000.0
    last = float(ticker.get("last") or mid)

    # fee/spread guard — aşırı genişse ele; orta genişlikte devam (force market)
    if spread_bps > MAX_SPREAD_FORCE_BPS:
        return SignalReport(symbol, 0, 0, total, [f"spread_wide={spread_bps:.0f}"], qv, last, bid, ask, mid)
    spread_ok = spread_bps <= MAX_SPREAD_BPS
    if spread_ok:
        passed += 1
        total += 1
        reasons.append(f"spread={spread_bps:.0f}bps")
    else:
        total += 1
        reasons.append(f"spread_wide={spread_bps:.0f}")

    # 1) ticker % — kırmızı / geri çekilme
    pct = float(ticker.get("percentage") or 0)
    if pct <= -0.5 or (-3.0 <= pct <= 0.8):
        passed += 1
        reasons.append(f"ticker%={pct:+.2f}")
    else:
        reasons.append(f"ticker%_fail={pct:+.2f}")

    # 2) book dip / mid vs ask
    book_dip = (mid - bid) / mid * 10000.0
    hi24 = float(ticker.get("high") or 0)
    pull_bps = ((hi24 - last) / hi24 * 10000.0) if hi24 > 0 else 0.0
    if pull_bps >= DIP_BPS or book_dip >= 2.0:
        passed += 1
        reasons.append(f"dip/pull={max(pull_bps, book_dip):.0f}bps")
    else:
        reasons.append(f"dip_fail={max(pull_bps, book_dip):.0f}")

    # 3-5) klines
    ohlcv = await ex.klines(symbol)
    closes = [float(r[4]) for r in ohlcv] if ohlcv else []
    vols = [float(r[5]) for r in ohlcv] if ohlcv else []
    rsi_v = rsi_wilder(closes) if closes else None
    ema_f = ema(closes, EMA_FAST) if closes else None
    ema_s = ema(closes, EMA_SLOW) if closes else None

    if rsi_v is not None and 1.0 < rsi_v <= RSI_OVERSOLD:
        passed += 1
        reasons.append(f"RSI={rsi_v:.1f}")
    else:
        reasons.append(f"RSI_fail={rsi_v}")

    # HARD VETO: aşırı alımda ALMA
    if rsi_v is not None and rsi_v > RSI_MAX_BUY:
        return SignalReport(
            symbol, 0, 0, total,
            [f"VETO_RSI={rsi_v:.1f}>{RSI_MAX_BUY}"] + reasons,
            qv, last, bid, ask, mid, rsi_v,
        )

    # RSI=0 / 100 sahte sinyal (yetersiz mum) → skorlama dışı
    if rsi_v is not None and (rsi_v <= 1.0 or rsi_v >= 99.0) and len(closes) < RSI_PERIOD + 5:
        rsi_v = None
        reasons.append("RSI_invalid")

    # HARD VETO: güçlü yeşil pump kovalama
    if pct > 8.0:
        return SignalReport(
            symbol, 0, 0, total,
            [f"VETO_pump%={pct:+.2f}"] + reasons,
            qv, last, bid, ask, mid, rsi_v,
        )

    if ema_f is not None and ema_s is not None and ema_f <= ema_s * 1.002 and last <= (ema_f or last):
        # EMA fast altında / death-cross yakın → dip AL
        passed += 1
        reasons.append("EMA_dip")
    else:
        reasons.append("EMA_fail")

    if len(closes) >= 10:
        mom = (closes[-1] - closes[-10]) / closes[-10] * 10000.0
        # kısa momentum negatif (düşüş) ama aşırı dump değil
        if -180 <= mom <= -8:
            passed += 1
            reasons.append(f"mom={mom:.0f}bps")
        else:
            reasons.append(f"mom_fail={mom:.0f}")
    else:
        reasons.append("mom_fail=na")
        total -= 0

    # 6) avgPrice — last ortalamanın altında
    avg = await ex.avg_price(symbol)
    if avg and avg > 0 and last <= avg * (1 - DIP_BPS / 20000.0):
        passed += 1
        reasons.append("avgPrice_below")
    else:
        reasons.append("avgPrice_fail")

    # 7) recent trades — son işlemlerde satış baskısı azalmış / bounce
    trades = await ex.recent_trades(symbol, 25)
    if trades:
        buys = sum(1 for tr in trades if tr.get("side") == "buy")
        sells = len(trades) - buys
        # dip sonrası alım gelmeye başlamış
        if buys >= sells:
            passed += 1
            reasons.append(f"tape_buy={buys}/{len(trades)}")
        else:
            reasons.append(f"tape_sell={sells}/{len(trades)}")
    else:
        reasons.append("tape_fail")

    # hacim spike bonus (skor)
    vol_score = 0.0
    if len(vols) >= 20:
        recent_v = sum(vols[-5:]) / 5
        base_v = sum(vols[-20:-5]) / 15 + 1e-12
        if recent_v > base_v * 1.4:
            vol_score = 1.5
            reasons.append("vol_spike")

    score = passed + vol_score + score_ticker(ticker) * 0.02
    return SignalReport(symbol, score, passed, total, reasons, qv, last, bid, ask, mid, rsi_v)


async def sync_positions(ex: Exchange, state: State, bal: dict) -> None:
    for asset, amt in list((bal.get("total") or {}).items()):
        a = str(asset).upper()
        if a in SKIP_BASES or a == QUOTE:
            continue
        tot = float(amt or 0)
        sym = f"{a}/{QUOTE}"
        if sym not in ex.rest.markets or tot <= 0:
            if sym in state.positions and tot <= 0:
                state.positions.pop(sym, None)
            continue
        if sym not in state.positions:
            state.positions[sym] = Pos(symbol=sym, entry=0.0, qty=tot, bought_at=time.time())
        else:
            state.positions[sym].qty = tot
    for sym in list(state.positions.keys()):
        base = sym.split("/")[0]
        if ex.total(bal, base) <= 0:
            state.positions.pop(sym, None)


async def sell_one(ex: Exchange, state: State, symbol: str) -> bool:
    """Hızlı SAT yolu — FAST loop."""
    pos = state.positions.get(symbol)
    if not pos:
        return False
    now = time.time()
    if now < state.sell_block_until.get(symbol, 0):
        return False

    bt = await ex.book_ticker(symbol)
    if not bt:
        return False
    bid, ask = bt
    if bid <= 0:
        return False

    entry = pos.entry
    if entry <= 0:
        entry = bid  # bilinmiyorsa güncelle, hemen satma
        pos.entry = bid
        return False

    rise = (bid - entry) / entry * 10000.0
    need = min_rise_bps()  # fee*safety + MIN_EDGE — erken/ucuz SAT yok

    ohlcv = await ex.klines(symbol)
    closes = [float(r[4]) for r in ohlcv] if ohlcv else []
    rsi_v = rsi_wilder(closes) if closes else None
    # RSI yüksekse eşiği İNDİRME — komisyona ezilmeyelim; biraz yükselt
    if rsi_v is not None and rsi_v >= RSI_EXIT:
        need = max(need, min_rise_bps())

    if rise < need:
        return False

    # net kâr kontrolü (brüt - 2 taraf fee)
    est_round_fee = entry * qty * MAKER_FEE * 2 * FEE_SAFETY if False else 0.0
    # qty henüz yok — aşağıda tekrar

    tick = ex.tick(symbol)
    price = ex.px(symbol, max(ask, bid + tick))
    if price <= ask:
        price = ex.px(symbol, ask + tick)

    bal = await ex.balance(force=True)
    if not bal:
        return False
    base = symbol.split("/")[0]
    free = ex.free(bal, base)
    qty = ex.amt(symbol, min(free, pos.qty) * 0.99)
    min_qty, min_cost = ex.limits(symbol)
    if qty < min_qty or qty * price < min_cost:
        return False

    gross = (price - entry) * qty
    fee_est = (entry * qty + price * qty) * MAKER_FEE * FEE_SAFETY
    if SELL_USE_MARKET:
        fee_est = (entry * qty + price * qty) * TAKER_FEE * FEE_SAFETY
    if gross <= fee_est:
        log.info(
            "skip SAT %s — brüt %.6f ≤ fee≈%.6f (rise %.1f need %.1f)",
            symbol, gross, fee_est, rise, need,
        )
        return False

    await ex.cancel_all(symbol)
    if SELL_USE_MARKET:
        try:
            say_order(f"MARKET SELL {symbol} qty≈{qty:.6f}")
            o = await ex.run(
                ex.rest.create_order,
                symbol,
                "market",
                "sell",
                qty,
                None,
                {"newClientOrderId": coid()},
            )
            if state.stats is not None:
                state.stats.ensure_today()
                state.stats.orders += 1
                state.stats.fees_bnb += est_fee(qty * bid) * (TAKER_FEE / max(MAKER_FEE, 1e-12))
            log.info("MARKET SELL %s qty=%.6f id=%s", symbol, qty, o.get("id"))
        except Exception as e:
            log.warning("market sell fail → limit: %s", e)
            o = await ex.place_fast(symbol, "sell", qty, price, state.stats)
    else:
        # maker SAT — fee düşük, kazanç oranı toparlanır
        o = await ex.place_fast(symbol, "sell", qty, price, state.stats)
    if not o:
        return False

    fill_px = float(o.get("average") or o.get("price") or price)
    pnl = (fill_px - entry) * qty
    state.realized_bnb += pnl
    state.positions.pop(symbol, None)
    state.sell_block_until[symbol] = now + SELL_COOLDOWN_SEC
    state.buy_block_until[symbol] = now + BUY_COOLDOWN_SEC

    st = state.stats
    st.ensure_today()
    st.sells += 1
    if pnl >= 0:
        st.win_trades += 1
        st.won_bnb += pnl
    else:
        st.loss_trades += 1
        st.lost_bnb += abs(pnl)

    rsi_s = f"{rsi_v:.1f}" if rsi_v is not None else "?"
    net_trade = pnl - fee_est
    say_sell(
        f"{symbol} +{rise:.1f}bps need≥{need:.1f} RSI={rsi_s} "
        f"pnl={pnl:+.6f} fee≈{fee_est:.6f} net≈{net_trade:+.6f} {QUOTE} | bugün SAT={st.sells}"
    )
    st.save()
    log.info("%s SAT +%.1fbps pnl≈%.5f fee≈%.5f %s", symbol, rise, pnl, fee_est, QUOTE)
    return True


async def buy_one(
    ex: Exchange,
    state: State,
    sig: SignalReport,
    budget: float,
    *,
    force_fill: bool = False,
) -> bool:
    now = time.time()
    if sig.symbol in state.positions or sig.symbol in state.pending:
        log.info("skip %s — zaten pozisyon/pending", sig.symbol)
        return False
    if now < state.buy_block_until.get(sig.symbol, 0):
        log.info("skip %s — cooldown", sig.symbol)
        return False
    if budget < MIN_QUOTE_FREE:
        log.info("skip %s — bütçe $%.2f < min $%.2f", sig.symbol, budget, MIN_QUOTE_FREE)
        return False
    if sig.rsi is not None and sig.rsi > RSI_MAX_BUY:
        log.info("skip %s — RSI %.1f", sig.symbol, sig.rsi)
        return False
    spread_bps = (sig.ask - sig.bid) / sig.mid * 10000.0 if sig.mid > 0 else 999
    lim = MAX_SPREAD_FORCE_BPS if force_fill else MAX_SPREAD_BPS
    if spread_bps > lim:
        log.info("skip %s — spread %.0f > %.0f", sig.symbol, spread_bps, lim)
        return False

    need = MIN_METHODS_FORCE if force_fill else MIN_METHODS_PASS
    if sig.passed < need:
        log.info("skip %s — methods %d < %d", sig.symbol, sig.passed, need)
        return False

    # min altında veya force → MARKET (AL yükselsin, fill kesin)
    use_market = ALLOW_TAKER_TO_FILL and (
        (force_fill and FORCE_MARKET_UNDER_MIN)
        or (force_fill and spread_bps > MAX_SPREAD_BPS)
        or (len(state.positions) + len(state.pending) < MIN_OPEN_COINS and FORCE_MARKET_UNDER_MIN)
    )

    methods = ",".join(sig.reasons[:5])
    await ex.cancel_all(sig.symbol)

    if use_market:
        o = await ex.place_market_buy(sig.symbol, budget * 0.96, state.stats)
        if not o:
            log.warning("skip %s — market buy fail (min notional? bud=$%.2f)", sig.symbol, budget)
            return False
        fill_px = float(o.get("average") or o.get("price") or sig.ask or sig.mid)
        filled = float(o.get("filled") or 0)
        if filled <= 0 and fill_px > 0:
            filled = (budget * 0.96) / fill_px
        state.positions[sig.symbol] = Pos(
            symbol=sig.symbol, entry=fill_px, qty=filled, bought_at=now, methods=methods
        )
        state.buy_block_until[sig.symbol] = now + BUY_COOLDOWN_SEC
        st = state.stats
        st.ensure_today()
        st.buys += 1
        say_buy(
            f"{sig.symbol} MARKET @≈{fill_px:.8f} bud=${budget:.2f} | "
            f"methods={sig.passed}/{sig.total} RSI={sig.rsi} | bugün AL={st.buys}"
        )
        st.save()
        return True

    tick = ex.tick(sig.symbol)
    price = ex.px(sig.symbol, max(sig.bid - tick, sig.bid * 0.9995))
    if price <= 0:
        return False
    min_qty, min_cost = ex.limits(sig.symbol)
    qty = ex.amt(sig.symbol, (budget * 0.96) / price)
    if qty < min_qty or qty * price < max(min_cost, MIN_QUOTE_FREE * 0.5):
        log.info(
            "skip %s — min notional (qty=%s cost≈%.2f min=%.2f)",
            sig.symbol, qty, qty * price, min_cost,
        )
        return False

    o = await ex.place_fast(sig.symbol, "buy", qty, price, state.stats)
    if not o:
        log.warning("skip %s — limit emir reddedildi", sig.symbol)
        return False

    oid = str(o.get("id") or "") or None
    state.pending[sig.symbol] = PendingBuy(
        symbol=sig.symbol,
        order_id=oid,
        price=price,
        qty=qty,
        budget=budget,
        placed_at=now,
        methods=methods,
    )
    say_order(f"PENDING BUY {sig.symbol} @ {price:.8f} (≤{PENDING_MAX_SEC:.0f}s)")
    return True


async def manage_pending(ex: Exchange, state: State) -> None:
    """Pending maker fill kontrol — dolmazsa market ile tamamla (min 5)."""
    if not state.pending:
        return
    bal = await ex.balance(force=True)
    if not bal:
        return
    now = time.time()
    for sym in list(state.pending.keys()):
        pb = state.pending[sym]
        base = sym.split("/")[0]
        tot = ex.total(bal, base)
        min_cost = ex.limits(sym)[1]
        # fill oldu mu?
        bt = await ex.book_ticker(sym)
        mid = ((bt[0] + bt[1]) / 2.0) if bt else pb.price
        if tot > 0 and (mid <= 0 or tot * mid >= min_cost * 0.5):
            state.positions[sym] = Pos(
                symbol=sym, entry=pb.price, qty=tot, bought_at=pb.placed_at, methods=pb.methods
            )
            state.pending.pop(sym, None)
            state.buy_block_until[sym] = now + BUY_COOLDOWN_SEC
            st = state.stats
            st.ensure_today()
            st.buys += 1
            say_buy(f"{sym} FILL @≈{pb.price:.8f} qty={tot:.6f} | bugün AL={st.buys}")
            st.save()
            continue

        age = now - pb.placed_at
        if age < PENDING_MAX_SEC:
            continue

        # timeout
        await ex.cancel_all(sym)
        need_force = len(state.positions) < MIN_OPEN_COINS
        if need_force and ALLOW_TAKER_TO_FILL and ex.free(bal, QUOTE) >= MIN_QUOTE_FREE:
            o = await ex.place_market_buy(sym, min(pb.budget, ex.free(bal, QUOTE) * 0.9), state.stats)
            state.pending.pop(sym, None)
            if o:
                fill_px = float(o.get("average") or o.get("price") or pb.price)
                filled = float(o.get("filled") or pb.qty)
                state.positions[sym] = Pos(
                    symbol=sym, entry=fill_px, qty=filled, bought_at=now, methods=pb.methods
                )
                state.buy_block_until[sym] = now + BUY_COOLDOWN_SEC
                st = state.stats
                st.ensure_today()
                st.buys += 1
                say_buy(f"{sym} MARKET-FILL timeout | bugün AL={st.buys}")
                st.save()
            else:
                log.warning("%s pending iptal — market de olmadı", sym)
        else:
            state.pending.pop(sym, None)
            log.info("%s pending timeout iptal", sym)


async def fast_loop(ex: Exchange, state: State) -> None:
    """SAT + pending fill — tarama beklemez."""
    while not state.kill:
        try:
            await manage_pending(ex, state)
            if state.positions:
                bal = await ex.balance()
                if bal:
                    await sync_positions(ex, state, bal)
                syms = list(state.positions.keys())
                await asyncio.gather(*(sell_one(ex, state, s) for s in syms), return_exceptions=True)
            await asyncio.sleep(FAST_SEC)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error("fast: %s", e)
            if ban_until_ms(e):
                await sleep_ban(e)
            else:
                await asyncio.sleep(3)


async def scan_once(ex: Exchange, state: State) -> None:
    if state.kill:
        return
    bal = await ex.balance(force=True)
    if not bal:
        return
    await sync_positions(ex, state, bal)

    if state.realized_bnb < -abs(MAX_DRAWDOWN_QUOTE):
        log.error("KILL DD %.4f USDT", state.realized_bnb)
        state.kill = True
        return

    free_bnb = ex.free(bal, QUOTE)
    open_n = len(state.positions)
    pending_n = len(state.pending)
    log.info(
        "SCAN | USDT=%.4f açık=%d pending=%d min=%d rise≥%.1fbps fee≈%.1fbps",
        free_bnb,
        open_n,
        pending_n,
        MIN_OPEN_COINS,
        min_rise_bps(),
        roundtrip_fee_bps(),
    )

    tickers = await ex.tickers()
    if not tickers:
        return
    movers = pick_movers(ex, tickers, state, SCAN_POOL)
    if not movers:
        log.warning("hareketli pair yok")
        return

    for sym, _, _ in movers:
        state.recently_scanned.append(sym)
    state.recently_scanned = state.recently_scanned[-120:]
    state.watchlist = [s for s, _, _ in movers[:SCAN_POOL]]
    print(
        f"HAVUZ{len(state.watchlist)} → hedef AL/SAT ≥{MIN_OPEN_COINS}",
        flush=True,
    )

    weights = volume_weights(movers[:SCAN_POOL])
    busy = set(state.positions) | set(state.pending)

    async def _one(sym: str, qv: float) -> Optional[SignalReport]:
        try:
            return await analyze_symbol(ex, sym, tickers.get(sym) or {}, qv)
        except Exception as e:
            log.warning("analiz %s: %s", sym, e)
            return None

    reports = await asyncio.gather(
        *[_one(sym, qv) for sym, qv, _ in movers[:SCAN_POOL] if sym not in busy],
        return_exceptions=True,
    )
    signals: List[SignalReport] = []
    for r in reports:
        if isinstance(r, SignalReport):
            log.info(
                "sig %s pass=%d/%d score=%.2f RSI=%s | %s",
                r.symbol,
                r.passed,
                r.total,
                r.score,
                f"{r.rsi:.1f}" if r.rsi is not None else "?",
                ", ".join(r.reasons[:5]),
            )
            if r.passed > 0 and not (r.reasons and str(r.reasons[0]).startswith("VETO")):
                signals.append(r)

    force = (open_n + pending_n) < MIN_OPEN_COINS
    need_pass = MIN_METHODS_FORCE if force else MIN_METHODS_PASS
    signals = [s for s in signals if s.passed >= need_pass]
    # sinyal azsa: veto olmayanları da force için al (bakiye dağılsın)
    if force and len(signals) < max(3, MIN_OPEN_COINS - open_n):
        extra = [
            r for r in reports
            if isinstance(r, SignalReport)
            and r.symbol not in busy
            and r.passed >= 0
            and not (r.reasons and str(r.reasons[0]).startswith("VETO"))
            and r.mid > 0
        ]
        for r in extra:
            if r not in signals:
                signals.append(r)
    signals.sort(key=lambda s: -s.score)

    # Tüm serbest BNB'yi en az MIN_OPEN (15) / TOP_N slota böl
    spendable = max(0.0, free_bnb - RESERVE_BNB)
    empty = max(0, max(TOP_N, MIN_OPEN_COINS) - open_n - pending_n)
    slots_left = 0
    if DEPLOY_ALL_BNB and spendable >= MIN_QUOTE_FREE and empty > 0:
        max_by_cash = max(1, int(spendable // max(MIN_QUOTE_FREE, 1e-12)))
        slots_left = min(empty, max_by_cash)
        log.info(
            "DAĞITIM | free=%.4f spend=%.4f → %d slota (açık=%d hedef≥%d / tarama=%d)",
            free_bnb, spendable, slots_left, open_n, MIN_OPEN_COINS, SCAN_POOL,
        )

    bought = 0
    # sinyal yetmezse watchlist'ten doldur
    buy_queue: List[Any] = list(signals)
    if len(buy_queue) < slots_left:
        for sym, qv, last in movers[:SCAN_POOL]:
            if sym in busy or any(getattr(s, "symbol", None) == sym for s in buy_queue):
                continue
            # sahte SignalReport — force market
            buy_queue.append(
                SignalReport(sym, 0.5, 1, 1, ["deploy_all"], qv, last, last * 0.999, last * 1.001, last, None)
            )
            if len(buy_queue) >= slots_left:
                break

    for sig in buy_queue:
        if bought >= max(0, slots_left):
            break
        bal = await ex.balance(force=True)
        if not bal:
            break
        free_bnb = ex.free(bal, QUOTE)
        spendable = max(0.0, free_bnb - RESERVE_BNB)
        remain = max(1, slots_left - bought)
        budget = spendable / remain
        if budget < MIN_QUOTE_FREE:
            if spendable >= MIN_QUOTE_FREE:
                budget = spendable  # son kalanı tek coine bas
            else:
                log.info("BNB bitti (free=%.5f)", free_bnb)
                break
        ok = await buy_one(ex, state, sig, budget, force_fill=True)
        if ok:
            bought += 1
            busy.add(sig.symbol)
            await asyncio.sleep(0.2)

    log.info(
        "SCAN bitti | emir/AL=%d | açık=%d pending=%d",
        bought,
        len(state.positions),
        len(state.pending),
    )
    state.stats.print_live()
    state.stats.save()


async def main_async() -> None:
    print("=" * 64)
    print("BNB · 50 arama (UP/DOWN/VOLAT) · min 15 coin AL/SAT")
    print(f"SCAN={SCAN_SEC:.0f}s | FAST={FAST_SEC:.0f}s | havuz={SCAN_POOL} | hedef≥{MIN_OPEN_COINS}")
    print(f"CCXT {ccxt.__version__}")
    print("Binance: Pay fees with BNB AÇIK olsun")
    print("=" * 64)

    key, secret = resolve_keys()
    ex = Exchange(key, secret)
    await ex.init()

    n_pairs = sum(1 for s in ex.rest.markets if s.endswith(f"/{QUOTE}") and ":" not in s)
    print(f"Spot {QUOTE} pair≈{n_pairs}")
    bal = await ex.balance(force=True)
    if not bal:
        raise SystemExit("Bakiye yok / ban")
    free = ex.free(bal, QUOTE)
    print(f"{QUOTE} free≈{free:.4f} | ≥{MIN_OPEN_COINS} coine dağıt (reserve {RESERVE_BNB})")
    if free < MIN_QUOTE_FREE * MIN_OPEN_COINS:
        print(f"UYARI: düşük {QUOTE} ({free:.4f}) — 15 coin için BNB artır")

    state = State(stats=DayStats.load())
    await sync_positions(ex, state, bal)
    print(f"Açık pozisyon≈{len(state.positions)}")
    print(_c(_GREEN, "AL=yeşil"), "|", _c(_RED, "SAT=kırmızı"), "|", _c(_ORANGE, "EMİR=turuncu"))
    print("Bilanço: her SCAN + Ctrl+C özet | dosya: output/live_mm_day_stats.json")
    state.stats.print_live()
    print("=" * 64)

    fast_task = asyncio.create_task(fast_loop(ex, state))
    try:
        while not state.kill:
            t0 = time.time()
            try:
                await scan_once(ex, state)
            except Exception as e:
                log.error("scan: %s", e)
                if ban_until_ms(e):
                    await sleep_ban(e)
            elapsed = time.time() - t0
            wait = max(5.0, SCAN_SEC - elapsed)
            st = state.stats
            st.ensure_today()
            log.info(
                "sonraki SCAN %.0fs | bugün emir=%d AL=%d SAT=%d net≈%+.5f%s",
                wait,
                st.orders,
                st.buys,
                st.sells,
                st.net(),
                QUOTE,
            )
            await asyncio.sleep(wait)
    except asyncio.CancelledError:
        pass
    finally:
        state.kill = True
        fast_task.cancel()
        for sym in list(state.positions.keys()):
            try:
                await ex.cancel_all(sym)
            except Exception:
                pass
        state.stats.print_summary()
        state.stats.save()
        print("Kapandı | realize≈%.5f USDT" % state.realized_bnb)
        print(f"Özet dosya: {STATS_PATH}")


def main() -> None:
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
        # Windows konsol renk
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nDurdu")


if __name__ == "__main__":
    main()
