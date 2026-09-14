#!/usr/bin/env python3
"""
DexScreener + Birdeye sermaye/zincir akışı taramalı Binance spot otomatik al-sat botu.

Özellikler:
  - BTC / ETH / SOL ve diğer yavaş majörleri atlar
  - Zincir bazlı gerçek hacim akışı (hangi zincire para gidiyor)
  - Coin bazlı sermaye akışı skoru (hangi coine para akıyor)
  - Holder sayısı artışı (Birdeye ile önceki tarama karşılaştırılır)
  - Coin Binance Spot'ta varsa tüm serbest USDT ile alır
  - Yükseliş sınırsız; zirveden %1 düşüşte satar
  - Alış fiyatından %1 düşerse stop-loss satışı
  - Pozisyon kapandıktan sonra yeni coin arar

Güvenlik:
  Anahtarları koda yazmayın. Ortam değişkenlerinden okunur:
    BINANCE_API_KEY, BINANCE_API_SECRET, BIRDEYE_API_KEY
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import ccxt.pro as ccxtpro

# ==========================================
# AYARLAR (API anahtarları .env / ortamdan)
# ==========================================
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "").strip()
BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY", "").strip()

# Yavaş / majör coinler — tarama dışı
SKIP_SYMBOLS = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "TRX", "TON", "AVAX",
    "DOT", "LINK", "MATIC", "POL", "LTC", "BCH", "ATOM", "NEAR", "APT",
    "SUI", "ARB", "OP", "FIL", "ICP", "HBAR", "XLM", "ETC", "UNI", "AAVE",
    "USDT", "USDC", "FDUSD", "DAI", "TUSD", "BUSD", "WBTC", "WETH", "WSOL",
    "STETH", "CBETH",
}

# Alım / satım kuralları
ENTRY_STOP_LOSS_PCT = 0.01      # alıştan %-1 → sat
TRAILING_DROP_PCT = 0.01        # zirveden %-1 → sat (üst sınır yok)
MIN_USDT_BALANCE = 5.0
STAKE_FRACTION = 0.995          # neredeyse tüm serbest USDT
MIN_BUY_RATIO = 52.0            # alış baskısı eşiği
MIN_PRICE_CHANGE_H1 = -0.5      # aşırı düşenleri ele
MIN_VOLUME_H1_USD = 5_000.0     # zayıf hacim ele
MIN_HOLDER_INCREASE = 1         # en az +1 holder artışı
SCAN_INTERVAL_SEC = 12
POSITION_POLL_SEC = 0.35
DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q=USDT"


class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    RESET = "\033[0m"


@dataclass
class TradeSignal:
    symbol: str
    chain: str
    token_address: str
    price_change_h1: float
    volume_h1: float
    buy_ratio: float
    sell_ratio: float
    holders: int
    holder_delta: int
    chain_flow_usd: float
    coin_flow_score: float


def require_keys() -> None:
    missing = [
        name
        for name, val in (
            ("BINANCE_API_KEY", BINANCE_API_KEY),
            ("BINANCE_API_SECRET", BINANCE_API_SECRET),
            ("BIRDEYE_API_KEY", BIRDEYE_API_KEY),
        )
        if not val
    ]
    if missing:
        raise SystemExit(
            f"Eksik ortam değişkeni: {', '.join(missing)}\n"
            "Örnek: export BINANCE_API_KEY=... BINANCE_API_SECRET=... BIRDEYE_API_KEY=..."
        )


async def get_birdeye_metrics(
    session: aiohttp.ClientSession, address: str, chain: str
) -> tuple[int, float]:
    """Holder sayısı + 1s hacim (USD). chain → Birdeye x-chain header."""
    headers = {
        "X-API-KEY": BIRDEYE_API_KEY,
        "accept": "application/json",
        "x-chain": _birdeye_chain(chain),
    }
    holder_count = 0
    volume_h1 = 0.0

    overview_url = f"https://public-api.birdeye.so/defi/token_overview?address={address}"
    try:
        async with session.get(overview_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                result = data.get("data") or {}
                raw = result.get("v1hUSD")
                if raw is None:
                    v24 = float(result.get("v24hUSD") or 0)
                    volume_h1 = v24 / 24.0
                else:
                    volume_h1 = float(raw or 0)
                # bazı yanıtlarda holder overview içinde gelir
                if result.get("holder"):
                    holder_count = int(result.get("holder") or 0)
    except Exception:
        pass

    if holder_count <= 0:
        holder_url = (
            f"https://public-api.birdeye.so/defi/v3/token/holder-count?address={address}"
        )
        try:
            async with session.get(
                holder_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    holder_count = int((data.get("data") or {}).get("holder") or 0)
        except Exception:
            pass

    return holder_count, volume_h1


def _birdeye_chain(chain_id: str) -> str:
    mapping = {
        "solana": "solana",
        "ethereum": "ethereum",
        "bsc": "bsc",
        "arbitrum": "arbitrum",
        "base": "base",
        "polygon": "polygon",
        "avalanche": "avalanche",
        "optimism": "optimism",
        "sui": "sui",
    }
    return mapping.get(chain_id.lower(), "solana")


def coin_flow_score(
    *,
    volume_h1: float,
    buy_ratio: float,
    price_change_h1: float,
    holder_delta: int,
    chain_share: float,
) -> float:
    """
    Gerçek sermaye akışı skoru:
      hacim * alış baskısı * (holder artışı) * zincir payı * momentum
    """
    buy_factor = max(buy_ratio - 50.0, 0.0) / 50.0  # 0..1
    holder_factor = 1.0 + min(max(holder_delta, 0) / 50.0, 2.0)
    momentum = 1.0 + max(price_change_h1, 0.0) / 100.0
    chain_factor = 0.5 + min(max(chain_share, 0.0), 1.0)
    return float(volume_h1) * (0.4 + buy_factor) * holder_factor * momentum * chain_factor


async def scan_and_find_coin(
    signal_queue: asyncio.Queue,
    binance_symbols: set[str],
    position_busy: asyncio.Event,
) -> None:
    print(f"{Colors.CYAN}[TARAYICI] DexScreener + Birdeye gerçek zincir/coin akışı aktif...{Colors.RESET}")
    # token_address -> son bilinen holder
    holder_memory: dict[str, int] = {}
    # token_address -> son bilinen 1s hacim (akış delta için)
    volume_memory: dict[str, float] = {}

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if position_busy.is_set():
                    await asyncio.sleep(2)
                    continue

                print(
                    f"{Colors.YELLOW}[SERMAYE AKIŞI] Zincir + coin akışı taranıyor "
                    f"(majörler atlanıyor)...{Colors.RESET}"
                )
                async with session.get(
                    DEX_SEARCH_URL, timeout=aiohttp.ClientTimeout(total=12)
                ) as response:
                    if response.status != 200:
                        print(f"{Colors.RED}[BİLGİ] DexScreener HTTP {response.status}{Colors.RESET}")
                        await asyncio.sleep(SCAN_INTERVAL_SEC)
                        continue

                    data = await response.json()
                    pairs = data.get("pairs") or []
                    if not pairs:
                        print(f"{Colors.RED}[BİLGİ] DexScreener boş yanıt{Colors.RESET}")
                        await asyncio.sleep(SCAN_INTERVAL_SEC)
                        continue

                    # 1) Gerçek zincir akışı: 24s hacim dağılımı
                    chain_volumes: dict[str, float] = {}
                    for pair in pairs:
                        ch = (pair.get("chainId") or "unknown").lower()
                        vol = float((pair.get("volume") or {}).get("h24") or 0)
                        chain_volumes[ch] = chain_volumes.get(ch, 0.0) + vol

                    total_flow = sum(chain_volumes.values()) or 1.0
                    dominant_chain = max(chain_volumes, key=chain_volumes.get)
                    print(
                        f"{Colors.CYAN}🌊 [ZİNCİR AKIŞI] En yoğun: {dominant_chain.upper()} "
                        f"| Toplam: ${total_flow:,.0f}{Colors.RESET}"
                    )
                    top_chains = sorted(chain_volumes.items(), key=lambda x: x[1], reverse=True)[:5]
                    for ch, vol in top_chains:
                        pct = (vol / total_flow) * 100
                        print(f"   • {ch.upper():12} ${vol:,.0f}  ({pct:.1f}%)")

                    # 2) Adayları skorla — sadece Binance'te listeli + majör değil
                    candidates: list[TradeSignal] = []

                    for pair in pairs:
                        base_token = pair.get("baseToken") or {}
                        symbol = (base_token.get("symbol") or "").strip()
                        token_address = (base_token.get("address") or "").strip()
                        chain = (pair.get("chainId") or "").lower()
                        if not symbol or not token_address:
                            continue

                        clean = symbol.upper().replace(" ", "")
                        if clean in SKIP_SYMBOLS or clean.endswith("USD"):
                            continue

                        binance_symbol = f"{clean}/USDT"
                        if binance_symbol not in binance_symbols:
                            continue  # Binance Spot'ta yoksa hiç bakma

                        price_change_h1 = float((pair.get("priceChange") or {}).get("h1") or 0)
                        if price_change_h1 < MIN_PRICE_CHANGE_H1:
                            continue

                        txns = (pair.get("txns") or {}).get("h1") or {}
                        buys = int(txns.get("buys") or 0)
                        sells = int(txns.get("sells") or 0)
                        total_txns = buys + sells
                        buy_ratio = (buys / total_txns) * 100 if total_txns > 0 else 50.0
                        sell_ratio = 100.0 - buy_ratio
                        if buy_ratio < MIN_BUY_RATIO:
                            continue

                        dex_vol_h1 = float((pair.get("volume") or {}).get("h1") or 0)
                        holder_count, be_vol = await get_birdeye_metrics(session, token_address, chain)
                        volume_h1 = max(be_vol, dex_vol_h1)

                        prev_holders = holder_memory.get(token_address)
                        holder_delta = 0
                        if prev_holders is not None:
                            holder_delta = holder_count - prev_holders
                        if holder_count > 0:
                            holder_memory[token_address] = holder_count

                        # İlk görmede delta yok → bir sonraki taramada doğrula
                        if prev_holders is None:
                            volume_memory[token_address] = volume_h1
                            continue

                        if holder_delta < MIN_HOLDER_INCREASE:
                            continue

                        prev_vol = volume_memory.get(token_address, 0.0)
                        volume_memory[token_address] = volume_h1
                        # hacim artışı yoksa zayıf aday
                        if volume_h1 < MIN_VOLUME_H1_USD and volume_h1 <= prev_vol:
                            continue

                        chain_share = chain_volumes.get(chain, 0.0) / total_flow
                        score = coin_flow_score(
                            volume_h1=volume_h1,
                            buy_ratio=buy_ratio,
                            price_change_h1=price_change_h1,
                            holder_delta=holder_delta,
                            chain_share=chain_share,
                        )

                        candidates.append(
                            TradeSignal(
                                symbol=binance_symbol,
                                chain=chain,
                                token_address=token_address,
                                price_change_h1=price_change_h1,
                                volume_h1=volume_h1,
                                buy_ratio=buy_ratio,
                                sell_ratio=sell_ratio,
                                holders=holder_count,
                                holder_delta=holder_delta,
                                chain_flow_usd=chain_volumes.get(chain, 0.0),
                                coin_flow_score=score,
                            )
                        )

                    if not candidates:
                        print(
                            f"{Colors.YELLOW}[BİLGİ] Uygun aday yok "
                            f"(Binance listeli + holder artışı + akış).{Colors.RESET}"
                        )
                        await asyncio.sleep(SCAN_INTERVAL_SEC)
                        continue

                    # En yüksek gerçek coin akışı skorunu seç
                    best = max(candidates, key=lambda c: c.coin_flow_score)
                    print(f"\n{Colors.GREEN}{'=' * 54}")
                    print(f"🟢 [AKİŞ YAKALANDI] HEDEF: {best.symbol} ({best.chain.upper()})")
                    print(
                        f"📈 1s Değişim: %{best.price_change_h1:.2f} | "
                        f"1s Hacim: ${best.volume_h1:,.2f}"
                    )
                    print(
                        f"👥 Holder: {best.holders}  (Δ +{best.holder_delta}) | "
                        f"Akış skoru: {best.coin_flow_score:,.0f}"
                    )
                    print(
                        f"🟢 Alış: %{best.buy_ratio:.1f}  |  "
                        f"{Colors.RED}🔴 Satış: %{best.sell_ratio:.1f}{Colors.RESET}"
                    )
                    print(
                        f"🌊 Zincir hacmi ({best.chain}): ${best.chain_flow_usd:,.0f}"
                    )
                    print(f"{Colors.GREEN}{'=' * 54}{Colors.RESET}\n")

                    await signal_queue.put(best)
                    # executor pozisyon açana kadar bekle
                    position_busy.set()

            except Exception as e:
                print(f"{Colors.RED}[TARAYICI HATA] {e}{Colors.RESET}")

            await asyncio.sleep(SCAN_INTERVAL_SEC)


async def manage_open_position(
    exchange: Any,
    symbol: str,
    amount: float,
    entry_price: float,
    position_busy: asyncio.Event,
) -> None:
    """
    Yükseliş sınırsız.
    - Alıştan %-1 → stop-loss sat
    - Zirveden %-1 → trailing sat
    """
    print(
        f"[POZİSYON] {symbol} takip | giriş={entry_price:.8g} | "
        f"SL=%{ENTRY_STOP_LOSS_PCT*100:.0f} | trailing=%{TRAILING_DROP_PCT*100:.0f}"
    )
    peak_price = entry_price
    stop_loss = entry_price * (1.0 - ENTRY_STOP_LOSS_PCT)

    try:
        while True:
            try:
                ticker = await exchange.fetch_ticker(symbol)
                current = float(ticker["last"])

                if current > peak_price:
                    peak_price = current

                # 1) Sabit stop: alış -%1
                if current <= stop_loss:
                    loss_pct = ((current - entry_price) / entry_price) * 100
                    print(
                        f"{Colors.RED}[STOP-LOSS] {symbol} | %{loss_pct:.2f} "
                        f"→ satılıyor...{Colors.RESET}"
                    )
                    order = await exchange.create_market_sell_order(symbol, amount)
                    print(f"{Colors.RED}[SATIŞ] ID: {order.get('id')}{Colors.RESET}")
                    break

                # 2) Trailing: zirveden %1 düşüş (üst sınır yok)
                drawdown = (peak_price - current) / peak_price if peak_price > 0 else 0
                if peak_price > entry_price and drawdown >= TRAILING_DROP_PCT:
                    profit_pct = ((current - entry_price) / entry_price) * 100
                    print(
                        f"{Colors.YELLOW}[TRAILING] {symbol} zirveden -%"
                        f"{drawdown*100:.2f} | Kâr: +%{profit_pct:.2f}{Colors.RESET}"
                    )
                    order = await exchange.create_market_sell_order(symbol, amount)
                    print(f"{Colors.GREEN}[SATIŞ] ID: {order.get('id')}{Colors.RESET}")
                    break

                await asyncio.sleep(POSITION_POLL_SEC)
            except Exception as e:
                print(f"{Colors.YELLOW}[POZİSYON UYARI] {e}{Colors.RESET}")
                await asyncio.sleep(1.0)
    finally:
        position_busy.clear()
        print(f"{Colors.CYAN}[TARAYICI] Pozisyon kapandı → yeni coin aranıyor.{Colors.RESET}")


async def binance_executor(
    signal_queue: asyncio.Queue,
    exchange: Any,
    position_busy: asyncio.Event,
) -> None:
    print("[BINANCE] Otomatik alım motoru hazır (tüm serbest USDT).")

    while True:
        signal: TradeSignal = await signal_queue.get()
        symbol = signal.symbol
        try:
            ticker = await exchange.fetch_ticker(symbol)
            price = float(ticker["last"])
            if price <= 0:
                raise ValueError("geçersiz fiyat")

            balance = await exchange.fetch_balance()
            usdt_free = float((balance.get("free") or {}).get("USDT") or 0)
            if usdt_free < MIN_USDT_BALANCE:
                print(
                    f"{Colors.RED}[YETERSİZ BAKİYE] Serbest USDT: {usdt_free:.4f}{Colors.RESET}"
                )
                position_busy.clear()
                continue

            stake = usdt_free * STAKE_FRACTION
            amount = stake / price

            # lot / precision düzeltmesi
            try:
                amount = float(exchange.amount_to_precision(symbol, amount))
            except Exception:
                pass

            print(
                f"{Colors.GREEN}[ALIM] {symbol} | {stake:.2f} USDT "
                f"(~{amount}) @ {price}{Colors.RESET}"
            )
            buy_order = await exchange.create_market_buy_order(symbol, amount)
            print(f"{Colors.GREEN}[ALIM OK] ID: {buy_order.get('id')}{Colors.RESET}")

            filled = float(buy_order.get("filled") or 0) or amount
            avg = float(buy_order.get("average") or price) or price

            asyncio.create_task(
                manage_open_position(exchange, symbol, filled, avg, position_busy)
            )
        except Exception as e:
            print(
                f"{Colors.YELLOW}[BINANCE ATLANDI] {symbol}: {e}{Colors.RESET}"
            )
            position_busy.clear()
        finally:
            signal_queue.task_done()


async def load_binance_usdt_symbols(exchange: Any) -> set[str]:
    markets = await exchange.load_markets()
    symbols: set[str] = set()
    for sym, meta in markets.items():
        if not meta.get("spot"):
            continue
        if meta.get("quote") != "USDT":
            continue
        if meta.get("active") is False:
            continue
        symbols.add(sym)
    print(f"[BINANCE] Spot USDT parite sayısı: {len(symbols)}")
    return symbols


async def main() -> None:
    require_keys()

    exchange = ccxtpro.binance(
        {
            "apiKey": BINANCE_API_KEY,
            "secret": BINANCE_API_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )

    signal_queue: asyncio.Queue = asyncio.Queue()
    position_busy = asyncio.Event()

    print("=" * 60)
    print(" ZİNCİR AKIŞI + HOLDER Δ + BINANCE SPOT BOT")
    print(
        f" Skip majörler | SL -%{ENTRY_STOP_LOSS_PCT*100:.0f} | "
        f"Trailing -%{TRAILING_DROP_PCT*100:.0f} zirveden | üst sınır yok"
    )
    print("=" * 60)

    try:
        binance_symbols = await load_binance_usdt_symbols(exchange)
        await asyncio.gather(
            scan_and_find_coin(signal_queue, binance_symbols, position_busy),
            binance_executor(signal_queue, exchange, position_busy),
        )
    finally:
        await exchange.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{Colors.RED}[ÇIKIŞ] Bot durduruldu.{Colors.RESET}")
