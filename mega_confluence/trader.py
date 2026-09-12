#!/usr/bin/env python3
"""
Paper trader with hard -1% stop and trailing peak exit.

Rules (as requested):
  - BUY when confluence says BUY and flat
  - While long: track peak price
  - Hard stop: PnL <= -1% → SELL (cut loss)
  - Trail: once price drops from peak by trail_pct → SELL (lock gains / exit turn)
  - No averaging down; one position at a time
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Position:
    symbol: str
    entry: float
    qty: float
    peak: float
    opened_at: float
    entry_score: float
    entry_bias: str


@dataclass
class TraderState:
    cash: float
    position: Position | None = None
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)


class PaperTrader:
    def __init__(
        self,
        *,
        starting_cash: float = 10_000.0,
        hard_stop_pct: float = 1.0,
        trail_pct: float = 0.45,
        position_fraction: float = 0.95,
        state_path: Path | None = None,
    ) -> None:
        self.hard_stop_pct = hard_stop_pct
        self.trail_pct = trail_pct
        self.position_fraction = position_fraction
        self.state_path = state_path
        self.state = TraderState(cash=starting_cash)
        if state_path and state_path.exists():
            self._load()

    def _load(self) -> None:
        assert self.state_path is not None
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        pos = data.get("position")
        self.state = TraderState(
            cash=float(data.get("cash", 10_000)),
            position=Position(**pos) if pos else None,
            closed_trades=list(data.get("closed_trades") or []),
            equity_curve=list(data.get("equity_curve") or []),
        )

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cash": self.state.cash,
            "position": asdict(self.state.position) if self.state.position else None,
            "closed_trades": self.state.closed_trades,
            "equity_curve": self.state.equity_curve[-500:],
            "updated_at": time.time(),
        }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def mark_equity(self, price: float) -> float:
        eq = self.state.cash
        if self.state.position:
            eq += self.state.position.qty * price
        self.state.equity_curve.append({"t": time.time(), "equity": eq, "price": price})
        return eq

    def on_tick(self, symbol: str, price: float, verdict: dict[str, Any]) -> dict[str, Any]:
        """Evaluate buy/sell on latest price + confluence verdict."""
        events: list[dict[str, Any]] = []
        action = verdict.get("action")
        bias = verdict.get("bias")
        score = float(verdict.get("score") or 0)

        pos = self.state.position
        if pos and pos.symbol != symbol:
            # switch not allowed while in position
            events.append({"type": "SKIP", "reason": f"open position in {pos.symbol}"})
            return {"events": events, "position": asdict(pos), "cash": self.state.cash}

        # Manage open position first
        if pos:
            pos.peak = max(pos.peak, price)
            pnl_pct = (price - pos.entry) / pos.entry * 100
            drop_from_peak = (pos.peak - price) / pos.peak * 100

            sell_reason = None
            if pnl_pct <= -self.hard_stop_pct:
                sell_reason = f"HARD_STOP pnl={pnl_pct:.3f}% <= -{self.hard_stop_pct}%"
            elif drop_from_peak >= self.trail_pct and price < pos.peak:
                # "düşmeye başladığı an" — peak'ten trail kadar geri çekilince sat
                sell_reason = (
                    f"TRAIL_EXIT peak={pos.peak:.6g} drop={drop_from_peak:.3f}% "
                    f"pnl={pnl_pct:.3f}%"
                )
            elif action == "SELL" and pnl_pct > 0:
                sell_reason = f"CONFLUENCE_SELL score={score:.3f} pnl={pnl_pct:.3f}%"

            if sell_reason:
                proceeds = pos.qty * price
                self.state.cash += proceeds
                trade = {
                    "symbol": pos.symbol,
                    "side": "SELL",
                    "entry": pos.entry,
                    "exit": price,
                    "peak": pos.peak,
                    "qty": pos.qty,
                    "pnl_pct": round(pnl_pct, 4),
                    "pnl_usd": round(proceeds - pos.qty * pos.entry, 4),
                    "reason": sell_reason,
                    "opened_at": pos.opened_at,
                    "closed_at": time.time(),
                }
                self.state.closed_trades.append(trade)
                events.append({"type": "SELL", **trade})
                self.state.position = None
            else:
                events.append(
                    {
                        "type": "HOLD_POS",
                        "pnl_pct": round(pnl_pct, 4),
                        "peak": pos.peak,
                        "drop_from_peak_pct": round(drop_from_peak, 4),
                    }
                )

        # Flat → maybe buy
        if self.state.position is None and action == "BUY":
            notional = self.state.cash * self.position_fraction
            if notional > 10 and price > 0:
                qty = notional / price
                self.state.cash -= qty * price
                self.state.position = Position(
                    symbol=symbol,
                    entry=price,
                    qty=qty,
                    peak=price,
                    opened_at=time.time(),
                    entry_score=score,
                    entry_bias=str(bias),
                )
                events.append(
                    {
                        "type": "BUY",
                        "symbol": symbol,
                        "price": price,
                        "qty": qty,
                        "notional": round(notional, 4),
                        "score": score,
                        "bias": bias,
                    }
                )

        equity = self.mark_equity(price)
        self.save()
        return {
            "events": events,
            "position": asdict(self.state.position) if self.state.position else None,
            "cash": round(self.state.cash, 4),
            "equity": round(equity, 4),
            "hard_stop_pct": self.hard_stop_pct,
            "trail_pct": self.trail_pct,
        }
