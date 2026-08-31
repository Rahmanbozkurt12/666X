#!/usr/bin/env python3
"""
Raydium'da yeni açılan LP havuzlarını yakalar, Telegram'a uyarı gönderir.

Phantom bir cüzdan arayüzüdür; izlenmesi gereken katman Raydium programı ve
blockchain'deki pool adresleridir. Bu script GeckoTerminal new_pools + Raydium
API ile yeni havuzları tarar.

Kullanım:
  export TELEGRAM_BOT_TOKEN=...
  export TELEGRAM_CHAT_ID=...

  python raydium_lp_alert.py --once --dry-run
  python raydium_lp_alert.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "raydium_lp_watch.json"
STATE_PATH = ROOT / "output" / "raydium_lp_state.json"
RAYDIUM_POOL_API = "https://api-v3.raydium.io/pools/info/ids"
SOL_MINT = "So11111111111111111111111111111111111111112"


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) and value.strip() else default


def telegram_send(token: str, chat_id: str, text: str, dry_run: bool = False) -> bool:
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
        print(f"[telegram] error: {exc}", file=sys.stderr)
        return False


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_gecko_new_pools(url: str, pages: int) -> list[dict[str, Any]]:
    pools: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        try:
            r = requests.get(url, params={"page": page}, timeout=40)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"[gecko] page {page} fail: {exc}", file=sys.stderr)
            continue
        rows = data.get("data") or []
        if not rows:
            break
        pools.extend(rows)
        time.sleep(0.35)
    return pools


def fetch_raydium_pool(pool_id: str) -> dict[str, Any] | None:
    try:
        r = requests.get(RAYDIUM_POOL_API, params={"ids": pool_id}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[raydium] pool {pool_id[:12]}… fail: {exc}", file=sys.stderr)
        return None
    rows = data.get("data") or []
    return rows[0] if rows else None


def pool_tokens(attrs: dict[str, Any], included: dict[str, dict[str, Any]]) -> tuple[str, str, str, str]:
  # returns base_sym, quote_sym, base_mint, quote_mint from gecko included map when possible
    rel = attrs.get("_relationships") or {}
    base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id")
    quote_id = ((rel.get("quote_token") or {}).get("data") or {}).get("id")
    base = included.get(base_id or "", {})
    quote = included.get(quote_id or "", {})
    ba = base.get("attributes") or {}
    qa = quote.get("attributes") or {}
    return (
        ba.get("symbol") or "?",
        qa.get("symbol") or "?",
        ba.get("address") or "",
        qa.get("address") or "",
    )


def passes_filters(
    *,
    dex: str,
    created_at: datetime | None,
    reserve_usd: float,
    base_sym: str,
    quote_sym: str,
    base_mint: str,
    quote_mint: str,
    settings: dict[str, Any],
) -> bool:
    if dex not in set(settings.get("dex_allowlist") or ["raydium", "raydium-clmm"]):
        return False

    max_age = int(settings.get("max_pool_age_minutes") or 180)
    if created_at:
        age = datetime.now(timezone.utc) - created_at
        if age > timedelta(minutes=max_age):
            return False

    if reserve_usd < float(settings.get("min_reserve_usd") or 0):
        return False

    exclude = {s.upper() for s in settings.get("exclude_symbols") or []}
    if base_sym.upper() in exclude and quote_sym.upper() in exclude:
        return False

    if settings.get("sol_pairs_only"):
        mints = {base_mint, quote_mint}
        syms = {base_sym.upper(), quote_sym.upper()}
        if SOL_MINT not in mints and "SOL" not in syms and "WSOL" not in syms:
            return False

    if settings.get("require_pump_mint_suffix"):
        pumpish = base_mint.endswith("pump") or quote_mint.endswith("pump")
        if not pumpish:
            return False

    return True


def build_message(
    *,
    pool_id: str,
    dex: str,
    name: str,
    created_at: datetime | None,
    reserve_usd: float,
    base_sym: str,
    quote_sym: str,
    base_mint: str,
    quote_mint: str,
    raydium: dict[str, Any] | None,
) -> str:
    when = created_at.strftime("%Y-%m-%d %H:%M UTC") if created_at else "?"
    lines = [
        "<b>🆕 YENİ RAYDIUM LP</b>",
        f"Pair: <b>{name or f'{base_sym}/{quote_sym}'}</b>",
        f"DEX: <code>{dex}</code>",
        f"Pool: <code>{pool_id}</code>",
        f"Likidite: <b>${reserve_usd:,.2f}</b>",
        f"Açılış: {when}",
        "",
        f"Token A: <b>{base_sym}</b>",
        f"<code>{base_mint}</code>",
        f"Token B: <b>{quote_sym}</b>",
        f"<code>{quote_mint}</code>",
    ]
    if raydium:
        ma = raydium.get("mintA") or {}
        mb = raydium.get("mintB") or {}
        amt_a = raydium.get("mintAmountA")
        amt_b = raydium.get("mintAmountB")
        prog = raydium.get("programId") or ""
        lp_mint = ((raydium.get("lpMint") or {}).get("address")) or ""
        lines.extend(
            [
                "",
                f"Program: <code>{prog}</code>",
                f"Havuzda: {amt_a} {ma.get('symbol')} + {amt_b} {mb.get('symbol')}",
                f"LP mint: <code>{lp_mint}</code>",
            ]
        )
    lines.extend(
        [
            "",
            f'<a href="https://solscan.io/account/{pool_id}">Solscan pool</a> · '
            f'<a href="https://raydium.io/liquidity/increase/?pool_id={pool_id}">Raydium</a>',
        ]
    )
    return "\n".join(lines)


def poll_once(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    dry_run: bool,
    token: str | None,
    chat_id: str | None,
) -> int:
    settings = config.get("settings") or {}
    seen: set[str] = set(state.get("seen") or [])
    bootstrapped = bool(state.get("bootstrapped"))
    sent = 0

    pools = fetch_gecko_new_pools(
        settings.get("gecko_new_pools_url")
        or "https://api.geckoterminal.com/api/v2/networks/solana/new_pools",
        int(settings.get("pages_to_scan") or 2),
    )

    candidates: list[dict[str, Any]] = []
    for row in pools:
        attrs = row.get("attributes") or {}
        rel = row.get("relationships") or {}
        dex = ((rel.get("dex") or {}).get("data") or {}).get("id") or ""
        pool_id = attrs.get("address") or ""
        if not pool_id:
            continue
        created_at = parse_ts(attrs.get("pool_created_at"))
        try:
            reserve_usd = float(attrs.get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            reserve_usd = 0.0

        # Gecko list endpoint doesn't always include token details inline.
        base_sym, quote_sym = "?", "?"
        base_mint, quote_mint = "", ""
        name = attrs.get("name") or attrs.get("pool_name") or ""

        # name often "TOKEN / SOL"
        if " / " in name:
            parts = [p.strip() for p in name.split(" / ", 1)]
            if len(parts) == 2:
                base_sym, quote_sym = parts[0], parts[1]

        raydium = fetch_raydium_pool(pool_id) if dex.startswith("raydium") else None
        if raydium:
            ma = raydium.get("mintA") or {}
            mb = raydium.get("mintB") or {}
            base_sym, quote_sym = ma.get("symbol") or base_sym, mb.get("symbol") or quote_sym
            base_mint, quote_mint = ma.get("address") or "", mb.get("address") or ""

        if not passes_filters(
            dex=dex,
            created_at=created_at,
            reserve_usd=reserve_usd,
            base_sym=base_sym,
            quote_sym=quote_sym,
            base_mint=base_mint,
            quote_mint=quote_mint,
            settings=settings,
        ):
            continue

        candidates.append(
            {
                "pool_id": pool_id,
                "dex": dex,
                "name": name,
                "created_at": created_at,
                "reserve_usd": reserve_usd,
                "base_sym": base_sym,
                "quote_sym": quote_sym,
                "base_mint": base_mint,
                "quote_mint": quote_mint,
                "raydium": raydium,
            }
        )
        time.sleep(0.2)

    # oldest first for chronological alerts
    candidates.sort(key=lambda x: x["created_at"] or datetime.min.replace(tzinfo=timezone.utc))

    for item in candidates:
        pool_id = item["pool_id"]
        if pool_id in seen:
            continue
        seen.add(pool_id)
        if bootstrapped:
            msg = build_message(**item)
            if telegram_send(token or "", chat_id or "", msg, dry_run=dry_run or not (token and chat_id)):
                sent += 1
                print(f"[alert] {item['dex']} {item['base_sym']}/{item['quote_sym']} {pool_id[:12]}…")
            time.sleep(0.2)

    seen_list = list(seen)
    if len(seen_list) > 10000:
        seen_list = seen_list[-8000:]

    state["seen"] = seen_list
    state["bootstrapped"] = True
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(STATE_PATH, state)
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="Raydium yeni LP havuzu Telegram alert")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"config yok: {config_path}")

    config = load_json(config_path)
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    dry_run = bool(args.dry_run) or not (token and chat_id)
    if dry_run and not args.dry_run:
        print("[info] Telegram env yok → dry-run", file=sys.stderr)

    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"seen": [], "bootstrapped": False}
    poll_seconds = int((config.get("settings") or {}).get("poll_seconds") or 60)

    print(f"raydium lp watch | poll={poll_seconds}s | dry_run={dry_run}")
    while True:
        n = poll_once(config, state, dry_run=dry_run, token=token, chat_id=chat_id)
        print(f"[{datetime.now(timezone.utc).isoformat()}] poll done, alerts={n}")
        if args.once:
            break
        time.sleep(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
