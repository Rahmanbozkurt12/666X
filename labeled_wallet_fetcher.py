#!/usr/bin/env python3
"""
eth-labels.com (ücretsiz) üzerinden etiketli cüzdanları çekip kaydeder.

Ekrandaki akış:
  GET https://eth-labels.com/accounts?chainId=1&nameTag=Binance&limit=100

Kullanım:
  python labeled_wallet_fetcher.py
  python labeled_wallet_fetcher.py --name-tag Binance --max 1000
  python labeled_wallet_fetcher.py --merge output/smart_money_wallets.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "output" / "labeled_wallets.json"
ETH_LABELS_BASE = "https://eth-labels.com"


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def fetch_eth_labels(
    *,
    chain_id: int = 1,
    name_tag: str | None = None,
    label: str | None = None,
    search: str | None = None,
    limit: int = 10000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"chainId": chain_id, "limit": limit, "offset": offset}
    if name_tag:
        params["nameTag"] = name_tag
    if label:
        params["label"] = label
    if search:
        params["search"] = search

    r = requests.get(f"{ETH_LABELS_BASE}/accounts", params=params, timeout=120)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError(f"unexpected response: {str(data)[:200]}")
    return data


def normalize_row(row: dict[str, Any], source: str) -> dict[str, Any]:
    addr = (row.get("address") or "").strip().lower()
    return {
        "address": addr,
        "label": row.get("label") or "",
        "name_tag": row.get("nameTag") or row.get("name_tag") or "",
        "chain_id": row.get("chainId") or row.get("chain_id") or 1,
        "source": source,
    }


def merge_smart_money(path: Path) -> list[dict[str, Any]]:
    data = load_json(path)
    rows = data.get("top_wallets") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        addr = (row.get("address") or "").strip().lower()
        if not addr:
            continue
        out.append(
            {
                "address": addr,
                "label": "smart_money",
                "name_tag": f"smart_money score={row.get('score', '')}",
                "chain_id": 1,
                "source": str(path.name),
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="eth-labels cüzdan kaydedici")
    parser.add_argument("--chain-id", type=int, default=1)
    parser.add_argument("--name-tag", default="Binance", help="ör: Binance, Coinbase, OKX")
    parser.add_argument("--label", default=None)
    parser.add_argument("--max", type=int, default=0, help="0 = hepsi")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--merge", nargs="*", default=[], help="ek JSON dosyaları")
    args = parser.parse_args()

    wallets: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []

    if args.name_tag or args.label:
        rows = fetch_eth_labels(
            chain_id=args.chain_id,
            name_tag=args.name_tag or None,
            label=args.label or None,
        )
        tag = args.name_tag or args.label or "eth_labels"
        sources.append({"source": "eth_labels", "query": tag, "fetched": len(rows)})
        for row in rows:
            item = normalize_row(row, f"eth_labels:{tag}")
            if item["address"]:
                wallets[item["address"]] = item

    for extra in args.merge:
        path = Path(extra)
        if not path.exists():
            print(f"[warn] yok: {path}")
            continue
        rows = merge_smart_money(path)
        sources.append({"source": str(path), "fetched": len(rows)})
        for item in rows:
            wallets[item["address"]] = item

    ordered = sorted(wallets.values(), key=lambda x: x["address"])
    if args.max and args.max > 0:
        ordered = ordered[: args.max]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(ordered),
        "sources": sources,
        "wallets": ordered,
    }
    out_path = Path(args.out)
    save_json(out_path, payload)
    print(f"wrote {len(ordered)} wallets → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
