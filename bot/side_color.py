"""Alış (bid) yeşil, satış (ask) kırmızı — JSONL / Telegram / terminal."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

BUY_SIDES = {"bid", "alis", "alış", "buy", "alım", "alim"}
SELL_SIDES = {"ask", "satis", "satış", "sell", "satim", "satım"}

TYPE_TR = {
    "duvar_tespit": "wall_detected",
    "duvar_kalkti": "wall_removed",
    "fiyat_duvara_yakin": "price_near_wall",
}

# VS Code Diff dili: + yeşil (ekleme), − kırmızı (silme)
DIFF_BUY = "+"
DIFF_SELL = "-"

ANSI_GREEN = "\033[32m"
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"


def side_kind(side: Any) -> str:
    value = str(side or "").strip().lower()
    if value in BUY_SIDES:
        return "buy"
    if value in SELL_SIDES:
        return "sell"
    return "other"


def side_emoji(side: Any) -> str:
    kind = side_kind(side)
    if kind == "buy":
        return "🟢"
    if kind == "sell":
        return "🔴"
    return "⚪"


def side_label(side: Any) -> str:
    kind = side_kind(side)
    if kind == "buy":
        return "ALIŞ (destek)"
    if kind == "sell":
        return "SATIŞ (direnç)"
    return str(side or "?")


def diff_prefix(side: Any) -> str:
    kind = side_kind(side)
    if kind == "buy":
        return DIFF_BUY
    if kind == "sell":
        return DIFF_SELL
    return " "


def diff_path_for(jsonl_path: str | Path) -> Path:
    path = Path(jsonl_path)
    if path.suffix.lower() == ".jsonl":
        return path.with_suffix(".diff")
    return path.parent / (path.name + ".diff")


def parse_event_line(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text:
        return None
    if text[0] in "+- " and len(text) > 1 and text[1] == "{":
        text = text[1:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        return data
    return None


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    out = dict(event)
    aliases = {
        "olay": "type",
        "sembol": "alias",
        "yon": "side",
        "fiyat": "price",
        "hacim": "size",
        "orta_fiyat": "mid_price",
        "mesafe_yuzde": "distance_pct",
        "zaman": "ts",
    }
    for src, dst in aliases.items():
        if dst not in out and src in event:
            out[dst] = event[src]
    kind = side_kind(out.get("side"))
    if kind == "buy":
        out["side"] = "bid"
    elif kind == "sell":
        out["side"] = "ask"
    etype = str(out.get("type") or "")
    if etype in TYPE_TR:
        out["type"] = TYPE_TR[etype]
    return out


def enable_windows_color() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def colorize(text: str, side: Any, *, enabled: bool = True) -> str:
    if not enabled:
        return text
    kind = side_kind(side)
    if kind == "buy":
        return f"{ANSI_GREEN}{text}{ANSI_RESET}"
    if kind == "sell":
        return f"{ANSI_RED}{text}{ANSI_RESET}"
    return text


def _fmt_num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value or "?")


def format_telegram_event(event: dict[str, Any]) -> str | None:
    ev = normalize_event(event)
    etype = ev.get("type")
    alias = ev.get("alias", "?")
    side = ev.get("side", "")
    price = ev.get("price")
    size = ev.get("size")
    mid = ev.get("mid_price")
    dist = ev.get("distance_pct")
    mark = side_emoji(side)
    label = side_label(side)
    kind = side_kind(side)
    if kind == "buy":
        title_side = "ALIŞ"
        title_side_lc = "alış"
    elif kind == "sell":
        title_side = "SATIŞ"
        title_side_lc = "satış"
    else:
        title_side = "DUVAR"
        title_side_lc = "duvar"

    if etype == "wall_detected":
        mid_line = f"Orta fiyat: {_fmt_num(mid)}\n" if mid not in (None, "") else ""
        size_line = f"Hacim: <b>{_fmt_num(size, 0)}</b>\n" if size not in (None, "") else ""
        return (
            f"{mark} <b>Bookmap — {title_side} duvarı</b>\n"
            f"<b>{alias}</b>\n"
            f"{mark} {label} @ <b>{_fmt_num(price)}</b>\n"
            f"{size_line}"
            f"{mid_line}"
        ).rstrip()
    if etype == "wall_removed":
        return (
            f"{mark} <b>Bookmap — {title_side} duvarı kalktı</b>\n"
            f"<b>{alias}</b>\n"
            f"{mark} {label} @ {_fmt_num(price)}"
        )
    if etype == "price_near_wall":
        dist_txt = _fmt_num(dist) if dist not in (None, "") else "?"
        size_txt = _fmt_num(size, 0) if size not in (None, "") else "?"
        mid_txt = _fmt_num(mid) if mid not in (None, "") else "?"
        return (
            f"{mark} <b>Bookmap — Fiyat {title_side_lc} duvarına yakın</b>\n"
            f"<b>{alias}</b>\n"
            f"{mark} {label} @ <b>{_fmt_num(price)}</b> (hacim {size_txt})\n"
            f"Orta: {mid_txt} | Mesafe: <b>{dist_txt}%</b>"
        )
    return None


def format_console_event(event: dict[str, Any], *, color: bool = True) -> str:
    ev = normalize_event(event)
    etype = ev.get("type")
    alias = ev.get("alias", "?")
    side = ev.get("side", "")
    price = ev.get("price")
    size = ev.get("size")
    mid = ev.get("mid_price")
    dist = ev.get("distance_pct")
    ts = ev.get("ts") or ""
    mark = side_emoji(side)
    label = side_label(side)

    if etype == "wall_detected":
        text = f"[{ts}] {mark} DUVAR  {alias}  {label} @ {price}  hacim={size}  mid={mid}"
    elif etype == "wall_removed":
        text = f"[{ts}] {mark} KALKTİ  {alias}  {label} @ {price}"
    elif etype == "price_near_wall":
        text = f"[{ts}] {mark} YAKIN  {alias}  {label} @ {price}  mesafe={dist}%  hacim={size}"
    else:
        text = f"[{ts}] {mark} {etype}: {ev}"
    return colorize(text, side, enabled=color)


def recolor_jsonl_to_diff(jsonl_path: Path, diff_path: Path | None = None) -> tuple[Path, int]:
    src = Path(jsonl_path)
    dest = Path(diff_path) if diff_path else diff_path_for(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with src.open(encoding="utf-8") as inf, dest.open("w", encoding="utf-8") as outf:
        for raw in inf:
            event = parse_event_line(raw)
            if event is None:
                continue
            ev = normalize_event(event)
            line = json.dumps(ev, ensure_ascii=False)
            outf.write(diff_prefix(ev.get("side")) + line + "\n")
            count += 1
    return dest, count


def append_diff_event(jsonl_path: str | Path, event: dict[str, Any]) -> None:
    dest = diff_path_for(jsonl_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False)
    with dest.open("a", encoding="utf-8") as f:
        f.write(diff_prefix(event.get("side")) + line + "\n")


def load_recent_events(path: Path, limit: int = 400) -> list[dict[str, Any]]:
    if not path.exists() or limit <= 0:
        return []
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for raw in f:
            event = parse_event_line(raw)
            if event is None:
                continue
            events.append(normalize_event(event))
    return events[-limit:]


def viewer_html_path() -> Path:
    return Path(__file__).resolve().parent / "events_viewer.html"
