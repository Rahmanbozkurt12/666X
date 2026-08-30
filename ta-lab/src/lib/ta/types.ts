export type Candle = {
  time: number // unix seconds
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export type Level = {
  price: number
  label: string
  kind: "support" | "resistance" | "pivot" | "fib" | "gann" | "vwap" | "ema" | "other"
  meta?: string
}

export type Signal = {
  time: number
  title: string
  detail: string
  bias: "bull" | "bear" | "neutral"
}

export type VolumeBin = {
  price: number
  volume: number
  pct: number
}

export type FullAnalysis = {
  symbol: string
  interval: string
  last: number
  atr: number
  atrPct: number
  levels: Level[]
  signals: Signal[]
  series: {
    ema7: { time: number; value: number }[]
    ema25: { time: number; value: number }[]
    ema99: { time: number; value: number }[]
    sma20: { time: number; value: number }[]
    bbUpper: { time: number; value: number }[]
    bbMid: { time: number; value: number }[]
    bbLower: { time: number; value: number }[]
    vwap: { time: number; value: number }[]
    rsi: { time: number; value: number }[]
    macd: { time: number; value: number }[]
    signal: { time: number; value: number }[]
    hist: { time: number; value: number }[]
  }
  fib: { ratio: number; price: number; label: string }[]
  pivots: Record<string, number>
  gann: { angle: string; price: number }[]
  candles: Signal[]
  patterns: Signal[]
  volumeProfile: VolumeBin[]
  wyckoff: { phase: string; note: string }
  elliott: { count: string; note: string }
  trend: { direction: "up" | "down" | "side"; note: string }
  orderBlocks: Level[]
}
