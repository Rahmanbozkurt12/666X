#!/usr/bin/env python3
"""
Bookmap Python API add-on — likidite duvarı (wall) tespiti ve harici koda köprü.

Bookmap içinde çalışır (bookmap kütüphanesi yalnızca Bookmap ortamında yüklüdür).
Tespit edilen olayları JSONL dosyasına yazar; bookmap_telegram_bridge.py bu dosyayı okur.

Kurulum:
  1. Bookmap → Settings → Manage plugins → Bookmap Add-ons (L1) → Python API
  2. Bu dosyayı Bookmap'te açın veya Scripts klasörüne kopyalayın
  3. Enstrüman grafiğinde add-on'u etkinleştirin
  4. Ayrı terminalde: python bookmap_telegram_bridge.py

Not: Bookmap 7.4+, Python 3.7.14+ gerekir.

ÖNEMLİ — VS Code / terminalden ÇALIŞTIRMAYIN!
  `import bookmap` hatası normaldir; bookmap modülü yalnızca Bookmap uygulaması
  içinden script çalıştırıldığında yüklenir. Bu dosyayı Bookmap → Python API
  editöründen açıp oradan Run edin.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bookmap as bm  # type: ignore[import-not-found]  # noqa: F401 — yalnızca Bookmap içinde mevcut

_SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("BOOKMAP_ROOT", _SCRIPT_DIR))
CONFIG_PATH = ROOT / "config" / "bookmap_alerts.json"
if not CONFIG_PATH.exists():
    CONFIG_PATH = _SCRIPT_DIR / "bookmap_alerts.json"

# --- state ---
alias_to_order_book: dict[str, Any] = {}
alias_to_instrument: dict[str, dict[str, Any]] = {}
known_walls: dict[str, set[tuple[str, int]]] = {}
last_alert_at: dict[str, float] = {}
settings: dict[str, Any] = {}
req_id = 0


def load_config() -> dict[str, Any]:
    defaults = {
        "export_path": "output/bookmap_events.jsonl",
        "scan_interval_sec": 2,
        "min_wall_size": 50_000,
        "near_wall_pct": 5.0,
        "cooldown_sec": 120,
        "export_to_file": True,
    }
    if not CONFIG_PATH.exists():
        return defaults
    with CONFIG_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)
    merged = {**defaults, **(raw.get("settings") or {})}
    return merged


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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(f"[wall_alert] {event.get('type')} {event.get('alias')} {event.get('side')} "
          f"@{event.get('price')} size={event.get('size')}", flush=True)


def cooldown_key(alias: str, event_type: str, side: str, price: float) -> str:
    return f"{alias}|{event_type}|{side}|{price:.2f}"


def can_alert(key: str) -> bool:
    now = time.time()
    cooldown = float(settings.get("cooldown_sec") or 120)
    prev = last_alert_at.get(key, 0.0)
    if now - prev < cooldown:
        return False
    last_alert_at[key] = now
    return True


def levels_to_price(level: int, pips: float) -> float:
    return float(level) * pips


def levels_to_size(level: int, size_multiplier: float) -> float:
    if size_multiplier == 0:
        return float(level)
    return float(level) / size_multiplier


def get_mid_price(order_book: Any, pips: float) -> float | None:
    bbo = bm.get_bbos(order_book)
    if not bbo:
        return None
    (bid_px, _), (ask_px, _) = bbo
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
    min_wall = float(settings.get("min_wall_size") or 50_000)
    near_pct = float(settings.get("near_wall_pct") or 5.0)
    mid = get_mid_price(order_book, pips)

    current: set[tuple[str, int]] = set()
    walls: list[dict[str, Any]] = []

    for side_name, book_side in (("bid", order_book["bids"]), ("ask", order_book["asks"])):
        for price_level, size_level in book_side.items():
            size = levels_to_size(size_level, size_multiplier)
            if size < min_wall:
                continue
            price = levels_to_price(price_level, pips)
            current.add((side_name, price_level))
            distance_pct = None
            if mid and mid > 0:
                distance_pct = abs(price - mid) / mid * 100.0
            walls.append(
                {
                    "side": side_name,
                    "price": round(price, 2),
                    "size": round(size, 2),
                    "distance_pct": round(distance_pct, 2) if distance_pct is not None else None,
                }
            )

    prev = known_walls.setdefault(alias, set())
    for side, price_level in current - prev:
        price = levels_to_price(price_level, pips)
        size_level = order_book["bids" if side == "bid" else "asks"][price_level]
        size = levels_to_size(size_level, size_multiplier)
        key = cooldown_key(alias, "wall_detected", side, price)
        if can_alert(key):
            emit_event(
                {
                    "type": "wall_detected",
                    "alias": alias,
                    "side": side,
                    "price": round(price, 2),
                    "size": round(size, 2),
                    "mid_price": round(mid, 2) if mid else None,
                }
            )

    for side, price_level in prev - current:
        price = levels_to_price(price_level, pips)
        key = cooldown_key(alias, "wall_removed", side, price)
        if can_alert(key):
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
        key = cooldown_key(alias, "price_near_wall", wall["side"], wall["price"])
        if can_alert(key):
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
    addon: Any,
    alias: str,
    full_name: str,
    is_crypto: bool,
    pips: float,
    size_multiplier: float,
    instrument_multiplier: float,
    supported_features: dict[str, object],
) -> None:
    global req_id

    alias_to_instrument[alias] = {
        "alias": alias,
        "full_name": full_name,
        "pips": pips,
        "size_multiplier": size_multiplier,
        "instrument_multiplier": instrument_multiplier,
    }
    alias_to_order_book[alias] = bm.create_order_book()
    known_walls[alias] = set()

    req_id += 1
    if supported_features.get("depth"):
        bm.subscribe_to_depth(addon, alias, req_id)
        print(f"[wall_alert] depth subscribed: {alias}", flush=True)
    elif supported_features.get("mbo"):
        req_id += 1
        bm.subscribe_to_mbo(addon, alias, req_id)
        print(f"[wall_alert] mbo subscribed: {alias}", flush=True)
    else:
        print(f"[wall_alert] depth/mbo desteklenmiyor: {alias}", flush=True)

    bm.add_number_settings_parameter(
        addon, alias, "Min wall size", int(settings.get("min_wall_size") or 50_000), 1_000, 5_000_000, 1_000
    )
    bm.add_number_settings_parameter(
        addon, alias, "Near wall %", float(settings.get("near_wall_pct") or 5.0), 0.5, 20.0, 0.5
    )


def handle_unsubscribe_instrument(addon: Any, alias: str) -> None:
    alias_to_order_book.pop(alias, None)
    alias_to_instrument.pop(alias, None)
    known_walls.pop(alias, None)
    print(f"[wall_alert] unsubscribed: {alias}", flush=True)


def handle_depth_info(addon: Any, alias: str, is_bid: bool, price: int, size: int) -> None:
    order_book = alias_to_order_book.get(alias)
    if order_book is not None:
        bm.on_depth(order_book, is_bid, price, size)


def on_interval_scan(addon: Any, alias: str) -> None:
    scan_walls(addon, alias)


def on_settings_change_handler(
    addon: Any, alias: str, setting_name: str, field_type: str, new_value: Any
) -> None:
    if setting_name == "Min wall size":
        settings["min_wall_size"] = float(new_value)
    elif setting_name == "Near wall %":
        settings["near_wall_pct"] = float(new_value)


if __name__ == "__main__":
    settings.update(load_config())
    addon = bm.create_addon()
    bm.add_depth_handler(addon, handle_depth_info)
    bm.add_on_interval_handler(addon, on_interval_scan)
    bm.add_on_setting_change_handler(addon, on_settings_change_handler)
    bm.start_addon(addon, handle_subscribe_instrument, handle_unsubscribe_instrument)
    bm.wait_until_addon_is_turned_off(addon)
