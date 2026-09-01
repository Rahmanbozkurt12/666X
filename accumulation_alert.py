#!/usr/bin/env python3
"""
Etiketli cüzdan listesinde token birikimini izler.

Örnek senaryo (FET):
  1000 cüzdan taranır → 100+ cüzdanda FET bakiyesi eşiği aşılıp artış varsa
  Telegram uyarısı: "kademeli birikim başladı"

Kullanım:
  python labeled_wallet_fetcher.py --name-tag Binance --max 1000
  python accumulation_alert.py --once --dry-run
  python accumulation_alert.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "accumulation_watch.json"
STATE_PATH = ROOT / "output" / "accumulation_state.json"
BALANCE_OF = "0x70a08231"


def load_json(path: Path) -> Any:
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


def load_wallets(path: Path, max_wallets: int) -> list[str]:
    data = load_json(path)
    rows = data.get("wallets") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"wallet file invalid: {path}")
    addrs: list[str] = []
    seen: set[str] = set()
    for row in rows:
        addr = (row.get("address") if isinstance(row, dict) else str(row)).strip().lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        addrs.append(addr)
        if max_wallets and len(addrs) >= max_wallets:
            break
    return addrs


def erc20_balance(rpc: str, token: str, wallet: str) -> int:
    data = BALANCE_OF + wallet[2:].lower().zfill(64)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": token, "data": data}, "latest"],
    }
    try:
        r = requests.post(rpc, json=payload, timeout=20)
        r.raise_for_status()
        result = r.json().get("result")
        if not result or result == "0x":
            return 0
        return int(result, 16)
    except (requests.RequestException, ValueError, TypeError):
        return -1


def format_tokens(raw: int, decimals: int) -> str:
    if raw < 0:
        return "?"
    value = raw / (10**decimals)
    if value >= 1_000_000:
        return f"{value/1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value/1_000:.2f}K"
    return f"{value:.4f}"


def poll_token(
    *,
    token_cfg: dict[str, Any],
    wallets: list[str],
    state: dict[str, Any],
    settings: dict[str, Any],
    dry_run: bool,
    tg_token: str | None,
    tg_chat: str | None,
) -> int:
    symbol = token_cfg.get("symbol") or "TOKEN"
    contract = (token_cfg.get("contract") or "").lower()
    decimals = int(token_cfg.get("decimals") or 18)
    rpc = settings.get("rpc_url") or "https://ethereum-rpc.publicnode.com"

    min_balance_raw = int(float(settings.get("min_token_balance") or 100) * (10**decimals))
    min_increase_raw = int(float(settings.get("min_balance_increase") or 10) * (10**decimals))
    min_accumulators = int(settings.get("min_accumulators_for_alert") or 100)
    cooldown_min = int(settings.get("alert_cooldown_minutes") or 60)

    token_state = state.setdefault("tokens", {}).setdefault(symbol, {})
    wallet_states: dict[str, Any] = token_state.setdefault("wallets", {})
    last_alert_at = token_state.get("last_alert_at")

    accumulators: list[dict[str, Any]] = []
    new_accumulators: list[dict[str, Any]] = []
    scanned = 0
    errors = 0

    for wallet in wallets:
        raw = erc20_balance(rpc, contract, wallet)
        scanned += 1
        if raw < 0:
            errors += 1
            time.sleep(0.05)
            continue

        prev = wallet_states.get(wallet, {})
        prev_raw = int(prev.get("balance_raw") or 0)
        delta = raw - prev_raw if prev_raw >= 0 else 0

        wallet_states[wallet] = {
            "balance_raw": raw,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        if raw >= min_balance_raw:
            item = {
                "address": wallet,
                "balance_raw": raw,
                "delta_raw": max(delta, 0),
            }
            accumulators.append(item)
            if delta >= min_increase_raw:
                new_accumulators.append(item)

        if scanned % 50 == 0:
            print(f"[{symbol}] scanned {scanned}/{len(wallets)} accum={len(accumulators)}")
        time.sleep(0.03)

    total_above = len(accumulators)
    new_count = len(new_accumulators)
    token_state["last_scan"] = {
        "scanned": scanned,
        "errors": errors,
        "accumulators": total_above,
        "new_accumulators": new_count,
        "at": datetime.now(timezone.utc).isoformat(),
    }

    should_alert = total_above >= min_accumulators
    if last_alert_at:
        try:
            last_dt = datetime.fromisoformat(last_alert_at)
            if datetime.now(timezone.utc) - last_dt < timedelta(minutes=cooldown_min):
                should_alert = False
        except ValueError:
            pass

    sent = 0
    if should_alert:
        top_new = sorted(new_accumulators, key=lambda x: x["delta_raw"], reverse=True)[:5]
        lines = [
            f"<b>📈 BİRİKİM SİNYALİ — {symbol}</b>",
            f"Eşik üstü cüzdan: <b>{total_above}</b> / {len(wallets)}",
            f"Bu turda artış gösteren: <b>{new_count}</b>",
            f"Min bakiye: {settings.get('min_token_balance')} {symbol}",
            f"Min artış: {settings.get('min_balance_increase')} {symbol}",
            "",
            "<b>En çok artan 5 cüzdan:</b>",
        ]
        if top_new:
            for row in top_new:
                lines.append(
                    f"• <code>{row['address']}</code> "
                    f"+{format_tokens(row['delta_raw'], decimals)} "
                    f"(toplam {format_tokens(row['balance_raw'], decimals)})"
                )
        else:
            lines.append("• Bu turda belirgin artış yok (eşik üstü sayı yüksek)")

        lines.append(f'\n<a href="https://etherscan.io/token/{contract}">Etherscan {symbol}</a>')
        msg = "\n".join(lines)
        if telegram_send(tg_token or "", tg_chat or "", msg, dry_run=dry_run or not (tg_token and tg_chat)):
            sent += 1
            token_state["last_alert_at"] = datetime.now(timezone.utc).isoformat()
            print(f"[alert] {symbol} accumulators={total_above} new={new_count}")

    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="Token birikim alert botu")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"config yok: {config_path}")

    config = load_json(config_path)
    settings = config.get("settings") or {}
    wallet_file = Path(settings.get("wallet_file") or ROOT / "output" / "labeled_wallets.json")
    if not wallet_file.is_absolute():
        wallet_file = ROOT / wallet_file

    if not wallet_file.exists():
        raise SystemExit(
            f"cüzdan dosyası yok: {wallet_file}\n"
            "önce: python active_wallet_discovery.py\n"
            "veya: python labeled_wallet_fetcher.py --name-tag Binance --max 1000"
        )

    max_wallets = int(settings.get("max_wallets_to_scan") or 1000)
    wallets = load_wallets(wallet_file, max_wallets)
    if not wallets:
        raise SystemExit("izlenecek cüzdan yok")

    tokens = [t for t in (config.get("tokens") or []) if t.get("enabled", True)]
    if not tokens:
        raise SystemExit("token listesi boş")

    tg_token = env("TELEGRAM_BOT_TOKEN")
    tg_chat = env("TELEGRAM_CHAT_ID")
    dry_run = bool(args.dry_run) or not (tg_token and tg_chat)
    if dry_run and not args.dry_run:
        print("[info] Telegram env yok → dry-run", file=sys.stderr)

    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"tokens": {}}
    poll_seconds = int(settings.get("poll_seconds") or 300)

    print(f"accumulation watch | wallets={len(wallets)} | tokens={[t.get('symbol') for t in tokens]} | poll={poll_seconds}s")
    while True:
        sent = 0
        for token_cfg in tokens:
            sent += poll_token(
                token_cfg=token_cfg,
                wallets=wallets,
                state=state,
                settings=settings,
                dry_run=dry_run,
                tg_token=tg_token,
                tg_chat=tg_chat,
            )
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_json(STATE_PATH, state)
        print(f"[{datetime.now(timezone.utc).isoformat()}] poll done alerts={sent}")
        if args.once:
            break
        time.sleep(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
