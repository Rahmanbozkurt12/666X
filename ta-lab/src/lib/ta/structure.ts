import type { Candle, Level, Signal, VolumeBin } from "./types"

const PHI = 1.6180339887
const FIB_RETR = [0, 0.236, 0.382, 0.5, 0.618, 0.65, 0.786, 1]
const FIB_EXT = [1.272, 1.414, 1.618, 2.0, 2.618]

export function swingHighLow(candles: Candle[], lookback = 120) {
  const slice = candles.slice(-lookback)
  let hi = slice[0]
  let lo = slice[0]
  for (const c of slice) {
    if (c.high > hi.high) hi = c
    if (c.low < lo.low) lo = c
  }
  return { high: hi.high, low: lo.low, highTime: hi.time, lowTime: lo.time }
}

export function fibonacciLevels(candles: Candle[]) {
  const { high, low, highTime, lowTime } = swingHighLow(candles)
  const range = high - low
  const downtrend = highTime < lowTime // high first then low = dump, retrace up from low
  // Always compute classic dump retracement from high→low AND bounce from low→high
  const dump = FIB_RETR.map((r) => ({
    ratio: r,
    price: high - range * r,
    label: `Fib dump ${r}`,
  }))
  const bounce = [...FIB_RETR, ...FIB_EXT].map((r) => ({
    ratio: r,
    price: low + range * r,
    label: r <= 1 ? `Fib bounce ${r}` : `Fib ext ${r}`,
  }))
  // Prefer primary set based on structure
  const primary = downtrend ? bounce : dump
  return { high, low, range, primary, dump, bounce, phi: PHI }
}

export function classicPivots(candles: Candle[]) {
  const prev = candles[candles.length - 2] ?? candles[candles.length - 1]
  const pp = (prev.high + prev.low + prev.close) / 3
  const r1 = 2 * pp - prev.low
  const s1 = 2 * pp - prev.high
  const r2 = pp + (prev.high - prev.low)
  const s2 = pp - (prev.high - prev.low)
  const r3 = prev.high + 2 * (pp - prev.low)
  const s3 = prev.low - 2 * (prev.high - pp)
  return { PP: pp, R1: r1, R2: r2, R3: r3, S1: s1, S2: s2, S3: s3 }
}

/** Simplified Gann square-of-range angles from swing low */
export function gannLevels(candles: Candle[]) {
  const { high, low } = swingHighLow(candles)
  const range = high - low
  const angles = [
    { angle: "1x8", mult: 0.125 },
    { angle: "1x4", mult: 0.25 },
    { angle: "1x3", mult: 1 / 3 },
    { angle: "1x2", mult: 0.5 },
    { angle: "1x1 (45°)", mult: 1 },
    { angle: "2x1", mult: 2 },
    { angle: "3x1", mult: 3 },
    { angle: "4x1", mult: 4 },
  ]
  const fracs = [0.125, 0.25, 0.333, 0.5, 1, 1.125, 1.25, 1.5]
  return angles.map((a, i) => ({
    angle: a.angle,
    price: low + range * fracs[i],
  }))
}

export function supportResistance(candles: Candle[], bins = 40): Level[] {
  const slice = candles.slice(-150)
  const lo = Math.min(...slice.map((c) => c.low))
  const hi = Math.max(...slice.map((c) => c.high))
  const step = (hi - lo) / bins || 1
  const scores = new Array(bins).fill(0)
  for (const c of slice) {
    const touchLo = Math.min(bins - 1, Math.max(0, Math.floor((c.low - lo) / step)))
    const touchHi = Math.min(bins - 1, Math.max(0, Math.floor((c.high - lo) / step)))
    scores[touchLo] += 1 + c.volume / 1e6
    scores[touchHi] += 1 + c.volume / 1e6
  }
  const ranked = scores
    .map((s, i) => ({ i, s, price: lo + (i + 0.5) * step }))
    .sort((a, b) => b.s - a.s)
    .slice(0, 6)
  const last = candles[candles.length - 1].close
  return ranked.map((r) => ({
    price: r.price,
    label: r.price < last ? "Destek" : "Direnç",
    kind: r.price < last ? "support" : "resistance",
    meta: `skor ${r.s.toFixed(1)}`,
  }))
}

export function detectOrderBlocks(candles: Candle[]): Level[] {
  const out: Level[] = []
  const slice = candles.slice(-80)
  for (let i = 2; i < slice.length - 1; i++) {
    const b = slice[i]
    const c = slice[i + 1]
    const move = Math.abs(c.close - b.open) / b.open
    // impulsive bullish after bearish candle → bullish OB
    if (b.close < b.open && c.close > c.open && c.close > b.high && move > 0.008) {
      out.push({
        price: (b.open + b.close) / 2,
        label: "Bull Order Block",
        kind: "support",
        meta: new Date(b.time * 1000).toISOString().slice(0, 16),
      })
    }
    if (b.close > b.open && c.close < c.open && c.close < b.low && move > 0.008) {
      out.push({
        price: (b.open + b.close) / 2,
        label: "Bear Order Block",
        kind: "resistance",
        meta: new Date(b.time * 1000).toISOString().slice(0, 16),
      })
    }
  }
  return out.slice(-4)
}

export function candlePatterns(candles: Candle[]): Signal[] {
  const out: Signal[] = []
  for (let i = 2; i < candles.length; i++) {
    const a = candles[i - 2]
    const b = candles[i - 1]
    const c = candles[i]
    const body = Math.abs(c.close - c.open)
    const range = c.high - c.low || 1e-9
    const lower = Math.min(c.open, c.close) - c.low
    const upper = c.high - Math.max(c.open, c.close)

    if (body / range < 0.1) {
      out.push({ time: c.time, title: "Doji", detail: "Kararsızlık / denge", bias: "neutral" })
    }
    if (lower > body * 2 && upper < body * 0.5 && c.close >= c.open) {
      out.push({ time: c.time, title: "Hammer", detail: "Potansiyel dip dönüş", bias: "bull" })
    }
    if (upper > body * 2 && lower < body * 0.5 && c.close <= c.open) {
      out.push({ time: c.time, title: "Shooting Star", detail: "Potansiyel tepe reddi", bias: "bear" })
    }
    if (b.close < b.open && c.close > c.open && c.open <= b.close && c.close >= b.open) {
      out.push({ time: c.time, title: "Bullish Engulfing", detail: "Yutan alım mumu", bias: "bull" })
    }
    if (b.close > b.open && c.close < c.open && c.open >= b.close && c.close <= b.open) {
      out.push({ time: c.time, title: "Bearish Engulfing", detail: "Yutan satım mumu", bias: "bear" })
    }
    // morning star crude
    if (
      a.close < a.open &&
      Math.abs(b.close - b.open) / (b.high - b.low || 1) < 0.3 &&
      c.close > c.open &&
      c.close > (a.open + a.close) / 2
    ) {
      out.push({ time: c.time, title: "Morning Star", detail: "3 mumlu dip dönüş", bias: "bull" })
    }
  }
  return out.slice(-12)
}

export function chartPatterns(candles: Candle[]): Signal[] {
  const out: Signal[] = []
  const n = candles.length
  if (n < 30) return out
  const closes = candles.map((c) => c.close)
  const last = closes[n - 1]
  const mid = closes[n - 15]
  const early = closes[n - 30]

  // Double bottom / top heuristic
  const lows = candles.slice(-40).map((c) => c.low)
  const highs = candles.slice(-40).map((c) => c.high)
  const min1 = Math.min(...lows.slice(0, 20))
  const min2 = Math.min(...lows.slice(20))
  if (Math.abs(min1 - min2) / last < 0.01 && last > (min1 + min2) / 2 * 1.01) {
    out.push({
      time: candles[n - 1].time,
      title: "Çift Dip (W) adayı",
      detail: `Dipler ~${min1.toFixed(4)} / ${min2.toFixed(4)}`,
      bias: "bull",
    })
  }
  const max1 = Math.max(...highs.slice(0, 20))
  const max2 = Math.max(...highs.slice(20))
  if (Math.abs(max1 - max2) / last < 0.01 && last < (max1 + max2) / 2 * 0.99) {
    out.push({
      time: candles[n - 1].time,
      title: "Çift Tepe (M) adayı",
      detail: `Tepeler ~${max1.toFixed(4)} / ${max2.toFixed(4)}`,
      bias: "bear",
    })
  }

  // Flag: sharp move then consolidation
  const impulse = (mid - early) / early
  const compress =
    Math.max(...candles.slice(-10).map((c) => c.high)) - Math.min(...candles.slice(-10).map((c) => c.low))
  const priorRange =
    Math.max(...candles.slice(-25, -10).map((c) => c.high)) -
    Math.min(...candles.slice(-25, -10).map((c) => c.low))
  if (Math.abs(impulse) > 0.03 && compress < priorRange * 0.45) {
    out.push({
      time: candles[n - 1].time,
      title: impulse > 0 ? "Boğa Bayrağı adayı" : "Ayı Bayrağı adayı",
      detail: "Sert hareket sonrası sıkışma",
      bias: impulse > 0 ? "bull" : "bear",
    })
  }

  // Triangle: narrowing highs/lows
  const h1 = Math.max(...candles.slice(-30, -15).map((c) => c.high))
  const h2 = Math.max(...candles.slice(-15).map((c) => c.high))
  const l1 = Math.min(...candles.slice(-30, -15).map((c) => c.low))
  const l2 = Math.min(...candles.slice(-15).map((c) => c.low))
  if (h2 < h1 && l2 > l1) {
    out.push({
      time: candles[n - 1].time,
      title: "Üçgen / sıkışma",
      detail: "Yüksekler alçalıyor, dipler yükseliyor",
      bias: "neutral",
    })
  }

  // OBO very rough: left shoulder, head, right shoulder
  const win = candles.slice(-45)
  if (win.length >= 45) {
    const p1 = Math.max(...win.slice(0, 15).map((c) => c.high))
    const head = Math.max(...win.slice(15, 30).map((c) => c.high))
    const p3 = Math.max(...win.slice(30).map((c) => c.high))
    if (head > p1 * 1.01 && head > p3 * 1.01 && Math.abs(p1 - p3) / head < 0.02) {
      out.push({
        time: candles[n - 1].time,
        title: "OBO (Omuz-Baş-Omuz) adayı",
        detail: "Tepe formasyonu ihtimali",
        bias: "bear",
      })
    }
  }
  return out
}

export function volumeProfile(candles: Candle[], bins = 24): VolumeBin[] {
  const slice = candles.slice(-120)
  const lo = Math.min(...slice.map((c) => c.low))
  const hi = Math.max(...slice.map((c) => c.high))
  const step = (hi - lo) / bins || 1
  const vols = new Array(bins).fill(0)
  for (const c of slice) {
    const idx = Math.min(bins - 1, Math.max(0, Math.floor(((c.high + c.low) / 2 - lo) / step)))
    vols[idx] += c.volume
  }
  const total = vols.reduce((a, b) => a + b, 0) || 1
  return vols.map((v, i) => ({
    price: lo + (i + 0.5) * step,
    volume: v,
    pct: (v / total) * 100,
  }))
}

export function wyckoffPhase(candles: Candle[]) {
  const slice = candles.slice(-60)
  const first = slice.slice(0, 20)
  const mid = slice.slice(20, 40)
  const last = slice.slice(40)
  const avg = (arr: Candle[]) => arr.reduce((a, c) => a + c.close, 0) / arr.length
  const vol = (arr: Candle[]) => arr.reduce((a, c) => a + c.volume, 0) / arr.length
  const a1 = avg(first)
  const a2 = avg(mid)
  const a3 = avg(last)
  const range =
    Math.max(...slice.map((c) => c.high)) - Math.min(...slice.map((c) => c.low))
  const midRange =
    (Math.max(...mid.map((c) => c.high)) + Math.min(...mid.map((c) => c.low))) / 2
  const compress =
    Math.max(...last.map((c) => c.high)) - Math.min(...last.map((c) => c.low)) < range * 0.35

  if (a2 < a1 * 0.98 && compress && vol(last) < vol(first)) {
    return { phase: "Akümülasyon (Wyckoff B/C)", note: "Satış sonrası yatay + hacim düşüşü" }
  }
  if (a2 > a1 * 1.02 && compress && vol(last) > vol(mid) * 0.9 && a3 < a2) {
    return { phase: "Distribüsyon (Wyckoff)", note: "Yükseliş sonrası yatay dağıtım izleri" }
  }
  if (a3 > a2 && a2 > a1) return { phase: "Markup", note: "Yükseliş evresi" }
  if (a3 < a2 && a2 < a1) return { phase: "Markdown", note: "Düşüş evresi" }
  return { phase: "Nötr / Range", note: `Orta seviye ~${midRange.toFixed(4)}` }
}

export function elliottHint(candles: Candle[]) {
  // Heuristic zigzag swing count — educational, not a full Elliott engine
  const pivots: { price: number; type: "h" | "l" }[] = []
  for (let i = 2; i < candles.length - 2; i++) {
    const c = candles[i]
    if (c.high > candles[i - 1].high && c.high > candles[i - 2].high && c.high > candles[i + 1].high && c.high > candles[i + 2].high) {
      pivots.push({ price: c.high, type: "h" })
    }
    if (c.low < candles[i - 1].low && c.low < candles[i - 2].low && c.low < candles[i + 1].low && c.low < candles[i + 2].low) {
      pivots.push({ price: c.low, type: "l" })
    }
  }
  const recent = pivots.slice(-5)
  if (recent.length >= 5) {
    return {
      count: "5 swing görüldü",
      note: "İtki (1-5) veya A-B-C düzeltme adayı — doğrulama gerekir",
    }
  }
  if (recent.length >= 3) {
    return { count: "3 swing", note: "Düzeltme (A-B-C) ihtimali" }
  }
  return { count: "Belirsiz", note: "Yeterli swing yok / yatay piyasa" }
}

export function trendChannel(candles: Candle[]) {
  const slice = candles.slice(-40)
  const first = slice[0].close
  const last = slice[slice.length - 1].close
  const chg = (last - first) / first
  if (chg > 0.02) return { direction: "up" as const, note: "Yükselen kanal / kısa trend yukarı" }
  if (chg < -0.02) return { direction: "down" as const, note: "Düşen kanal / kısa trend aşağı" }
  return { direction: "side" as const, note: "Yatay / kanal sıkışması" }
}
