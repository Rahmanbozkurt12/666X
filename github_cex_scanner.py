#!/usr/bin/env python3
"""
GitHub CEX coin erken uyarı tarayıcısı.

Major CEX GitHub org/repo'larını tarar; listing / new asset / trading pair
sinyallerini coin yükselişinden önce Telegram'a basar.

Kullanım:
  export GITHUB_TOKEN=ghp_...          # önerilir (rate limit)
  export TELEGRAM_BOT_TOKEN=...
  export TELEGRAM_CHAT_ID=...

  python github_cex_scanner.py                 # sürekli tara
  python github_cex_scanner.py --once          # tek tur
  python github_cex_scanner.py --dry-run       # Telegram yok, konsol
  python github_cex_scanner.py --once --dry-run --priority high
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "github_cex_targets.json"
STATE_PATH = ROOT / "output" / "github_cex_state.json"
ALERTS_LOG = ROOT / "output" / "github_cex_alerts.jsonl"

GITHUB_API = "https://api.github.com"
USER_AGENT = "666X-github-cex-scanner/1.0"

PRIORITY_WEIGHT = {"high": 25, "medium": 10, "low": 0}

# Commit/PR/event metninden olası coin ticker'ları çek
TICKER_RE = re.compile(r"\b([A-Z]{2,12})(?:[/_-]?(USDT|USDC|BTC|ETH|BUSD|FDUSD|USD))?\b")
NOISE_TICKERS = {
    "HTTP", "HTTPS", "API", "SDK", "JSON", "YAML", "HTML", "CSS", "UTC", "PDF",
    "URL", "URI", "UUID", "SHA", "MD", "PR", "CI", "CD", "OK", "ID", "V1", "V2",
    "V3", "V4", "GET", "POST", "PUT", "PATCH", "DELETE", "NULL", "TRUE", "FALSE",
    "AND", "OR", "THE", "FOR", "NEW", "ADD", "FIX", "DOC", "DOCS", "README",
    "LICENSE", "MIT", "GPL", "TODO", "FIXME", "WIP", "HOT", "COLD", "SPOT",
    "TEST", "PROD", "DEV", "MAIN", "MASTER", "BRANCH", "COMMIT", "MERGE",
    "RELEASE", "TAG", "ORG", "REPO", "GITHUB", "TELEGRAM", "CEX", "DEX",
    "USDT", "USDC", "BUSD", "FDUSD", "USD", "EUR", "TRY", "GBP", "JPY",
    "ETH", "BTC", "BNB", "SOL", "TRX", "XRP", "ADA", "DOT", "AVAX",
    "CHANGELOG", "COINBASE", "BINANCE", "BYBIT", "KUCOIN", "GATEIO", "BITGET",
    "MEXC", "KRAKEN", "HUOBI", "OKX", "PYTHON", "JAVA", "GOLANG", "NODE",
    "TYPESCRIPT", "CONSTANTS", "WEBSOCKET", "REST", "PUBLIC", "PRIVATE",
    "UPDATE", "ENUMS", "EXAMPLE", "PACKAGE", "MODULES", "SRC", "LIB", "PKG",
    "DIST", "JS", "TS", "GO", "PY", "NPMIGNORE", "GITIGNORE", "POSTMAN",
    "OPENAPI", "CLIENT", "MODEL", "INTERNAL", "ACCOUNTS", "FEES", "USER",
    "OFFLINE", "CONSTS", "WS", "UTA", "UPEX", "COM", "JSII",
}


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) and value.strip() else default


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GitHubClient:
    def __init__(self, token: str | None) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.remaining: int | None = None
        self.reset_at: int | None = None

    def get(self, path: str, params: dict[str, Any] | None = None, *, abs_url: str | None = None) -> Any:
        url = abs_url or f"{GITHUB_API}{path}"
        try:
            r = self.session.get(url, params=params, timeout=40)
        except requests.RequestException as exc:
            print(f"[github] request fail {url}: {exc}", file=sys.stderr)
            return None

        self.remaining = _to_int(r.headers.get("X-RateLimit-Remaining"))
        self.reset_at = _to_int(r.headers.get("X-RateLimit-Reset"))

        if r.status_code == 403 and (self.remaining == 0 or "rate limit" in (r.text or "").lower()):
            wait = max(5, (self.reset_at or int(time.time()) + 30) - int(time.time()))
            if wait > 120:
                print(
                    f"[github] rate limit dolu ({wait}s). GITHUB_TOKEN ekle veya sonra dene. Skip: {path}",
                    file=sys.stderr,
                )
                return None
            print(f"[github] rate limit — {wait}s bekleniyor", file=sys.stderr)
            time.sleep(wait)
            return self.get(path, params=params, abs_url=abs_url)

        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            print(f"[github] HTTP {r.status_code} {path}: {r.text[:220]}", file=sys.stderr)
            return None
        try:
            return r.json()
        except ValueError:
            return None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def event_id(kind: str, *parts: str) -> str:
    raw = "|".join([kind, *[p for p in parts if p]])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def extract_tickers(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in TICKER_RE.finditer(text.upper()):
        base = m.group(1)
        if base in NOISE_TICKERS or len(base) < 2:
            continue
        if base not in seen:
            seen.add(base)
            found.append(base)
        if len(found) >= 8:
            break
    return found


def score_text(
    text: str,
    *,
    high_kw: list[str],
    medium_kw: list[str],
    file_hints: list[str],
    paths: list[str] | None = None,
    priority: str = "medium",
    require_high: bool = False,
) -> tuple[int, list[str], bool]:
    """Returns (score, hits, has_high_signal)."""
    lower = text.lower()
    hits: list[str] = []
    score = PRIORITY_WEIGHT.get(priority, 0)
    has_high = False

    for kw in high_kw:
        if kw.lower() in lower:
            hits.append(kw)
            score += 40
            has_high = True

    for kw in medium_kw:
        # kelime sınırı — "spot" SDK path'lerinde sürekli false positive
        if re.search(rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])", lower):
            hits.append(kw)
            score += 8

    for path in paths or []:
        p = path.lower().replace("\\", "/")
        base = p.rsplit("/", 1)[-1]
        for hint in file_hints:
            # "coin" → coinbase false positive olmasın; path segment / dosya adı
            if re.search(rf"(^|[_./-]){re.escape(hint)}([_./-]|$)", p) or hint == base:
                hits.append(f"file:{hint}")
                score += 15
                break

    tickers = extract_tickers(text)
    if tickers:
        score += min(30, 6 * len(tickers))
        hits.extend(f"${t}" for t in tickers[:5])

    if require_high and not has_high:
        score = min(score, 20)

    uniq: list[str] = []
    seen: set[str] = set()
    for h in hits:
        key = h.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(h)
    return score, uniq, has_high


def telegram_send(token: str, chat_id: str, text: str, dry_run: bool = False) -> bool:
    if dry_run:
        print("--- DRY-RUN TELEGRAM ---\n" + text + "\n------------------------")
        return True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            print(f"[telegram] HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return False
        return True
    except requests.RequestException as exc:
        print(f"[telegram] error: {exc}", file=sys.stderr)
        return False


def fmt_alert(
    *,
    venue: str,
    title: str,
    score: int,
    hits: list[str],
    url: str,
    detail: str,
    source: str,
) -> str:
    hit_str = ", ".join(hits[:10]) if hits else "—"
    detail_short = (detail or "").strip().replace("<", "").replace(">", "")
    if len(detail_short) > 280:
        detail_short = detail_short[:277] + "..."
    return (
        f"<b>🚨 CEX GITHUB SİNYALİ</b>\n"
        f"<b>{venue}</b> · score {score}\n"
        f"Kaynak: <code>{source}</code>\n"
        f"<b>{title}</b>\n"
        f"Hits: {hit_str}\n"
        f"{detail_short}\n"
        f"<a href=\"{url}\">GitHub</a>"
    )


def scan_org_events(
    gh: GitHubClient,
    org: dict[str, Any],
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    bootstrap: bool,
) -> list[dict[str, Any]]:
    name = org["name"]
    venue = org.get("venue") or name
    priority = org.get("priority") or "medium"
    settings = config.get("settings") or {}
    kw = config.get("keywords") or {}
    per_page = int(settings.get("events_per_org") or 30)

    data = gh.get(f"/orgs/{name}/events", params={"per_page": per_page})
    if not isinstance(data, list):
        # bazı org'lar user gibi; users endpoint dene
        data = gh.get(f"/users/{name}/events/public", params={"per_page": per_page})
    if not isinstance(data, list):
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set(state.get("seen") or [])

    for ev in data:
        eid = str(ev.get("id") or "")
        etype = str(ev.get("type") or "")
        repo = ((ev.get("repo") or {}).get("name")) or ""
        actor = ((ev.get("actor") or {}).get("login")) or ""
        payload = ev.get("payload") or {}
        created = ev.get("created_at") or ""

        texts: list[str] = [etype, repo, actor]
        paths: list[str] = []
        url = f"https://github.com/{repo}" if repo else "https://github.com"

        if etype == "PushEvent":
            for c in payload.get("commits") or []:
                texts.append(str(c.get("message") or ""))
                url = f"https://github.com/{repo}/commit/{c.get('sha')}" if c.get("sha") else url
        elif etype == "CreateEvent":
            texts.append(str(payload.get("ref_type") or ""))
            texts.append(str(payload.get("ref") or ""))
            texts.append(str(payload.get("description") or ""))
        elif etype == "ReleaseEvent":
            rel = payload.get("release") or {}
            texts.append(str(rel.get("name") or ""))
            texts.append(str(rel.get("tag_name") or ""))
            texts.append(str(rel.get("body") or ""))
            url = rel.get("html_url") or url
        elif etype in {"IssuesEvent", "IssueCommentEvent", "PullRequestEvent", "PullRequestReviewCommentEvent"}:
            obj = payload.get("issue") or payload.get("pull_request") or {}
            texts.append(str(obj.get("title") or ""))
            texts.append(str(obj.get("body") or ""))
            url = obj.get("html_url") or url
        elif etype == "PublicEvent":
            texts.append("repository made public")
        else:
            # düşük sinyal event'leri atla
            continue

        key = event_id("event", eid or repo, etype, created)
        if key in seen:
            continue
        seen.add(key)
        if bootstrap:
            continue

        blob = "\n".join(texts)
        score, hits, has_high = score_text(
            blob,
            high_kw=kw.get("high") or [],
            medium_kw=kw.get("medium") or [],
            file_hints=kw.get("file_hints") or [],
            paths=paths,
            priority=priority,
            require_high=True,
        )
        min_score = int(settings.get("min_score") or 35)
        if score < min_score or not has_high:
            continue

        title = f"{etype} · {repo}"
        detail = next((t for t in texts[3:] if t.strip()), blob[:200])
        out.append(
            {
                "key": key,
                "venue": venue,
                "title": title,
                "score": score,
                "hits": hits,
                "url": url,
                "detail": detail,
                "source": f"org-events:{name}",
                "created_at": created,
            }
        )

    state["seen"] = list(seen)
    return out


def scan_watch_repos(
    gh: GitHubClient,
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    bootstrap: bool,
) -> list[dict[str, Any]]:
    settings = config.get("settings") or {}
    kw = config.get("keywords") or {}
    per_page = int(settings.get("commits_per_repo") or 15)
    min_score = int(settings.get("min_score") or 35)
    out: list[dict[str, Any]] = []
    seen: set[str] = set(state.get("seen") or [])

    for row in config.get("watch_repos") or []:
        repo = row.get("repo") or ""
        venue = row.get("venue") or repo.split("/")[0]
        if not repo or "/" not in repo:
            continue
        owner, name = repo.split("/", 1)
        commits = gh.get(
            f"/repos/{owner}/{name}/commits",
            params={"per_page": per_page},
        )
        if not isinstance(commits, list):
            continue

        for c in commits:
            sha = c.get("sha") or ""
            commit = c.get("commit") or {}
            msg = str(commit.get("message") or "")
            html = c.get("html_url") or f"https://github.com/{repo}/commit/{sha}"
            key = event_id("commit", repo, sha)
            if key in seen:
                continue
            seen.add(key)
            if bootstrap:
                continue

            paths: list[str] = []
            pre_score, _, pre_high = score_text(
                msg,
                high_kw=kw.get("high") or [],
                medium_kw=kw.get("medium") or [],
                file_hints=kw.get("file_hints") or [],
                paths=[],
                priority="high",
            )
            # Watch-repo: sadece listing keyword veya yüksek skorlu mesajlarda dosya çek
            if (pre_high or pre_score >= min_score) and sha:
                files_meta = gh.get(f"/repos/{owner}/{name}/commits/{sha}")
                if isinstance(files_meta, dict):
                    for f in files_meta.get("files") or []:
                        paths.append(str(f.get("filename") or ""))
                    time.sleep(0.15)

            blob = msg + "\n" + "\n".join(paths)
            score, hits, has_high = score_text(
                blob,
                high_kw=kw.get("high") or [],
                medium_kw=kw.get("medium") or [],
                file_hints=kw.get("file_hints") or [],
                paths=paths,
                priority="high",
                require_high=True,
            )
            # SDK gürültüsü: listing keyword şart
            if not has_high or score < min_score:
                continue

            out.append(
                {
                    "key": key,
                    "venue": venue,
                    "title": f"Commit · {repo}",
                    "score": score,
                    "hits": hits,
                    "url": html,
                    "detail": msg.split("\n", 1)[0][:300],
                    "source": f"watch-repo:{repo}",
                    "created_at": (commit.get("author") or {}).get("date") or "",
                }
            )

        time.sleep(0.25)

    state["seen"] = list(seen)
    return out


def scan_code_search(
    gh: GitHubClient,
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    bootstrap: bool,
) -> list[dict[str, Any]]:
    """Son güncellenen listing-benzeri kod/dosya hit'leri (erken sinyal)."""
    settings = config.get("settings") or {}
    kw = config.get("keywords") or {}
    min_score = int(settings.get("min_score") or 35)
    out: list[dict[str, Any]] = []
    seen: set[str] = set(state.get("seen") or [])

    for row in config.get("code_searches") or []:
        query = row.get("query") or ""
        venue = row.get("venue") or "CEX"
        if not query:
            continue
        data = gh.get(
            "/search/code",
            params={"q": query, "per_page": 10, "sort": "indexed", "order": "desc"},
        )
        if not isinstance(data, dict):
            continue
        items = data.get("items") or []
        for item in items:
            repo_full = ((item.get("repository") or {}).get("full_name")) or ""
            path = item.get("path") or ""
            html = item.get("html_url") or f"https://github.com/{repo_full}"
            name = item.get("name") or ""
            key = event_id("codesearch", repo_full, path, name)
            if key in seen:
                continue
            seen.add(key)
            if bootstrap:
                continue

            blob = f"{repo_full}\n{path}\n{name}\n{query}"
            score, hits, _has_high = score_text(
                blob,
                high_kw=kw.get("high") or [],
                medium_kw=kw.get("medium") or [],
                file_hints=kw.get("file_hints") or [],
                paths=[path, name],
                priority="high",
            )
            score += 20
            if score < min_score:
                continue
            out.append(
                {
                    "key": key,
                    "venue": venue,
                    "title": f"Code hit · {repo_full}",
                    "score": score,
                    "hits": hits,
                    "url": html,
                    "detail": path or name,
                    "source": f"code-search:{venue}",
                    "created_at": now_iso(),
                }
            )
        time.sleep(2.0)  # code search rate limit sıkı

    state["seen"] = list(seen)
    return out


def scan_new_repos(
    gh: GitHubClient,
    org: dict[str, Any],
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    bootstrap: bool,
) -> list[dict[str, Any]]:
    """Org'da yeni public repo = bazen listing bot / announcement repo sinyali."""
    name = org["name"]
    venue = org.get("venue") or name
    priority = org.get("priority") or "medium"
    settings = config.get("settings") or {}
    kw = config.get("keywords") or {}
    min_score = int(settings.get("min_score") or 35)

    data = gh.get(
        f"/orgs/{name}/repos",
        params={"per_page": 20, "sort": "created", "direction": "desc", "type": "public"},
    )
    if not isinstance(data, list):
        data = gh.get(
            f"/users/{name}/repos",
            params={"per_page": 20, "sort": "created", "direction": "desc", "type": "owner"},
        )
    if not isinstance(data, list):
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set(state.get("seen") or [])
    known = set(state.get("known_repos") or [])

    for repo in data:
        full = repo.get("full_name") or ""
        if not full:
            continue
        if full in known:
            continue
        known.add(full)
        key = event_id("newrepo", full)
        if key in seen:
            continue
        seen.add(key)
        if bootstrap:
            continue

        blob = " ".join(
            [
                full,
                str(repo.get("name") or ""),
                str(repo.get("description") or ""),
                str(repo.get("homepage") or ""),
            ]
        )
        score, hits, has_high = score_text(
            blob,
            high_kw=kw.get("high") or [],
            medium_kw=kw.get("medium") or [],
            file_hints=kw.get("file_hints") or [],
            paths=[str(repo.get("name") or "")],
            priority=priority,
        )
        # high-priority CEX yeni repo her zaman bildir; diğerlerinde listing sinyali iste
        score += 30 if priority == "high" else 15
        if priority != "high" and not has_high:
            continue
        if score < min_score and priority != "high":
            continue

        out.append(
            {
                "key": key,
                "venue": venue,
                "title": f"Yeni repo · {full}",
                "score": score,
                "hits": hits or ["new-repo"],
                "url": repo.get("html_url") or f"https://github.com/{full}",
                "detail": str(repo.get("description") or "Yeni public repo"),
                "source": f"new-repo:{name}",
                "created_at": repo.get("created_at") or "",
            }
        )

    state["seen"] = list(seen)
    state["known_repos"] = sorted(known)[-5000:]
    return out


def trim_seen(state: dict[str, Any], limit: int = 30000) -> None:
    seen = list(state.get("seen") or [])
    if len(seen) > limit:
        state["seen"] = seen[-int(limit * 0.8) :]


def poll_once(
    config: dict[str, Any],
    state: dict[str, Any],
    gh: GitHubClient,
    *,
    dry_run: bool,
    token: str | None,
    chat_id: str | None,
    priority_filter: str | None,
    skip_code_search: bool,
) -> int:
    settings = config.get("settings") or {}
    bootstrap = bool(settings.get("bootstrap_silent", True)) and not state.get("bootstrapped")
    max_alerts = int(settings.get("max_alerts_per_poll") or 25)

    orgs = list(config.get("orgs") or [])
    if priority_filter == "high":
        orgs = [o for o in orgs if (o.get("priority") or "medium") == "high"]
    elif priority_filter == "medium":
        orgs = [o for o in orgs if (o.get("priority") or "medium") in {"high", "medium"}]

    alerts: list[dict[str, Any]] = []

    print(f"[scan] orgs={len(orgs)} bootstrap={bootstrap} remaining={gh.remaining}")

    for org in orgs:
        alerts.extend(scan_org_events(gh, org, config, state, bootstrap=bootstrap))
        alerts.extend(scan_new_repos(gh, org, config, state, bootstrap=bootstrap))
        time.sleep(0.35)

    alerts.extend(scan_watch_repos(gh, config, state, bootstrap=bootstrap))

    if not skip_code_search:
        alerts.extend(scan_code_search(gh, config, state, bootstrap=bootstrap))

    # skor sırası
    alerts.sort(key=lambda a: int(a.get("score") or 0), reverse=True)
    alerts = alerts[:max_alerts]

    sent = 0
    for a in alerts:
        msg = fmt_alert(
            venue=str(a.get("venue")),
            title=str(a.get("title")),
            score=int(a.get("score") or 0),
            hits=list(a.get("hits") or []),
            url=str(a.get("url")),
            detail=str(a.get("detail") or ""),
            source=str(a.get("source") or ""),
        )
        ok = telegram_send(
            token or "",
            chat_id or "",
            msg,
            dry_run=dry_run or not (token and chat_id),
        )
        if ok:
            sent += 1
            print(f"[alert] {a.get('venue')} score={a.get('score')} {a.get('title')}")
            append_jsonl(
                ALERTS_LOG,
                {**a, "sent_at": now_iso(), "dry_run": dry_run or not (token and chat_id)},
            )
        time.sleep(0.2)

    if bootstrap:
        state["bootstrapped"] = True
        print("[scan] bootstrap tamam — sonraki poll'da yeni sinyaller alert üretecek")

    trim_seen(state)
    state["updated_at"] = now_iso()
    state["last_alert_count"] = sent
    save_json(STATE_PATH, state)
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub CEX coin erken uyarı tarayıcısı")
    parser.add_argument("--once", action="store_true", help="Tek tarama turu")
    parser.add_argument("--dry-run", action="store_true", help="Telegram gönderme")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument(
        "--priority",
        choices=["high", "medium", "low"],
        default=None,
        help="Sadece bu öncelik ve üstünü tara (high=sadece high)",
    )
    parser.add_argument("--skip-code-search", action="store_true", help="Code search'i atla (rate limit)")
    parser.add_argument("--reset-state", action="store_true", help="State dosyasını silip yeniden bootstrap")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"config yok: {config_path}")

    if args.reset_state and STATE_PATH.exists():
        STATE_PATH.unlink()
        print(f"[info] state silindi: {STATE_PATH}")

    config = load_json(config_path)
    gh_token = env("GITHUB_TOKEN") or env("GH_TOKEN")
    if not gh_token:
        # gh CLI oturumu varsa token'ı kullan
        try:
            proc = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                gh_token = proc.stdout.strip()
                print("[info] GITHUB_TOKEN yok — gh auth token kullanılıyor", file=sys.stderr)
        except (OSError, subprocess.SubprocessError):
            pass
    tg_token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    dry_run = bool(args.dry_run) or not (tg_token and chat_id)
    if dry_run and not args.dry_run:
        print("[info] Telegram env yok → dry-run", file=sys.stderr)
    if not gh_token:
        print("[warn] GITHUB_TOKEN yok — rate limit düşük (60/saat). Token önerilir.", file=sys.stderr)

    gh = GitHubClient(gh_token)
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"seen": [], "known_repos": [], "bootstrapped": False}
    poll_seconds = int((config.get("settings") or {}).get("poll_seconds") or 60)

    print(
        f"GitHub CEX scanner | poll={poll_seconds}s | dry_run={dry_run} | "
        f"orgs={len(config.get('orgs') or [])} | priority={args.priority or 'all'}"
    )

    while True:
        n = poll_once(
            config,
            state,
            gh,
            dry_run=dry_run,
            token=tg_token,
            chat_id=chat_id,
            priority_filter=args.priority,
            skip_code_search=bool(args.skip_code_search),
        )
        print(f"[{now_iso()}] poll done, alerts={n}, rate_remaining={gh.remaining}")
        if args.once:
            break
        time.sleep(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
