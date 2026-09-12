#!/usr/bin/env python3
"""
Behavioral reconstruction of the target Solana wallet trading loop.

This is NOT the closed-source bot behind A6PS…Evbot.
It mirrors the on-chain flow we fingerprinted:

  listen → filter → Pump.fun AMM buy → manage → sell → FAST* fee + tip

Live trading is OFF. No private key required.

Usage:
  python3 solana_pump_behavior_bot.py --once
  python3 solana_pump_behavior_bot.py --simulate-trade
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "output" / "solana_pump_behavior_state.json"
CFG_PATH = ROOT / "config" / "solana_target_wallet.json"

PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
METEORA_DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"


@dataclass
class PaperPosition:
    mint: str
    entry_sol: float
    size_tokens: float
    entry_ts: float
    peak_mult: float = 1.0


@dataclass
class BotState:
    cash_sol: float = 10.0
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    closed: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "DRY_RUN"


@dataclass
class StrategyParams:
    buy_sol: float = 0.05
    take_profit_mult: float = 1.35
    stop_loss_mult: float = 0.85
    max_hold_sec: float = 90.0
    trail_drop: float = 0.12
    fee_rate: float = 0.01
    tip_sol: float = 0.001


def save_state(path: Path, state: BotState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cash_sol": state.cash_sol,
        "mode": state.mode,
        "positions": {k: asdict(v) for k, v in state.positions.items()},
        "closed": state.closed[-200:],
        "updated_at": time.time(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_state(path: Path) -> BotState:
    if not path.exists():
        return BotState()
    data = json.loads(path.read_text(encoding="utf-8"))
    positions = {k: PaperPosition(**v) for k, v in (data.get("positions") or {}).items()}
    return BotState(
        cash_sol=float(data.get("cash_sol", 10.0)),
        positions=positions,
        closed=list(data.get("closed") or []),
        mode=str(data.get("mode") or "DRY_RUN"),
    )


def filter_candidate(event: dict[str, Any]) -> tuple[bool, str]:
    """Private alpha lives here and is NOT readable from chain. Skeleton only."""
    if event.get("is_honeypot"):
        return False, "honeypot_flag"
    if float(event.get("dev_sell_pct") or 0) > 30:
        return False, "dev_dump_risk"
    if float(event.get("age_sec") or 0) > 120:
        return False, "too_old_for_snipe"
    if float(event.get("liquidity_sol") or 0) < 5:
        return False, "low_liquidity"
    return True, "pass"


def simulate_buy(
    state: BotState, params: StrategyParams, mint: str, px_sol: float
) -> dict[str, Any]:
    cost = params.buy_sol
    fee = cost * params.fee_rate
    tip = params.tip_sol
    total = cost + fee + tip
    if state.cash_sol < total:
        return {"type": "SKIP", "reason": "insufficient_sol"}
    if mint in state.positions:
        return {"type": "SKIP", "reason": "already_in"}

    tokens = cost / max(px_sol, 1e-12)
    state.cash_sol -= total
    state.positions[mint] = PaperPosition(
        mint=mint,
        entry_sol=px_sol,
        size_tokens=tokens,
        entry_ts=time.time(),
        peak_mult=1.0,
    )
    return {
        "type": "BUY",
        "venue": "pump.fun_amm",
        "program": PUMP_AMM,
        "mint": mint,
        "spend_sol": cost,
        "fee_sol": round(fee, 6),
        "tip_sol": tip,
        "tokens": tokens,
        "note": "DRY_RUN — would call BuyExactQuoteIn + tip + FAST* fee transfer",
    }


def simulate_manage(
    state: BotState, params: StrategyParams, mint: str, px_sol: float
) -> dict[str, Any] | None:
    pos = state.positions.get(mint)
    if not pos:
        return None

    mult = px_sol / pos.entry_sol
    pos.peak_mult = max(pos.peak_mult, mult)
    held = time.time() - pos.entry_ts
    drop_from_peak = (pos.peak_mult - mult) / pos.peak_mult if pos.peak_mult else 0.0

    reason = None
    if mult >= params.take_profit_mult:
        reason = f"TAKE_PROFIT x{mult:.3f}"
    elif mult <= params.stop_loss_mult:
        reason = f"STOP_LOSS x{mult:.3f}"
    elif drop_from_peak >= params.trail_drop and mult < pos.peak_mult:
        reason = f"TRAIL peak={pos.peak_mult:.3f} now={mult:.3f}"
    elif held >= params.max_hold_sec:
        reason = f"TIME_EXIT {held:.0f}s"

    if not reason:
        return {
            "type": "HOLD",
            "mint": mint,
            "mult": round(mult, 4),
            "peak": round(pos.peak_mult, 4),
            "held_sec": round(held, 1),
        }

    proceeds = pos.size_tokens * px_sol
    fee = proceeds * params.fee_rate
    state.cash_sol += proceeds - fee
    trade = {
        "type": "SELL",
        "venue": "pump.fun_amm_or_migrated_dex",
        "program_candidates": [PUMP_AMM, RAYDIUM_CPMM, METEORA_DLMM],
        "mint": mint,
        "entry": pos.entry_sol,
        "exit": px_sol,
        "mult": round(mult, 4),
        "proceeds_sol": round(proceeds, 6),
        "fee_sol": round(fee, 6),
        "reason": reason,
        "held_sec": round(held, 1),
    }
    state.closed.append(trade)
    del state.positions[mint]
    return trade


def demo_events() -> list[dict[str, Any]]:
    now = time.time()
    return [
        {
            "mint": "DemoMint1111111111111111111111111111111111",
            "px_sol": 0.00001,
            "age_sec": 8,
            "liquidity_sol": 12,
            "dev_sell_pct": 0,
            "is_honeypot": False,
            "ts": now,
        },
        {
            "mint": "DemoMint2222222222222222222222222222222222",
            "px_sol": 0.00002,
            "age_sec": 200,
            "liquidity_sol": 40,
            "dev_sell_pct": 5,
            "is_honeypot": False,
            "ts": now,
        },
    ]


def architecture_banner() -> None:
    print(
        """
╔══════════════════════════════════════════════════════════════╗
║  RECONSTRUCTED ARCHITECTURE (from on-chain fingerprints)    ║
╠══════════════════════════════════════════════════════════════╣
║  Listener  → Pump.fun logs / gRPC create+trade stream        ║
║  Filter    → PRIVATE (not recoverable from chain)            ║
║  Executor  → BuyExactQuoteIn (pAMMBay) + CU + tip            ║
║  Risk      → TP / SL / time / trail (inferred)               ║
║  Exit      → Sell or Raydium CPMM / Meteora DLMM             ║
║  Fee       → SOL transfer to FAST* vanity wallets            ║
║                                                              ║
║  Source code unavailable — behavior clone only (DRY_RUN).    ║
╚══════════════════════════════════════════════════════════════╝
"""
    )


def run_once(simulate_trade: bool) -> dict[str, Any]:
    architecture_banner()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8")) if CFG_PATH.exists() else {}
    state = load_state(STATE_PATH)
    params = StrategyParams()
    events: list[dict[str, Any]] = []

    print("Target wallet:", cfg.get("target_wallet"))
    print(
        "Reconstructed loop:",
        json.dumps(cfg.get("reconstructed_loop"), indent=2, ensure_ascii=False),
    )
    print(f"Mode={state.mode} cash={state.cash_sol:.4f} SOL open={list(state.positions)}\n")

    if not simulate_trade:
        print("No trade simulation. Pass --simulate-trade for paper buy/sell demo.")
        save_state(STATE_PATH, state)
        return {"mode": state.mode, "cash_sol": state.cash_sol}

    for ev in demo_events():
        ok, why = filter_candidate(ev)
        print(f"candidate {ev['mint'][:12]}… filter={ok} ({why})")
        if not ok:
            continue
        buy = simulate_buy(state, params, ev["mint"], float(ev["px_sol"]))
        events.append(buy)
        print(" ", buy)
        if buy.get("type") != "BUY":
            continue

        for m in (1.1, 1.25, 1.4, 1.32, 1.2):
            px = float(ev["px_sol"]) * m
            out = simulate_manage(state, params, ev["mint"], px)
            events.append(out or {})
            print(" ", out)
            if out and out.get("type") == "SELL":
                break
        break

    save_state(STATE_PATH, state)
    summary = {
        "mode": state.mode,
        "cash_sol": round(state.cash_sol, 6),
        "open": list(state.positions),
        "closed_trades": len(state.closed),
        "events": events,
    }
    print("\n---", summary)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--simulate-trade", action="store_true")
    args = ap.parse_args()
    run_once(simulate_trade=bool(args.simulate_trade))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
