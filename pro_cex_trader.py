#!/usr/bin/env python3
"""
PRO CEX TRADER — radar YÜKSEK sinyallerini Binance spot'ta al/sat.

Kurallar:
  • Sadece conviction == YUKSEK coinleri alır
  • En fazla MAX_POSITIONS (varsayılan 10) açık pozisyon
  • Serbest USDT bakiyeyi bu turda alınacak coine EŞİT böler
  • Satış: SL tam çıkış · TP1 %50 · TP2 kalanı
  • Varsayılan DRY-RUN (emir atmaz). Canlı: LIVE=1 + API key

Kullanım:
  python3 pro_cex_trader.py --once --dry-run
  LIVE=1 python3 pro_cex_trader.py --once

Gerekli env (canlı):
  BINANCE_API_KEY / BINANCE_API_SECRET
  opsiyonel: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Script klasörünü path'e ekle (VS Code / output/ içinden çalışınca)
_SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (_SCRIPT_DIR, _SCRIPT_DIR.parent):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

try:
    import pro_cex_radar as radar
except ImportError:
    raise SystemExit(
        "\n[HATA] pro_cex_radar.py bulunamadı.\n\n"
        "KOLAY ÇÖZÜM — tek dosya kullan:\n"
        "  python pro_cex_bot.py --once --dry-run --fast\n\n"
        "VEYA aynı klasöre koy:\n"
        "  - pro_cex_radar.py\n"
        "  - pro_cex_trader.py\n"
        "  cd C:\\Users\\Rahman\\OneDrive\\Desktop\\bot\n"
        "  python pro_cex_trader.py --once --dry-run\n"
    ) from None

ROOT = radar.ROOT
OUTPUT_DIR = radar.OUTPUT_DIR
POS_PATH = OUTPUT_DIR / "pro_cex_positions.json"
TRADE_LOG = OUTPUT_DIR / "pro_cex_trades.jsonl"
CONFIG_NAME = "pro_cex_trader.json"

DEFAULT_TRADER: dict[str, Any] = {
    "max_positions": 10,
    "quote": "USDT",
    "deploy_pct": 0.95,
    "min_order_usdt": 11.0,
    "tp1_sell_pct": 0.50,
    "poll_seconds": 180,
    "use_radar_fast": False,
    "rest_base": "https://data-api.binance.vision",
    "trade_base": "https://api.binance.com",
    "recv_window": 5000,
}

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "pro-cex-trader/1.0"})


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) and v.strip() else default


def load_trader_config(cli: str | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULT_TRADER)
    paths = []
    if cli:
        paths.append(Path(cli))
    paths += [
        ROOT / "config" / CONFIG_NAME,
        ROOT / CONFIG_NAME,
        Path.cwd() / "config" / CONFIG_NAME,
    ]
    for p in paths:
        if p.is_file():
            raw = radar.load_json(p)
            cfg.update(raw)
            print(f"[trader-config] {p}")
            return cfg
    print("[trader-config] gömülü varsayılan")
    return cfg


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_trade(row: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with TRADE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Binance helpers (public + signed)
# ---------------------------------------------------------------------------

def public_get(base: str, path: str, params: dict[str, Any] | None = None) -> Any:
    r = HTTP.get(f"{base}{path}", params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def signed_request(
    method: str,
    trade_base: str,
    path: str,
    api_key: str,
    api_secret: str,
    params: dict[str, Any] | None = None,
    recv_window: int = 5000,
) -> Any:
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = recv_window
    query = urllib.parse.urlencode(params, doseq=True)
    sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{trade_base}{path}?{query}&signature={sig}"
    headers = {"X-MBX-APIKEY": api_key}
    if method == "GET":
        r = HTTP.get(url, headers=headers, timeout=30)
    elif method == "POST":
        r = HTTP.post(url, headers=headers, timeout=30)
    elif method == "DELETE":
        r = HTTP.delete(url, headers=headers, timeout=30)
    else:
        raise ValueError(method)
    if r.status_code >= 400:
        raise RuntimeError(f"Binance {r.status_code}: {r.text[:400]}")
    return r.json()


def load_filters(rest_base: str) -> dict[str, dict[str, float]]:
    """symbol -> {stepSize, minQty, minNotional}"""
    info = public_get(rest_base, "/api/v3/exchangeInfo")
    out: dict[str, dict[str, float]] = {}
    for s in info.get("symbols", []):
        sym = s["symbol"]
        step = min_qty = min_notional = 0.0
        for f in s.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step = float(f["stepSize"])
                min_qty = float(f["minQty"])
            elif f.get("filterType") in {"MIN_NOTIONAL", "NOTIONAL"}:
                min_notional = float(f.get("minNotional") or f.get("notional") or 0)
        out[sym] = {"stepSize": step, "minQty": min_qty, "minNotional": min_notional}
    return out


def round_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    precision = max(0, int(round(-math.log10(step)))) if step < 1 else 0
    floored = math.floor(qty / step) * step
    return float(f"{floored:.{precision}f}")


def get_price(rest_base: str, symbol: str) -> float:
    t = public_get(rest_base, "/api/v3/ticker/price", {"symbol": symbol})
    return float(t["price"])


class BinanceAccount:
    def __init__(self, cfg: dict[str, Any], live: bool):
        self.cfg = cfg
        self.live = live
        self.api_key = env("BINANCE_API_KEY") or ""
        self.api_secret = env("BINANCE_API_SECRET") or ""
        self.trade_base = cfg.get("trade_base") or "https://api.binance.com"
        self.rest_base = cfg.get("rest_base") or "https://data-api.binance.vision"
        self.recv = int(cfg.get("recv_window") or 5000)
        self.filters = load_filters(self.rest_base)
        self._paper_usdt = float(env("PAPER_USDT") or 1000)
        self._paper_balances: dict[str, float] = {"USDT": self._paper_usdt}

        if self.live and not (self.api_key and self.api_secret):
            raise SystemExit("LIVE=1 için BINANCE_API_KEY + BINANCE_API_SECRET gerekli")

    def free_usdt(self) -> float:
        if not self.live:
            return float(self._paper_balances.get("USDT", 0))
        acc = signed_request(
            "GET", self.trade_base, "/api/v3/account",
            self.api_key, self.api_secret, recv_window=self.recv,
        )
        for b in acc.get("balances", []):
            if b.get("asset") == "USDT":
                return float(b.get("free") or 0)
        return 0.0

    def free_asset(self, asset: str) -> float:
        if not self.live:
            return float(self._paper_balances.get(asset, 0))
        acc = signed_request(
            "GET", self.trade_base, "/api/v3/account",
            self.api_key, self.api_secret, recv_window=self.recv,
        )
        for b in acc.get("balances", []):
            if b.get("asset") == asset:
                return float(b.get("free") or 0)
        return 0.0

    def market_buy_quote(self, symbol: str, quote_usdt: float) -> dict[str, Any]:
        """USDT ile market alış (quoteOrderQty)."""
        price = get_price(self.rest_base, symbol)
        if not self.live:
            qty = quote_usdt / price if price > 0 else 0
            base = symbol.replace("USDT", "")
            self._paper_balances["USDT"] = self._paper_balances.get("USDT", 0) - quote_usdt
            self._paper_balances[base] = self._paper_balances.get(base, 0) + qty
            return {
                "symbol": symbol,
                "side": "BUY",
                "status": "FILLED",
                "price": price,
                "executedQty": str(qty),
                "cummulativeQuoteQty": str(quote_usdt),
                "paper": True,
            }
        return signed_request(
            "POST",
            self.trade_base,
            "/api/v3/order",
            self.api_key,
            self.api_secret,
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": f"{quote_usdt:.2f}",
            },
            recv_window=self.recv,
        )

    def market_sell_qty(self, symbol: str, qty: float) -> dict[str, Any]:
        filt = self.filters.get(symbol) or {}
        step = float(filt.get("stepSize") or 0)
        qty = round_step(qty, step) if step else qty
        if qty <= 0:
            raise RuntimeError(f"qty=0 {symbol}")
        price = get_price(self.rest_base, symbol)
        if not self.live:
            base = symbol.replace("USDT", "")
            have = self._paper_balances.get(base, 0)
            qty = min(qty, have)
            self._paper_balances[base] = have - qty
            self._paper_balances["USDT"] = self._paper_balances.get("USDT", 0) + qty * price
            return {
                "symbol": symbol,
                "side": "SELL",
                "status": "FILLED",
                "price": price,
                "executedQty": str(qty),
                "cummulativeQuoteQty": str(qty * price),
                "paper": True,
            }
        # quantity format
        step = float(filt.get("stepSize") or 0.0001)
        precision = max(0, int(round(-math.log10(step)))) if step < 1 else 0
        qstr = f"{qty:.{precision}f}"
        return signed_request(
            "POST",
            self.trade_base,
            "/api/v3/order",
            self.api_key,
            self.api_secret,
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": qstr,
            },
            recv_window=self.recv,
        )


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

def load_positions() -> dict[str, Any]:
    if POS_PATH.exists():
        return radar.load_json(POS_PATH)
    return {"positions": {}, "updated_at": None}


def save_positions(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    radar.save_json(POS_PATH, state)


def manage_exits(account: BinanceAccount, state: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    """SL / TP1 / TP2 satışları."""
    notes: list[str] = []
    positions: dict[str, Any] = state.get("positions") or {}
    tp1_pct = float(cfg.get("tp1_sell_pct") or 0.5)
    rest = account.rest_base
    closed: list[str] = []

    for base, pos in list(positions.items()):
        symbol = pos["symbol"]
        try:
            price = get_price(rest, symbol)
        except Exception as exc:
            notes.append(f"price fail {symbol}: {exc}")
            continue
        entry = float(pos["entry"])
        stop = float(pos["stop"])
        tp1 = float(pos["tp1"])
        tp2 = float(pos["tp2"])
        qty = float(pos["qty"])
        sold_tp1 = bool(pos.get("sold_tp1"))

        # SL
        if price <= stop:
            try:
                order = account.market_sell_qty(symbol, qty)
                fill_qty = float(order.get("executedQty") or qty)
                notes.append(f"🛑 SL SAT {base} @ {price} qty={fill_qty}")
                log_trade(
                    {
                        "ts": now_iso(),
                        "action": "SELL_SL",
                        "base": base,
                        "symbol": symbol,
                        "price": price,
                        "qty": fill_qty,
                        "entry": entry,
                        "pnl_pct": round((price / entry - 1) * 100, 3),
                        "live": account.live,
                        "order": order,
                    }
                )
                closed.append(base)
            except Exception as exc:
                notes.append(f"SL fail {base}: {exc}")
            continue

        # TP1 partial
        if price >= tp1 and not sold_tp1:
            sell_qty = qty * tp1_pct
            try:
                order = account.market_sell_qty(symbol, sell_qty)
                fill_qty = float(order.get("executedQty") or sell_qty)
                pos["qty"] = max(0.0, qty - fill_qty)
                pos["sold_tp1"] = True
                notes.append(f"🎯 TP1 SAT %{tp1_pct*100:.0f} {base} @ {price}")
                log_trade(
                    {
                        "ts": now_iso(),
                        "action": "SELL_TP1",
                        "base": base,
                        "symbol": symbol,
                        "price": price,
                        "qty": fill_qty,
                        "entry": entry,
                        "pnl_pct": round((price / entry - 1) * 100, 3),
                        "live": account.live,
                        "order": order,
                    }
                )
            except Exception as exc:
                notes.append(f"TP1 fail {base}: {exc}")
            continue

        # TP2 full remaining
        if price >= tp2:
            try:
                order = account.market_sell_qty(symbol, qty)
                fill_qty = float(order.get("executedQty") or qty)
                notes.append(f"🏁 TP2 SAT {base} @ {price} qty={fill_qty}")
                log_trade(
                    {
                        "ts": now_iso(),
                        "action": "SELL_TP2",
                        "base": base,
                        "symbol": symbol,
                        "price": price,
                        "qty": fill_qty,
                        "entry": entry,
                        "pnl_pct": round((price / entry - 1) * 100, 3),
                        "live": account.live,
                        "order": order,
                    }
                )
                closed.append(base)
            except Exception as exc:
                notes.append(f"TP2 fail {base}: {exc}")

    for b in closed:
        positions.pop(b, None)
    state["positions"] = positions
    return notes


def manage_entries(
    account: BinanceAccount,
    state: dict[str, Any],
    signals: list[radar.Signal],
    cfg: dict[str, Any],
) -> list[str]:
    """YÜKSEK sinyalleri al — max 10, bakiyeyi böl."""
    notes: list[str] = []
    positions: dict[str, Any] = state.get("positions") or {}
    max_pos = int(cfg.get("max_positions") or 10)
    min_order = float(cfg.get("min_order_usdt") or 11)
    deploy = float(cfg.get("deploy_pct") or 0.95)

    slots = max_pos - len(positions)
    if slots <= 0:
        notes.append(f"pozisyon dolu ({len(positions)}/{max_pos})")
        return notes

    # adaylar: YUKSEK, henüz yok
    cands = [
        s for s in signals
        if s.conviction == "YUKSEK" and s.base not in positions and s.stop and s.tp1 and s.tp2
    ]
    cands.sort(key=lambda s: -s.score)
    cands = cands[:slots]
    if not cands:
        notes.append("yeni YÜKSEK aday yok")
        return notes

    free = account.free_usdt()
    budget = free * deploy
    per = budget / len(cands)
    if per < min_order:
        # daha az coin al
        n = int(budget // min_order)
        if n <= 0:
            notes.append(f"USDT yetersiz free={free:.2f} (min {min_order})")
            return notes
        cands = cands[:n]
        per = budget / len(cands)

    notes.append(
        f"AL planı: {len(cands)} coin × ~{per:.2f} USDT "
        f"(free={free:.2f}, max={max_pos})"
    )

    for sig in cands:
        symbol = sig.symbol
        filt = account.filters.get(symbol) or {}
        min_notional = float(filt.get("minNotional") or min_order)
        quote = max(per, min_notional)
        if account.free_usdt() < quote:
            notes.append(f"bakiye bitti, {sig.base} atlandı")
            break
        try:
            order = account.market_buy_quote(symbol, quote)
            # fill parse
            fill_quote = float(order.get("cummulativeQuoteQty") or quote)
            fill_qty = float(order.get("executedQty") or 0)
            px = float(order.get("price") or 0)
            if fill_qty <= 0 and px > 0:
                fill_qty = fill_quote / px
            if fill_qty <= 0:
                px = get_price(account.rest_base, symbol)
                fill_qty = fill_quote / px
            entry = fill_quote / fill_qty if fill_qty else get_price(account.rest_base, symbol)
            positions[sig.base] = {
                "symbol": symbol,
                "entry": entry,
                "qty": fill_qty,
                "stop": sig.stop,
                "tp1": sig.tp1,
                "tp2": sig.tp2,
                "score": sig.score,
                "cex_count": sig.cex_count,
                "sold_tp1": False,
                "opened_at": now_iso(),
                "reasons": sig.reasons[:8],
            }
            notes.append(
                f"🟢 AL {sig.base} ~{fill_quote:.2f} USDT @ {entry:.8g} "
                f"SL {sig.stop} TP1 {sig.tp1} TP2 {sig.tp2}"
            )
            log_trade(
                {
                    "ts": now_iso(),
                    "action": "BUY",
                    "base": sig.base,
                    "symbol": symbol,
                    "price": entry,
                    "qty": fill_qty,
                    "quote": fill_quote,
                    "stop": sig.stop,
                    "tp1": sig.tp1,
                    "tp2": sig.tp2,
                    "score": sig.score,
                    "live": account.live,
                    "order": {
                        k: order.get(k)
                        for k in ("orderId", "status", "paper", "executedQty")
                        if k in order
                    },
                }
            )
        except Exception as exc:
            notes.append(f"AL fail {sig.base}: {exc}")

    state["positions"] = positions
    return notes


def print_portfolio(account: BinanceAccount, state: dict[str, Any]) -> None:
    positions = state.get("positions") or {}
    print(f"\n═══ PORTFÖY ({len(positions)}/10) · USDT free={account.free_usdt():.2f} ═══")
    if not positions:
        print("  (boş)")
        return
    for base, pos in positions.items():
        try:
            px = get_price(account.rest_base, pos["symbol"])
        except Exception:
            px = float(pos["entry"])
        entry = float(pos["entry"])
        pnl = (px / entry - 1) * 100
        print(
            f"  {base:<8} qty={float(pos['qty']):.6g}  entry={entry:.6g}  "
            f"now={px:.6g}  PnL%{pnl:+.2f}  "
            f"SL {pos['stop']}  TP1 {pos['tp1']}  TP2 {pos['tp2']}"
            f"{'  [TP1✓]' if pos.get('sold_tp1') else ''}"
        )


def run_cycle(
    account: BinanceAccount,
    radar_cfg: dict[str, Any],
    trader_cfg: dict[str, Any],
    *,
    fast: bool,
) -> None:
    if fast:
        radar_cfg = dict(radar_cfg)
        radar_cfg["cex_max_symbols"] = 30
        radar_cfg["max_binance_symbols"] = 120

    state = load_positions()

    print("\n[exit] açık pozisyonlar kontrol…")
    for note in manage_exits(account, state, trader_cfg):
        print(" ", note)
    save_positions(state)

    print("\n[radar] tarama…")
    rows, meta = radar.run_scan(radar_cfg)
    print(radar.format_report(rows, meta, top=10))

    print("\n[entry] YÜKSEK alımlar…")
    for note in manage_entries(account, state, rows, trader_cfg):
        print(" ", note)
    save_positions(state)
    print_portfolio(account, state)


def main() -> int:
    ap = argparse.ArgumentParser(description="PRO CEX Binance al/sat (max 10 coin)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Emir atma (paper)")
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--config", default=None, help="radar config")
    ap.add_argument("--trader-config", default=None)
    args = ap.parse_args()

    live_env = (env("LIVE") or "0") == "1"
    live = live_env and not args.dry_run
    if live_env and args.dry_run:
        print("[info] --dry-run LIVE'ı eziyor → paper")
    if not live:
        print("[MODE] DRY-RUN / PAPER — gerçek emir YOK")
    else:
        print("[MODE] ⚠️  LIVE Binance spot — gerçek para")

    radar_cfg, src = radar.load_config(args.config)
    print(f"[radar-config] {src}")
    trader_cfg = load_trader_config(args.trader_config)

    account = BinanceAccount(trader_cfg, live=live)
    print(f"[account] USDT free ≈ {account.free_usdt():.2f}")

    poll = int(trader_cfg.get("poll_seconds") or 180)
    while True:
        try:
            run_cycle(account, radar_cfg, trader_cfg, fast=args.fast)
        except Exception as exc:
            print(f"[HATA] {exc}", file=sys.stderr)
        if args.once:
            break
        print(f"\n[sleep] {poll}s…")
        time.sleep(poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
