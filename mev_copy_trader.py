#!/usr/bin/env python3
"""
Gecikmeli smart-copy bot (varsayılan 60 sn).

Ne yapar:
  - Verdiğin cüzdanların ERC20 transferlerini izler
  - AL / SAT sinyali üretir
  - copy_delay_seconds sonra hâlâ tutuyor mu bakar (hold confirm)
  - Aynı blok / ~12 sn içinde gir-çık MEV atomik trade'leri ATLAR
  - Varsayılan: trade_enabled=true → Binance key varsa GERÇEK market emir
  - --dry-run ile kağıt moduna düşer (emir yok)
  - Token Binance USDT'te listeliyse bakiyene göre küçük market AL/SAT

ÖNEMLİ:
  Jared / UniV4 / Eff6 tipi MEV botlar çoğu alımı aynı tx'te satar.
  60 sn sonra kopyalamak genelde ZARAR eder — hold filtresi bu yüzden var.
  Asıl işe yarayan: sniper / smart-money cüzdanları (dakikalarca tutanlar).

Kullanım:
  python mev_copy_trader.py                 # CANLI al-sat (key gerekir)
  python mev_copy_trader.py --dry-run       # sadece sinyal, emir yok
  python mev_copy_trader.py --once --dry-run

Env:
  BINANCE_API_KEY, BINANCE_API_SECRET       # zorunlu (canlı için)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID      # opsiyonel
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "mev_copy_wallets.json"
STATE_PATH = ROOT / "output" / "mev_copy_state.json"
SIGNALS_PATH = ROOT / "output" / "mev_copy_signals.jsonl"


@dataclass
class Watched:
    address: str
    label: str
    kind: str
    chain: str
    enabled: bool = True


@dataclass
class PendingSignal:
    key: str
    side: str  # BUY | SELL
    wallet: str
    label: str
    kind: str
    chain: str
    token: str
    symbol: str
    amount: float
    tx_hash: str
    seen_at: float
    execute_at: float
    token_contract: str = ""
    usd_hint: float | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) and v.strip() else default


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def append_signal(row: dict[str, Any]) -> None:
    SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SIGNALS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_watched(cfg: dict[str, Any]) -> list[Watched]:
    out: list[Watched] = []
    for row in cfg.get("wallets") or []:
        if row.get("enabled") is False:
            continue
        addr = (row.get("address") or "").strip()
        if not addr or not addr.startswith("0x") or len(addr) < 42:
            continue
        chain = (row.get("chain") or "ethereum").lower()
        out.append(
            Watched(
                address=addr.lower(),
                label=str(row.get("label") or addr[:10]),
                kind=str(row.get("kind") or "unknown"),
                chain=chain,
                enabled=True,
            )
        )
    return out


def explorer_get(api: str, params: dict[str, Any], timeout: float = 25.0) -> Any:
    r = requests.get(api, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    # etherscan-style
    if isinstance(data, dict) and "result" in data:
        return data.get("result")
    return data


def fetch_tokentx(
    api: str, address: str, *, page: int = 1, offset: int = 40
) -> list[dict[str, Any]]:
    try:
        result = explorer_get(
            api,
            {
                "module": "account",
                "action": "tokentx",
                "address": address,
                "page": page,
                "offset": offset,
                "sort": "desc",
            },
        )
    except Exception as e:  # noqa: BLE001
        print(f"  ! tokentx fail {address[:10]}… {e}", flush=True)
        return []
    if isinstance(result, str):
        print(f"  ! tokentx msg {address[:10]}… {result[:120]}", flush=True)
        return []
    if not isinstance(result, list):
        return []
    return result


def is_stable_or_quote(symbol: str, settings: dict[str, Any]) -> bool:
    s = (symbol or "").upper()
    stables = {x.upper() for x in (settings.get("stable_symbols") or [])}
    quotes = {x.upper() for x in (settings.get("quote_symbols") or [])}
    return s in stables or s in quotes


def parse_amount(row: dict[str, Any]) -> float:
    try:
        raw = float(row.get("value") or 0)
        dec = int(row.get("tokenDecimal") or 18)
        return raw / (10**dec)
    except (TypeError, ValueError):
        return 0.0


def classify_leg(
    row: dict[str, Any], wallet: str, settings: dict[str, Any]
) -> tuple[str | None, str, str, float]:
    """Return (side_hint, symbol, token_addr, amount). side_hint: IN/OUT for wallet."""
    fr = (row.get("from") or "").lower()
    to = (row.get("to") or "").lower()
    sym = str(row.get("tokenSymbol") or "?")
    token = (row.get("contractAddress") or "").lower()
    amt = parse_amount(row)
    if settings.get("skip_spam_decimals_zero") and int(row.get("tokenDecimal") or 18) == 0:
        return None, sym, token, amt
    if to == wallet:
        return "IN", sym, token, amt
    if fr == wallet:
        return "OUT", sym, token, amt
    return None, sym, token, amt


def group_by_tx(
    rows: list[dict[str, Any]], wallet: str, settings: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        side, sym, token, amt = classify_leg(row, wallet, settings)
        if side is None or amt <= 0:
            continue
        tx = (row.get("hash") or "").lower()
        if not tx:
            continue
        ts = int(row.get("timeStamp") or 0)
        grouped.setdefault(tx, []).append(
            {
                "side": side,
                "symbol": sym,
                "token": token,
                "amount": amt,
                "ts": ts,
                "hash": tx,
            }
        )
    return grouped


def infer_trade(
    legs: list[dict[str, Any]], settings: dict[str, Any], kind: str
) -> dict[str, Any] | None:
    """
    Tek tx içindeki token bacaklarından yönsel AL/SAT çıkar.
    Aynı tx'te hem altcoin IN hem OUT → atomik MEV → None.
    """
    alt_in = [x for x in legs if x["side"] == "IN" and not is_stable_or_quote(x["symbol"], settings)]
    alt_out = [x for x in legs if x["side"] == "OUT" and not is_stable_or_quote(x["symbol"], settings)]
    quote_out = [x for x in legs if x["side"] == "OUT" and is_stable_or_quote(x["symbol"], settings)]
    quote_in = [x for x in legs if x["side"] == "IN" and is_stable_or_quote(x["symbol"], settings)]

    # Atomik sandwich/arb: aynı tx'te altcoin girip çıkmış
    if alt_in and alt_out:
        return {"skip": "atomic_mev_in_out", "kind": kind}

    if alt_in and (quote_out or not alt_out):
        best = max(alt_in, key=lambda x: x["amount"])
        return {
            "side": "BUY",
            "symbol": best["symbol"],
            "token": best["token"],
            "amount": best["amount"],
            "ts": best["ts"],
            "hash": best["hash"],
        }
    if alt_out and (quote_in or not alt_in):
        best = max(alt_out, key=lambda x: x["amount"])
        return {
            "side": "SELL",
            "symbol": best["symbol"],
            "token": best["token"],
            "amount": best["amount"],
            "ts": best["ts"],
            "hash": best["hash"],
        }
    return None


def telegram_send(text: str) -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    chat = env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[:3500], "disable_web_page_preview": True},
            timeout=20,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  ! telegram {e}", flush=True)


def binance_symbol(base: str) -> str | None:
    base = base.upper().replace("WETH", "ETH").replace("WBTC", "BTC")
    if base in {"USDT", "USDC", "ETH", "BTC", "BNB"}:
        return None
    return f"{base}USDT"


def try_binance_copy(
    side: str,
    symbol_base: str,
    settings: dict[str, Any],
    *,
    live: bool,
) -> str:
    if not live or not settings.get("prefer_binance_if_listed", True):
        return "skip_not_live"
    key = env("BINANCE_API_KEY") or env("API_KEY")
    secret = env("BINANCE_API_SECRET") or env("API_SECRET")
    if not key or not secret:
        return "skip_no_binance_keys"
    pair = binance_symbol(symbol_base)
    if not pair:
        return "skip_bad_symbol"
    try:
        import ccxt  # type: ignore
    except ImportError:
        return "skip_no_ccxt"
    try:
        ex = ccxt.binance(
            {
                "apiKey": key,
                "secret": secret,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        markets = ex.load_markets()
        if pair not in markets:
            return f"skip_not_listed:{pair}"
        quote = float(settings.get("max_copy_usd") or 50)
        bal = ex.fetch_balance()
        usdt = float((bal.get("USDT") or {}).get("free") or 0)
        quote = min(quote, max(0.0, usdt * float(settings.get("copy_pct_of_signal") or 0.05) * 20))
        # basit tavan: max_copy_usd
        quote = min(quote, float(settings.get("max_copy_usd") or 50))
        if quote < 6:
            return f"skip_small_balance usdt={usdt:.2f}"
        ticker = ex.fetch_ticker(pair)
        px = float(ticker.get("last") or 0) or 0.0
        if px <= 0:
            return "skip_no_price"
        if side == "BUY":
            amt = quote / px
            order = ex.create_market_buy_order(pair, amt)
            return f"BINANCE BUY {pair} ~${quote:.2f} amt={amt} id={order.get('id')}"
        base_free = float((bal.get(symbol_base.upper()) or {}).get("free") or 0)
        if base_free <= 0:
            return f"skip_no_base {symbol_base}"
        amt = min(base_free, quote / px)
        order = ex.create_market_sell_order(pair, amt)
        return f"BINANCE SELL {pair} amt={amt} id={order.get('id')}"
    except Exception as e:  # noqa: BLE001
        return f"binance_err:{e}"


def process_wallet(
    w: Watched,
    settings: dict[str, Any],
    chain_cfg: dict[str, Any],
    state: dict[str, Any],
    pending: list[PendingSignal],
) -> None:
    api = chain_cfg.get("explorer_api")
    if not api:
        return
    rows = fetch_tokentx(api, w.address, offset=50)
    if not rows:
        return

    seen_key = f"{w.chain}:{w.address}"
    seen: set[str] = set(state.setdefault("seen_tx", {}).setdefault(seen_key, []))
    boot = seen_key not in state.setdefault("bootstrapped", [])

    grouped = group_by_tx(rows, w.address, settings)
    new_keys: list[str] = []

    for tx, legs in grouped.items():
        if tx in seen:
            continue
        new_keys.append(tx)
        if boot:
            continue  # ilk turda spam yok, sadece seed
        trade = infer_trade(legs, settings, w.kind)
        if not trade:
            continue
        if trade.get("skip"):
            print(
                f"  · skip {w.label} {tx[:10]}… {trade['skip']}",
                flush=True,
            )
            continue
        side = trade["side"]
        delay = float(settings.get("copy_delay_seconds") or 60)
        sig = PendingSignal(
            key=f"{w.chain}:{tx}:{side}:{trade['token']}",
            side=side,
            wallet=w.address,
            label=w.label,
            kind=w.kind,
            chain=w.chain,
            token=trade["symbol"],
            symbol=trade["symbol"],
            amount=float(trade["amount"]),
            tx_hash=trade["hash"],
            seen_at=time.time(),
            execute_at=time.time() + delay,
            token_contract=trade["token"],
        )
        # aynı sinyal kuyrukta varsa ekleme
        if any(p.key == sig.key for p in pending):
            continue
        pending.append(sig)
        print(
            f"  → QUEUE {side} {sig.symbol} from {w.label} "
            f"delay={int(delay)}s tx={tx[:12]}… kind={w.kind}",
            flush=True,
        )

    # seen güncelle (son 400 tut)
    for tx in new_keys:
        seen.add(tx)
    trimmed = list(seen)[-400:]
    state["seen_tx"][seen_key] = trimmed
    if boot:
        state.setdefault("bootstrapped", []).append(seen_key)
        print(f"  seed {w.label} ({len(trimmed)} tx)", flush=True)


def still_holding(
    w_addr: str,
    token: str,
    chain_cfg: dict[str, Any],
    side: str,
    settings: dict[str, Any],
) -> bool:
    """
    Delay sonrası kaba hold kontrolü:
    son transferlerde token hâlâ cüzdanda görünüyor mu / yeni OUT var mı.
    """
    api = chain_cfg.get("explorer_api")
    if not api:
        return True
    rows = fetch_tokentx(api, w_addr, offset=30)
    if not rows:
        return True
    token = token.lower()
    last_in = 0
    last_out = 0
    for row in rows:
        if (row.get("contractAddress") or "").lower() != token:
            continue
        ts = int(row.get("timeStamp") or 0)
        fr = (row.get("from") or "").lower()
        to = (row.get("to") or "").lower()
        if to == w_addr:
            last_in = max(last_in, ts)
        if fr == w_addr:
            last_out = max(last_out, ts)
    if side == "BUY":
        # sattıysa kopyalama
        if last_out and last_out >= last_in:
            return False
        return True
    if side == "SELL":
        return True
    return True


def flush_pending(
    pending: list[PendingSignal],
    settings: dict[str, Any],
    chains: dict[str, Any],
    *,
    live: bool,
) -> list[PendingSignal]:
    keep: list[PendingSignal] = []
    now = time.time()
    for sig in pending:
        if now < sig.execute_at:
            keep.append(sig)
            continue
        chain_cfg = chains.get(sig.chain) or {}
        ok_hold = still_holding(
            sig.wallet, sig.token_contract, chain_cfg, sig.side, settings
        )
        if not ok_hold:
            msg = (
                f"SKIP hold yok | {sig.side} {sig.symbol} | {sig.label} | "
                f"tx={sig.tx_hash[:14]}… (MEV/hızlı çıkış)"
            )
            print(f"  × {msg}", flush=True)
            append_signal(
                {
                    "ts": now_iso(),
                    "action": "SKIP_NO_HOLD",
                    "side": sig.side,
                    "symbol": sig.symbol,
                    "label": sig.label,
                    "kind": sig.kind,
                    "tx": sig.tx_hash,
                }
            )
            continue

        exec_note = "DRY_RUN"
        if live and settings.get("trade_enabled"):
            exec_note = try_binance_copy(sig.side, sig.symbol, settings, live=True)
        else:
            exec_note = try_binance_copy(sig.side, sig.symbol, settings, live=False)

        msg = (
            f"COPY {sig.side} {sig.symbol} amt≈{sig.amount:.6g} | "
            f"{sig.label} ({sig.kind}) | +{int(settings.get('copy_delay_seconds') or 60)}s | "
            f"{exec_note} | {sig.tx_hash}"
        )
        print(f"  ✓ {msg}", flush=True)
        append_signal(
            {
                "ts": now_iso(),
                "action": "COPY",
                "side": sig.side,
                "symbol": sig.symbol,
                "amount": sig.amount,
                "label": sig.label,
                "kind": sig.kind,
                "chain": sig.chain,
                "tx": sig.tx_hash,
                "exec": exec_note,
                "live": bool(live and settings.get("trade_enabled")),
            }
        )
        telegram_send(msg)
    return keep


def run_loop(*, once: bool, live: bool, force_dry: bool, config_path: Path) -> int:
    cfg = load_json(config_path)
    settings = dict(cfg.get("settings") or {})
    # Config varsayılan canlı; --live zorla açar; --dry-run kağıt moda düşürür
    if live:
        settings["trade_enabled"] = True
        settings["dry_run"] = False
    if force_dry:
        settings["trade_enabled"] = False
        settings["dry_run"] = True

    do_live = bool(settings.get("trade_enabled")) and not bool(settings.get("dry_run"))
    chains = settings.get("chains") or {}
    watched = load_watched(cfg)
    if not watched:
        print("izlenecek cüzdan yok — config/mev_copy_wallets.json", file=sys.stderr)
        return 1

    if do_live and not (env("BINANCE_API_KEY") or env("API_KEY")):
        print(
            "[UYARI] trade_enabled=true ama BINANCE_API_KEY yok — emir gidemez, sinyal yazar",
            flush=True,
        )
        do_live = False

    state = load_json(STATE_PATH) if STATE_PATH.exists() else {}
    pending: list[PendingSignal] = []
    # restore pending keys lightly skipped (process restart = drop queue — güvenli)

    mode = "CANLI AL-SAT" if do_live else "DRY (emir yok)"
    print(
        f"[mev-copy] mode={mode} wallets={len(watched)} "
        f"delay={settings.get('copy_delay_seconds')}s "
        f"max_copy_usd={settings.get('max_copy_usd')} "
        f"trade={settings.get('trade_enabled')} dry_run={settings.get('dry_run')}",
        flush=True,
    )
    for w in watched:
        print(f"  watch {w.label} | {w.kind} | {w.chain} | {w.address}", flush=True)

    while True:
        t0 = time.time()
        for w in watched:
            chain_cfg = chains.get(w.chain) or {}
            if chain_cfg.get("enabled") is False:
                continue
            try:
                process_wallet(w, settings, chain_cfg, state, pending)
            except Exception as e:  # noqa: BLE001
                print(f"  ! {w.label} {e}", flush=True)
            time.sleep(1.1)

        pending[:] = flush_pending(pending, settings, chains, live=do_live)
        save_json(STATE_PATH, state)

        if once:
            print(f"[done] pending_left={len(pending)} signals→{SIGNALS_PATH}", flush=True)
            return 0

        sleep_for = max(3.0, float(settings.get("poll_seconds") or 15) - (time.time() - t0))
        time.sleep(sleep_for)


def main() -> int:
    p = argparse.ArgumentParser(description="60s delayed wallet copy trader (live by default)")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="force paper mode (no orders)")
    p.add_argument(
        "--live",
        action="store_true",
        help="force live even if config dry (keys required)",
    )
    args = p.parse_args()
    if args.live and args.dry_run:
        print("--live ve --dry-run birlikte olmaz", file=sys.stderr)
        return 2
    return run_loop(
        once=args.once,
        live=bool(args.live),
        force_dry=bool(args.dry_run),
        config_path=Path(args.config),
    )


if __name__ == "__main__":
    raise SystemExit(main())
