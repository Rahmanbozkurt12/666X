#!/usr/bin/env python3
"""
Multichain confluence → Binance spot bot.

Sinyaller (opsiyonel API key'lerle aktif):
  1) volume_spike (Binance REST klines)
  2) orderbook_imbalance (REST + WebSocket depth)
  3) holder_velocity / smart_money (Moralis/Etherscan + flag file)
  4) news_sentiment (CryptoPanic)

Yürütme: Binance USDT spot (dry-run varsayılan)
Çıkış: peak trail + entry stop (yüzde)

Kullanım:
  export BINANCE_API_KEY=...
  export BINANCE_API_SECRET=...
  # opsiyonel: MORALIS_API_KEY ETHERSCAN_API_KEY CRYPTOPANIC_API_KEY HELIUS_API_KEY
  python3 -m binance_confluence --once
  python3 -m binance_confluence --live
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from pathlib import Path
from typing import Any

from binance.client import Client

from binance_confluence.executor import BinanceSpotExecutor
from binance_confluence.scoring import aggregate
from binance_confluence.signals.news import scan_news_sentiment
from binance_confluence.signals.onchain import scan_onchain_watchlist
from binance_confluence.signals.orderbook import OrderBookCache, scan_orderbook_imbalance
from binance_confluence.signals.volume_spike import scan_volume_spikes

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CFG = ROOT / "config" / "binance_confluence.json"
STATE_PATH = ROOT / "output" / "binance_confluence_state.json"


def load_cfg(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def get_client(*, need_private: bool) -> Client:
    key = env("BINANCE_API_KEY")
    secret = env("BINANCE_API_SECRET")
    if need_private and (not key or not secret):
        raise SystemExit("LIVE için BINANCE_API_KEY / BINANCE_API_SECRET gerekli.")

    public_base = env("BINANCE_PUBLIC_API") or "https://data-api.binance.vision/api"
    # Skip constructor ping (often 451 in restricted regions), then set public base.
    _ping = Client.ping
    Client.ping = lambda self: {}  # type: ignore[method-assign]
    try:
        client = Client(api_key=key or "", api_secret=secret or "", requests_params={"timeout": 20})
    finally:
        Client.ping = _ping  # type: ignore[method-assign]

    client.API_URL = public_base
    try:
        client.ping()
        print(f"[API] public market data via {public_base}")
    except Exception as e:
        print(f"[UYARI] public ping failed ({e}); continuing")
    return client


def fetch_liquid_usdt_symbols(client: Client, min_qv: float, limit: int) -> list[str]:
    tickers = client.get_ticker()
    rows = []
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        if any(x in sym for x in ("UP", "DOWN", "BEAR", "BULL")):
            continue
        # skip non-ascii tickers (meme unicode pairs)
        if not sym.isascii():
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if qv >= min_qv:
            rows.append((sym, qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:limit]]


def banner(cfg: dict[str, Any], dry: bool) -> None:
    print(
        f"""
╔══════════════════════════════════════════════════════════════╗
║  MULTICHAIN CONFLUENCE → BINANCE SPOT                        ║
╠══════════════════════════════════════════════════════════════╣
║  Signals: volume | orderbook | holders | smart$ | news       ║
║  Exec:    Binance USDT spot ({'DRY_RUN' if dry else 'LIVE'})                         ║
║  Exit:    trail {cfg.get('trail_pct')}% from peak | stop {cfg.get('stop_pct')}% from entry      ║
║  Tip:     Tokyo/Frankfurt VPS for lower latency              ║
╚══════════════════════════════════════════════════════════════╝
"""
    )


def run_once(cfg: dict[str, Any], *, live: bool) -> int:
    dry = not live and bool(cfg.get("dry_run", True))
    if live:
        dry = False
    banner(cfg, dry)

    client = get_client(need_private=not dry)
    if not dry:
        client.get_account()
    elif env("BINANCE_API_KEY") and env("BINANCE_API_SECRET"):
        try:
            client.get_account()
        except Exception as e:
            print(f"[UYARI] account check skipped/failed: {e}")

    weights = (cfg.get("confluence") or {}).get("weights") or {}
    min_score = float((cfg.get("confluence") or {}).get("min_score") or 2.0)
    ob_cfg = cfg.get("orderbook") or {}
    vol_cfg = cfg.get("volume_spike") or {}
    news_cfg = cfg.get("news") or {}
    onchain_cfg = cfg.get("onchain") or {}

    cache = OrderBookCache(top_levels=int(ob_cfg.get("top_levels") or 10))
    ws_syms = list(ob_cfg.get("ws_symbols") or [])
    cache.start_ws(ws_syms)

    ex = BinanceSpotExecutor(
        client,
        dry_run=dry,
        state_path=STATE_PATH,
        trail_pct=float(cfg.get("trail_pct") or 0.20),
        stop_pct=float(cfg.get("stop_pct") or 0.50),
        stake_fraction=float(cfg.get("stake_fraction") or 0.95),
        daily_loss_limit=float(cfg.get("daily_loss_limit_usdt") or 50),
    )

    # exits first
    ex.manage_exits()
    if ex.stop_bot:
        cache.stop()
        return 0
    if len(ex.positions) >= int(cfg.get("max_open_positions") or 1):
        print("[INFO] max open positions — only managing exits")
        cache.stop()
        return 0

    universe = fetch_liquid_usdt_symbols(
        client,
        float(cfg.get("min_quote_volume_usdt") or 500_000),
        int(cfg.get("scan_batch_size") or 40) * 3,
    )
    batch = universe[:]
    random.shuffle(batch)
    batch = batch[: int(cfg.get("scan_batch_size") or 40)]
    # always include WS symbols + onchain mapped symbols
    for s in ws_syms:
        if s not in batch:
            batch.append(s)
    for row in onchain_cfg.get("watchlist") or []:
        s = row.get("binance_symbol")
        if s and s not in batch:
            batch.append(s)

    signals = []
    signals += scan_volume_spikes(
        client,
        batch,
        interval=str(vol_cfg.get("interval") or "5m"),
        lookback=int(vol_cfg.get("lookback") or 15),
        min_volume_mult=float(vol_cfg.get("min_volume_mult") or 2.5),
        min_price_change=float(vol_cfg.get("min_price_change") or 0.005),
        weight=float(weights.get("volume_spike") or 1.0),
    )
    signals += scan_orderbook_imbalance(
        client,
        batch,
        cache,
        top_levels=int(ob_cfg.get("top_levels") or 10),
        min_ratio=float(ob_cfg.get("min_bid_ask_ratio") or 3.0),
        weight=float(weights.get("orderbook_imbalance") or 1.2),
    )
    signals += scan_onchain_watchlist(onchain_cfg, weights)

    if news_cfg.get("enabled", True):
        currencies = list(news_cfg.get("currencies") or ["BTC", "ETH", "SOL"])
        symbol_map = {c: f"{c}USDT" for c in currencies}
        signals += scan_news_sentiment(
            currencies=currencies,
            min_sentiment=float(news_cfg.get("min_sentiment") or 0.35),
            symbol_map=symbol_map,
            weight=float(weights.get("news_sentiment") or 1.0),
        )

    ranked = aggregate(signals, min_score=min_score)
    print(f"[SIGNALS] raw={len(signals)} confluence_hits={len(ranked)}")
    for sym, total, items in ranked[:5]:
        parts = ", ".join(f"{i.name}:{i.score:.2f}" for i in items)
        print(f"  {sym} total={total:.2f} | {parts}")

    if ranked:
        sym, total, items = ranked[0]
        reason = " + ".join(i.reason for i in items)
        ex.buy(sym, f"score={total:.2f} | {reason}")

    cache.stop()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Multichain confluence → Binance spot")
    ap.add_argument("--config", type=Path, default=DEFAULT_CFG)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--live", action="store_true", help="Gerçek emir (dry_run kapalı)")
    ap.add_argument("--loop", action="store_true", help="Sürekli çalış")
    args = ap.parse_args()
    cfg = load_cfg(args.config)
    poll = int(cfg.get("poll_seconds") or 15)

    if args.once and not args.loop:
        return run_once(cfg, live=args.live)

    # default: loop
    while True:
        try:
            run_once(cfg, live=args.live)
        except KeyboardInterrupt:
            print("\nDurdu.")
            return 0
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
        time.sleep(max(5, poll))


if __name__ == "__main__":
    raise SystemExit(main())
