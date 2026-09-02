#!/usr/bin/env python3
"""
Bookmap add-on'un yazdığı JSONL olaylarını okuyup Telegram'a gönderir.

Bookmap add-on (bookmap/wall_alert_addon.py) varsayılan olarak
  ~/Documents/666X/output/bookmap_events.jsonl
dosyasına yazar; bu script o dosyayı (veya --events ile verilen yolu) tail eder.

Kullanım:
  export TELEGRAM_BOT_TOKEN=...
  export TELEGRAM_CHAT_ID=...
  python bookmap_telegram_bridge.py
  python bookmap_telegram_bridge.py --dry-run
  python bookmap_telegram_bridge.py --events "C:/Users/Rahman/Documents/666X/output/bookmap_events.jsonl"
  python bookmap_telegram_bridge.py --replay   # mevcut dosyayı baştan oku (test)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "bookmap_alerts.json"
DEFAULT_EVENTS = ROOT / "output" / "bookmap_events.jsonl"
STATE_PATH = ROOT / "output" / "bookmap_bridge_state.json"


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def candidate_event_paths() -> list[Path]:
    home = Path.home()
    return [
        home / "Documents" / "666X" / "output" / "bookmap_events.jsonl",
        home / "666X" / "output" / "bookmap_events.jsonl",
        home / "OneDrive" / "Documents" / "666X" / "output" / "bookmap_events.jsonl",
        DEFAULT_EVENTS,
        ROOT / "bookmap" / "output" / "bookmap_events.jsonl",
    ]


def resolve_events_path(cli_path: str | None, cfg: dict[str, Any], settings: dict[str, Any]) -> Path:
    env_path = env("BOOKMAP_EXPORT_PATH")
    if cli_path:
        return Path(cli_path).expanduser()
    if env_path:
        return Path(env_path).expanduser()
    configured = (cfg.get("events_path") or settings.get("export_path") or "").strip()
    if configured:
        p = Path(configured).expanduser()
        return p if p.is_absolute() else ROOT / p
    for candidate in candidate_event_paths():
        if candidate.exists():
            return candidate
    # Prefer Documents path even if not created yet (addon creates it)
    return candidate_event_paths()[0]


def telegram_send(token: str, chat_id: str, text: str, *, dry_run: bool) -> bool:
    if dry_run:
        print("--- DRY-RUN TELEGRAM ---\n" + text + "\n------------------------")
        return True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            print(f"[telegram] HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return False
        return True
    except requests.RequestException as exc:
        print(f"[telegram] {exc}", file=sys.stderr)
        return False


def side_label(side: str) -> str:
    if side == "ask":
        return "SATIŞ (direnç)"
    if side == "bid":
        return "ALIŞ (destek)"
    return side


def format_event(event: dict[str, Any]) -> str | None:
    etype = event.get("type")
    alias = event.get("alias", "?")
    side = event.get("side", "")
    price = event.get("price")
    size = event.get("size")
    mid = event.get("mid_price")
    dist = event.get("distance_pct")

    if etype == "wall_detected":
        mid_line = f"Mid: {mid:,.2f}\n" if mid else ""
        return (
            f"🧱 <b>Bookmap — Likidite duvarı</b>\n"
            f"<b>{alias}</b>\n"
            f"{side_label(side)} @ <b>{price:,.2f}</b>\n"
            f"Hacim: <b>{size:,.0f}</b>\n"
            f"{mid_line}"
        ).rstrip()
    if etype == "wall_removed":
        return (
            f"↩️ <b>Bookmap — Duvar kalktı</b>\n"
            f"<b>{alias}</b>\n"
            f"{side_label(side)} @ {price:,.2f}"
        )
    if etype == "price_near_wall":
        return (
            f"⚠️ <b>Bookmap — Fiyat duvara yakın</b>\n"
            f"<b>{alias}</b>\n"
            f"{side_label(side)} @ <b>{price:,.2f}</b> (hacim {size:,.0f})\n"
            f"Mid: {mid:,.2f} | Mesafe: <b>{dist:.2f}%</b>"
        )
    return None


def event_fingerprint(event: dict[str, Any]) -> str:
    parts = [
        event.get("type", ""),
        event.get("alias", ""),
        str(event.get("side", "")),
        str(event.get("price", "")),
        str(event.get("size", "")),
        event.get("ts", ""),
    ]
    return "|".join(parts)


def load_config() -> tuple[dict[str, Any], dict[str, Any]]:
    if not CONFIG_PATH.exists():
        return (
            {"poll_seconds": 3, "alert_types": ["wall_detected", "wall_removed", "price_near_wall"]},
            {},
        )
    raw = load_json(CONFIG_PATH)
    return raw.get("telegram_bridge") or {}, raw.get("settings") or {}


def tail_events(
    path: Path,
    *,
    state: dict[str, Any],
    dry_run: bool,
    token: str | None,
    chat_id: str | None,
    allowed_types: set[str],
    replay: bool,
) -> int:
    sent = 0
    seen: set[str] = set(state.get("seen") or [])
    offset = 0 if replay else int(state.get("offset") or 0)

    if not path.exists():
        return 0

    size = path.stat().st_size
    if offset > size:
        offset = 0

    with path.open(encoding="utf-8") as f:
        f.seek(offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") not in allowed_types:
                continue
            fp = event_fingerprint(event)
            if fp in seen:
                continue
            msg = format_event(event)
            if not msg:
                continue
            if token and chat_id and telegram_send(token, chat_id, msg, dry_run=dry_run):
                sent += 1
                seen.add(fp)
                print(f"[{datetime.now(timezone.utc).isoformat()}] alert: {event.get('type')} {event.get('alias')}")
            elif dry_run and telegram_send("", "", msg, dry_run=True):
                sent += 1
                seen.add(fp)
        offset = f.tell()

    state["offset"] = offset
    state["seen"] = list(seen)[-5000:]
    state["events_path"] = str(path)
    save_json(STATE_PATH, state)
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="Bookmap JSONL → Telegram köprüsü")
    parser.add_argument("--dry-run", action="store_true", help="Telegram gönderme, konsola yaz")
    parser.add_argument("--replay", action="store_true", help="Dosyayı baştan oku")
    parser.add_argument("--once", action="store_true", help="Tek tur oku ve çık")
    parser.add_argument("--events", help="bookmap_events.jsonl mutlak yolu")
    args = parser.parse_args()

    cfg, settings = load_config()
    events_path = resolve_events_path(args.events, cfg, settings)

    poll_seconds = int(cfg.get("poll_seconds") or 3)
    allowed_types = set(cfg.get("alert_types") or ["wall_detected", "wall_removed", "price_near_wall"])

    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    dry_run = bool(args.dry_run) or not (token and chat_id)
    if dry_run and not args.dry_run:
        print("[info] Telegram env yok → dry-run", file=sys.stderr)

    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"offset": 0, "seen": []}
    print(f"watching {events_path} | poll={poll_seconds}s | dry_run={dry_run}")
    if not events_path.exists():
        print(
            "[info] Dosya henüz yok. Bookmap add-on Enable edilince oluşur.\n"
            "       Bookmap logunda 'events -> ...' satırındaki yolu --events ile verin.",
            file=sys.stderr,
        )

    total = 0
    while True:
        n = tail_events(
            events_path,
            state=state,
            dry_run=dry_run,
            token=token,
            chat_id=chat_id,
            allowed_types=allowed_types,
            replay=args.replay and total == 0,
        )
        total += n
        if args.once:
            break
        time.sleep(poll_seconds)

    print(f"done, alerts={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
