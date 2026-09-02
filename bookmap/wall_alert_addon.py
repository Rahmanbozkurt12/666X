#!/usr/bin/env python3
"""
Bookmap wall alert — Build ile jar yapin, VS Code'dan CALISTIRMAYIN.

Kurulum:
  1. Configure add-ons -> Python API tik -> Open embedded editor
  2. Bu dosyayi yapistir -> Save -> Build (Is trading strategy KAPALI)
  3. File -> Open build folder -> .jar
  4. Configure add-ons -> eski bot Remove -> Add... yeni jar -> Allow -> mavi tik
  5. VS Code: python bookmap_telegram_bridge.py --dry-run
"""

from __future__ import annotations

import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bookmap as bm  # type: ignore

_SCRIPT_DIR = Path(__file__).resolve().parent
_CANDIDATE_ROOTS = [
    Path(os.environ["BOOKMAP_ROOT"]) if os.environ.get("BOOKMAP_ROOT") else None,
    Path.home() / "OneDrive" / "Desktop" / "bot",
    Path.home() / "Desktop" / "bot",
    _SCRIPT_DIR,
]
ROOT = next((p for p in _CANDIDATE_ROOTS if p is not None and (p.exists() or p == _SCRIPT_DIR)), _SCRIPT_DIR)

alias_to_order_book: dict[str, Any] = {}
alias_to_instrument: dict[str, dict[str, Any]] = {}
known_walls: dict[str, set[tuple[str, int]]] = {}
last_alert_at: dict[str, float] = {}
settings: dict[str, Any] = {
    "export_path": "output/bookmap_events.jsonl",
    "scan_interval_sec": 2,
    "min_wall_size": 5.0,  # kripto icin dusuk; sonra artirin
    "near_wall_pct": 5.0,
    "cooldown_sec": 60,
    "export_to_file": True,
}
req_id = 0


def export_path() -> Path:
    rel = settings.get("export_path") or "output/bookmap_events.jsonl"
    path = Path(rel)
    if not path.is_absolute():
        path = ROOT / path
    return path


def emit_event(event: dict[str, Any]) -> None:
    if not settings.get("export_to_file", True):
        return
    event.setdefault("ts", datetime.now(timezone.utc).isoformat())
    path = export_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(
            "[wall_alert] wrote %s -> %s" % (event.get("type"), path),
            flush=True,
        )
    except Exception:
        traceback.print_exc()
        print("[wall_alert] WRITE FAILED path=%s" % path, flush=True)


def cooldown_ok(key: str) -> bool:
    now = time.time()
    cooldown = float(settings.get("cooldown_sec") or 60)
    prev = last_alert_at.get(key, 0.0)
    if now - prev < cooldown:
        return False
    last_alert_at[key] = now
    return True


def levels_to_price(level: int, pips: float) -> float:
    return float(level) * pips


def levels_to_size(level: int, size_multiplier: float) -> float:
    if not size_multiplier:
        return float(level)
    return float(level) / float(size_multiplier)


def get_mid_price(order_book: Any, pips: float):
    try:
        bbo = bm.get_bbo(order_book)  # NOT get_bbos
    except Exception:
        traceback.print_exc()
        return None
    if bbo is None:
        return None
    try:
        bid_side, ask_side = bbo
    except Exception:
        return None
    if not bid_side or not ask_side:
        return None
    bid_px, _ = bid_side
    ask_px, _ = ask_side
    if bid_px is None or ask_px is None:
        return None
    return (levels_to_price(bid_px, pips) + levels_to_price(ask_px, pips)) / 2.0


def scan_walls(addon: Any, alias: str) -> None:
    order_book = alias_to_order_book.get(alias)
    instrument = alias_to_instrument.get(alias)
    if not order_book or not instrument:
        return

    pips = instrument["pips"]
    size_multiplier = instrument["size_multiplier"]
    min_wall = float(settings.get("min_wall_size") or 5.0)
    near_pct = float(settings.get("near_wall_pct") or 5.0)
    mid = get_mid_price(order_book, pips)

    current = set()
    walls = []

    for side_name, book_side in (("bid", order_book["bids"]), ("ask", order_book["asks"])):
        # SortedDict: items() destekler
        for price_level, size_level in list(book_side.items()):
            size = levels_to_size(size_level, size_multiplier)
            if size < min_wall:
                continue
            price = levels_to_price(price_level, pips)
            current.add((side_name, int(price_level)))
            distance_pct = None
            if mid and mid > 0:
                distance_pct = abs(price - mid) / mid * 100.0
            walls.append(
                {
                    "side": side_name,
                    "price": round(price, 2),
                    "size": round(size, 4),
                    "distance_pct": round(distance_pct, 2) if distance_pct is not None else None,
                }
            )

    prev = known_walls.setdefault(alias, set())
    for side, price_level in current - prev:
        price = levels_to_price(price_level, pips)
        size_level = order_book["bids" if side == "bid" else "asks"][price_level]
        size = levels_to_size(size_level, size_multiplier)
        key = "%s|wall_detected|%s|%.2f" % (alias, side, price)
        if cooldown_ok(key):
            emit_event(
                {
                    "type": "wall_detected",
                    "alias": alias,
                    "side": side,
                    "price": round(price, 2),
                    "size": round(size, 4),
                    "mid_price": round(mid, 2) if mid else None,
                }
            )

    for side, price_level in prev - current:
        price = levels_to_price(price_level, pips)
        key = "%s|wall_removed|%s|%.2f" % (alias, side, price)
        if cooldown_ok(key):
            emit_event(
                {
                    "type": "wall_removed",
                    "alias": alias,
                    "side": side,
                    "price": round(price, 2),
                }
            )

    known_walls[alias] = current

    if mid is None:
        return
    for wall in walls:
        dist = wall.get("distance_pct")
        if dist is None or dist > near_pct:
            continue
        key = "%s|price_near_wall|%s|%.2f" % (alias, wall["side"], wall["price"])
        if cooldown_ok(key):
            emit_event(
                {
                    "type": "price_near_wall",
                    "alias": alias,
                    "side": wall["side"],
                    "price": wall["price"],
                    "size": wall["size"],
                    "mid_price": round(mid, 2),
                    "distance_pct": dist,
                }
            )


def handle_subscribe_instrument(
    addon,
    alias,
    full_name,
    is_crypto,
    pips,
    size_multiplier,
    instrument_multiplier,
    supported_features,
):
    global req_id
    print("[wall_alert] subscribe %s crypto=%s ROOT=%s" % (alias, is_crypto, ROOT), flush=True)
    print("[wall_alert] export -> %s" % export_path(), flush=True)

    alias_to_instrument[alias] = {
        "alias": alias,
        "full_name": full_name,
        "pips": pips,
        "size_multiplier": size_multiplier,
        "instrument_multiplier": instrument_multiplier,
    }
    alias_to_order_book[alias] = bm.create_order_book()
    known_walls[alias] = set()

    emit_event(
        {
            "type": "addon_started",
            "alias": alias,
            "export_path": str(export_path()),
            "min_wall_size": settings.get("min_wall_size"),
        }
    )

    req_id += 1
    depth_ok = False
    try:
        depth_ok = bool(supported_features.get("depth"))
    except Exception:
        depth_ok = True

    if depth_ok:
        bm.subscribe_to_depth(addon, alias, req_id)
        print("[wall_alert] depth subscribed: %s" % alias, flush=True)
    else:
        print("[wall_alert] depth yok: %s features=%s" % (alias, supported_features), flush=True)

    try:
        bm.add_number_settings_parameter(
            addon, alias, "Min wall size", float(settings["min_wall_size"]), 0.1, 5_000_000.0, 0.1
        )
        bm.add_number_settings_parameter(
            addon, alias, "Near wall %", float(settings["near_wall_pct"]), 0.1, 50.0, 0.1
        )
    except Exception:
        traceback.print_exc()


def handle_unsubscribe_instrument(addon, alias):
    alias_to_order_book.pop(alias, None)
    alias_to_instrument.pop(alias, None)
    known_walls.pop(alias, None)
    print("[wall_alert] unsubscribed: %s" % alias, flush=True)


def handle_depth_info(addon, alias, is_bid, price, size):
    order_book = alias_to_order_book.get(alias)
    if order_book is not None:
        bm.on_depth(order_book, is_bid, price, size)


def on_interval_scan(addon, alias):
    try:
        scan_walls(addon, alias)
    except Exception:
        traceback.print_exc()


def on_settings_change_handler(addon, alias, setting_name, field_type, new_value):
    if setting_name == "Min wall size":
        settings["min_wall_size"] = float(new_value)
    elif setting_name == "Near wall %":
        settings["near_wall_pct"] = float(new_value)
    print("[wall_alert] setting %s=%s" % (setting_name, new_value), flush=True)


if __name__ == "__main__":
    print("[wall_alert] starting ROOT=%s" % ROOT, flush=True)
    addon = bm.create_addon()
    bm.add_depth_handler(addon, handle_depth_info)
    bm.add_on_interval_handler(addon, on_interval_scan)
    bm.add_on_setting_change_handler(addon, on_settings_change_handler)
    bm.start_addon(addon, handle_subscribe_instrument, handle_unsubscribe_instrument)
    bm.wait_until_addon_is_turned_off(addon)
