#!/usr/bin/env python3
"""
ÇOK ZİNCİR CÜZDAN KOPYA → Binance spot

Zincirler: Ethereum + Base + Solana
Sen cüzdan listesine adres eklersin → bot tarar.
Binance'te USDT çifti olan coin → bakiyeyi 10 eşit parçaya bölüp AL/SAT.

Kullanım:
  1) BINANCE_API_KEY / SECRET yaz
  2) WALLETS listesine cüzdan ekle (chain: ethereum|base|solana)
  3) pip install requests
  4) python multi_chain_binance_copy.py

Opsiyonel:
  HELIUS_API_KEY  → Solana RPC (önerilir)
  ETHERSCAN_API_KEY → ETH/Base (opsiyonel)

Not:
  - Zincir meme coin Binance'te yoksa SKIP
  - Aynı saniye değil (poll gecikmeli)
  - İlk açılışta geçmiş tx kopyalanmaz
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# =============================================================================
# KEYS
# =============================================================================
BINANCE_API_KEY = "BURAYA_API_KEY"
BINANCE_API_SECRET = "BURAYA_SECRET_KEY"
HELIUS_API_KEY = ""          # önerilir (Solana)
ETHERSCAN_API_KEY = ""       # opsiyonel

# =============================================================================
# CÜZDANLAR — sen ekle / çıkar
# chain: "ethereum" | "base" | "solana"
# =============================================================================
WALLETS: list[dict[str, Any]] = [
    # Örnek Solana (senin verdiğin)
    {
        "address": "HxjwdF326ZunmUwC1iXhfgL3ku78YsksN6n7Rfxzwr6b",
        "label": "sol-bot-1",
        "chain": "solana",
        "enabled": True,
    },
    # Örnek — Ethereum (adresini yaz, enabled True yap)
    {
        "address": "0xBURAYA_ETH_CUZDAN",
        "label": "eth-1",
        "chain": "ethereum",
        "enabled": False,
    },
    # Örnek — Base
    {
        "address": "0xBURAYA_BASE_CUZDAN",
        "label": "base-1",
        "chain": "base",
        "enabled": False,
    },
]

# =============================================================================
# AYAR
# =============================================================================
BALANCE_SLOTS = 10              # USDT / 10 eşit
COPY_PCT_OF_SLOT = 0.90
MIN_ORDER_USDT = 6.0
POLL_SECONDS = 25
WALLET_PAUSE_SEC = 2.0
DRY_RUN = False
LIVE = True
MAX_OPEN_BASES = 10             # aynı anda max kaç farklı coin

# EVM explorers
CHAINS_EVM: dict[str, dict[str, Any]] = {
    "ethereum": {
        "apis": [
            "https://eth.blockscout.com/api",
            "https://api.etherscan.io/v2/api?chainid=1",
        ],
        "tx": "https://etherscan.io/tx/",
    },
    "base": {
        "apis": [
            "https://base.blockscout.com/api",
            "https://api.etherscan.io/v2/api?chainid=8453",
        ],
        "tx": "https://basescan.org/tx/",
    },
}

STABLES = {
    "USDT", "USDC", "USD1", "DAI", "FDUSD", "BUSD", "TUSD", "USDE", "USDC.E", "WETH", "ETH",
    "WBTC", "BTC", "CBETH", "WSTETH", "STETH",
}
SKIP_BASES = set(STABLES) | {"MSOL", "WSOL", "WEETH"}

SOL_KNOWN: dict[str, str] = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACLegJs": "PYTH",
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL": "JTO",
}

# =============================================================================

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "output" / "multi_chain_copy_state.json"
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "multi-chain-binance-copy/1.0"})

_jup: dict[str, str] = {}
_bn_pairs: set[str] | None = None
_explorer_cool = 0.0
_last_exp = 0.0


def clean(s: str) -> str:
    s = (s or "").strip()
    for q in ('"', "'"):
        if len(s) >= 2 and s[0] == s[-1] == q:
            s = s[1:-1].strip()
    return "".join(s.split())


def pick(*vals: str) -> str:
    for v in vals:
        c = clean(v)
        if c:
            return c
    return ""


def api_keys() -> tuple[str, str]:
    k = pick(BINANCE_API_KEY, os.getenv("BINANCE_API_KEY") or "")
    s = pick(BINANCE_API_SECRET, os.getenv("BINANCE_API_SECRET") or "")
    if k in {"", "BURAYA_API_KEY"} or s in {"", "BURAYA_SECRET_KEY"}:
        raise SystemExit("BINANCE_API_KEY / SECRET doldur")
    return k, s


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {
        "seen": {},          # key -> True
        "paper_usdt": 500.0,
        "paper_pos": {},
        "bootstrapped": {},  # wallet -> True
    }


def save_state(st: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    seen = st.get("seen") or {}
    if len(seen) > 4000:
        # eskiyi bud
        items = list(seen.items())[-2500:]
        st["seen"] = dict(items)
    STATE_PATH.write_text(json.dumps(st, indent=2), encoding="utf-8")


def enabled_wallets() -> list[dict[str, Any]]:
    out = []
    for w in WALLETS:
        if not w.get("enabled", True):
            continue
        addr = clean(str(w.get("address") or ""))
        chain = str(w.get("chain") or "").lower()
        if not addr or "BURAYA" in addr.upper():
            continue
        if chain not in {"ethereum", "base", "solana"}:
            print(f"[skip] bilinmeyen chain: {chain}")
            continue
        out.append({**w, "address": addr, "chain": chain})
    return out


# ---- Binance ----

def bn_time() -> int:
    try:
        return int(HTTP.get("https://api.binance.com/api/v3/time", timeout=10).json()["serverTime"])
    except Exception:
        return int(time.time() * 1000)


def bn_signed(method: str, path: str, key: str, secret: str, params: dict | None = None) -> Any:
    p = dict(params or {})
    p["timestamp"] = bn_time()
    p["recvWindow"] = 60000
    q = urllib.parse.urlencode(p)
    sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": key}
    url = f"https://api.binance.com{path}"
    if method == "GET":
        r = HTTP.get(f"{url}?{q}&signature={sig}", headers=headers, timeout=30)
    else:
        r = HTTP.post(
            url,
            data=f"{q}&signature={sig}",
            headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Binance {r.status_code}: {r.text[:280]}")
    return r.json()


def load_binance_usdt_bases() -> set[str]:
    global _bn_pairs
    if _bn_pairs is not None:
        return _bn_pairs
    print("[binance] USDT spot çiftleri yükleniyor…")
    info = HTTP.get("https://api.binance.com/api/v3/exchangeInfo", timeout=60).json()
    bases: set[str] = set()
    for s in info.get("symbols") or []:
        if (
            s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
            and s.get("isSpotTradingAllowed", True)
        ):
            bases.add(str(s.get("baseAsset") or "").upper())
    _bn_pairs = bases
    print(f"  → {len(bases)} USDT pair")
    return bases


def bn_free_usdt(key: str, secret: str) -> float:
    acc = bn_signed("GET", "/api/v3/account", key, secret)
    for b in acc.get("balances") or []:
        if b.get("asset") == "USDT":
            return float(b.get("free") or 0)
    return 0.0


def bn_free_asset(key: str, secret: str, asset: str) -> float:
    acc = bn_signed("GET", "/api/v3/account", key, secret)
    for b in acc.get("balances") or []:
        if b.get("asset") == asset.upper():
            return float(b.get("free") or 0)
    return 0.0


def bn_price(base: str) -> float:
    t = HTTP.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": f"{base}USDT"},
        timeout=15,
    ).json()
    return float(t["price"])


def bn_buy(key: str, secret: str, base: str, usdt: float) -> dict:
    return bn_signed(
        "POST",
        "/api/v3/order",
        key,
        secret,
        {
            "symbol": f"{base}USDT",
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": f"{usdt:.2f}",
        },
    )


def bn_sell(key: str, secret: str, base: str, qty: float) -> dict:
    q = float(f"{qty:.8f}")
    return bn_signed(
        "POST",
        "/api/v3/order",
        key,
        secret,
        {
            "symbol": f"{base}USDT",
            "side": "SELL",
            "type": "MARKET",
            "quantity": f"{q:.8f}".rstrip("0").rstrip("."),
        },
    )


def slot_budget(free_usdt: float) -> float:
    return (free_usdt / max(1, BALANCE_SLOTS)) * COPY_PCT_OF_SLOT


# ---- EVM tokentx ----

def explorer_get(api: str, params: dict[str, Any]) -> Any:
    global _explorer_cool, _last_exp
    now = time.time()
    if now < _explorer_cool:
        time.sleep(_explorer_cool - now)
    gap = 1.2 - (time.time() - _last_exp)
    if gap > 0:
        time.sleep(gap)

    p = dict(params)
    ek = pick(ETHERSCAN_API_KEY, os.getenv("ETHERSCAN_API_KEY") or "")
    if "etherscan.io" in api and ek:
        p["apikey"] = ek

    _last_exp = time.time()
    r = HTTP.get(api, params=p, timeout=35)
    if r.status_code == 429:
        _explorer_cool = time.time() + 25
        raise RuntimeError("429")
    r.raise_for_status()
    data = r.json()
    # etherscan style
    if isinstance(data, dict) and str(data.get("status")) == "0":
        msg = str(data.get("result") or data.get("message") or "")
        if "rate" in msg.lower() or "Max rate" in msg:
            _explorer_cool = time.time() + 25
            raise RuntimeError("429 " + msg)
    if isinstance(data, dict) and "result" in data:
        return data["result"]
    return data


def fetch_evm_tokentx(chain: str, address: str) -> list[dict[str, Any]]:
    cfg = CHAINS_EVM.get(chain)
    if not cfg:
        return []
    last = None
    for api in cfg["apis"]:
        try:
            result = explorer_get(
                api,
                {
                    "module": "account",
                    "action": "tokentx",
                    "address": address,
                    "page": 1,
                    "offset": 30,
                    "sort": "desc",
                },
            )
            if isinstance(result, list):
                return result
            last = result
        except Exception as e:
            last = e
            print(f"  ! {chain} explorer {api[:36]}… {e}")
            continue
    if last:
        print(f"  ! {chain} tokentx boş/hata: {last}")
    return []


def normalize_evm_symbol(sym: str) -> str:
    s = (sym or "").upper().strip()
    # wrapped prefix temizle (WETH→ETH vb. — SKIP_BASES'te zaten)
    if s.startswith("W") and len(s) > 1 and s[1:] in {"ETH", "BTC", "BNB", "POL", "MATIC", "AVAX"}:
        return s[1:]
    return s


def resolve_bn_base(sym: str, bn_bases: set[str]) -> str | None:
    """Explorer/Jupiter sembolünü Binance USDT base'e çevir (1000PEPE vb.)."""
    s = (sym or "").upper().strip()
    if not s:
        return None
    if s in bn_bases:
        return s
    for cand in (f"1000{s}", f"10000{s}", f"1000000{s}"):
        if cand in bn_bases:
            return cand
    if s.startswith("1000") and s[4:] in bn_bases:
        return s[4:]
    return None


def evm_events(wallet: str, chain: str, rows: list[dict], seen: dict, boot: bool) -> list[dict]:
    """IN=BUY OUT=SELL sinyalleri."""
    w = wallet.lower()
    events: list[dict] = []
    for row in rows:
        tx = str(row.get("hash") or "")
        if not tx:
            continue
        key = f"{chain}:{tx}:{row.get('contractAddress')}:{row.get('from')}:{row.get('to')}"
        if key in seen:
            continue
        seen[key] = True
        if boot:
            continue
        fr = (row.get("from") or "").lower()
        to = (row.get("to") or "").lower()
        try:
            raw = float(row.get("value") or 0)
            dec = int(row.get("tokenDecimal") or 18)
            amt = raw / (10**dec)
        except Exception:
            continue
        if amt <= 0:
            continue
        if int(row.get("tokenDecimal") or 18) == 0:
            continue
        sym = normalize_evm_symbol(str(row.get("tokenSymbol") or ""))
        if not sym or sym in SKIP_BASES:
            continue
        if to == w:
            side = "BUY"
        elif fr == w:
            side = "SELL"
        else:
            continue
        events.append(
            {
                "side": side,
                "base": sym,
                "chain": chain,
                "wallet": wallet,
                "tx": tx,
                "amount": amt,
                "contract": (row.get("contractAddress") or "").lower(),
            }
        )
    return events


# ---- Solana ----

def sol_rpc_url() -> str:
    hk = pick(HELIUS_API_KEY, os.getenv("HELIUS_API_KEY") or "")
    if hk:
        return f"https://mainnet.helius-rpc.com/?api-key={hk}"
    return "https://api.mainnet-beta.solana.com"


def sol_rpc(method: str, params: list) -> Any:
    r = HTTP.post(
        sol_rpc_url(),
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=40,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")


def jup_sym(mint: str) -> str | None:
    if mint in SOL_KNOWN:
        return SOL_KNOWN[mint]
    global _jup
    if not _jup:
        try:
            rows = HTTP.get("https://tokens.jup.ag/tokens?tags=verified", timeout=40).json()
            for t in rows if isinstance(rows, list) else []:
                a, s = str(t.get("address") or ""), str(t.get("symbol") or "").upper().replace(" ", "")
                if a and s and len(s) <= 12:
                    _jup[a] = s
            print(f"[jup] {len(_jup)} token")
        except Exception as e:
            print(f"[jup] {e}")
    return _jup.get(mint)


def sol_parse_deltas(tx: dict, wallet: str) -> list[dict]:
    meta = tx.get("meta") or {}
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    pubs = []
    for k in keys:
        pubs.append(k if isinstance(k, str) else str((k or {}).get("pubkey") or ""))

    def mp(rows: list) -> dict[tuple[str, str], float]:
        out: dict[tuple[str, str], float] = {}
        for r in rows or []:
            owner, mint = str(r.get("owner") or ""), str(r.get("mint") or "")
            if owner != wallet or not mint:
                continue
            ui = (r.get("uiTokenAmount") or {}).get("uiAmount")
            try:
                out[(owner, mint)] = float(ui) if ui is not None else 0.0
            except Exception:
                out[(owner, mint)] = 0.0
        return out

    a, b = mp(meta.get("preTokenBalances") or []), mp(meta.get("postTokenBalances") or [])
    mints = {m for _, m in a} | {m for _, m in b}
    deltas = []
    for mint in mints:
        d = b.get((wallet, mint), 0.0) - a.get((wallet, mint), 0.0)
        if abs(d) > 1e-12:
            deltas.append({"mint": mint, "delta": d})
    try:
        i = pubs.index(wallet)
        dsol = ((meta.get("postBalances") or [0])[i] - (meta.get("preBalances") or [0])[i]) / 1e9
        if abs(dsol) >= 0.02:
            deltas.append({"mint": "So11111111111111111111111111111111111111112", "delta": dsol})
    except Exception:
        pass
    return deltas


def sol_events(wallet: str, seen: dict, boot: bool) -> list[dict]:
    events: list[dict] = []
    try:
        rows = sol_rpc("getSignaturesForAddress", [wallet, {"limit": 12}]) or []
    except Exception as e:
        print(f"  ! sol sigs: {e}")
        return events

    for row in reversed(rows):
        sig = row.get("signature")
        if not sig:
            continue
        key = f"solana:{sig}"
        if key in seen:
            continue
        seen[key] = True
        if boot or row.get("err"):
            continue
        try:
            tx = sol_rpc(
                "getTransaction",
                [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            )
        except Exception as e:
            print(f"  ! sol tx: {e}")
            continue
        if not tx:
            continue
        scored = []
        for d in sol_parse_deltas(tx, wallet):
            sym = jup_sym(d["mint"])
            if not sym or sym.upper() in SKIP_BASES:
                continue
            scored.append((abs(float(d["delta"])), d, sym.upper()))
        scored.sort(key=lambda x: -x[0])
        for _a, d, sym in scored[:2]:
            events.append(
                {
                    "side": "BUY" if d["delta"] > 0 else "SELL",
                    "base": sym,
                    "chain": "solana",
                    "wallet": wallet,
                    "tx": sig,
                    "amount": abs(float(d["delta"])),
                    "contract": d["mint"],
                }
            )
        time.sleep(0.25)
    return events


# ---- execute ----

def execute(
    ev: dict,
    key: str,
    secret: str,
    state: dict,
    open_bases: set[str],
    bn_bases: set[str],
) -> str:
    raw = str(ev["base"]).upper()
    side = ev["side"]
    tag = f"{ev['chain']}/{ev.get('label') or ev['wallet'][:8]}"

    if raw in SKIP_BASES:
        return f"SKIP {raw} stable/wrap"
    base = resolve_bn_base(raw, bn_bases)
    if not base:
        return f"SKIP {side} {raw} — Binance USDT yok ({tag})"
    if base in SKIP_BASES:
        return f"SKIP {base} stable/wrap"

    # paper / dry
    if DRY_RUN or not LIVE:
        free = float(state.get("paper_usdt") or 0)
        budget = slot_budget(free)
        pos = state.setdefault("paper_pos", {})
        if side == "BUY":
            if len([b for b, q in pos.items() if float(q) > 0]) >= MAX_OPEN_BASES and float(pos.get(base) or 0) <= 0:
                return f"PAPER BUY {base} — max {MAX_OPEN_BASES} coin"
            if budget < MIN_ORDER_USDT:
                return f"PAPER BUY {base} yetersiz"
            px = bn_price(base)
            qty = budget / px
            state["paper_usdt"] = free - budget
            pos[base] = float(pos.get(base) or 0) + qty
            open_bases.add(base)
            return f"PAPER BUY {base} ${budget:.2f} @ {px} ← {tag} tx…{str(ev['tx'])[-8:]}"
        have = float(pos.get(base) or 0)
        if have <= 0:
            return f"PAPER SELL {base} pozisyon yok"
        px = bn_price(base)
        state["paper_usdt"] = free + have * px
        pos[base] = 0.0
        open_bases.discard(base)
        return f"PAPER SELL {base} qty={have:.6g} ← {tag}"

    free = bn_free_usdt(key, secret)
    budget = slot_budget(free)

    if side == "BUY":
        # açık coin sayısı
        if base not in open_bases and len(open_bases) >= MAX_OPEN_BASES:
            return f"BUY {base} — zaten {MAX_OPEN_BASES} coin açık"
        if budget < MIN_ORDER_USDT:
            return f"BUY {base} slot ${budget:.2f} < min"
        o = bn_buy(key, secret, base, budget)
        open_bases.add(base)
        return f"LIVE BUY {base} ${budget:.2f} id={o.get('orderId')} ← {tag}"

    have = bn_free_asset(key, secret, base)
    if have <= 0:
        return f"SELL {base} — Binance bakiyesi yok"
    px = bn_price(base)
    qty = min(have, budget / px if px > 0 else have)
    o = bn_sell(key, secret, base, qty)
    if bn_free_asset(key, secret, base) <= 0:
        open_bases.discard(base)
    return f"LIVE SELL {base} qty≈{qty:.6g} id={o.get('orderId')} ← {tag}"


def refresh_open_bases(key: str, secret: str, bn_bases: set[str]) -> set[str]:
    if DRY_RUN or not LIVE:
        return set()
    acc = bn_signed("GET", "/api/v3/account", key, secret)
    open_b: set[str] = set()
    for b in acc.get("balances") or []:
        asset = str(b.get("asset") or "").upper()
        tot = float(b.get("free") or 0) + float(b.get("locked") or 0)
        if asset in bn_bases and asset not in SKIP_BASES and tot > 0:
            # küçük toz bakiyeleri sayma
            try:
                if tot * bn_price(asset) >= MIN_ORDER_USDT * 0.5:
                    open_b.add(asset)
            except Exception:
                pass
    return open_b


def main() -> int:
    print("=" * 64)
    print("Multi-chain wallet COPY → Binance (ETH + Base + Solana)")
    print(f"Slot: USDT/{BALANCE_SLOTS} | max coin={MAX_OPEN_BASES}")
    print(f"Mod : {'DRY/PAPER' if DRY_RUN or not LIVE else 'LIVE'}")
    print("=" * 64)

    wallets = enabled_wallets()
    if not wallets:
        raise SystemExit("WALLETS boş — en az 1 enabled cüzdan ekle")
    for w in wallets:
        print(f"  • {w['chain']:8} {w.get('label') or ''} {w['address'][:12]}…")

    key, secret = api_keys()
    bn_bases = load_binance_usdt_bases()
    state = load_state()
    seen: dict = state.setdefault("seen", {})
    boot: dict = state.setdefault("bootstrapped", {})

    if LIVE and not DRY_RUN:
        free = bn_free_usdt(key, secret)
        print(f"[binance] USDT≈${free:.2f} | slot≈${slot_budget(free):.2f}")
        open_bases = refresh_open_bases(key, secret, bn_bases)
        print(f"[binance] açık coin≈{len(open_bases)}: {', '.join(sorted(open_bases)[:12])}")
    else:
        open_bases = {b for b, q in (state.get("paper_pos") or {}).items() if float(q) > 0}

    # bootstrap: geçmişi işaretle
    print("[init] geçmiş tx işaretleniyor…")
    for w in wallets:
        wid = f"{w['chain']}:{w['address']}"
        if boot.get(wid):
            continue
        addr, chain = w["address"], w["chain"]
        if chain in CHAINS_EVM:
            rows = fetch_evm_tokentx(chain, addr)
            evm_events(addr, chain, rows, seen, boot=True)
        else:
            sol_events(addr, seen, boot=True)
        boot[wid] = True
        time.sleep(WALLET_PAUSE_SEC)
    state["bootstrapped"] = boot
    state["seen"] = seen
    save_state(state)
    print("  → hazır. Yeni işlemler kopyalanacak.\n")

    while True:
        try:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"--- {ts} UTC tarama ---")
            batch: list[dict] = []
            for w in wallets:
                addr, chain = w["address"], w["chain"]
                label = w.get("label") or ""
                print(f"[{chain}] {label or addr[:10]}…")
                if chain in CHAINS_EVM:
                    rows = fetch_evm_tokentx(chain, addr)
                    evs = evm_events(addr, chain, rows, seen, boot=False)
                else:
                    evs = sol_events(addr, seen, boot=False)
                for e in evs:
                    e["label"] = label
                batch.extend(evs)
                time.sleep(WALLET_PAUSE_SEC)

            if not batch:
                print("  (yeni sinyal yok)")
            for ev in batch:
                try:
                    msg = execute(ev, key, secret, state, open_bases, bn_bases)
                    print(f"  → {msg}")
                except Exception as e:
                    print(f"  ! HATA {ev.get('side')} {ev.get('base')}: {e}")
                time.sleep(0.5)

            state["seen"] = seen
            save_state(state)
        except KeyboardInterrupt:
            print("\nDurdu")
            save_state(state)
            return 0
        except Exception as e:
            print(f"[loop] {e}")
            time.sleep(8)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
