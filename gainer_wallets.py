#!/usr/bin/env python3
"""
Binance günlük yükselen coinlerde pump ONCESI biriktiren cüzdanları bulur.

Akış:
  1. Binance 24h top gainers (data-api.binance.vision)
  2. CoinGecko ile kontrat adresi çözümleme
  3. Blockscout / Ethplorer ile transfer geçmişi
  4. Pump öncesi (T-72h → T-24h) net biriktiren cüzdanları skorla
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

BINANCE_TICKER_URL = "https://data-api.binance.vision/api/v3/ticker/24hr"
COINGECKO_COIN_URL = "https://api.coingecko.com/api/v3/coins/{}"
COINGECKO_SEARCH_URL = "https://api.coingecko.com/api/v3/search"

BLOCKSCOUT = {
    "ethereum": "https://eth.blockscout.com/api/v2",
    "binance-smart-chain": "https://bsc.blockscout.com/api/v2",
    "base": "https://base.blockscout.com/api/v2",
}

STABLE_BASES = {"USDC", "BUSD", "TUSD", "USDP", "FDUSD", "DAI", "EUR", "AEUR", "USDT"}
KNOWN_CEX = {
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be",
    "0x28c6c06298d514db089934071355e5743bf21d60",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d",
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b",
}
KNOWN_DEX = {
    "0x7a250d5630b4cf539739df2c5dacb4c659f2498d",
    "0x68b3465833fb72a70fdfcfad7f8c9f5c6f2d3e88",
    "0x111111125421ca6dc452d289314280a0f8842a65",
    "0xdef1c0ded9bec7f1a1670819833240f027b24dff",
    "0x10ed43c718714eb63d5aa57b78b547ef880d203c",
    "0xba0aa737d1e246fc1575bdef39eb6bf534234965",  # PROM market maker
}


@dataclass
class Gainer:
    symbol: str
    change_pct: float
    quote_volume: float
    last_price: float


@dataclass
class EarlyWallet:
    symbol: str
    address: str
    early_buy: float
    net_position: float
    total_sold: float
    early_buy_usd: float
    score: float
    chain: str
    explorer: str


def get_binance_gainers(min_pct: float = 10.0, min_volume: float = 1_000_000, limit: int = 15) -> list[Gainer]:
    response = requests.get(BINANCE_TICKER_URL, timeout=30)
    response.raise_for_status()
    gainers: list[Gainer] = []
    for item in response.json():
        symbol = item["symbol"]
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        if base in STABLE_BASES:
            continue
        pct = float(item["priceChangePercent"])
        vol = float(item["quoteVolume"])
        if pct >= min_pct and vol >= min_volume:
            gainers.append(
                Gainer(
                    symbol=base,
                    change_pct=pct,
                    quote_volume=vol,
                    last_price=float(item["lastPrice"]),
                )
            )
    gainers.sort(key=lambda g: g.change_pct, reverse=True)
    return gainers[:limit]


KNOWN_CONTRACTS = {
    "PROM": ("ethereum", "0xfc82bb4ba86045af6f327323a46e80412b91b27d"),
    "PORTAL": ("ethereum", "0x1bbe973bef3a977fc51cbed703e8ffdefe001fed"),
    "AMP": ("ethereum", "0xff20817765cb7f73d4bde2e66e067e58d11095c2"),
    "STORJ": ("ethereum", "0xb64ef51c888972c908cfacf59b47c1afbc0ab8ac"),
    "ONG": None,  # native Ontology — CEX içi
    "UTK": ("ethereum", "0xdc9ac3c20d29ad9bc547bd1059c6171638908bf4"),
    "AERO": ("base", "0x940181a94a35a4569e4529a3cdfb74e38fd98631"),
    "VIRTUAL": ("ethereum", "0x44ff860922b0daf173da273ea789e35d617265e7"),
    "COTI": ("ethereum", "0xddb342249667cf597136f8b851487696aa673acb"),
}


def resolve_contract(symbol: str, cache: dict) -> tuple[str, str, str] | None:
    key = symbol.upper()
    if key in cache:
        return cache[key]
    if key in KNOWN_CONTRACTS:
        known = KNOWN_CONTRACTS[key]
        if known is None:
            cache[key] = None
            return None
        result = (known[0], known[1].lower(), key.lower())
        cache[key] = result
        return result

    try:
        search = requests.get(COINGECKO_SEARCH_URL, params={"query": symbol.lower()}, timeout=20)
        search.raise_for_status()
        coins = search.json().get("coins", [])
    except (requests.RequestException, ValueError):
        cache[key] = None
        return None

    if not coins:
        cache[key] = None
        return None

    coin_id = None
    for coin in coins:
        if coin.get("symbol", "").upper() == key:
            coin_id = coin["id"]
            break
    if not coin_id:
        coin_id = coins[0]["id"]

    time.sleep(1.2)
    try:
        detail_resp = requests.get(
            COINGECKO_COIN_URL.format(coin_id),
            params={
                "localization": "false",
                "tickers": "false",
                "community_data": "false",
                "developer_data": "false",
            },
            timeout=20,
        )
        if detail_resp.status_code != 200:
            cache[key] = None
            return None
        detail = detail_resp.json()
    except (requests.RequestException, ValueError):
        cache[key] = None
        return None

    priority_chains = ["ethereum", "binance-smart-chain", "base", "arbitrum-one"]
    detail_platforms = detail.get("detail_platforms", {})
    for chain in priority_chains:
        info = detail_platforms.get(chain) or {}
        address = info.get("contract_address")
        if address:
            result = (chain, address.lower(), coin_id)
            cache[key] = result
            return result

    for chain, info in detail_platforms.items():
        address = (info or {}).get("contract_address")
        if address:
            result = (chain, address.lower(), coin_id)
            cache[key] = result
            return result

    cache[key] = None
    return None


def fetch_blockscout_transfers(chain: str, contract: str, max_pages: int = 8) -> list[dict]:
    base = BLOCKSCOUT.get(chain)
    if not base:
        return []

    transfers: list[dict] = []
    next_params = None
    for _ in range(max_pages):
        url = f"{base}/tokens/{contract}/transfers"
        params = next_params or {}
        response = requests.get(url, params=params, timeout=30)
        if response.status_code != 200:
            break
        payload = response.json()
        transfers.extend(payload.get("items", []))
        next_params = payload.get("next_page_params")
        if not next_params:
            break
        time.sleep(0.25)
    return transfers


def fetch_ethplorer_transfers(contract: str, limit: int = 100) -> list[dict]:
    url = f"https://api.ethplorer.io/getTokenHistory/{contract}"
    response = requests.get(
        url,
        params={"apiKey": "freekey", "limit": limit, "type": "transfer"},
        timeout=30,
    )
    if response.status_code != 200:
        return []
    return response.json().get("operations", [])


def parse_blockscout_transfer(item: dict) -> tuple[int, str, str, float]:
    ts = item.get("timestamp")
    if isinstance(ts, str):
        ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    from_addr = (item.get("from") or {}).get("hash", "").lower()
    to_addr = (item.get("to") or {}).get("hash", "").lower()
    total = item.get("total") or {}
    decimals = int(total.get("decimals") or 18)
    value = int(total.get("value") or 0) / (10**decimals)
    return ts, from_addr, to_addr, value


def parse_ethplorer_transfer(item: dict) -> tuple[int, str, str, float]:
    ts = int(item.get("timestamp") or 0)
    from_addr = str(item.get("from", "")).lower()
    to_addr = str(item.get("to", "")).lower()
    decimals = int((item.get("tokenInfo") or {}).get("decimals") or 18)
    value = int(item.get("value") or 0) / (10**decimals)
    return ts, from_addr, to_addr, value


def is_noise_address(address: str) -> bool:
    if not address or address == "0x0000000000000000000000000000000000000000":
        return True
    return address in KNOWN_CEX or address in KNOWN_DEX


def analyze_early_buyers(
    symbol: str,
    chain: str,
    contract: str,
    price_usd: float,
    lookback_hours: int = 48,
) -> list[EarlyWallet]:
    now = int(time.time())
    lookback_ts = now - lookback_hours * 3600

    raw_transfers: list[tuple[int, str, str, float]] = []
    if chain in BLOCKSCOUT:
        for item in fetch_blockscout_transfers(chain, contract, max_pages=15):
            parsed = parse_blockscout_transfer(item)
            if parsed[0] >= lookback_ts:
                raw_transfers.append(parsed)
    elif chain == "ethereum":
        for item in fetch_ethplorer_transfers(contract, limit=200):
            parsed = parse_ethplorer_transfer(item)
            if parsed[0] >= lookback_ts:
                raw_transfers.append(parsed)

    if not raw_transfers:
        return []

    timestamps = sorted({t[0] for t in raw_transfers})
    # Pump genelde son 24h içinde — erken pencere: transferlerin ilk %40'lık zaman dilimi
    span_start = timestamps[0]
    span_end = timestamps[-1]
    early_cutoff_ts = span_start + (span_end - span_start) * 0.4

    wallet_in = defaultdict(float)
    wallet_out = defaultdict(float)
    wallet_early_in = defaultdict(float)
    wallet_to_binance = defaultdict(float)

    for ts, from_addr, to_addr, amount in raw_transfers:
        if from_addr and not is_noise_address(from_addr):
            wallet_out[from_addr] += amount
        if to_addr and not is_noise_address(to_addr):
            wallet_in[to_addr] += amount
            if ts <= early_cutoff_ts:
                wallet_early_in[to_addr] += amount
        if to_addr in KNOWN_CEX and not is_noise_address(from_addr):
            wallet_to_binance[from_addr] += amount

    results: list[EarlyWallet] = []
    min_early = max(50.0, 200.0 if price_usd < 1 else 20.0)

    for wallet, early_buy in wallet_early_in.items():
        if early_buy < min_early:
            continue
        net = wallet_in[wallet] - wallet_out[wallet]
        sold = wallet_out[wallet]
        if net <= 0 and wallet_to_binance[wallet] <= 0:
            continue

        early_usd = early_buy * price_usd
        hold_ratio = max(net, 0) / max(wallet_in[wallet], 1)
        binance_bonus = 1 + (wallet_to_binance[wallet] / max(early_buy, 1)) * 2
        score = early_buy * (hold_ratio + 0.5) * (1 + early_usd / 5_000) * binance_bonus

        explorer_base = BLOCKSCOUT.get(chain, "https://eth.blockscout.com/api/v2").replace("/api/v2", "")
        results.append(
            EarlyWallet(
                symbol=symbol,
                address=wallet,
                early_buy=round(early_buy, 2),
                net_position=round(max(net, 0), 2),
                total_sold=round(sold + wallet_to_binance[wallet], 2),
                early_buy_usd=round(early_usd, 2),
                score=round(score, 2),
                chain=chain,
                explorer=f"{explorer_base}/address/{wallet}",
            )
        )

    results.sort(key=lambda w: w.score, reverse=True)
    return results


def print_report(gainers: list[Gainer], all_wallets: list[EarlyWallet], top_per_coin: int) -> None:
    print("\n=== BINANCE GÜNLÜK YÜKSELENLER ===\n")
    for idx, g in enumerate(gainers, start=1):
        print(
            f"{idx:>2}. {g.symbol:<8} {g.change_pct:+.2f}%  "
            f"hacim=${g.quote_volume/1e6:.1f}M  fiyat=${g.last_price:.4g}"
        )

    print("\n=== PUMP ÖNCESİ BİRİKTİREN CÜZDANLAR (transfer zamanının ilk %40'ı) ===\n")
    by_symbol: dict[str, list[EarlyWallet]] = defaultdict(list)
    for wallet in all_wallets:
        by_symbol[wallet.symbol].append(wallet)

    for g in gainers:
        wallets = by_symbol.get(g.symbol, [])[:top_per_coin]
        print(f"\n--- {g.symbol} (+{g.change_pct:.1f}%) ---")
        if not wallets:
            print("  On-chain erken birikim bulunamadı (CEX içi alım olabilir)")
            continue
        for idx, w in enumerate(wallets, start=1):
            print(
                f"  {idx}. {w.address}\n"
                f"     Erken alım: {w.early_buy:,.0f} {w.symbol} (~${w.early_buy_usd:,.0f}) | "
                f"Net: {w.net_position:,.0f} | Satış: {w.total_sold:,.0f}\n"
                f"     Chain: {w.chain} | {w.explorer}"
            )

    repeat: dict[str, int] = defaultdict(int)
    for w in all_wallets:
        repeat[w.address] += 1
    multi = [(addr, cnt) for addr, cnt in repeat.items() if cnt >= 2]
    if multi:
        print("\n=== BIRDEN FAZLA GAINER'DA ERKEN GİREN CÜZDANLAR ===")
        for addr, cnt in sorted(multi, key=lambda x: x[1], reverse=True):
            symbols = sorted({w.symbol for w in all_wallets if w.address == addr})
            print(f"  {addr}  → {cnt} coin: {', '.join(symbols)}")


def save_outputs(gainers: list[Gainer], wallets: list[EarlyWallet], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gainers": [asdict(g) for g in gainers],
        "early_wallets": [asdict(w) for w in wallets],
    }
    json_path = output_dir / "gainer_early_wallets.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nKaydedildi: {json_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Binance gainers için pump öncesi cüzdan bulucu")
    parser.add_argument("--min-pct", type=float, default=10.0, help="Minimum 24h yükseliş %%")
    parser.add_argument("--min-volume", type=float, default=1_000_000, help="Minimum USDT hacim")
    parser.add_argument("--limit", type=int, default=8, help="Kaç gainer analiz edilsin")
    parser.add_argument("--top-per-coin", type=int, default=5, help="Coin başına kaç cüzdan")
    parser.add_argument("--output-dir", type=str, default="output")
    parser.add_argument("--symbols", type=str, default=None, help="Virgülle: PROM,PORTAL,AMP")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        gainers = []
        tickers = {t["symbol"]: t for t in requests.get(BINANCE_TICKER_URL, timeout=30).json()}
        for sym in symbols:
            t = tickers.get(f"{sym}USDT")
            if t:
                gainers.append(
                    Gainer(
                        symbol=sym,
                        change_pct=float(t["priceChangePercent"]),
                        quote_volume=float(t["quoteVolume"]),
                        last_price=float(t["lastPrice"]),
                    )
                )
    else:
        print("Binance gainers çekiliyor...")
        gainers = get_binance_gainers(args.min_pct, args.min_volume, args.limit)

    cache: dict = {}
    all_wallets: list[EarlyWallet] = []

    for gainer in gainers:
        print(f"Analiz: {gainer.symbol} (+{gainer.change_pct:.1f}%)...")
        resolved = resolve_contract(gainer.symbol, cache)
        if not resolved:
            print(f"  Kontrat bulunamadı: {gainer.symbol}")
            continue
        chain, contract, _ = resolved
        wallets = analyze_early_buyers(
            gainer.symbol,
            chain,
            contract,
            price_usd=gainer.last_price,
        )
        all_wallets.extend(wallets[: args.top_per_coin])
        time.sleep(0.5)

    all_wallets.sort(key=lambda w: w.score, reverse=True)
    print_report(gainers, all_wallets, args.top_per_coin)
    save_outputs(gainers, all_wallets, Path(args.output_dir))


if __name__ == "__main__":
    main()
