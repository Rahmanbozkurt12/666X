#!/usr/bin/env python3
"""
Binance Spot Market Maker — CANLI (gerçek AL/SAT)

- Testnet YOK (her zaman canlı Binance spot)
- Serbest USDT bakiyesini N parçaya böler (varsayılan 8)
- Her parçada bir pair: limit bid + ask (Post-Only / GTX)
- Spread komisyonu + min edge altına inmez
- Drawdown kill-switch: açık emirleri iptal eder, durur

Çalıştır:
  cd market_maker
  pip install -r requirements.txt
  # market_maker_config.json → api_key / api_secret doldur
  python binance_market_maker_bot_3.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Deque, Dict, List, Optional, Tuple

import ccxt

try:
    import ccxt.pro as ccxtpro
except ImportError:
    ccxtpro = None  # type: ignore


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "DOGE/USDT",
    "ADA/USDT",
    "AVAX/USDT",
]
DEFAULT_SLOTS = 8
CONFIG_NAME = "market_maker_config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mm")


def _precision_from_step(step: float) -> int:
    d = Decimal(str(step)).normalize()
    exp = d.as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 8


def client_order_id() -> str:
    return ("x-GRBCT6GB" + uuid.uuid4().hex)[:32]


def config_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Fees:
    maker: float = 0.001
    taker: float = 0.001
    safety_mult: float = 1.5
    min_edge_bps: float = 8.0
    post_only: bool = True


@dataclass
class Config:
    api_key: str = ""
    api_secret: str = ""
    quote_asset: str = "USDT"
    symbols: List[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    balance_slots: int = DEFAULT_SLOTS
    split_live_balance: bool = True
    total_capital: float = 0.0  # 0 = canlı bakiyeden
    max_inventory_ratio: float = 0.9
    max_drawdown_ratio: float = 0.05
    base_spread_ticks: float = 4.0
    volatility_multiplier: float = 4.0
    inventory_skew_strength: float = 2.0
    imbalance_skew_strength: float = 1.0
    min_order_lifetime: float = 8.0
    max_order_replace_freq: float = 12.0
    min_quote_balance: float = 5.0
    use_avellaneda: bool = True
    risk_aversion: float = 0.05
    time_horizon: float = 0.5
    fees: Fees = field(default_factory=Fees)

    @staticmethod
    def default_dict() -> dict:
        return {
            "exchange": {
                "api_key": "",
                "api_secret": "",
                "testnet": False,
                "quote_asset": "USDT",
                "symbols": list(DEFAULT_SYMBOLS),
            },
            "trading": {
                "total_capital": 0.0,
                "balance_slots": DEFAULT_SLOTS,
                "split_live_balance": True,
                "max_inventory_ratio": 0.9,
                "max_drawdown_ratio": 0.05,
                "base_spread_ticks": 4.0,
                "volatility_multiplier": 4.0,
                "inventory_skew_strength": 2.0,
                "imbalance_skew_strength": 1.0,
                "min_order_lifetime": 8.0,
                "max_order_replace_freq": 12.0,
                "min_quote_balance": 5.0,
            },
            "risk": {
                "use_avellaneda": True,
                "risk_aversion": 0.05,
                "time_horizon": 0.5,
            },
            "fees": {
                "maker_fee_rate": 0.001,
                "taker_fee_rate": 0.001,
                "fee_safety_mult": 1.5,
                "min_edge_bps": 8.0,
                "post_only": True,
            },
        }

    @classmethod
    def create_file(cls, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cls.default_dict(), f, indent=4)

    @classmethod
    def load(cls, path: str) -> "Config":
        if not os.path.exists(path):
            cls.create_file(path)
            raise SystemExit(
                f"Config oluşturuldu: {path}\n"
                "api_key / api_secret doldurup tekrar çalıştır."
            )

        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        ex = raw.get("exchange", {})
        tr = raw.get("trading", {})
        rk = raw.get("risk", {})
        fe = raw.get("fees", {})

        # CANLI ZORUNLU — testnet yok sayılır
        if ex.get("testnet", False):
            log.warning("config testnet=true ama bu bot CANLI; testnet yok sayıldı")

        key = (ex.get("api_key") or os.getenv("BINANCE_API_KEY") or "").strip()
        sec = (ex.get("api_secret") or os.getenv("BINANCE_API_SECRET") or "").strip()

        symbols = ex.get("symbols") or DEFAULT_SYMBOLS
        symbols = [str(s).strip() for s in symbols if str(s).strip()]
        if not symbols:
            symbols = list(DEFAULT_SYMBOLS)

        slots = int(tr.get("balance_slots", DEFAULT_SLOTS) or DEFAULT_SLOTS)
        if len(symbols) > slots:
            symbols = symbols[:slots]

        return cls(
            api_key=key,
            api_secret=sec,
            quote_asset=str(ex.get("quote_asset") or "USDT"),
            symbols=symbols,
            balance_slots=slots,
            split_live_balance=bool(tr.get("split_live_balance", True)),
            total_capital=float(tr.get("total_capital") or 0.0),
            max_inventory_ratio=float(tr.get("max_inventory_ratio", 0.9)),
            max_drawdown_ratio=float(tr.get("max_drawdown_ratio", 0.05)),
            base_spread_ticks=float(tr.get("base_spread_ticks", 4.0)),
            volatility_multiplier=float(tr.get("volatility_multiplier", 4.0)),
            inventory_skew_strength=float(tr.get("inventory_skew_strength", 2.0)),
            imbalance_skew_strength=float(tr.get("imbalance_skew_strength", 1.0)),
            min_order_lifetime=float(tr.get("min_order_lifetime", 8.0)),
            max_order_replace_freq=float(tr.get("max_order_replace_freq", 12.0)),
            min_quote_balance=float(tr.get("min_quote_balance", 5.0)),
            use_avellaneda=bool(rk.get("use_avellaneda", True)),
            risk_aversion=float(rk.get("risk_aversion", 0.05)),
            time_horizon=float(rk.get("time_horizon", 0.5)),
            fees=Fees(
                maker=float(fe.get("maker_fee_rate", 0.001)),
                taker=float(fe.get("taker_fee_rate", 0.001)),
                safety_mult=float(fe.get("fee_safety_mult", 1.5)),
                min_edge_bps=float(fe.get("min_edge_bps", 8.0)),
                post_only=bool(fe.get("post_only", True)),
            ),
        )

    def validate_keys(self) -> None:
        bad = {
            "",
            "your_binance_api_key_here",
            "your_actual_api_key_here",
            "BURAYA_KEY",
            "BURAYA_API_KEY",
        }
        if self.api_key in bad or self.api_secret in bad or not self.api_key or not self.api_secret:
            raise SystemExit(
                "API key/secret eksik.\n"
                f"  → {config_path()} içinde api_key / api_secret yaz\n"
                "  → veya: set BINANCE_API_KEY=... & set BINANCE_API_SECRET=...\n"
                "  → Binance → API Management → Spot Trade izinli key"
            )


# ---------------------------------------------------------------------------
# Exchange client (CANLI)
# ---------------------------------------------------------------------------

class LiveBinance:
    """ccxt Binance spot — sandbox her zaman kapalı."""

    def __init__(self, api_key: str, api_secret: str, post_only: bool = True):
        self.post_only = post_only
        opts = {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "rateLimit": 80,
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
                log.warning("CCXT Pro açılamadı, REST kullanılacak: %s", e)
                self.ws = None

        self.markets_ok = False

    async def _run(self, fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def init(self) -> None:
        await self._run(self.rest.load_markets)
        self.markets_ok = True
        log.info("CANLI Binance spot bağlandı | markets=%d", len(self.rest.markets))

    async def close(self) -> None:
        try:
            if self.ws is not None:
                await self.ws.close()
        except Exception:
            pass
        try:
            if hasattr(self.rest, "close"):
                maybe = self.rest.close()
                if asyncio.iscoroutine(maybe):
                    await maybe
        except Exception:
            pass

    def market(self, symbol: str) -> Optional[dict]:
        if not self.markets_ok:
            return None
        return self.rest.markets.get(symbol)

    async def balance(self) -> Optional[dict]:
        try:
            return await self._run(self.rest.fetch_balance)
        except Exception as e:
            log.error("balance: %s", e)
            return None

    async def order_book(self, symbol: str, limit: int = 20) -> Optional[dict]:
        try:
            return await self._run(self.rest.fetch_order_book, symbol, limit)
        except Exception as e:
            log.error("orderbook %s: %s", symbol, e)
            return None

    async def open_orders(self, symbol: str) -> List[dict]:
        try:
            return await self._run(self.rest.fetch_open_orders, symbol) or []
        except Exception as e:
            log.error("open_orders %s: %s", symbol, e)
            return []

    async def my_trades(self, symbol: str, limit: int = 30) -> List[dict]:
        try:
            return await self._run(self.rest.fetch_my_trades, symbol, None, limit) or []
        except Exception as e:
            log.error("trades %s: %s", symbol, e)
            return []

    async def trading_fees(self, symbol: str) -> Tuple[float, float]:
        maker, taker = 0.001, 0.001
        try:
            fees = await self._run(self.rest.fetch_trading_fees)
            if symbol in fees:
                maker = float(fees[symbol].get("maker", maker))
                taker = float(fees[symbol].get("taker", taker))
                return maker, taker
        except Exception:
            pass
        m = self.market(symbol)
        if m:
            maker = float(m.get("maker", maker))
            taker = float(m.get("taker", taker))
        return maker, taker

    def round_amount(self, symbol: str, amount: float) -> float:
        m = self.market(symbol)
        if not m or amount <= 0:
            return 0.0
        try:
            return float(self.rest.amount_to_precision(symbol, amount))
        except Exception:
            return 0.0

    def round_price(self, symbol: str, price: float) -> float:
        m = self.market(symbol)
        if not m or price <= 0:
            return 0.0
        try:
            return float(self.rest.price_to_precision(symbol, price))
        except Exception:
            return price

    def limits(self, symbol: str) -> Tuple[float, float, float]:
        """min_qty, lot-ish step, min_notional"""
        m = self.market(symbol) or {}
        lim = m.get("limits") or {}
        min_qty = float((lim.get("amount") or {}).get("min") or 0.0)
        min_cost = float((lim.get("cost") or {}).get("min") or 5.0)
        prec = m.get("precision") or {}
        step = 1e-8
        if "amount" in prec:
            a = prec["amount"]
            step = 10 ** (-a) if isinstance(a, int) else float(a)
        return min_qty, step, min_cost

    def tick(self, symbol: str) -> float:
        m = self.market(symbol) or {}
        prec = m.get("precision") or {}
        if "price" in prec:
            p = prec["price"]
            return 10 ** (-p) if isinstance(p, int) else float(p)
        return 0.01

    async def place_limit(
        self, symbol: str, side: str, amount: float, price: float
    ) -> Optional[dict]:
        amount = self.round_amount(symbol, amount)
        price = self.round_price(symbol, price)
        if amount <= 0 or price <= 0:
            return None

        min_qty, _, min_cost = self.limits(symbol)
        if amount < min_qty:
            return None
        if amount * price < min_cost:
            return None

        params: Dict[str, Any] = {"newClientOrderId": client_order_id()}
        if self.post_only:
            params["timeInForce"] = "GTX"  # Post-Only

        try:
            order = await self._run(
                self.rest.create_order, symbol, "limit", side, amount, price, params
            )
            log.info(
                "EMİR %s %s %.8f @ %.8f id=%s",
                side.upper(),
                symbol,
                amount,
                price,
                order.get("id"),
            )
            return order
        except Exception as e:
            msg = str(e)
            if "Post Only" in msg or "-5022" in msg:
                log.warning("Post-only reddedildi %s %s: %s", side, symbol, e)
            else:
                log.error("place_order %s %s: %s", side, symbol, e)
            return None

    async def cancel(self, order_id: str, symbol: str) -> None:
        try:
            await self._run(self.rest.cancel_order, order_id, symbol)
            log.info("iptal %s %s", symbol, order_id)
        except Exception as e:
            log.error("cancel %s: %s", order_id, e)

    async def cancel_all(self, symbol: str) -> None:
        for o in await self.open_orders(symbol):
            oid = o.get("id")
            if oid:
                await self.cancel(str(oid), symbol)

    async def watch_book(self, symbol: str) -> Optional[dict]:
        if not self.ws:
            return None
        try:
            return await self.ws.watch_order_book(symbol, 20)
        except Exception as e:
            log.error("ws book %s: %s", symbol, e)
            return None


# ---------------------------------------------------------------------------
# Single-symbol market maker
# ---------------------------------------------------------------------------

@dataclass
class Book:
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    imbalance: float = 0.0
    volatility: float = 0.0
    ts: float = 0.0


class SlotMaker:
    def __init__(
        self,
        exchange: LiveBinance,
        cfg: Config,
        symbol: str,
        slot_usdt: float,
        starting_equity: float,
    ):
        self.ex = exchange
        self.cfg = cfg
        self.symbol = symbol
        self.base = symbol.split("/")[0]
        self.quote = symbol.split("/")[1] if "/" in symbol else cfg.quote_asset
        self.slot_usdt = float(slot_usdt)
        self.starting_equity = float(starting_equity)
        self.log = logging.getLogger(f"mm.{symbol}")

        self.book = Book()
        self.prices: Deque[Tuple[float, float]] = deque(maxlen=600)
        self.base_free = 0.0
        self.quote_free = 0.0
        self.position_base = 0.0
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.open: Dict[str, dict] = {}
        self.last_quote_ts = 0.0
        self.last_trade_id: Optional[str] = None
        self.seen_trades: set = set()
        self.kill = False
        self.maker_fee = cfg.fees.maker
        self.taker_fee = cfg.fees.taker
        self.running = True

    # ---- fees / spread floor ----

    def rt_fee_rate(self) -> float:
        return max(self.maker_fee, 0.0) * self.cfg.fees.safety_mult * 2.0

    def min_full_spread(self, mid: float) -> float:
        tick = self.ex.tick(self.symbol)
        edge = mid * (self.cfg.fees.min_edge_bps / 10000.0)
        fee = mid * self.rt_fee_rate()
        tick_floor = tick * max(2.0, self.cfg.base_spread_ticks)
        return max(fee + edge, tick_floor)

    def min_half(self, mid: float) -> float:
        return self.min_full_spread(mid) / 2.0

    # ---- market state ----

    def update_from_book(self, ob: dict) -> None:
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        if not bids or not asks:
            return
        bid, bq = float(bids[0][0]), float(bids[0][1])
        ask, aq = float(asks[0][0]), float(asks[0][1])
        mid = (bid + ask) / 2.0
        tot = bq + aq
        imb = ((bq - aq) / tot) if tot > 0 else 0.0

        self.book = Book(bid=bid, ask=ask, mid=mid, imbalance=imb, ts=time.time())
        self.prices.append((time.time(), mid))
        self._update_vol()
        if self.position_base != 0 and mid > 0:
            # approx unrealized vs slot mid-entry not tracked → use last mid move soft
            self.unrealized_pnl = 0.0  # realized tracked from fills; equity via balances

    def _update_vol(self) -> None:
        if len(self.prices) < 5:
            return
        recent = list(self.prices)[-60:]
        rets = []
        for i in range(1, len(recent)):
            a, b = recent[i - 1][1], recent[i][1]
            if a > 0:
                rets.append(math.log(b / a))
        if not rets:
            return
        vol = math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(60 * 10)
        self.book.volatility = vol

    async def refresh_balance(self) -> None:
        bal = await self.ex.balance()
        if not bal:
            return
        self.base_free = float((bal.get("free") or {}).get(self.base, 0.0) or 0.0)
        self.quote_free = float((bal.get("free") or {}).get(self.quote, 0.0) or 0.0)
        self.position_base = float((bal.get("total") or {}).get(self.base, 0.0) or 0.0)

    async def sync_fees(self) -> None:
        m, t = await self.ex.trading_fees(self.symbol)
        self.maker_fee = m
        self.taker_fee = t
        self.log.info(
            "fees maker=%.4f%% taker=%.4f%% floor≈%.3f%% post_only=%s slot=$%.2f",
            m * 100,
            t * 100,
            self.rt_fee_rate() * 100 + self.cfg.fees.min_edge_bps / 100.0,
            self.cfg.fees.post_only,
            self.slot_usdt,
        )

    async def bootstrap_trade_cursor(self) -> None:
        trades = await self.ex.my_trades(self.symbol, limit=1)
        if trades:
            self.last_trade_id = str(trades[-1]["id"])

    async def poll_fills(self) -> None:
        trades = await self.ex.my_trades(self.symbol, limit=40)
        for t in trades:
            tid = str(t["id"])
            if tid in self.seen_trades:
                continue
            if self.last_trade_id and tid.isdigit() and self.last_trade_id.isdigit():
                if int(tid) <= int(self.last_trade_id):
                    continue
            self.seen_trades.add(tid)
            self.last_trade_id = tid
            side = t.get("side")
            amt = float(t.get("amount") or 0)
            px = float(t.get("price") or 0)
            fee = float((t.get("fee") or {}).get("cost") or 0)
            if side == "buy":
                self.realized_pnl -= amt * px + fee
            else:
                self.realized_pnl += amt * px - fee
            self.log.info("FILL %s %.6f @ %.6f fee=%.6f", side, amt, px, fee)
        if len(self.seen_trades) > 500:
            self.seen_trades = set(list(self.seen_trades)[-200:])

    # ---- risk ----

    def inventory_usd(self) -> float:
        return abs(self.position_base) * max(self.book.mid, 0.0)

    def max_inv_usd(self) -> float:
        return self.slot_usdt * self.cfg.max_inventory_ratio

    def check_risk(self) -> bool:
        if self.book.mid > 0 and self.inventory_usd() > self.max_inv_usd() * 1.05:
            self.log.warning("envanter limit: $%.2f > $%.2f", self.inventory_usd(), self.max_inv_usd())
            return False

        # drawdown vs slot equity proxy: starting_equity is total/n; use realized on this symbol
        dd = -min(0.0, self.realized_pnl)
        max_dd = self.slot_usdt * self.cfg.max_drawdown_ratio
        if self.slot_usdt > 0 and dd > max_dd:
            self.log.error("KILL SWITCH drawdown $%.2f > $%.2f", dd, max_dd)
            self.kill = True
            return False
        return True

    # ---- quotes ----

    def inventory_pos(self) -> float:
        max_base = self.max_inv_usd() / self.book.mid if self.book.mid > 0 else 0.0
        if max_base <= 0:
            return 0.0
        return max(-1.0, min(1.0, self.position_base / max_base))

    def build_quotes(self) -> Optional[Tuple[float, float, float, float]]:
        mid = self.book.mid
        if mid <= 0:
            return None

        fee_half = self.min_half(mid)
        tick = self.ex.tick(self.symbol)
        vol = max(self.book.volatility, 0.0005)

        base_half = max(
            self.cfg.base_spread_ticks * tick / 2.0,
            mid * 0.0003,
            fee_half,
        )
        vol_half = min(self.cfg.volatility_multiplier * vol * mid * 0.05, mid * 0.008)
        half = max(base_half, vol_half, fee_half)

        if self.cfg.use_avellaneda:
            q = max(-0.5, min(0.5, self.inventory_pos()))
            adj = q * self.cfg.risk_aversion * min(vol ** 2, 1e-4) * self.cfg.time_horizon
            adj = max(-mid * 0.001, min(mid * 0.001, adj))
            center = mid - adj
        else:
            center = mid

        inv_skew = -self.inventory_pos() * self.cfg.inventory_skew_strength * tick
        imb_skew = self.book.imbalance * self.cfg.imbalance_skew_strength * tick
        skew_cap = half * 0.25
        inv_skew = max(-skew_cap, min(skew_cap, inv_skew))
        imb_skew = max(-skew_cap, min(skew_cap, imb_skew))

        bid = center - half + inv_skew - imb_skew
        ask = center + half + inv_skew + imb_skew

        # fee floor
        min_full = self.min_full_spread(mid)
        if ask - bid < min_full:
            half = min_full / 2.0
            bid = mid - half
            ask = mid + half

        bid = self.ex.round_price(self.symbol, bid)
        ask = self.ex.round_price(self.symbol, ask)
        if ask <= bid:
            ask = self.ex.round_price(self.symbol, bid + max(tick, min_full))

        if (ask - bid) < min_full * 0.98:
            return None

        # sizes: ~90% of slot
        min_qty, _, min_cost = self.ex.limits(self.symbol)
        target = max(min_cost * 1.05, self.slot_usdt * 0.90)
        base_sz = target / mid

        ip = self.inventory_pos()
        bid_sz = base_sz * max(0.5, 1.0 + ip * 0.15)
        ask_sz = base_sz * max(0.5, 1.0 - ip * 0.15)

        max_buy_notional = min(self.quote_free * 0.95, self.slot_usdt)
        if max_buy_notional >= min_cost:
            bid_sz = min(bid_sz, max_buy_notional / mid)
        else:
            bid_sz = 0.0

        max_sell = min(self.base_free * 0.95, self.slot_usdt / mid)
        if max_sell * mid >= min_cost:
            ask_sz = min(ask_sz, max_sell)
        else:
            ask_sz = 0.0

        bid_sz = self.ex.round_amount(self.symbol, bid_sz)
        ask_sz = self.ex.round_amount(self.symbol, ask_sz)

        if bid_sz > 0 and bid_sz * bid < min_cost:
            bid_sz = 0.0
        if ask_sz > 0 and ask_sz * ask < min_cost:
            ask_sz = 0.0

        # inventory hard stop: don't buy more
        if self.inventory_usd() >= self.max_inv_usd():
            bid_sz = 0.0

        return bid, ask, bid_sz, ask_sz

    async def replace_quotes(self) -> None:
        now = time.time()
        if now - self.last_quote_ts < self.cfg.max_order_replace_freq:
            return
        if self.kill:
            await self.ex.cancel_all(self.symbol)
            self.open.clear()
            return

        q = self.build_quotes()
        if not q:
            return
        bid, ask, bid_sz, ask_sz = q

        await self.ex.cancel_all(self.symbol)
        self.open.clear()
        await asyncio.sleep(0.15)

        if bid_sz > 0 and self.quote_free >= self.cfg.min_quote_balance:
            o = await self.ex.place_limit(self.symbol, "buy", bid_sz, bid)
            if o and o.get("id"):
                self.open[str(o["id"])] = {"side": "buy", "price": bid, "amount": bid_sz}

        if ask_sz > 0 and self.base_free > 0:
            o = await self.ex.place_limit(self.symbol, "sell", ask_sz, ask)
            if o and o.get("id"):
                self.open[str(o["id"])] = {"side": "sell", "price": ask, "amount": ask_sz}

        self.last_quote_ts = time.time()
        self.log.info(
            "quote bid=%.6f×%.6f ask=%.6f×%.6f mid=%.6f spread=%.3f%%",
            bid,
            bid_sz,
            ask,
            ask_sz,
            self.book.mid,
            ((ask - bid) / self.book.mid * 100) if self.book.mid else 0,
        )

    async def sync_open(self) -> None:
        live = await self.ex.open_orders(self.symbol)
        ids = {str(o["id"]) for o in live}
        for oid in list(self.open):
            if oid not in ids:
                del self.open[oid]
                # fill olabilir → hemen yeniden quote
                self.last_quote_ts = 0.0

    def status_line(self) -> str:
        return (
            f"{self.symbol:10} mid={self.book.mid:.6f} "
            f"base={self.position_base:.6f} slot=${self.slot_usdt:.2f} "
            f"rpnl=${self.realized_pnl:.2f} open={len(self.open)} "
            f"{'KILL' if self.kill else 'OK'}"
        )

    async def run(self, stagger: float = 0.0) -> None:
        await asyncio.sleep(stagger)
        if self.symbol not in (self.ex.rest.markets or {}):
            self.log.error("market yok: %s", self.symbol)
            return

        await self.sync_fees()
        await self.refresh_balance()
        await self.bootstrap_trade_cursor()

        ob = await self.ex.order_book(self.symbol)
        if ob:
            self.update_from_book(ob)
            await self.replace_quotes()

        use_ws = self.ex.ws is not None
        last_rest = 0.0
        last_status = 0.0

        while self.running:
            try:
                if self.kill:
                    await self.ex.cancel_all(self.symbol)
                    self.log.error("kill switch — slot durdu")
                    break

                if use_ws:
                    ob = await self.ex.watch_book(self.symbol)
                    if ob:
                        self.update_from_book(ob)
                else:
                    now = time.time()
                    if now - last_rest >= 1.0:
                        ob = await self.ex.order_book(self.symbol)
                        if ob:
                            self.update_from_book(ob)
                        last_rest = now

                await self.refresh_balance()
                await self.poll_fills()
                await self.sync_open()

                if not self.check_risk():
                    if self.kill:
                        await self.ex.cancel_all(self.symbol)
                        break
                    # inventory: only allow sells
                    await self.replace_quotes()
                else:
                    await self.replace_quotes()

                if time.time() - last_status > 15:
                    print(self.status_line())
                    last_status = time.time()

                await asyncio.sleep(0.05 if use_ws else 0.4)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.error("loop: %s", e)
                await asyncio.sleep(2.0)

        await self.ex.cancel_all(self.symbol)
        self.running = False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_live(cfg: Config) -> None:
    ex = LiveBinance(cfg.api_key, cfg.api_secret, post_only=cfg.fees.post_only)
    await ex.init()

    bal = await ex.balance()
    if not bal:
        await ex.close()
        raise SystemExit("Bakiye alınamadı — API key / izinleri kontrol et")

    quote_free = float((bal.get("free") or {}).get(cfg.quote_asset, 0.0) or 0.0)
    if cfg.split_live_balance and quote_free > 0:
        total = quote_free
    elif cfg.total_capital > 0:
        total = cfg.total_capital
    else:
        total = quote_free

    symbols = list(cfg.symbols)
    n = max(1, min(len(symbols), cfg.balance_slots))
    symbols = symbols[:n]
    slot = total / n

    print("=" * 64)
    print("Binance Market Maker — CANLI AL/SAT (testnet YOK)")
    print("=" * 64)
    print(f"USDT serbest : ${quote_free:,.2f}")
    print(f"Kullanılan   : ${total:,.2f}")
    print(f"Slotlar      : {n} × ${slot:,.2f}")
    print(f"Pairler      : {', '.join(symbols)}")
    print(
        f"Fee floor    : ~{(cfg.fees.maker * cfg.fees.safety_mult * 2 * 100) + cfg.fees.min_edge_bps / 100:.3f}% "
        f"+ post_only={cfg.fees.post_only}"
    )
    print(f"Kill switch  : slot drawdown %{cfg.max_drawdown_ratio * 100:.1f}")
    print("Durdur       : Ctrl+C")
    print("=" * 64)

    if slot < 6:
        await ex.close()
        raise SystemExit(
            f"Slot çok küçük (${slot:.2f}). En az ~$6–10 / coin lazım "
            f"(min notional). USDT ekle veya symbols azalt."
        )

    makers = [
        SlotMaker(ex, cfg, sym, slot_usdt=slot, starting_equity=slot) for sym in symbols
    ]

    tasks = [
        asyncio.create_task(m.run(stagger=i * 0.35), name=f"mm-{m.symbol}")
        for i, m in enumerate(makers)
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for m in makers:
            m.running = False
            try:
                await ex.cancel_all(m.symbol)
            except Exception:
                pass
        await ex.close()
        print("Tüm emirler iptal | bağlantı kapandı")


def main() -> None:
    print(f"CCXT v{ccxt.__version__}")
    if ccxtpro:
        print("CCXT Pro: var (WebSocket)")
    else:
        print("CCXT Pro: yok → REST (pip install 'ccxt[pro]')")

    path = config_path()
    cfg = Config.load(path)
    cfg.validate_keys()

    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    try:
        asyncio.run(run_live(cfg))
    except KeyboardInterrupt:
        print("\nDurduruldu (Ctrl+C)")


if __name__ == "__main__":
    main()
