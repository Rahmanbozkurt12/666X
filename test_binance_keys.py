#!/usr/bin/env python3
"""
Binance API key/secret hızlı test.
Kullanım (allbinancee.py ile aynı klasörde):
  python test_binance_keys.py
  python test_binance_keys.py --file allbinancee.py
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests


def clean(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    for ch in (" ", "\t", "\r", "\n", "\u200b", "\ufeff"):
        s = s.replace(ch, "")
    return s


def load_from_py(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    def grab(name: str) -> str:
        m = re.search(
            rf'{name}\s*=\s*([\"\'])(?P<v>.*?)\\1',
            text,
            flags=re.DOTALL,
        )
        return clean(m.group("v") if m else "")
    return grab("BINANCE_API_KEY_HARDCODE"), grab("BINANCE_API_SECRET_HARDCODE")


def test(key: str, secret: str) -> int:
    key, secret = clean(key), clean(secret)
    print(f"key len={len(key)}  son4=…{key[-4:] if len(key)>=4 else '?'}")
    print(f"secret len={len(secret)}  son4=…{secret[-4:] if len(secret)>=4 else '?'}")
    if not key or not secret:
        print("HATA: key/secret boş")
        return 1
    if key == secret:
        print("HATA: API Key ile Secret AYNI — yer değiştirmiş veya yanlış yapıştırılmış")
        return 1

    base = "https://api.binance.com"
    t = requests.get(f"{base}/api/v3/time", timeout=15).json()["serverTime"]
    params = {"timestamp": t, "recvWindow": 60000}
    query = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    r = requests.get(
        f"{base}/api/v3/account?{query}&signature={sig}",
        headers={"X-MBX-APIKEY": key},
        timeout=30,
    )
    print(f"HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code == 200:
        bal = r.json().get("balances") or []
        usdt = next((b for b in bal if b.get("asset") == "USDT"), None)
        print("OK — imza geçerli")
        if usdt:
            print(f"USDT free={usdt.get('free')} locked={usdt.get('locked')}")
        return 0
    if "-1022" in r.text:
        print(
            "\n-1022 = SECRET KEY Binance'teki ile UYUŞMUYOR.\n"
            "Çözüm: API Management → Edit → yeni Secret kopyala → dosyaya yapıştır → kaydet.\n"
            "API Key satırına Secret yazma."
        )
    return 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="", help="allbinancee.py veya binance_dip_buy_radar.py")
    args = ap.parse_args()
    here = Path(__file__).resolve().parent
    candidates = []
    if args.file:
        candidates.append(Path(args.file))
    candidates += [
        here / "allbinancee.py",
        here / "binance_dip_buy_radar.py",
        here.parent / "binance_dip_buy_radar.py",
        Path.cwd() / "allbinancee.py",
        Path.cwd() / "binance_dip_buy_radar.py",
    ]
    src = next((p for p in candidates if p.is_file()), None)
    if not src:
        print("Bot py dosyası bulunamadı. --file yolunu ver.")
        return 1
    print(f"dosya: {src}")
    key, secret = load_from_py(src)
    return test(key, secret)


if __name__ == "__main__":
    raise SystemExit(main())
