import type { Candle } from "./ta/types"

const BASES = [
  "https://www.binance.com",
  "https://data-api.binance.vision",
]

export async function fetchKlines(
  symbol: string,
  interval: string,
  limit = 500,
): Promise<Candle[]> {
  let lastErr: unknown
  for (const base of BASES) {
    try {
      const url = `${base}/api/v3/klines?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=${limit}`
      const res = await fetch(url)
      if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
      const raw = (await res.json()) as (string | number)[][]
      return raw.map((c) => ({
        time: Math.floor(Number(c[0]) / 1000),
        open: Number(c[1]),
        high: Number(c[2]),
        low: Number(c[3]),
        close: Number(c[4]),
        volume: Number(c[5]),
      }))
    } catch (e) {
      lastErr = e
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error("Binance kline fetch failed")
}

export const INTERVALS = ["15m", "1h", "4h", "1d"] as const
export const DEFAULT_SYMBOLS = [
  "BTCUSDT",
  "ETHUSDT",
  "SOLUSDT",
  "BNBUSDT",
  "XRPUSDT",
  "DOGEUSDT",
  "SUSDT",
  "ENAUSDT",
  "SUIUSDT",
  "1000PEPEUSDT",
]
