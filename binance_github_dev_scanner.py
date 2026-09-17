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
    signals: list[str]
    market: dict[str, Any]
    github: dict[str, Any]


def score_github(act: RepoActivity) -> tuple[float, list[str]]:
    if not act.exists:
        return 0.0, ["no_repo"]

    score = 0.0
    signals: list[str] = []

    accel7 = act.commit_accel_7_vs_prior or 0.0
    accel90 = act.commit_accel_7_vs_90avg or 0.0
    accel30 = act.commit_accel_30_vs_prior or 0.0

    # Commit velocity spikes
    if accel7 >= 3 and act.windows["7"].commits >= 3:
        score += 28
        signals.append(f"commit_spike_7d_x{accel7:.1f}")
    elif accel7 >= 2 and act.windows["7"].commits >= 2:
        score += 18
        signals.append(f"commit_up_7d_x{accel7:.1f}")

    if accel90 >= 2.5 and act.windows["7"].commits >= 3:
        score += 18
        signals.append(f"vs_90d_avg_x{accel90:.1f}")

    if accel30 >= 2 and act.windows["30"].commits >= 8:
        score += 12
        signals.append(f"commit_spike_30d_x{accel30:.1f}")

    # Recency
    dsc = act.days_since_commit
    if dsc is not None:
        if dsc <= 1:
            score += 10
            signals.append("commit_last_24h")
        elif dsc <= 3:
            score += 6
            signals.append("commit_last_3d")
        elif dsc > 60:
            score -= 8
            signals.append("stale_repo")

    # Contributors
    if (act.contributor_delta_30 or 0) >= 3:
        score += 10
        signals.append(f"new_contributors_+{act.contributor_delta_30}")
    elif (act.contributor_delta_30 or 0) >= 1 and act.windows["7"].commits >= 2:
        score += 5
        signals.append("contributor_growth")

    # Releases
    if act.windows["7"].releases >= 1:
        score += 12
        signals.append(f"release_7d_{act.windows['7'].releases}")
    elif act.windows["30"].releases >= 2:
        score += 7
        signals.append(f"releases_30d_{act.windows['30'].releases}")

    # Large code changes
    if (act.large_change_ratio_7 or 0) >= 0.35 and act.windows["7"].large_commits >= 2:
        score += 14
        signals.append("large_code_changes_7d")
    elif act.windows["7"].additions + act.windows["7"].deletions >= 2000:
        score += 8
        signals.append("heavy_diff_7d")

    # Absolute activity floor
    if act.windows["7"].commits >= 10:
        score += 6
        signals.append("high_abs_commits_7d")

    return clamp(score), signals


def score_market(mkt: dict[str, Any]) -> tuple[float, list[str]]:
    score = 0.0
    signals: list[str] = []
    chg = float(mkt.get("change_pct_24h") or 0)
    vol = float(mkt.get("quote_volume_24h") or 0)
    cvd_r = float((mkt.get("cvd") or {}).get("cvd_ratio") or 0)
    imb = float((mkt.get("orderbook") or {}).get("imbalance") or 0.5)

    # Volume rank already filtered by --top; still reward very high volume
    if vol >= 50_000_000:
        score += 10
        signals.append("high_volume")
    elif vol >= 10_000_000:
        score += 5

    # Early pump bias: mild green + buy pressure better than already +30%
    if 2 <= chg <= 12:
        score += 12
        signals.append("early_uptrend")
    elif 12 < chg <= 25:
        score += 6
        signals.append("momentum")
    elif chg > 40:
        score -= 5
        signals.append("extended_pump")
    elif chg < -8:
        score -= 4
        signals.append("dumping")

    if cvd_r >= 0.15:
        score += 14
        signals.append(f"cvd_buy_{cvd_r:.2f}")
    elif cvd_r >= 0.05:
        score += 7
        signals.append(f"cvd_mild_buy_{cvd_r:.2f}")
    elif cvd_r <= -0.15:
        score -= 8
        signals.append(f"cvd_sell_{cvd_r:.2f}")

    if imb >= 0.62:
        score += 10
        signals.append(f"book_bid_{imb:.2f}")
    elif imb <= 0.38:
        score -= 6
        signals.append(f"book_ask_{imb:.2f}")

    return clamp(score), signals


def combine_scores(gh: float, mkt: float) -> float:
    # GitHub anomali ağırlığı yüksek — amaç erken geliştirme sinyali
    return clamp(0.62 * gh + 0.38 * mkt)


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
    lines = ["<b>Binance × GitHub Dev Anomaly</b>"]
    for i, r in enumerate(rows[:limit], 1):
        sig = ", ".join(r.signals[:4]) or "—"
        repo = r.repo or "—"
        lines.append(
            f"{i}. <b>{r.symbol}</b> score={r.combined_score:.0f} "
            f"(gh={r.github_score:.0f} mkt={r.market_score:.0f})\n"
            f"   <code>{repo}</code>\n"
            f"   {sig}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def select_tickers(
    tickers: list[dict[str, Any]],
    *,
    top: int | None,
    symbols: list[str] | None,
) -> list[dict[str, Any]]:
    if symbols:
        want = {s.upper().replace("/", "") for s in symbols}
        want = {s if s.endswith("USDT") else f"{s}USDT" for s in want}
        picked = [t for t in tickers if t["symbol"] in want]
        missing = want - {t["symbol"] for t in picked}
        if missing:
            print(f"[warn] bulunamayan semboller: {sorted(missing)}", file=sys.stderr)
        return picked
    n = top or 25
    return tickers[:n]


def run_scan(args: argparse.Namespace) -> dict[str, Any]:
    token = resolve_github_token()
    if not token:
        print("[warn] GITHUB_TOKEN yok — rate limit düşük", file=sys.stderr)
    else:
        print("[info] GitHub token aktif", file=sys.stderr)

    http = Http(token)
    tickers = binance_usdt_tickers(http)
    selected = select_tickers(tickers, top=args.top, symbols=args.symbols)
    print(f"[scan] Binance USDT={len(tickers)} seçilen={len(selected)}", file=sys.stderr)

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

        # market microstructure
        ob = binance_orderbook(http, symbol)
        cvd = binance_cvd(http, symbol)
        market = {
            **t,
            "orderbook": ob,
            "cvd": cvd,
        }
        mkt_score, mkt_signals = score_market(market)

        gh_payload: dict[str, Any]
        if not repo:
            gh_score, gh_signals = 0.0, ["repo_unresolved"]
            gh_payload = {"repo": None, "exists": False}
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
        reports.append(
            CoinReport(
                symbol=symbol,
                base=base,
                repo=repo,
                repo_source=str(mapping.get("source") or "none"),
                repo_confidence=float(mapping.get("confidence") or 0),
                github_score=gh_score,
                market_score=mkt_score,
                combined_score=combined,
                signals=signals,
                market=market,
                github=gh_payload,
            )
        )
        time.sleep(0.2)

    save_repo_cache_safe(cache)
    reports.sort(key=lambda r: r.combined_score, reverse=True)
    filtered = [r for r in reports if r.combined_score >= args.min_score]

    payload = {
        "generated_at": iso(now_utc()),
        "binance_base": BINANCE_BASE,
        "params": {
            "top": args.top,
            "symbols": args.symbols,
            "min_score": args.min_score,
            "detail_commits": args.detail_commits,
        },
        "github_rate_remaining": http.gh_remaining,
        "scanned": len(reports),
        "hits": len(filtered),
        "results": [asdict(r) for r in reports],
        "anomalies": [asdict(r) for r in filtered],
    }
    out_path = Path(args.output) if args.output else REPORT_PATH
    save_json(out_path, payload)

    print_console(reports, args.min_score)
    print(f"\n[report] {out_path} | anomalies>={args.min_score}: {len(filtered)}", file=sys.stderr)

    if args.telegram:
        tg = env("TELEGRAM_BOT_TOKEN")
        chat = env("TELEGRAM_CHAT_ID")
        if tg and chat:
            top_rows = filtered[:8] if filtered else reports[:5]
            telegram_send(tg, chat, format_top_telegram(top_rows))
        else:
            print("[warn] TELEGRAM env yok", file=sys.stderr)

    return payload


def save_repo_cache_safe(cache: dict[str, Any]) -> None:
    save_json(CACHE_PATH, {"updated_at": iso(now_utc()), "cache": cache})


def print_console(reports: list[CoinReport], min_score: float) -> None:
    print("\n=== Binance × GitHub Dev Anomaly ===")
    print(f"{'#':<3} {'SYMBOL':<12} {'COMB':>5} {'GH':>5} {'MKT':>5} {'REPO':<32} SIGNALS")
    for i, r in enumerate(reports[:30], 1):
        mark = "*" if r.combined_score >= min_score else " "
        repo = (r.repo or "—")[:32]
        sig = ", ".join(r.signals[:3])
        print(
            f"{i:<3}{mark}{r.symbol:<11} {r.combined_score:5.1f} {r.github_score:5.1f} "
            f"{r.market_score:5.1f} {repo:<32} {sig}"
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Binance USDT × GitHub dev anomaly scanner")
    p.add_argument("--top", type=int, default=20, help="Hacme göre ilk N USDT çifti (default 20)")
    p.add_argument("--symbols", type=str, default=None, help="Virgüllü liste: BTC,ETH,SOL")
    p.add_argument("--min-score", type=float, default=45.0, help="Anomali eşiği")
    p.add_argument("--detail-commits", type=int, default=20, help="Büyük diff için incelenen commit sayısı")
    p.add_argument("--refresh-map", action="store_true", help="Repo cache yenile")
    p.add_argument("--skip-coingecko", action="store_true", help="Sadece manual/cache/search")
    p.add_argument("--output", type=str, default=None, help="Rapor JSON yolu")
    p.add_argument("--telegram", action="store_true", help="Top anomalileri Telegram'a gönder")
    p.add_argument("--dry-run", action="store_true", help="Alias — Telegram kapalı (varsayılan)")
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
