#!/usr/bin/env python3
"""
Solana trading-bot fingerprint (public RPC only).

Extracts program IDs + fee destinations from a wallet's recent txs.
Does NOT download closed-source code — only on-chain fingerprints.

Usage:
  python3 solana_bot_fingerprint.py
  python3 solana_bot_fingerprint.py --wallet A6PSQFRfv93hoAn1LhQGRT2dYQtjDKX6SE2vN9MEvbot
  python3 solana_bot_fingerprint.py --limit 30
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_CFG = ROOT / "config" / "solana_target_wallet.json"


def load_cfg(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rpc(url: str, method: str, params: list[Any], retries: int = 6) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.load(resp)
            if "error" in data:
                raise RuntimeError(data["error"])
            return data["result"]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError) as e:
            last = e
            time.sleep(1.4 * (i + 1))
    raise RuntimeError(f"RPC failed: {last}")


def vanity_brand(addr: str, brands: dict[str, str]) -> str | None:
    for prefix, label in brands.items():
        if addr.startswith(prefix):
            return label
    return None


def collect_programs(tx: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    msg = tx["transaction"]["message"]
    for ix in msg.get("instructions") or []:
        pid = ix.get("programId")
        if pid:
            out.add(pid)
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            pid = ix.get("programId")
            if pid:
                out.add(pid)
    return out


def collect_sol_transfers(tx: dict[str, Any], source: str) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []

    def walk(ix: dict[str, Any]) -> None:
        if ix.get("program") != "system":
            return
        parsed = ix.get("parsed") or {}
        if parsed.get("type") != "transfer":
            return
        info = parsed.get("info") or {}
        if info.get("source") != source:
            return
        dest = info.get("destination")
        lamports = int(info.get("lamports") or 0)
        if dest and lamports > 0:
            found.append((dest, lamports))

    msg = tx["transaction"]["message"]
    for ix in msg.get("instructions") or []:
        walk(ix)
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            walk(ix)
    return found


def instruction_hits(tx: dict[str, Any]) -> list[str]:
    logs = (tx.get("meta") or {}).get("logMessages") or []
    keys = ("Buy", "Sell", "Swap", "Instruction:")
    return [line for line in logs if any(k in line for k in keys)][:12]


def conclude(fee_table: list[dict[str, Any]], programs: Counter[str]) -> str:
    brands = [f.get("brand_guess") for f in fee_table if f.get("brand_guess")]
    pump = any("pump" in p for p, _ in programs.most_common(10))
    if any(b and "private_fast" in str(b) for b in brands):
        return (
            "Fee vanity FAST* → not a known public bot (Axiom/Trojan/GMGN/BullX). "
            "Likely private/custom Pump.fun sniper. Source code is not on GitHub."
        )
    if brands:
        return f"Fee brand matched: {brands[0]}. That product is closed-source."
    if pump:
        return "Pump.fun AMM used, but fee brand unmatched → custom bot or new terminal."
    return "Not enough fee signal; raise --limit or use a private RPC."


def analyze(wallet: str, rpc_url: str, limit: int, min_fee_sol: float, cfg: dict[str, Any]) -> dict[str, Any]:
    known_programs = dict(cfg.get("known_programs") or {})
    brands = dict(cfg.get("known_fee_brands") or {})

    sigs = rpc(rpc_url, "getSignaturesForAddress", [wallet, {"limit": limit}])
    program_counter: Counter[str] = Counter()
    fee_lamports: dict[str, int] = defaultdict(int)
    fee_hits: dict[str, int] = defaultdict(int)
    trade_like = 0
    samples: list[dict[str, Any]] = []

    for i, row in enumerate(sigs or []):
        sig = row["signature"]
        time.sleep(0.35)
        tx = rpc(
            rpc_url,
            "getTransaction",
            [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        if not tx:
            continue

        progs = collect_programs(tx)
        for p in progs:
            program_counter[known_programs.get(p, p)] += 1

        transfers = collect_sol_transfers(tx, wallet)
        for dest, lamports in transfers:
            if lamports / 1e9 >= min_fee_sol:
                fee_lamports[dest] += lamports
                fee_hits[dest] += 1

        hits = instruction_hits(tx)
        if any(x in " ".join(hits) for x in ("Buy", "Sell", "Swap")):
            trade_like += 1
            if len(samples) < 8:
                samples.append(
                    {
                        "signature": sig,
                        "blockTime": row.get("blockTime"),
                        "programs": sorted(known_programs.get(p, p) for p in progs),
                        "fees_sol": [
                            {"to": d, "sol": round(l / 1e9, 6)}
                            for d, l in transfers
                            if l / 1e9 >= min_fee_sol
                        ],
                        "log_hits": hits[:8],
                    }
                )
        if i and i % 10 == 0:
            print(f"... scanned {i}/{len(sigs)} sigs", flush=True)

    fee_table = []
    for dest, lamports in sorted(fee_lamports.items(), key=lambda x: -x[1]):
        fee_table.append(
            {
                "address": dest,
                "total_sol": round(lamports / 1e9, 6),
                "hits": fee_hits[dest],
                "brand_guess": vanity_brand(dest, brands),
            }
        )

    return {
        "wallet": wallet,
        "scanned_signatures": len(sigs or []),
        "trade_like_txs": trade_like,
        "top_programs": program_counter.most_common(15),
        "fee_destinations": fee_table[:25],
        "samples": samples,
        "conclusion": conclude(fee_table, program_counter),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Fingerprint Solana trading bot from wallet txs")
    ap.add_argument("--config", type=Path, default=DEFAULT_CFG)
    ap.add_argument("--wallet", default=None)
    ap.add_argument("--rpc", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", type=Path, default=ROOT / "output" / "solana_bot_fingerprint.json")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    wallet = args.wallet or cfg["target_wallet"]
    rpc_url = args.rpc or cfg.get("rpc_url") or "https://api.mainnet-beta.solana.com"
    limit = int(args.limit or cfg.get("signature_limit") or 40)
    min_fee = float(cfg.get("min_fee_sol") or 0.001)

    print(f"Scanning {wallet} (limit={limit}) …")
    report = analyze(wallet, rpc_url, limit, min_fee, cfg)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n=== TOP PROGRAMS ===")
    for name, n in report["top_programs"]:
        print(f"  {n:4}  {name}")
    print("\n=== FEE DESTINATIONS (>= min_fee_sol) ===")
    for row in report["fee_destinations"][:12]:
        brand = f"  [{row['brand_guess']}]" if row.get("brand_guess") else ""
        print(f"  {row['total_sol']:.4f} SOL x{row['hits']}  {row['address']}{brand}")
    print("\n=== CONCLUSION ===")
    print(report["conclusion"])
    print(f"\nSaved: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
