#!/usr/bin/env python3
"""
Binance listeli coinlerde gerçek Smart Money cüzdanlarını bulur.

Akış:
  1. Binance GET /api/v3/exchangeInfo → aktif USDT spot semboller
  2. CoinGecko → ETH / BSC / Base / Arbitrum kontrat eşlemesi
  3. Blockscout → son 24/48h yüksek hacimli transferler
  4. CEX / DEX / MEV kara listesi ile gürültü filtresi
  5. Skor = çoklu coin çeşitliliği + on-chain USD hacim
  6. Top 20 gerçek cüzdan → terminal + JSON
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

BINANCE_EXCHANGE_INFO = "https://data-api.binance.vision/api/v3/exchangeInfo"
BINANCE_TICKER_24H = "https://data-api.binance.vision/api/v3/ticker/24hr"
COINGECKO_SEARCH = "https://api.coingecko.com/api/v3/search"
COINGECKO_COIN = "https://api.coingecko.com/api/v3/coins/{}"
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"

BLOCKSCOUT = {
    "ethereum": "https://eth.blockscout.com/api/v2",
    "binance-smart-chain": "https://bsc.blockscout.com/api/v2",
    "base": "https://base.blockscout.com/api/v2",
    "arbitrum-one": "https://arbitrum.blockscout.com/api/v2",
}

PRIORITY_CHAINS = ["ethereum", "binance-smart-chain", "base", "arbitrum-one"]

STABLE_BASES = {
    "USDT", "USDC", "BUSD", "TUSD", "USDP", "FDUSD", "DAI", "EUR", "AEUR",
    "TRY", "BRL", "BIDR", "IDRT", "USTC", "USD1",
}

# Bilinen CEX deposit / hot / cold wallet'lar (genişletilmiş)
CEX_ADDRESSES: set[str] = {
    # Binance
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be",
    "0xd551234ae421e3bcba99a0da6d736074f22192ff",
    "0x564286362092d8e7936f0549571a803b203aaced",
    "0x0681d8db095565fe8a346fa0277bffde9c0edbbf",
    "0xfe9e8709d3215310075d67e3ed32a380ccf451c8",
    "0x4e9ce36e442e55ecd9025b9a6e0d88485d628a67",
    "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8",
    "0xf977814e90da44bfa03b6295a0616a897441acec",
    "0x001866ae5b3de6caa5a51543fd9fb64f524f5478",
    "0x85b931a32a0725be14285b66f1a22178c672d69b",
    "0x708396f17127c42383e3b9014072679b2f60b82f",
    "0xe0f0cfde7ee664943906f17f7f14342e76a5cec7",
    "0x8f22f2063d253846b53609231ed80fa571bc0c8f",
    "0x28c6c06298d514db089934071355e5743bf21d60",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d",
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f",
    "0x9696f59e4d72e237be84ffd425dcad154bf96976",
    "0x4d9ff50ef4da947364bb9650892b2554e7be5e2b",
    "0x4976a4a02f38326660d17bf34b431dc6e2eb2327",
    "0x5a52e96bacdabb82fd05763e25335261b270efcb",
    "0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503",
    "0xb38e8c17e38363af6ebdcb3dae12e0243582891d",
    # Coinbase
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3",
    "0x503828976d22510aad0201ac7ec88293211d23da",
    "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740",
    "0x3cd751e6b0078be393132286c442345e5dc49699",
    "0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511",
    "0xeb2629a2734e272bcc07bda959863f316f4bd4cf",
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43",  # Coinbase Prime
    # OKX
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b",
    "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3",
    "0xa7efae728d2936e78bda97dc267687568dd593f3",
    "0x2c8fbb630289363ac80705a1a61273f76fd5a161",
    "0x5041ed759dd4afc3a72b8192c143f72f4724081a",
    # Kraken / KuCoin / Gate / Huobi / Bybit / Crypto.com
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2",
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0",
    "0x2b5634c42055806a59e9107ed44d43c426e58258",
    "0xa1d8d972560c2f8144af871db508f0b0b10a3fbf",
    "0x0d0707963952f2fba59dd06f2b425ace40b492fe",
    "0xe93381fb4c4f14bda253907b18fad305d799241a",
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40",  # Bybit
    "0x46340b20830761efd32832a74d7169b29feb9758",
    "0x72a53cdbbcc1b9dfa55093336708bacafa7c17c7",  # Crypto.com
    "0xbd8ef191caa1571e8ad4619ae894e07a75de0c35",  # Paribu
    "0xfbb1b73c4f0bda4f67dca266ce6ef42f520fbb98",  # Bittrex
    "0x876eabf441b2ee5b5b0554fd502a8e0600950cfa",  # Bitfinex
}

# DEX router / aggregator / pool factory
DEX_ROUTERS: set[str] = {
    "0x7a250d5630b4cf539739df2c5dacb4c659f2498d",  # Uniswap V2
    "0xe592427a0aece92de3edee1f18e0157c05861564",  # Uniswap V3
    "0x68b3465833fb72a70fdfcfad7f8c9f5c6f2d3e88",  # Uniswap Universal
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap Universal Router
    "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5
    "0x111111125421ca6dc452d289314280a0f8842a65",  # 1inch v6
    "0xdef1c0ded9bec7f1a1670819833240f027b24dff",  # 0x
    "0x10ed43c718714eb63d5aa57b78b547ef880d203c",  # Pancake V2
    "0x1b02da8cb0d097eb8d57a175b88c7d8b47997506",  # Sushi
    "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f",  # Sushi V2
    "0x6352a56caadc4f1e25cd6c75970fa768a3304e64",  # OpenOcean
    "0xba0aa737d1e246fc1575bdef39eb6bf534234965",  # PROM MM/pool
    "0x2f7b8a5412ee05aee7be2f864bfd8c94a8c6f021",
    "0x06fd4ba7973a0d39a91734bbc35bc2bcaa99e3b0",
    "0x00000000000000adc04c56bf30ac9d3c0aaf14dc",  # Seaport
    "0x0000000000000068f116a894984e2db1123eb395",  # Seaport 1.6
}

# Bilinen MEV / builder / sandwich bot'lar
MEV_BOTS: set[str] = {
    "0x000000000000084e9179226c73abb8b7810c4a5e",  # jaredfromsubway
    "0xa69babef1ca67a37ffaf7a485dfff3382056e78c",
    "0x00000000003b3cc22af3ae1eac0440bcee416b40",
    "0x98c3d3183c4b8a650614ad179a1a98be9d8d26c0",
    "0x80a64c6d7f12c47b7c66c5b4e20e72b1073aec3f",
    "0x6b75d8af000000e20b7a7ddf000ba900b4009a80",
    "0x000000000035b5e5ad9019092c665357240f594e",
    "0x000000fee13a103a10d593b9ae06b3e05f2e7e1c",
}

# Protokol / settlement / permit2 (bireysel trader değil)
PROTOCOL_ADDRESSES: set[str] = {
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41",  # CoW Protocol Settlement
    "0x000000000004444c5dc75cb358380d2e3de08a90",  # Uniswap Permit2 related
    "0x000000000022d473030f116ddee9f6b43ac78ba3",  # Permit2
    "0xba12222222228d8ba445958a75a0704d566bf2c8",  # Balancer Vault
    "0x1111111254fb6c44bac0bed2854e76f90643097d",  # 1inch Aggregation Router V4
    "0xdef171fe48cf0115b1d80b88dc2536274f51eff3",  # Paraswap
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5",  # Kyber
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5",
}

ZERO = "0x0000000000000000000000000000000000000000"

# CoinGecko rate-limit fallback: bilinen Binance ERC-20/BEP-20/Base kontratları
KNOWN_CONTRACTS: dict[str, list[tuple[str, str]]] = {
    "PROM": [("ethereum", "0xfc82bb4ba86045af6f327323a46e80412b91b27d")],
    "PORTAL": [("ethereum", "0x1bbe973bef3a977fc51cbed703e8ffdefe001fed")],
    "AMP": [("ethereum", "0xff20817765cb7f73d4bde2e66e067e58d11095c2")],
    "STORJ": [("ethereum", "0xb64ef51c888972c908cfacf59b47c1afbc0ab8ac")],
    "LINK": [("ethereum", "0x514910771af9ca656af840dff83e8264ecf986ca")],
    "UNI": [("ethereum", "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984")],
    "AAVE": [("ethereum", "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9")],
    "PEPE": [("ethereum", "0x6982508145454ce325ddbe47a25d4ec3d2311933")],
    "SHIB": [("ethereum", "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce")],
    "FET": [("ethereum", "0xaea46a60368a7bd060eec7df8cba43b7ef41ad85")],
    "CRV": [("ethereum", "0xd533a949740bb3306d119cc777fa900ba034cd52")],
    "LDO": [("ethereum", "0x5a98fcbea516cf06857215779fd812ca3bef1b32")],
    "ARB": [("arbitrum-one", "0x912ce59144191c1204e64559fe8253a0e49e6548")],
    "OP": [("ethereum", "0x4200000000000000000000000000000000000042")],
    "AERO": [("base", "0x940181a94a35a4569e4529a3cdfb74e38fd98631")],
    "VIRTUAL": [("ethereum", "0x44ff860922b0daf173da273ea789e35d617265e7")],
    "COTI": [("ethereum", "0xddb342249667cf597136f8b851487696aa673acb")],
    "UTK": [("ethereum", "0xdc9ac3c20d29ad9bc547bd1059c6171638908bf4")],
    "MANA": [("ethereum", "0x0f5d2fb29fb7d3cfee444a200298f468908cc942")],
    "SAND": [("ethereum", "0x3845badade8e6dff049820680d1f14bd3903a5d0")],
    "GRT": [("ethereum", "0xc944e90c64b2c07662a292be6244bdf05cda44a7")],
    "IMX": [("ethereum", "0xf57e7e7c23978c3caec3c3548e3d615c346e79ff")],
    "APT": [],  # native
    "SUI": [],
    "SOL": [],
}


@dataclass
class MappedToken:
    symbol: str
    chain: str
    contract: str
    price_usd: float = 0.0
    volume_usd: float = 0.0


@dataclass
class SmartWallet:
    address: str
    coin_count: int
    total_volume_usd: float
    score: float
    coins: dict[str, float] = field(default_factory=dict)
    chains: list[str] = field(default_factory=list)
    tx_count: int = 0


def load_cex_from_csv(path: Path) -> set[str]:
    addresses: set[str] = set()
    if not path.exists():
        return addresses
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        if not line.strip():
            continue
        addr = line.split(",")[0].strip().lower()
        if addr.startswith("0x") and len(addr) == 42:
            addresses.add(addr)
    return addresses


def is_noise_address(address: str, extra_cex: set[str] | None = None) -> bool:
    if not address:
        return True
    addr = address.lower()
    if addr == ZERO:
        return True
    if addr in CEX_ADDRESSES or addr in DEX_ROUTERS or addr in MEV_BOTS:
        return True
    if addr in PROTOCOL_ADDRESSES:
        return True
    if extra_cex and addr in extra_cex:
        return True
    # Contract-like burn / null / vanity system addresses
    if addr.startswith("0x000000000000000000000000000000000000"):
        return True
    if addr.startswith("0x00000000000") and addr.count("0") >= 30:
        return True
    return False


def fetch_binance_usdt_symbols() -> list[str]:
    response = requests.get(BINANCE_EXCHANGE_INFO, timeout=30)
    response.raise_for_status()
    payload = response.json()
    symbols: list[str] = []
    for item in payload.get("symbols", []):
        if item.get("status") != "TRADING":
            continue
        if item.get("quoteAsset") != "USDT":
            continue
        if not item.get("isSpotTradingAllowed", True):
            continue
        base = item.get("baseAsset", "")
        if base in STABLE_BASES:
            continue
        symbols.append(base)
    return sorted(set(symbols))


def fetch_binance_volumes() -> dict[str, float]:
    response = requests.get(BINANCE_TICKER_24H, timeout=30)
    response.raise_for_status()
    volumes: dict[str, float] = {}
    for item in response.json():
        symbol = item.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        volumes[base] = float(item.get("quoteVolume") or 0)
    return volumes


def resolve_contracts(
    symbols: list[str],
    cache_path: Path,
    max_lookup: int = 40,
) -> list[MappedToken]:
    cache: dict = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    mapped: list[MappedToken] = []
    lookups = 0

    for symbol in symbols:
        key = symbol.upper()
        entries: list[tuple[str, str]] = []

        if key in cache:
            entries = [(c["chain"], c["contract"]) for c in cache[key]]
        elif key in KNOWN_CONTRACTS:
            entries = list(KNOWN_CONTRACTS[key])
            cache[key] = [{"chain": c, "contract": a} for c, a in entries]
        elif lookups < max_lookup:
            lookups += 1
            try:
                search = requests.get(COINGECKO_SEARCH, params={"query": symbol.lower()}, timeout=20)
                if search.status_code != 200:
                    time.sleep(1.5)
                    continue
                coins = search.json().get("coins", [])
                coin_id = None
                for coin in coins:
                    if coin.get("symbol", "").upper() == key:
                        coin_id = coin["id"]
                        break
                if not coin_id and coins:
                    coin_id = coins[0]["id"]
                if not coin_id:
                    cache[key] = []
                    continue
                time.sleep(1.3)
                detail = requests.get(
                    COINGECKO_COIN.format(coin_id),
                    params={
                        "localization": "false",
                        "tickers": "false",
                        "community_data": "false",
                        "developer_data": "false",
                    },
                    timeout=20,
                )
                if detail.status_code != 200:
                    continue
                platforms = detail.json().get("detail_platforms") or {}
                for chain in PRIORITY_CHAINS:
                    info = platforms.get(chain) or {}
                    address = (info.get("contract_address") or "").lower()
                    if address.startswith("0x"):
                        entries.append((chain, address))
                cache[key] = [{"chain": c, "contract": a} for c, a in entries]
            except (requests.RequestException, ValueError, KeyError):
                continue

        for chain, contract in entries:
            if chain not in BLOCKSCOUT:
                continue
            mapped.append(MappedToken(symbol=key, chain=chain, contract=contract.lower()))

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    return mapped


def attach_prices(tokens: list[MappedToken], volumes: dict[str, float]) -> None:
    # Prefer Binance last price via ticker (already have volumes); fetch prices separately
    tickers = requests.get(BINANCE_TICKER_24H, timeout=30).json()
    price_map = {
        t["symbol"][:-4]: float(t["lastPrice"])
        for t in tickers
        if t.get("symbol", "").endswith("USDT")
    }
    for token in tokens:
        token.price_usd = price_map.get(token.symbol, 0.0)
        token.volume_usd = volumes.get(token.symbol, 0.0)


def fetch_blockscout_transfers(chain: str, contract: str, hours: int, max_pages: int = 12) -> list[dict]:
    base = BLOCKSCOUT.get(chain)
    if not base:
        return []

    cutoff = int(time.time()) - hours * 3600
    transfers: list[dict] = []
    next_params = None

    for _ in range(max_pages):
        url = f"{base}/tokens/{contract}/transfers"
        try:
            response = requests.get(url, params=next_params or {}, timeout=30)
            if response.status_code != 200:
                break
            payload = response.json()
        except requests.RequestException:
            break

        items = payload.get("items") or []
        if not items:
            break

        stop = False
        for item in items:
            ts_raw = item.get("timestamp")
            if isinstance(ts_raw, str):
                ts = int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp())
            else:
                ts = int(ts_raw or 0)
            if ts < cutoff:
                stop = True
                continue
            transfers.append(item)

        if stop:
            break
        next_params = payload.get("next_page_params")
        if not next_params:
            break
        time.sleep(0.15)

    return transfers


def parse_transfer(item: dict) -> tuple[int, str, str, float]:
    ts_raw = item.get("timestamp")
    if isinstance(ts_raw, str):
        ts = int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp())
    else:
        ts = int(ts_raw or 0)
    from_addr = ((item.get("from") or {}).get("hash") or "").lower()
    to_addr = ((item.get("to") or {}).get("hash") or "").lower()
    total = item.get("total") or {}
    decimals = int(total.get("decimals") or 18)
    value = int(total.get("value") or 0) / (10**decimals)
    return ts, from_addr, to_addr, value


def analyze_smart_money(
    tokens: list[MappedToken],
    hours: int,
    extra_cex: set[str],
    min_transfer_usd: float,
) -> list[SmartWallet]:
    # wallet -> symbol -> usd volume
    volume_by_wallet: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    chains_by_wallet: dict[str, set[str]] = defaultdict(set)
    tx_by_wallet: dict[str, int] = defaultdict(int)

    for token in tokens:
        if token.price_usd <= 0:
            continue
        print(f"  → {token.symbol} @ {token.chain} ({token.contract[:10]}...)")
        transfers = fetch_blockscout_transfers(token.chain, token.contract, hours=hours)
        for item in transfers:
            _, from_addr, to_addr, amount = parse_transfer(item)
            usd = amount * token.price_usd
            if usd < min_transfer_usd:
                continue
            for wallet in (from_addr, to_addr):
                if is_noise_address(wallet, extra_cex):
                    continue
                volume_by_wallet[wallet][token.symbol] += usd
                chains_by_wallet[wallet].add(token.chain)
                tx_by_wallet[wallet] += 1

    wallets: list[SmartWallet] = []
    for address, coins in volume_by_wallet.items():
        coin_count = len(coins)
        total_usd = sum(coins.values())
        if coin_count < 1 or total_usd <= 0:
            continue
        # Ağırlıklı skor: çeşitlilik (log ölçekli) + hacim
        # score = 0.55 * diversity + 0.45 * volume_norm (relative, applied later)
        wallets.append(
            SmartWallet(
                address=address,
                coin_count=coin_count,
                total_volume_usd=round(total_usd, 2),
                score=0.0,
                coins={k: round(v, 2) for k, v in sorted(coins.items(), key=lambda x: -x[1])},
                chains=sorted(chains_by_wallet[address]),
                tx_count=tx_by_wallet[address],
            )
        )

    if not wallets:
        return []

    max_coins = max(w.coin_count for w in wallets) or 1
    max_vol = max(w.total_volume_usd for w in wallets) or 1.0
    for wallet in wallets:
        diversity = wallet.coin_count / max_coins
        volume = wallet.total_volume_usd / max_vol
        # Çoklu coin'e ekstra bonus: 2+ coin ×1.15, 3+ ×1.3
        multi_bonus = 1.0 + min(wallet.coin_count - 1, 4) * 0.15
        wallet.score = round((0.55 * diversity + 0.45 * volume) * multi_bonus, 6)

    wallets.sort(key=lambda w: w.score, reverse=True)
    return wallets


def print_report(wallets: list[SmartWallet], top: int, meta: dict) -> None:
    print("\n=== TOP GERÇEK SMART MONEY CÜZDANLARI ===\n")
    print(f"Binance USDT spot: {meta.get('binance_symbols')}")
    print(f"Analiz edilen kontrat: {meta.get('mapped_contracts')}")
    print(f"Pencere: son {meta.get('hours')} saat")
    print(f"Skor = 0.55×coin_çeşitliliği + 0.45×USD_hacim (+ çoklu coin bonusu)")
    print(f"Kara liste: CEX deposit/hot + DEX router + MEV bot\n")

    header = f"{'#':<3} {'Skor':>8} {'Coin':>4} {'USD Hacim':>14}  Cüzdan"
    print(header)
    print("-" * 90)

    for idx, wallet in enumerate(wallets[:top], start=1):
        coins = ", ".join(f"{s}:${v:,.0f}" for s, v in list(wallet.coins.items())[:5])
        print(
            f"{idx:<3} {wallet.score:>8.4f} {wallet.coin_count:>4} "
            f"${wallet.total_volume_usd:>12,.0f}  {wallet.address}"
        )
        print(f"     Zincir: {', '.join(wallet.chains)} | Coinler: {coins}")
        print(f"     Explorer: https://etherscan.io/address/{wallet.address}")


def save_json(wallets: list[SmartWallet], meta: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "meta": meta,
        "top_wallets": [asdict(w) for w in wallets],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nKaydedildi: {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Binance Smart Money cüzdan bulucu")
    parser.add_argument("--hours", type=int, default=48, help="On-chain bakış penceresi (saat)")
    parser.add_argument("--max-coins", type=int, default=25, help="Hacme göre kaç Binance coin analiz edilsin")
    parser.add_argument("--min-transfer-usd", type=float, default=500.0, help="Minimum transfer USD eşiği")
    parser.add_argument("--top", type=int, default=20, help="Raporlanacak cüzdan sayısı")
    parser.add_argument("--coingecko-lookups", type=int, default=20, help="CoinGecko yeni sorgu limiti")
    parser.add_argument("--output", type=str, default="output/smart_money_wallets.json")
    parser.add_argument("--cache", type=str, default="output/contract_cache.json")
    parser.add_argument("--cex-csv", type=str, default="output/cex_wallets_ranked.csv")
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="Virgülle özel sembol listesi (PROM,LINK,PEPE). Verilirse max-coins yok sayılır.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print("1) Binance exchangeInfo → aktif USDT spot semboller...")
    all_symbols = fetch_binance_usdt_symbols()
    volumes = fetch_binance_volumes()
    print(f"   {len(all_symbols)} USDT spot sembol bulundu.")

    if args.symbols:
        selected = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        ranked = sorted(
            [s for s in all_symbols if volumes.get(s, 0) > 0],
            key=lambda s: volumes.get(s, 0),
            reverse=True,
        )
        selected = ranked[: args.max_coins]

    print(f"2) Kontrat eşleştirme ({len(selected)} coin)...")
    tokens = resolve_contracts(selected, Path(args.cache), max_lookup=args.coingecko_lookups)
    attach_prices(tokens, volumes)
    # Aynı sembol birden fazla zincirde olabilir; düşük hacimli native'sizleri tut
    tokens = [t for t in tokens if t.price_usd > 0]
    print(f"   {len(tokens)} kontrat eşleşti.")

    extra_cex = load_cex_from_csv(Path(args.cex_csv))
    print(f"3) Kara liste: {len(CEX_ADDRESSES)+len(extra_cex)} CEX + {len(DEX_ROUTERS)} DEX + {len(MEV_BOTS)} MEV")

    print(f"4) Son {args.hours} saat on-chain transfer taraması...")
    wallets = analyze_smart_money(
        tokens=tokens,
        hours=args.hours,
        extra_cex=extra_cex,
        min_transfer_usd=args.min_transfer_usd,
    )

    # Sadece "gerçek" smart money: en az 1 coin, CEX filtresi zaten uygulandı
    # Çoklu coin tercih: Top 20 içinde çeşitlilik öncelikli sıralama korunur
    top_wallets = wallets[: args.top]

    meta = {
        "binance_symbols": len(all_symbols),
        "selected_symbols": selected,
        "mapped_contracts": len(tokens),
        "hours": args.hours,
        "min_transfer_usd": args.min_transfer_usd,
        "wallets_scored": len(wallets),
        "noise_filter": {
            "cex_hardcoded": len(CEX_ADDRESSES),
            "cex_from_csv": len(extra_cex),
            "dex_routers": len(DEX_ROUTERS),
            "mev_bots": len(MEV_BOTS),
            "protocols": len(PROTOCOL_ADDRESSES),
        },
    }

    print_report(top_wallets, args.top, meta)
    save_json(top_wallets, meta, Path(args.output))


if __name__ == "__main__":
    main()
