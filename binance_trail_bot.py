#!/usr/bin/env python3
"""
Binance USDT spot bot — Z-Score / ADX tarama + zirve trail çıkış.

Çıkış kuralları (istenilen):
  - Yükselişte sabit +1% SATMA → peak takip et, önü açık kalsın
  - Peak'ten %trail_pct geri çekilince kârdan sat (varsayılan 0.020)
  - Alış fiyatından %stop_pct düşünce sat (varsayılan 0.50)

Güvenlik:
  - API key/secret SADECE ortam değişkeninden
  - Varsayılan --dry-run (gerçek emir yok)
  - Withdrawal kapalı API key kullan

Kullanım:
  export BINANCE_API_KEY=...
  export BINANCE_API_SECRET=...
  python3 binance_trail_bot.py --dry-run
  python3 binance_trail_bot.py --live
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from binance.client import Client
from binance.exceptions import BinanceAPIException

ROOT = Path(__file__).resolve().parent
POSITIONS_FILE = ROOT / "output" / "binance_trail_positions.json"
DAILY_PNL_FILE = ROOT / "output" / "binance_trail_daily_pnl.json"
COOLDOWN_FILE = ROOT / "output" / "binance_trail_cooldown.json"

# --- risk / exit (yüzde birimi: 0.50 = %0.50) ---
DEFAULT_TRAIL_PCT = 0.020  # peak'ten bu kadar düşünce kârdan sat
DEFAULT_STOP_PCT = 0.50  # alıştan bu kadar düşünce stop
COOLDOWN_SECONDS = 2 * 60
SCAN_BATCH_SIZE = 40
MIN_QUOTE_VOLUME = 200_000.0

active_positions: dict[str, dict[str, Any]] = {}
daily_pnl_state: dict[str, Any] = {"date": None, "realized_pnl": 0.0}
STOP_BOT_DUE_TO_LOSS_LIMIT = False
symbol_filters_cache: dict[str, dict[str, Any]] = {}
cooldown_state: dict[str, float] = {}
DRY_RUN = True


# ---------------------- persistence ----------------------


def _ensure_output() -> None:
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[UYARI] {path.name} okunamadı: {e}")
        return default


def save_json(path: Path, data: Any) -> None:
    _ensure_output()
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[UYARI] {path.name} yazılamadı: {e}")


def load_positions() -> None:
    global active_positions
    active_positions = load_json(POSITIONS_FILE, {})


def save_positions() -> None:
    save_json(POSITIONS_FILE, active_positions)


def _today_str() -> str:
    return time.strftime("%Y-%m-%d")


def load_daily_pnl() -> None:
    global daily_pnl_state
    daily_pnl_state = load_json(DAILY_PNL_FILE, {"date": None, "realized_pnl": 0.0})
    if daily_pnl_state.get("date") != _today_str():
        daily_pnl_state = {"date": _today_str(), "realized_pnl": 0.0}
        save_daily_pnl()


def save_daily_pnl() -> None:
    save_json(DAILY_PNL_FILE, daily_pnl_state)


def record_realized_pnl(pnl: float, daily_loss_limit: float) -> None:
    global daily_pnl_state, STOP_BOT_DUE_TO_LOSS_LIMIT
    if daily_pnl_state.get("date") != _today_str():
        daily_pnl_state = {"date": _today_str(), "realized_pnl": 0.0}
    daily_pnl_state["realized_pnl"] += pnl
    save_daily_pnl()
    net = float(daily_pnl_state["realized_pnl"])
    print(f"[GÜNLÜK PNL] Bugünkü net: {net:.4f} USDT")
    if daily_loss_limit > 0 and net <= -abs(daily_loss_limit):
        print(f"\n[KRİTİK] Günlük zarar limiti ({daily_loss_limit} USDT) aşıldı — bot duruyor.\n")
        STOP_BOT_DUE_TO_LOSS_LIMIT = True


def load_cooldowns() -> None:
    global cooldown_state
    raw = load_json(COOLDOWN_FILE, {})
    cooldown_state = {k: float(v) for k, v in raw.items()}


def save_cooldowns() -> None:
    save_json(COOLDOWN_FILE, cooldown_state)


def mark_sold(symbol: str) -> None:
    cooldown_state[symbol] = time.time()
    save_cooldowns()


def is_on_cooldown(symbol: str) -> bool:
    last = cooldown_state.get(symbol)
    if last is None:
        return False
    return (time.time() - last) < COOLDOWN_SECONDS


# ---------------------- binance client ----------------------


def env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def get_client() -> Client | None:
    api_key = env("BINANCE_API_KEY")
    secret = env("BINANCE_API_SECRET")
    if not api_key or not secret:
        print("[HATA] BINANCE_API_KEY / BINANCE_API_SECRET ortam değişkeni gerekli.")
        return None
    try:
        return Client(api_key=api_key, api_secret=secret, testnet=False)
    except Exception as e:
        print(f"[HATA] Binance bağlantı: {e}")
        return None


def safe_api_call(func, *args, max_retries: int = 3, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except BinanceAPIException as bae:
            if bae.status_code in (429, 418):
                wait = 30 * (attempt + 1)
                print(f"[RATE LIMIT] {bae.status_code}, {wait}s…")
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            print(f"[AĞ] {e} — 5s…")
            time.sleep(5)
    return None


def build_symbol_filters_cache(client: Client) -> None:
    global symbol_filters_cache
    try:
        exchange_info = safe_api_call(client.get_exchange_info)
        if not exchange_info:
            return
        cache: dict[str, dict[str, Any]] = {}
        for s in exchange_info["symbols"]:
            symbol = s["symbol"]
            step_size = 0.0001
            min_qty = 0.0
            min_notional = 0.0
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    step_size = float(f["stepSize"])
                    min_qty = float(f["minQty"])
                elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = float(f.get("minNotional", f.get("notional", 0.0)))
            cache[symbol] = {
                "step_size": step_size,
                "min_qty": min_qty,
                "min_notional": min_notional,
                "quote_precision": int(s.get("quoteAssetPrecision", 2)),
                "status": s["status"],
                "quote_asset": s["quoteAsset"],
            }
        symbol_filters_cache = cache
        print(f"[BİLGİ] {len(cache)} sembol filtresi cache'lendi.")
    except Exception as e:
        print(f"[HATA] Filtre cache: {e}")


def round_step_size(quantity: float, step_size: float) -> float:
    if step_size <= 0:
        return quantity
    q = Decimal(str(quantity))
    step = Decimal(str(step_size))
    return float((q // step) * step)


# ---------------------- indicators ----------------------


def calculate_adx_and_atr(client: Client, symbol: str, period: int = 14) -> tuple[float, float]:
    try:
        klines = safe_api_call(
            client.get_klines,
            symbol=symbol,
            interval=Client.KLINE_INTERVAL_1HOUR,
            limit=period + 10,
        )
        if not klines or len(klines) < period + 2:
            return 25.0, 0.0
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        tr_list = []
        for i in range(1, len(klines)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_list.append(tr)
        atr = sum(tr_list[-period:]) / period if tr_list else 0.0
        price_range = max(highs[-period:]) - min(lows[-period:])
        avg_price = closes[-1] or 1e-12
        adx_proxy = (price_range / avg_price) * 100 * 5
        return adx_proxy, atr
    except Exception:
        return 20.0, 0.0


def check_spread_ok(client: Client, symbol: str, max_spread_pct: float = 0.5) -> bool:
    try:
        orderbook = safe_api_call(client.get_order_book, symbol=symbol, limit=5)
        if not orderbook:
            return False
        bids, asks = orderbook.get("bids") or [], orderbook.get("asks") or []
        if not bids or not asks:
            return False
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        if best_bid <= 0:
            return False
        spread_pct = ((best_ask - best_bid) / best_bid) * 100
        return spread_pct <= max_spread_pct
    except Exception:
        return False


def calculate_z_score(client: Client, symbol: str, period: int = 20) -> tuple[float, float]:
    try:
        klines = safe_api_call(
            client.get_klines,
            symbol=symbol,
            interval=Client.KLINE_INTERVAL_1HOUR,
            limit=period + 5,
        )
        if not klines or len(klines) < period:
            return 0.0, 0.0
        closes = [float(k[4]) for k in klines[-period:]]
        mean = sum(closes) / len(closes)
        variance = sum((x - mean) ** 2 for x in closes) / len(closes)
        std_dev = math.sqrt(variance) if variance > 0 else 1e-8
        z_score = (closes[-1] - mean) / std_dev
        return z_score, mean
    except Exception:
        return 0.0, 0.0


# ---------------------- wallet / orders ----------------------


def get_available_usdt(client: Client) -> float:
    try:
        balance = safe_api_call(client.get_asset_balance, asset="USDT")
        if not balance:
            return 0.0
        return float(balance.get("free", 0.0))
    except Exception as e:
        print(f"[UYARI] USDT bakiye: {e}")
        return 0.0


def get_wallet_positions(client: Client) -> dict[str, dict[str, Any]]:
    global active_positions
    load_positions()
    try:
        account = safe_api_call(client.get_account)
        if not account:
            return active_positions
        current: set[str] = set()
        for balance in account.get("balances", []):
            asset = balance["asset"]
            total = float(balance["free"]) + float(balance["locked"])
            if asset == "USDT" or total <= 0:
                continue
            symbol = f"{asset}USDT"
            filt = symbol_filters_cache.get(symbol)
            if not filt:
                continue
            ticker = safe_api_call(client.get_symbol_ticker, symbol=symbol)
            if not ticker:
                continue
            price = float(ticker["price"])
            min_notional = float(filt.get("min_notional") or 5.0)
            if total * price < min_notional:
                continue
            current.add(symbol)
            if symbol not in active_positions:
                buy_price = price
                try:
                    trades = safe_api_call(client.get_my_trades, symbol=symbol, limit=5)
                    if trades:
                        buys = [t for t in trades if t.get("isBuyer")]
                        if buys:
                            buy_price = float(buys[-1]["price"])
                except Exception:
                    pass
                active_positions[symbol] = {
                    "buy_price": buy_price,
                    "qty": total,
                    "peak": max(buy_price, price),
                }
                save_positions()
            else:
                pos = active_positions[symbol]
                pos["qty"] = total
                pos["peak"] = max(float(pos.get("peak") or pos["buy_price"]), price)
        for sym in list(active_positions.keys()):
            if sym not in current:
                del active_positions[sym]
        save_positions()
    except Exception as e:
        print(f"[UYARI] Cüzdan okuma: {e}")
    return active_positions


def execute_buy(client: Client, symbol: str, strategy_name: str) -> None:
    available = get_available_usdt(client)
    filt = symbol_filters_cache.get(symbol, {})
    min_notional = float(filt.get("min_notional") or 0.0)
    quote_precision = int(filt.get("quote_precision") or 2)
    raw_amount = available * 0.99
    usdt_amount = float(f"{raw_amount:.{quote_precision}f}")

    if available <= 0 or usdt_amount <= 0:
        print(f"[ATLANDI] {symbol}: yetersiz USDT")
        return
    if min_notional and usdt_amount < min_notional:
        print(f"[ATLANDI] {symbol}: tutar {usdt_amount} < min_notional {min_notional}")
        return

    if DRY_RUN:
        ticker = safe_api_call(client.get_symbol_ticker, symbol=symbol)
        if not ticker:
            return
        px = float(ticker["price"])
        qty = usdt_amount / px
        active_positions[symbol] = {"buy_price": px, "qty": qty, "peak": px}
        save_positions()
        print(f"\n[DRY BUY] {strategy_name} {symbol} @ {px:.6f} qty≈{qty:.6f} notional≈{usdt_amount}\n")
        return

    try:
        order = safe_api_call(client.order_market_buy, symbol=symbol, quoteOrderQty=usdt_amount)
    except BinanceAPIException as bae:
        print(f"[ALIM HATA] {symbol}: {bae.message}")
        return
    if not order:
        return
    fills = order.get("fills") or []
    if fills:
        total_qty = sum(float(f["qty"]) for f in fills)
        total_cost = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        avg_price = total_cost / total_qty
    else:
        ticker = safe_api_call(client.get_symbol_ticker, symbol=symbol)
        avg_price = float(ticker["price"])
        total_qty = usdt_amount / avg_price
    active_positions[symbol] = {
        "buy_price": avg_price,
        "qty": total_qty,
        "peak": avg_price,
    }
    save_positions()
    print(f"\n[ALIM] {strategy_name} {symbol} @ {avg_price:.6f} qty={total_qty}\n")


def execute_sell(client: Client, symbol: str, qty: float) -> tuple[bool, float | None]:
    filt = symbol_filters_cache.get(symbol, {})
    step = float(filt.get("step_size") or 0.0001)
    min_qty = float(filt.get("min_qty") or 0.0)
    adjusted = round_step_size(qty, step)
    if adjusted <= 0 or adjusted < min_qty:
        print(f"[UYARI] {symbol}: qty min altında ({adjusted})")
        return False, None

    if DRY_RUN:
        ticker = safe_api_call(client.get_symbol_ticker, symbol=symbol)
        px = float(ticker["price"]) if ticker else None
        print(f"\n[DRY SELL] {symbol} qty={adjusted} px={px}\n")
        return True, px

    try:
        order = safe_api_call(client.order_market_sell, symbol=symbol, quantity=adjusted)
    except BinanceAPIException as bae:
        print(f"[SATIŞ HATA] {symbol}: {bae.message}")
        return False, None
    if not order:
        return False, None
    fills = order.get("fills") or []
    avg = None
    if fills:
        tq = sum(float(f["qty"]) for f in fills)
        tr = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        avg = tr / tq if tq else None
    if avg is None:
        ticker = safe_api_call(client.get_symbol_ticker, symbol=symbol)
        avg = float(ticker["price"]) if ticker else None
    print(f"\n[SATIŞ] {symbol} @ {avg}\n")
    return True, avg


# ---------------------- position management (NEW EXITS) ----------------------


def manage_positions(
    client: Client,
    daily_loss_limit: float,
    trail_pct: float,
    stop_pct: float,
) -> None:
    """
    Yükselişte satma → peak güncelle.
    Peak'ten trail_pct (%) geri çekilince kârdan sat.
    Alıştan stop_pct (%) düşünce stop sat.
    """
    positions = get_wallet_positions(client)
    if not positions:
        return

    for symbol, pos in list(positions.items()):
        try:
            ticker = safe_api_call(client.get_symbol_ticker, symbol=symbol)
            if not ticker:
                continue
            price = float(ticker["price"])
            buy = float(pos["buy_price"])
            peak = float(pos.get("peak") or buy)
            if price > peak:
                peak = price
                pos["peak"] = peak
                save_positions()

            pnl_pct = ((price - buy) / buy) * 100 if buy else 0.0
            drop_from_peak = ((peak - price) / peak) * 100 if peak else 0.0

            print(
                f"[TAKİP] {symbol} | alış={buy:.6f} peak={peak:.6f} "
                f"şimdi={price:.6f} | pnl={pnl_pct:+.3f}% | peak↓={drop_from_peak:.3f}%"
            )

            reason = None
            # 1) hard stop from entry
            if pnl_pct <= -abs(stop_pct):
                reason = f"STOP_LOSS pnl={pnl_pct:.3f}% <= -{stop_pct}%"
            # 2) trail from peak (only meaningful once we've moved; still allow even if slightly red after spike)
            elif drop_from_peak >= abs(trail_pct) and price < peak:
                reason = (
                    f"TRAIL_EXIT peak={peak:.6f} drop={drop_from_peak:.3f}% "
                    f">= {trail_pct}% | pnl={pnl_pct:+.3f}%"
                )

            if not reason:
                continue

            print(f"[ÇIKIŞ] {symbol}: {reason}")
            sold, sell_price = execute_sell(client, symbol, float(pos["qty"]))
            if not sold:
                continue
            mark_sold(symbol)
            if sell_price is not None:
                pnl = (sell_price - buy) * float(pos["qty"])
                record_realized_pnl(pnl, daily_loss_limit)
            active_positions.pop(symbol, None)
            save_positions()
        except Exception as e:
            print(f"[HATA] Pozisyon {symbol}: {e}")


# ---------------------- scan / strategy ----------------------


def scan_and_trade(
    client: Client,
    daily_loss_limit: float,
    trail_pct: float,
    stop_pct: float,
) -> None:
    manage_positions(client, daily_loss_limit, trail_pct, stop_pct)
    if STOP_BOT_DUE_TO_LOSS_LIMIT:
        return

    wallet = get_wallet_positions(client)
    if wallet:
        return  # tek pozisyon modu

    all_tickers = safe_api_call(client.get_ticker)
    if not all_tickers:
        return

    print("Piyasa taranıyor (Z-Score / ADX)…")
    by_sym = {t["symbol"]: t for t in all_tickers}
    candidates: list[str] = []
    for t in all_tickers:
        symbol = t["symbol"]
        filt = symbol_filters_cache.get(symbol)
        if not filt or filt["quote_asset"] != "USDT" or filt["status"] != "TRADING":
            continue
        if is_on_cooldown(symbol):
            continue
        try:
            qv = float(t["quoteVolume"])
        except (KeyError, ValueError, TypeError):
            continue
        if qv < MIN_QUOTE_VOLUME:
            continue
        candidates.append(symbol)

    random.shuffle(candidates)
    best = None
    best_score = float("-inf")

    for symbol in candidates[:SCAN_BATCH_SIZE]:
        if not check_spread_ok(client, symbol):
            continue
        adx, _atr = calculate_adx_and_atr(client, symbol)
        if adx < 30.0:
            z_score, _ = calculate_z_score(client, symbol)
            if z_score <= -1.8:
                score = abs(z_score)
                if score > best_score:
                    best_score = score
                    best = (symbol, "Z-Score Mean Reversion")
        else:
            t = by_sym.get(symbol)
            if not t:
                continue
            try:
                chg = float(t["priceChangePercent"])
            except (KeyError, ValueError, TypeError):
                continue
            if chg >= 2.0 and chg > best_score:
                best_score = chg
                best = (symbol, "ATR Trend/Grid")

    if best:
        symbol, name = best
        print(f"[SİNYAL] {symbol} | {name} | skor={best_score:.4f}")
        execute_buy(client, symbol, name)


# ---------------------- main ----------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Binance trail-exit spot bot")
    p.add_argument("--dry-run", action="store_true", default=True, help="Paper (varsayılan)")
    p.add_argument("--live", action="store_true", help="Gerçek market emir")
    p.add_argument("--trail-pct", type=float, default=DEFAULT_TRAIL_PCT, help="Peak'ten düşüş %% (örn 0.020)")
    p.add_argument("--stop-pct", type=float, default=DEFAULT_STOP_PCT, help="Alıştan stop %% (örn 0.50)")
    p.add_argument("--daily-loss-limit", type=float, default=50.0)
    p.add_argument("--refresh-filters-every", type=int, default=360)
    p.add_argument("--sleep", type=int, default=10)
    return p.parse_args()


def main() -> int:
    global DRY_RUN
    args = parse_args()
    DRY_RUN = not bool(args.live)
    mode = "DRY_RUN" if DRY_RUN else "LIVE"

    client = get_client()
    if not client:
        return 2

    try:
        safe_api_call(client.get_account)
        print(f"[BAĞLANTI OK] mode={mode}")
    except Exception as e:
        print(f"[KRİTİK] API doğrulanamadı: {e}")
        return 1

    if not DRY_RUN:
        print("⚠️  LIVE — gerçek para. Withdrawal kapalı key kullan.")
        time.sleep(2)

    load_daily_pnl()
    load_cooldowns()
    build_symbol_filters_cache(client)

    print(
        f"Bot aktif | trail={args.trail_pct}% peak↓ | stop={args.stop_pct}% giriş↓ | "
        f"sabit +1% TP YOK (yükseliş serbest)"
    )

    loop = 0
    while True:
        try:
            scan_and_trade(client, args.daily_loss_limit, args.trail_pct, args.stop_pct)
            if STOP_BOT_DUE_TO_LOSS_LIMIT:
                return 0
            loop += 1
            if loop % max(1, args.refresh_filters_every) == 0:
                build_symbol_filters_cache(client)
            time.sleep(max(3, args.sleep))
        except KeyboardInterrupt:
            print("\nDurduruldu.")
            return 0
        except Exception as e:
            print(f"Ana döngü: {e}")
            time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
