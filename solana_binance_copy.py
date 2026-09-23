#!/usr/bin/env python3
"""
ÖRNEK — Solana cüzdan → Binance spot kopya (bakiye bölünür)

Ne yapar:
  1) Solana cüzdanının yeni işlemlerini izler
  2) Token artışı = AL, azalış = SAT (sadece Binance'te USDT çifti varsa)
  3) Serbest USDT'yi N parçaya böler → her kopya emri max 1 slot

Çalıştır:
  pip install requests
  # KEY yaz →
  python solana_binance_copy.py

Not:
  • Solana meme coin'lerin çoğu Binance'te YOK → atlanır (logda SKIP)
  • Aynı saniye kopya değil (~poll aralığı gecikmeli)
  • ÖRNEK / eğitim — risk sende
"""

from __future__ import annotations

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

# =============================================================================
# KEYS
# =============================================================================
BINANCE_API_KEY = "BURAYA_API_KEY"
BINANCE_API_SECRET = "BURAYA_SECRET_KEY"

# Opsiyonel — Helius ücretsiz key: https://helius.dev (RPC ban daha az)
HELIUS_API_KEY = ""  # örn: "abc123"

# =============================================================================
# İZLENEN CÜZDAN + AYAR
# =============================================================================
SOLANA_WALLET = "HxjwdF326ZunmUwC1iXhfgL3ku78YsksN6n7Rfxzwr6b"

BALANCE_SLOTS = 8                 # USDT / 8 = max tek emir
COPY_PCT_OF_SLOT = 0.90           # slot'un %90'ı
MIN_ORDER_USDT = 6.0
POLL_SECONDS = 20
MAX_SIGS_PER_POLL = 15
DRY_RUN = False                   # True = emir atmaz, sadece log
LIVE = True                       # False = paper

# Bilinen mint → Binance base (genişletilebilir)
KNOWN_MINTS: dict[str, str] = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",  # skip trade (stable)
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",  # skip
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "ETH",   # wormhole ETH bazen
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": "BTC",   # wormhole BTC
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "MSOL",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACLegJs": "PYTH",
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL": "JTO",
}

STABLES = {"USDC", "USDT", "USD1", "DAI", "FDUSD", "BUSD", "TUSD", "USDE"}
SKIP_BASES = set(STABLES) | {"MSOL", "WSOL"}

# =============================================================================

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "output" / "solana_copy_state.json"
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "solana-binance-copy/1.0"})


def clean(s: str) -> str:
    s = (s or "").strip()
    for q in ('"', "'"):
        if len(s) >= 2 and s[0] == s[-1] == q:
            s = s[1:-1].strip()
    return "".join(s.split())


def keys() -> tuple[str, str]:
    k = clean(BINANCE_API_KEY) or clean(os.getenv("BINANCE_API_KEY") or "")
    s = clean(BINANCE_API_SECRET) or clean(os.getenv("BINANCE_API_SECRET") or "")
    bad = {"", "BURAYA_API_KEY", "BURAYA_SECRET_KEY"}
    if k in bad or s in bad:
        raise SystemExit("BINANCE_API_KEY / SECRET doldur")
    return k, s


def rpc_url() -> str:
    hk = clean(HELIUS_API_KEY) or clean(os.getenv("HELIUS_API_KEY") or "")
    if hk:
        return f"https://mainnet.helius-rpc.com/?api-key={hk}"
    return "https://api.mainnet-beta.solana.com"


def rpc(method: str, params: list[Any]) -> Any:
    r = HTTP.post(
        rpc_url(),
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=40,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"seen_sigs": [], "paper_usdt": 100.0, "paper_pos": {}}


def save_state(st: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # son 500 sig tut
    seen = st.get("seen_sigs") or []
    st["seen_sigs"] = seen[-500:]
    STATE_PATH.write_text(json.dumps(st, indent=2), encoding="utf-8")


# ---- Jupiter token list cache (mint → symbol) ----
_JUP: dict[str, str] = {}


def jup_symbol(mint: str) -> str | None:
    if mint in KNOWN_MINTS:
        return KNOWN_MINTS[mint]
    global _JUP
    if not _JUP:
        try:
            # küçük / sık kullanılan liste
            rows = HTTP.get("https://tokens.jup.ag/tokens?tags=verified", timeout=30).json()
            if isinstance(rows, list):
                for t in rows:
                    addr = str(t.get("address") or "")
                    sym = str(t.get("symbol") or "").upper().replace(" ", "")
                    if addr and sym and len(sym) <= 12:
                        _JUP[addr] = sym
                print(f"[jup] {len(_JUP)} verified token yüklendi")
        except Exception as e:
            print(f"[jup] yüklenemedi: {e}")
    return _JUP.get(mint)


# ---- Binance ----

def bn_time(base: str = "https://api.binance.com") -> int:
    try:
        return int(HTTP.get(f"{base}/api/v3/time", timeout=10).json()["serverTime"])
    except Exception:
        return int(time.time() * 1000)


def bn_signed(
    method: str,
    path: str,
    api_key: str,
    api_secret: str,
    params: dict[str, Any] | None = None,
) -> Any:
    base = "https://api.binance.com"
    p = dict(params or {})
    p["timestamp"] = bn_time(base)
    p["recvWindow"] = 60000
    q = urllib.parse.urlencode(p)
    sig = hmac.new(api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": api_key}
    url = f"{base}{path}"
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
        raise RuntimeError(f"Binance {r.status_code}: {r.text[:300]}")
    return r.json()


def bn_free_usdt(api_key: str, api_secret: str) -> float:
    acc = bn_signed("GET", "/api/v3/account", api_key, api_secret)
    for b in acc.get("balances") or []:
        if b.get("asset") == "USDT":
            return float(b.get("free") or 0)
    return 0.0


def bn_has_pair(base: str) -> bool:
    sym = f"{base}USDT"
    try:
        r = HTTP.get(
            "https://api.binance.com/api/v3/exchangeInfo",
            params={"symbol": sym},
            timeout=15,
        )
        if r.status_code != 200:
            return False
        info = r.json()
        for s in info.get("symbols") or []:
            if s.get("symbol") == sym and s.get("status") == "TRADING":
                return True
    except Exception:
        return False
    return False


def bn_price(base: str) -> float:
    t = HTTP.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": f"{base}USDT"},
        timeout=15,
    ).json()
    return float(t["price"])


def bn_buy_quote(api_key: str, api_secret: str, base: str, usdt: float) -> dict[str, Any]:
    return bn_signed(
        "POST",
        "/api/v3/order",
        api_key,
        api_secret,
        {
            "symbol": f"{base}USDT",
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": f"{usdt:.2f}",
        },
    )


def bn_sell_qty(api_key: str, api_secret: str, base: str, qty: float) -> dict[str, Any]:
    # basit precision
    q = float(f"{qty:.6f}")
    if q <= 0:
        raise RuntimeError("qty=0")
    return bn_signed(
        "POST",
        "/api/v3/order",
        api_key,
        api_secret,
        {
            "symbol": f"{base}USDT",
            "side": "SELL",
            "type": "MARKET",
            "quantity": f"{q:.6f}".rstrip("0").rstrip(".") if q >= 1 else f"{q:.6f}",
        },
    )


def bn_free_asset(api_key: str, api_secret: str, asset: str) -> float:
    acc = bn_signed("GET", "/api/v3/account", api_key, api_secret)
    for b in acc.get("balances") or []:
        if b.get("asset") == asset.upper():
            return float(b.get("free") or 0)
    return 0.0


# ---- Solana parse ----

def fetch_new_sigs(wallet: str, seen: set[str]) -> list[str]:
    rows = rpc(
        "getSignaturesForAddress",
        [wallet, {"limit": MAX_SIGS_PER_POLL}],
    ) or []
    out: list[str] = []
    for row in rows:
        sig = row.get("signature")
        if not sig or sig in seen:
            continue
        if row.get("err"):
            seen.add(sig)
            continue
        out.append(sig)
    return list(reversed(out))  # eski → yeni


def parse_token_deltas(tx: dict[str, Any], wallet: str) -> list[dict[str, Any]]:
    """pre/post token balances → wallet için mint delta."""
    meta = tx.get("meta") or {}
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    # accountKeys bazen string, bazen {pubkey:..}
    pubs: list[str] = []
    for k in keys:
        if isinstance(k, str):
            pubs.append(k)
        elif isinstance(k, dict):
            pubs.append(str(k.get("pubkey") or ""))

    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []

    def idx_map(rows: list) -> dict[tuple[str, str], float]:
        m: dict[tuple[str, str], float] = {}
        for r in rows:
            owner = str(r.get("owner") or "")
            mint = str(r.get("mint") or "")
            ui = ((r.get("uiTokenAmount") or {}).get("uiAmount"))
            if owner != wallet or not mint:
                continue
            try:
                amt = float(ui) if ui is not None else 0.0
            except (TypeError, ValueError):
                amt = 0.0
            m[(owner, mint)] = amt
        return m

    a = idx_map(pre)
    b = idx_map(post)
    mints = set([m for _, m in a.keys()] + [m for _, m in b.keys()])
    deltas: list[dict[str, Any]] = []
    for mint in mints:
        before = a.get((wallet, mint), 0.0)
        after = b.get((wallet, mint), 0.0)
        d = after - before
        if abs(d) < 1e-12:
            continue
        deltas.append({"mint": mint, "delta": d, "before": before, "after": after})

    # native SOL delta (lamports)
    try:
        idx = pubs.index(wallet)
        pre_sol = (meta.get("preBalances") or [0])[idx] / 1e9
        post_sol = (meta.get("postBalances") or [0])[idx] / 1e9
        dsol = post_sol - pre_sol
        # fee yüzünden küçük değişimleri yok say
        if abs(dsol) >= 0.01:
            deltas.append(
                {
                    "mint": "So11111111111111111111111111111111111111112",
                    "delta": dsol,
                    "before": pre_sol,
                    "after": post_sol,
                }
            )
    except Exception:
        pass
    return deltas


def slot_usdt(free: float) -> float:
    return (free / max(1, BALANCE_SLOTS)) * COPY_PCT_OF_SLOT


def execute_copy(
    *,
    side: str,
    base: str,
    api_key: str,
    api_secret: str,
    state: dict[str, Any],
    sig: str,
) -> str:
    if base in SKIP_BASES:
        return f"SKIP stable/wrap {base}"
    if not bn_has_pair(base):
        return f"SKIP Binance'te yok: {base}USDT"

    if DRY_RUN or not LIVE:
        px = bn_price(base)
        free = float(state.get("paper_usdt") or 100)
        budget = slot_usdt(free)
        if side == "BUY":
            if budget < MIN_ORDER_USDT:
                return f"PAPER BUY yetersiz USDT {free:.2f}"
            qty = budget / px
            state["paper_usdt"] = free - budget
            pos = state.setdefault("paper_pos", {})
            pos[base] = float(pos.get(base) or 0) + qty
            return f"PAPER BUY {base} ~${budget:.2f} @ {px} (sig…{sig[-8:]})"
        # SELL
        pos = state.setdefault("paper_pos", {})
        have = float(pos.get(base) or 0)
        if have <= 0:
            return f"PAPER SELL {base} pozisyon yok"
        sell_q = have  # tüm kopya pozisyon
        proceeds = sell_q * px
        pos[base] = 0.0
        state["paper_usdt"] = free + proceeds
        return f"PAPER SELL {base} qty={sell_q:.6g} ~${proceeds:.2f}"

    free = bn_free_usdt(api_key, api_secret)
    budget = slot_usdt(free)
    if side == "BUY":
        if budget < MIN_ORDER_USDT:
            return f"BUY atlandı — slot ${budget:.2f} < min"
        order = bn_buy_quote(api_key, api_secret, base, budget)
        return f"LIVE BUY {base} ${budget:.2f} → {order.get('status')} id={order.get('orderId')}"

    have = bn_free_asset(api_key, api_secret, base)
    if have <= 0:
        return f"SELL {base} — Binance'te bakiye yok (önce kopya AL gerekir)"
    # slot kadar sat (hepsini dump etme)
    px = bn_price(base)
    max_q = budget / px if px > 0 else have
    qty = min(have, max_q)
    order = bn_sell_qty(api_key, api_secret, base, qty)
    return f"LIVE SELL {base} qty≈{qty:.6g} → {order.get('status')} id={order.get('orderId')}"


def process_sig(sig: str, wallet: str, api_key: str, api_secret: str, state: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
    if not tx:
        notes.append(f"tx yok {sig[-8:]}")
        return notes
    deltas = parse_token_deltas(tx, wallet)
    if not deltas:
        notes.append(f"delta yok …{sig[-8:]}")
        return notes

    # stable dışındaki en büyük |delta| öncelikli
    scored: list[tuple[float, dict[str, Any], str]] = []
    for d in deltas:
        sym = jup_symbol(d["mint"])
        if not sym:
            notes.append(f"mint bilinmiyor {d['mint'][:8]}… Δ{d['delta']:+.4g}")
            continue
        scored.append((abs(float(d["delta"])), d, sym))
    scored.sort(key=lambda x: -x[0])

    for _abs, d, sym in scored[:3]:
        side = "BUY" if d["delta"] > 0 else "SELL"
        try:
            msg = execute_copy(
                side=side,
                base=sym.upper(),
                api_key=api_key,
                api_secret=api_secret,
                state=state,
                sig=sig,
            )
            notes.append(f"{side} {sym}: {msg}")
            print(f"  → {msg}")
        except Exception as e:
            notes.append(f"HATA {side} {sym}: {e}")
            print(f"  ! HATA {side} {sym}: {e}")
        time.sleep(0.4)
    return notes


def main() -> int:
    print("=" * 60)
    print("Solana → Binance COPY (ÖRNEK)")
    print(f"Cüzdan : {SOLANA_WALLET}")
    print(f"Slot   : USDT/{BALANCE_SLOTS} × {COPY_PCT_OF_SLOT:.0%}")
    print(f"Mod    : {'DRY/PAPER' if (DRY_RUN or not LIVE) else 'LIVE'}")
    print(f"RPC    : {'Helius' if clean(HELIUS_API_KEY) or os.getenv('HELIUS_API_KEY') else 'public (yavaş/ban riski)'}")
    print("=" * 60)

    api_key, api_secret = keys()
    if LIVE and not DRY_RUN:
        try:
            free = bn_free_usdt(api_key, api_secret)
            print(f"[binance] USDT free ≈ ${free:.2f} | slot ≈ ${slot_usdt(free):.2f}")
        except Exception as e:
            raise SystemExit(f"Binance bağlanamadı: {e}") from e

    state = load_state()
    seen = set(state.get("seen_sigs") or [])

    # ilk turda geçmişi işaretle (açılışta eski tx'leri ALMA)
    print("[init] mevcut imzalar işaretleniyor (geçmiş kopyalanmaz)…")
    try:
        rows = rpc("getSignaturesForAddress", [SOLANA_WALLET, {"limit": MAX_SIGS_PER_POLL}]) or []
        for row in rows:
            sig = row.get("signature")
            if sig:
                seen.add(sig)
        state["seen_sigs"] = list(seen)
        save_state(state)
        print(f"  → {len(rows)} sig işaretli. Yeni işlemler kopyalanacak.")
    except Exception as e:
        print(f"[init] RPC hata: {e}")
        print("  Helius API key koy (HELIUS_API_KEY) veya sonra tekrar dene.")

    print(f"[loop] her {POLL_SECONDS}s… Ctrl+C dur\n")
    while True:
        try:
            news = fetch_new_sigs(SOLANA_WALLET, seen)
            if not news:
                print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC — yeni tx yok")
            for sig in news:
                print(f"\n[tx] {sig}")
                process_sig(sig, SOLANA_WALLET, api_key, api_secret, state)
                seen.add(sig)
                state["seen_sigs"] = list(seen)
                save_state(state)
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\nDurdu")
            save_state(state)
            return 0
        except Exception as e:
            print(f"[loop hata] {e}")
            time.sleep(5)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
