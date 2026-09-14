#!/usr/bin/env python3
"""Shared signal types for multichain → Binance confluence bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Signal:
    name: str
    symbol: str  # Binance symbol e.g. ETHUSDT
    score: float  # weighted contribution before global weight
    reason: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    symbol: str
    entry_price: float
    quantity: float
    peak_price: float
    opened_at: float
    dry_run: bool = True
