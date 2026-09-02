#!/usr/bin/env python3
"""
Bookmap Python API add-on — likidite duvarı (wall) tespiti.

SADECE Bookmap icinde calisir. VS Code / terminalden calistirmayin.
  Bookmap -> Settings -> Manage plugins -> Bookmap Add-ons (L1) -> Python API
  Bu dosyayi Bookmap Python API editorunden acip Enable edin.

Olaylar varsayilan olarak Documents/666X/output/bookmap_events.jsonl dosyasina yazilir.
Ayni dosyayi bookmap_telegram_bridge.py okur.
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime

try:
    from datetime import timezone
except ImportError:  # very old Python
    timezone = None  # type: ignore

import bookmap as bm  # type: ignore[import-not-found]

# --- paths (Bookmap often copies the script under C:\Bookmap\Python\tmp\...) ---
try:
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _SCRIPT_DIR = os.getcwd()


def _home_dir():
    return os.path.expanduser("~") or os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""


def _default_export_candidates():
    home = _home_dir()
    docs = os.path.join(home, "Documents", "666X", "output", "bookmap_events.jsonl")
    home_repo = os.path.join(home, "666X", "output", "bookmap_events.jsonl")
    onedrive = os.path.join(home, "OneDrive", "Documents", "666X", "output", "bookmap_events.jsonl")
    script_out = os.path.join(_SCRIPT_DIR, "output", "bookmap_events.jsonl")
    return [docs, home_repo, onedrive, script_out]


def _is_bookmap_temp(path):
    norm = path.replace("/", "\\").lower()
    return "\\bookmap\\python\\" in norm or "/bookmap/python/" in path.lower()


# --- state ---
alias_to_order_book = {}
alias_to_mbo_book = {}
alias_to_instrument = {}
known_walls = {}
last_alert_at = {}
settings = {
    "export_path": "",
    "scan_interval_sec": 2,
    "min_wall_size": 50000,
    "near_wall_pct": 5.0,
    "cooldown_sec": 120,
    "export_to_file": True,
}
req_id = 0
_export_path_cache = None
_export_error_logged = False


def _utc_now_iso():
    if timezone is not None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.utcnow().isoformat() + "Z"


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config():
    defaults = dict(settings)
    candidates = []
    env_root = os.environ.get("BOOKMAP_ROOT")
    if env_root:
        candidates.append(os.path.join(env_root, "config", "bookmap_alerts.json"))
    candidates.append(os.path.join(_SCRIPT_DIR, "config", "bookmap_alerts.json"))
    candidates.append(os.path.join(_SCRIPT_DIR, "bookmap_alerts.json"))
    # repo layout when script lives in bookmap/
    parent = os.path.dirname(_SCRIPT_DIR)
    candidates.append(os.path.join(parent, "config", "bookmap_alerts.json"))
    for home_sub in (
        os.path.join(_home_dir(), "Documents", "666X", "config", "bookmap_alerts.json"),
        os.path.join(_home_dir(), "666X", "config", "bookmap_alerts.json"),
    ):
        candidates.append(home_sub)

    for path in candidates:
        if path and os.path.isfile(path):
            try:
                raw = _load_json(path)
                merged = dict(defaults)
                merged.update(raw.get("settings") or {})
                print("[wall_alert] config loaded: " + path, flush=True)
                return merged
            except Exception as exc:
                print("[wall_alert] config read failed: %s (%s)" % (path, exc), flush=True)
    print("[wall_alert] config not found, using defaults", flush=True)
    return defaults


def resolve_export_path():
    global _export_path_cache
    if _export_path_cache:
        return _export_path_cache

    env_path = os.environ.get("BOOKMAP_EXPORT_PATH")
    configured = (settings.get("export_path") or "").strip()
    candidates = []
    if env_path:
        candidates.append(env_path)
    if configured:
        if os.path.isabs(configured):
            candidates.append(configured)
        else:
            env_root = os.environ.get("BOOKMAP_ROOT")
            if env_root:
                candidates.append(os.path.join(env_root, configured))
            if not _is_bookmap_temp(_SCRIPT_DIR):
                # script in repo (…/666X/bookmap) → resolve against repo root
                candidates.append(os.path.join(os.path.dirname(_SCRIPT_DIR), configured))
                candidates.append(os.path.join(_SCRIPT_DIR, configured))
    candidates.extend(_default_export_candidates())

    last_err = None
    for path in candidates:
        try:
            folder = os.path.dirname(path)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            # probe write
            with open(path, "a", encoding="utf-8") as f:
                f.write("")
            _export_path_cache = path
            print("[wall_alert] export path: " + path, flush=True)
            return path
        except Exception as exc:
            last_err = exc
            continue
    raise IOError("export path yazilamiyor: %s" % last_err)


def notify(addon, alias, message):
    print("[wall_alert] " + message, flush=True)
    try:
        if addon is not None and alias:
            bm.send_user_message(addon, alias, "[wall_alert] " + message)
    except Exception:
        pass


def emit_event(event, addon=None, alias=None):
    global _export_error_logged
    if not settings.get("export_to_file", True):
        return
    event.setdefault("ts", _utc_now_iso())
    try:
        path = resolve_export_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(
            "[wall_alert] %s %s %s @%s size=%s"
            % (
                event.get("type"),
                event.get("alias"),
                event.get("side"),
                event.get("price"),
                event.get("size"),
            ),
            flush=True,
        )
    except Exception as exc:
        if not _export_error_logged:
            _export_error_logged = True
            notify(
                addon,
                alias or event.get("alias"),
                "dosya yazma hatasi: %s — Bookmap ayarinda Export path'i mutlak yol yapin" % exc,
            )


def cooldown_key(alias, event_type, side, price):
    return "%s|%s|%s|%.2f" % (alias, event_type, side, float(price))


def can_alert(key):
    now = time.time()
    cooldown = float(settings.get("cooldown_sec") or 120)
    prev = last_alert_at.get(key, 0.0)
    if now - prev < cooldown:
        return False
    last_alert_at[key] = now
    return True


def levels_to_price(level, pips):
    return float(level) * float(pips)


def levels_to_size(level, size_multiplier):
    sm = float(size_multiplier) if size_multiplier else 0.0
    if sm == 0:
        return float(level)
    return float(level) / sm


def get_mid_price(order_book, pips):
    """Safe BBO read — Bookmap can return None for a side before book is ready."""
    try:
        bbo = bm.get_bbos(order_book)
    except Exception:
        return None
    if not bbo:
        return None
    try:
        bid_side, ask_side = bbo
    except Exception:
        return None
    if bid_side is None or ask_side is None:
        return None
    try:
        bid_px, _bid_sz = bid_side
        ask_px, _ask_sz = ask_side
    except Exception:
        return None
    if bid_px is None or ask_px is None:
        return None
    return (levels_to_price(bid_px, pips) + levels_to_price(ask_px, pips)) / 2.0


def _iter_side(book_side):
    # SortedDict supports .items(); copy keys to avoid mutation during iterate
    try:
        return list(book_side.items())
    except Exception:
        return []


def mbp_book_for(alias):
    """Prefer depth book if it has levels; otherwise fall back to MBO aggregated book."""
    depth = alias_to_order_book.get(alias)
    if depth is not None:
        try:
            if depth["bids"] or depth["asks"]:
                return depth
        except Exception:
            pass
    mbo = alias_to_mbo_book.get(alias)
    if mbo is not None:
        return mbo.get("mbp_book") or depth
    return depth


def scan_walls(addon, alias):
    order_book = mbp_book_for(alias)
    instrument = alias_to_instrument.get(alias)
    if not order_book or not instrument:
        return

    pips = instrument["pips"]
    size_multiplier = instrument["size_multiplier"]
    min_wall = float(settings.get("min_wall_size") or 50000)
    near_pct = float(settings.get("near_wall_pct") or 5.0)
    mid = get_mid_price(order_book, pips)

    current = set()
    walls = []

    for side_name, book_side in (("bid", order_book["bids"]), ("ask", order_book["asks"])):
        for price_level, size_level in _iter_side(book_side):
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
                },
                addon=addon,
                alias=alias,
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
                },
                addon=addon,
                alias=alias,
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
                },
                addon=addon,
                alias=alias,
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
    try:
        alias_to_instrument[alias] = {
            "alias": alias,
            "full_name": full_name,
            "pips": pips,
            "size_multiplier": size_multiplier,
            "instrument_multiplier": instrument_multiplier,
        }
        alias_to_order_book[alias] = bm.create_order_book()
        known_walls[alias] = set()

        # Always subscribe to depth like official examples.
        # supported_features can be incomplete / empty and wrongly skip data.
        req_id += 1
        bm.subscribe_to_depth(addon, alias, req_id)
        notify(addon, alias, "depth subscribed: %s" % alias)

        # Also try MBO — some providers only deliver MBO (not aggregated depth).
        try:
            req_id += 1
            alias_to_mbo_book[alias] = bm.create_mbo_book()
            bm.subscribe_to_mbo(addon, alias, req_id)
            notify(addon, alias, "mbo also subscribed: %s" % alias)
        except Exception as exc:
            alias_to_mbo_book.pop(alias, None)
            print("[wall_alert] mbo subscribe skipped: %s" % exc, flush=True)

        bm.add_number_settings_parameter(
            addon,
            alias,
            "Min wall size",
            float(settings.get("min_wall_size") or 50000),
            1000,
            5000000,
            1000,
        )
        bm.add_number_settings_parameter(
            addon,
            alias,
            "Near wall %",
            float(settings.get("near_wall_pct") or 5.0),
            0.5,
            20.0,
            0.5,
        )
        try:
            default_export = settings.get("export_path") or _default_export_candidates()[0]
            bm.add_string_settings_parameter(addon, alias, "Export path", str(default_export))
        except Exception as exc:
            print("[wall_alert] Export path setting skipped: %s" % exc, flush=True)

        # probe export early so user sees path / errors in Bookmap log
        try:
            path = resolve_export_path()
            notify(addon, alias, "ready — events -> %s" % path)
        except Exception as exc:
            notify(addon, alias, "export path hatasi: %s" % exc)
    except Exception:
        print("[wall_alert] subscribe error:\n" + traceback.format_exc(), flush=True)


def handle_unsubscribe_instrument(addon, alias):
    alias_to_order_book.pop(alias, None)
    alias_to_mbo_book.pop(alias, None)
    alias_to_instrument.pop(alias, None)
    known_walls.pop(alias, None)
    print("[wall_alert] unsubscribed: %s" % alias, flush=True)


def handle_depth_info(addon, alias, is_bid, price, size):
    try:
        order_book = alias_to_order_book.get(alias)
        if order_book is not None:
            bm.on_depth(order_book, is_bid, price, size)
    except Exception:
        print("[wall_alert] depth error:\n" + traceback.format_exc(), flush=True)


def handle_mbo(addon, alias, event_type, order_id, price, size):
    """Official signature: (addon, alias, event_type, order_id, price, size)."""
    try:
        mbo = alias_to_mbo_book.get(alias)
        if mbo is None:
            return
        et = str(event_type).upper() if event_type is not None else ""
        if et == "ASK_NEW":
            bm.on_new_order(mbo, order_id, False, price, size)
        elif et == "BID_NEW":
            bm.on_new_order(mbo, order_id, True, price, size)
        elif et == "REPLACE":
            bm.on_replace_order(mbo, order_id, price, size)
        elif et == "CANCEL":
            bm.on_remove_order(mbo, order_id)
    except Exception:
        print("[wall_alert] mbo error:\n" + traceback.format_exc(), flush=True)


def on_interval_scan(addon, alias):
    try:
        scan_walls(addon, alias)
    except Exception:
        print("[wall_alert] scan error:\n" + traceback.format_exc(), flush=True)


def on_settings_change_handler(addon, alias, setting_name, field_type, new_value):
    global _export_path_cache, _export_error_logged
    try:
        if setting_name == "Min wall size":
            settings["min_wall_size"] = float(new_value)
        elif setting_name == "Near wall %":
            settings["near_wall_pct"] = float(new_value)
        elif setting_name == "Export path":
            settings["export_path"] = str(new_value).strip()
            _export_path_cache = None
            _export_error_logged = False
            try:
                path = resolve_export_path()
                notify(addon, alias, "export path updated: %s" % path)
            except Exception as exc:
                notify(addon, alias, "export path hatasi: %s" % exc)
    except Exception:
        print("[wall_alert] settings error:\n" + traceback.format_exc(), flush=True)


if __name__ == "__main__":
    try:
        settings.update(load_config())
        addon = bm.create_addon()
        bm.add_depth_handler(addon, handle_depth_info)
        try:
            bm.add_mbo_handler(addon, handle_mbo)
        except Exception as exc:
            print("[wall_alert] mbo handler not available: %s" % exc, flush=True)
        bm.add_on_interval_handler(addon, on_interval_scan)
        bm.add_on_setting_change_handler(addon, on_settings_change_handler)
        bm.start_addon(addon, handle_subscribe_instrument, handle_unsubscribe_instrument)
        print("[wall_alert] addon started — Enable it on an instrument chart", flush=True)
        bm.wait_until_addon_is_turned_off(addon)
    except Exception:
        print("[wall_alert] FATAL:\n" + traceback.format_exc(), flush=True)
        sys.exit(1)
