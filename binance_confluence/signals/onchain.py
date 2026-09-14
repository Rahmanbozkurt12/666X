#!/usr/bin/env python3
"""
On-chain holder velocity + smart-money stubs.

Uses optional Moralis / Etherscan / Helius APIs when keys exist.
Without keys, returns empty signals (bot still runs on volume+orderbook).
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from binance_confluence.signals.base import Signal

# simple in-memory history for holder counts: key -> list[(ts, count)]
_HOLDER_HISTORY: dict[str, list[tuple[float, int]]] = {}


def _env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def _http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> Any | None:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "666X-confluence/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            import json

            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None


def fetch_erc20_holder_count_etherscan(contract: str, chain_id: int = 1) -> int | None:
    """Etherscan API V2 style token holder count when available."""
    key = _env("ETHERSCAN_API_KEY")
    if not key or contract == "native":
        return None
    # tokenholdercount is not universal on all explorers; best-effort
    q = urllib.parse.urlencode(
        {
            "chainid": chain_id,
            "module": "token",
            "action": "tokenholdercount",
            "contractaddress": contract,
            "apikey": key,
        }
    )
    data = _http_json(f"https://api.etherscan.io/v2/api?{q}")
    if not data:
        return None
    try:
        return int(data.get("result") or 0) or None
    except (TypeError, ValueError):
        return None


def fetch_moralis_holders(chain: str, contract: str) -> int | None:
    key = _env("MORALIS_API_KEY")
    if not key or contract == "native":
        return None
    chain_map = {"ethereum": "eth", "bsc": "bsc", "polygon": "polygon"}
    c = chain_map.get(chain, chain)
    url = f"https://deep-index.moralis.io/api/v2.2/erc20/{contract}/owners?chain={c}&limit=1"
    data = _http_json(url, headers={"X-API-Key": key, "Accept": "application/json"})
    if not data:
        return None
    # Moralis may return total in cursor APIs differently; use len fallback
    total = data.get("total")
    if total is not None:
        try:
            return int(total)
        except (TypeError, ValueError):
            pass
    return None


def record_holder_sample(key: str, count: int, keep_seconds: float = 7200) -> list[tuple[float, int]]:
    now = time.time()
    hist = _HOLDER_HISTORY.setdefault(key, [])
    hist.append((now, count))
    _HOLDER_HISTORY[key] = [(t, c) for t, c in hist if now - t <= keep_seconds]
    return _HOLDER_HISTORY[key]


def holder_growth_signal(
    key: str,
    hist: list[tuple[float, int]],
    *,
    window_sec: float = 3600,
    mult: float = 3.0,
) -> tuple[float, str] | None:
    if len(hist) < 3:
        return None
    now = hist[-1][0]
    recent = [(t, c) for t, c in hist if now - t <= window_sec]
    older = [(t, c) for t, c in hist if window_sec < now - t <= window_sec * 2]
    if len(recent) < 2 or not older:
        return None
    recent_delta = recent[-1][1] - recent[0][1]
    older_delta = older[-1][1] - older[0][1]
    baseline = max(older_delta, 1)
    growth_mult = recent_delta / baseline
    if growth_mult >= mult and recent_delta > 0:
        return growth_mult, f"net_new_holders_1h≈{recent_delta} ({growth_mult:.1f}x baseline)"
    return None


def scan_onchain_watchlist(cfg: dict[str, Any], weights: dict[str, float]) -> list[Signal]:
    if not cfg.get("enabled", True):
        return []
    out: list[Signal] = []
    mult = float(cfg.get("holder_growth_mult") or 3.0)
    w_hold = float(weights.get("holder_velocity") or 1.5)
    w_smart = float(weights.get("smart_money") or 1.5)

    for row in cfg.get("watchlist") or []:
        symbol = row.get("binance_symbol")
        chain = (row.get("chain") or "ethereum").lower()
        contract = row.get("contract") or ""
        if not symbol or not contract:
            continue
        key = f"{chain}:{contract}"
        count = None
        if chain in {"ethereum", "eth"}:
            count = fetch_moralis_holders("ethereum", contract) or fetch_erc20_holder_count_etherscan(
                contract, 1
            )
        elif chain == "bsc":
            count = fetch_moralis_holders("bsc", contract)
        # solana holder APIs vary; leave hook for HELIUS later

        if count is not None:
            hist = record_holder_sample(key, count)
            g = holder_growth_signal(key, hist, mult=mult)
            if g:
                growth_mult, reason = g
                out.append(
                    Signal(
                        name="holder_velocity",
                        symbol=symbol,
                        score=w_hold * min(growth_mult / mult, 3.0),
                        reason=reason,
                        meta={"holders": count, "chain": chain, "contract": contract},
                    )
                )

        # Smart money: optional webhook inbox file (populated by external Arkham/webhook worker)
        inbox = row.get("smart_money_flag_file")
        if inbox:
            from pathlib import Path

            p = Path(inbox)
            if p.exists():
                age = time.time() - p.stat().st_mtime
                if age < 900:  # 15m fresh flag
                    out.append(
                        Signal(
                            name="smart_money",
                            symbol=symbol,
                            score=w_smart,
                            reason=f"smart_money_flag age={age:.0f}s",
                            meta={"flag_file": inbox},
                        )
                    )
    return out
