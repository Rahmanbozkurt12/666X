#!/usr/bin/env python3
"""
Phantom cüzdan key → Jupiter on-chain AL/SAT botu

Phantom'da "API key" YOK. Akış:
  1) Ayrı bir trading cüzdanı oluştur (ana seed'i verme)
  2) Phantom → Settings → Security → Export Private Key → base58 key
  3) Key'i env'e koy, botu açık bırak → Jupiter ile swap imzalar

Çalıştır:
  export SOLANA_PRIVATE_KEY='base58...'
  # önerilir: export HELIUS_API_KEY='...'
  pip install requests solders base58
  python phantom_jupiter_bot.py

DRY_RUN=True iken zincire göndermez (sadece quote/log).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# =============================================================================
# AYAR
# =============================================================================
# Öncelik: env SOLANA_PRIVATE_KEY  |  yoksa aşağıdaki (boş bırak)
SOLANA_PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY", "").strip()
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "").strip()
JUPITER_API_KEY = os.environ.get("JUPITER_API_KEY", "").strip()  # opsiyonel

DRY_RUN = True                  # True = imza/gönderim yok, sadece plan + quote
POLL_SEC = 25.0
SLIPPAGE_BPS = 100              # %1
MIN_SOL_RESERVE = 0.02          # gas için SOL bırak
BUY_SOL_AMOUNT = 0.05           # her AL için harcanacak SOL
TAKE_PROFIT_PCT = 18.0          # +%18 → SAT
STOP_LOSS_PCT = -10.0           # -%10 → SAT
MAX_BUYS_PER_TOKEN = 1          # pozisyon varken tekrar AL yok

# İşlenecek tokenlar (mint = Phantom'daki sözleşme adresi)
# Örnek: USDC — gerçek meme mint'ini kendin yaz
TOKENS: list[dict[str, Any]] = [
    {
        "symbol": "USDC",
        "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "decimals": 6,
        "buy_sol": BUY_SOL_AMOUNT,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
        "enabled": False,  # true yapmadan AL atmaz
    },
]

SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_BASE = "https://lite-api.jup.ag/swap/v1"
ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "output" / "phantom_jupiter_state.json"
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "phantom-jupiter-bot/1.0"})
if JUPITER_API_KEY:
    HTTP.headers["x-api-key"] = JUPITER_API_KEY


def rpc_url() -> str:
    if HELIUS_API_KEY:
        return f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    return os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_keypair() -> Keypair:
    raw = SOLANA_PRIVATE_KEY or os.environ.get("SOLANA_PRIVATE_KEY", "").strip()
    if not raw or raw.startswith("BURAYA"):
        raise SystemExit(
            "SOLANA_PRIVATE_KEY yok.\n"
            "Phantom → Settings → Security & Privacy → Export Private Key\n"
            "export SOLANA_PRIVATE_KEY='...'  (ayrı trading cüzdanı kullan)"
        )
    # base58 secret (Phantom export) veya json byte array
    if raw.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(raw)))
    import base58

    return Keypair.from_bytes(base58.b58decode(raw))


def rpc(method: str, params: list[Any]) -> Any:
    r = HTTP.post(
        rpc_url(),
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=45,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError(body["error"])
    return body.get("result")


def sol_balance(pubkey: str) -> float:
    lamports = int(rpc("getBalance", [pubkey])["value"])
    return lamports / 1e9


def token_balance(owner: str, mint: str) -> tuple[float, int]:
    """(ui_amount, raw_amount)"""
    res = rpc(
        "getTokenAccountsByOwner",
        [
            owner,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ],
    )
    total_raw = 0
    decimals = 0
    for acc in res.get("value") or []:
        info = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
        total_raw += int(info["amount"])
        decimals = int(info["decimals"])
    if total_raw <= 0:
        return 0.0, 0
    return total_raw / (10**decimals), total_raw


def jup_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
    r = HTTP.get(
        f"{JUP_BASE}/quote",
        params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data or "outAmount" not in data:
        raise RuntimeError(f"quote hata: {data}")
    return data


def jup_swap_tx(user: str, quote: dict) -> str:
    r = HTTP.post(
        f"{JUP_BASE}/swap",
        json={
            "userPublicKey": user,
            "quoteResponse": quote,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        },
        timeout=45,
    )
    r.raise_for_status()
    data = r.json()
    tx = data.get("swapTransaction")
    if not tx:
        raise RuntimeError(f"swap tx yok: {data}")
    return tx


def send_swap(kp: Keypair, quote: dict) -> str:
    raw_b64 = jup_swap_tx(str(kp.pubkey()), quote)
    raw = base64.b64decode(raw_b64)
    vtx = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(vtx.message, [kp])
    sig = rpc(
        "sendTransaction",
        [
            base64.b64encode(bytes(signed)).decode(),
            {
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "encoding": "base64",
                "maxRetries": 3,
            },
        ],
    )
    return str(sig)


@dataclass
class Pos:
    mint: str
    symbol: str
    cost_sol: float = 0.0
    qty: float = 0.0
    buys: int = 0
    last_side: str = ""


@dataclass
class State:
    positions: dict[str, Pos] = field(default_factory=dict)

    def save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            k: {
                "mint": p.mint,
                "symbol": p.symbol,
                "cost_sol": p.cost_sol,
                "qty": p.qty,
                "buys": p.buys,
                "last_side": p.last_side,
            }
            for k, p in self.positions.items()
        }
        STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "State":
        st = cls()
        if not STATE_PATH.exists():
            return st
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            for k, v in raw.items():
                st.positions[k] = Pos(
                    mint=v["mint"],
                    symbol=v.get("symbol") or k,
                    cost_sol=float(v.get("cost_sol") or 0),
                    qty=float(v.get("qty") or 0),
                    buys=int(v.get("buys") or 0),
                    last_side=str(v.get("last_side") or ""),
                )
        except Exception:
            pass
        return st


def pnl_pct(pos: Pos, token_ui: float, out_sol_lamports: int) -> Optional[float]:
    if pos.cost_sol <= 0 or token_ui <= 0:
        return None
    out_sol = out_sol_lamports / 1e9
    return (out_sol / pos.cost_sol - 1.0) * 100.0


def do_buy(kp: Keypair, st: State, cfg: dict) -> None:
    mint = cfg["mint"]
    sym = cfg["symbol"]
    buy_sol = float(cfg.get("buy_sol") or BUY_SOL_AMOUNT)
    pub = str(kp.pubkey())
    sol = sol_balance(pub)
    if sol < buy_sol + MIN_SOL_RESERVE:
        log(f"{sym} AL atlandı — SOL yetersiz ({sol:.4f})")
        return
    lamports = int(buy_sol * 1e9)
    quote = jup_quote(SOL_MINT, mint, lamports, SLIPPAGE_BPS)
    out_amt = int(quote["outAmount"])
    log(f"{sym} AL quote | {buy_sol:.4f} SOL → raw_out={out_amt} | impact={quote.get('priceImpactPct')}")
    if DRY_RUN:
        log(f"{sym} DRY_RUN — zincire gönderilmedi (paper pos)")
        pos = st.positions.get(mint) or Pos(mint=mint, symbol=sym)
        pos.cost_sol += buy_sol
        pos.qty = max(pos.qty, out_amt / (10 ** int(cfg.get("decimals") or 6)))
        pos.buys += 1
        pos.last_side = "buy"
        st.positions[mint] = pos
        st.save()
        return
    sig = send_swap(kp, quote)
    log(f"{sym} AL OK https://solscan.io/tx/{sig}")
    ui, _ = token_balance(pub, mint)
    pos = st.positions.get(mint) or Pos(mint=mint, symbol=sym)
    pos.cost_sol += buy_sol
    pos.qty = ui
    pos.buys += 1
    pos.last_side = "buy"
    st.positions[mint] = pos
    st.save()


def do_sell(kp: Keypair, st: State, cfg: dict, reason: str) -> None:
    mint = cfg["mint"]
    sym = cfg["symbol"]
    pub = str(kp.pubkey())
    ui, raw = token_balance(pub, mint)
    pos = st.positions.get(mint)
    if raw <= 0 and DRY_RUN and pos and pos.qty > 0:
        # paper sat
        dec = int(cfg.get("decimals") or 6)
        raw = int(pos.qty * (10**dec))
        ui = pos.qty
    if raw <= 0:
        log(f"{sym} SAT atlandı — bakiye 0")
        st.positions.pop(mint, None)
        st.save()
        return
    quote = jup_quote(mint, SOL_MINT, raw, SLIPPAGE_BPS)
    out_sol = int(quote["outAmount"]) / 1e9
    log(f"{sym} SAT ({reason}) quote | {ui:.6g} → {out_sol:.6f} SOL")
    if DRY_RUN:
        log(f"{sym} DRY_RUN — zincire gönderilmedi")
        st.positions.pop(mint, None)
        st.save()
        return
    sig = send_swap(kp, quote)
    log(f"{sym} SAT OK https://solscan.io/tx/{sig}")
    st.positions.pop(mint, None)
    st.save()


def tick(kp: Keypair, st: State) -> None:
    pub = str(kp.pubkey())
    sol = sol_balance(pub)
    log(f"cüzdan={pub[:4]}…{pub[-4:]} SOL={sol:.4f} dry={DRY_RUN}")

    for cfg in TOKENS:
        if not cfg.get("enabled", True):
            continue
        mint = cfg["mint"]
        sym = cfg["symbol"]
        ui, raw = token_balance(pub, mint)
        pos = st.positions.get(mint)
        paper_qty = pos.qty if (DRY_RUN and pos) else 0.0

        if raw > 0 or paper_qty > 0:
            # pozisyon var → TP/SL
            if raw > 0:
                quote = jup_quote(mint, SOL_MINT, raw, SLIPPAGE_BPS)
                out_lamports = int(quote["outAmount"])
                live_ui = ui
            else:
                dec = int(cfg.get("decimals") or 6)
                paper_raw = max(1, int(paper_qty * (10**dec)))
                quote = jup_quote(mint, SOL_MINT, paper_raw, SLIPPAGE_BPS)
                out_lamports = int(quote["outAmount"])
                live_ui = paper_qty
            if pos is None:
                pos = Pos(mint=mint, symbol=sym, cost_sol=out_lamports / 1e9, qty=live_ui, buys=1)
                st.positions[mint] = pos
                st.save()
                log(f"{sym} mevcut bakiye işaretlendi (cost≈quote)")
            pct = pnl_pct(pos, live_ui, out_lamports)
            tp = float(cfg.get("take_profit_pct", TAKE_PROFIT_PCT))
            sl = float(cfg.get("stop_loss_pct", STOP_LOSS_PCT))
            log(f"{sym} pos qty={live_ui:.6g} pnl≈{pct if pct is not None else '?'}% (tp={tp} sl={sl})")
            if pct is not None and (pct >= tp or pct <= sl):
                reason = "TP" if pct >= tp else "SL"
                do_sell(kp, st, cfg, reason)
            continue

        # pozisyon yok → AL
        buys = (pos.buys if pos else 0)
        if buys >= int(cfg.get("max_buys", MAX_BUYS_PER_TOKEN)):
            continue
        do_buy(kp, st, cfg)


def main() -> None:
    kp = load_keypair()
    st = State.load()
    log("=" * 56)
    log("Phantom key → Jupiter AL/SAT bot")
    log(f"pubkey={kp.pubkey()}")
    log(f"rpc={rpc_url().split('?')[0]}")
    log(f"DRY_RUN={DRY_RUN} | poll={POLL_SEC}s | slippage={SLIPPAGE_BPS}bps")
    enabled = [t["symbol"] for t in TOKENS if t.get("enabled", True)]
    log(f"aktif tokenlar: {enabled or '(hiç — TOKENS[].enabled=True yap)'}")
    log("=" * 56)
    if DRY_RUN:
        log("UYARI: DRY_RUN=True — gerçek swap yok. Canlı için False yap.")

    while True:
        try:
            tick(kp, st)
        except KeyboardInterrupt:
            log("çıkış")
            return
        except Exception as e:
            log(f"hata: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
