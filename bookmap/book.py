#!/usr/bin/env python3
"""
Bookmap Python API add-on — likidite duvarı tespiti.

SADECE Bookmap code editor icinde calisir (VS Code'dan DEGIL).
Bookmap -> Save / Enable on a Live instrument chart.

Olaylar su dosyaya yazilir (bridge ayni yolu izler):
  C:\\Users\\Rahman\\OneDrive\\Desktop\\bot\\output\\bookmap_events.jsonl
  (Masaüstü yolu da denenir)
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime

import bookmap as bm  # type: ignore[import-not-found]

try:
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _SCRIPT_DIR = os.getcwd()


def _home():
    return os.path.expanduser("~") or os.environ.get("USERPROFILE") or ""


def _default_exports():
    home = _home()
    return [
        os.path.join(home, "OneDrive", "Desktop", "bot", "output", "bookmap_events.jsonl"),
        os.path.join(home, "OneDrive", "Masaüstü", "bot", "output", "bookmap_events.jsonl"),
        os.path.join(home, "Desktop", "bot", "output", "bookmap_events.jsonl"),
        os.path.join(home, "Masaüstü", "bot", "output", "bookmap_events.jsonl"),
        os.path.join(_SCRIPT_DIR, "output", "bookmap_events.jsonl"),
    ]


alias_to_order_book = {}
alias_to_mbo_book = {}
alias_to_instrument = {}
known_walls = {}
last_alert_at = {}
settings = {
    "export_path": "",
    "min_wall_size": 50000,
    "near_wall_pct": 5.0,
    "cooldown_sec": 120,
    "export_to_file": True,
}
req_id = 0
_export_path_cache = None
_export_error_logged = False
_last_scan_at = {}


def _utc_now_iso():
    try:
        from datetime import timezone
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return datetime.utcnow().isoformat() + "Z"


def load_config():
    defaults = dict(settings)
    candidates = [
        os.path.join(_SCRIPT_DIR, "bookmap_alerts.json"),
        os.path.join(_home(), "OneDrive", "Desktop", "bot", "bookmap_alerts.json"),
        os.path.join(_home(), "OneDrive", "Masaüstü", "bot", "bookmap_alerts.json"),
        os.path.join(_home(), "Desktop", "bot", "bookmap_alerts.json"),
        os.path.join(_home(), "Masaüstü", "bot", "bookmap_alerts.json"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                merged = dict(defaults)
                merged.update(raw.get("settings") or raw)
                print("[book] config: " + path, flush=True)
                return merged
            except Exception as exc:
                print("[book] config fail %s: %s" % (path, exc), flush=True)
    return defaults


def resolve_export_path():
    global _export_path_cache
    if _export_path_cache:
        return _export_path_cache

    candidates = []
    env_path = os.environ.get("BOOKMAP_EXPORT_PATH")
    configured = (settings.get("export_path") or "").strip()
    if env_path:
        candidates.append(env_path)
    if configured:
        candidates.append(configured)
    candidates.extend(_default_exports())

    last_err = None
    for path in candidates:
        try:
            folder = os.path.dirname(path)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            with open(path, "a", encoding="utf-8") as f:
                f.write("")
            _export_path_cache = path
            print("[book] export path: " + path, flush=True)
            return path
        except Exception as exc:
            last_err = exc
    raise IOError("export yazilamiyor: %s" % last_err)


def notify(addon, alias, message):
    print("[book] " + message, flush=True)
    try:
        if addon is not None and alias:
            bm.send_user_message(addon, alias, "[book] " + message)
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
            "[book] %s %s %s @%s size=%s"
            % (event.get("type"), event.get("alias"), event.get("side"), event.get("price"), event.get("size")),
            flush=True,
        )
    except Exception as exc:
        if not _export_error_logged:
            _export_error_logged = True
            notify(addon, alias or event.get("alias"), "dosya yazma hatasi: %s" % exc)


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
    """Bookmap get_bbos bazen None doner — unpack etmeden once kontrol et."""
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
        bid_px, _ = bid_side
        ask_px, _ = ask_side
    except Exception:
        return None
    if bid_px is None or ask_px is None:
        return None
    return (levels_to_price(bid_px, pips) + levels_to_price(ask_px, pips)) / 2.0


def _iter_side(book_side):
    try:
        return list(book_side.items())
    except Exception:
        return []


def mbp_book_for(alias):
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

        req_id += 1
        bm.subscribe_to_depth(addon, alias, req_id)
        notify(addon, alias, "depth subscribed: %s" % alias)

        try:
            req_id += 1
            alias_to_mbo_book[alias] = bm.create_mbo_book()
            bm.subscribe_to_mbo(addon, alias, req_id)
            notify(addon, alias, "mbo subscribed: %s" % alias)
        except Exception as exc:
            alias_to_mbo_book.pop(alias, None)
            print("[book] mbo skip: %s" % exc, flush=True)

        bm.add_number_settings_parameter(
            addon, alias, "Min wall size", float(settings.get("min_wall_size") or 50000), 1000, 5000000, 1000
        )
        bm.add_number_settings_parameter(
            addon, alias, "Near wall %", float(settings.get("near_wall_pct") or 5.0), 0.5, 20.0, 0.5
        )
        try:
            bm.add_string_settings_parameter(addon, alias, "Export path", _default_exports()[0])
        except Exception:
            pass

        try:
            path = resolve_export_path()
            notify(addon, alias, "ready — events -> %s" % path)
        except Exception as exc:
            notify(addon, alias, "export path hatasi: %s" % exc)
    except Exception:
        print("[book] subscribe error:\n" + traceback.format_exc(), flush=True)


def handle_unsubscribe_instrument(addon, alias):
    alias_to_order_book.pop(alias, None)
    alias_to_mbo_book.pop(alias, None)
    alias_to_instrument.pop(alias, None)
    known_walls.pop(alias, None)
    print("[book] unsubscribed: %s" % alias, flush=True)


def handle_depth_info(addon, alias, is_bid, price, size):
    try:
        order_book = alias_to_order_book.get(alias)
        if order_book is not None:
            bm.on_depth(order_book, is_bid, price, size)
    except Exception:
        print("[book] depth error:\n" + traceback.format_exc(), flush=True)


def handle_mbo_info(addon, alias, event_type, order_id, price, size):
    """Resmi imza: (addon, alias, event_type, order_id, price, size) — bm.on_mbo YOK."""
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
        print("[book] mbo error:\n" + traceback.format_exc(), flush=True)


def on_interval_scan(addon, alias):
    """Bookmap her 0.1 sn cagirir; scan_interval_sec ile seyreltiyoruz."""
    try:
        interval = float(settings.get("scan_interval_sec") or 2.0)
        now = time.time()
        prev = _last_scan_at.get(alias, 0.0)
        if now - prev < interval:
            return
        _last_scan_at[alias] = now
        scan_walls(addon, alias)
    except Exception:
        print("[book] scan error:\n" + traceback.format_exc(), flush=True)


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
        print("[book] settings error:\n" + traceback.format_exc(), flush=True)


if __name__ == "__main__":
    try:
        settings.update(load_config())
        addon = bm.create_addon()
        bm.add_depth_handler(addon, handle_depth_info)
        try:
            bm.add_mbo_handler(addon, handle_mbo_info)
        except Exception as exc:
            print("[book] mbo handler yok: %s" % exc, flush=True)
        # NOT: 3. arguman YOK — interval Bookmap tarafinda sabit ~0.1sn
        bm.add_on_interval_handler(addon, on_interval_scan)
        bm.add_on_setting_change_handler(addon, on_settings_change_handler)
        bm.start_addon(addon, handle_subscribe_instrument, handle_unsubscribe_instrument)
        print("[book] addon started — Enable on Live chart", flush=True)
        bm.wait_until_addon_is_turned_off(addon)
    except Exception:
        print("[book] FATAL:\n" + traceback.format_exc(), flush=True)
        sys.exit(1)
