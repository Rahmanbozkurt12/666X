#!/usr/bin/env python3
"""5m volume-spike scanner (REST)."""

from __future__ import annotations

from typing import Any

from binance_confluence.signals.base import Signal


def scan_volume_spikes(
    client: Any,
    symbols: list[str],
    *,
    interval: str = "5m",
    lookback: int = 15,
    min_volume_mult: float = 2.5,
    min_price_change: float = 0.005,
    weight: float = 1.0,
) -> list[Signal]:
    out: list[Signal] = []
    for sym in symbols:
        try:
            klines = client.get_klines(symbol=sym, interval=interval, limit=lookback)
            if not klines or len(klines) < 12:
                continue
            volumes = [float(x[5]) for x in klines]
            closes = [float(x[4]) for x in klines]
            avg_vol = sum(volumes[:-1]) / max(1, len(volumes) - 1)
            last_vol = volumes[-1]
            if avg_vol <= 0:
                continue
            mult = last_vol / avg_vol
            chg = (closes[-1] - closes[-2]) / closes[-2] if closes[-2] else 0.0
            if mult >= min_volume_mult and chg > min_price_change:
                score = weight * min(mult / min_volume_mult, 3.0)
                out.append(
                    Signal(
                        name="volume_spike",
                        symbol=sym,
                        score=score,
                        reason=f"vol_mult={mult:.2f}x price_chg={chg*100:.2f}%",
                        meta={"volume_mult": mult, "price_change": chg, "price": closes[-1]},
                    )
                )
        except Exception:
            continue
    out.sort(key=lambda s: s.score, reverse=True)
    return out
