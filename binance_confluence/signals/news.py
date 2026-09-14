#!/usr/bin/env python3
"""CryptoPanic news + optional lightweight sentiment."""

from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request
from typing import Any

from binance_confluence.signals.base import Signal

POS_WORDS = {
    "surge",
    "soar",
    "rally",
    "partnership",
    "etf",
    "approval",
    "bull",
    "record",
    "launch",
    "listing",
    "upgrade",
    "funding",
}
NEG_WORDS = {
    "hack",
    "exploit",
    "sec",
    "lawsuit",
    "ban",
    "crash",
    "bear",
    "delist",
    "fraud",
    "arrest",
    "outage",
}


def _env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def simple_sentiment(text: str) -> float:
    t = text.lower()
    words = set(re.findall(r"[a-z]+", t))
    pos = len(words & POS_WORDS)
    neg = len(words & NEG_WORDS)
    if pos == neg == 0:
        return 0.0
    return (pos - neg) / max(pos + neg, 1)


def fetch_cryptopanic(currencies: list[str]) -> list[dict[str, Any]]:
    key = _env("CRYPTOPANIC_API_KEY")
    if not key:
        return []
    q = urllib.parse.urlencode(
        {
            "auth_token": key,
            "currencies": ",".join(currencies),
            "kind": "news",
            "public": "true",
        }
    )
    url = f"https://cryptopanic.com/api/v1/posts/?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "666X-confluence/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            import json

            data = json.loads(resp.read().decode())
        return list(data.get("results") or [])
    except Exception:
        return []


def scan_news_sentiment(
    *,
    currencies: list[str],
    min_sentiment: float,
    symbol_map: dict[str, str],
    weight: float = 1.0,
) -> list[Signal]:
    """
    symbol_map: {"BTC": "BTCUSDT", "ETH": "ETHUSDT", ...}
    """
    posts = fetch_cryptopanic(currencies)
    best: dict[str, Signal] = {}
    for post in posts:
        title = str(post.get("title") or "")
        # CryptoPanic votes if present
        votes = post.get("votes") or {}
        pos = float(votes.get("positive") or 0)
        neg = float(votes.get("negative") or 0)
        if pos + neg > 0:
            sent = (pos - neg) / (pos + neg)
        else:
            sent = simple_sentiment(title)
        if sent < min_sentiment:
            continue
        for c in post.get("currencies") or []:
            code = (c.get("code") if isinstance(c, dict) else None) or ""
            code = str(code).upper()
            sym = symbol_map.get(code)
            if not sym:
                continue
            score = weight * min(max(sent, 0.0) / max(min_sentiment, 1e-6), 3.0)
            sig = Signal(
                name="news_sentiment",
                symbol=sym,
                score=score,
                reason=f"news sent={sent:.2f} | {title[:80]}",
                meta={"sentiment": sent, "title": title},
            )
            if sym not in best or sig.score > best[sym].score:
                best[sym] = sig
    return list(best.values())
