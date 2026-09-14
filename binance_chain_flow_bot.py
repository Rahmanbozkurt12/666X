#!/usr/bin/env python3
"""
Gerçek Zincir Akışı + Holder Artışı → Binance Spot Al-Sat Botu

Kurallar:
  - BTC / ETH / SOL / BNB / USDT gibi yavaş majörler HARİÇ
  - Binance Spot'ta listeli coinleri tara → DexScreener zincir akışı + Birdeye holder
  - Elimizdeki USDT'nin tamamına yakınını kullan (%99.8)
  - Yükseliş sınırsız (sabit take-profit yok)
  - Zirveden %1 geri çekilince sat
  - Giriş fiyatından %1 düşünce stop-loss sat
  - Satıştan sonra yeni coin ara

Güvenlik:
  - API anahtarları ortam değişkeninden okunur (koda yazılmaz)
  - Varsayılan: --dry-run (gerçek emir yok)
  - Withdrawal kapalı API key kullanın

Kullanım:
  export BINANCE_API_KEY=...
  export BINANCE_API_SECRET=...
  export BIRDEYE_API_KEY=...   # opsiyonel ama holder için önerilir
  python3 binance_chain_flow_bot.py --dry-run
  python3 binance_chain_flow_bot.py --live
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from decimal import Decimal
from typing import Any

import aiohttp
import ccxt.async_support as ccxt

# ==========================================
# AYARLAR
# ==========================================

EXCLUDED_SYMBOLS = {
    "BTC", "ETH", "SOL", "BNB", "USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD",
    "XRP", "ADA", "AVAX", "DOGE", "TRX", "DOT", "LINK", "MATIC", "POL", "LTC",
    "BCH", "ATOM", "NEAR", "APT", "SUI", "TON", "WETH", "WBTC", "STETH", "WBNB",
    "WSOL",
}

CHAIN_MAP = {
    "solana": "solana",
    "ethereum": "ethereum",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "base": "base",
    "polygon": "polygon",
    "optimism": "optimism",
    "avalanche": "avalanche",
    "sui": "sui",
}

MIN_PRICE_CHANGE_H1 = 0.5
MIN_BUY_RATIO = 55.0
MIN_VOLUME_H1_USD = 10_000.0
MIN_TXNS_H1 = 5
MIN_HOLDER_GROWTH = 1
MIN_USDT_BALANCE = 5.0
STAKE_FRACTION = 0.998
STOP_LOSS_PCT = 0.01
TRAIL_DRAWDOWN_PCT = 0.01
TRAIL_ARM_PCT = 0.002
SCAN_INTERVAL_SEC = 10
POSITION_POLL_SEC = 0.35
SIGNAL_COOLDOWN_SEC = 20
TRADE_COOLDOWN_SEC = 300

HOLDER_HISTORY: dict[str, int] = {}
RECENTLY_TRADED: dict[str, float] = {}


class RuntimeState:
    """Tarayıcı ↔ executor paylaşımı."""

    def __init__(self) -> None:
        self.position_busy = False


STATE = RuntimeState()


class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    RESET = "\033[0m"


def env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def is_excluded_symbol(symbol: str) -> bool:
    clean = symbol.upper().strip()
    if clean in EXCLUDED_SYMBOLS:
        return True
    if len(clean) > 12 or "-" in clean or "/" in clean:
        return True
    if "ERC20" in clean or "BEP20" in clean:
        return True
    for suffix in ("UP", "DOWN", "BEAR", "BULL", "3L", "3S"):
        if clean.endswith(suffix) and len(clean) > len(suffix):
            return True
    return False


def round_amount(amount: float, precision: int | None = 6) -> float:
    if precision is None:
        return float(Decimal(str(amount)))
    return float(round(Decimal(str(amount)), precision))


def score_signal(sig: dict[str, Any]) -> float:
    return (
        float(sig.get("holder_growth", 0)) * 3.0
        + float(sig.get("buy_ratio", 0)) * 0.5
        + float(sig.get("price_change", 0)) * 1.5
        + min(float(sig.get("volume_h1", 0)) / 50_000.0, 5.0)
    )


async def get_birdeye_metrics(
    session: aiohttp.ClientSession,
    address: str,
    symbol: str,
    chain: str,
    api_key: str | None,
) -> tuple[int, int, float]:
    holder_count = 0
    holder_growth = 0
    volume_h1 = 0.0

    if not api_key:
        return holder_count, holder_growth, volume_h1

    be_chain = CHAIN_MAP.get(chain.lower())
    if not be_chain:
        return holder_count, holder_growth, volume_h1

    headers = {
        "X-API-KEY": api_key,
        "accept": "application/json",
        "x-chain": be_chain,
    }
    timeout = aiohttp.ClientTimeout(total=5)

    overview_url = f"https://public-api.birdeye.so/defi/token_overview?address={address}"
    try:
        async with session.get(overview_url, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                result = data.get("data") or {}
                v1h = result.get("v1hUSD")
                v24h = result.get("v24hUSD")
                if v1h is not None:
                    volume_h1 = float(v1h or 0)
                elif v24h is not None:
                    volume_h1 = float(v24h or 0) / 24.0
    except Exception:
        pass

    holder_url = f"https://public-api.birdeye.so/defi/v3/token/holder-count?address={address}"
    try:
        async with session.get(holder_url, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                holder_count = int((data.get("data") or {}).get("holder", 0) or 0)
                key = f"{be_chain}:{symbol.upper()}"
                if key in HOLDER_HISTORY:
                    holder_growth = holder_count - HOLDER_HISTORY[key]
                else:
                    holder_growth = 0
                HOLDER_HISTORY[key] = holder_count
    except Exception:
        pass

    return holder_count, holder_growth, volume_h1


async def load_binance_usdt_markets(exchange: ccxt.binance) -> set[str]:
    async def from_ccxt() -> set[str]:
        markets = await exchange.load_markets()
        out: set[str] = set()
        for symbol, m in markets.items():
            if m.get("spot") and m.get("quote") == "USDT" and m.get("active", True):
                out.add(symbol)
        return out

    async def from_vision_mirror() -> set[str]:
        url = "https://data-api.binance.vision/api/v3/exchangeInfo"
        out: set[str] = set()
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
        for s in data.get("symbols") or []:
            if s.get("status") != "TRADING":
                continue
            if s.get("quoteAsset") != "USDT":
                continue
            if s.get("isSpotTradingAllowed") is False:
                continue
            base = s.get("baseAsset")
            if base:
                out.add(f"{base}/USDT")
        try:
            exchange.hostname = "data-api.binance.vision"
            await exchange.load_markets()
        except Exception:
            pass
        return out

    try:
        listed = await from_ccxt()
        if listed:
            return listed
    except Exception as e:
        print(
            f"{Colors.YELLOW}[BİNANCE] exchangeInfo engelli/hatalı ({e}) — "
            f"data-api mirror deneniyor...{Colors.RESET}"
        )
    return await from_vision_mirror()


async def fetch_binance_movers(
    session: aiohttp.ClientSession,
    binance_markets: set[str],
) -> list[dict[str, Any]]:
    urls = [
        "https://data-api.binance.vision/api/v3/ticker/24hr",
        "https://api.binance.com/api/v3/ticker/24hr",
    ]
    rows: list[Any] = []
    for url in urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    continue
                rows = await resp.json()
                if isinstance(rows, list) and rows:
                    break
        except Exception:
            continue

    movers: list[dict[str, Any]] = []
    for row in rows:
        sym = row.get("symbol") or ""
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        ccxt_sym = f"{base}/USDT"
        if ccxt_sym not in binance_markets or is_excluded_symbol(base):
            continue
        try:
            change = float(row.get("priceChangePercent") or 0)
            quote_vol = float(row.get("quoteVolume") or 0)
            last = float(row.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        if last <= 0 or quote_vol < 200_000 or change < MIN_PRICE_CHANGE_H1:
            continue
        movers.append(
            {
                "symbol": ccxt_sym,
                "base": base,
                "change_24h": change,
                "quote_volume": quote_vol,
                "last": last,
            }
        )
    movers.sort(key=lambda x: x["change_24h"], reverse=True)
    return movers[:80]


async def dex_lookup_symbol(
    session: aiohttp.ClientSession, base: str
) -> list[dict[str, Any]]:
    url = f"https://api.dexscreener.com/latest/dex/search?q={base}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return list(data.get("pairs") or [])
    except Exception:
        return []


async def scan_and_find_coin(
    signal_queue: asyncio.Queue,
    binance_markets: set[str],
    birdeye_key: str | None,
    stop_event: asyncio.Event,
) -> None:
    print(f"{Colors.CYAN}[TARAYICI] Gerçek Zincir & Holder Akış Motoru Devrede...{Colors.RESET}")

    async with aiohttp.ClientSession() as session:
        while not stop_event.is_set():
            try:
                if STATE.position_busy or not signal_queue.empty():
                    await asyncio.sleep(2)
                    continue

                print(
                    f"{Colors.YELLOW}[PİYASA TARANIYOR] "
                    f"Binance movers + zincir akışı + holder…{Colors.RESET}"
                )

                movers = await fetch_binance_movers(session, binance_markets)
                if not movers:
                    print(f"{Colors.YELLOW}[TARAYICI] Binance mover bulunamadı.{Colors.RESET}")
                    await asyncio.sleep(SCAN_INTERVAL_SEC)
                    continue

                chain_volumes: dict[str, float] = {}
                enriched: list[dict[str, Any]] = []

                for mv in movers[:40]:
                    if time.time() - RECENTLY_TRADED.get(mv["symbol"], 0) < TRADE_COOLDOWN_SEC:
                        continue

                    pairs = await dex_lookup_symbol(session, mv["base"])
                    matched = [
                        p
                        for p in pairs
                        if ((p.get("baseToken") or {}).get("symbol") or "")
                        .upper()
                        .strip()
                        == mv["base"]
                    ]
                    if not matched:
                        continue

                    matched.sort(
                        key=lambda p: float((p.get("volume") or {}).get("h1") or 0),
                        reverse=True,
                    )
                    pair = matched[0]
                    chain = (pair.get("chainId") or "unknown").lower()
                    token_address = (pair.get("baseToken") or {}).get("address")
                    if not token_address:
                        continue

                    vol_h1 = float((pair.get("volume") or {}).get("h1") or 0)
                    chain_volumes[chain] = chain_volumes.get(chain, 0.0) + vol_h1

                    price_change_h1 = float((pair.get("priceChange") or {}).get("h1") or 0)
                    if price_change_h1 == 0:
                        price_change_h1 = float(mv["change_24h"]) / 24.0

                    txns = (pair.get("txns") or {}).get("h1") or {}
                    buys = int(txns.get("buys") or 0)
                    sells = int(txns.get("sells") or 0)
                    total_txns = buys + sells
                    if total_txns < MIN_TXNS_H1:
                        continue

                    buy_ratio = (buys / total_txns) * 100.0
                    sell_ratio = 100.0 - buy_ratio
                    if buy_ratio < MIN_BUY_RATIO or price_change_h1 < MIN_PRICE_CHANGE_H1:
                        continue

                    holder_count, holder_growth, be_vol = await get_birdeye_metrics(
                        session, token_address, mv["base"], chain, birdeye_key
                    )
                    volume_h1 = be_vol if be_vol > 0 else vol_h1
                    if volume_h1 < MIN_VOLUME_H1_USD:
                        continue
                    if birdeye_key and holder_growth < MIN_HOLDER_GROWTH:
                        continue

                    enriched.append(
                        {
                            "symbol": mv["symbol"],
                            "chain": chain,
                            "address": token_address,
                            "price_change": price_change_h1,
                            "volume_h1": volume_h1,
                            "buy_ratio": buy_ratio,
                            "sell_ratio": sell_ratio,
                            "holders": holder_count,
                            "holder_growth": holder_growth,
                            "binance_change_24h": mv["change_24h"],
                            "binance_quote_vol": mv["quote_volume"],
                        }
                    )

                if chain_volumes:
                    ranked_chains = sorted(
                        chain_volumes, key=chain_volumes.get, reverse=True
                    )
                    print(
                        f"{Colors.CYAN}🌊 [ZİNCİR AKIŞI] (Binance eşleşmeli) Sıra: "
                        + " > ".join(
                            f"{c.upper()}(${chain_volumes[c]:,.0f})"
                            for c in ranked_chains[:5]
                        )
                        + f" / 1s{Colors.RESET}"
                    )
                    for sig in enriched:
                        sig["chain_rank"] = (
                            ranked_chains.index(sig["chain"])
                            if sig["chain"] in ranked_chains
                            else 99
                        )
                else:
                    for sig in enriched:
                        sig["chain_rank"] = 50

                if not enriched:
                    print(
                        f"{Colors.YELLOW}[TARAYICI] Filtreleri geçen Binance altcoin yok "
                        f"(alış baskısı / hacim / holder).{Colors.RESET}"
                    )
                else:
                    enriched.sort(
                        key=lambda s: (s.get("chain_rank", 99), -score_signal(s))
                    )
                    best = enriched[0]
                    print(f"\n{Colors.GREEN}==================================================")
                    print(
                        f"🟢 [GERÇEK AKIŞ YAKALANDI] COİN: {best['symbol']} "
                        f"({best['chain'].upper()})"
                    )
                    print(
                        f"📈 1s Değişim: %{best['price_change']:.2f} | "
                        f"1s Hacim: ${best['volume_h1']:,.2f}"
                    )
                    print(
                        f"🏦 Binance 24s: %{best.get('binance_change_24h', 0):.2f} | "
                        f"Vol: ${best.get('binance_quote_vol', 0):,.0f}"
                    )
                    print(
                        f"👥 Holder: {best['holders']} "
                        f"(Artış: +{best['holder_growth']})"
                    )
                    print(
                        f"🟢 Alış Baskısı: %{best['buy_ratio']:.2f}  |  "
                        f"{Colors.RED}🔴 Satış Baskısı: %{best['sell_ratio']:.2f}{Colors.RESET}"
                    )
                    print(
                        f"{Colors.GREEN}=================================================={Colors.RESET}"
                    )
                    await signal_queue.put(best)
                    await asyncio.sleep(SIGNAL_COOLDOWN_SEC)

            except Exception as e:
                print(f"{Colors.RED}[TARAYICI HATA] {e}{Colors.RESET}")

            await asyncio.sleep(SCAN_INTERVAL_SEC)


async def fetch_last_price(exchange: ccxt.binance, symbol: str) -> float:
    try:
        ticker = await exchange.fetch_ticker(symbol)
        last = float(ticker["last"])
        if last > 0:
            return last
    except Exception:
        pass

    compact = symbol.replace("/", "")
    urls = [
        f"https://data-api.binance.vision/api/v3/ticker/price?symbol={compact}",
        f"https://api.binance.com/api/v3/ticker/price?symbol={compact}",
    ]
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in urls:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    return float(data["price"])
            except Exception:
                continue
    raise RuntimeError(f"Fiyat alınamadı: {symbol}")


async def manage_open_position(
    exchange: ccxt.binance,
    symbol: str,
    amount: float,
    entry_price: float,
    dry_run: bool,
) -> str:
    print(
        f"[POZİSYON TAKİBİ] {symbol} | giriş={entry_price:.8g} | "
        f"SL=-%{STOP_LOSS_PCT*100:.1f} | trail=zirve-%{TRAIL_DRAWDOWN_PCT*100:.1f}"
    )
    peak_price = entry_price
    stop_loss_limit = entry_price * (1.0 - STOP_LOSS_PCT)

    while True:
        try:
            current_price = await fetch_last_price(exchange, symbol)

            if current_price > peak_price:
                peak_price = current_price
                print(
                    f"{Colors.CYAN}[YENİ ZİRVE] {symbol} @ {peak_price:.8g} "
                    f"(+{((peak_price / entry_price) - 1) * 100:.2f}%){Colors.RESET}"
                )

            if current_price <= stop_loss_limit:
                loss_pct = ((current_price - entry_price) / entry_price) * 100
                print(
                    f"{Colors.RED}[STOP-LOSS] {symbol} | Zarar: %{loss_pct:.2f} "
                    f"| Satış...{Colors.RESET}"
                )
                if dry_run:
                    print(f"{Colors.YELLOW}[DRY-RUN SATIŞ] {symbol} x {amount}{Colors.RESET}")
                else:
                    sell_order = await exchange.create_market_sell_order(symbol, amount)
                    print(f"{Colors.RED}[SATIŞ OK] ID: {sell_order.get('id')}{Colors.RESET}")
                return "stop"

            drawdown = (peak_price - current_price) / peak_price if peak_price > 0 else 0
            armed = peak_price >= entry_price * (1.0 + TRAIL_ARM_PCT)
            if armed and drawdown >= TRAIL_DRAWDOWN_PCT:
                profit_pct = ((current_price - entry_price) / entry_price) * 100
                print(
                    f"{Colors.YELLOW}[ZİRVEDEN DÖNÜŞ] {symbol} | "
                    f"zirve={peak_price:.8g} → {current_price:.8g} | "
                    f"Kâr: +%{profit_pct:.2f} | Satılıyor...{Colors.RESET}"
                )
                if dry_run:
                    print(f"{Colors.YELLOW}[DRY-RUN SATIŞ] {symbol} x {amount}{Colors.RESET}")
                else:
                    sell_order = await exchange.create_market_sell_order(symbol, amount)
                    print(f"{Colors.GREEN}[KÂR CEBE] ID: {sell_order.get('id')}{Colors.RESET}")
                return "trail"

            await asyncio.sleep(POSITION_POLL_SEC)
        except Exception as e:
            print(f"{Colors.RED}[POZİSYON HATA] {e}{Colors.RESET}")
            await asyncio.sleep(1.0)


async def binance_executor(
    signal_queue: asyncio.Queue,
    exchange: ccxt.binance,
    dry_run: bool,
    stop_event: asyncio.Event,
) -> None:
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"[BİNANCE] %100 Bakiye Alım Motoru Devrede ({mode}).")

    while not stop_event.is_set():
        try:
            signal = await asyncio.wait_for(signal_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        symbol = signal["symbol"]
        STATE.position_busy = True
        try:
            price = await fetch_last_price(exchange, symbol)

            if dry_run:
                try:
                    balance = await exchange.fetch_balance()
                    usdt_free = float(balance["free"].get("USDT") or 0)
                except Exception:
                    usdt_free = 0.0
                if usdt_free < MIN_USDT_BALANCE:
                    usdt_free = 100.0
                    print(
                        f"{Colors.YELLOW}[DRY-RUN] Gerçek USDT yok — "
                        f"sanal bakiye {usdt_free} USDT{Colors.RESET}"
                    )
            else:
                balance = await exchange.fetch_balance()
                usdt_free = float(balance["free"].get("USDT") or 0)

            if usdt_free < MIN_USDT_BALANCE:
                print(f"{Colors.RED}[YETERSİZ BAKİYE] USDT: {usdt_free:.4f}{Colors.RESET}")
                continue

            stake = usdt_free * STAKE_FRACTION
            amount = stake / price
            try:
                amount = float(exchange.amount_to_precision(symbol, amount))
            except Exception:
                amount = round_amount(amount, 6)

            print(
                f"{Colors.GREEN}[TAM BAKİYE ALIM] {symbol} | "
                f"{stake:.2f} USDT @ {price:.8g} ≈ {amount}{Colors.RESET}"
            )

            if dry_run:
                print(
                    f"{Colors.YELLOW}[DRY-RUN ALIM] {symbol} quote≈{stake:.2f} USDT{Colors.RESET}"
                )
                entry = price
                filled_amount = amount
            else:
                buy_order = None
                if hasattr(exchange, "create_market_buy_order_with_cost"):
                    try:
                        buy_order = await exchange.create_market_buy_order_with_cost(
                            symbol, stake
                        )
                    except Exception:
                        buy_order = None
                if buy_order is None:
                    try:
                        buy_order = await exchange.create_order(
                            symbol,
                            "market",
                            "buy",
                            amount,
                            None,
                            {"quoteOrderQty": stake},
                        )
                    except Exception:
                        buy_order = await exchange.create_market_buy_order(symbol, amount)

                print(f"{Colors.GREEN}[ALIM OK] ID: {buy_order.get('id')}{Colors.RESET}")
                filled_amount = float(buy_order.get("filled") or 0)
                entry = float(buy_order.get("average") or buy_order.get("price") or price)
                if filled_amount <= 0:
                    bal = await exchange.fetch_balance()
                    base = symbol.split("/")[0]
                    filled_amount = float(bal["free"].get(base) or amount)
                try:
                    filled_amount = float(
                        exchange.amount_to_precision(symbol, filled_amount)
                    )
                except Exception:
                    pass

            reason = await manage_open_position(
                exchange, symbol, filled_amount, entry, dry_run
            )
            RECENTLY_TRADED[symbol] = time.time()
            print(
                f"{Colors.CYAN}[DÖNGÜ] {symbol} kapandı ({reason}) — "
                f"yeni coin aranıyor...{Colors.RESET}"
            )

        except Exception as e:
            print(f"{Colors.YELLOW}[BİNANCE ATLANDI] {symbol}: {e}{Colors.RESET}")
            RECENTLY_TRADED[symbol] = time.time()
        finally:
            STATE.position_busy = False
            signal_queue.task_done()


async def run(dry_run: bool) -> None:
    api_key = env("BINANCE_API_KEY")
    api_secret = env("BINANCE_API_SECRET")
    birdeye_key = env("BIRDEYE_API_KEY")

    if not dry_run and (not api_key or not api_secret):
        raise SystemExit("LIVE mod için BINANCE_API_KEY ve BINANCE_API_SECRET gerekli.")

    if not birdeye_key:
        print(
            f"{Colors.YELLOW}[UYARI] BIRDEYE_API_KEY yok — "
            f"holder artışı filtresi kapalı, sadece Dex hacmi kullanılır.{Colors.RESET}"
        )

    exchange = ccxt.binance(
        {
            "apiKey": api_key or "",
            "secret": api_secret or "",
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )

    stop_event = asyncio.Event()
    signal_queue: asyncio.Queue = asyncio.Queue(maxsize=1)

    print("=" * 60)
    print(" 🚀 ZİNCİR AKIŞI + HOLDER + BİNANCE %100 BOT")
    print(f" Mod: {'DRY-RUN' if dry_run else 'LIVE'}")
    print(f" Hariç: {', '.join(sorted(EXCLUDED_SYMBOLS)[:8])}…")
    print(f" SL: -%{STOP_LOSS_PCT*100:.0f} | Trail: zirve-%{TRAIL_DRAWDOWN_PCT*100:.0f}")
    print("=" * 60)

    try:
        markets = await load_binance_usdt_markets(exchange)
        print(f"[BİNANCE] {len(markets)} aktif USDT spot çifti yüklendi.")
        await asyncio.gather(
            scan_and_find_coin(signal_queue, markets, birdeye_key, stop_event),
            binance_executor(signal_queue, exchange, dry_run, stop_event),
        )
    except asyncio.CancelledError:
        stop_event.set()
    finally:
        await exchange.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Zincir akışlı Binance spot bot")
    parser.add_argument("--live", action="store_true", help="Gerçek Binance emirleri")
    parser.add_argument("--dry-run", action="store_true", help="Gerçek emir yok (varsayılan)")
    args = parser.parse_args()
    dry_run = not args.live

    try:
        asyncio.run(run(dry_run=dry_run))
    except KeyboardInterrupt:
        print(f"\n{Colors.RED}[ÇIKIŞ] Bot durduruldu.{Colors.RESET}")


if __name__ == "__main__":
    main()
