#!/usr/bin/env python3
"""
Mega balina cüzdanlarında son X saatte hangi altcoin girişi oldu — FET HARİÇ.

Ekrandaki akışın repodaki karşılığı:
  - 10 mega balina cüzdanı taranır
  - Son 3 saatte gelen ERC20 girişleri sayılır
  - FET ve stablecoin'ler filtrelenir
  - En çok giriş gören altcoinler listelenir

Kullanım:
  python whale_altcoin_scan.py
  python whale_altcoin_scan.py --hours 3
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "whale_altcoin_scan.json"
DEFAULT_OUT = ROOT / "output" / "whale_altcoin_scan_last.json"


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_whale_wallets(path: Path, limit: int) -> list[dict[str, Any]]:
    data = load_json(path)
    rows = data.get("top_wallets") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"invalid wallet source: {path}")
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        addr = (row.get("address") or "").strip().lower()
        if addr:
            out.append(row)
    return out


def fetch_wallet_token_inflows(
    api: str,
    wallet: str,
    since_ts: int,
    *,
    pages: int = 3,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    inflows: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        try:
            r = requests.get(
                api,
                params={
                    "module": "account",
                    "action": "tokentx",
                    "address": wallet,
                    "page": page,
                    "offset": page_size,
                    "sort": "desc",
                },
                timeout=45,
            )
            r.raise_for_status()
            rows = r.json().get("result") or []
        except (requests.RequestException, ValueError):
            break
        if not isinstance(rows, list) or not rows:
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
            if to_addr != wallet:
                continue
            inflows.append(tx)
        if stop:
            break
    return inflows


def scan(config: dict[str, Any], *, hours: int | None = None) -> dict[str, Any]:
    settings = config.get("settings") or {}
    api = settings.get("explorer_api") or "https://eth.blockscout.com/api"
    activity_hours = int(hours or settings.get("activity_hours") or 3)
    wallet_path = Path(settings.get("wallet_source") or ROOT / "output/smart_money_wallets.json")
    if not wallet_path.is_absolute():
        wallet_path = ROOT / wallet_path
    max_wallets = int(settings.get("max_wallets") or 10)
    top_n = int(settings.get("top_results") or 10)

    exclude_symbols = {s.upper() for s in (config.get("exclude_symbols") or ["FET"])}
    exclude_contracts = {(c or "").lower() for c in (config.get("exclude_contracts") or [])}

    now = int(datetime.now(timezone.utc).timestamp())
    since_ts = now - activity_hours * 3600
    whales = load_whale_wallets(wallet_path, max_wallets)

    print(f"--- {activity_hours} SAATLIK AKILLI PARA TARAMASI (FET HARİÇ) ---")
    print(f"Balina cüzdan sayısı: {len(whales)}\n")

    token_counter: Counter[str] = Counter()
    token_details: dict[str, dict[str, Any]] = {}
    per_wallet_hits: list[dict[str, Any]] = []

    for i, whale in enumerate(whales, start=1):
        addr = whale["address"]
        inflows = fetch_wallet_token_inflows(api, addr, since_ts)
        wallet_tokens: Counter[str] = Counter()
        for tx in inflows:
            symbol = (tx.get("tokenSymbol") or "?").upper()
            contract = (tx.get("contractAddress") or "").lower()
            if symbol in exclude_symbols or contract in exclude_contracts:
                continue
            wallet_tokens[symbol] += 1
            token_counter[symbol] += 1
            if symbol not in token_details:
                token_details[symbol] = {
                    "contract": contract,
                    "name": tx.get("tokenName") or symbol,
                    "decimals": tx.get("tokenDecimal"),
                }

        hit_count = sum(wallet_tokens.values())
        per_wallet_hits.append(
            {
                "address": addr,
                "score": whale.get("score"),
                "inflow_count": hit_count,
                "tokens": dict(wallet_tokens),
            }
        )
        print(
            f"[{i}/{len(whales)}] Cüzdan tarandı | "
            f"Son {activity_hours} saatte tespit edilen altcoin girişi: {hit_count}"
        )

    print(f"\n--- SON {activity_hours} SAATTE EN ÇOK GİRİŞ GÖREN ALTCOINLER (FET YOK) ---")
    if token_counter:
        for token, adet in token_counter.most_common(top_n):
            detail = token_details.get(token, {})
            print(f"- Token: {token} | {activity_hours} Saatteki Giriş/Alım Sayısı: {adet}")
            if detail.get("contract"):
                print(f"  contract: {detail['contract']}")
    else:
        print(
            f"Son {activity_hours} saat içinde bu mega balinalara "
            f"(FET hariç) herhangi bir altcoin girişi gerçekleşmedi."
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "activity_hours": activity_hours,
        "exclude_symbols": sorted(exclude_symbols),
        "wallets_scanned": len(whales),
        "token_inflow_counts": dict(token_counter.most_common()),
        "token_details": token_details,
        "per_wallet": per_wallet_hits,
    }
    save_json(DEFAULT_OUT, payload)
    print(f"\n→ {DEFAULT_OUT}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Balina altcoin giriş taraması (FET hariç)")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--hours", type=int, default=None)
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"config yok: {config_path}")
    scan(load_json(config_path), hours=args.hours)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
