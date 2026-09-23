#!/usr/bin/env python3
"""
Binance USDT coin → GitHub repo → aktivite anomali skoru → piyasa birleşik rapor.

Akış:
  Binance USDT listesi
    → resmi GitHub reposunu bul (CoinGecko + cache + manual map)
    → son 7 / 30 / 90 günlük geliştirme aktivitesini ölç
    → geçmiş baseline ile karşılaştır
    → olağandışı artışlara puan ver
    → Binance hacim / CVD / order-book ile birleştir
    → JSON + konsol (+ opsiyonel Telegram)

Kullanım:
  export GITHUB_TOKEN=...   # önerilir (rate limit)
  python3 binance_github_dev_scanner.py --top 20
  python3 binance_github_dev_scanner.py --symbols BTC,ETH,SOL
  python3 binance_github_dev_scanner.py --top 40 --min-score 55 --telegram
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
MAP_PATH = ROOT / "config" / "coin_github_map.json"
OUT_DIR = ROOT / "output"
REPORT_PATH = OUT_DIR / "binance_github_dev_report.json"
CACHE_PATH = OUT_DIR / "coin_github_map_cache.json"

BINANCE_BASE = os.environ.get("BINANCE_API_BASE", "https://data-api.binance.vision")
COINGECKO = "https://api.coingecko.com/api/v3"
GITHUB_API = "https://api.github.com"
USER_AGENT = "666X-binance-github-dev-scanner/1.0"

GITHUB_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.I,
)

SKIP_BASES = {
    "USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "USDD", "EUR", "TRY",
    "AEUR", "EURI", "BFUSD", "USD1", "XUSD", "PAXG",
}

# Mega-cap / her zaman aktif projeler — GitHub gürültüsü trade sinyali değildir
MEGA_CAP_BASES = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX",
    "DOT", "LINK", "BCH", "LTC", "SHIB", "SUI", "NEAR", "APT", "PEPE", "WIF",
}

# ACTIONABLE için zorunlu hard gate'ler
TRADE_GATES = {
    "min_repo_confidence": 0.7,
    "min_accel_7": 2.0,          # 7g commit / prior 7g
    "min_commits_7": 3,
    "max_change_pct_24h": 15.0,  # kaçırma — pump peşinde koşma
    "min_change_pct_24h": -6.0,
    "min_cvd_ratio": -0.02,      # hafif sell'e izin; agresif sell veto
    "min_book_imbalance": 0.48,
    "max_days_since_commit": 10,
    "min_quote_volume": 1_500_000,
}


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) and value.strip() else default


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Http:
    def __init__(self, token: str | None = None) -> None:
        self.s = requests.Session()
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if token:
            headers.update(
                {
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Accept": "application/vnd.github+json",
                }
            )
        self.s.headers.update(headers)
        self.gh_remaining: int | None = None

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int = 40,
        retries: int = 2,
    ) -> Any:
        for attempt in range(retries + 1):
            try:
                r = self.s.get(url, params=params, timeout=timeout)
            except requests.RequestException as exc:
                print(f"[http] {exc}", file=sys.stderr)
                time.sleep(0.5 * (attempt + 1))
                continue

            if "api.github.com" in url:
                rem = r.headers.get("X-RateLimit-Remaining")
                if rem and rem.isdigit():
                    self.gh_remaining = int(rem)
                if r.status_code == 403 and "rate limit" in (r.text or "").lower():
                    reset = r.headers.get("X-RateLimit-Reset")
                    wait = 30
                    if reset and reset.isdigit():
                        wait = max(5, int(reset) - int(time.time()))
                    if wait > 90:
                        print(f"[github] rate limit ({wait}s) skip {url}", file=sys.stderr)
                        return None
                    time.sleep(wait)
                    continue

            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            if r.status_code >= 400:
                print(f"[http] {r.status_code} {url}: {r.text[:160]}", file=sys.stderr)
                return None
            try:
                return r.json()
            except ValueError:
                return None
        return None


def resolve_github_token() -> str | None:
    token = env("GITHUB_TOKEN") or env("GH_TOKEN")
    if token:
        return token
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------


def binance_usdt_tickers(http: Http) -> list[dict[str, Any]]:
    data = http.get(f"{BINANCE_BASE}/api/v3/ticker/24hr")
    if not isinstance(data, list):
        raise SystemExit("Binance ticker alınamadı — BINANCE_API_BASE kontrol et")
    out: list[dict[str, Any]] = []
    for row in data:
        sym = str(row.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in SKIP_BASES:
            continue
        if base.endswith(("UP", "DOWN", "BEAR", "BULL")):
            continue
        try:
            qv = float(row.get("quoteVolume") or 0)
            price = float(row.get("lastPrice") or 0)
            chg = float(row.get("priceChangePercent") or 0)
        except (TypeError, ValueError):
            continue
        if qv <= 0 or price <= 0:
            continue
        out.append(
            {
                "symbol": sym,
                "base": base,
                "price": price,
                "change_pct_24h": chg,
                "quote_volume_24h": qv,
                "trades_24h": int(row.get("count") or 0),
            }
        )
    out.sort(key=lambda x: x["quote_volume_24h"], reverse=True)
    return out


def binance_orderbook(http: Http, symbol: str, limit: int = 50) -> dict[str, float]:
    data = http.get(f"{BINANCE_BASE}/api/v3/depth", params={"symbol": symbol, "limit": limit})
    if not isinstance(data, dict):
        return {"bid_qty": 0.0, "ask_qty": 0.0, "imbalance": 0.5}
    bid_qty = sum(float(q) for _, q in (data.get("bids") or []))
    ask_qty = sum(float(q) for _, q in (data.get("asks") or []))
    total = bid_qty + ask_qty
    return {
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "imbalance": safe_div(bid_qty, total, 0.5),
    }


def binance_cvd(http: Http, symbol: str, limit: int = 1000) -> dict[str, float]:
    data = http.get(f"{BINANCE_BASE}/api/v3/trades", params={"symbol": symbol, "limit": limit})
    if not isinstance(data, list) or not data:
        return {"buy_quote": 0.0, "sell_quote": 0.0, "cvd_quote": 0.0, "cvd_ratio": 0.0}
    buy = sell = 0.0
    for t in data:
        try:
            quote = float(t.get("qty") or 0) * float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if t.get("isBuyerMaker"):
            sell += quote
        else:
            buy += quote
    cvd = buy - sell
    tot = buy + sell
    return {
        "buy_quote": buy,
        "sell_quote": sell,
        "cvd_quote": cvd,
        "cvd_ratio": safe_div(cvd, tot, 0.0),
    }


# ---------------------------------------------------------------------------
# Repo mapping
# ---------------------------------------------------------------------------


def parse_github_repo(url: str) -> str | None:
    m = GITHUB_URL_RE.search(url or "")
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    repo = repo.removesuffix(".git")
    if owner.lower() in {"topics", "settings", "orgs", "marketplace", "features", "pricing", "sponsors"}:
        return None
    return f"{owner}/{repo}"


def load_repo_maps() -> tuple[dict[str, Any], dict[str, Any]]:
    manual: dict[str, Any] = {}
    cache: dict[str, Any] = {}
    if MAP_PATH.exists():
        data = load_json(MAP_PATH)
        if isinstance(data, dict):
            raw = data.get("manual")
            if isinstance(raw, dict):
                manual = dict(raw)
            else:
                manual = {
                    k: v
                    for k, v in data.items()
                    if k not in {"manual", "notes"} and isinstance(v, (str, dict))
                }
    if CACHE_PATH.exists():
        data = load_json(CACHE_PATH)
        if isinstance(data, dict):
            raw = data.get("cache")
            if isinstance(raw, dict):
                cache = dict(raw)
            else:
                cache = {
                    k: v
                    for k, v in data.items()
                    if isinstance(v, dict) and "repo" in v
                }
    return manual, cache


def coingecko_symbol_index(http: Http) -> dict[str, str]:
    """symbol.lower() → coingecko id (ilk eşleşme; manual map tercih edilir)."""
    data = http.get(f"{COINGECKO}/coins/list", params={"include_platform": "false"})
    if not isinstance(data, list):
        return {}
    idx: dict[str, str] = {}
    for row in data:
        sym = str(row.get("symbol") or "").lower()
        cid = str(row.get("id") or "")
        if sym and cid and sym not in idx:
            idx[sym] = cid
    return idx


def coingecko_github(http: Http, coin_id: str) -> str | None:
    data = http.get(
        f"{COINGECKO}/coins/{coin_id}",
        params={
            "localization": "false",
            "tickers": "false",
            "market_data": "false",
            "community_data": "false",
            "developer_data": "false",
        },
    )
    if not isinstance(data, dict):
        return None
    links = data.get("links") or {}
    for u in (links.get("repos_url") or {}).get("github") or []:
        parsed = parse_github_repo(str(u))
        if parsed:
            return parsed
    for u in links.get("homepage") or []:
        parsed = parse_github_repo(str(u))
        if parsed:
            return parsed
    return None


def github_search_repo(http: Http, base: str) -> str | None:
    data = http.get(
        f"{GITHUB_API}/search/repositories",
        params={
            "q": f"{base} cryptocurrency OR blockchain in:name,description",
            "sort": "stars",
            "order": "desc",
            "per_page": 5,
        },
    )
    if not isinstance(data, dict):
        return None
    items = data.get("items") or []
    bl = base.lower()
    for item in items:
        full = item.get("full_name") or ""
        name = (item.get("name") or "").lower()
        desc = (item.get("description") or "").lower()
        stars = int(item.get("stargazers_count") or 0)
        if stars < 30:
            continue
        if bl in name or bl in full.lower() or bl in desc:
            return full
    if items and int(items[0].get("stargazers_count") or 0) >= 200:
        return items[0].get("full_name")
    return None


def resolve_repo(
    http: Http,
    base: str,
    *,
    manual: dict[str, Any],
    cache: dict[str, Any],
    cg_index: dict[str, str],
    refresh: bool = False,
) -> dict[str, Any]:
    m = manual.get(base) or manual.get(base.upper())
    if isinstance(m, str) and "/" in m:
        return {"repo": m, "source": "manual", "confidence": 1.0}
    if isinstance(m, dict) and m.get("repo"):
        return {"repo": m["repo"], "source": "manual", "confidence": float(m.get("confidence") or 1.0)}

    if not refresh and base in cache and cache[base].get("repo"):
        return cache[base]

    repo = None
    source = "none"
    conf = 0.0

    cg_id = cg_index.get(base.lower())
    if cg_id:
        repo = coingecko_github(http, cg_id)
        time.sleep(1.15)
        if repo:
            source, conf = "coingecko", 0.9

    if not repo:
        repo = github_search_repo(http, base)
        time.sleep(0.7)
        if repo:
            source, conf = "github_search", 0.55

    result = {
        "repo": repo,
        "source": source,
        "confidence": conf,
        "updated_at": iso(now_utc()),
    }
    cache[base] = result
    return result


# ---------------------------------------------------------------------------
# GitHub activity metrics
# ---------------------------------------------------------------------------


@dataclass
class WindowStats:
    days: int
    commits: int = 0
    commit_authors: int = 0
    additions: int = 0
    deletions: int = 0
    large_commits: int = 0  # |add+del| >= threshold
    releases: int = 0
    commits_per_day: float = 0.0


@dataclass
class RepoActivity:
    repo: str
    exists: bool = False
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    pushed_at: str | None = None
    last_commit_at: str | None = None
    days_since_commit: float | None = None
    contributors_total: int = 0
    windows: dict[str, WindowStats] = field(default_factory=dict)
    # baseline karşılaştırmaları
    commit_accel_7_vs_prior: float | None = None  # 7d rate / prior 7d rate
    commit_accel_7_vs_90avg: float | None = None
    commit_accel_30_vs_prior: float | None = None
    contributor_delta_30: int | None = None
    large_change_ratio_7: float | None = None
    raw_notes: list[str] = field(default_factory=list)


def _empty_windows() -> dict[str, WindowStats]:
    return {str(d): WindowStats(days=d) for d in (7, 30, 90)}


def fetch_commits_since(http: Http, repo: str, since: datetime, per_page: int = 100, max_pages: int = 5) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    out: list[dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        data = http.get(
            f"{GITHUB_API}/repos/{owner}/{name}/commits",
            params={
                "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "per_page": per_page,
                "page": page,
            },
        )
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < per_page:
            break
        page += 1
        time.sleep(0.15)
    return out


def fetch_commit_detail(http: Http, repo: str, sha: str) -> dict[str, Any] | None:
    owner, name = repo.split("/", 1)
    data = http.get(f"{GITHUB_API}/repos/{owner}/{name}/commits/{sha}")
    return data if isinstance(data, dict) else None


def fetch_releases_since(http: Http, repo: str, since: datetime) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    data = http.get(
        f"{GITHUB_API}/repos/{owner}/{name}/releases",
        params={"per_page": 30},
    )
    if not isinstance(data, list):
        return []
    out = []
    for rel in data:
        published = parse_iso(rel.get("published_at") or rel.get("created_at"))
        if published and published >= since:
            out.append(rel)
    return out


def fetch_contributors(http: Http, repo: str) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    data = http.get(
        f"{GITHUB_API}/repos/{owner}/{name}/contributors",
        params={"per_page": 100, "anon": "true"},
    )
    return data if isinstance(data, list) else []


def analyze_repo(http: Http, repo: str, *, detail_commits: int = 25) -> RepoActivity:
    act = RepoActivity(repo=repo, windows=_empty_windows())
    owner, name = repo.split("/", 1)
    meta = http.get(f"{GITHUB_API}/repos/{owner}/{name}")
    if not isinstance(meta, dict):
        act.raw_notes.append("repo_not_found")
        return act

    act.exists = True
    act.stars = int(meta.get("stargazers_count") or 0)
    act.forks = int(meta.get("forks_count") or 0)
    act.open_issues = int(meta.get("open_issues_count") or 0)
    act.pushed_at = meta.get("pushed_at")

    now = now_utc()
    since_90 = now - timedelta(days=90)
    since_60 = now - timedelta(days=60)
    since_30 = now - timedelta(days=30)
    since_14 = now - timedelta(days=14)
    since_7 = now - timedelta(days=7)

    commits = fetch_commits_since(http, repo, since_90, max_pages=6)
    releases = fetch_releases_since(http, repo, since_90)
    contributors = fetch_contributors(http, repo)
    act.contributors_total = len(contributors)

    # author buckets by window
    authors: dict[str, set[str]] = {"7": set(), "30": set(), "90": set(), "prior7": set(), "prior30": set()}
    commit_times: list[datetime] = []

    for c in commits:
        commit = c.get("commit") or {}
        author = ((c.get("author") or {}).get("login")) or ((commit.get("author") or {}).get("name")) or "unknown"
        dt = parse_iso((commit.get("author") or {}).get("date") or (commit.get("committer") or {}).get("date"))
        if not dt:
            continue
        commit_times.append(dt)
        if dt >= since_7:
            act.windows["7"].commits += 1
            authors["7"].add(author)
        if dt >= since_30:
            act.windows["30"].commits += 1
            authors["30"].add(author)
        if dt >= since_90:
            act.windows["90"].commits += 1
            authors["90"].add(author)
        # prior windows for baseline
        if since_14 <= dt < since_7:
            authors["prior7"].add(author)
        if since_60 <= dt < since_30:
            authors["prior30"].add(author)

    for key in ("7", "30", "90"):
        w = act.windows[key]
        w.commit_authors = len(authors[key])
        w.commits_per_day = safe_div(w.commits, w.days)

    # releases per window
    for rel in releases:
        dt = parse_iso(rel.get("published_at") or rel.get("created_at"))
        if not dt:
            continue
        if dt >= since_7:
            act.windows["7"].releases += 1
        if dt >= since_30:
            act.windows["30"].releases += 1
        if dt >= since_90:
            act.windows["90"].releases += 1

    # last commit
    if commit_times:
        last = max(commit_times)
        act.last_commit_at = iso(last)
        act.days_since_commit = (now - last).total_seconds() / 86400.0
    elif act.pushed_at:
        pushed = parse_iso(act.pushed_at)
        if pushed:
            act.last_commit_at = iso(pushed)
            act.days_since_commit = (now - pushed).total_seconds() / 86400.0

    # large code changes — recent commits detail
    recent = sorted(
        [c for c in commits if parse_iso(((c.get("commit") or {}).get("author") or {}).get("date"))],
        key=lambda c: parse_iso(((c.get("commit") or {}).get("author") or {}).get("date")) or since_90,
        reverse=True,
    )[:detail_commits]

    prior7_commits = 0
    prior30_commits = 0
    for c in commits:
        dt = parse_iso(((c.get("commit") or {}).get("author") or {}).get("date"))
        if not dt:
            continue
        if since_14 <= dt < since_7:
            prior7_commits += 1
        if since_60 <= dt < since_30:
            prior30_commits += 1

    large_threshold = 400
    for c in recent:
        sha = c.get("sha")
        if not sha:
            continue
        detail = fetch_commit_detail(http, repo, sha)
        time.sleep(0.12)
        if not detail:
            continue
        stats = detail.get("stats") or {}
        add = int(stats.get("additions") or 0)
        dele = int(stats.get("deletions") or 0)
        dt = parse_iso(((detail.get("commit") or {}).get("author") or {}).get("date"))
        if not dt:
            continue
        touched = add + dele
        for key, since in (("7", since_7), ("30", since_30), ("90", since_90)):
            if dt >= since:
                act.windows[key].additions += add
                act.windows[key].deletions += dele
                if touched >= large_threshold:
                    act.windows[key].large_commits += 1

    w7 = act.windows["7"]
    w30 = act.windows["30"]
    w90 = act.windows["90"]

    act.commit_accel_7_vs_prior = safe_div(w7.commits, max(prior7_commits, 1))
    # 7d rate vs 90d average daily rate
    avg90 = safe_div(w90.commits, 90.0)
    act.commit_accel_7_vs_90avg = safe_div(w7.commits_per_day, max(avg90, 0.01))
    act.commit_accel_30_vs_prior = safe_div(w30.commits, max(prior30_commits, 1))
    act.contributor_delta_30 = len(authors["30"]) - len(authors["prior30"])
    act.large_change_ratio_7 = safe_div(w7.large_commits, max(min(w7.commits, detail_commits), 1))

    return act


# ---------------------------------------------------------------------------
# Scoring + market merge
# ---------------------------------------------------------------------------


@dataclass
class CoinReport:
    symbol: str
    base: str
    repo: str | None
    repo_source: str
    repo_confidence: float
    github_score: float
    market_score: float
    combined_score: float
    tier: str  # ACTIONABLE | WATCH | NOISE
    vetoes: list[str]
    signals: list[str]
    risk: dict[str, Any]
    market: dict[str, Any]
    github: dict[str, Any]


def score_github(act: RepoActivity) -> tuple[float, list[str]]:
    """Sadece anomali puanlanır. 'Her gün commit var' puan değil, kapı koşuludur."""
    if not act.exists:
        return 0.0, ["no_repo"]

    score = 0.0
    signals: list[str] = []

    accel7 = act.commit_accel_7_vs_prior or 0.0
    accel90 = act.commit_accel_7_vs_90avg or 0.0
    accel30 = act.commit_accel_30_vs_prior or 0.0
    w7 = act.windows["7"]
    w30 = act.windows["30"]

    # --- Asıl alpha: hız anomalisi ---
    if accel7 >= 4 and w7.commits >= 4:
        score += 36
        signals.append(f"commit_spike_7d_x{accel7:.1f}")
    elif accel7 >= 3 and w7.commits >= 3:
        score += 28
        signals.append(f"commit_spike_7d_x{accel7:.1f}")
    elif accel7 >= 2 and w7.commits >= 3:
        score += 18
        signals.append(f"commit_up_7d_x{accel7:.1f}")

    if accel90 >= 3 and w7.commits >= 3:
        score += 16
        signals.append(f"vs_90d_avg_x{accel90:.1f}")
    elif accel90 >= 2 and w7.commits >= 3:
        score += 8
        signals.append(f"vs_90d_avg_x{accel90:.1f}")

    if accel30 >= 2.5 and w30.commits >= 8:
        score += 10
        signals.append(f"commit_spike_30d_x{accel30:.1f}")

    # Release anomali (sessiz repodan ani release)
    if w7.releases >= 1 and (act.windows["90"].releases <= 2 or accel7 >= 1.5):
        score += 14
        signals.append(f"release_7d_{w7.releases}")
    elif w30.releases >= 3 and act.windows["90"].releases <= w30.releases:
        score += 6
        signals.append(f"releases_30d_{w30.releases}")

    # Yeni contributor + hız birlikte anlamlı
    if (act.contributor_delta_30 or 0) >= 3 and accel7 >= 1.8:
        score += 10
        signals.append(f"new_contributors_+{act.contributor_delta_30}")

    # Büyük diff ancak spike ile
    if accel7 >= 1.8 and (act.large_change_ratio_7 or 0) >= 0.35 and w7.large_commits >= 2:
        score += 12
        signals.append("large_code_changes_7d")
    elif accel7 >= 1.8 and (w7.additions + w7.deletions) >= 3000:
        score += 6
        signals.append("heavy_diff_7d")

    # Stale cezası
    dsc = act.days_since_commit
    if dsc is not None and dsc > 45:
        score -= 15
        signals.append("stale_repo")

    # Mutlak aktivite tek başına puan DEĞİL (BTC/ETH gürültüsü buradan geliyordu)
    return clamp(score), signals


def score_market(mkt: dict[str, Any]) -> tuple[float, list[str]]:
    score = 0.0
    signals: list[str] = []
    chg = float(mkt.get("change_pct_24h") or 0)
    vol = float(mkt.get("quote_volume_24h") or 0)
    cvd_r = float((mkt.get("cvd") or {}).get("cvd_ratio") or 0)
    imb = float((mkt.get("orderbook") or {}).get("imbalance") or 0.5)

    if 2_000_000 <= vol < 80_000_000:
        score += 8
        signals.append("tradable_liquidity")
    elif vol >= 80_000_000:
        score += 3
        signals.append("very_high_volume")
    elif vol < 1_000_000:
        score -= 10
        signals.append("thin_liquidity")

    # Erken trend — uzamış pump'ı ödüllendirme
    if 0 <= chg <= 8:
        score += 14
        signals.append("pre_pump_zone")
    elif 8 < chg <= 15:
        score += 6
        signals.append("early_uptrend")
    elif 15 < chg <= 25:
        score -= 4
        signals.append("late_momentum")
    elif chg > 25:
        score -= 18
        signals.append("extended_pump_no_chase")
    elif chg < -10:
        score -= 10
        signals.append("dumping")

    if cvd_r >= 0.12:
        score += 16
        signals.append(f"cvd_buy_{cvd_r:.2f}")
    elif cvd_r >= 0.04:
        score += 8
        signals.append(f"cvd_mild_buy_{cvd_r:.2f}")
    elif cvd_r <= -0.12:
        score -= 16
        signals.append(f"cvd_sell_{cvd_r:.2f}")

    if imb >= 0.60:
        score += 12
        signals.append(f"book_bid_{imb:.2f}")
    elif imb <= 0.40:
        score -= 12
        signals.append(f"book_ask_{imb:.2f}")

    return clamp(score), signals


def combine_scores(gh: float, mkt: float) -> float:
    # Anomali yoksa market tek başına trade açtırmasın
    if gh < 18:
        return clamp(0.25 * gh + 0.20 * mkt)
    return clamp(0.58 * gh + 0.42 * mkt)


def classify_tier(
    *,
    base: str,
    repo_confidence: float,
    gh_score: float,
    mkt_score: float,
    combined: float,
    gh_signals: list[str],
    market: dict[str, Any],
    github: dict[str, Any],
    allow_mega: bool,
) -> tuple[str, list[str]]:
    """ACTIONABLE = trade adayı. WATCH = izle. NOISE = işlem yok."""
    vetoes: list[str] = []
    g = TRADE_GATES
    chg = float(market.get("change_pct_24h") or 0)
    vol = float(market.get("quote_volume_24h") or 0)
    cvd_r = float((market.get("cvd") or {}).get("cvd_ratio") or 0)
    imb = float((market.get("orderbook") or {}).get("imbalance") or 0.5)
    accel7 = float(github.get("commit_accel_7_vs_prior") or 0)
    commits7 = int(((github.get("windows") or {}).get("7") or {}).get("commits") or 0)
    dsc = github.get("days_since_commit")
    has_spike = any(
        s.startswith("commit_spike_7d") or s.startswith("commit_up_7d") or s.startswith("vs_90d_avg")
        or s.startswith("release_7d")
        for s in gh_signals
    )

    if not allow_mega and base in MEGA_CAP_BASES:
        vetoes.append("mega_cap_noise")
    if repo_confidence < g["min_repo_confidence"]:
        vetoes.append("low_repo_confidence")
    if not github.get("exists"):
        vetoes.append("no_repo")
    if not has_spike or accel7 < g["min_accel_7"] or commits7 < g["min_commits_7"]:
        # release_7d tek başına spike sayılabilir
        if not any(s.startswith("release_7d") for s in gh_signals):
            vetoes.append("no_dev_anomaly")
        elif commits7 < 1 and accel7 < 1.2:
            vetoes.append("weak_release_only")
    if chg > g["max_change_pct_24h"]:
        vetoes.append("already_pumped")
    if chg < g["min_change_pct_24h"]:
        vetoes.append("dumping")
    if cvd_r < g["min_cvd_ratio"]:
        vetoes.append("cvd_sell_pressure")
    if imb < g["min_book_imbalance"]:
        vetoes.append("ask_heavy_book")
    if dsc is not None and float(dsc) > g["max_days_since_commit"]:
        vetoes.append("stale_dev")
    if vol < g["min_quote_volume"]:
        vetoes.append("thin_volume")
    if gh_score < 18:
        vetoes.append("weak_github_score")

    if not vetoes and combined >= 45 and gh_score >= 22 and mkt_score >= 20:
        return "ACTIONABLE", vetoes
    if not vetoes and combined >= 35:
        return "WATCH", vetoes
    if has_spike and "mega_cap_noise" not in vetoes and combined >= 30:
        return "WATCH", vetoes
    return "NOISE", vetoes


def build_risk_hint(http: Http, symbol: str, market: dict[str, Any]) -> dict[str, Any]:
    """Son 24x1h mumdan basit stop / invalidation önerisi (otomatik emir yok)."""
    price = float(market.get("price") or 0)
    data = http.get(
        f"{BINANCE_BASE}/api/v3/klines",
        params={"symbol": symbol, "interval": "1h", "limit": 36},
    )
    if not isinstance(data, list) or len(data) < 10 or price <= 0:
        return {
            "entry_zone": price,
            "invalidation": None,
            "risk_pct": None,
            "note": "klines_unavailable — max risk %1-2 hesabın, chase etme",
        }
    lows = [float(k[3]) for k in data]
    highs = [float(k[2]) for k in data]
    recent_low = min(lows[-12:])
    recent_high = max(highs[-12:])
    invalidation = recent_low * 0.992
    risk_pct = safe_div(price - invalidation, price) * 100.0
    if risk_pct <= 0 or risk_pct > 12:
        invalidation = price * 0.97
        risk_pct = 3.0
    return {
        "entry_zone": round(price, 8),
        "invalidation": round(invalidation, 8),
        "risk_pct": round(risk_pct, 2),
        "range_high_12h": round(recent_high, 8),
        "range_low_12h": round(recent_low, 8),
        "position_hint": "risk_per_trade_max_1pct_equity",
        "note": "Sinyal ≠ emir. Stop yoksa işleme girme.",
    }


def windows_to_dict(windows: dict[str, WindowStats]) -> dict[str, Any]:
    return {k: asdict(v) for k, v in windows.items()}


def telegram_send(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30,
        )
        return r.status_code == 200
    except requests.RequestException as exc:
        print(f"[telegram] {exc}", file=sys.stderr)
        return False


def format_top_telegram(rows: list[CoinReport], limit: int = 8) -> str:
    actionable = [r for r in rows if r.tier == "ACTIONABLE"]
    watch = [r for r in rows if r.tier == "WATCH"]
    lines = ["<b>Binance × GitHub — TRADE FILTER</b>"]
    if not actionable:
        lines.append("ACTIONABLE yok — işlem açma.")
    for i, r in enumerate(actionable[:limit], 1):
        sig = ", ".join(r.signals[:4]) or "—"
        risk = r.risk or {}
        lines.append(
            f"{i}. <b>{r.symbol}</b> [{r.tier}] score={r.combined_score:.0f}\n"
            f"   gh={r.github_score:.0f} mkt={r.market_score:.0f}\n"
            f"   stop≈{risk.get('invalidation')} risk≈{risk.get('risk_pct')}%\n"
            f"   {sig}"
        )
    if watch and not actionable:
        lines.append("\nWATCH (işlem yok):")
        for r in watch[:5]:
            lines.append(f"• {r.symbol} {r.combined_score:.0f} — {', '.join(r.signals[:2])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def select_tickers(
    tickers: list[dict[str, Any]],
    *,
    top: int | None,
    symbols: list[str] | None,
    rank_from: int = 1,
    rank_to: int | None = None,
    exclude_mega: bool = True,
) -> list[dict[str, Any]]:
    pool = list(tickers)
    if exclude_mega:
        pool = [t for t in pool if t["base"] not in MEGA_CAP_BASES]

    if symbols:
        want = {s.upper().replace("/", "") for s in symbols}
        want = {s if s.endswith("USDT") else f"{s}USDT" for s in want}
        picked = [t for t in tickers if t["symbol"] in want]  # explicit symbols keep mega if asked
        missing = want - {t["symbol"] for t in picked}
        if missing:
            print(f"[warn] bulunamayan semboller: {sorted(missing)}", file=sys.stderr)
        return picked

    start = max(0, (rank_from or 1) - 1)
    end = rank_to if rank_to is not None else (start + (top or 40))
    if top and rank_to is None:
        end = start + top
    return pool[start:end]


def run_scan(args: argparse.Namespace) -> dict[str, Any]:
    token = resolve_github_token()
    if not token:
        print("[warn] GITHUB_TOKEN yok — rate limit düşük", file=sys.stderr)
    else:
        print("[info] GitHub token aktif", file=sys.stderr)

    http = Http(token)
    tickers = binance_usdt_tickers(http)
    selected = select_tickers(
        tickers,
        top=args.top,
        symbols=args.symbols,
        rank_from=args.rank_from,
        rank_to=args.rank_to,
        exclude_mega=not args.include_mega,
    )
    print(
        f"[scan] Binance USDT={len(tickers)} seçilen={len(selected)} "
        f"mode={args.mode} mega={'on' if args.include_mega else 'off'}",
        file=sys.stderr,
    )

    manual, cache = load_repo_maps()
    cg_index: dict[str, str] = {}
    if not args.skip_coingecko:
        print("[scan] CoinGecko index yükleniyor…", file=sys.stderr)
        cg_index = coingecko_symbol_index(http)
        print(f"[scan] coingecko symbols={len(cg_index)}", file=sys.stderr)

    reports: list[CoinReport] = []

    for i, t in enumerate(selected, 1):
        base = t["base"]
        symbol = t["symbol"]
        print(f"[{i}/{len(selected)}] {symbol} …", file=sys.stderr)

        mapping = resolve_repo(
            http,
            base,
            manual=manual,
            cache=cache,
            cg_index=cg_index,
            refresh=args.refresh_map,
        )
        repo = mapping.get("repo")
        repo_conf = float(mapping.get("confidence") or 0)

        ob = binance_orderbook(http, symbol)
        cvd = binance_cvd(http, symbol)
        market = {**t, "orderbook": ob, "cvd": cvd}
        mkt_score, mkt_signals = score_market(market)

        if not repo:
            gh_score, gh_signals = 0.0, ["repo_unresolved"]
            gh_payload: dict[str, Any] = {"repo": None, "exists": False}
        else:
            act = analyze_repo(http, repo, detail_commits=args.detail_commits)
            gh_score, gh_signals = score_github(act)
            gh_payload = {
                "repo": act.repo,
                "exists": act.exists,
                "stars": act.stars,
                "forks": act.forks,
                "open_issues": act.open_issues,
                "pushed_at": act.pushed_at,
                "last_commit_at": act.last_commit_at,
                "days_since_commit": act.days_since_commit,
                "contributors_total": act.contributors_total,
                "windows": windows_to_dict(act.windows),
                "commit_accel_7_vs_prior": act.commit_accel_7_vs_prior,
                "commit_accel_7_vs_90avg": act.commit_accel_7_vs_90avg,
                "commit_accel_30_vs_prior": act.commit_accel_30_vs_prior,
                "contributor_delta_30": act.contributor_delta_30,
                "large_change_ratio_7": act.large_change_ratio_7,
                "notes": act.raw_notes,
            }

        combined = combine_scores(gh_score, mkt_score)
        signals = gh_signals + mkt_signals
        tier, vetoes = classify_tier(
            base=base,
            repo_confidence=repo_conf,
            gh_score=gh_score,
            mkt_score=mkt_score,
            combined=combined,
            gh_signals=gh_signals,
            market=market,
            github=gh_payload,
            allow_mega=bool(args.include_mega),
        )
        risk = build_risk_hint(http, symbol, market) if tier in {"ACTIONABLE", "WATCH"} else {
            "note": "tier_noise — risk hesabı yok",
        }

        reports.append(
            CoinReport(
                symbol=symbol,
                base=base,
                repo=repo,
                repo_source=str(mapping.get("source") or "none"),
                repo_confidence=repo_conf,
                github_score=gh_score,
                market_score=mkt_score,
                combined_score=combined,
                tier=tier,
                vetoes=vetoes,
                signals=signals,
                risk=risk,
                market=market,
                github=gh_payload,
            )
        )
        time.sleep(0.2)

    save_repo_cache_safe(cache)
    reports.sort(key=lambda r: (r.tier != "ACTIONABLE", r.tier != "WATCH", -r.combined_score))

    actionable = [r for r in reports if r.tier == "ACTIONABLE"]
    watch = [r for r in reports if r.tier == "WATCH"]
    if args.mode == "trade":
        display = actionable
        filtered = actionable
    elif args.mode == "watch":
        display = actionable + watch
        filtered = display
    else:
        display = reports
        filtered = [r for r in reports if r.combined_score >= args.min_score or r.tier != "NOISE"]

    payload = {
        "generated_at": iso(now_utc()),
        "binance_base": BINANCE_BASE,
        "warning": (
            "Bu araç otomatik alım satım botu DEĞİLDİR. ACTIONABLE dışı sinyallerle işlem "
            "açmak zarar üretir. Mega-cap GitHub aktivitesi normal gürültüdür."
        ),
        "params": {
            "top": args.top,
            "symbols": args.symbols,
            "min_score": args.min_score,
            "detail_commits": args.detail_commits,
            "mode": args.mode,
            "rank_from": args.rank_from,
            "rank_to": args.rank_to,
            "include_mega": args.include_mega,
        },
        "gates": TRADE_GATES,
        "github_rate_remaining": http.gh_remaining,
        "scanned": len(reports),
        "actionable": len(actionable),
        "watch": len(watch),
        "hits": len(filtered),
        "results": [asdict(r) for r in reports],
        "trade_candidates": [asdict(r) for r in actionable],
        "watchlist": [asdict(r) for r in watch],
    }
    out_path = Path(args.output) if args.output else REPORT_PATH
    save_json(out_path, payload)

    print_console(display if args.mode != "research" else reports, args.min_score, args.mode)
    if args.mode == "trade" and watch and not actionable:
        print("\n--- WATCH (ALMA, sadece izle) ---")
        for r in watch[:8]:
            print(f"  {r.symbol} comb={r.combined_score:.0f} gh={r.github_score:.0f} | {', '.join(r.signals[:3])}")
    if args.mode == "trade" and not actionable:
        # En sık veto — kullanıcı neden boş döndüğünü anlasın
        c = Counter(v for r in reports for v in r.vetoes)
        if c:
            print("\n--- En sık veto ---")
            for k, n in c.most_common(6):
                print(f"  {k}: {n}")
    print(
        f"\n[report] {out_path} | ACTIONABLE={len(actionable)} WATCH={len(watch)} "
        f"NOISE={len(reports) - len(actionable) - len(watch)}",
        file=sys.stderr,
    )
    if args.mode == "trade" and not actionable:
        print(
            "[guard] ACTIONABLE yok — bugün işlem açma. Eski skor tablosunu trade sinyali sanma.",
            file=sys.stderr,
        )

    if args.telegram:
        tg = env("TELEGRAM_BOT_TOKEN")
        chat = env("TELEGRAM_CHAT_ID")
        if tg and chat:
            telegram_send(tg, chat, format_top_telegram(reports))
        else:
            print("[warn] TELEGRAM env yok", file=sys.stderr)

    return payload


def save_repo_cache_safe(cache: dict[str, Any]) -> None:
    save_json(CACHE_PATH, {"updated_at": iso(now_utc()), "cache": cache})


def print_console(reports: list[CoinReport], min_score: float, mode: str) -> None:
    print("\n=== Binance × GitHub (trade-safe filter) ===")
    print(f"mode={mode} | ACTIONABLE dışında ALIM YOK")
    print(f"{'#':<3} {'TIER':<11} {'SYMBOL':<12} {'COMB':>5} {'GH':>5} {'MKT':>5} {'REPO':<28} SIGNALS/VETO")
    for i, r in enumerate(reports[:40], 1):
        repo = (r.repo or "—")[:28]
        if r.tier == "NOISE":
            extra = "veto:" + ",".join(r.vetoes[:3])
        else:
            extra = ", ".join(r.signals[:3])
        print(
            f"{i:<3} {r.tier:<11} {r.symbol:<12} {r.combined_score:5.1f} {r.github_score:5.1f} "
            f"{r.market_score:5.1f} {repo:<28} {extra}"
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Binance USDT × GitHub dev anomaly scanner (trade-safe)")
    p.add_argument("--top", type=int, default=40, help="Hacim sırasından N coin (mega hariç default)")
    p.add_argument("--rank-from", type=int, default=1, help="Hacim sırası başlangıç (1=en yüksek, mega hariç)")
    p.add_argument("--rank-to", type=int, default=None, help="Hacim sırası bitiş")
    p.add_argument("--symbols", type=str, default=None, help="Virgüllü liste: SEI,TIA,INJ")
    p.add_argument("--min-score", type=float, default=45.0, help="Research modunda skor eşiği")
    p.add_argument("--detail-commits", type=int, default=12, help="Büyük diff için incelenen commit")
    p.add_argument(
        "--mode",
        choices=["trade", "watch", "research"],
        default="trade",
        help="trade=sadece ACTIONABLE | watch=ACTIONABLE+WATCH | research=hepsi",
    )
    p.add_argument("--include-mega", action="store_true", help="BTC/ETH/SOL vb. mega-cap'leri dahil et")
    p.add_argument("--refresh-map", action="store_true", help="Repo cache yenile")
    p.add_argument("--skip-coingecko", action="store_true", help="Sadece manual/cache/search")
    p.add_argument("--output", type=str, default=None, help="Rapor JSON yolu")
    p.add_argument("--telegram", action="store_true", help="Sonuçları Telegram'a gönder")
    p.add_argument("--dry-run", action="store_true", help="Alias — Telegram kapalı")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.symbols:
        args.symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        args.top = None
    run_scan(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
