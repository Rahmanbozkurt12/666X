#!/usr/bin/env python3
"""
TEK DOSYA — gecikmeli cüzdan kopya + Binance GERÇEK al-sat.

Kullanım:
  1) Aşağıya BINANCE_API_KEY / BINANCE_API_SECRET yaz (veya env koy)
  2) python mev_copy_trader.py

Kağıt test:
  python mev_copy_trader.py --dry-run

Ne yapar:
  - Listedeki cüzdanların token transferini izler
  - 60 sn sonra hâlâ tutuyorsa Binance USDT market AL/SAT
  - Aynı tx'te gir-çık MEV atomik trade → atlar
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# =============================================================================
# ANAHTARLAR — buraya yaz VEYA ortam değişkeni kullan
# =============================================================================
BINANCE_API_KEY = ""  # örn: "abc..."
BINANCE_API_SECRET = ""  # örn: "xyz..."
TELEGRAM_BOT_TOKEN = ""  # opsiyonel
TELEGRAM_CHAT_ID = ""  # opsiyonel
ETHERSCAN_API_KEY = ""  # opsiyonel — 429 azalır (ücretsiz etherscan key)

# =============================================================================
# AYAR + CÜZDANLAR (tek dosya — ayrı json gerekmez)
# =============================================================================
CONFIG: dict[str, Any] = {
    "settings": {
        "poll_seconds": 45,
        "wallet_pause_sec": 3.0,
        "copy_delay_seconds": 60,
        "min_usd_notional": 8.0,
        "max_copy_usd": 50.0,
        "copy_pct_of_free_usdt": 0.10,
        "dry_run": False,
        "trade_enabled": True,
        "prefer_binance_if_listed": True,
        "stable_symbols": [
            "USDT", "USDC", "USD1", "DAI", "FDUSD", "BUSD", "TUSD", "USDe", "USDE",
        ],
        "quote_symbols": ["WETH", "ETH", "WBTC", "BTC", "WAVAX", "AVAX"],
        "skip_spam_decimals_zero": True,
        "chains": {
            "ethereum": {
                "explorer_apis": [
                    "https://eth.blockscout.com/api",
                    "https://api.etherscan.io/v2/api?chainid=1",
                ],
                "explorer_tx": "https://etherscan.io/tx/",
                "enabled": True,
            },
            "avalanche": {
                "explorer_apis": [
                    "https://api.routescan.io/v2/network/mainnet/evm/43114/etherscan/api",
                ],
                "explorer_tx": "https://snowtrace.io/tx/",
                "enabled": True,
            },
        },
    },
    "wallets": [
        {
            "address": "0x1f2F10D1C40777AE1Da742455c65828FF36Df387",
            "label": "jaredfromsubway 2.0",
            "kind": "mev_sandwich",
            "chain": "ethereum",
            "enabled": True,
        },
        {
            "address": "0xae2Fc483527b8ef99eb5d9b44875f005ba1FaE13",
            "label": "jared EOA",
            "kind": "mev_caller",
            "chain": "ethereum",
            "enabled": True,
        },
        {
            "address": "0x278d858f05b94576C1E6f73285886876ff6eF8D2",
            "label": "UniV4 MEV executor",
            "kind": "mev_arb",
            "chain": "ethereum",
            "enabled": True,
        },
        {
            "address": "0xEff6cb8b614999d130E537751Ee99724D01aA167",
            "label": "MEV Bot Eff6",
            "kind": "mev_arb",
            "chain": "ethereum",
            "enabled": True,
        },
        {
            "address": "0x7976Da39D375dCaE90b9dE1B88C13a38F40E47Be",
            "label": "Avalanche Blackhole HF",
            "kind": "hf_mm",
            "chain": "avalanche",
            "enabled": True,
        },
        {
            "address": "0x3328F7f4A1D1C57c35df56bBf0c9dCAFCA309C49",
            "label": "Banana Gun related (ETH)",
            "kind": "sniper",
            "chain": "ethereum",
            "enabled": True,
        },
    ],
}

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "output" / "mev_copy_state.json"
SIGNALS_PATH = ROOT / "output" / "mev_copy_signals.jsonl"
# İsteğe bağlı dış config (varsa üstüne yazar)
EXTERNAL_CONFIG = ROOT / "config" / "mev_copy_wallets.json"


@dataclass
class Watched:
    address: str
    label: str
    kind: str
    chain: str


@dataclass
class PendingSignal:
    key: str
    side: str
    wallet: str
    label: str
    kind: str
    chain: str
    symbol: str
    amount: float
    tx_hash: str
    seen_at: float
    execute_at: float
    token_contract: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pick_env(*names: str, hardcoded: str = "") -> str | None:
    if hardcoded and hardcoded.strip():
        return hardcoded.strip()
    for n in names:
        v = os.environ.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def append_signal(row: dict[str, Any]) -> None:
    SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SIGNALS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def merge_config() -> dict[str, Any]:
    cfg = json.loads(json.dumps(CONFIG))  # deep copy
    if EXTERNAL_CONFIG.exists():
        try:
            raw = load_json(EXTERNAL_CONFIG)
            if isinstance(raw.get("settings"), dict):
                cfg["settings"].update(raw["settings"])
                # nested chains
                if isinstance(raw["settings"].get("chains"), dict):
                    cfg["settings"]["chains"] = raw["settings"]["chains"]
            if isinstance(raw.get("wallets"), list) and raw["wallets"]:
                cfg["wallets"] = raw["wallets"]
            print(f"[cfg] dış config yüklendi: {EXTERNAL_CONFIG}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[cfg] dış config okunamadı, gömülü kullanılıyor: {e}", flush=True)
    return cfg


def load_watched(cfg: dict[str, Any]) -> list[Watched]:
    out: list[Watched] = []
    for row in cfg.get("wallets") or []:
        if row.get("enabled") is False:
            continue
        addr = (row.get("address") or "").strip()
        if not addr.startswith("0x") or len(addr) < 42:
            continue
        out.append(
            Watched(
                address=addr.lower(),
                label=str(row.get("label") or addr[:10]),
                kind=str(row.get("kind") or "unknown"),
                chain=str(row.get("chain") or "ethereum").lower(),
            )
        )
    return out


def explorer_get(api: str, params: dict[str, Any], timeout: float = 25.0) -> Any:
    # etherscan v2 URL already has ?chainid= — merge carefully
    if "?" in api:
        base, qs = api.split("?", 1)
        url = base
        # parse existing qs into params without overwrite
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params.setdefault(k, v)
    else:
        url = api
    # Etherscan key (v2 / classic)
    ek = pick_env("ETHERSCAN_API_KEY", hardcoded=ETHERSCAN_API_KEY)
    if ek and "etherscan.io" in url:
        params.setdefault("apikey", ek)
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code == 429:
        raise requests.HTTPError("429", response=r)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "result" in data:
        # etherscan NOTOK
        if str(data.get("status")) == "0" and isinstance(data.get("result"), str):
            msg = str(data.get("result") or data.get("message") or "")
            if "rate" in msg.lower() or "Max rate" in msg:
                raise requests.HTTPError("429 " + msg, response=r)
            if "Invalid API Key" in msg or "NOTOK" in str(data.get("message") or ""):
                # başka explorer dene
                raise RuntimeError(msg)
        return data.get("result")
    return data


_last_explorer_call = 0.0
_explorer_cooldown_until = 0.0


def fetch_tokentx(
    chain_cfg: dict[str, Any], address: str, *, offset: int = 40
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Returns (rows, error).
    error='rate_limit' → çağıran bekle / pending tut
    """
    global _last_explorer_call, _explorer_cooldown_until
    now = time.time()
    if now < _explorer_cooldown_until:
        wait = _explorer_cooldown_until - now
        print(f"  · explorer cooldown {wait:.0f}s…", flush=True)
        time.sleep(wait)

    apis: list[str] = []
    if chain_cfg.get("explorer_apis"):
        apis = list(chain_cfg["explorer_apis"])
    elif chain_cfg.get("explorer_api"):
        apis = [str(chain_cfg["explorer_api"])]

    last_err: str | None = None
    for api in apis:
        # global min gap 1.4s
        gap = 1.4 - (time.time() - _last_explorer_call)
        if gap > 0:
            time.sleep(gap)
        try:
            _last_explorer_call = time.time()
            result = explorer_get(
                api,
                {
                    "module": "account",
                    "action": "tokentx",
                    "address": address,
                    "page": 1,
                    "offset": offset,
                    "sort": "desc",
                },
            )
            if isinstance(result, str):
                last_err = result[:120]
                if "rate" in result.lower():
                    _explorer_cooldown_until = time.time() + 25
                    continue
                continue
            if isinstance(result, list):
                return result, None
        except requests.HTTPError as e:
            last_err = str(e)
            resp = getattr(e, "response", None)
            code = getattr(resp, "status_code", None)
            if code == 429 or "429" in str(e):
                _explorer_cooldown_until = time.time() + 30
                print(f"  ! 429 → 30s bekle, yedek API dene ({api[:40]}…)", flush=True)
                time.sleep(5)
                continue
            print(f"  ! tokentx HTTP {address[:10]}… {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            print(f"  ! tokentx fail {address[:10]}… {e}", flush=True)
            continue

    if last_err and ("429" in last_err or "rate" in last_err.lower()):
        return [], "rate_limit"
    return [], last_err or "empty"


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
        grouped.setdefault(tx, []).append(
            {
                "side": side,
                "symbol": sym,
                "token": token,
                "amount": amt,
                "ts": int(row.get("timeStamp") or 0),
                "hash": tx,
            }
        )
    return grouped


def infer_trade(
    legs: list[dict[str, Any]], settings: dict[str, Any], kind: str
) -> dict[str, Any] | None:
    alt_in = [x for x in legs if x["side"] == "IN" and not is_stable_or_quote(x["symbol"], settings)]
    alt_out = [x for x in legs if x["side"] == "OUT" and not is_stable_or_quote(x["symbol"], settings)]
    quote_out = [x for x in legs if x["side"] == "OUT" and is_stable_or_quote(x["symbol"], settings)]
    quote_in = [x for x in legs if x["side"] == "IN" and is_stable_or_quote(x["symbol"], settings)]

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
    token = pick_env("TELEGRAM_BOT_TOKEN", hardcoded=TELEGRAM_BOT_TOKEN)
    chat = pick_env("TELEGRAM_CHAT_ID", hardcoded=TELEGRAM_CHAT_ID)
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


_EX: Any = None


def get_binance():
    global _EX
    if _EX is not None:
        return _EX
    try:
        import ccxt  # type: ignore
    except ImportError as e:
        raise RuntimeError("ccxt yok: pip install ccxt") from e
    key = pick_env("BINANCE_API_KEY", "API_KEY", hardcoded=BINANCE_API_KEY)
    secret = pick_env("BINANCE_API_SECRET", "API_SECRET", hardcoded=BINANCE_API_SECRET)
    if not key or not secret:
        raise RuntimeError("BINANCE_API_KEY / BINANCE_API_SECRET eksik")
    _EX = ccxt.binance(
        {
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot", "recvWindow": 60000},
        }
    )
    _EX.load_markets()
    return _EX


def binance_pair(base: str) -> str | None:
    b = (base or "").upper().replace("WETH", "ETH").replace("WBTC", "BTC")
    # on-chain sembol ≠ binance (PEPE ok, SKY vs MKR vs vs)
    aliases = {
        "WETH": "ETH",
        "WBTC": "BTC",
        "WBNB": "BNB",
        "WAVAX": "AVAX",
    }
    b = aliases.get(b, b)
    if b in {"USDT", "USDC", "FDUSD", "DAI", "BUSD", "TUSD"}:
        return None
    return f"{b}USDT"


def try_binance_copy(
    side: str,
    symbol_base: str,
    settings: dict[str, Any],
    *,
    live: bool,
) -> str:
    if not live:
        return "DRY_RUN (emir yok)"
    if not settings.get("prefer_binance_if_listed", True):
        return "skip_binance_off"
    pair = binance_pair(symbol_base)
    if not pair:
        return f"skip_bad_symbol:{symbol_base}"
    try:
        ex = get_binance()
    except Exception as e:  # noqa: BLE001
        return f"binance_init_err:{e}"

    if pair not in ex.markets:
        return f"skip_not_listed:{pair}"

    market = ex.markets[pair]
    min_cost = float(settings.get("min_usd_notional") or 8)
    # market limits
    try:
        lim = (market.get("limits") or {}).get("cost") or {}
        if lim.get("min"):
            min_cost = max(min_cost, float(lim["min"]))
    except Exception:  # noqa: BLE001
        pass

    try:
        bal = ex.fetch_balance()
        usdt = float((bal.get("USDT") or {}).get("free") or 0)
        max_usd = float(settings.get("max_copy_usd") or 50)
        pct = float(settings.get("copy_pct_of_free_usdt") or 0.10)
        quote = min(max_usd, usdt * pct)
        if quote < min_cost:
            return f"skip_small_balance usdt={usdt:.2f} need≥{min_cost}"

        ticker = ex.fetch_ticker(pair)
        px = float(ticker.get("last") or ticker.get("ask") or 0)
        if px <= 0:
            return "skip_no_price"

        if side == "BUY":
            # Binance spot: quoteOrderQty ile market buy — LOT_SIZE hatasını azaltır
            quote = float(ex.cost_to_precision(pair, quote))
            if quote < min_cost:
                return f"skip_quote_low {quote}"
            order = ex.create_order(
                pair,
                "market",
                "buy",
                None,
                None,
                {"quoteOrderQty": quote},
            )
            return f"BINANCE BUY {pair} ${quote} id={order.get('id')}"

        # SELL
        base = symbol_base.upper().replace("WETH", "ETH").replace("WBTC", "BTC")
        free = float((bal.get(base) or {}).get("free") or 0)
        if free <= 0:
            # bazen market base farklı
            free = float((bal.get(pair.replace("USDT", "")) or {}).get("free") or 0)
        if free <= 0:
            return f"skip_no_base:{base}"
        max_amt = quote / px
        amt = min(free, max_amt)
        amt = float(ex.amount_to_precision(pair, amt))
        if amt <= 0:
            return "skip_amt0"
        notional = amt * px
        if notional < min_cost:
            return f"skip_sell_dust ${notional:.4f}"
        order = ex.create_order(pair, "market", "sell", amt)
        return f"BINANCE SELL {pair} amt={amt} id={order.get('id')}"
    except Exception as e:  # noqa: BLE001
        return f"binance_err:{type(e).__name__}:{e}"


def process_wallet(
    w: Watched,
    settings: dict[str, Any],
    chain_cfg: dict[str, Any],
    state: dict[str, Any],
    pending: list[PendingSignal],
) -> None:
    rows, err = fetch_tokentx(chain_cfg, w.address, offset=40)
    if err == "rate_limit":
        print(f"  · rate_limit {w.label} — tur atlandı", flush=True)
        return
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
            continue
        trade = infer_trade(legs, settings, w.kind)
        if not trade:
            continue
        if trade.get("skip"):
            print(f"  · skip {w.label} {tx[:10]}… {trade['skip']}", flush=True)
            continue
        delay = float(settings.get("copy_delay_seconds") or 60)
        sig = PendingSignal(
            key=f"{w.chain}:{tx}:{trade['side']}:{trade['token']}",
            side=str(trade["side"]),
            wallet=w.address,
            label=w.label,
            kind=w.kind,
            chain=w.chain,
            symbol=str(trade["symbol"]),
            amount=float(trade["amount"]),
            tx_hash=str(trade["hash"]),
            seen_at=time.time(),
            execute_at=time.time() + delay,
            token_contract=str(trade["token"]),
        )
        if any(p.key == sig.key for p in pending):
            continue
        pending.append(sig)
        print(
            f"  → QUEUE {sig.side} {sig.symbol} | {w.label} | +{int(delay)}s | {tx[:12]}…",
            flush=True,
        )

    for tx in new_keys:
        seen.add(tx)
    state["seen_tx"][seen_key] = list(seen)[-400:]
    if boot:
        state.setdefault("bootstrapped", []).append(seen_key)
        print(f"  seed {w.label} ({len(state['seen_tx'][seen_key])} tx)", flush=True)


def still_holding(
    w_addr: str, token: str, chain_cfg: dict[str, Any], side: str
) -> bool | None:
    """True/False hold; None = API hatası (ertele)."""
    rows, err = fetch_tokentx(chain_cfg, w_addr, offset=25)
    if err == "rate_limit" or (not rows and err):
        return None
    if not rows:
        return True
    token = token.lower()
    last_in = last_out = 0
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
    if side == "BUY" and last_out and last_out >= last_in:
        return False
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
        hold = still_holding(sig.wallet, sig.token_contract, chain_cfg, sig.side)
        if hold is None:
            # rate limit — 20 sn sonra tekrar dene
            sig.execute_at = now + 20
            keep.append(sig)
            print(f"  · hold check ertelendi (429) {sig.symbol}", flush=True)
            continue
        if not hold:
            msg = f"SKIP hold yok | {sig.side} {sig.symbol} | {sig.label}"
            print(f"  × {msg}", flush=True)
            append_signal(
                {
                    "ts": now_iso(),
                    "action": "SKIP_NO_HOLD",
                    "side": sig.side,
                    "symbol": sig.symbol,
                    "label": sig.label,
                    "tx": sig.tx_hash,
                }
            )
            continue

        exec_note = try_binance_copy(sig.side, sig.symbol, settings, live=live)
        msg = (
            f"COPY {sig.side} {sig.symbol} | {sig.label} | "
            f"+{int(settings.get('copy_delay_seconds') or 60)}s | {exec_note}"
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
                "live": live,
            }
        )
        telegram_send(msg)
    return keep


COPY_STATE_PATH = ROOT / "output" / "mev_copy_state.json"
_PENDING: list[PendingSignal] = []


def try_account_copy(
    account: Any,
    side: str,
    symbol_base: str,
    settings: dict[str, Any],
    positions: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """allbinancee BinanceAccount ile gerçek AL/SAT. (note, new_pos_or_none)"""
    pair = binance_pair(symbol_base)
    if not pair:
        return f"skip_bad_symbol:{symbol_base}", None

    base = pair.replace("USDT", "")
    max_usd = float(settings.get("max_copy_usd") or 50)
    pct = float(settings.get("copy_pct_of_free_usdt") or 0.10)
    min_cost = float(settings.get("min_usd_notional") or 8)
    usdt = float(account.free_usdt())
    quote = min(max_usd, usdt * pct)
    if quote < min_cost and side == "BUY":
        return f"skip_small_balance usdt={usdt:.2f}", None

    hard_stop = float(settings.get("hard_stop_pct") or 1.5)
    quick_tp = float(settings.get("quick_tp_pct") or 2.5)

    # Binance'te yoksa filters'ta olmaz
    filters = getattr(account, "filters", None) or {}
    if filters and pair not in filters:
        return f"skip_not_listed:{pair}", None

    try:
        if side == "BUY":
            if base in positions:
                return f"skip_already_open:{base}", None
            use_limit = bool(settings.get("use_limit_orders", True))
            order = account.smart_buy_quote(
                pair, quote, use_limit=use_limit, wait_sec=float(settings.get("limit_wait_sec") or 2)
            )
            fill_quote = float(order.get("cummulativeQuoteQty") or quote)
            fill_qty = float(order.get("executedQty") or 0)
            px = float(order.get("price") or 0)
            if fill_qty <= 0 and px > 0:
                fill_qty = fill_quote / px
            if fill_qty <= 0:
                return "buy_fill0", None
            entry = fill_quote / fill_qty
            pos = {
                "symbol": pair,
                "entry": entry,
                "peak": entry,
                "qty": fill_qty,
                "stop": round(entry * (1.0 - hard_stop / 100.0), 10),
                "tp1": round(entry * (1.0 + quick_tp / 100.0), 10),
                "tp2": round(entry * (1.0 + max(quick_tp * 2, 5) / 100.0), 10),
                "score": 70,
                "pump_score": 60,
                "edge_score": 70,
                "is_uc": False,
                "ignition": False,
                "keep_runner": False,
                "cex_count": 0,
                "sector": "copy",
                "source": "wallet_copy",
                "sold_tp1": False,
                "runner": False,
                "breakeven": False,
                "opened_at": now_iso(),
            }
            return f"BOT BUY {pair} ${fill_quote:.2f} qty={fill_qty}", pos

        # SELL
        qty = 0.0
        if base in positions:
            qty = float(positions[base].get("qty") or 0)
        free = float(account.free_asset(base) or 0) if hasattr(account, "free_asset") else 0.0
        if free > 0:
            qty = free if qty <= 0 else min(qty, free)
        if qty <= 0:
            return f"skip_no_base:{base}", None
        order = account.market_sell_qty(pair, qty)
        fill_qty = float(order.get("executedQty") or qty)
        return f"BOT SELL {pair} qty={fill_qty}", {"_close": base}
    except Exception as e:  # noqa: BLE001
        return f"account_err:{type(e).__name__}:{e}", None


def run_wallet_copy_cycle(account: Any, bot_state: dict[str, Any], wc: dict[str, Any]) -> list[str]:
    """
    allbinancee her trade turunda çağırır.
    İzlenen cüzdan AL/SAT → 60sn hold → account ile Binance emir + positions güncelle.
    """
    notes: list[str] = []
    if not wc.get("enabled", True):
        return notes

    # ayarları gömülü CONFIG ile birleştir
    base_cfg = merge_config()
    settings = dict(base_cfg.get("settings") or {})
    settings.update({k: v for k, v in wc.items() if k not in ("wallets", "enabled", "chains")})
    if isinstance(wc.get("chains"), dict):
        settings["chains"] = wc["chains"]
    wallets_cfg = {"wallets": wc.get("wallets") or base_cfg.get("wallets") or []}
    watched = load_watched(wallets_cfg)
    if not watched:
        notes.append("wallet_copy: izlenecek cüzdan yok")
        return notes

    chains = settings.get("chains") or {}
    state = load_json(COPY_STATE_PATH) if COPY_STATE_PATH.exists() else {}
    global _PENDING
    pending = _PENDING
    positions: dict[str, Any] = bot_state.setdefault("positions", {})

    live = bool(getattr(account, "live", True)) and bool(settings.get("trade_enabled", True))
    notes.append(
        f"wallet_copy: {len(watched)} cüzdan · delay={settings.get('copy_delay_seconds')}s · "
        f"{'CANLI' if live else 'DRY'}"
    )

    for w in watched:
        chain_cfg = chains.get(w.chain) or {}
        if chain_cfg.get("enabled") is False:
            continue
        try:
            process_wallet(w, settings, chain_cfg, state, pending)
        except Exception as e:  # noqa: BLE001
            notes.append(f"wallet_copy scan {w.label}: {e}")
        time.sleep(float(settings.get("wallet_pause_sec") or 2.5))

    # due signals → account emir
    now = time.time()
    keep: list[PendingSignal] = []
    for sig in pending:
        if now < sig.execute_at:
            keep.append(sig)
            continue
        chain_cfg = chains.get(sig.chain) or {}
        hold = still_holding(sig.wallet, sig.token_contract, chain_cfg, sig.side)
        if hold is None:
            sig.execute_at = now + 20
            keep.append(sig)
            notes.append(f"hold ertelendi 429 · {sig.symbol}")
            continue
        if not hold:
            notes.append(f"SKIP hold yok · {sig.side} {sig.symbol} · {sig.label}")
            append_signal(
                {
                    "ts": now_iso(),
                    "action": "SKIP_NO_HOLD",
                    "side": sig.side,
                    "symbol": sig.symbol,
                    "label": sig.label,
                    "tx": sig.tx_hash,
                }
            )
            continue

        if live:
            exec_note, extra = try_account_copy(
                account, sig.side, sig.symbol, settings, positions
            )
            if extra and sig.side == "BUY" and "symbol" in extra:
                positions[extra["symbol"].replace("USDT", "")] = extra
            if extra and sig.side == "SELL" and extra.get("_close"):
                positions.pop(str(extra["_close"]), None)
        else:
            exec_note = "DRY_RUN"

        notes.append(f"COPY {sig.side} {sig.symbol} · {sig.label} · {exec_note}")
        append_signal(
            {
                "ts": now_iso(),
                "action": "COPY",
                "side": sig.side,
                "symbol": sig.symbol,
                "label": sig.label,
                "exec": exec_note,
                "via": "allbinancee",
                "tx": sig.tx_hash,
                "live": live,
            }
        )
        telegram_send(f"COPY {sig.side} {sig.symbol} | {sig.label} | {exec_note}")

    _PENDING = keep
    bot_state["positions"] = positions
    save_json(COPY_STATE_PATH, state)
    return notes


def run_loop(*, once: bool, force_dry: bool) -> int:
    cfg = merge_config()
    settings = dict(cfg.get("settings") or {})
    if force_dry:
        settings["trade_enabled"] = False
        settings["dry_run"] = True

    do_live = bool(settings.get("trade_enabled", True)) and not bool(
        settings.get("dry_run", False)
    )
    key_ok = bool(
        pick_env("BINANCE_API_KEY", "API_KEY", hardcoded=BINANCE_API_KEY)
        and pick_env("BINANCE_API_SECRET", "API_SECRET", hardcoded=BINANCE_API_SECRET)
    )
    if do_live and not key_ok:
        print(
            "[HATA] Canlı mod açık ama API key yok.\n"
            "  Dosyanın başındaki BINANCE_API_KEY / BINANCE_API_SECRET doldur\n"
            "  veya: set BINANCE_API_KEY=... & set BINANCE_API_SECRET=...",
            flush=True,
        )
        return 2

    if do_live:
        try:
            ex = get_binance()
            bal = ex.fetch_balance()
            usdt = float((bal.get("USDT") or {}).get("free") or 0)
            print(f"[binance] bağlandı · serbest USDT={usdt:.2f}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[HATA] Binance bağlanamadı: {e}", flush=True)
            return 3

    watched = load_watched(cfg)
    if not watched:
        print("izlenecek cüzdan yok", file=sys.stderr)
        return 1

    chains = settings.get("chains") or {}
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {}
    pending: list[PendingSignal] = []

    mode = "CANLI AL-SAT (Binance)" if do_live else "DRY (emir yok)"
    print(
        f"[mev-copy] mode={mode} wallets={len(watched)} "
        f"delay={settings.get('copy_delay_seconds')}s "
        f"max_copy_usd={settings.get('max_copy_usd')}",
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
            pause = float(settings.get("wallet_pause_sec") or 3.0)
            time.sleep(pause)

        pending[:] = flush_pending(pending, settings, chains, live=do_live)
        save_json(STATE_PATH, state)

        if once:
            print(f"[done] pending={len(pending)} → {SIGNALS_PATH}", flush=True)
            return 0

        sleep_for = max(3.0, float(settings.get("poll_seconds") or 15) - (time.time() - t0))
        time.sleep(sleep_for)


def main() -> int:
    p = argparse.ArgumentParser(description="Tek dosya: 60s copy → Binance canlı al-sat")
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="emir gönderme")
    args = p.parse_args()
    return run_loop(once=args.once, force_dry=bool(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
