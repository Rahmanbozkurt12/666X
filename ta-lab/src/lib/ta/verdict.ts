import type { FullAnalysis, Signal } from "./types"

export type HourlyBand = {
  hour: number
  low: number
  high: number
  note: string
}

export type Verdict = {
  bias: "bull" | "bear" | "neutral"
  score: number // -100 .. +100
  confidence: number // 0..100
  headline: string
  bullets: string[]
  votes: { method: string; bias: "bull" | "bear" | "neutral"; weight: number; note: string }[]
  targets: { down5: number; up10: number; fib618: number; pivot: number }
  hourly: HourlyBand[]
}

function voteFromSignals(sigs: Signal[], method: string, weight: number) {
  if (!sigs.length) return { method, bias: "neutral" as const, weight, note: "sinyal yok" }
  const bull = sigs.filter((s) => s.bias === "bull").length
  const bear = sigs.filter((s) => s.bias === "bear").length
  if (bull === bear) return { method, bias: "neutral" as const, weight, note: `${bull}B/${bear}S` }
  return {
    method,
    bias: bull > bear ? ("bull" as const) : ("bear" as const),
    weight,
    note: `${Math.max(bull, bear)} baskın (${bull}B/${bear}S)`,
  }
}

export function buildVerdict(a: FullAnalysis): Verdict {
  const votes: Verdict["votes"] = []

  // 1 Price action / trend
  votes.push({
    method: "Price Action / Trend",
    bias: a.trend.direction === "up" ? "bull" : a.trend.direction === "down" ? "bear" : "neutral",
    weight: 18,
    note: a.trend.note,
  })

  // Order blocks near price
  const nearOB = a.orderBlocks.filter((o) => Math.abs(o.price - a.last) / a.last < 0.02)
  if (nearOB.length) {
    const bullish = nearOB.some((o) => o.label.includes("Bull"))
    const bearish = nearOB.some((o) => o.label.includes("Bear"))
    votes.push({
      method: "Order Block",
      bias: bullish && !bearish ? "bull" : bearish && !bullish ? "bear" : "neutral",
      weight: 10,
      note: nearOB.map((o) => o.label).join(", "),
    })
  } else {
    votes.push({ method: "Order Block", bias: "neutral", weight: 6, note: "yakın OB yok" })
  }

  // 2 Fib position vs 0.618
  const fib618 = a.fib.find((f) => f.ratio === 0.618)?.price ?? a.last
  const fibDist = (a.last - fib618) / a.last
  votes.push({
    method: "Fibonacci φ",
    bias: Math.abs(fibDist) < 0.004 ? "neutral" : a.last > fib618 ? "bull" : "bear",
    weight: 14,
    note: `0.618=${fib618.toFixed(4)} · fark ${(fibDist * 100).toFixed(2)}%`,
  })

  // Pivot
  const pp = a.pivots.PP
  votes.push({
    method: "Pivot",
    bias: a.last > pp * 1.002 ? "bull" : a.last < pp * 0.998 ? "bear" : "neutral",
    weight: 10,
    note: `PP ${pp.toFixed(4)}`,
  })

  // 3 Patterns
  votes.push(voteFromSignals(a.patterns, "Formasyonlar", 12))

  // 4 Elliott / Wyckoff
  const w = a.wyckoff.phase.toLowerCase()
  let wBias: "bull" | "bear" | "neutral" = "neutral"
  if (w.includes("markup") || w.includes("aküm")) wBias = "bull"
  if (w.includes("markdown") || w.includes("distrib")) wBias = "bear"
  votes.push({ method: "Wyckoff", bias: wBias, weight: 10, note: a.wyckoff.phase })

  const e = a.elliott.note.toLowerCase()
  votes.push({
    method: "Elliot",
    bias: e.includes("düzeltme") ? "neutral" : e.includes("itki") ? "bull" : "neutral",
    weight: 6,
    note: `${a.elliott.count} — ${a.elliott.note}`,
  })

  // 5 Candles
  votes.push(voteFromSignals(a.candles.slice(-5), "Mum yüzleri", 10))

  // 6 Indicators from signals
  const ind = a.signals.filter((s) =>
    ["RSI", "MACD", "Golden", "Death"].some((k) => s.title.includes(k)),
  )
  votes.push(voteFromSignals(ind, "İndikatörler", 14))

  // 7 Volume POC
  const poc = [...a.volumeProfile].sort((x, y) => y.volume - x.volume)[0]
  if (poc) {
    votes.push({
      method: "VPVR POC",
      bias: a.last > poc.price * 1.002 ? "bull" : a.last < poc.price * 0.998 ? "bear" : "neutral",
      weight: 8,
      note: `POC ${poc.price.toFixed(4)} (${poc.pct.toFixed(1)}%)`,
    })
  }

  let score = 0
  let tw = 0
  for (const v of votes) {
    const s = v.bias === "bull" ? 1 : v.bias === "bear" ? -1 : 0
    score += s * v.weight
    tw += v.weight
  }
  const norm = tw ? (score / tw) * 100 : 0
  const bias: Verdict["bias"] = norm >= 15 ? "bull" : norm <= -15 ? "bear" : "neutral"
  const confidence = Math.min(95, Math.round(Math.abs(norm) + votes.filter((v) => v.bias === bias).length * 4))

  const headline =
    bias === "bull"
      ? "Çoklu yöntem boğa ağırlıklı"
      : bias === "bear"
        ? "Çoklu yöntem ayı ağırlıklı"
        : "Çoklu yöntem nötr / kararsız"

  const bullets = votes
    .filter((v) => v.bias === bias || (bias === "neutral" && v.bias !== "neutral"))
    .slice(0, 5)
    .map((v) => `${v.method}: ${v.note}`)

  // Hourly bands from ATR + bias drift
  const atr = a.atr || a.last * 0.005
  const drift = bias === "bull" ? 0.15 : bias === "bear" ? -0.15 : 0
  const hourly: HourlyBand[] = []
  let center = a.last
  for (let h = 1; h <= 10; h++) {
    center = center * (1 + (drift * atr) / a.last)
    const half = atr * (0.55 + h * 0.04)
    hourly.push({
      hour: h,
      low: center - half,
      high: center + half,
      note: h <= 3 ? "yakın volatilite" : h <= 7 ? "orta bant" : "genişleyen belirsizlik",
    })
  }

  return {
    bias,
    score: Math.round(norm),
    confidence,
    headline,
    bullets,
    votes,
    targets: {
      down5: a.last * 0.95,
      up10: a.last * 1.1,
      fib618,
      pivot: pp,
    },
    hourly,
  }
}
