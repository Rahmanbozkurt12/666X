#!/usr/bin/env python3
"""
Solana likidite avcısı — Jupiter AL/SAT

Ne yapar:
  • Yeni / hacmi↑ / havuz likiditesi↑ coinleri tarar (GeckoTerminal + DexScreener)
  • Pool adresini doğrular (RPC + Jupiter route var mı)
  • Giriş: likidite + hacim artıyorsa
  • Çıkış: havuzdaki para (likidite) zirveden düşmeye başlayınca
  • Komisyon: round-trip fee + slippage tamponu; Jupiter quote fail → girme

Phantom API yok. SOLANA_PRIVATE_KEY (ayrı trading cüzdanı) ile imzalar.

  export SOLANA_PRIVATE_KEY='...'
  # önerilir: export HELIUS_API_KEY='...'
  python phantom_jupiter_bot.py
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
# KEY — sadece ADRES yetmez; AL/SAT için PRIVATE KEY şart
# =============================================================================
# 1) Bilgisayarda üret:
#    python3 -c "from solders.keypair import Keypair; k=Keypair(); print('ADRES', k.pubkey()); print('KEY', k)"
# 2) Phantom'dan o ADRES'e SOL gönder
# 3) Aşağıya KEY'i yapıştır (ADRES değil!)
SOLANA_PRIVATE_KEY = ""             # buraya private key (base58) — ADRES DEĞİL
# Opsiyonel (boş bırakılabilir):
HELIUS_API_KEY = ""                 # https://helius.dev — boşsa public RPC
JUPITER_API_KEY = ""                # boş bırak — lite API çalışır

# Env varsa dosyadakinin üstüne yazar
SOLANA_PRIVATE_KEY = (os.environ.get("SOLANA_PRIVATE_KEY") or SOLANA_PRIVATE_KEY).strip()
HELIUS_API_KEY = (os.environ.get("HELIUS_API_KEY") or HELIUS_API_KEY).strip()
JUPITER_API_KEY = (os.environ.get("JUPITER_API_KEY") or JUPITER_API_KEY).strip()

DRY_RUN = True                      # True = zincire gönderme
POLL_SEC = 20.0
SCAN_SEC = 35.0

BUY_SOL = 0.05                      # her AL (SOL)
MIN_SOL_RESERVE = 0.03              # gas + fee için bırak
MAX_OPEN = 3                        # aynı anda max pozisyon
SLIPPAGE_BPS = 150                  # %1.5 (meme için)
PRIORITY_FEE = "auto"

# Komisyon / tampon (yaklaşık)
# Jupiter/DEX ~%0.25–1 + slippage + network → round-trip güvenlik
ROUNDTRIP_FEE_PCT = 2.5             # %2.5 maliyet varsay
MIN_EDGE_OVER_FEE_PCT = 1.0         # TP en az fee+%1 (bilgi; asıl çıkış likidite)

# Giriş filtreleri
MIN_LIQ_USD = 8_000.0               # bundan düşük havuza girme (sıkışır)
MAX_LIQ_USD = 800_000.0             # çok büyük pool = geç kalmış
MIN_VOL_H1_USD = 3_000.0
MIN_LIQ_RISE_PCT = 8.0              # izlenen süre içinde liq ↑
MIN_VOL_RISE_PCT = 15.0             # vol h1 vs önceki örnek ↑
MAX_PAIR_AGE_MIN = 180              # "çıkan coin" ≈ 3 saat
MIN_PAIR_AGE_SEC = 45               # çok taze rug riski — 45sn bekle
REQUIRE_SOL_QUOTE = True            # sadece */SOL havuz

# Çıkış: havuz parası eksilince
LIQ_DROP_FROM_PEAK_PCT = 12.0       # zirveden -%12 → SAT
LIQ_DROP_ABS_PCT = 8.0              # son ölçüme göre -%8 → SAT
STOP_LOSS_PCT = -25.0               # fiyat acil stop
TAKE_PROFIT_PCT = 45.0              # opsiyonel TP
MAX_HOLD_MIN = 90                   # süre dolunca çık

# Güvenlik
SKIP_IF_MINT_AUTHORITY = True       # mint authority varsa atla (rug)
SKIP_IF_FREEZE_AUTHORITY = True
REQUIRE_JUPITER_SELL_ROUTE = True   # satılamayan tokena girme
BLACKLIST_DEX = {"unknown"}         # gerekirse genişlet

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUP_BASE = "https://lite-api.jup.ag/swap/v1"
GT_BASE = "https://api.geckoterminal.com/api/v2"
DS_BASE = "https://api.dexscreener.com/latest/dex"

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "output" / "phantom_liq_state.json"
HTTP = requests.Session()
HTTP.headers.update(
    {
        "User-Agent": "phantom-liq-bot/1.1",
        "Accept": "application/json",
    }
)
if JUPITER_API_KEY:
    HTTP.headers["x-api-key"] = JUPITER_API_KEY


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
    raw = SOLANA_PRIVATE_KEY or os.environ.get("SOLANA_PRIVATE_KEY", "").strip()
    if not raw or raw.startswith("BURAYA"):
        raise SystemExit(
            "SOLANA_PRIVATE_KEY yok.\n"
            "Yeni key üret → Phantom'dan o ADRES'e SOL gönder:\n"
            "  python3 -c \"from solders.keypair import Keypair; "
            "k=Keypair(); print('ADRES', k.pubkey()); print('KEY', k)\""
        )
    if raw.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(raw)))
    return Keypair.from_base58_string(raw)


def sol_balance(pubkey: str) -> float:
    return int(rpc("getBalance", [pubkey])["value"]) / 1e9


def token_raw_balance(owner: str, mint: str) -> int:
    res = rpc(
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    total = 0
    for acc in res.get("value") or []:
        info = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
        total += int(info["amount"])
    return total


def account_exists(addr: str) -> bool:
    try:
        info = rpc("getAccountInfo", [addr, {"encoding": "base64"}])
        return bool(info and info.get("value"))
    except Exception:
        return False


def mint_risk_flags(mint: str) -> dict[str, bool]:
    """mint/freeze authority varsa True (risk)."""
    out = {"mint_auth": False, "freeze_auth": False, "ok": False}
    try:
        info = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        val = (info or {}).get("value")
        if not val:
            return out
        parsed = (((val.get("data") or {}) if isinstance(val.get("data"), dict) else {}) or {})
        # jsonParsed: data is [...,] or dict with parsed
        if isinstance(val.get("data"), dict):
            p = val["data"].get("parsed") or {}
            info2 = p.get("info") or {}
            out["mint_auth"] = info2.get("mintAuthority") is not None
            out["freeze_auth"] = info2.get("freezeAuthority") is not None
            out["ok"] = True
    except Exception as e:
        log(f"mint parse uyarı {mint[:6]}…: {e}")
    return out


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
    if r.status_code != 200:
        raise RuntimeError(f"quote HTTP {r.status_code}: {r.text[:180]}")
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
        raise RuntimeError(f"swap tx yok: {r.text[:200]}")
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


# ---- keşif ----
def _gt_get(path: str) -> list[dict]:
    r = HTTP.get(f"{GT_BASE}{path}", timeout=30)
    if r.status_code != 200:
        log(f"GT {path} HTTP {r.status_code}")
        return []
    return list((r.json() or {}).get("data") or [])


def discover_pools() -> list[dict[str, Any]]:
    """Yeni + trending Solana havuzları."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in (
        "/networks/solana/new_pools?page=1",
        "/networks/solana/trending_pools?page=1",
    ):
        for item in _gt_get(path):
            at = item.get("attributes") or {}
            pool = at.get("address") or ""
            if not pool or pool in seen:
                continue
            rel = item.get("relationships") or {}
            base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
            quote_id = ((rel.get("quote_token") or {}).get("data") or {}).get("id") or ""
            # id: solana_<mint>
            def mint_of(x: str) -> str:
                return x.split("solana_", 1)[-1] if "solana_" in x else x

            base = mint_of(base_id)
            quote = mint_of(quote_id)
            if REQUIRE_SOL_QUOTE and quote not in (SOL_MINT, "solana", ""):
                # quote mint SOL değilse atla (bazen id kısa)
                if quote != SOL_MINT and "So11111111111111111111111111111111111111112" not in quote_id:
                    # hâlâ isimde / SOL var mı
                    name = (at.get("name") or "").upper()
                    if "/ SOL" not in name and not name.endswith("SOL"):
                        continue
            vol = at.get("volume_usd") or {}
            rows.append(
                {
                    "pool": pool,
                    "name": at.get("name") or "?",
                    "base_mint": base,
                    "quote_mint": quote if quote else SOL_MINT,
                    "created_at": at.get("pool_created_at"),
                    "vol_h1": float(vol.get("h1") or 0),
                    "vol_m5": float(vol.get("m5") or 0),
                    "reserve_usd": float(at.get("reserve_in_usd") or 0)
                    if at.get("reserve_in_usd") not in (None, "")
                    else 0.0,
                    "source": path.split("/")[-1].split("?")[0],
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
    """Pool doğrula + DexScreener likidite/hacim."""
    pool = row["pool"]
    if not account_exists(pool):
        log(f"SKIP pool yok on-chain: {pool[:8]}…")
        return None
    ds = dexscreener_pair(pool)
    liq = 0.0
    vol_h1 = row.get("vol_h1") or 0.0
    vol_m5 = row.get("vol_m5") or 0.0
    symbol = row.get("name") or "?"
    price_usd = 0.0
    created_ms = None
    base_mint = row.get("base_mint") or ""
    if ds:
        liq = float(((ds.get("liquidity") or {}).get("usd")) or 0)
        vol = ds.get("volume") or {}
        vol_h1 = float(vol.get("h1") or vol_h1 or 0)
        vol_m5 = float(vol.get("m5") or vol_m5 or 0)
        base_mint = (ds.get("baseToken") or {}).get("address") or base_mint
        symbol = (ds.get("baseToken") or {}).get("symbol") or symbol
        price_usd = float(ds.get("priceUsd") or 0)
        created_ms = ds.get("pairCreatedAt")
        dex = (ds.get("dexId") or "").lower()
        if dex in BLACKLIST_DEX:
            return None
        quote = ((ds.get("quoteToken") or {}).get("address") or "").strip()
        if REQUIRE_SOL_QUOTE and quote and quote != SOL_MINT:
            return None
    else:
        liq = float(row.get("reserve_usd") or 0)

    if not base_mint or base_mint == SOL_MINT:
        return None
    if liq < MIN_LIQ_USD or liq > MAX_LIQ_USD:
        return None
    if vol_h1 < MIN_VOL_H1_USD and vol_m5 * 12 < MIN_VOL_H1_USD:
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
        "symbol": symbol.split("/")[0].strip(),
        "liq_usd": liq,
        "vol_h1": vol_h1,
        "vol_m5": vol_m5,
        "price_usd": price_usd,
        "age_min": age_min,
    }


@dataclass
class PoolWatch:
    pool: str
    mint: str
    symbol: str
    liq_hist: list[tuple[float, float]] = field(default_factory=list)  # ts, liq
    vol_hist: list[tuple[float, float]] = field(default_factory=list)

    def push(self, liq: float, vol: float) -> None:
        now = time.time()
        self.liq_hist.append((now, liq))
        self.vol_hist.append((now, vol))
        self.liq_hist = self.liq_hist[-40:]
        self.vol_hist = self.vol_hist[-40:]

    def liq_rise_pct(self) -> Optional[float]:
        if len(self.liq_hist) < 2:
            return None
        a, b = self.liq_hist[0][1], self.liq_hist[-1][1]
        if a <= 0:
            return None
        return (b / a - 1.0) * 100.0

    def vol_rise_pct(self) -> Optional[float]:
        if len(self.vol_hist) < 2:
            return None
        a, b = self.vol_hist[0][1], self.vol_hist[-1][1]
        if a <= 0:
            return None
        return (b / a - 1.0) * 100.0


@dataclass
class Position:
    mint: str
    pool: str
    symbol: str
    cost_sol: float
    entry_liq: float
    peak_liq: float
    entry_ts: float
    entry_price_usd: float = 0.0
    paper_raw: int = 0


@dataclass
class State:
    watches: dict[str, PoolWatch] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    cooldown: dict[str, float] = field(default_factory=dict)

    def save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "positions": {
                k: {
                    "mint": p.mint,
                    "pool": p.pool,
                    "symbol": p.symbol,
                    "cost_sol": p.cost_sol,
                    "entry_liq": p.entry_liq,
                    "peak_liq": p.peak_liq,
                    "entry_ts": p.entry_ts,
                    "entry_price_usd": p.entry_price_usd,
                    "paper_raw": p.paper_raw,
                }
                for k, p in self.positions.items()
            },
            "cooldown": self.cooldown,
            "watches": {
                k: {
                    "pool": w.pool,
                    "mint": w.mint,
                    "symbol": w.symbol,
                    "liq_hist": w.liq_hist[-20:],
                    "vol_hist": w.vol_hist[-20:],
                }
                for k, w in self.watches.items()
            },
        }
        STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "State":
        st = cls()
        if not STATE_PATH.exists():
            return st
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            for k, v in (raw.get("positions") or {}).items():
                st.positions[k] = Position(**v)
            st.cooldown = {k: float(v) for k, v in (raw.get("cooldown") or {}).items()}
            for k, v in (raw.get("watches") or {}).items():
                w = PoolWatch(pool=v["pool"], mint=v["mint"], symbol=v.get("symbol") or "?")
                w.liq_hist = [tuple(x) for x in (v.get("liq_hist") or [])]  # type: ignore
                w.vol_hist = [tuple(x) for x in (v.get("vol_hist") or [])]  # type: ignore
                st.watches[k] = w
        except Exception as e:
            log(f"state load: {e}")
        return st


def fee_ok_for_entry(buy_sol: float) -> bool:
    """Round-trip fee sonrası anlamlı boyut kalsın."""
    cost = buy_sol * (ROUNDTRIP_FEE_PCT / 100.0)
    return buy_sol - cost >= buy_sol * 0.5 and buy_sol >= 0.01


def can_sell_via_jupiter(mint: str, raw_amount: int) -> bool:
    if raw_amount <= 0:
        # tahmini küçük miktar ile route dene
        raw_amount = 100000
    try:
        q = jup_quote(mint, SOL_MINT, raw_amount, SLIPPAGE_BPS)
        return int(q.get("outAmount") or 0) > 0
    except Exception as e:
        log(f"Jupiter SAT route yok {mint[:6]}…: {e}")
        return False


def score_candidate(e: dict, w: PoolWatch) -> float:
    lr = w.liq_rise_pct() or 0.0
    vr = w.vol_rise_pct() or 0.0
    age = e.get("age_min") or 99.0
    # yeni + liq↑ + vol↑
    s = lr * 1.2 + vr * 1.0 + min(e.get("vol_m5") or 0, 5000) / 500.0
    s += max(0.0, 60.0 - age) * 0.15
    return s


def try_buy(kp: Keypair, st: State, e: dict) -> None:
    mint = e["base_mint"]
    pool = e["pool"]
    sym = e["symbol"]
    if mint in st.positions:
        return
    if time.time() < st.cooldown.get(mint, 0):
        return
    if len(st.positions) >= MAX_OPEN:
        return
    if not fee_ok_for_entry(BUY_SOL):
        return

    flags = mint_risk_flags(mint)
    if flags.get("ok"):
        if SKIP_IF_MINT_AUTHORITY and flags.get("mint_auth"):
            log(f"{sym} SKIP mintAuthority var (rug risk) pool={pool[:8]}…")
            return
        if SKIP_IF_FREEZE_AUTHORITY and flags.get("freeze_auth"):
            log(f"{sym} SKIP freezeAuthority var pool={pool[:8]}…")
            return

    pub = str(kp.pubkey())
    sol = sol_balance(pub)
    if sol < BUY_SOL + MIN_SOL_RESERVE:
        log(f"SOL yetersiz {sol:.4f} (gerek≈{BUY_SOL + MIN_SOL_RESERVE:.4f})")
        return

    # Önce AL quote
    lamports = int(BUY_SOL * 1e9)
    try:
        buy_q = jup_quote(SOL_MINT, mint, lamports, SLIPPAGE_BPS)
    except Exception as ex:
        log(f"{sym} SKIP Jupiter AL route yok: {ex}")
        return
    out_raw = int(buy_q["outAmount"])
    if out_raw <= 0:
        return

    # Satış rotası zorunlu — paramız içeride kalmasın
    if REQUIRE_JUPITER_SELL_ROUTE and not can_sell_via_jupiter(mint, max(out_raw // 10, 1)):
        log(f"{sym} SKIP — SAT route yok (sıkışırdı) pool={pool}")
        return

    impact = float(buy_q.get("priceImpactPct") or 0)
    log(
        f"AL aday {sym} pool={pool} liq=${e['liq_usd']:.0f} vol1h=${e['vol_h1']:.0f} "
        f"age={e.get('age_min')} impact={impact}% fee_buf≈{ROUNDTRIP_FEE_PCT}%"
    )

    if DRY_RUN:
        log(f"{sym} DRY_RUN AL {BUY_SOL} SOL (gönderilmedi)")
        st.positions[mint] = Position(
            mint=mint,
            pool=pool,
            symbol=sym,
            cost_sol=BUY_SOL,
            entry_liq=float(e["liq_usd"]),
            peak_liq=float(e["liq_usd"]),
            entry_ts=time.time(),
            entry_price_usd=float(e.get("price_usd") or 0),
            paper_raw=out_raw,
        )
        st.save()
        return

    sig = send_swap(kp, buy_q)
    log(f"{sym} AL OK https://solscan.io/tx/{sig}")
    # kısa bekle + bakiye
    time.sleep(2.5)
    raw = token_raw_balance(pub, mint)
    st.positions[mint] = Position(
        mint=mint,
        pool=pool,
        symbol=sym,
        cost_sol=BUY_SOL,
        entry_liq=float(e["liq_usd"]),
        peak_liq=float(e["liq_usd"]),
        entry_ts=time.time(),
        entry_price_usd=float(e.get("price_usd") or 0),
        paper_raw=raw,
    )
    st.save()


def try_sell(kp: Keypair, st: State, pos: Position, reason: str, liq_now: float) -> None:
    pub = str(kp.pubkey())
    raw = token_raw_balance(pub, pos.mint)
    if raw <= 0 and DRY_RUN:
        raw = pos.paper_raw
    if raw <= 0:
        log(f"{pos.symbol} SAT bakiye 0 — poz silindi")
        st.positions.pop(pos.mint, None)
        st.cooldown[pos.mint] = time.time() + 20 * 60
        st.save()
        return
    try:
        q = jup_quote(pos.mint, SOL_MINT, raw, SLIPPAGE_BPS)
    except Exception as e:
        log(f"{pos.symbol} SAT quote fail (bekleniyor): {e}")
        return
    out_sol = int(q["outAmount"]) / 1e9
    fee_est = pos.cost_sol * (ROUNDTRIP_FEE_PCT / 100.0)
    net = out_sol - (pos.cost_sol)  # kaba
    log(
        f"SAT {pos.symbol} ({reason}) liq=${liq_now:.0f} peak=${pos.peak_liq:.0f} "
        f"→ {out_sol:.4f} SOL | kaba_net≈{net:+.4f} fee≈{fee_est:.4f}"
    )
    if DRY_RUN:
        log(f"{pos.symbol} DRY_RUN SAT")
        st.positions.pop(pos.mint, None)
        st.cooldown[pos.mint] = time.time() + 20 * 60
        st.save()
        return
    sig = send_swap(kp, q)
    log(f"{pos.symbol} SAT OK https://solscan.io/tx/{sig}")
    st.positions.pop(pos.mint, None)
    st.cooldown[pos.mint] = time.time() + 20 * 60
    st.save()


def manage_positions(kp: Keypair, st: State) -> None:
    for mint, pos in list(st.positions.items()):
        ds = dexscreener_pair(pos.pool)
        liq = float(((ds or {}).get("liquidity") or {}).get("usd") or 0) if ds else 0.0
        if liq <= 0:
            # pool kayboldu / rug — çıkmayı dene
            log(f"{pos.symbol} pool likidite okunamadı → acil SAT denenecek")
            try_sell(kp, st, pos, "POOL_GONE", 0)
            continue
        pos.peak_liq = max(pos.peak_liq, liq)
        price = float((ds or {}).get("priceUsd") or 0)
        pnl_px = None
        if pos.entry_price_usd > 0 and price > 0:
            pnl_px = (price / pos.entry_price_usd - 1.0) * 100.0
        drop_peak = (liq / pos.peak_liq - 1.0) * 100.0 if pos.peak_liq > 0 else 0.0
        drop_entry = (liq / pos.entry_liq - 1.0) * 100.0 if pos.entry_liq > 0 else 0.0
        held_min = (time.time() - pos.entry_ts) / 60.0
        log(
            f"POS {pos.symbol} liq=${liq:.0f} peak={pos.peak_liq:.0f} "
            f"dPeak={drop_peak:.1f}% dEntry={drop_entry:.1f}% pnl≈{pnl_px} hold={held_min:.1f}m"
        )

        reason = None
        if drop_peak <= -LIQ_DROP_FROM_PEAK_PCT:
            reason = f"LIQ_DROP_PEAK {drop_peak:.1f}%"
        elif drop_entry <= -LIQ_DROP_ABS_PCT:
            reason = f"LIQ_DROP_ENTRY {drop_entry:.1f}%"
        elif pnl_px is not None and pnl_px <= STOP_LOSS_PCT:
            reason = f"SL {pnl_px:.1f}%"
        elif pnl_px is not None and pnl_px >= TAKE_PROFIT_PCT:
            # TP sadece fee üstü
            if pnl_px >= ROUNDTRIP_FEE_PCT + MIN_EDGE_OVER_FEE_PCT:
                reason = f"TP {pnl_px:.1f}%"
        elif held_min >= MAX_HOLD_MIN:
            reason = "MAX_HOLD"

        if reason:
            try_sell(kp, st, pos, reason, liq)
        else:
            st.positions[mint] = pos
    st.save()


def scan_and_enter(kp: Keypair, st: State) -> None:
    raw = discover_pools()
    log(f"tarama aday={len(raw)} (new+trending)")
    scored: list[tuple[float, dict, PoolWatch]] = []
    for row in raw:
        e = enrich(row)
        if not e:
            continue
        w = st.watches.get(e["pool"]) or PoolWatch(
            pool=e["pool"], mint=e["base_mint"], symbol=e["symbol"]
        )
        w.mint = e["base_mint"]
        w.symbol = e["symbol"]
        w.push(float(e["liq_usd"]), float(e["vol_h1"] or e["vol_m5"]))
        st.watches[e["pool"]] = w
        lr = w.liq_rise_pct()
        vr = w.vol_rise_pct()
        # ilk örnekte geçmiş yok — bir sonraki taramada gir
        if lr is None or vr is None:
            continue
        if lr < MIN_LIQ_RISE_PCT and vr < MIN_VOL_RISE_PCT:
            continue
        # en az biri güçlü olsun
        if lr < MIN_LIQ_RISE_PCT * 0.5 and vr < MIN_VOL_RISE_PCT:
            continue
        scored.append((score_candidate(e, w), e, w))

    scored.sort(key=lambda x: x[0], reverse=True)
    # watch list şişmesin
    if len(st.watches) > 80:
        ranked = sorted(
            st.watches.items(),
            key=lambda kv: (kv[1].liq_hist[-1][0] if kv[1].liq_hist else 0),
            reverse=True,
        )
        keep_pools = {p.pool for p in st.positions.values()}
        fresh: dict[str, PoolWatch] = {}
        for k, v in ranked:
            if k in keep_pools or len(fresh) < 60:
                fresh[k] = v
        st.watches = fresh

    for sc, e, w in scored[:8]:
        log(
            f"sinyal {e['symbol']} score={sc:.1f} liq=${e['liq_usd']:.0f} "
            f"liqΔ={w.liq_rise_pct():.1f}% volΔ={w.vol_rise_pct():.1f}% pool={e['pool'][:10]}…"
        )
        try_buy(kp, st, e)
        if len(st.positions) >= MAX_OPEN:
            break
    st.save()


def main() -> None:
    kp = load_keypair()
    st = State.load()
    log("=" * 60)
    log("Likidite avcısı | Jupiter AL/SAT | pool kontrol + fee tampon")
    log(f"pubkey={kp.pubkey()}")
    log(f"DRY_RUN={DRY_RUN} buy={BUY_SOL}SOL max_open={MAX_OPEN}")
    log(
        f"giriş: liq↑≥{MIN_LIQ_RISE_PCT}% vol↑≥{MIN_VOL_RISE_PCT}% | "
        f"çıkış: peak liq↓{LIQ_DROP_FROM_PEAK_PCT}% fee≈{ROUNDTRIP_FEE_PCT}%"
    )
    log("=" * 60)
    last_scan = 0.0
    while True:
        try:
            manage_positions(kp, st)
            now = time.time()
            if now - last_scan >= SCAN_SEC:
                scan_and_enter(kp, st)
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
