#!/usr/bin/env python3
"""
Binance Spot anlık alım-satım botu (EMA + RSI).

Varsayılan: DRY_RUN (paper). Gerçek emir için config live.enabled=true
ve BINANCE_API_KEY / BINANCE_API_SECRET gerekir (withdrawal kapalı tut).

Kullanım:
  python binance_spot_bot.py                 # sürekli dry-run
  python binance_spot_bot.py --once          # tek tur
  python binance_spot_bot.py --symbol BTCUSDT
  python binance_spot_bot.py --live          # gerçek trade (dikkat!)

Uyarı: Bu kod eğitim / iskelet amaçlıdır. Kâr garantisi yoktur.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "binance_spot_bot.json"
STATE_PATH = ROOT / "output" / "binance_spot_bot_state.json"
# Public market data mirror (geo-restricted ortamlarda api.binance.com 451 verebilir)
DEFAULT_PUBLIC_BASE = "https://data-api.binance.vision"
DEFAULT_TRADE_BASE = "https://api.binance.com"


# ─── utils ───────────────────────────────────────────────────────────────────


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) and value.strip() else default


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_pct(x: float) -> str:
    return f"{x:+.3f}%"


# ─── indicators (pure Python) ────────────────────────────────────────────────


def ema(vals: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    if len(vals) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(vals[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_g, avg_l = gains / period, losses / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


# ─── Binance REST ────────────────────────────────────────────────────────────


class BinanceClient:
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        recv_window_ms: int = 5000,
        public_base: str | None = None,
        trade_base: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window_ms = recv_window_ms
        self.public_base = (public_base or env("BINANCE_PUBLIC_BASE") or DEFAULT_PUBLIC_BASE).rstrip(
            "/"
        )
        self.trade_base = (trade_base or env("BINANCE_API_BASE") or DEFAULT_TRADE_BASE).rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "666X-binance-spot-bot/1.0"})
        if api_key:
            self.session.headers["X-MBX-APIKEY"] = api_key

    def _get_public(self, path: str, params: dict[str, Any] | None = None) -> Any:
        bases = [self.public_base]
        if self.trade_base not in bases:
            bases.append(self.trade_base)
        last_err: Exception | None = None
        for base in bases:
            try:
                r = self.session.get(f"{base}{path}", params=params or {}, timeout=20)
                if r.status_code == 451:
                    last_err = requests.HTTPError(f"451 from {base}", response=r)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                last_err = e
                continue
        raise RuntimeError(f"Public market data failed: {last_err}")

    def _signed(self, method: str, path: str, params: dict[str, Any]) -> Any:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Live mode requires BINANCE_API_KEY and BINANCE_API_SECRET")
        params = dict(params)
        params["timestamp"] = now_ms()
        params["recvWindow"] = self.recv_window_ms
        query = urllib.parse.urlencode(params, doseq=True)
        sig = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        url = f"{self.trade_base}{path}?{query}&signature={sig}"
        r = self.session.request(method, url, timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"Binance API {r.status_code}: {r.text}")
        return r.json()

    def klines(self, symbol: str, interval: str, limit: int = 120) -> list[dict[str, float]]:
        raw = self._get_public(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        out: list[dict[str, float]] = []
        for row in raw:
            out.append(
                {
                    "open_time": float(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "close_time": float(row[6]),
                }
            )
        return out

    def price(self, symbol: str) -> float:
        data = self._get_public("/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def account(self) -> dict[str, Any]:
        return self._signed("GET", "/api/v3/account", {})

    def market_buy_quote(self, symbol: str, quote_qty: float) -> dict[str, Any]:
        # quoteOrderQty: spend this many USDT
        return self._signed(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": f"{quote_qty:.8f}".rstrip("0").rstrip("."),
            },
        )

    def market_sell_base(self, symbol: str, quantity: float) -> dict[str, Any]:
        return self._signed(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": f"{quantity:.8f}".rstrip("0").rstrip("."),
            },
        )


# ─── strategy ────────────────────────────────────────────────────────────────


@dataclass
class Signal:
    action: str  # BUY | SELL | HOLD
    reason: str
    price: float
    ema_fast: float
    ema_slow: float
    rsi: float


def evaluate_signal(
    candles: list[dict[str, float]],
    *,
    ema_fast_p: int,
    ema_slow_p: int,
    rsi_period: int,
    rsi_buy_max: float,
    rsi_sell_min: float,
) -> Signal:
    closes = [c["close"] for c in candles]
    price = closes[-1]
    ef = ema(closes, ema_fast_p)
    es = ema(closes, ema_slow_p)
    rs = rsi(closes, rsi_period)

    if ef[-1] is None or es[-1] is None or rs[-1] is None:
        return Signal("HOLD", "indicators_warming_up", price, 0.0, 0.0, 0.0)
    if ef[-2] is None or es[-2] is None:
        return Signal("HOLD", "need_prev_bar", price, ef[-1], es[-1], rs[-1])

    fast, slow = ef[-1], es[-1]
    prev_fast, prev_slow = ef[-2], es[-2]
    r = rs[-1]

    bullish_cross = prev_fast <= prev_slow and fast > slow
    bearish_cross = prev_fast >= prev_slow and fast < slow

    if bullish_cross and r <= rsi_buy_max:
        return Signal(
            "BUY",
            f"EMA_CROSS_UP rsi={r:.1f}<={rsi_buy_max}",
            price,
            fast,
            slow,
            r,
        )
    if bearish_cross or r >= rsi_sell_min:
        why = "EMA_CROSS_DOWN" if bearish_cross else f"RSI_OVERBOUGHT>={rsi_sell_min}"
        return Signal("SELL", f"{why} rsi={r:.1f}", price, fast, slow, r)
    return Signal(
        "HOLD",
        f"trend={'up' if fast > slow else 'down'} rsi={r:.1f}",
        price,
        fast,
        slow,
        r,
    )


# ─── paper / live trader ─────────────────────────────────────────────────────


@dataclass
class Position:
    symbol: str
    entry: float
    qty: float
    peak: float
    opened_at: float
    entry_reason: str


@dataclass
class BotState:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "DRY_RUN"


class SpotBot:
    def __init__(
        self,
        client: BinanceClient,
        cfg: dict[str, Any],
        *,
        live: bool,
        state_path: Path = STATE_PATH,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.live = live
        self.state_path = state_path
        risk = cfg.get("risk") or {}
        self.hard_stop_pct = float(risk.get("hard_stop_pct", 1.5))
        self.take_profit_pct = float(risk.get("take_profit_pct", 3.0))
        self.trail_pct = float(risk.get("trail_pct", 0.8))
        self.position_fraction = float(cfg.get("position_fraction", 0.95))
        self.max_open = int(cfg.get("max_open_positions", 1))
        starting = float(cfg.get("starting_cash_usdt", 1000))
        self.state = BotState(cash=starting, mode="LIVE" if live else "DRY_RUN")
        if state_path.exists():
            self._load()
        else:
            self.state.mode = "LIVE" if live else "DRY_RUN"

    def _load(self) -> None:
        data = load_json(self.state_path)
        positions: dict[str, Position] = {}
        for sym, p in (data.get("positions") or {}).items():
            positions[sym] = Position(**p)
        self.state = BotState(
            cash=float(data.get("cash", self.state.cash)),
            positions=positions,
            closed_trades=list(data.get("closed_trades") or []),
            mode=str(data.get("mode") or self.state.mode),
        )

    def save(self) -> None:
        payload = {
            "cash": round(self.state.cash, 6),
            "mode": self.state.mode,
            "positions": {k: asdict(v) for k, v in self.state.positions.items()},
            "closed_trades": self.state.closed_trades[-200:],
            "updated_at": time.time(),
        }
        save_json(self.state_path, payload)

    def equity(self, prices: dict[str, float]) -> float:
        eq = self.state.cash
        for sym, pos in self.state.positions.items():
            eq += pos.qty * prices.get(sym, pos.entry)
        return eq

    def _open_buy(self, symbol: str, price: float, reason: str) -> dict[str, Any] | None:
        if symbol in self.state.positions:
            return None
        if len(self.state.positions) >= self.max_open:
            return {"type": "SKIP", "reason": "max_open_positions", "symbol": symbol}

        notional = self.state.cash * self.position_fraction
        if notional < 11 or price <= 0:
            return {"type": "SKIP", "reason": "insufficient_cash", "cash": self.state.cash}

        qty = notional / price
        fill_price = price
        exchange: dict[str, Any] | None = None

        if self.live:
            exchange = self.client.market_buy_quote(symbol, notional)
            # approximate fill from order response
            executed = float(exchange.get("executedQty") or 0)
            cumm = float(exchange.get("cummulativeQuoteQty") or notional)
            if executed > 0:
                qty = executed
                fill_price = cumm / executed
            notional = cumm

        self.state.cash -= notional
        self.state.positions[symbol] = Position(
            symbol=symbol,
            entry=fill_price,
            qty=qty,
            peak=fill_price,
            opened_at=time.time(),
            entry_reason=reason,
        )
        return {
            "type": "BUY",
            "symbol": symbol,
            "price": fill_price,
            "qty": qty,
            "notional": round(notional, 4),
            "reason": reason,
            "live": self.live,
            "exchange": exchange,
        }

    def _close_sell(self, symbol: str, price: float, reason: str) -> dict[str, Any] | None:
        pos = self.state.positions.get(symbol)
        if not pos:
            return None

        fill_price = price
        proceeds = pos.qty * price
        exchange: dict[str, Any] | None = None

        if self.live:
            exchange = self.client.market_sell_base(symbol, pos.qty)
            executed = float(exchange.get("executedQty") or pos.qty)
            cumm = float(exchange.get("cummulativeQuoteQty") or proceeds)
            if executed > 0:
                fill_price = cumm / executed
            proceeds = cumm

        pnl_pct = (fill_price - pos.entry) / pos.entry * 100
        pnl_usd = proceeds - pos.qty * pos.entry
        self.state.cash += proceeds
        trade = {
            "type": "SELL",
            "symbol": symbol,
            "entry": pos.entry,
            "exit": fill_price,
            "peak": pos.peak,
            "qty": pos.qty,
            "pnl_pct": round(pnl_pct, 4),
            "pnl_usd": round(pnl_usd, 4),
            "reason": reason,
            "opened_at": pos.opened_at,
            "closed_at": time.time(),
            "live": self.live,
            "exchange": exchange,
        }
        self.state.closed_trades.append(trade)
        del self.state.positions[symbol]
        return trade

    def on_signal(self, symbol: str, signal: Signal) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        price = signal.price
        pos = self.state.positions.get(symbol)

        if pos:
            pos.peak = max(pos.peak, price)
            pnl_pct = (price - pos.entry) / pos.entry * 100
            drop = (pos.peak - price) / pos.peak * 100 if pos.peak > 0 else 0.0

            sell_reason = None
            if pnl_pct <= -self.hard_stop_pct:
                sell_reason = f"HARD_STOP {fmt_pct(pnl_pct)}"
            elif pnl_pct >= self.take_profit_pct:
                sell_reason = f"TAKE_PROFIT {fmt_pct(pnl_pct)}"
            elif drop >= self.trail_pct and price < pos.peak:
                sell_reason = f"TRAIL_EXIT drop={drop:.3f}% pnl={fmt_pct(pnl_pct)}"
            elif signal.action == "SELL":
                sell_reason = f"SIGNAL {signal.reason} pnl={fmt_pct(pnl_pct)}"

            if sell_reason:
                ev = self._close_sell(symbol, price, sell_reason)
                if ev:
                    events.append(ev)
            else:
                events.append(
                    {
                        "type": "HOLD_POS",
                        "symbol": symbol,
                        "pnl_pct": round(pnl_pct, 4),
                        "peak": pos.peak,
                        "drop_from_peak_pct": round(drop, 4),
                        "signal": signal.action,
                        "detail": signal.reason,
                    }
                )
        elif signal.action == "BUY":
            ev = self._open_buy(symbol, price, signal.reason)
            if ev:
                events.append(ev)
        else:
            events.append(
                {
                    "type": "FLAT",
                    "symbol": symbol,
                    "signal": signal.action,
                    "detail": signal.reason,
                    "ema_fast": round(signal.ema_fast, 6),
                    "ema_slow": round(signal.ema_slow, 6),
                    "rsi": round(signal.rsi, 2),
                    "price": price,
                }
            )

        return events


# ─── main loop ───────────────────────────────────────────────────────────────


def load_config(path: Path) -> dict[str, Any]:
    return load_json(path)


def run_once(
    bot: SpotBot,
    symbols: list[str],
    *,
    interval: str,
    klines_limit: int,
    strat: dict[str, Any],
) -> dict[str, Any]:
    prices: dict[str, float] = {}
    all_events: list[dict[str, Any]] = []

    for symbol in symbols:
        candles = bot.client.klines(symbol, interval, klines_limit)
        signal = evaluate_signal(
            candles,
            ema_fast_p=int(strat.get("ema_fast", 9)),
            ema_slow_p=int(strat.get("ema_slow", 21)),
            rsi_period=int(strat.get("rsi_period", 14)),
            rsi_buy_max=float(strat.get("rsi_buy_max", 65)),
            rsi_sell_min=float(strat.get("rsi_sell_min", 70)),
        )
        prices[symbol] = signal.price
        events = bot.on_signal(symbol, signal)
        all_events.extend(events)
        for ev in events:
            tag = ev.get("type")
            if tag in {"BUY", "SELL"}:
                print(
                    f"[{bot.state.mode}] {tag} {symbol} "
                    f"px={ev.get('price') or ev.get('exit')} "
                    f"pnl={ev.get('pnl_pct', '-')} reason={ev.get('reason')}"
                )
            else:
                print(
                    f"[{bot.state.mode}] {tag} {symbol} "
                    f"px={signal.price:.6g} rsi={signal.rsi:.1f} "
                    f"{ev.get('detail') or ev.get('reason') or ''}"
                )

    eq = bot.equity(prices)
    bot.save()
    summary = {
        "mode": bot.state.mode,
        "cash": round(bot.state.cash, 4),
        "equity": round(eq, 4),
        "open_positions": list(bot.state.positions.keys()),
        "events": all_events,
        "closed_trades": len(bot.state.closed_trades),
    }
    print(
        f"--- cash={summary['cash']:.2f} USDT | equity={summary['equity']:.2f} | "
        f"open={summary['open_positions'] or '[]'} | trades={summary['closed_trades']}"
    )
    return summary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Binance Spot EMA+RSI trading bot")
    p.add_argument("--config", type=Path, default=CONFIG_PATH)
    p.add_argument("--once", action="store_true", help="Tek tur çalıştır")
    p.add_argument("--symbol", action="append", dest="symbols", help="Örn: BTCUSDT (tekrarlanabilir)")
    p.add_argument(
        "--live",
        action="store_true",
        help="Gerçek market emir (API key gerekir; withdrawal kapalı tut)",
    )
    p.add_argument("--poll", type=int, default=None, help="Poll saniye (config override)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config)
    symbols = args.symbols or list(cfg.get("symbols") or ["BTCUSDT"])
    interval = str(cfg.get("interval") or "1m")
    klines_limit = int(cfg.get("klines_limit") or 120)
    poll = int(args.poll or cfg.get("poll_seconds") or 15)
    strat = dict(cfg.get("strategy") or {})

    live_cfg = bool((cfg.get("live") or {}).get("enabled"))
    live = bool(args.live or live_cfg)

    api_key = env("BINANCE_API_KEY")
    api_secret = env("BINANCE_API_SECRET")
    recv = int((cfg.get("live") or {}).get("recv_window_ms") or 5000)

    if live:
        if not api_key or not api_secret:
            print("LIVE için BINANCE_API_KEY ve BINANCE_API_SECRET gerekli.", file=sys.stderr)
            return 2
        print("⚠️  LIVE MODE — gerçek para ile market emir gönderilecek.")
        time.sleep(2)
    else:
        print("DRY_RUN (paper) — emir gönderilmez. State:", STATE_PATH)

    client = BinanceClient(api_key=api_key, api_secret=api_secret, recv_window_ms=recv)
    bot = SpotBot(client, cfg, live=live)

    try:
        if args.once:
            run_once(bot, symbols, interval=interval, klines_limit=klines_limit, strat=strat)
            return 0
        while True:
            run_once(bot, symbols, interval=interval, klines_limit=klines_limit, strat=strat)
            time.sleep(max(5, poll))
    except KeyboardInterrupt:
        print("\nDurduruldu.")
        bot.save()
        return 0
    except (requests.HTTPError, RuntimeError, requests.RequestException) as e:
        print(f"Hata: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
