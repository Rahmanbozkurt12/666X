#!/usr/bin/env python3
"""
Binance Spot — BNB · TOP 20 hareketli · multi-method AL · hızlı SAT · CANLI

1) API KEY / SECRET yaz (+ BNB bakiye, Pay fees with BNB AÇIK)
2) pip install ccxt
3) python live_mm.py

Mimari:
  • Her 55 sn SCAN → hacim+hareket TOP20 + tüm metodlar → AL (min 5 kesin)
  • Her 3 sn FAST → SAT + pending fill (hızlı)
  • RSI>52 / pump VETO (kazanç oranı)
  • Maker; min5 için fill olmazsa MARKET
  • Komisyon eşiği + günlük bilanço
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
TOP_N = 20
MIN_OPEN_COINS = 5
SCAN_SEC = 55.0                 # daha hızlı AL turu (eski 100)
FAST_SEC = 3.0                  # SAT / fill takibi hızlı
BUY_COOLDOWN_SEC = 480.0        # 8 dk (biraz daha çevik)
SELL_COOLDOWN_SEC = 25.0

# Komisyon koruması (BNB fee ≈ %0.075)
MAKER_FEE = 0.00075
TAKER_FEE = 0.00075
FEE_SAFETY = 1.25
MIN_EDGE_BPS = 8.0              # fee üstü kâr — biraz daha çabuk SAT
# SAT eşiği dinamik ≈ fee*safety*2 + edge

DIP_BPS = 22.0
MIN_QUOTE_VOL_BNB = 8.0         # BNB pair'ler düşük hacimli olabilir — 20'yi doldur
CANDIDATE_POOL = 80
MIN_BNB_FREE = 0.008
MAX_DRAWDOWN_BNB = 0.25
MAX_SPREAD_BPS = 45.0
ALLOW_TAKER_TO_FILL = True      # min 5 için dolduramayan maker → market AL
PENDING_MAX_SEC = 18.0          # fill olmazsa iptal / taker

# Multi-method — kazanç oranı için sıkı AL
RSI_PERIOD = 14
RSI_OVERSOLD = 42.0
RSI_MAX_BUY = 52.0              # üstü ALMA (HBAR 78 gibi pump yasak)
RSI_EXIT = 58.0                 # daha erken kâr al
EMA_FAST = 7
EMA_SLOW = 21
MIN_METHODS_PASS = 4            # kaliteli AL
MIN_METHODS_FORCE = 3           # min 5 doldururken taban (yine RSI_MAX_BUY şart)
KLINE_TF = "1m"
KLINE_LIMIT = 60

BALANCE_CACHE_SEC = 20.0
POST_ONLY = True

SKIP_BASES = {
    "BNB", "USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDE", "USD1",
    "EUR", "TRY", "BRL", "AEUR",
    # BTC/ETH serbest bırakıldı — hacim listesine girebilir; skor mid-cap'i öne alır
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
    """BNB cinsinden tahmini maker komisyon."""
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
            f"  Kazanç (brüt)        : {_c(_GREEN, f'+{self.won_bnb:.6f} BNB')}",
            f"  Kayıp (brüt)         : {_c(_RED, f'-{self.lost_bnb:.6f} BNB')}",
            f"  Komisyon (tahmini)   : {_c(_ORANGE, f'{self.fees_bnb:.6f} BNB')}  (maker≈{MAKER_FEE*100:.3f}%)",
            f"  Net (kazanç-kayıp-fee): {_c(net_c, f'{net:+.6f} BNB')}",
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
            + _c(net_c, f"net={net:+.5f} BNB")
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
        if quote_bnb < max(min_cost, MIN_BNB_FREE * 0.5):
            return None
        async with self._order_lock:
            try:
                say_order(f"MARKET BUY {symbol} ~{quote_bnb:.4f} BNB")
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
    try:
        ch = abs(float(t.get("percentage") or 0))
    except Exception:
        ch = 0.0
    qv = float(t.get("quoteVolume") or 0)
    if qv <= 0:
        return 0.0
    return ch * math.log10(qv + 10.0) + (ch ** 1.2) * 0.5


def _bnb_spot_rows(ex: Exchange, tickers: Dict[str, dict]) -> List[Tuple[str, dict, float, float, float]]:
    """(sym, ticker, qv, last, abs_pct)"""
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
        if qv < MIN_QUOTE_VOL_BNB:
            continue
        last = float(t.get("last") or t.get("close") or 0)
        if last <= 0:
            continue
        try:
            pct = abs(float(t.get("percentage") or 0))
        except Exception:
            pct = 0.0
        out.append((sym, t, qv, last, pct))
    return out


def pick_movers(
    ex: Exchange,
    tickers: Dict[str, dict],
    state: State,
    n: int = TOP_N,
) -> List[Tuple[str, float, float]]:
    """
    En az n coin: yarısı hacim lideri + yarısı en hareketli (|%|).
    Eksik kalırsa skor havuzundan tamamla — logdaki 4'lük listeyi önler.
    """
    rows = _bnb_spot_rows(ex, tickers)
    if not rows:
        return []

    by_vol = sorted(rows, key=lambda r: -r[2])
    by_move = sorted(rows, key=lambda r: (-r[4], -r[2]))
    recent = set(state.recently_scanned[-30:])

    picked: List[str] = []
    meta: Dict[str, Tuple[float, float]] = {}

    half = max(1, n // 2)
    for sym, _t, qv, last, _p in by_vol[:half]:
        if sym not in picked:
            picked.append(sym)
            meta[sym] = (qv, last)
    for sym, _t, qv, last, _p in by_move[:half + 5]:
        if sym not in picked:
            picked.append(sym)
            meta[sym] = (qv, last)
        if len(picked) >= n:
            break

    # skor ile tamamla (rotasyon: recent cezalı ama eleme yok)
    scored = sorted(
        (
            (score_ticker(t) * (0.55 if sym in recent else 1.0), sym, qv, last)
            for sym, t, qv, last, _ in rows
        ),
        key=lambda x: -x[0],
    )
    for _sc, sym, qv, last in scored:
        if sym in picked:
            continue
        picked.append(sym)
        meta[sym] = (qv, last)
        if len(picked) >= n:
            break

    # pozisyonlar her zaman listede
    for sym in list(state.positions.keys()) + list(state.pending.keys()):
        if sym not in picked:
            t = tickers.get(sym) or {}
            picked.append(sym)
            meta[sym] = (float(t.get("quoteVolume") or 1), float(t.get("last") or 0))

    result = [(s, meta[s][0], meta[s][1]) for s in picked[: max(n, len(state.positions))]]
    log.info(
        "watchlist aday=%d → seçilen=%d (hacim+hareket)",
        len(rows),
        len(result),
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

    # fee guard: geniş spread = komisyona ezilme
    if spread_bps > MAX_SPREAD_BPS:
        return SignalReport(symbol, 0, 0, total, [f"spread_wide={spread_bps:.0f}"], qv, last, bid, ask, mid)

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

    if rsi_v is not None and rsi_v <= RSI_OVERSOLD:
        passed += 1
        reasons.append(f"RSI={rsi_v:.1f}")
    else:
        reasons.append(f"RSI_fail={rsi_v}")

    # HARD VETO: aşırı alımda ALMA (logdaki HBAR RSI78 hatası)
    if rsi_v is not None and rsi_v > RSI_MAX_BUY:
        return SignalReport(
            symbol, 0, 0, total,
            [f"VETO_RSI={rsi_v:.1f}>{RSI_MAX_BUY}"] + reasons,
            qv, last, bid, ask, mid, rsi_v,
        )

    # HARD VETO: güçlü yeşil pump kovalama
    if pct > 4.0:
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
    need = min_rise_bps()

    # RSI güçlendirici: overbought ise eşiği biraz indir
    ohlcv = await ex.klines(symbol)
    closes = [float(r[4]) for r in ohlcv] if ohlcv else []
    rsi_v = rsi_wilder(closes) if closes else None
    if rsi_v is not None and rsi_v >= RSI_EXIT:
        need = max(roundtrip_fee_bps() + 4.0, need * 0.85)

    if rise < need:
        return False

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

    # iptal + emir peş peşe (hız) — kârda market SAT (hız + fill)
    await ex.cancel_all(symbol)
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
            state.stats.fees_bnb += est_fee(qty * bid) * (TAKER_FEE / MAKER_FEE)
        log.info("MARKET SELL %s qty=%.6f id=%s", symbol, qty, o.get("id"))
    except Exception as e:
        log.warning("market sell fail → limit: %s", e)
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
    say_sell(
        f"{symbol} +{rise:.1f}bps need≥{need:.1f} RSI={rsi_s} "
        f"pnl={pnl:+.6f}BNB | bugün satış={st.sells} net≈{st.net():+.5f}BNB"
    )
    st.save()
    log.info("%s SAT +%.1fbps pnl≈%.5fBNB fee-safe", symbol, rise, pnl)
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
        return False
    if now < state.buy_block_until.get(sig.symbol, 0):
        return False
    if budget < MIN_BNB_FREE:
        return False
    if sig.rsi is not None and sig.rsi > RSI_MAX_BUY:
        return False
    spread_bps = (sig.ask - sig.bid) / sig.mid * 10000.0 if sig.mid > 0 else 999
    if spread_bps > MAX_SPREAD_BPS:
        return False

    need = MIN_METHODS_FORCE if force_fill else MIN_METHODS_PASS
    if sig.passed < need:
        return False

    # min 5 için: maker doldurmazsa market (kesin al)
    use_market = force_fill and ALLOW_TAKER_TO_FILL and (
        len(state.positions) + len(state.pending) < MIN_OPEN_COINS
    )

    methods = ",".join(sig.reasons[:5])
    await ex.cancel_all(sig.symbol)

    if use_market:
        o = await ex.place_market_buy(sig.symbol, budget * 0.96, state.stats)
        if not o:
            return False
        # market → hemen pozisyon (balance sync sonra düzeltir)
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
            f"{sig.symbol} MARKET @≈{fill_px:.8f} bud={budget:.4f}BNB | "
            f"methods={sig.passed}/{sig.total} RSI={sig.rsi} | bugün AL={st.buys}"
        )
        st.save()
        return True

    # maker: bid'e yapış (tick altı değil — fill şansı yüksek)
    tick = ex.tick(sig.symbol)
    price = ex.px(sig.symbol, max(sig.bid - tick, sig.bid * 0.9995))
    if price <= 0:
        return False
    min_qty, min_cost = ex.limits(sig.symbol)
    qty = ex.amt(sig.symbol, (budget * 0.96) / price)
    if qty < min_qty or qty * price < max(min_cost, MIN_BNB_FREE * 0.4):
        return False

    o = await ex.place_fast(sig.symbol, "buy", qty, price, state.stats)
    if not o:
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
    say_order(f"PENDING BUY {sig.symbol} @ {price:.8f} (fill bekleniyor ≤{PENDING_MAX_SEC:.0f}s)")
    log.info("%s AL emir methods=%d/%d pending", sig.symbol, sig.passed, sig.total)
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
        if need_force and ALLOW_TAKER_TO_FILL and ex.free(bal, QUOTE) >= MIN_BNB_FREE:
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

    if state.realized_bnb < -abs(MAX_DRAWDOWN_BNB):
        log.error("KILL DD %.4f BNB", state.realized_bnb)
        state.kill = True
        return

    free_bnb = ex.free(bal, QUOTE)
    open_n = len(state.positions)
    pending_n = len(state.pending)
    log.info(
        "SCAN | BNB=%.4f açık=%d pending=%d min=%d rise≥%.1fbps fee≈%.1fbps",
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
    movers = pick_movers(ex, tickers, state, TOP_N)
    if not movers:
        log.warning("hareketli pair yok")
        return

    for sym, _, _ in movers:
        state.recently_scanned.append(sym)
    state.recently_scanned = state.recently_scanned[-80:]
    state.watchlist = [s for s, _, _ in movers[:TOP_N]]
    print(
        f"TOP{len(state.watchlist)}:",
        ", ".join(s.replace(f"/{QUOTE}", "") for s in state.watchlist),
    )

    weights = volume_weights(movers[:TOP_N])
    busy = set(state.positions) | set(state.pending)

    async def _one(sym: str, qv: float) -> Optional[SignalReport]:
        try:
            return await analyze_symbol(ex, sym, tickers.get(sym) or {}, qv)
        except Exception as e:
            log.warning("analiz %s: %s", sym, e)
            return None

    reports = await asyncio.gather(
        *[_one(sym, qv) for sym, qv, _ in movers[:TOP_N] if sym not in busy],
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
    signals.sort(key=lambda s: -s.score)

    slots_left = 0
    if free_bnb >= MIN_BNB_FREE:
        if force:
            slots_left = MIN_OPEN_COINS - open_n - pending_n
        else:
            slots_left = min(3, TOP_N - open_n - pending_n)

    bought = 0
    for sig in signals:
        if bought >= max(0, slots_left):
            break
        bal = await ex.balance(force=True)
        if not bal:
            break
        free_bnb = ex.free(bal, QUOTE)
        remain = max(1, slots_left - bought)
        if force:
            budget = free_bnb / remain
        else:
            w = weights.get(sig.symbol, 1.0 / TOP_N)
            budget = min(free_bnb * min(0.35, max(0.08, w * 2.0)), free_bnb / remain)
        if budget < MIN_BNB_FREE:
            continue
        ok = await buy_one(ex, state, sig, budget, force_fill=force)
        if ok:
            bought += 1
            await asyncio.sleep(0.25)

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
    print("BNB TOP20 · hacim+hareket · multi-method · fee-safe · min5 fill")
    print(f"SCAN={SCAN_SEC:.0f}s | FAST={FAST_SEC:.0f}s | min_rise={min_rise_bps():.1f}bps | RSI_max_buy={RSI_MAX_BUY}")
    print(f"CCXT {ccxt.__version__}")
    print("Binance: Pay fees with BNB AÇIK olsun")
    print("=" * 64)

    key, secret = resolve_keys()
    ex = Exchange(key, secret)
    await ex.init()

    bnb_n = sum(1 for s in ex.rest.markets if s.endswith(f"/{QUOTE}") and ":" not in s)
    print(f"BNB spot pair≈{bnb_n}")
    bal = await ex.balance(force=True)
    if not bal:
        raise SystemExit("Bakiye yok / ban")
    free = ex.free(bal, QUOTE)
    print(f"BNB free≈{free:.4f} | min {MIN_OPEN_COINS} coin hedef")
    if free < MIN_BNB_FREE * MIN_OPEN_COINS:
        print(f"UYARI: düşük BNB ({free:.4f})")

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
                "sonraki SCAN %.0fs | bugün emir=%d AL=%d SAT=%d net≈%+.5fBNB",
                wait,
                st.orders,
                st.buys,
                st.sells,
                st.net(),
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
        print("Kapandı | realize≈%.5f BNB" % state.realized_bnb)
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
