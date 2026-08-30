#!/usr/bin/env python3
"""Minimal self-checks for market_radar signal rules (no network)."""

from __future__ import annotations

import market_radar as mr


def test_squeeze_and_fomo() -> None:
    cfg = {
        "settings": {
            "fomo_chg_pct": 8.0,
            "min_quote_volume_usdt": 2_000_000,
            "korea_lead_ratio": 1.15,
            "squeeze_funding_lt": -0.0003,
            "squeeze_oi_drop_pct": 1.5,
            "squeeze_price_up_pct": 1.0,
            "dump_risk_chg_pct": 12.0,
        }
    }
    row = mr.SpotRow("ZKCUSDT", "ZKC", 0.06, 55.0, 20_000_000, 0.07, 0.04)
    fut = {"funding": -0.001, "oi_1h_chg_pct": -3.0, "ls_ratio": 0.95, "taker_bs": 1.1}
    sigs = mr.detect_signals(row, fut, korea_quote_usdt=80_000_000, turk_quote_usdt=5_000_000, cfg=cfg)
    kinds = {s.kind for s in sigs}
    assert "CEX_FOMO" in kinds
    assert "KOREA_FOMO" in kinds
    assert "SHORT_SQUEEZE" in kinds
    assert "CEX_ORDERBOOK" in kinds
    print("ok", sorted(kinds))


if __name__ == "__main__":
    test_squeeze_and_fomo()
