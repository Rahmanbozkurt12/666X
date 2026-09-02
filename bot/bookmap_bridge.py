#!/usr/bin/env python3
"""
Bookmap Canlı Likidite Analizörü — VS Code / terminal köprüsü.

Bookmap add-on (book.py) olayları JSONL dosyasına yazar; bu script o dosyayı izler.

Kurulum (sizin makine):
  Klasör:  C:\\Users\\Rahman\\OneDrive\\Desktop\\bot
  veya:    C:\\Users\\Rahman\\OneDrive\\Masaüstü\\bot

  python bookmap_bridge.py
  python bookmap_bridge.py --once
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


def bot_root() -> Path:
    return Path(__file__).resolve().parent


def candidate_event_files() -> list[Path]:
    """Rahman PC: Desktop / Masaüstü / OneDrive varyasyonları."""
    home = Path.home()
    names = [
        home / "OneDrive" / "Desktop" / "bot" / "output" / "bookmap_events.jsonl",
        home / "OneDrive" / "Masaüstü" / "bot" / "output" / "bookmap_events.jsonl",
        home / "Desktop" / "bot" / "output" / "bookmap_events.jsonl",
        home / "Masaüstü" / "bot" / "output" / "bookmap_events.jsonl",
        bot_root() / "output" / "bookmap_events.jsonl",
        bot_root() / "bookmap_events.jsonl",
    ]
    # tekilleştir
    seen: set[str] = set()
    out: list[Path] = []
    for p in names:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def resolve_events_path(cli: str | None) -> Path:
    if cli:
        return Path(cli).expanduser()
    env = os.environ.get("BOOKMAP_EXPORT_PATH")
    if env:
        return Path(env).expanduser()
    for p in candidate_event_files():
        if p.exists():
            return p
    return candidate_event_files()[0]


def side_label(side: str) -> str:
    if side == "ask":
        return "SATIŞ (direnç)"
    if side == "bid":
        return "ALIŞ (destek)"
    return side or "?"


def analyze_event(event: dict[str, Any]) -> None:
    etype = event.get("type")
    alias = event.get("alias", "?")
    side = event.get("side", "")
    price = event.get("price")
    size = event.get("size")
    mid = event.get("mid_price")
    dist = event.get("distance_pct")
    ts = event.get("ts") or datetime.now(timezone.utc).isoformat()

    if etype == "wall_detected":
        print(
            f"[{ts}] DUVAR  {alias}  {side_label(side)} @ {price}  "
            f"hacim={size}  mid={mid}"
        )
    elif etype == "wall_removed":
        print(f"[{ts}] KALKTİ  {alias}  {side_label(side)} @ {price}")
    elif etype == "price_near_wall":
        print(
            f"[{ts}] YAKIN  {alias}  {side_label(side)} @ {price}  "
            f"mesafe={dist}%  hacim={size}"
        )
    else:
        print(f"[{ts}] {etype}: {event}")


def tail_file(path: Path, *, offset: int, replay: bool) -> tuple[int, int]:
    """Yeni satırları oku. (yeni_offset, işlenen_adet) döner."""
    if not path.exists():
        return offset, 0

    size = path.stat().st_size
    if replay:
        offset = 0
    elif offset > size:
        offset = 0

    processed = 0
    with path.open("r", encoding="utf-8") as f:
        f.seek(offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            analyze_event(event)
            processed += 1
        offset = f.tell()
    return offset, processed


def main() -> int:
    parser = argparse.ArgumentParser(description="Bookmap Canlı Likidite Analizörü")
    parser.add_argument("--events", help="bookmap_events.jsonl mutlak yolu")
    parser.add_argument("--once", action="store_true", help="Tek tur oku ve çık")
    parser.add_argument("--replay", action="store_true", help="Dosyayı baştan oku")
    parser.add_argument("--poll", type=float, default=2.0, help="Bekleme saniyesi")
    args = parser.parse_args()

    events_path = resolve_events_path(args.events)
    events_path.parent.mkdir(parents=True, exist_ok=True)

    print("Bookmap Canlı Likidite Analizörü Başlatıldı...")
    print(f"Dosya izleniyor: {events_path}")
    if not events_path.exists():
        print("Bookmap veri dosyası bekleniyor...")
        print("  → Bookmap code editor'de book.py'yi Enable edin")
        print("  → Logda 'events -> ...' satırındaki yol bu dosya olmalı")

    offset = 0
    first = True
    while True:
        offset, n = tail_file(events_path, offset=offset, replay=args.replay and first)
        first = False
        if args.once:
            print(f"Bitti, olay={n}")
            return 0
        time.sleep(max(0.5, float(args.poll)))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nDurduruldu.")
        raise SystemExit(0)
