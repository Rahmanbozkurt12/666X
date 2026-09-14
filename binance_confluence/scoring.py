#!/usr/bin/env python3
"""Aggregate multichain/CEX signals into Binance trade decisions."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from binance_confluence.signals.base import Signal


def aggregate(signals: Iterable[Signal], min_score: float) -> list[tuple[str, float, list[Signal]]]:
    by_sym: dict[str, list[Signal]] = defaultdict(list)
    for s in signals:
        by_sym[s.symbol].append(s)
    ranked: list[tuple[str, float, list[Signal]]] = []
    for sym, items in by_sym.items():
        total = sum(i.score for i in items)
        if total >= min_score:
            ranked.append((sym, total, items))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked
