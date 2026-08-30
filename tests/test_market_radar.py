#!/usr/bin/env python3
"""Self-checks for hardened market_radar (no network except optional)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import market_radar as mr


def test_validate_config() -> None:
    cfg = mr.load_json(ROOT / "config" / "market_radar.json")
    cfg = mr.validate_config(cfg)
    assert cfg["settings"]["min_severity_to_alert"] >= 3
    print("config ok")


def test_signal_rules() -> None:
    cfg = mr.validate_config(mr.load_json(ROOT / "config" / "market_radar.json"))
    row = mr.SpotRow("ZKCUSDT", "ZKC", 0.06, 55.0, 25_000_000, 0.07, 0.04)
    fut = {"funding": -0.0012, "oi_1h_chg_pct": -4.0, "ls_ratio": 0.9, "taker_bs": 1.2}
    sigs = mr.detect_signals(row, fut, korea_quote_usdt=90_000_000, turk_quote_usdt=12_000_000, cfg=cfg)
    kinds = {s.kind for s in sigs}
    assert "CEX_FOMO" in kinds
    assert "KOREA_FOMO" in kinds
    assert "SHORT_SQUEEZE" in kinds
    # muted kind may exist but severity filter handled in cycle
    print("signals ok", sorted(kinds))


def test_safe_float() -> None:
    assert mr.safe_float("1.5") == 1.5
    assert mr.safe_float("x") is None
    assert mr.safe_float("nan") is None
    print("safe_float ok")


def test_no_squeeze_without_taker() -> None:
    cfg = mr.validate_config(mr.load_json(ROOT / "config" / "market_radar.json"))
    row = mr.SpotRow("AAAUSDT", "AAA", 1.0, 10.0, 20_000_000, 1.1, 0.9)
    fut = {"funding": -0.001, "oi_1h_chg_pct": -5.0, "ls_ratio": 1.0, "taker_bs": 0.7}
    sigs = mr.detect_signals(row, fut, 0, 0, cfg)
    assert "SHORT_SQUEEZE" not in {s.kind for s in sigs}
    print("taker gate ok")


if __name__ == "__main__":
    test_safe_float()
    test_validate_config()
    test_signal_rules()
    test_no_squeeze_without_taker()
    print("ALL SELF-CHECKS PASSED")
