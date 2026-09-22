#!/usr/bin/env python3
"""
Binance Spot — BNB çiftleri · hareketli 20 tarama · hacme göre emir · CANLI

1) API KEY / SECRET yaz  (+ hesabında BNB olsun)
2) pip install ccxt
3) python live_mm.py

Ne yapar:
  • USDT değil — XXX/BNB pair'lerde çalışır (komisyon baskısı daha az)
  • Her 100 sn: en hareketli 20 BNB pair tarar (rotasyon — hep aynı coin değil)
  • En az 5 farklı coin tutmaya çalışır
  • Aldığı coini 10 dk tekrar ALMAZ
  • Emir boyutu pair'in hacmine göre (yüksek hacim → daha büyük pay)
  • Düşüşte AL, yükselişte SAT
  • Ban (-1003/418) → otomatik bekler
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import ccxt

# =============================================================================
BINANCE_API_KEY = "BURAYA_API_KEY"
BINANCE_API_SECRET = "BURAYA_SECRET_KEY"
# =============================================================================

QUOTE = "BNB"
TOP_N = 20                      # her turda taranan hareketli coin
MIN_OPEN_COINS = 5              # en az bu kadar farklı coin tut
SCAN_SEC = 100.0                # tarama süresi
BUY_COOLDOWN_SEC = 600.0        # aldıktan sonra aynı coini tekrar alma (10 dk)
SELL_COOLDOWN_SEC = 60.0        # sattıktan sonra kısa ara

DIP_BPS = 30.0                  # kısa vadeli düşüş → AL
RISE_BPS = 40.0                 # girişe göre yükseliş → SAT
MIN_QUOTE_VOL_BNB = 50.0        # çok ölü BNB pair ele
CANDIDATE_POOL = 60             # rotasyon için geniş havuz
MIN_BNB_FREE = 0.02             # min serbest BNB
FEE_BUFFER = 0.002              # maker+edge için min kâr payı
MAX_DRAWDOWN_BNB = 0.15         # toplam realize DD kill (BNB cinsinden)

BALANCE_CACHE_SEC = 55.0
POST_ONLY = True
MAKER_FEE = 0.001

SKIP_BASES = {
    "BNB", "USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDE", "USD1",
    "EUR", "TRY", "BRL", "AEUR", "BTC", "ETH",  # mega-cap'i zorla alma → daha hareketli mid
}

# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("live_mm")


def clean(s: str) -> str:
    s = (s or "").strip()
    for q in ('"', "'", "\u201c", "\u201d", "\u2018", "\u2019"):
        if len(s) >= 2 and s[0] == q and s[-1] == q:
            s = s[1:-1].strip()
    for ch in (" ", "\t", "\r", "\n", "\u200b", "\ufeff"):
        s = s.replace(ch, "")
    return s


def resolve_keys() -> Tuple[str, str]:
    k = clean(BINANCE_API_KEY) or clean(os.getenv("BINANCE_API_KEY") or "")
    s = clean(BINANCE_API_SECRET) or clean(os.getenv("BINANCE_API_SECRET") or "")
    bad = {"", "BURAYA_API_KEY", "BURAYA_SECRET_KEY", "YOUR_KEY", "YOUR_SECRET"}
    if k in bad or s in bad:
        raise SystemExit(
            "\nAPI KEY BOŞ!\n"
            '  BINANCE_API_KEY = "gerçek_key"\n'
            '  BINANCE_API_SECRET = "gerçek_secret"\n'
        )
    return k, s


def coid() -> str:
    return ("x-BNB20" + uuid.uuid4().hex)[:32]


def ban_until_ms(err: Exception | str) -> Optional[int]:
    msg = str(err)
    m = re.search(r"banned until (\d+)", msg, re.I)
    if m:
        return int(m.group(1))
    if "418" in msg or "-1003" in msg or "DDoSProtection" in msg or "teapot" in msg.lower():
        return int(time.time() * 1000) + 15 * 60 * 1000
    return None


async def sleep_ban(err: Exception | str) -> None:
    until = ban_until_ms(err)
    if not until:
        await asyncio.sleep(30)
        return
    now = int(time.time() * 1000)
    wait = max(5.0, (until - now) / 1000.0 + 5.0)
    human = datetime.fromtimestamp(until / 1000, tz=timezone.utc).strftime("%H:%M:%S UTC")
    log.error("IP BAN — %s kadar bekleniyor (≈%.0f sn)", human, wait)
    end = time.time() + wait
    while time.time() < end:
        left = end - time.time()
        log.info("ban bekleme… %.0f sn kaldı", left)
        await asyncio.sleep(min(30.0, left))


@dataclass
class Pos:
    symbol: str
    entry: float
    qty: float
    bought_at: float


@dataclass
class State:
    positions: Dict[str, Pos] = field(default_factory=dict)
    buy_block_until: Dict[str, float] = field(default_factory=dict)   # AL sonrası 10 dk
    sell_block_until: Dict[str, float] = field(default_factory=dict)
    recently_scanned: List[str] = field(default_factory=list)        # rotasyon hafızası
    realized_bnb: float = 0.0
    kill: bool = False


class Exchange:
    def __init__(self, key: str, secret: str):
        opts = {
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "rateLimit": 350,
            "options": {"defaultType": "spot", "adjustForTimeDifference": True},
        }
        self.rest = ccxt.binance(opts)
        self.rest.set_sandbox_mode(False)
        self._bal: Optional[dict] = None
        self._bal_ts = 0.0
        self._bal_lock = asyncio.Lock()
        self._order_lock = asyncio.Lock()

    async def run(self, fn, *a, **kw):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*a, **kw))

    async def init(self) -> None:
        while True:
            try:
                await self.run(self.rest.load_markets)
                log.info("CANLI Binance | markets=%d | quote=%s", len(self.rest.markets), QUOTE)
                return
            except Exception as e:
                if ban_until_ms(e):
                    await sleep_ban(e)
                    continue
                raise

    async def balance(self, force: bool = False) -> Optional[dict]:
        async with self._bal_lock:
            now = time.time()
            if (not force) and self._bal is not None and (now - self._bal_ts) < BALANCE_CACHE_SEC:
                return self._bal
            try:
                self._bal = await self.run(self.rest.fetch_balance)
                self._bal_ts = time.time()
                return self._bal
            except Exception as e:
                if ban_until_ms(e):
                    await sleep_ban(e)
                    return self._bal
                log.error("balance: %s", e)
                return self._bal

    async def tickers(self) -> Dict[str, dict]:
        try:
            data = await self.run(self.rest.fetch_tickers)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            log.error("tickers: %s", e)
            return {}

    async def book(self, symbol: str) -> Optional[dict]:
        try:
            return await self.run(self.rest.fetch_order_book, symbol, 5)
        except Exception as e:
            if ban_until_ms(e):
                await sleep_ban(e)
            return None

    def free(self, bal: dict, asset: str) -> float:
        return float((bal.get("free") or {}).get(asset, 0) or 0)

    def total(self, bal: dict, asset: str) -> float:
        return float((bal.get("total") or {}).get(asset, 0) or 0)

    def amt(self, symbol: str, x: float) -> float:
        if x <= 0:
            return 0.0
        try:
            return float(self.rest.amount_to_precision(symbol, x))
        except Exception:
            return 0.0

    def px(self, symbol: str, x: float) -> float:
        if x <= 0:
            return 0.0
        try:
            return float(self.rest.price_to_precision(symbol, x))
        except Exception:
            return x

    def limits(self, symbol: str) -> Tuple[float, float]:
        m = self.rest.markets.get(symbol) or {}
        lim = m.get("limits") or {}
        return (
            float((lim.get("amount") or {}).get("min") or 0),
            float((lim.get("cost") or {}).get("min") or 0.001),
        )

    def tick(self, symbol: str) -> float:
        m = self.rest.markets.get(symbol) or {}
        p = (m.get("precision") or {}).get("price")
        if isinstance(p, int):
            return 10 ** (-p)
        if p:
            return float(p)
        return 1e-6

    async def place(self, symbol: str, side: str, amount: float, price: float) -> Optional[dict]:
        amount = self.amt(symbol, amount)
        price = self.px(symbol, price)
        if amount <= 0 or price <= 0:
            return None
        min_qty, min_cost = self.limits(symbol)
        if amount < min_qty or amount * price < min_cost:
            return None

        async with self._order_lock:
            for attempt in range(2):
                try:
                    otype = "LIMIT_MAKER" if POST_ONLY else "limit"
                    params: Dict[str, Any] = {"newClientOrderId": coid()}
                    if attempt == 1 and POST_ONLY:
                        otype = "limit"
                        params["postOnly"] = True
                    o = await self.run(
                        self.rest.create_order, symbol, otype, side, amount, price, params
                    )
                    log.info(
                        "EMİR %s %s qty=%.8f @ %.8f id=%s",
                        side.upper(),
                        symbol,
                        amount,
                        price,
                        o.get("id"),
                    )
                    return o
                except Exception as e:
                    msg = str(e)
                    if ban_until_ms(e):
                        await sleep_ban(e)
                        continue
                    if any(x in msg for x in ("Post Only", "-5022", "would immediately", "Order would")):
                        log.warning("post-only reddedildi %s %s", side, symbol)
                        return None
                    if attempt == 0:
                        continue
                    log.error("order %s %s: %s", side, symbol, e)
                    return None
            return None

    async def cancel_all(self, symbol: str) -> None:
        async with self._order_lock:
            try:
                if hasattr(self.rest, "cancel_all_orders"):
                    await self.run(self.rest.cancel_all_orders, symbol)
                    return
            except Exception as e:
                if ban_until_ms(e):
                    await sleep_ban(e)


def score_ticker(t: dict) -> float:
    """Hareket skoru: |%change| × log(hacim) — hep aynı mega-cap olmasın."""
    try:
        ch = abs(float(t.get("percentage") or 0))
    except Exception:
        ch = 0.0
    qv = float(t.get("quoteVolume") or 0)
    if qv <= 0:
        return 0.0
    # yüzde değişim ağır + hacim log
    return ch * math.log10(qv + 10.0) + (ch ** 1.2) * 0.5


def pick_movers(
    ex: Exchange,
    tickers: Dict[str, dict],
    state: State,
    n: int = TOP_N,
) -> List[Tuple[str, float, float]]:
    """
    Döner: [(symbol, quoteVolumeBNB, last_price), ...]
    Geniş havuzdan skorla, son tarananları cezalandır → rotasyon.
    """
    recent = set(state.recently_scanned[-40:])
    rows: List[Tuple[float, str, float, float]] = []

    for sym, t in tickers.items():
        if not sym.endswith(f"/{QUOTE}"):
            continue
        if ":BNB" in sym or ":USDT" in sym:
            continue
        m = ex.rest.markets.get(sym) or {}
        if m.get("contract") or m.get("spot") is False:
            continue
        base = sym.split("/")[0].upper()
        if base in SKIP_BASES:
            continue
        if m.get("active") is False:
            continue
        qv = float(t.get("quoteVolume") or 0)
        if qv < MIN_QUOTE_VOL_BNB:
            continue
        last = float(t.get("last") or t.get("close") or 0)
        if last <= 0:
            continue
        sc = score_ticker(t)
        if sym in recent:
            sc *= 0.35  # son tarananları düşür → çeşitlilik
        if sc <= 0:
            continue
        rows.append((sc, sym, qv, last))

    rows.sort(key=lambda x: -x[0])
    pool = rows[: max(CANDIDATE_POOL, n)]
    # skor ağırlıklı rastgele örnekle (hep aynı top-20 olmasın)
    if len(pool) > n:
        weights = [max(0.01, r[0]) for r in pool]
        chosen_idx: Set[int] = set()
        out_rows: List[Tuple[float, str, float, float]] = []
        # önce en yüksek 8'i garanti et (gerçekten hareketli)
        for i, r in enumerate(pool[:8]):
            out_rows.append(r)
            chosen_idx.add(i)
        while len(out_rows) < n and len(chosen_idx) < len(pool):
            i = random.choices(range(len(pool)), weights=weights, k=1)[0]
            if i in chosen_idx:
                weights[i] *= 0.5
                continue
            chosen_idx.add(i)
            out_rows.append(pool[i])
        selected = out_rows
    else:
        selected = pool[:n]

    # pozisyonda olanları her zaman ekle (satış kontrolü için)
    have = {r[1] for r in selected}
    for sym in list(state.positions.keys()):
        if sym in have:
            continue
        t = tickers.get(sym) or {}
        qv = float(t.get("quoteVolume") or 1.0)
        last = float(t.get("last") or state.positions[sym].entry or 0)
        selected.append((999.0, sym, qv, last))
        have.add(sym)

    result = [(sym, qv, last) for _sc, sym, qv, last in selected[: max(n, len(state.positions) + n)]]
    # dedupe
    seen: Set[str] = set()
    uniq: List[Tuple[str, float, float]] = []
    for sym, qv, last in result:
        if sym in seen:
            continue
        seen.add(sym)
        uniq.append((sym, qv, last))
    return uniq[: max(TOP_N, MIN_OPEN_COINS)]


def volume_weights(items: List[Tuple[str, float, float]]) -> Dict[str, float]:
    """Hacme göre pay (normalize)."""
    vols = {sym: max(qv, 1.0) for sym, qv, _ in items}
    # log ölçek — tek coin tüm bakiyeyi yemesin
    logs = {s: math.log10(v + 10.0) for s, v in vols.items()}
    tot = sum(logs.values()) or 1.0
    return {s: logs[s] / tot for s in logs}


async def sync_positions(ex: Exchange, state: State, bal: dict) -> None:
    """Cüzdandaki BNB-dışı bakiyeleri pozisyon olarak işle."""
    min_keep = set(state.positions.keys())
    for asset, amt in (bal.get("total") or {}).items():
        a = str(asset).upper()
        if a in SKIP_BASES or a == QUOTE:
            continue
        tot = float(amt or 0)
        if tot <= 0:
            continue
        sym = f"{a}/{QUOTE}"
        if sym not in ex.rest.markets:
            continue
        mid = 0.0
        # ucuz kontrol: mevcut pos entry kullan
        if sym in state.positions and state.positions[sym].entry > 0:
            mid = state.positions[sym].entry
        cost_est = tot * mid if mid > 0 else 0
        min_cost = ex.limits(sym)[1]
        if mid > 0 and cost_est < min_cost * 0.5:
            continue
        if sym not in state.positions:
            state.positions[sym] = Pos(symbol=sym, entry=mid or 0.0, qty=tot, bought_at=time.time())
        else:
            state.positions[sym].qty = tot
        min_keep.add(sym)

    # sıfırlananları düş
    for sym in list(state.positions.keys()):
        base = sym.split("/")[0]
        if ex.total(bal, base) <= 0 and sym not in min_keep:
            state.positions.pop(sym, None)
        elif ex.total(bal, base) <= 0:
            state.positions.pop(sym, None)


async def try_sell(ex: Exchange, state: State, symbol: str, bid: float, ask: float) -> bool:
    pos = state.positions.get(symbol)
    if not pos or pos.entry <= 0 or bid <= 0:
        return False
    now = time.time()
    if now < state.sell_block_until.get(symbol, 0):
        return False

    rise = (bid - pos.entry) / pos.entry * 10000.0
    # fee'yi geçecek yükseliş şart
    need = max(RISE_BPS, (MAKER_FEE * 2 + FEE_BUFFER) * 10000.0)
    if rise < need:
        return False

    tick = ex.tick(symbol)
    price = ex.px(symbol, max(ask, bid + tick))
    if price <= ask:
        price = ex.px(symbol, ask + tick)
    bal = await ex.balance(force=True)
    if not bal:
        return False
    base = symbol.split("/")[0]
    free = ex.free(bal, base)
    qty = ex.amt(symbol, min(free, pos.qty) * 0.98)
    min_qty, min_cost = ex.limits(symbol)
    if qty < min_qty or qty * price < min_cost:
        return False

    await ex.cancel_all(symbol)
    await asyncio.sleep(0.3)
    o = await ex.place(symbol, "sell", qty, price)
    if not o:
        return False

    # realize kaba
    pnl = (price - pos.entry) * qty
    state.realized_bnb += pnl
    state.positions.pop(symbol, None)
    state.sell_block_until[symbol] = now + SELL_COOLDOWN_SEC
    # satış sonrası da 10 dk AL yasak (çift işlem spam olmasın)
    state.buy_block_until[symbol] = now + BUY_COOLDOWN_SEC
    log.info("%s SAT +%.1f bps entry=%.8f → %.8f pnl≈%.5f BNB", symbol, rise, pos.entry, price, pnl)
    return True


async def try_buy(
    ex: Exchange,
    state: State,
    symbol: str,
    bid: float,
    ask: float,
    mid: float,
    bnb_budget: float,
) -> bool:
    now = time.time()
    if symbol in state.positions:
        return False
    if now < state.buy_block_until.get(symbol, 0):
        return False
    if bnb_budget < MIN_BNB_FREE:
        return False
    if mid <= 0 or bid <= 0:
        return False

    # kısa "dip": ask/bid mid'e yakın ve 24h skor zaten hareketli — book'ta bid'e yaslan
    # dip şartı: mid, ask'a göre ucuz (spread içi) + basit: percentage negatif veya ask-bid geniş
    tick = ex.tick(symbol)
    # LIMIT_MAKER alış = bid altında
    price = ex.px(symbol, bid - tick)
    if price <= 0 or price >= bid:
        price = ex.px(symbol, bid - tick)
    if price <= 0:
        return False

    # düşüş filtresi: last mid, recent ask üstünden ucuzsa (spread/2 + DIP)
    # book mid'e göre: (ask - mid)/mid ≈ spread/2; ekstra: bid, mid'den DIP_BPS düşük olsun
    dip = (mid - bid) / mid * 10000.0 if mid > 0 else 0.0
    # ayrıca ticker % değişim negatifse bonus — çağıran dip_ok geçsin
    min_qty, min_cost = ex.limits(symbol)
    qty = ex.amt(symbol, (bnb_budget * 0.95) / price)
    if qty < min_qty or qty * price < max(min_cost, MIN_BNB_FREE * 0.5):
        return False

    await ex.cancel_all(symbol)
    await asyncio.sleep(0.3)
    o = await ex.place(symbol, "buy", qty, price)
    if not o:
        return False

    state.positions[symbol] = Pos(symbol=symbol, entry=price, qty=qty, bought_at=now)
    state.buy_block_until[symbol] = now + BUY_COOLDOWN_SEC
    log.info(
        "%s AL @ %.8f qty=%.6f budget=%.4f BNB | 10dk tekrar AL yok (dip≈%.1f bps)",
        symbol,
        price,
        qty,
        bnb_budget,
        dip,
    )
    return True


def dip_ok(ticker: dict, bid: float, mid: float) -> bool:
    """Düşüşte al: 24h % negatif veya book'ta belirgin dip."""
    try:
        pct = float(ticker.get("percentage") or 0)
    except Exception:
        pct = 0.0
    if mid <= 0:
        return False
    book_dip = (mid - bid) / mid * 10000.0
    if pct <= -0.4:  # gün içi kırmızı
        return True
    if book_dip >= DIP_BPS * 0.15 and pct < 0.2:
        return True
    # güçlü hareketli ama kısa geri çekilme: high-low aralığı
    try:
        hi = float(ticker.get("high") or 0)
        last = float(ticker.get("last") or mid)
        if hi > 0 and (hi - last) / hi * 10000.0 >= DIP_BPS:
            return True
    except Exception:
        pass
    return False


async def scan_once(ex: Exchange, state: State) -> None:
    if state.kill:
        return

    bal = await ex.balance(force=True)
    if not bal:
        return
    await sync_positions(ex, state, bal)

    if state.realized_bnb < -abs(MAX_DRAWDOWN_BNB):
        log.error("KILL — realize DD %.4f BNB", state.realized_bnb)
        state.kill = True
        return

    free_bnb = ex.free(bal, QUOTE)
    open_n = len(state.positions)
    log.info(
        "tarama | BNB free=%.4f | açık coin=%d (min %d) | realize=%.5f BNB",
        free_bnb,
        open_n,
        MIN_OPEN_COINS,
        state.realized_bnb,
    )

    tickers = await ex.tickers()
    if not tickers:
        return

    movers = pick_movers(ex, tickers, state, TOP_N)
    if not movers:
        log.warning("hareketli BNB pair bulunamadı")
        return

    # rotasyon hafızası
    for sym, _, _ in movers:
        state.recently_scanned.append(sym)
    state.recently_scanned = state.recently_scanned[-80:]

    names = [s.replace(f"/{QUOTE}", "") for s, _, _ in movers[:TOP_N]]
    print(f"TOP{TOP_N} hareketli BNB: {', '.join(names)}")

    weights = volume_weights(movers[:TOP_N])

    # 1) önce açık pozisyonlarda SAT kontrol
    for sym, qv, last in movers:
        if sym not in state.positions:
            continue
        ob = await ex.book(sym)
        await asyncio.sleep(0.35)
        if not ob or not (ob.get("bids") and ob.get("asks")):
            continue
        bid = float(ob["bids"][0][0])
        ask = float(ob["asks"][0][0])
        await try_sell(ex, state, sym, bid, ask)

    bal = await ex.balance(force=True)
    if not bal:
        return
    await sync_positions(ex, state, bal)
    free_bnb = ex.free(bal, QUOTE)
    open_n = len(state.positions)

    # 2) en az MIN_OPEN_COINS olacak şekilde AL — hacme göre bütçe
    need = max(0, MIN_OPEN_COINS - open_n)
    # ekstra: free BNB varsa top movers'a da dağıt (max TOP_N slot mantığı)
    slots_left = max(need, 0)
    if free_bnb >= MIN_BNB_FREE * MIN_OPEN_COINS and open_n < MIN_OPEN_COINS:
        slots_left = MIN_OPEN_COINS - open_n
    elif free_bnb >= MIN_BNB_FREE and open_n < TOP_N:
        # min 5 dolduysa da kalan BNB ile hacimli dip'lere gir (max 3 ek / tur)
        slots_left = min(3, TOP_N - open_n) if open_n >= MIN_OPEN_COINS else (MIN_OPEN_COINS - open_n)

    if slots_left <= 0 or free_bnb < MIN_BNB_FREE:
        log.info("AL yok — açık=%d free=%.4f BNB", open_n, free_bnb)
        return

    # hacme göre bütçe: free_bnb'yi aday ağırlıklarıyla böl
    candidates: List[Tuple[str, float, float, dict]] = []
    for sym, qv, last in movers[:TOP_N]:
        if sym in state.positions:
            continue
        if time.time() < state.buy_block_until.get(sym, 0):
            continue
        t = tickers.get(sym) or {}
        candidates.append((sym, qv, last, t))

    # en hareketli + dip olanları öne al
    ranked: List[Tuple[float, str, float, float, dict]] = []
    for sym, qv, last, t in candidates:
        sc = score_ticker(t) * weights.get(sym, 0.01)
        ranked.append((sc, sym, qv, last, t))
    ranked.sort(key=lambda x: -x[0])

    bought = 0
    # min 5 için agresif: dip şartı biraz gevşek tutulur need>0 iken
    for sc, sym, qv, last, t in ranked:
        if bought >= slots_left:
            break
        bal = await ex.balance()
        if not bal:
            break
        free_bnb = ex.free(bal, QUOTE)
        remain_slots = max(1, slots_left - bought)
        # hacim ağırlıklı pay
        w = weights.get(sym, 1.0 / TOP_N)
        # kalan slotlara göre yeniden normalize kabaca
        budget = free_bnb * min(0.45, max(0.08, w * 2.5))
        budget = min(budget, free_bnb / remain_slots)
        if budget < MIN_BNB_FREE:
            continue

        ob = await ex.book(sym)
        await asyncio.sleep(0.4)
        if not ob or not (ob.get("bids") and ob.get("asks")):
            continue
        bid = float(ob["bids"][0][0])
        ask = float(ob["asks"][0][0])
        mid = (bid + ask) / 2.0

        force_fill_min = open_n + bought < MIN_OPEN_COINS
        if not force_fill_min and not dip_ok(t, bid, mid):
            continue
        # min 5 doldururken en az hafif kırmızı veya hareketli olsun
        if force_fill_min:
            pct = float(t.get("percentage") or 0)
            if pct > 3.0:  # aşırı yeşilde kovalama
                continue

        ok = await try_buy(ex, state, sym, bid, ask, mid, budget)
        if ok:
            bought += 1
            open_n += 1
            await asyncio.sleep(0.6)

    log.info("tur bitti | alınan=%d | açık≈%d", bought, open_n)


async def main_async() -> None:
    print("=" * 64)
    print("Binance BNB pairs · hareketli 20 · hacme göre emir · min 5 coin")
    print(f"CCXT {ccxt.__version__}")
    print(f"scan={SCAN_SEC:.0f}s | buy cooldown={BUY_COOLDOWN_SEC/60:.0f}dk | quote={QUOTE}")
    print("=" * 64)

    key, secret = resolve_keys()
    ex = Exchange(key, secret)
    await ex.init()

    # BNB market var mı?
    bnb_pairs = [s for s in ex.rest.markets if s.endswith(f"/{QUOTE}") and ":" not in s]
    print(f"Spot {QUOTE} pair sayısı≈{len(bnb_pairs)}")
    if len(bnb_pairs) < 10:
        await asyncio.sleep(0)
        raise SystemExit("Yeterli BNB pair yok")

    bal = await ex.balance(force=True)
    if not bal:
        raise SystemExit("Bakiye alınamadı / ban")
    free = ex.free(bal, QUOTE)
    print(f"BNB free≈{free:.4f}")
    if free < MIN_BNB_FREE * MIN_OPEN_COINS:
        print(
            f"UYARI: min {MIN_OPEN_COINS} coin için ≈{MIN_BNB_FREE * MIN_OPEN_COINS:.3f} BNB önerilir "
            f"(şimdi {free:.4f})"
        )

    state = State()
    await sync_positions(ex, state, bal)
    print(f"Mevcut açık≈{len(state.positions)}")
    print("Ctrl+C ile dur")
    print("=" * 64)

    while not state.kill:
        try:
            t0 = time.time()
            await scan_once(ex, state)
            elapsed = time.time() - t0
            wait = max(5.0, SCAN_SEC - elapsed)
            log.info("sonraki tarama %.0fs sonra…", wait)
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("loop: %s", e)
            if ban_until_ms(e):
                await sleep_ban(e)
            else:
                await asyncio.sleep(20)

    # kapatırken açık emirleri iptale çalış
    for sym in list(state.positions.keys()):
        try:
            await ex.cancel_all(sym)
        except Exception:
            pass
    print("Kapandı | realize≈%.5f BNB" % state.realized_bnb)


def main() -> None:
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nDurdu")


if __name__ == "__main__":
    main()
