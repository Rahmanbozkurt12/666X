#!/usr/bin/env python3
"""
Binance TÜM USDT spot coinleri — dipten erken AL radarı.

AR örneğindeki gibi +%50 olduktan sonra değil; henüz dipteyken /
yükselişin başında yakalar. Her coin için çok katmanlı skor:

  1) Teknik (5m/15m/1h/1d): dip yakın, EMA dönüş, RSI toparlanma
  2) Hacim: 5m dipten hacim yükselişi, 24s hacim uyanışı
  3) Momentum erken: 0→+, yeşil mum, henüz parabolic değil
  4) Relative strength: BTC'ye göre erken güç
  5) Tokenomics proxy: full-float benzeri (max≈circ) tercih
  6) Geç kalma filtresi: 24s zaten +%15 / RSI>70 / dipten +%25 → RED

Sinyaller:
  🟢 AL   — skor ≥ min_score_al  ve erken faz
  🟡 İZLE — skor orta, gelişiyor
  🔴 GEÇ  — rally olmuş / aşırı alım (haber değil)
  ⚪ YOK  — dip şartı yok

Kullanım:
  python3 binance_dip_buy_radar.py --once --dry-run
  python3 binance_dip_buy_radar.py --once --top 20
  python3 binance_dip_buy_radar.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "binance_dip_buy_radar.json"
OUTPUT_PATH = ROOT / "output" / "binance_dip_buy_signals.json"
STATE_PATH = ROOT / "output" / "binance_dip_buy_state.json"

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "binance-dip-buy-radar/1.0"})


@dataclass
class Analysis:
    symbol: str
    base: str
    price: float
    change_24h_pct: float
    quote_volume_24h: float
    score: float
    action: str
    phase: str
    reasons: list[str] = field(default_factory=list)
    layers: dict[str, Any] = field(default_factory=dict)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) and v.strip() else default


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def get_json(url: str, params: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    r = HTTP.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
    return e


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def parse_klines(rows: list[list[Any]]) -> dict[str, list[float]]:
    return {
        "o": [float(r[1]) for r in rows],
        "h": [float(r[2]) for r in rows],
        "l": [float(r[3]) for r in rows],
        "c": [float(r[4]) for r in rows],
        "v": [float(r[5]) for r in rows],
        "qv": [float(r[7]) for r in rows],
    }


def closed_slice(d: dict[str, list[float]]) -> dict[str, list[float]]:
    """Son (oluşan) mumu düş."""
    if len(d["c"]) < 3:
        return d
    return {k: v[:-1] for k, v in d.items()}


def list_usdt_symbols(cfg: dict[str, Any]) -> list[str]:
    base_url = cfg["rest_base"]
    info = get_json(f"{base_url}/api/v3/exchangeInfo")
    quote = cfg.get("quote") or "USDT"
    stables = {s.upper() for s in (cfg.get("stable_bases") or [])}
    skip_bases = {s.upper() for s in (cfg.get("skip_bases") or [])}
    skip_suf = tuple(cfg.get("skip_suffixes") or [])
    skip_stocks = bool(cfg.get("skip_tokenized_stocks", True))
    out: list[str] = []
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != quote:
            continue
        if not s.get("isSpotTradingAllowed", True):
            continue
        b = str(s.get("baseAsset") or "").upper()
        if b in stables or b in skip_bases:
            continue
        if any(b.endswith(suf) for suf in skip_suf):
            continue
        if skip_stocks and is_tokenized_stock(b):
            continue
        sym = str(s["symbol"])
        if any(sym.endswith(f"{suf}{quote}") for suf in skip_suf):
            continue
        out.append(sym)
    return out


def is_tokenized_stock(base: str) -> bool:
    """Binance tokenized US stock (AAPLB, NVDAB, GOOGLB…). BNB hariç."""
    b = base.upper()
    if b in {"BNB", "BB", "OMNI"}:
        return False
    if len(b) >= 4 and b.endswith("B") and b[:-1].isalpha():
        return True
    return False


def fetch_tickers(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    base = cfg["rest_base"]
    rows = get_json(f"{base}/api/v3/ticker/24hr")
    return {r["symbol"]: r for r in rows if "symbol" in r}


def fetch_ohlcv(cfg: dict[str, Any], symbol: str, interval: str, limit: int) -> dict[str, list[float]] | None:
    base = cfg["rest_base"]
    try:
        rows = get_json(
            f"{base}/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
            timeout=25,
        )
        if not isinstance(rows, list) or len(rows) < 10:
            return None
        return parse_klines(rows)
    except Exception:  # noqa: BLE001
        return None


def analyze_volume_bottom(vols: list[float], mult: float) -> tuple[bool, float]:
    if len(vols) < 8:
        return False, 0.0
    last, prev, prev2 = vols[-1], vols[-2], vols[-3]
    if last <= 0:
        return False, 0.0
    trough = min(vols[-12:-1]) if len(vols) >= 12 else min(vols[:-1])
    # sıfır/toz dip hacim → sahte ×1000 sinyali engelle
    if trough <= 0:
        avg = sum(vols[-12:-1]) / max(1, len(vols[-12:-1]))
        trough = max(avg * 0.25, last * 0.05)
        if trough <= 0:
            return False, 0.0
    rise = last / trough
    chain = last > prev >= prev2 * 0.95
    ok = (last >= trough * mult and last > prev) or (
        chain and last >= trough * 1.2 and rise >= 1.15
    )
    return ok, rise


def analyze_symbol(
    symbol: str,
    ticker: dict[str, Any],
    cfg: dict[str, Any],
    btc_change_24h: float,
) -> Analysis | None:
    early = cfg.get("early_buy") or {}
    late = cfg.get("late_reject") or {}
    ohlcv_cfg = cfg.get("ohlcv") or {}

    try:
        price = float(ticker.get("lastPrice") or 0)
        chg24 = float(ticker.get("priceChangePercent") or 0)
        qv24 = float(ticker.get("quoteVolume") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None

    base = symbol.replace(cfg.get("quote") or "USDT", "")
    min_qv = float(cfg.get("min_quote_volume_usdt") or 0)
    if min_qv and qv24 < min_qv:
        return None

    # --- Geç kalma: zaten şişmişse erken AL adayı değil ---
    already_late = (
        chg24 >= float(late.get("max_already_up_24h_pct") or 15)
        or chg24 >= float(early.get("max_24h_change_pct") or 8) + 7
    )

    d5 = fetch_ohlcv(cfg, symbol, "5m", int(ohlcv_cfg.get("5m") or 48))
    d15 = fetch_ohlcv(cfg, symbol, "15m", int(ohlcv_cfg.get("15m") or 96))
    d1h = fetch_ohlcv(cfg, symbol, "1h", int(ohlcv_cfg.get("1h") or 72))
    d1d = fetch_ohlcv(cfg, symbol, "1d", int(ohlcv_cfg.get("1d") or 90))
    if not d5 or not d1h or not d1d:
        return None

    c5 = closed_slice(d5)
    c15 = closed_slice(d15) if d15 else None
    c1h = closed_slice(d1h)
    c1d = closed_slice(d1d)

    score = 0.0
    reasons: list[str] = []
    layers: dict[str, Any] = {"change_24h_pct": chg24, "quote_volume_24h": qv24}

    # ========== 1) TEKNİK — dip yakınlığı ==========
    lookback_d = int(early.get("near_low_lookback_days") or 14)
    day_lows = c1d["l"][-lookback_d:] if len(c1d["l"]) >= lookback_d else c1d["l"]
    day_low = min(day_lows) if day_lows else price
    from_low_pct = ((price / day_low) - 1.0) * 100.0 if day_low > 0 else 999.0
    near_max = float(early.get("near_low_max_pct") or 8.0)
    layers["from_nd_low_pct"] = round(from_low_pct, 2)

    if from_low_pct <= near_max:
        score += 18
        reasons.append(f"DIP_YAKIN_{lookback_d}g(%{from_low_pct:.1f})")
    elif from_low_pct <= near_max * 1.5:
        score += 10
        reasons.append(f"DIP_BOLGE(%{from_low_pct:.1f})")
    elif from_low_pct <= float(late.get("max_from_14d_low_pct") or 25):
        score += 3
    else:
        score -= 12
        reasons.append(f"DIP_UZAK(%{from_low_pct:.1f})")

    # ========== 2) HACİM — 5m dipten yükseliş ==========
    vol_ok, vol_rise = analyze_volume_bottom(
        c5["v"], float(early.get("volume_rise_mult_5m") or 1.4)
    )
    layers["vol_rise_5m"] = round(vol_rise, 2)
    if vol_ok:
        score += 16
        reasons.append(f"HACIM_DIP×{vol_rise:.2f}")
        if vol_rise >= 2.5:
            score += 4
            reasons.append("HACIM_PATLAMA")

    # 24s hacim uyanışı vs 30g ort (1d)
    if len(c1d["v"]) >= 20:
        avg30 = sum(c1d["v"][-30:]) / min(30, len(c1d["v"]))
        today_v = c1d["v"][-1]
        vol_vs_30 = today_v / avg30 if avg30 > 0 else 0
        layers["vol_vs_30d"] = round(vol_vs_30, 2)
        if 1.2 <= vol_vs_30 <= 4.0:
            score += 8
            reasons.append(f"HACIM_UYANIS×{vol_vs_30:.1f}")
        elif vol_vs_30 > 4.0 and chg24 > 10:
            # aşırı hacim + büyük yükseliş = genelde GEÇ
            score -= 6
            reasons.append("HACIM_COK_GEC")

    # ========== 3) MOMENTUM ERKEN — 0→+ / yeşil ==========
    closes5 = c5["c"]
    opens5 = c5["o"]
    if len(closes5) >= 3:
        ret_prev = (closes5[-2] - closes5[-3]) / closes5[-3] if closes5[-3] else 0
        ret_now = (closes5[-1] - closes5[-2]) / closes5[-2] if closes5[-2] else 0
        zero_to_pos = ret_prev <= 0 and ret_now > 0
        green_after_red = closes5[-1] > opens5[-1] and closes5[-2] <= opens5[-2]
        layers["ret_5m_pct"] = round(ret_now * 100, 3)
        if zero_to_pos:
            score += 12
            reasons.append("0→+")
        if green_after_red:
            score += 6
            reasons.append("YESIL_MUM")

    # Higher-low (15m)
    if c15 and len(c15["l"]) >= 20:
        recent = min(c15["l"][-6:])
        prior = min(c15["l"][-18:-6])
        if recent > prior * 1.001 and closes5[-1] > recent:
            score += 8
            reasons.append("HIGHER_LOW")
            layers["higher_low"] = True

    # ========== 4) RSI toparlanma (aşırı alım değil) ==========
    rsi_1h = rsi(c1h["c"], 14)
    rsi_1d = rsi(c1d["c"], 14)
    layers["rsi_1h"] = round(rsi_1h, 1) if rsi_1h is not None else None
    layers["rsi_1d"] = round(rsi_1d, 1) if rsi_1d is not None else None
    min_rsi = float(early.get("min_rsi_1h") or 25)
    max_rsi = float(early.get("max_rsi_1h") or 55)
    if rsi_1h is not None:
        if min_rsi <= rsi_1h <= max_rsi:
            score += 12
            reasons.append(f"RSI_TOPARLANMA({rsi_1h:.0f})")
        elif rsi_1h < min_rsi:
            score += 4
            reasons.append(f"RSI_ASIRI_SATIM({rsi_1h:.0f})")
        elif rsi_1h >= float(late.get("max_rsi_1h") or 70):
            score -= 15
            reasons.append(f"RSI_ASIRI_ALIM({rsi_1h:.0f})")
            already_late = True

    # ========== 5) EMA dönüş (1h) ==========
    ema7 = ema(c1h["c"], 7)
    ema25 = ema(c1h["c"], 25)
    layers["ema7_1h"] = round(ema7, 6) if ema7 else None
    layers["ema25_1h"] = round(ema25, 6) if ema25 else None
    if ema7 and ema25:
        if price > ema7 > ema25:
            # güçlü trend — erken değilse ceza
            if chg24 < 5 and from_low_pct <= 12:
                score += 6
                reasons.append("EMA_STACK_ERKEN")
            else:
                score -= 4
                reasons.append("EMA_STACK_GEC")
        elif price > ema7 and ema7 < ema25:
            score += 10
            reasons.append("EMA7_KIRILIM")  # henüz golden cross öncesi kıpırdanma

    # ========== 6) Relative strength vs BTC ==========
    rel = chg24 - btc_change_24h
    layers["rel_vs_btc_pct"] = round(rel, 2)
    if -2 <= chg24 <= 8 and rel > 1.5:
        score += 8
        reasons.append(f"RS_BTC(+{rel:.1f})")
    elif chg24 < -3 and rel > 0:
        score += 5
        reasons.append("BTC_ALTI_GUC")

    # ========== 7) 24s değişim erken penceresi ==========
    min24 = float(early.get("min_24h_change_pct") or -25)
    max24 = float(early.get("max_24h_change_pct") or 8)
    if min24 <= chg24 <= max24:
        score += 10
        reasons.append(f"24s_ERKEN(%{chg24:+.1f})")
    elif chg24 > max24:
        score -= 10
        reasons.append(f"24s_KACMIS(%{chg24:+.1f})")
        already_late = True

    # ========== 8) Likidite kalitesi ==========
    if qv24 >= 1_000_000:
        score += 5
        reasons.append("LIKIT")
    elif qv24 >= 300_000:
        score += 2

    # ========== SKOR / AKSİYON ==========
    score = max(0.0, min(100.0, score))
    min_al = float(early.get("min_score_al") or 62)
    min_izle = float(early.get("min_score_izle") or 48)

    if already_late or from_low_pct > float(late.get("max_from_14d_low_pct") or 25):
        if score >= min_izle:
            action, phase = "GEÇ", "rally_olmus"
        else:
            action, phase = "GEÇ", "asiri_uzama"
        # GEÇ skorunu düşürme — rapor için tut
    elif score >= min_al and vol_ok and from_low_pct <= near_max * 1.25:
        action, phase = "AL", "dip_erken"
    elif score >= min_izle and (vol_ok or from_low_pct <= near_max):
        action, phase = "İZLE", "gelisiyor"
    else:
        action, phase = "YOK", "sinyal_yok"

    # Erken AL için zorunlu: hacim + dip yakın + RSI aşırı alım değil
    if action == "AL":
        if not (vol_ok and from_low_pct <= near_max * 1.35):
            action, phase = "İZLE", "eksik_onay"
        elif rsi_1h is not None and rsi_1h > 58:
            action, phase = "İZLE", "rsi_sicak"
        elif chg24 > float(early.get("max_24h_change_pct") or 8):
            action, phase = "GEÇ", "24s_kacmis"

    return Analysis(
        symbol=symbol,
        base=base,
        price=price,
        change_24h_pct=chg24,
        quote_volume_24h=qv24,
        score=round(score, 1),
        action=action,
        phase=phase,
        reasons=reasons,
        layers=layers,
    )


def telegram_send(token: str, chat_id: str, text: str, dry_run: bool = False) -> bool:
    if dry_run:
        print("--- DRY-RUN TELEGRAM ---\n" + text + "\n------------------------")
        return True
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        return r.status_code == 200
    except requests.RequestException as exc:
        print(f"[telegram] {exc}", file=sys.stderr)
        return False


def format_report(rows: list[Analysis], top: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    als = [r for r in rows if r.action == "AL"]
    izles = [r for r in rows if r.action == "İZLE"]
    gec = [r for r in rows if r.action == "GEÇ"]
    lines = [
        f"BINANCE DİP AL RADARI · {now}",
        f"Tarama: {len(rows)} aday | 🟢AL={len(als)} 🟡İZLE={len(izles)} 🔴GEÇ={len(gec)}",
        "",
    ]
    if als:
        lines.append("═══ 🟢 AL (dipten erken) ═══")
        for i, r in enumerate(als[:top], 1):
            lines.append(
                f"{i:2d}. {r.base:<8} skor={r.score:5.1f}  "
                f"%{r.change_24h_pct:+.1f}  ${r.price}  "
                f"dip+{r.layers.get('from_nd_low_pct')}%  "
                f"vol×{r.layers.get('vol_rise_5m')}  "
                f"RSI{r.layers.get('rsi_1h')}  "
                f"| {', '.join(r.reasons[:5])}"
            )
        lines.append("")
    else:
        lines.append("🟢 AL yok — dipte erken sinyal şu an yok")
        lines.append("")

    if izles:
        lines.append("── 🟡 İZLE ──")
        for i, r in enumerate(izles[: min(8, top)], 1):
            lines.append(
                f"{i:2d}. {r.base:<8} skor={r.score:5.1f}  %{r.change_24h_pct:+.1f}  "
                f"| {', '.join(r.reasons[:4])}"
            )
        lines.append("")

    # Uyarı: kaçırılmış rally (AR tipi)
    hot = sorted(gec, key=lambda x: x.change_24h_pct, reverse=True)[:5]
    if hot:
        lines.append("── 🔴 GEÇ (yükselmiş — AL değil) ──")
        for r in hot:
            lines.append(f"   {r.base:<8} %{r.change_24h_pct:+.1f}  skor={r.score}")
    return "\n".join(lines)


def format_telegram(als: list[Analysis]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not als:
        return f"<b>⚪ Binance dip radar</b>\nAL yok.\n<i>{now}</i>"
    lines = [f"<b>🟢 BINANCE DİP AL</b> · {len(als)} coin", f"<i>{now}</i>", ""]
    for r in als[:10]:
        lines.append(
            f"<b>{r.base}</b> skor <b>{r.score:.0f}</b>\n"
            f"Fiyat: {r.price} · 24s %{r.change_24h_pct:+.1f}\n"
            f"Dip+{r.layers.get('from_nd_low_pct')}% · "
            f"Hacim×{r.layers.get('vol_rise_5m')} · RSI {r.layers.get('rsi_1h')}\n"
            f"{', '.join(r.reasons[:6])}\n"
        )
    return "\n".join(lines)


def should_alert(state: dict[str, Any], bases: list[str], cooldown: int = 1800) -> list[str]:
    last_map: dict[str, str] = state.get("alerted") or {}
    now = datetime.now(timezone.utc)
    fresh: list[str] = []
    for b in bases:
        prev = last_map.get(b)
        if prev:
            try:
                if (now - datetime.fromisoformat(prev)).total_seconds() < cooldown:
                    continue
            except ValueError:
                pass
        fresh.append(b)
    return fresh


def run_scan(cfg: dict[str, Any], workers: int) -> list[Analysis]:
    print("[1/3] sembol + ticker…", flush=True)
    symbols = list_usdt_symbols(cfg)
    tickers = fetch_tickers(cfg)
    max_sym = int(cfg.get("max_symbols") or 0)
    min_qv = float(cfg.get("min_quote_volume_usdt") or 0)

    btc = tickers.get("BTCUSDT") or {}
    try:
        btc_chg = float(btc.get("priceChangePercent") or 0)
    except (TypeError, ValueError):
        btc_chg = 0.0

    ranked: list[str] = []
    for sym in symbols:
        t = tickers.get(sym)
        if not t:
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if min_qv and qv < min_qv:
            continue
        ranked.append(sym)
    ranked.sort(key=lambda s: float((tickers.get(s) or {}).get("quoteVolume") or 0), reverse=True)
    if max_sym > 0:
        ranked = ranked[:max_sym]

    print(f"[2/3] {len(ranked)} coin analiz (BTC 24s %{btc_chg:+.2f})…", flush=True)
    results: list[Analysis] = []

    def job(sym: str) -> Analysis | None:
        return analyze_symbol(sym, tickers[sym], cfg, btc_chg)

    with ThreadPoolExecutor(max_workers=max(4, workers)) as pool:
        futs = {pool.submit(job, s): s for s in ranked}
        done = 0
        for fut in as_completed(futs):
            done += 1
            if done % 50 == 0:
                print(f"  … {done}/{len(ranked)}", flush=True)
            try:
                row = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if row:
                results.append(row)

    print(f"[3/3] {len(results)} sonuç", flush=True)
    # AL önce, sonra skor
    order = {"AL": 0, "İZLE": 1, "GEÇ": 2, "YOK": 3}
    results.sort(key=lambda r: (order.get(r.action, 9), -r.score, -r.quote_volume_24h))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Binance dipten erken AL radarı")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--workers", type=int, default=0)
    args = parser.parse_args()

    cfg = load_json(Path(args.config))
    workers = int(args.workers or cfg.get("workers") or 16)
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    dry = bool(args.dry_run) or args.no_telegram or not (token and chat_id)
    if dry and not args.dry_run and not args.no_telegram:
        print("[info] Telegram env yok → dry-run", file=sys.stderr)

    poll = int(cfg.get("poll_seconds") or 180)
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"alerted": {}}

    while True:
        t0 = time.time()
        rows = run_scan(cfg, workers)
        # Rapor: YOK'ları gizle
        visible = [r for r in rows if r.action != "YOK"]
        print("\n" + format_report(visible, args.top) + "\n")

        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_sec": round(time.time() - t0, 1),
            "counts": {
                a: sum(1 for r in rows if r.action == a)
                for a in ("AL", "İZLE", "GEÇ", "YOK")
            },
            "signals": [asdict(r) for r in visible[:80]],
        }
        save_json(OUTPUT_PATH, payload)

        als = [r for r in rows if r.action == "AL"]
        fresh = should_alert(state, [r.base for r in als])
        alert_rows = [r for r in als if r.base in fresh]
        if alert_rows and not args.no_telegram:
            msg = format_telegram(alert_rows)
            if telegram_send(token or "", chat_id or "", msg, dry_run=dry):
                alerted = state.setdefault("alerted", {})
                now = datetime.now(timezone.utc).isoformat()
                for r in alert_rows:
                    alerted[r.base] = now
                # trim
                if len(alerted) > 500:
                    items = sorted(alerted.items(), key=lambda x: x[1], reverse=True)[:300]
                    state["alerted"] = dict(items)
                save_json(STATE_PATH, state)

        if args.once:
            break
        print(f"[sleep] {poll}s…")
        time.sleep(poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
