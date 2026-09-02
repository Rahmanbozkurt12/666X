#!/usr/bin/env python3
"""
Yeni veya aktif cüzdan keşfi — eski Binance Dep etiketli cüzdanlar DEĞİL.

Mantık:
  1. Son N saatte FET transferlerini tara (Blockscout, ücretsiz)
  2. Alıcı cüzdanları topla
  3. Cüzdan yaşı (ilk tx) + son aktivite filtrele
  4. CEX/DEX router etiketlilerini ele
  5. output/active_wallets.json kaydet → accumulation_alert bunu kullanır

Kullanım:
  python active_wallet_discovery.py
  python active_wallet_discovery.py --max-age-days 60 --activity-hours 48
  python active_wallet_discovery.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "active_wallet_discovery.json"
DEFAULT_OUT = ROOT / "output" / "active_wallets.json"
CONTRACT_CACHE = ROOT / "output" / "contract_cache.json"
WATCHED_PATH = ROOT / "config" / "watched_wallets.json"
ETH_LABELS = "https://eth-labels.com/labels"
ZERO = "0x0000000000000000000000000000000000000000"


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def blockscout_get(api: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        r = requests.get(api, params=params, timeout=45)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[api] fail {params.get('action')}: {exc}", file=sys.stderr)
        return []
    result = data.get("result")
    if not isinstance(result, list):
        return []
    return result


def load_router_blocklist() -> set[str]:
    block: set[str] = set()
    if not WATCHED_PATH.exists():
        return block
    cfg = load_json(WATCHED_PATH)
    for key in ("dex_routers_ethereum", "dex_routers_bsc"):
        for addr in cfg.get(key) or []:
            block.add(str(addr).lower())
    return block


def is_labeled_cex(address: str) -> bool:
    try:
        r = requests.get(f"{ETH_LABELS}/{address}", timeout=15)
        if r.status_code != 200:
            return False
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return False
        for row in rows:
            label = (row.get("label") or "").lower()
            tag = (row.get("nameTag") or "").lower()
            if any(
                k in label or k in tag
                for k in ("exchange", "binance", "coinbase", "okx", "kraken", "cex", "hot wallet", "deposit")
            ):
                return True
    except (requests.RequestException, ValueError):
        return False
    return False


def wallet_first_tx_ts(api: str, address: str) -> int | None:
    rows = blockscout_get(
        api,
        {
            "module": "account",
            "action": "txlist",
            "address": address,
            "page": 1,
            "offset": 1,
            "sort": "asc",
        },
    )
    if not rows:
        return None
    try:
        return int(rows[0].get("timeStamp") or 0)
    except (TypeError, ValueError):
        return None


def wallet_age_days(first_ts: int | None, now: int) -> float | None:
    if not first_ts:
        return None
    return (now - first_ts) / 86400


def fetch_recent_token_receivers(
    api: str,
    contract: str,
    *,
    pages: int,
    page_size: int,
    since_ts: int,
) -> dict[str, dict[str, Any]]:
    """Alıcı cüzdanlar ve toplam alınan miktar."""
    receivers: dict[str, dict[str, Any]] = {}
    for page in range(1, pages + 1):
        rows = blockscout_get(
            api,
            {
                "module": "account",
                "action": "tokentx",
                "contractaddress": contract,
                "page": page,
                "offset": page_size,
                "sort": "desc",
            },
        )
        if not rows:
            break
        stop = False
        for tx in rows:
            try:
                ts = int(tx.get("timeStamp") or 0)
            except (TypeError, ValueError):
                ts = 0
            if ts < since_ts:
                stop = True
                break
            to_addr = (tx.get("to") or "").lower()
            if not to_addr or to_addr == ZERO:
                continue
            try:
                raw = int(tx.get("value") or 0)
            except (TypeError, ValueError):
                raw = 0
            if raw <= 0:
                continue
            bucket = receivers.setdefault(
                to_addr,
                {"received_raw": 0, "tx_count": 0, "last_ts": 0, "first_seen_ts": ts},
            )
            bucket["received_raw"] += raw
            bucket["tx_count"] += 1
            bucket["last_ts"] = max(bucket["last_ts"], ts)
            bucket["first_seen_ts"] = min(bucket["first_seen_ts"], ts)
        if stop:
            break
        time.sleep(0.25)
    return receivers


def load_token_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    exclude = {s.upper() for s in (config.get("exclude_symbols") or ["FET"])}
    settings = config.get("settings") or {}
    tokens: list[dict[str, Any]] = []

    for row in config.get("tokens") or []:
        sym = (row.get("symbol") or "").upper()
        if sym in exclude:
            continue
        tokens.append(row)

    if settings.get("use_contract_cache") and CONTRACT_CACHE.exists():
        cache = load_json(CONTRACT_CACHE)
        for sym, entries in cache.items():
            if sym.upper() in exclude:
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if (entry.get("chain") or "") not in ("ethereum", "eth"):
                    continue
                contract = entry.get("contract")
                if not contract:
                    continue
                tokens.append(
                    {
                        "symbol": sym.upper(),
                        "contract": contract,
                        "decimals": 18,
                        "chain": "ethereum",
                    }
                )
                break

    # dedupe by contract
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for t in tokens:
        c = (t.get("contract") or "").lower()
        if c in seen:
            continue
        seen.add(c)
        unique.append(t)
    return unique


def passes_mode(
    *,
    mode: str,
    age_days: float | None,
    max_age_days: float,
    min_age_days: float,
    active_in_window: bool,
) -> bool:
    is_new = age_days is not None and min_age_days <= age_days <= max_age_days
    if mode == "new_only":
        return is_new
    if mode == "active_only":
        return active_in_window
    # new_or_active
    return is_new or active_in_window


def discover(config: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    settings = config.get("settings") or {}
    api = settings.get("explorer_api") or "https://eth.blockscout.com/api"
    token_list = load_token_list(config)
    if not token_list:
        raise ValueError("taranacak token yok (FET hariç liste boş)")

    activity_hours = int(settings.get("activity_hours") or 72)
    max_age_days = float(settings.get("max_wallet_age_days") or 90)
    min_age_days = float(settings.get("min_wallet_age_days") or 0)
    min_received = float(settings.get("min_token_received") or settings.get("min_fet_received") or 50)
    max_received = float(settings.get("max_token_received") or settings.get("max_fet_received") or 0)
    hard_max_age = float(settings.get("hard_max_age_days") or 0)
    max_wallets = int(settings.get("max_wallets") or 1000)
    mode = settings.get("mode") or "new_or_active"
    exclude_cex = bool(settings.get("exclude_labeled_cex", True))
    exclude_routers = bool(settings.get("exclude_dex_routers", True))

    now = int(datetime.now(timezone.utc).timestamp())
    since_ts = now - activity_hours * 3600
    routers = load_router_blocklist() if exclude_routers else set()

    all_candidates: dict[str, dict[str, Any]] = {}
    scanned_tokens: list[str] = []

    for token in token_list:
        contract = (token.get("contract") or "").lower()
        symbol = token.get("symbol") or "TOKEN"
        decimals = int(token.get("decimals") or 18)
        min_received_raw = int(min_received * (10**decimals))
        max_received_raw = int(max_received * (10**decimals)) if max_received > 0 else 0

        print(f"[discover] {symbol} | last {activity_hours}h | max_age={max_age_days}d | mode={mode}")
        receivers = fetch_recent_token_receivers(
            api,
            contract,
            pages=int(settings.get("transfer_pages") or 5),
            page_size=int(settings.get("transfer_page_size") or 100),
            since_ts=since_ts,
        )
        scanned_tokens.append(symbol)
        print(f"[discover] {symbol} raw receivers: {len(receivers)}")

        for addr, stats in receivers.items():
            if stats["received_raw"] < min_received_raw:
                continue
            if max_received_raw and stats["received_raw"] > max_received_raw:
                continue
            if addr in routers:
                continue
            if exclude_cex and is_labeled_cex(addr):
                continue

            first_ts = wallet_first_tx_ts(api, addr)
            age_days = wallet_age_days(first_ts, now)
            active_in_window = stats["tx_count"] > 0 and stats["last_ts"] >= since_ts

            if hard_max_age > 0 and age_days is not None and age_days > hard_max_age:
                continue
            if not passes_mode(
                mode=mode,
                age_days=age_days,
                max_age_days=max_age_days,
                min_age_days=min_age_days,
                active_in_window=active_in_window,
            ):
                continue

            received_human = stats["received_raw"] / (10**decimals)
            score = received_human * (2.0 if (age_days is not None and age_days <= 30) else 1.0)
            score += stats["tx_count"] * 5

            existing = all_candidates.get(addr)
            if existing and existing["score"] >= score:
                continue

            all_candidates[addr] = {
                "address": addr,
                "label": "active" if active_in_window else "new",
                "name_tag": f"{symbol} receiver | age={age_days:.0f}d" if age_days else f"{symbol} receiver",
                "chain_id": 1,
                "source": "active_wallet_discovery",
                "token": symbol,
                "token_contract": contract,
                "wallet_age_days": round(age_days, 1) if age_days is not None else None,
                "first_tx_at": datetime.fromtimestamp(first_ts, tz=timezone.utc).isoformat()
                if first_ts
                else None,
                "received": round(received_human, 4),
                "tx_count": stats["tx_count"],
                "last_active_at": datetime.fromtimestamp(stats["last_ts"], tz=timezone.utc).isoformat()
                if stats["last_ts"]
                else None,
                "score": round(score, 2),
            }
            time.sleep(0.08)

    candidates = sorted(all_candidates.values(), key=lambda x: x["score"], reverse=True)
    picked = candidates[:max_wallets]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(picked),
        "tokens_scanned": scanned_tokens,
        "exclude_symbols": config.get("exclude_symbols") or ["FET"],
        "filters": {
            "activity_hours": activity_hours,
            "max_wallet_age_days": max_age_days,
            "min_received": min_received,
            "mode": mode,
            "exclude_labeled_cex": exclude_cex,
        },
        "stats": {
            "unique_candidates": len(candidates),
            "saved": len(picked),
        },
        "wallets": picked,
    }

    if not dry_run:
        save_json(DEFAULT_OUT, payload)
        print(f"wrote {len(picked)} active/new wallets → {DEFAULT_OUT} (FET hariç)")
    else:
        print(f"[dry-run] would save {len(picked)} wallets from {len(scanned_tokens)} tokens (no FET)")
        for w in picked[:5]:
            print(
                f"  {w['token']} {w['address'][:12]}… "
                f"age={w['wallet_age_days']}d recv={w['received']}"
            )

    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Yeni/aktif cüzdan keşfi")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-age-days", type=float, default=None)
    parser.add_argument("--activity-hours", type=int, default=None)
    parser.add_argument("--mode", choices=["new_or_active", "new_only", "active_only"], default=None)
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"config yok: {config_path}")
    config = load_json(config_path)
    settings = config.setdefault("settings", {})
    if args.max_age_days is not None:
        settings["max_wallet_age_days"] = args.max_age_days
    if args.activity_hours is not None:
        settings["activity_hours"] = args.activity_hours
    if args.mode:
        settings["mode"] = args.mode

    discover(config, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
