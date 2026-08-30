import type { Candle, FullAnalysis, Level, Signal } from "./types"
import {
  atr,
  bollinger,
  ema,
  macd,
  rsi,
  sma,
  toLine,
  vwapSeries,
} from "./indicators"
import {
  candlePatterns,
  chartPatterns,
  classicPivots,
  detectOrderBlocks,
  elliottHint,
  fibonacciLevels,
  gannLevels,
  supportResistance,
  trendChannel,
  volumeProfile,
  wyckoffPhase,
} from "./structure"

export function analyze(symbol: string, interval: string, candles: Candle[]): FullAnalysis {
  const closes = candles.map((c) => c.close)
  const last = closes[closes.length - 1]
  const atrVal = atr(candles, 14)

  const ema7 = ema(closes, 7)
  const ema25 = ema(closes, 25)
  const ema99 = ema(closes, 99)
  const sma20 = sma(closes, 20)
  const bb = bollinger(closes, 20, 2)
  const vwap = vwapSeries(candles)
  const rsiVals = rsi(closes, 14)
  const m = macd(closes)

  const fib = fibonacciLevels(candles)
  const pivots = classicPivots(candles)
  const gann = gannLevels(candles)
  const sr = supportResistance(candles)
  const orderBlocks = detectOrderBlocks(candles)
  const candlesSig = candlePatterns(candles)
  const patterns = chartPatterns(candles)
  const vp = volumeProfile(candles)
  const wyckoff = wyckoffPhase(candles)
  const elliott = elliottHint(candles)
  const trend = trendChannel(candles)

  const levels: Level[] = [
    ...sr,
    ...orderBlocks,
    ...fib.primary.slice(0, 8).map((f) => ({
      price: f.price,
      label: f.label,
      kind: "fib" as const,
      meta: `φ ailesi ${f.ratio}`,
    })),
    ...Object.entries(pivots).map(([k, price]) => ({
      price,
      label: `Pivot ${k}`,
      kind: "pivot" as const,
    })),
    ...gann.slice(0, 6).map((g) => ({
      price: g.price,
      label: `Gann ${g.angle}`,
      kind: "gann" as const,
    })),
  ]

  const lastVwap = vwap[vwap.length - 1]
  if (lastVwap != null) {
    levels.push({ price: lastVwap, label: "VWAP", kind: "vwap" })
  }
  const e7 = ema7[ema7.length - 1]
  const e25 = ema25[ema25.length - 1]
  const e99 = ema99[ema99.length - 1]
  if (e7 != null) levels.push({ price: e7, label: "EMA7", kind: "ema" })
  if (e25 != null) levels.push({ price: e25, label: "EMA25", kind: "ema" })
  if (e99 != null) levels.push({ price: e99, label: "EMA99", kind: "ema" })

  const signals: Signal[] = []
  const lastRsi = rsiVals[rsiVals.length - 1]
  if (lastRsi != null) {
    if (lastRsi >= 70)
      signals.push({ time: candles.at(-1)!.time, title: "RSI aşırı alım", detail: `RSI ${lastRsi.toFixed(1)}`, bias: "bear" })
    else if (lastRsi <= 30)
      signals.push({ time: candles.at(-1)!.time, title: "RSI aşırı satım", detail: `RSI ${lastRsi.toFixed(1)}`, bias: "bull" })
    else
      signals.push({ time: candles.at(-1)!.time, title: "RSI nötr", detail: `RSI ${lastRsi.toFixed(1)}`, bias: "neutral" })
  }
  if (e7 != null && e25 != null) {
    if (e7 > e25 && ema7[ema7.length - 2]! <= ema25[ema25.length - 2]!) {
      signals.push({ time: candles.at(-1)!.time, title: "Golden Cross (EMA7/25)", detail: "Kısa trend yukarı kesişim", bias: "bull" })
    } else if (e7 < e25 && ema7[ema7.length - 2]! >= ema25[ema25.length - 2]!) {
      signals.push({ time: candles.at(-1)!.time, title: "Death Cross (EMA7/25)", detail: "Kısa trend aşağı kesişim", bias: "bear" })
    }
  }
  const hist = m.hist[m.hist.length - 1]
  const prevHist = m.hist[m.hist.length - 2]
  if (hist != null && prevHist != null) {
    if (prevHist < 0 && hist > 0)
      signals.push({ time: candles.at(-1)!.time, title: "MACD bullish flip", detail: "Histogram +", bias: "bull" })
    if (prevHist > 0 && hist < 0)
      signals.push({ time: candles.at(-1)!.time, title: "MACD bearish flip", detail: "Histogram −", bias: "bear" })
  }

  // POC from volume profile
  const poc = [...vp].sort((a, b) => b.volume - a.volume)[0]
  if (poc) {
    levels.push({ price: poc.price, label: "VPVR POC", kind: "other", meta: `${poc.pct.toFixed(1)}% hacim` })
  }

  return {
    symbol,
    interval,
    last,
    atr: atrVal,
    atrPct: (atrVal / last) * 100,
    levels: levels.sort((a, b) => b.price - a.price),
    signals: [...signals, ...candlesSig.slice(-3), ...patterns],
    series: {
      ema7: toLine(candles, ema7),
      ema25: toLine(candles, ema25),
      ema99: toLine(candles, ema99),
      sma20: toLine(candles, sma20),
      bbUpper: toLine(candles, bb.upper),
      bbMid: toLine(candles, bb.mid),
      bbLower: toLine(candles, bb.lower),
      vwap: toLine(candles, vwap),
      rsi: toLine(candles, rsiVals),
      macd: toLine(candles, m.macdLine),
      signal: toLine(candles, m.signal),
      hist: toLine(candles, m.hist),
    },
    fib: fib.primary,
    pivots,
    gann,
    candles: candlesSig,
    patterns,
    volumeProfile: vp,
    wyckoff,
    elliott,
    trend,
    orderBlocks,
  }
}
