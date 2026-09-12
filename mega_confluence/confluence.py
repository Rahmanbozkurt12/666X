#!/usr/bin/env python3
"""Aggregate 128 method votes into a single buy/sell/hold verdict."""

from __future__ import annotations

from typing import Any

from .methods import Vote, run_all_methods


def aggregate(votes: list[Vote]) -> dict[str, Any]:
    usable = [v for v in votes if v.available and v.weight > 0]
    if not usable:
        return {
            "score": 0.0,
            "bias": "HOLD",
            "action": "HOLD",
            "bull_count": 0,
            "bear_count": 0,
            "neutral_count": 0,
            "available": 0,
            "total": len(votes),
            "top_bull": [],
            "top_bear": [],
        }

    score = sum(v.score * v.weight for v in usable) / sum(v.weight for v in usable)
    bull = [v for v in usable if v.score >= 0.25]
    bear = [v for v in usable if v.score <= -0.25]
    neut = [v for v in usable if -0.25 < v.score < 0.25]

    # Action thresholds — require clear confluence to BUY
    if score >= 0.28 and len(bull) >= max(8, len(bear) + 3):
        bias, action = "BUY", "BUY"
    elif score <= -0.28 and len(bear) >= max(8, len(bull) + 3):
        bias, action = "SELL", "SELL"
    elif score >= 0.12:
        bias, action = "LEAN_BUY", "HOLD"
    elif score <= -0.12:
        bias, action = "LEAN_SELL", "HOLD"
    else:
        bias, action = "HOLD", "HOLD"

    top_bull = sorted(bull, key=lambda v: v.score * v.weight, reverse=True)[:8]
    top_bear = sorted(bear, key=lambda v: v.score * v.weight)[:8]

    return {
        "score": round(score, 4),
        "bias": bias,
        "action": action,
        "bull_count": len(bull),
        "bear_count": len(bear),
        "neutral_count": len(neut),
        "available": len(usable),
        "total": len(votes),
        "top_bull": [{"id": v.id, "name": v.name, "score": round(v.score, 3), "detail": v.detail} for v in top_bull],
        "top_bear": [{"id": v.id, "name": v.name, "score": round(v.score, 3), "detail": v.detail} for v in top_bear],
    }


def analyze_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    votes = run_all_methods(bundle)
    agg = aggregate(votes)
    last = bundle["candles"][-1]["close"]
    return {
        "symbol": bundle["symbol"],
        "interval": bundle["interval"],
        "price": last,
        "fetched_at": bundle.get("fetched_at"),
        "verdict": agg,
        "votes": [v.to_dict() for v in votes],
    }
