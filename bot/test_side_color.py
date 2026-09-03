#!/usr/bin/env python3
"""Alış yeşil / satış kırmızı yardımcı testleri."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from side_color import (
    diff_prefix,
    format_console_event,
    format_telegram_event,
    normalize_event,
    parse_event_line,
    recolor_jsonl_to_diff,
    side_kind,
)


class SideColorTests(unittest.TestCase):
    def test_side_kind(self) -> None:
        self.assertEqual(side_kind("bid"), "buy")
        self.assertEqual(side_kind("alis"), "buy")
        self.assertEqual(side_kind("ask"), "sell")
        self.assertEqual(side_kind("satis"), "sell")

    def test_parse_prefixed_lines(self) -> None:
        bid = parse_event_line('+{"type":"wall_detected","side":"bid","price":1}')
        ask = parse_event_line('-{"type":"wall_detected","side":"ask","price":2}')
        plain = parse_event_line('{"type":"wall_detected","side":"bid","price":3}')
        assert bid is not None and ask is not None and plain is not None
        self.assertEqual(bid["side"], "bid")
        self.assertEqual(ask["side"], "ask")
        self.assertEqual(plain["price"], 3)

    def test_telegram_colors(self) -> None:
        buy = format_telegram_event(
            {
                "type": "wall_detected",
                "alias": "BTCUSD_PERP@BNF",
                "side": "bid",
                "price": 88930,
                "size": 47888,
                "mid_price": 88950,
            }
        )
        sell = format_telegram_event(
            {
                "type": "price_near_wall",
                "alias": "BTCUSD_PERP@BNF",
                "side": "ask",
                "price": 88942.5,
                "size": 143664,
                "mid_price": 88950,
                "distance_pct": 0.01,
            }
        )
        assert buy is not None and sell is not None
        self.assertIn("🟢", buy)
        self.assertIn("ALIŞ", buy)
        self.assertIn("🔴", sell)
        self.assertIn("SATIŞ", sell)

    def test_turkish_normalize(self) -> None:
        ev = normalize_event(
            {
                "olay": "duvar_tespit",
                "sembol": "BTC",
                "yon": "satis",
                "fiyat": 100.0,
                "hacim": 50.0,
            }
        )
        self.assertEqual(ev["type"], "wall_detected")
        self.assertEqual(ev["side"], "ask")
        self.assertEqual(ev["alias"], "BTC")

    def test_console_ansi(self) -> None:
        text = format_console_event(
            {"type": "wall_removed", "alias": "X", "side": "bid", "price": 1, "ts": "t"},
            color=True,
        )
        self.assertIn("\033[32m", text)
        self.assertIn("🟢", text)

    def test_recolor_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "bookmap_events.jsonl"
            jsonl.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "wall_detected", "side": "bid", "price": 1}),
                        json.dumps({"type": "wall_detected", "side": "ask", "price": 2}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            dest, count = recolor_jsonl_to_diff(jsonl)
            self.assertEqual(count, 2)
            lines = dest.read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines[0].startswith(diff_prefix("bid")))
            self.assertTrue(lines[1].startswith(diff_prefix("ask")))


if __name__ == "__main__":
    unittest.main()
