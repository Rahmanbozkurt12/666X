#!/usr/bin/env python3
"""
CEX / DEX / pump.fun cüzdan hareketlerini izler, Telegram'a uyarı gönderir.

Kullanım:
  export TELEGRAM_BOT_TOKEN=...
  export TELEGRAM_CHAT_ID=...
  # opsiyonel: ETHERSCAN_API_KEY (BSC için), HELIUS_API_KEY (Solana için)

  python telegram_cex_alert.py                 # sürekli izle
  python telegram_cex_alert.py --once          # tek tur (test)
  python telegram_cex_alert.py --dry-run       # Telegram gönderme, konsola yaz
  python telegram_cex_alert.py --build-watchlist  # ranked CEX JSON'dan liste güncelle

Not:
  - IN  = izlenen CEX cüzdanına giriş  → genelde deposit / alım sonrası transfer
  - OUT = izlenen CEX cüzdanından çıkış → genelde withdrawal / cold→hot hareketi
  - Spam airdrop'lar (decimals=0, absürt miktar) varsayılan olarak filtrelenir.
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

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "watched_wallets.json"
STATE_PATH = ROOT / "output" / "alert_state.json"
RANKED_PATH = ROOT / "output" / "cex_wallets_ranked.json"

@dataclass(frozen=True)
class Watched:
    address: str
    label: str
    venue: str
    chain: str
    role: str


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) and value.strip() else default


def load_watched(config: dict[str, Any]) -> list[Watched]:
    out: list[Watched] = []
    for row in config.get("wallets", []):
        if row.get("enabled") is False:
            continue
        addr = (row.get("address") or "").strip()
        chain = (row.get("chain") or "ethereum").strip().lower()
        if not addr:
            continue
        if chain in {"ethereum", "bsc", "eth"}:
            addr = addr.lower()
            if chain == "eth":
                chain = "ethereum"
        out.append(
            Watched(
                address=addr,
                label=str(row.get("label") or addr[:10]),
                venue=str(row.get("venue") or "Unknown"),
                chain=chain,
                role=str(row.get("role") or "unknown"),
            )
        )
    return out


def classify_side(watched_addr: str, frm: str, to: str) -> str:
    w = watched_addr.lower()
    if to.lower() == w:
        return "IN"
    if frm.lower() == w:
        return "OUT"
    return "OTHER"


def side_emoji(side: str) -> str:
    return {"IN": "🟢 DEPOSIT/IN", "OUT": "🔴 WITHDRAW/OUT"}.get(side, "⚪ MOVE")


def is_spam_transfer(tx: dict[str, Any], skip_spam: bool) -> bool:
    if not skip_spam:
        return False
    try:
        decimals = int(tx.get("tokenDecimal") or tx.get("decimals") or 18)
    except ValueError:
        decimals = 18
    try:
        raw = int(tx.get("value") or 0)
    except ValueError:
        raw = 0
    symbol = (tx.get("tokenSymbol") or tx.get("symbol") or "").upper()
    # Arkham spam pattern: decimals=0 + huge integer airdrop onto CEX wallets
    if decimals == 0 and raw >= 10**12:
        return True
    if decimals == 0 and raw > 0 and len(symbol) > 12:
        return True
    # empty / placeholder tokens
    if not symbol and raw == 0:
        return True
    return False


def format_amount(raw: str | int, decimals: int | str) -> str:
    try:
        d = int(decimals)
        value = int(raw) / (10**d if d >= 0 else 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return str(raw)
    if value >= 1_000_000_000:
        return f"{value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value/1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value/1_000:.2f}K"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.8f}"


def telegram_send(token: str, chat_id: str, text: str, dry_run: bool = False) -> bool:
    if dry_run:
        print("--- DRY-RUN TELEGRAM ---\n" + text + "\n------------------------")
        return True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            print(f"[telegram] HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return False
        return True
    except requests.RequestException as exc:
        print(f"[telegram] error: {exc}", file=sys.stderr)
        return False


def blockscout_tokentx(
    api_base: str,
    address: str,
    *,
    api_key: str | None = None,
    page: int = 1,
    offset: int = 40,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "module": "account",
        "action": "tokentx",
        "address": address,
        "page": page,
        "offset": offset,
        "sort": "desc",
    }
    if api_key:
        params["apikey"] = api_key
    # Etherscan V2 style base already contains query (?chainid=56)
    if "?" in api_base:
        sep = "&"
        url = api_base
    else:
        sep = "?"
        url = api_base
    # requests handles params; for bases with existing query, still fine
    try:
        r = requests.get(url if "?" not in api_base else api_base, params=params, timeout=40)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[evm] fetch fail {address[:10]}…: {exc}", file=sys.stderr)
        return []

    result = data.get("result")
    if data.get("status") == "0" and isinstance(result, str):
        # common: No transactions found / NOTOK
        if "No transaction" in result or result == "No transactions found":
            return []
        print(f"[evm] api msg {address[:10]}…: {result[:200]}", file=sys.stderr)
        return []
    if not isinstance(result, list):
        return []
    return result


def solana_signatures(rpc: str, address: str, limit: int = 20) -> list[dict[str, Any]]:
    helius = env("HELIUS_API_KEY")
    if helius:
        rpc = f"https://mainnet.helius-rpc.com/?api-key={helius}"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": limit}],
    }
    try:
        r = requests.post(rpc, json=payload, timeout=40)
        r.raise_for_status()
        data = r.json()
        return data.get("result") or []
    except (requests.RequestException, ValueError) as exc:
        print(f"[sol] fetch fail {address[:8]}…: {exc}", file=sys.stderr)
        return []


def alert_key(chain: str, tx_hash: str, watched: str, side: str, contract: str = "") -> str:
    return f"{chain}|{tx_hash}|{watched}|{side}|{contract}".lower()


def build_evm_message(
    watched: Watched,
    side: str,
    tx: dict[str, Any],
    explorer_tx: str,
) -> str:
    symbol = tx.get("tokenSymbol") or "?"
    decimals = tx.get("tokenDecimal") or 18
    amount = format_amount(tx.get("value") or 0, decimals)
    frm = tx.get("from") or "?"
    to = tx.get("to") or "?"
    hx = tx.get("hash") or "?"
    ts = tx.get("timeStamp")
    when = ""
    if ts:
        try:
            when = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except (TypeError, ValueError, OSError):
            when = str(ts)
    return (
        f"<b>{side_emoji(side)}</b>\n"
        f"<b>{watched.venue}</b> · {watched.label}\n"
        f"Role: <code>{watched.role}</code>\n"
        f"Chain: <code>{watched.chain}</code>\n"
        f"Token: <b>{symbol}</b> · {amount}\n"
        f"From: <code>{frm}</code>\n"
        f"To: <code>{to}</code>\n"
        f"Time: {when}\n"
        f"<a href=\"{explorer_tx}{hx}\">Tx</a>"
    )


def build_sol_message(watched: Watched, sig: dict[str, Any], explorer_tx: str) -> str:
    hx = sig.get("signature") or "?"
    err = sig.get("err")
    status = "FAIL" if err else "OK"
    slot = sig.get("slot")
    return (
        f"<b>🟣 SOLANA ACTIVITY</b>\n"
        f"<b>{watched.venue}</b> · {watched.label}\n"
        f"Role: <code>{watched.role}</code>\n"
        f"Status: {status} · slot {slot}\n"
        f"<a href=\"{explorer_tx}{hx}\">Tx</a>"
    )


def poll_once(
    config: dict[str, Any],
    watched: list[Watched],
    state: dict[str, Any],
    *,
    dry_run: bool,
    token: str | None,
    chat_id: str | None,
) -> int:
    settings = config.get("settings") or {}
    skip_spam = bool(settings.get("skip_spam", True))
    lookback = int(settings.get("lookback_on_start", 25))
    chains = settings.get("chains") or {}
    seen: set[str] = set(state.get("seen") or [])
    bootstrapped: set[str] = set(state.get("bootstrapped") or [])
    sent = 0
    etherscan_key = env("ETHERSCAN_API_KEY")

    by_chain: dict[str, list[Watched]] = {}
    for w in watched:
        by_chain.setdefault(w.chain, []).append(w)

    # --- Ethereum via Blockscout ---
    for chain_name in ("ethereum", "bsc"):
        wallets = by_chain.get(chain_name) or []
        if not wallets:
            continue
        chain_cfg = chains.get(chain_name) or {}
        api = chain_cfg.get("explorer_api")
        explorer_tx = chain_cfg.get("explorer_tx") or ""
        if not api:
            continue
        if chain_name == "bsc" and "etherscan.io/v2" in api and not etherscan_key:
            print("[bsc] ETHERSCAN_API_KEY yok — BSC cüzdanları atlandı", file=sys.stderr)
            continue

        for w in wallets:
            page_size = max(lookback, 50)
            txs = blockscout_tokentx(
                api,
                w.address,
                api_key=etherscan_key if chain_name == "bsc" else None,
                offset=page_size,
            )
            wallet_key = f"{chain_name}:{w.address}"
            first_pass = wallet_key not in bootstrapped
            # newest first from API; process oldest→newest for chronological alerts
            ordered = list(reversed(txs))

            for tx in ordered:
                frm = (tx.get("from") or "").lower()
                to = (tx.get("to") or "").lower()
                side = classify_side(w.address, frm, to)
                if side == "OTHER":
                    continue
                if is_spam_transfer(tx, skip_spam):
                    continue
                hx = tx.get("hash") or ""
                contract = (tx.get("contractAddress") or "").lower()
                key = alert_key(chain_name, hx, w.address, side, contract)
                if key in seen:
                    continue
                seen.add(key)
                # first pass: seed state, don't spam historical dumps
                if first_pass:
                    continue
                if not token or not chat_id:
                    if not dry_run:
                        print("[warn] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID eksik", file=sys.stderr)
                        continue
                msg = build_evm_message(w, side, tx, explorer_tx)
                if telegram_send(token or "", chat_id or "", msg, dry_run=dry_run):
                    sent += 1
                    print(f"[alert] {w.venue} {side} {tx.get('tokenSymbol')} {hx[:16]}…")
                time.sleep(0.15)

            bootstrapped.add(wallet_key)
            time.sleep(0.35)

    # --- Solana ---
    sol_wallets = by_chain.get("solana") or []
    if sol_wallets:
        sol_cfg = chains.get("solana") or {}
        rpc = sol_cfg.get("rpc") or "https://api.mainnet-beta.solana.com"
        explorer_tx = sol_cfg.get("explorer_tx") or "https://solscan.io/tx/"
        for w in sol_wallets:
            sigs = solana_signatures(rpc, w.address, limit=max(lookback, 40))
            wallet_key = f"solana:{w.address}"
            first_pass = wallet_key not in bootstrapped
            ordered = list(reversed(sigs))
            for sig in ordered:
                hx = sig.get("signature") or ""
                if not hx:
                    continue
                key = alert_key("solana", hx, w.address, "ACT", "")
                if key in seen:
                    continue
                seen.add(key)
                if first_pass:
                    continue
                msg = build_sol_message(w, sig, explorer_tx)
                if telegram_send(token or "", chat_id or "", msg, dry_run=dry_run or not (token and chat_id)):
                    sent += 1
                    print(f"[alert] {w.venue} SOL {hx[:16]}…")
                time.sleep(0.15)
            bootstrapped.add(wallet_key)
            time.sleep(0.4)

    # keep state bounded
    seen_list = list(seen)
    if len(seen_list) > 20000:
        seen_list = seen_list[-15000:]
    state["seen"] = seen_list
    state["bootstrapped"] = sorted(bootstrapped)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(STATE_PATH, state)
    return sent


def build_watchlist_from_ranked(config_path: Path) -> None:
    if not RANKED_PATH.exists():
        raise SystemExit(f"missing {RANKED_PATH}")
    ranked = load_json(RANKED_PATH)
    if not isinstance(ranked, list):
        raise SystemExit("ranked json must be a list")

    venues = {
        "binance": "Binance",
        "coinbase": "Coinbase",
        "okx": "OKX",
        "okex": "OKX",
        "paribu": "Paribu",
        "btcturk": "BtcTurk",
    }
    picked: list[dict[str, Any]] = []
    seen_addr: set[str] = set()
    for row in ranked:
        ex = (row.get("exchange") or "").lower()
        tag = (row.get("name_tag") or "")
        venue = None
        for key, name in venues.items():
            if key in ex or key in tag.lower():
                venue = name
                break
        if not venue:
            continue
        addr = (row.get("address") or "").lower()
        if addr in seen_addr:
            continue
        seen_addr.add(addr)
        picked.append(
            {
                "address": addr,
                "label": tag or venue,
                "venue": venue,
                "chain": "ethereum",
                "role": row.get("wallet_type") or "hot/deposit",
            }
        )
        # keep watchlist manageable
        if sum(1 for p in picked if p["venue"] == venue) >= 12:
            # allow other venues to continue
            pass

    # trim per venue
    by_venue: dict[str, list[dict[str, Any]]] = {}
    for p in picked:
        by_venue.setdefault(p["venue"], []).append(p)
    trimmed: list[dict[str, Any]] = []
    for venue, rows in by_venue.items():
        trimmed.extend(rows[:10])

    # preserve special non-ranked wallets from current config
    current = load_json(config_path) if config_path.exists() else {"settings": {}, "wallets": []}
    keep_venues = {"Binance Alpha", "PancakeSwap", "pump.fun", "Phantom", "BtcTurk"}
    extras = [
        w
        for w in current.get("wallets", [])
        if w.get("venue") in keep_venues or w.get("chain") in {"bsc", "solana"}
    ]
    # avoid dupes
    existing = { (w.get("address") or "").lower() for w in trimmed }
    for w in extras:
        a = (w.get("address") or "")
        key = a.lower() if w.get("chain") in {"ethereum", "bsc", "eth"} else a
        if key.lower() in existing or key in existing:
            continue
        trimmed.append(w)

    current["wallets"] = trimmed
    save_json(config_path, current)
    print(f"wrote {len(trimmed)} wallets → {config_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="CEX wallet Telegram alert bot")
    parser.add_argument("--once", action="store_true", help="Tek poll turu")
    parser.add_argument("--dry-run", action="store_true", help="Telegram gönderme")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--build-watchlist", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    if args.build_watchlist:
        build_watchlist_from_ranked(config_path)
        return 0

    if not config_path.exists():
        raise SystemExit(f"config yok: {config_path}")

    config = load_json(config_path)
    watched = load_watched(config)
    if not watched:
        raise SystemExit("izlenecek cüzdan yok")

    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    dry_run = bool(args.dry_run) or not (token and chat_id)
    if dry_run and not args.dry_run:
        print("[info] Telegram env yok → dry-run modunda çalışıyor", file=sys.stderr)

    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"seen": [], "bootstrapped": []}
    poll_seconds = int((config.get("settings") or {}).get("poll_seconds") or 45)

    print(f"watching {len(watched)} wallets | poll={poll_seconds}s | dry_run={dry_run}")
    while True:
        n = poll_once(config, watched, state, dry_run=dry_run, token=token, chat_id=chat_id)
        print(f"[{datetime.now(timezone.utc).isoformat()}] poll done, alerts={n}")
        if args.once:
            break
        time.sleep(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
