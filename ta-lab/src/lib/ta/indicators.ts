import type { Candle } from "./types"

export function sma(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = Array(values.length).fill(null)
  let sum = 0
  for (let i = 0; i < values.length; i++) {
    sum += values[i]
    if (i >= period) sum -= values[i - period]
    if (i >= period - 1) out[i] = sum / period
  }
  return out
}

export function ema(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = Array(values.length).fill(null)
  if (values.length < period) return out
  const k = 2 / (period + 1)
  let prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period
  out[period - 1] = prev
  for (let i = period; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k)
    out[i] = prev
  }
  return out
}

export function rsi(closes: number[], period = 14): (number | null)[] {
  const out: (number | null)[] = Array(closes.length).fill(null)
  if (closes.length <= period) return out
  let gain = 0
  let loss = 0
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1]
    if (d >= 0) gain += d
    else loss -= d
  }
  let avgGain = gain / period
  let avgLoss = loss / period
  out[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss)
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1]
    const g = d > 0 ? d : 0
    const l = d < 0 ? -d : 0
    avgGain = (avgGain * (period - 1) + g) / period
    avgLoss = (avgLoss * (period - 1) + l) / period
    out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss)
  }
  return out
}

export function macd(closes: number[], fast = 12, slow = 26, signalPeriod = 9) {
  const emaFast = ema(closes, fast)
  const emaSlow = ema(closes, slow)
  const macdLine: (number | null)[] = closes.map((_, i) =>
    emaFast[i] != null && emaSlow[i] != null ? (emaFast[i] as number) - (emaSlow[i] as number) : null,
  )
  const macdVals = macdLine.map((v) => v ?? 0)
  const firstValid = macdLine.findIndex((v) => v != null)
  const signalFull = ema(macdVals.slice(firstValid), signalPeriod)
  const signal: (number | null)[] = Array(closes.length).fill(null)
  for (let i = 0; i < signalFull.length; i++) {
    signal[firstValid + i] = signalFull[i]
  }
  const hist: (number | null)[] = closes.map((_, i) =>
    macdLine[i] != null && signal[i] != null ? (macdLine[i] as number) - (signal[i] as number) : null,
  )
  return { macdLine, signal, hist }
}

export function bollinger(closes: number[], period = 20, mult = 2) {
  const mid = sma(closes, period)
  const upper: (number | null)[] = Array(closes.length).fill(null)
  const lower: (number | null)[] = Array(closes.length).fill(null)
  for (let i = period - 1; i < closes.length; i++) {
    const slice = closes.slice(i - period + 1, i + 1)
    const mean = mid[i] as number
    const variance = slice.reduce((a, v) => a + (v - mean) ** 2, 0) / period
    const sd = Math.sqrt(variance)
    upper[i] = mean + mult * sd
    lower[i] = mean - mult * sd
  }
  return { upper, mid, lower }
}

export function vwapSeries(candles: Candle[]): (number | null)[] {
  const out: (number | null)[] = []
  let pv = 0
  let vol = 0
  // reset each UTC day for spot-style VWAP
  let day = -1
  for (const c of candles) {
    const d = Math.floor(c.time / 86400)
    if (d !== day) {
      day = d
      pv = 0
      vol = 0
    }
    const typical = (c.high + c.low + c.close) / 3
    pv += typical * c.volume
    vol += c.volume
    out.push(vol > 0 ? pv / vol : null)
  }
  return out
}

export function atr(candles: Candle[], period = 14): number {
  if (candles.length < period + 1) return 0
  const trs: number[] = []
  for (let i = 1; i < candles.length; i++) {
    const c = candles[i]
    const p = candles[i - 1]
    trs.push(Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close)))
  }
  const slice = trs.slice(-period)
  return slice.reduce((a, b) => a + b, 0) / slice.length
}

export function toLine(
  candles: Candle[],
  values: (number | null)[],
): { time: number; value: number }[] {
  const out: { time: number; value: number }[] = []
  for (let i = 0; i < candles.length; i++) {
    const v = values[i]
    if (v != null && Number.isFinite(v)) out.push({ time: candles[i].time, value: v })
  }
  return out
}
