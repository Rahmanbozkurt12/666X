#!/usr/bin/env python3
"""
Yeni havuz botu — her açılan */SOL havuza ~$0.50 gir, ~$1 olunca sat.

  1) phantom_keys.example.json → kopyala → phantom_keys.json
  2) privateKey + walletPublicKey doldur (apiKey boş kalabilir)
  3) pip install requests solders
  4) python phantom.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

# =============================================================================
# KEY — phantom_keys.json oku (Git'e koyma)
# =============================================================================
# Aynı klasöre phantom_keys.json koy:
# {
#   "apiKey": "",
#   "walletPublicKey": "ADRES",
#   "privateKey": "UZUN_KEY"
# }
SOLANA_PRIVATE_KEY = ""
HELIUS_API_KEY = ""
JUPITER_API_KEY = ""
EXPECTED_PUBKEY = ""
KEYS_PATH = Path(__file__).resolve().parent / "phantom_keys.json"

_env_pk = (os.environ.get("SOLANA_PRIVATE_KEY") or "").strip()
_env_helius = (os.environ.get("HELIUS_API_KEY") or "").strip()
_env_jup = (os.environ.get("JUPITER_API_KEY") or "").strip()
if _env_pk:
    SOLANA_PRIVATE_KEY = _env_pk
if _env_helius:
    HELIUS_API_KEY = _env_helius
if _env_jup:
    JUPITER_API_KEY = _env_jup


def load_keys_file() -> None:
    """phantom_keys.json → privateKey alanını yükle."""
    global SOLANA_PRIVATE_KEY, HELIUS_API_KEY, JUPITER_API_KEY, EXPECTED_PUBKEY
    if not KEYS_PATH.exists():
        return
    try:
        data = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"phantom_keys.json okunamadı: {e}") from e
    pk = (data.get("privateKey") or data.get("private_key") or data.get("secretKey") or "").strip()
    if pk:
        SOLANA_PRIVATE_KEY = pk
    pub = (data.get("walletPublicKey") or data.get("publicKey") or data.get("address") or "").strip()
    if pub:
        EXPECTED_PUBKEY = pub
    api = (data.get("apiKey") or data.get("api_key") or "").strip()
    if api and not HELIUS_API_KEY and len(api) < 80:
        HELIUS_API_KEY = api
    print(f"[keys] yüklendi: {KEYS_PATH.name}", flush=True)


# =============================================================================
# STRATEJİ — $0.50 AL → $1.00 SAT
# =============================================================================
DRY_RUN = True
BUY_USD = 0.50                      # her yeni havuza giriş
SELL_USD = 1.00                     # bu değere gelince sat (≈2x)
MAX_OPEN = 8                        # aynı anda max pozisyon
MIN_SOL_RESERVE_USD = 1.0           # gas için ~$1 SOL bırak
SLIPPAGE_BPS = 200                  # %2
PRIORITY_FEE = "auto"

# Yeni havuz filtreleri (aşırı çöpü ele)
MIN_LIQ_USD = 500.0                 # çok boş havuza girme
MAX_LIQ_USD = 250_000.0
MAX_PAIR_AGE_MIN = 90               # son 90 dk içinde açılan
MIN_PAIR_AGE_SEC = 20               # 20sn bekle (anlık rug)
REQUIRE_JUPITER_SELL_ROUTE = True   # satılamayana girme
SKIP_IF_MINT_AUTHORITY = False      # pump.fun çoğu mint auth'lu — kapalı
SKIP_IF_FREEZE_AUTHORITY = True
REQUIRE_SOL_QUOTE = True

POLL_SEC = 12.0
SCAN_SEC = 18.0
STOP_LOSS_USD = 0.22                # ~$0.50 → $0.22 acil çık
MAX_HOLD_MIN = 120

SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_BASE = "https://lite-api.jup.ag/swap/v1"
GT_BASE = "https://api.geckoterminal.com/api/v2"
DS_BASE = "https://api.dexscreener.com/latest/dex"

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "output" / "phantom_050_state.json"
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "phantom-050-bot/1.0", "Accept": "application/json"})


def _apply_http_api_key() -> None:
    if JUPITER_API_KEY:
        HTTP.headers["x-api-key"] = JUPITER_API_KEY


_sol_px_cache = {"ts": 0.0, "px": 0.0}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def rpc_url() -> str:
    if HELIUS_API_KEY:
        return f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    return os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()


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


def load_keypair() -> Keypair:
    load_keys_file()
    _apply_http_api_key()
    raw = SOLANA_PRIVATE_KEY.strip().strip('"').strip("'")
    if not raw:
        raise SystemExit(
            "Private key yok.\n"
            "1) phantom_keys.json oluştur (örnek: phantom_keys.example.json)\n"
            "2) privateKey alanına uzun KEY'i yaz\n"
            "veya SOLANA_PRIVATE_KEY = 'KEY' yaz."
        )
    if raw.startswith("["):
        kp = Keypair.from_bytes(bytes(json.loads(raw)))
    elif len(raw) < 80:
        raise SystemExit(
            f"privateKey çok kısa ({len(raw)} karakter) — bu muhtemelen ADRES.\n"
            "walletPublicKey = ADRES (Solscan / Binance)\n"
            "privateKey = uzun KEY (87+ karakter)\n"
            "Dosyaya privateKey yaz."
        )
    else:
        try:
            kp = Keypair.from_base58_string(raw)
        except Exception as e:
            raise SystemExit(
                f"Private key okunamadı: {e}\n"
                "KEY'i baştan kopyala; boşluk/tırnak olmasın."
            ) from e
    if EXPECTED_PUBKEY and str(kp.pubkey()) != EXPECTED_PUBKEY:
        raise SystemExit(
            f"KEY ile walletPublicKey uyuşmuyor!\n"
            f"  key→ {kp.pubkey()}\n"
            f"  json→ {EXPECTED_PUBKEY}\n"
            "Doğru privateKey / walletPublicKey çiftini kullan."
        )
    return kp


def sol_usd() -> float:
    now = time.time()
    if now - _sol_px_cache["ts"] < 60 and _sol_px_cache["px"] > 0:
        return _sol_px_cache["px"]
    try:
        r = HTTP.get(f"{DS_BASE}/tokens/{SOL_MINT}", timeout=20)
        ps = (r.json() or {}).get("pairs") or []
        for p in ps:
            if p.get("chainId") == "solana" and p.get("priceUsd"):
                px = float(p["priceUsd"])
                if px > 0:
                    _sol_px_cache.update(ts=now, px=px)
                    return px
    except Exception as e:
        log(f"SOL fiyat: {e}")
    if _sol_px_cache["px"] > 0:
        return _sol_px_cache["px"]
    return 150.0  # fallback


def usd_to_lamports(usd: float) -> int:
    px = sol_usd()
    sol = usd / px
    return max(1, int(sol * 1e9))


def sol_balance(pubkey: str) -> float:
    return int(rpc("getBalance", [pubkey])["value"]) / 1e9


def token_raw_balance(owner: str, mint: str) -> int:
    res = rpc(
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    total = 0
    for acc in res.get("value") or []:
        total += int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    return total


def account_exists(addr: str) -> bool:
    try:
        info = rpc("getAccountInfo", [addr, {"encoding": "base64"}])
        return bool(info and info.get("value"))
    except Exception:
        return False


def mint_risk_flags(mint: str) -> dict[str, bool]:
    out = {"mint_auth": False, "freeze_auth": False, "ok": False}
    try:
        info = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        val = (info or {}).get("value")
        if not val or not isinstance(val.get("data"), dict):
            return out
        info2 = ((val["data"].get("parsed") or {}).get("info") or {})
        out["mint_auth"] = info2.get("mintAuthority") is not None
        out["freeze_auth"] = info2.get("freezeAuthority") is not None
        out["ok"] = True
    except Exception:
        pass
    return out


def jup_quote(input_mint: str, output_mint: str, amount: int) -> dict:
    r = HTTP.get(
        f"{JUP_BASE}/quote",
        params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(SLIPPAGE_BPS),
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"quote HTTP {r.status_code}: {r.text[:160]}")
    data = r.json()
    if "outAmount" not in data:
        raise RuntimeError(f"quote yok: {data}")
    return data


def jup_swap_b64(user: str, quote: dict) -> str:
    r = HTTP.post(
        f"{JUP_BASE}/swap",
        json={
            "userPublicKey": user,
            "quoteResponse": quote,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": PRIORITY_FEE,
        },
        timeout=45,
    )
    r.raise_for_status()
    tx = r.json().get("swapTransaction")
    if not tx:
        raise RuntimeError(f"swap tx yok: {r.text[:180]}")
    return tx


def send_swap(kp: Keypair, quote: dict) -> str:
    raw = base64.b64decode(jup_swap_b64(str(kp.pubkey()), quote))
    vtx = VersionedTransaction.from_bytes(raw)
    sig = kp.sign_message(to_bytes_versioned(vtx.message))
    signed = VersionedTransaction.populate(vtx.message, [sig])
    return str(
        rpc(
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
    )


def discover_new_pools() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    r = HTTP.get(f"{GT_BASE}/networks/solana/new_pools?page=1", timeout=30)
    if r.status_code != 200:
        log(f"new_pools HTTP {r.status_code}")
        return []
    for item in (r.json() or {}).get("data") or []:
        at = item.get("attributes") or {}
        pool = at.get("address") or ""
        if not pool or pool in seen:
            continue
        name = (at.get("name") or "").upper()
        if REQUIRE_SOL_QUOTE and "/ SOL" not in name and not name.endswith("/SOL"):
            # quote ilişkisi
            qid = (((item.get("relationships") or {}).get("quote_token") or {}).get("data") or {}).get("id") or ""
            if SOL_MINT not in qid:
                continue
        base_id = (((item.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
        base = base_id.split("solana_", 1)[-1] if "solana_" in base_id else base_id
        vol = at.get("volume_usd") or {}
        rows.append(
            {
                "pool": pool,
                "name": at.get("name") or "?",
                "base_mint": base,
                "created_at": at.get("pool_created_at"),
                "vol_m5": float(vol.get("m5") or 0),
                "reserve_usd": float(at.get("reserve_in_usd") or 0)
                if at.get("reserve_in_usd") not in (None, "")
                else 0.0,
            }
        )
        seen.add(pool)
    return rows


def dexscreener_pair(pool: str) -> Optional[dict]:
    r = HTTP.get(f"{DS_BASE}/pairs/solana/{pool}", timeout=25)
    if r.status_code != 200:
        return None
    data = r.json() or {}
    pair = data.get("pair") or (data.get("pairs") or [None])[0]
    return pair if isinstance(pair, dict) else None


def enrich(row: dict) -> Optional[dict]:
    pool = row["pool"]
    if not account_exists(pool):
        return None
    ds = dexscreener_pair(pool)
    base_mint = row.get("base_mint") or ""
    symbol = (row.get("name") or "?").split("/")[0].strip()
    liq = float(row.get("reserve_usd") or 0)
    price_usd = 0.0
    created_ms = None
    if ds:
        liq = float(((ds.get("liquidity") or {}).get("usd")) or liq or 0)
        base_mint = (ds.get("baseToken") or {}).get("address") or base_mint
        symbol = (ds.get("baseToken") or {}).get("symbol") or symbol
        price_usd = float(ds.get("priceUsd") or 0)
        created_ms = ds.get("pairCreatedAt")
        quote = ((ds.get("quoteToken") or {}).get("address") or "").strip()
        if REQUIRE_SOL_QUOTE and quote and quote != SOL_MINT:
            return None
    if not base_mint or base_mint == SOL_MINT:
        return None
    if liq < MIN_LIQ_USD or liq > MAX_LIQ_USD:
        return None

    age_min = None
    if created_ms:
        age_min = max(0.0, (time.time() * 1000 - float(created_ms)) / 60000.0)
    elif row.get("created_at"):
        try:
            dt = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        except Exception:
            age_min = None
    if age_min is not None:
        if age_min * 60 < MIN_PAIR_AGE_SEC:
            return None
        if age_min > MAX_PAIR_AGE_MIN:
            return None

    return {
        **row,
        "base_mint": base_mint,
        "symbol": symbol,
        "liq_usd": liq,
        "price_usd": price_usd,
        "age_min": age_min,
    }


@dataclass
class Position:
    mint: str
    pool: str
    symbol: str
    cost_usd: float
    entry_ts: float
    paper_raw: int = 0


@dataclass
class State:
    positions: dict[str, Position] = field(default_factory=dict)
    seen_pools: dict[str, float] = field(default_factory=dict)  # pool -> ts
    cooldown: dict[str, float] = field(default_factory=dict)

    def save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(
                {
                    "positions": {k: v.__dict__ for k, v in self.positions.items()},
                    "seen_pools": self.seen_pools,
                    "cooldown": self.cooldown,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls) -> "State":
        st = cls()
        if not STATE_PATH.exists():
            return st
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            for k, v in (raw.get("positions") or {}).items():
                st.positions[k] = Position(**v)
            st.seen_pools = {k: float(v) for k, v in (raw.get("seen_pools") or {}).items()}
            st.cooldown = {k: float(v) for k, v in (raw.get("cooldown") or {}).items()}
        except Exception as e:
            log(f"state: {e}")
        return st


def position_value_usd(owner: str, pos: Position) -> tuple[float, int]:
    """Token → SOL quote → USD."""
    raw = token_raw_balance(owner, pos.mint)
    if raw <= 0 and DRY_RUN:
        raw = pos.paper_raw
    if raw <= 0:
        return 0.0, 0
    q = jup_quote(pos.mint, SOL_MINT, raw)
    out_sol = int(q["outAmount"]) / 1e9
    return out_sol * sol_usd(), raw


def try_buy(kp: Keypair, st: State, e: dict) -> None:
    mint = e["base_mint"]
    pool = e["pool"]
    sym = e["symbol"]
    if mint in st.positions:
        return
    if pool in st.seen_pools and mint not in st.positions:
        # bu havuza daha önce bakıldı / girildi
        return
    if time.time() < st.cooldown.get(mint, 0):
        return
    if len(st.positions) >= MAX_OPEN:
        return

    flags = mint_risk_flags(mint)
    if flags.get("ok"):
        if SKIP_IF_MINT_AUTHORITY and flags.get("mint_auth"):
            log(f"{sym} SKIP mintAuth")
            st.seen_pools[pool] = time.time()
            return
        if SKIP_IF_FREEZE_AUTHORITY and flags.get("freeze_auth"):
            log(f"{sym} SKIP freezeAuth")
            st.seen_pools[pool] = time.time()
            return

    pub = str(kp.pubkey())
    need_sol = BUY_USD / sol_usd()
    reserve = MIN_SOL_RESERVE_USD / sol_usd()
    bal = sol_balance(pub)
    if bal < need_sol + reserve:
        log(f"SOL yetersiz {bal:.4f} (gerek≈{need_sol + reserve:.4f})")
        return

    lamports = usd_to_lamports(BUY_USD)
    try:
        buy_q = jup_quote(SOL_MINT, mint, lamports)
    except Exception as ex:
        log(f"{sym} SKIP AL route: {ex}")
        st.seen_pools[pool] = time.time()
        return
    out_raw = int(buy_q["outAmount"])
    if out_raw <= 0:
        st.seen_pools[pool] = time.time()
        return

    if REQUIRE_JUPITER_SELL_ROUTE:
        try:
            jup_quote(mint, SOL_MINT, max(out_raw // 10, 1))
        except Exception as ex:
            log(f"{sym} SKIP SAT route yok: {ex}")
            st.seen_pools[pool] = time.time()
            return

    log(
        f"AL ${BUY_USD:.2f} → {sym} | pool={pool[:10]}… liq=${e['liq_usd']:.0f} "
        f"age={e.get('age_min')} SOL≈{sol_usd():.2f}"
    )

    if DRY_RUN:
        log(f"{sym} DRY_RUN AL (gönderilmedi)")
        st.positions[mint] = Position(
            mint=mint, pool=pool, symbol=sym, cost_usd=BUY_USD, entry_ts=time.time(), paper_raw=out_raw
        )
        st.seen_pools[pool] = time.time()
        st.save()
        return

    sig = send_swap(kp, buy_q)
    log(f"{sym} AL OK https://solscan.io/tx/{sig}")
    time.sleep(2.0)
    raw = token_raw_balance(pub, mint)
    st.positions[mint] = Position(
        mint=mint, pool=pool, symbol=sym, cost_usd=BUY_USD, entry_ts=time.time(), paper_raw=raw
    )
    st.seen_pools[pool] = time.time()
    st.save()


def try_sell(kp: Keypair, st: State, pos: Position, reason: str, value_usd: float, raw: int) -> None:
    if raw <= 0:
        st.positions.pop(pos.mint, None)
        st.cooldown[pos.mint] = time.time() + 15 * 60
        st.save()
        return
    try:
        q = jup_quote(pos.mint, SOL_MINT, raw)
    except Exception as e:
        log(f"{pos.symbol} SAT quote fail: {e}")
        return
    out_sol = int(q["outAmount"]) / 1e9
    log(f"SAT {pos.symbol} ({reason}) değer≈${value_usd:.2f} → {out_sol:.5f} SOL")
    if DRY_RUN:
        log(f"{pos.symbol} DRY_RUN SAT")
        st.positions.pop(pos.mint, None)
        st.cooldown[pos.mint] = time.time() + 15 * 60
        st.save()
        return
    sig = send_swap(kp, q)
    log(f"{pos.symbol} SAT OK https://solscan.io/tx/{sig}")
    st.positions.pop(pos.mint, None)
    st.cooldown[pos.mint] = time.time() + 15 * 60
    st.save()


def manage_positions(kp: Keypair, st: State) -> None:
    pub = str(kp.pubkey())
    for mint, pos in list(st.positions.items()):
        try:
            value_usd, raw = position_value_usd(pub, pos)
        except Exception as e:
            log(f"{pos.symbol} değer okunamadı: {e}")
            continue
        held = (time.time() - pos.entry_ts) / 60.0
        log(
            f"POS {pos.symbol} değer≈${value_usd:.2f} (hedef ${SELL_USD:.2f} / SL ${STOP_LOSS_USD:.2f}) "
            f"hold={held:.1f}m"
        )
        reason = None
        if value_usd >= SELL_USD:
            reason = f"TP ${value_usd:.2f}>={SELL_USD}"
        elif value_usd > 0 and value_usd <= STOP_LOSS_USD:
            reason = f"SL ${value_usd:.2f}"
        elif held >= MAX_HOLD_MIN:
            reason = "MAX_HOLD"
        if reason:
            try_sell(kp, st, pos, reason, value_usd, raw)


def scan_new(kp: Keypair, st: State) -> None:
    raw = discover_new_pools()
    log(f"yeni havuz tarama={len(raw)} | SOL=${sol_usd():.2f} | buy=${BUY_USD} sell=${SELL_USD}")
    # eski seen temizle
    cut = time.time() - 6 * 3600
    st.seen_pools = {k: v for k, v in st.seen_pools.items() if v >= cut}

    for row in raw:
        if row["pool"] in st.seen_pools:
            continue
        e = enrich(row)
        if not e:
            st.seen_pools[row["pool"]] = time.time()
            continue
        log(f"aday {e['symbol']} liq=${e['liq_usd']:.0f} age={e.get('age_min')} pool={e['pool'][:10]}…")
        try_buy(kp, st, e)
        if len(st.positions) >= MAX_OPEN:
            break
    st.save()


def main() -> None:
    kp = load_keypair()
    st = State.load()
    log("=" * 56)
    log(f"$0.50 AL → $1.00 SAT | yeni Solana havuzları | DRY_RUN={DRY_RUN}")
    log(f"pubkey={kp.pubkey()} | SOL≈${sol_usd():.2f}")
    log("=" * 56)
    last_scan = 0.0
    while True:
        try:
            manage_positions(kp, st)
            now = time.time()
            if now - last_scan >= SCAN_SEC:
                scan_new(kp, st)
                last_scan = now
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
