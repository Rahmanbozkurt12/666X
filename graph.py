#!/usr/bin/env python3
"""
CEX altcoin depolama cüzdanlarını graph analizi ile bulur.

Veri kaynakları:
  - dawsbot/evm-labels (Ethereum CEX adresleri)
  - DataDr69/labeled_ethereum_addresses_dataset (işlem sayıları)

Metrikler:
  - txn_count: toplam işlem (deposit proxy)
  - graph_score: exchange cluster içinde PageRank
  - altcoin_count: Ethplorer üzerinden benzersiz ERC-20 altcoin sayısı (top N)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import networkx as nx
import pandas as pd
import requests

EVM_LABELS_URL = (
    "https://raw.githubusercontent.com/dawsbot/evm-labels/master/src/mainnet/exchange/all.csv"
)
CEX_LABELS_URL = (
    "https://raw.githubusercontent.com/DataDr69/labeled_ethereum_addresses_dataset/main/csv/01_cex_labels.csv"
)
ETHPLORER_BASE = "https://api.ethplorer.io"
STABLECOIN_SYMBOLS = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "FRAX", "USDD", "GUSD", "PYUSD"}

PRIORITY_EXCHANGES = [
    "binance",
    "coinbase",
    "okx",
    "okex",
    "kraken",
    "kucoin",
    "bybit",
    "gateio",
    "huobi",
    "bitfinex",
    "paribu",
    "btcturk",
    "crypto.com",
    "gemini",
    "bitstamp",
]


@dataclass
class CexWallet:
    address: str
    name_tag: str
    exchange: str
    txn_count: int
    eth_balance: float
    graph_score: float = 0.0
    altcoin_count: int = 0
    token_count: int = 0
    wallet_type: str = "unknown"


def fetch_text(url: str, timeout: int = 30) -> str:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def parse_txn_count(value: str | float | int | None) -> int:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    cleaned = str(value).replace(",", "").strip()
    if not cleaned:
        return 0
    try:
        return int(float(cleaned))
    except ValueError:
        return 0


def parse_eth_balance(value: str | float | int | None) -> float:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(" ETH", "").replace(",", "").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def normalize_exchange(label: str, name_tag: str) -> str:
    source = f"{label} {name_tag}".lower()
    for exchange in PRIORITY_EXCHANGES:
        if exchange.replace(".", "") in source.replace(".", ""):
            return exchange
    if "okex" in source:
        return "okx"
    match = re.match(r"^([a-z0-9 .]+?)(?:\s+\d+|\s+hot|\s+cold|\s+reserve|\s+deposit|$)", name_tag.lower())
    if match:
        return match.group(1).strip()
    return label.lower() or "unknown"


def infer_wallet_type(name_tag: str) -> str:
    tag = name_tag.lower()
    if any(k in tag for k in ("cold", "reserve", "staking", "proof of assets")):
        return "cold/reserve"
    if any(k in tag for k in ("deposit", "hot", "peg")):
        return "deposit/hot"
    if re.search(r"\b\d+\b", tag):
        return "hot/deposit"
    return "exchange"


def load_cex_wallets() -> list[CexWallet]:
    labels_raw = fetch_text(EVM_LABELS_URL)
    stats_raw = fetch_text(CEX_LABELS_URL)

    labels_df = pd.read_csv(io.StringIO(labels_raw))
    stats_df = pd.read_csv(io.StringIO(stats_raw))

    stats_df["address_norm"] = stats_df["Address"].str.lower()
    stats_map = {
        row["address_norm"]: {
            "txn_count": parse_txn_count(row.get("Txn Count")),
            "eth_balance": parse_eth_balance(row.get("Balance")),
            "label": str(row.get("Label", "")).lower(),
        }
        for _, row in stats_df.iterrows()
    }

    wallets: list[CexWallet] = []
    seen: set[str] = set()

    for _, row in labels_df.iterrows():
        address = str(row["address"]).lower()
        if address in seen:
            continue
        seen.add(address)

        name_tag = str(row.get("nameTag", "") or "").strip()
        stats = stats_map.get(address, {})
        exchange = normalize_exchange(stats.get("label", ""), name_tag)

        wallets.append(
            CexWallet(
                address=address,
                name_tag=name_tag or exchange.title(),
                exchange=exchange,
                txn_count=stats.get("txn_count", 0),
                eth_balance=stats.get("eth_balance", 0.0),
                wallet_type=infer_wallet_type(name_tag),
            )
        )

    return wallets


def build_exchange_graph(wallets: list[CexWallet]) -> nx.Graph:
    graph = nx.Graph()

    for exchange in sorted({w.exchange for w in wallets}):
        graph.add_node(exchange, node_type="exchange")

    for wallet in wallets:
        graph.add_node(
            wallet.address,
            node_type="wallet",
            name_tag=wallet.name_tag,
            exchange=wallet.exchange,
            txn_count=wallet.txn_count,
            wallet_type=wallet.wallet_type,
        )
        weight = max(wallet.txn_count, 1)
        graph.add_edge(wallet.exchange, wallet.address, weight=weight)

        # Aynı borsanın yüksek hacimli cüzdanları birbirine bağlanır (cluster).
        graph.nodes[wallet.address]["exchange_group"] = wallet.exchange

    # Exchange içi bağlantılar: txn_count benzerliği yüksek cüzdanlar
    by_exchange: dict[str, list[CexWallet]] = {}
    for wallet in wallets:
        by_exchange.setdefault(wallet.exchange, []).append(wallet)

    for exchange, group in by_exchange.items():
        top = sorted(group, key=lambda w: w.txn_count, reverse=True)[:10]
        for i, left in enumerate(top):
            for right in top[i + 1 :]:
                pair_weight = min(left.txn_count, right.txn_count) or 1
                if graph.has_edge(left.address, right.address):
                    graph[left.address][right.address]["weight"] += pair_weight
                else:
                    graph.add_edge(left.address, right.address, weight=pair_weight, kind="intra_exchange")

    return graph


def score_wallets(wallets: list[CexWallet], graph: nx.Graph) -> None:
    try:
        pagerank = nx.pagerank(graph, weight="weight")
    except nx.PowerIterationFailedConvergence:
        pagerank = {node: 1 / graph.number_of_nodes() for node in graph.nodes}

    for wallet in wallets:
        wallet.graph_score = pagerank.get(wallet.address, 0.0)


def fetch_token_stats(address: str, api_key: str = "freekey", retries: int = 3) -> tuple[int, int]:
    url = f"{ETHPLORER_BASE}/getAddressInfo/{address}?apiKey={api_key}"
    for attempt in range(retries):
        try:
            response = requests.get(url, timeout=20)
            if response.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            payload = response.json()
            tokens = payload.get("tokens") or []
            symbols = []
            for token in tokens:
                info = token.get("tokenInfo") or {}
                symbol = str(info.get("symbol") or "").upper()
                if symbol:
                    symbols.append(symbol)
            unique = set(symbols)
            altcoins = {s for s in unique if s not in STABLECOIN_SYMBOLS and s not in {"ETH", "WETH"}}
            return len(unique), len(altcoins)
        except requests.RequestException:
            time.sleep(1.0 * (attempt + 1))
    return 0, 0


def enrich_top_wallets(wallets: list[CexWallet], top_n: int, sleep_s: float = 0.35) -> None:
    candidates = sorted(wallets, key=lambda w: (w.txn_count, w.graph_score), reverse=True)[:top_n]
    for wallet in candidates:
        token_count, altcoin_count = fetch_token_stats(wallet.address)
        wallet.token_count = token_count
        wallet.altcoin_count = altcoin_count
        time.sleep(sleep_s)


def composite_score(wallet: CexWallet) -> float:
    txn_component = min(wallet.txn_count / 1_000_000, 1.0) * 0.45
    graph_component = wallet.graph_score * 100 * 0.20
    altcoin_component = min(wallet.altcoin_count / 100, 1.0) * 0.35
    return txn_component + graph_component + altcoin_component


def wallet_rows(wallets: list[CexWallet]) -> list[dict]:
    rows = []
    for wallet in wallets:
        row = asdict(wallet)
        row["composite_score"] = round(composite_score(wallet), 6)
        row["etherscan"] = f"https://etherscan.io/address/{wallet.address}"
        rows.append(row)
    return rows


def print_report(wallets: list[CexWallet], top: int, exchange_filter: str | None) -> None:
    rows = wallet_rows(wallets)
    if exchange_filter:
        rows = [r for r in rows if exchange_filter.lower() in r["exchange"].lower()]

    rows.sort(key=lambda r: r["composite_score"], reverse=True)

    print("\n=== EN ÇOK KULLANILAN CEX ALTCOIN DEPOLAMA CÜZDANLARI ===\n")
    print(f"Toplam etiketli CEX cüzdanı: {len(wallets)}")
    print(f"Filtre: {exchange_filter or 'tüm borsalar'}")
    print(f"Skor = işlem hacmi + graph merkeziyeti + altcoin çeşitliliği\n")

    header = f"{'#':<3} {'Borsa':<10} {'İşlem':>12} {'Altcoin':>8} {'Skor':>8}  Adres / Etiket"
    print(header)
    print("-" * len(header) + "-" * 40)

    for idx, row in enumerate(rows[:top], start=1):
        print(
            f"{idx:<3} {row['exchange'][:10]:<10} {row['txn_count']:>12,} "
            f"{row['altcoin_count']:>8} {row['composite_score']:>8.4f}  "
            f"{row['address'][:10]}... {row['name_tag'][:28]}"
        )

    print("\n--- Borsa bazında en aktif deposit cüzdanı ---")
    by_exchange: dict[str, dict] = {}
    for row in rows:
        ex = row["exchange"]
        if ex not in by_exchange or row["composite_score"] > by_exchange[ex]["composite_score"]:
            by_exchange[ex] = row

    priority = [ex for ex in PRIORITY_EXCHANGES if ex in by_exchange]
    others = sorted(set(by_exchange) - set(priority))
    for ex in priority + others:
        row = by_exchange[ex]
        print(
            f"  {ex:<12} {row['address']}  "
            f"txn={row['txn_count']:,}  altcoin={row['altcoin_count']}  ({row['name_tag']})"
        )

    if not any(r["exchange"] == "btcturk" for r in rows):
        print("\nNot: BtcTurk için doğrulanmış Ethereum deposit adresi public dataset'te yok.")
        print("     Türkiye için Paribu adresleri mevcut.")


def save_outputs(rows: list[dict], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "cex_wallets_ranked.json"
    csv_path = output_dir / "cex_wallets_ranked.csv"

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=2, ensure_ascii=False)

    fieldnames = [
        "address",
        "name_tag",
        "exchange",
        "txn_count",
        "eth_balance",
        "graph_score",
        "altcoin_count",
        "token_count",
        "wallet_type",
        "composite_score",
        "etherscan",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nKaydedildi: {json_path}")
    print(f"Kaydedildi: {csv_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CEX altcoin depolama cüzdanlarını graph analizi ile bul")
    parser.add_argument("--top", type=int, default=25, help="Raporlanacak cüzdan sayısı")
    parser.add_argument("--exchange", type=str, default=None, help="Borsa filtresi (ör. binance, coinbase)")
    parser.add_argument(
        "--enrich-top",
        type=int,
        default=20,
        help="Ethplorer ile altcoin sayısı sorgulanacak üst cüzdan adedi",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="output",
        help="JSON/CSV çıktı klasörü",
    )
    parser.add_argument("--skip-enrich", action="store_true", help="Ethplorer sorgularını atla")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print("CEX cüzdan etiketleri indiriliyor...")
    wallets = load_cex_wallets()
    print(f"{len(wallets)} cüzdan yüklendi.")

    print("Graph oluşturuluyor...")
    graph = build_exchange_graph(wallets)
    score_wallets(wallets, graph)
    print(f"Graph: {graph.number_of_nodes()} node, {graph.number_of_edges()} edge")

    if not args.skip_enrich and args.enrich_top > 0:
        print(f"Top {args.enrich_top} cüzdan için altcoin çeşitliliği sorgulanıyor (Ethplorer)...")
        enrich_top_wallets(wallets, args.enrich_top)

    rows = wallet_rows(wallets)
    rows.sort(key=lambda r: r["composite_score"], reverse=True)
    save_outputs(rows, Path(args.output_dir))
    print_report(wallets, args.top, args.exchange)


if __name__ == "__main__":
    main()
