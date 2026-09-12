#!/usr/bin/env python3
"""CLI for mega confluence scanner + paper trader."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .confluence import analyze_bundle
from .fetch import gather_market_bundle, fetch_ticker_24h
from .trader import PaperTrader

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "mega_confluence.json"
DEFAULT_STATE = ROOT / "output" / "mega_confluence_state.json"
DEFAULT_OUT = ROOT / "output" / "mega_confluence_last.json"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            "interval": "15m",
            "hard_stop_pct": 1.0,
            "trail_pct": 0.45,
            "starting_cash": 10000,
            "poll_seconds": 60,
            "min_quote_volume_usdt": 5_000_000,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def pick_symbols(cfg: dict[str, Any], explicit: list[str] | None, top_n: int) -> list[str]:
    if explicit:
        return [s.upper().replace("/", "") for s in explicit]
    symbols = [s.upper() for s in cfg.get("symbols") or []]
    if top_n <= 0:
        return symbols
    try:
        tickers = fetch_ticker_24h()
        usdt = []
        for t in tickers:
            sym = str(t.get("symbol") or "")
            if not sym.endswith("USDT"):
                continue
            if any(x in sym for x in ("UPUSDT", "DOWNUSDT", "BULL", "BEAR")):
                continue
            qv = float(t.get("quoteVolume") or 0)
            if qv < float(cfg.get("min_quote_volume_usdt", 5_000_000)):
                continue
            usdt.append((sym, qv))
        usdt.sort(key=lambda x: x[1], reverse=True)
        ranked = [s for s, _ in usdt[:top_n]]
        # keep configured symbols first
        out = []
        for s in symbols + ranked:
            if s not in out:
                out.append(s)
        return out[: max(len(symbols), top_n)]
    except Exception:
        return symbols or ["BTCUSDT"]


def print_report(result: dict[str, Any], trade: dict[str, Any] | None = None) -> None:
    v = result["verdict"]
    print("=" * 72)
    print(f"{result['symbol']} @ {result['price']:.6g}  interval={result['interval']}")
    print(
        f"VERDICT: {v['bias']}  action={v['action']}  score={v['score']:.4f}  "
        f"avail={v['available']}/{v['total']}  bull={v['bull_count']} bear={v['bear_count']}"
    )
    print("Top bull:")
    for row in v["top_bull"][:5]:
        print(f"  + #{row['id']:03d} {row['name']}: {row['score']:+.2f}  {row['detail']}")
    print("Top bear:")
    for row in v["top_bear"][:5]:
        print(f"  - #{row['id']:03d} {row['name']}: {row['score']:+.2f}  {row['detail']}")
    if trade:
        for ev in trade.get("events") or []:
            print(f"TRADE EVENT: {ev}")
        print(
            f"Account: cash={trade.get('cash')} equity={trade.get('equity')} "
            f"pos={trade.get('position')}"
        )
    print("=" * 72)


def run_once(
    symbol: str,
    interval: str,
    trader: PaperTrader | None,
    *,
    execute: bool,
) -> dict[str, Any]:
    bundle = gather_market_bundle(symbol, interval=interval)
    result = analyze_bundle(bundle)
    trade = None
    if execute and trader is not None:
        trade = trader.on_tick(symbol, result["price"], result["verdict"])
        result["trade"] = trade
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="128-method crypto confluence scanner + paper trader "
        "(-1% hard stop, trailing peak exit)"
    )
    p.add_argument("--symbol", action="append", dest="symbols", help="Symbol (repeatable)")
    p.add_argument("--interval", default=None)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--top", type=int, default=0, help="Also scan top N USDT pairs by volume")
    p.add_argument("--json", action="store_true", help="Print JSON only")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--execute", action="store_true", help="Enable paper buy/sell")
    p.add_argument("--loop", action="store_true", help="Poll continuously")
    p.add_argument("--poll", type=int, default=None, help="Loop seconds")
    p.add_argument("--hard-stop", type=float, default=None, help="Hard stop %% (default 1.0)")
    p.add_argument("--trail", type=float, default=None, help="Trail drop from peak %%")
    p.add_argument("--cash", type=float, default=None)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    interval = args.interval or cfg.get("interval", "15m")
    symbols = pick_symbols(cfg, args.symbols, args.top)
    hard_stop = args.hard_stop if args.hard_stop is not None else float(cfg.get("hard_stop_pct", 1.0))
    trail = args.trail if args.trail is not None else float(cfg.get("trail_pct", 0.45))
    cash = args.cash if args.cash is not None else float(cfg.get("starting_cash", 10000))
    poll = args.poll if args.poll is not None else int(cfg.get("poll_seconds", 60))

    trader = None
    if args.execute:
        trader = PaperTrader(
            starting_cash=cash,
            hard_stop_pct=hard_stop,
            trail_pct=trail,
            state_path=args.state,
        )

    def cycle() -> list[dict[str, Any]]:
        results = []
        best = None
        for sym in symbols:
            try:
                # In execute mode, only auto-buy the strongest BUY among scanned set
                r = run_once(sym, interval, trader=None, execute=False)
                results.append(r)
                if r["verdict"]["action"] == "BUY":
                    if best is None or r["verdict"]["score"] > best["verdict"]["score"]:
                        best = r
                if not args.json:
                    print_report(r)
            except Exception as e:  # noqa: BLE001
                err = {"symbol": sym, "error": str(e)}
                results.append(err)
                if not args.json:
                    print(f"ERROR {sym}: {e}", file=sys.stderr)

        trade_info = None
        if args.execute and trader is not None:
            # manage existing position on its symbol first
            if trader.state.position:
                pos_sym = trader.state.position.symbol
                try:
                    r = run_once(pos_sym, interval, trader, execute=True)
                    trade_info = r.get("trade")
                    # refresh matching result
                    results = [r if x.get("symbol") == pos_sym else x for x in results]
                    if not any(x.get("symbol") == pos_sym for x in results):
                        results.append(r)
                    if not args.json:
                        print_report(r, trade_info)
                except Exception as e:  # noqa: BLE001
                    print(f"ERROR managing {pos_sym}: {e}", file=sys.stderr)
            elif best is not None:
                # open best BUY
                trade_info = trader.on_tick(best["symbol"], best["price"], best["verdict"])
                best["trade"] = trade_info
                if not args.json:
                    print_report(best, trade_info)

        payload = {
            "generated_at": time.time(),
            "interval": interval,
            "symbols": symbols,
            "execute": args.execute,
            "hard_stop_pct": hard_stop,
            "trail_pct": trail,
            "results": results,
            "best_buy": None
            if best is None
            else {
                "symbol": best["symbol"],
                "score": best["verdict"]["score"],
                "price": best["price"],
            },
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if args.json:
            print(json.dumps(payload, indent=2))
        return results

    if args.loop:
        if not args.json:
            print(
                f"Looping symbols={symbols} interval={interval} poll={poll}s "
                f"execute={args.execute} hard_stop=-{hard_stop}% trail={trail}%"
            )
        while True:
            cycle()
            time.sleep(max(15, poll))
    else:
        cycle()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
