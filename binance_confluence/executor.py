#!/usr/bin/env python3
"""Binance spot executor with peak-trail / stop exits (percent units)."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from binance.client import Client
from binance.exceptions import BinanceAPIException

from binance_confluence.signals.base import Position


class BinanceSpotExecutor:
    def __init__(
        self,
        client: Client,
        *,
        dry_run: bool,
        state_path: Path,
        trail_pct: float,
        stop_pct: float,
        stake_fraction: float = 0.95,
        daily_loss_limit: float = 50.0,
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        self.state_path = state_path
        self.trail_pct = trail_pct  # e.g. 0.20 means 0.20%
        self.stop_pct = stop_pct  # e.g. 0.50 means 0.50%
        self.stake_fraction = stake_fraction
        self.daily_loss_limit = daily_loss_limit
        self.positions: dict[str, Position] = {}
        self.realized_pnl = 0.0
        self.pnl_date = time.strftime("%Y-%m-%d")
        self.stop_bot = False
        self.paper_cash = 10_000.0
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        self.realized_pnl = float(data.get("realized_pnl") or 0)
        self.pnl_date = data.get("pnl_date") or self.pnl_date
        self.paper_cash = float(data.get("paper_cash") or self.paper_cash)
        for sym, p in (data.get("positions") or {}).items():
            self.positions[sym] = Position(**p)

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "dry_run": self.dry_run,
            "paper_cash": self.paper_cash,
            "realized_pnl": self.realized_pnl,
            "pnl_date": self.pnl_date,
            "positions": {k: vars(v) for k, v in self.positions.items()},
            "updated_at": time.time(),
        }
        self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _roll_pnl_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self.pnl_date != today:
            self.pnl_date = today
            self.realized_pnl = 0.0

    def usdt_free(self) -> float:
        if self.dry_run:
            return self.paper_cash
        try:
            bal = self.client.get_asset_balance(asset="USDT")
            return float((bal or {}).get("free") or 0)
        except Exception:
            return 0.0

    def _filters(self, symbol: str) -> dict[str, float]:
        info = self.client.get_symbol_info(symbol)
        out = {"min_qty": 0.0, "step_size": 0.0001, "min_notional": 5.0}
        if not info:
            return out
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                out["min_qty"] = float(f["minQty"])
                out["step_size"] = float(f["stepSize"])
            elif f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
                out["min_notional"] = float(f.get("minNotional") or f.get("notional") or 5.0)
        return out

    def _adj_qty(self, symbol: str, raw: float) -> float:
        meta = self._filters(symbol)
        step = meta["step_size"] or 0.0001
        precision = max(0, -int(math.floor(math.log10(step)))) if step > 0 else 8
        adj = math.floor(raw / step) * step
        if adj < meta["min_qty"]:
            return 0.0
        return float(round(adj, precision))

    def price(self, symbol: str) -> float | None:
        try:
            t = self.client.get_symbol_ticker(symbol=symbol)
            return float(t["price"])
        except Exception:
            return None

    def buy(self, symbol: str, reason: str) -> Position | None:
        if symbol in self.positions:
            return None
        px = self.price(symbol)
        if not px:
            return None
        usdt = self.usdt_free() * self.stake_fraction
        meta = self._filters(symbol)
        if usdt < meta["min_notional"]:
            print(f"[SKIP] {symbol}: USDT {usdt:.2f} < min_notional")
            return None
        qty = self._adj_qty(symbol, usdt / px)
        if qty <= 0:
            return None

        if self.dry_run:
            self.paper_cash -= qty * px
            print(f"[DRY BUY] {symbol} qty={qty} @ {px} | cash→{self.paper_cash:.2f} | {reason}")
        else:
            try:
                self.client.order_market_buy(symbol=symbol, quantity=qty)
                print(f"[LIVE BUY] {symbol} qty={qty} @ ~{px} | {reason}")
            except BinanceAPIException as e:
                print(f"[BUY FAIL] {symbol}: {e}")
                return None

        pos = Position(
            symbol=symbol,
            entry_price=px,
            quantity=qty,
            peak_price=px,
            opened_at=time.time(),
            dry_run=self.dry_run,
        )
        self.positions[symbol] = pos
        self.save()
        return pos

    def sell(self, symbol: str, reason: str) -> None:
        pos = self.positions.get(symbol)
        if not pos:
            return
        px = self.price(symbol) or pos.entry_price
        if self.dry_run:
            self.paper_cash += pos.quantity * px
            print(f"[DRY SELL] {symbol} qty={pos.quantity} @ {px} | cash→{self.paper_cash:.2f} | {reason}")
        else:
            try:
                self.client.order_market_sell(symbol=symbol, quantity=pos.quantity)
                print(f"[LIVE SELL] {symbol} qty={pos.quantity} @ ~{px} | {reason}")
            except BinanceAPIException as e:
                print(f"[SELL FAIL] {symbol}: {e}")
                return
        pnl = (px - pos.entry_price) * pos.quantity
        self._roll_pnl_day()
        self.realized_pnl += pnl
        print(f"[PNL] {symbol} trade={pnl:.4f} USDT | day={self.realized_pnl:.4f}")
        if self.daily_loss_limit > 0 and self.realized_pnl <= -abs(self.daily_loss_limit):
            print("[KRİTİK] Günlük zarar limiti — bot duracak.")
            self.stop_bot = True
        del self.positions[symbol]
        self.save()

    def manage_exits(self) -> None:
        """Peak trail + entry stop. Units: percent (0.20 = 0.20%)."""
        for symbol, pos in list(self.positions.items()):
            px = self.price(symbol)
            if not px:
                continue
            pos.peak_price = max(pos.peak_price, px)
            pnl_pct = (px - pos.entry_price) / pos.entry_price * 100
            drop_peak = (pos.peak_price - px) / pos.peak_price * 100
            reason = None
            if pnl_pct <= -abs(self.stop_pct):
                reason = f"STOP_LOSS {pnl_pct:.3f}%"
            elif px < pos.peak_price and round(drop_peak, 4) >= round(abs(self.trail_pct), 4):
                reason = f"TRAIL_EXIT peak_drop={drop_peak:.4f}% pnl={pnl_pct:+.3f}%"
            print(
                f"[HOLD] {symbol} px={px:.6g} peak={pos.peak_price:.6g} "
                f"pnl={pnl_pct:+.3f}% peak↓={drop_peak:.3f}%"
            )
            if reason:
                self.sell(symbol, reason)
            else:
                self.save()
