#!/usr/bin/env python3
"""
Bookmap Canlı Likidite Analizörü — VS Code / terminal köprüsü.

Alış (bid) yeşil, satış (ask) kırmızı.

Kurulum (sizin makine):
  Klasör:  C:\\Users\\Rahman\\OneDrive\\Desktop\\bot
  veya:    C:\\Users\\Rahman\\OneDrive\\Masaüstü\\bot

  python bookmap_bridge.py
  python bookmap_bridge.py --once
  python bookmap_bridge.py --boya      # mevcut JSONL'i VS Code'da yeşil/kırmızı yap
  python bookmap_bridge.py --viewer    # tarayıcıda renkli canlı görünüm
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from side_color import (
    diff_path_for,
    enable_windows_color,
    format_console_event,
    load_recent_events,
    parse_event_line,
    recolor_jsonl_to_diff,
    viewer_html_path,
)


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


def analyze_event(event: dict[str, Any], *, color: bool) -> None:
    print(format_console_event(event, color=color), flush=True)


def tail_file(path: Path, *, offset: int, replay: bool, color: bool) -> tuple[int, int]:
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
            event = parse_event_line(line)
            if event is None:
                continue
            analyze_event(event, color=color)
            processed += 1
        offset = f.tell()
    return offset, processed


def run_viewer(events_path: Path, host: str, port: int) -> int:
    html_path = viewer_html_path()
    if not html_path.is_file():
        print(f"viewer HTML yok: {html_path}", file=sys.stderr)
        return 1

    page = html_path.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if parsed.path == "/api/events":
                limit = 400
                raw_limit = parse_qs(parsed.query).get("limit", ["400"])[0]
                try:
                    limit = max(1, min(2000, int(raw_limit)))
                except ValueError:
                    pass
                payload = json.dumps(load_recent_events(events_path, limit), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_error(404)

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"Renkli görünüm: {url}")
    print(f"Kaynak: {events_path}")
    print("Alış yeşil, satış kırmızı. Durdurmak için Ctrl+C.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDurduruldu.")
    finally:
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bookmap Canlı Likidite Analizörü")
    parser.add_argument("--events", help="bookmap_events.jsonl mutlak yolu")
    parser.add_argument("--once", action="store_true", help="Tek tur oku ve çık")
    parser.add_argument("--replay", action="store_true", help="Dosyayı baştan oku")
    parser.add_argument("--poll", type=float, default=2.0, help="Bekleme saniyesi")
    parser.add_argument("--boya", action="store_true", help="JSONL'i VS Code yeşil/kırmızı .diff dosyasına çevir")
    parser.add_argument("--viewer", action="store_true", help="Tarayıcıda renkli canlı görünüm")
    parser.add_argument("--port", type=int, default=8765, help="--viewer portu")
    parser.add_argument("--no-color", action="store_true", help="Terminal renklerini kapat")
    args = parser.parse_args()

    events_path = resolve_events_path(args.events)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    color = not args.no_color and sys.stdout.isatty()
    if color:
        enable_windows_color()

    if args.boya:
        if not events_path.exists():
            print(f"JSONL yok: {events_path}", file=sys.stderr)
            return 1
        dest, count = recolor_jsonl_to_diff(events_path)
        print(f"{count} satır boyandı → {dest}")
        print("VS Code'da bu .diff dosyasını açın: alış yeşil (+), satış kırmızı (-).")
        return 0

    if args.viewer:
        return run_viewer(events_path, "127.0.0.1", int(args.port))

    print("Bookmap Canlı Likidite Analizörü Başlatıldı...")
    print("Alış = yeşil  |  Satış = kırmızı")
    print(f"Dosya izleniyor: {events_path}")
    print(f"VS Code renkli kopya: {diff_path_for(events_path)}  (python bookmap_bridge.py --boya)")
    if not events_path.exists():
        print("Bookmap veri dosyası bekleniyor...")
        print("  → Bookmap code editor'de book.py'yi Enable edin")
        print("  → Logda 'events -> ...' satırındaki yol bu dosya olmalı")

    offset = 0
    first = True
    while True:
        offset, n = tail_file(
            events_path, offset=offset, replay=args.replay and first, color=color
        )
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
