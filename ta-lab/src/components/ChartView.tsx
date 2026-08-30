import { useEffect, useRef } from "react"
import {
  createChart,
  CandlestickSeries,
  LineSeries,
  type IChartApi,
  type CandlestickData,
  type LineData,
  ColorType,
  CrosshairMode,
} from "lightweight-charts"
import type { Candle, FullAnalysis } from "../lib/ta/types"

type Props = {
  candles: Candle[]
  analysis: FullAnalysis | null
  show: {
    ema: boolean
    bb: boolean
    vwap: boolean
    fib: boolean
  }
}

export function ChartView({ candles, analysis, show }: Props) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!ref.current) return
    const chart: IChartApi = createChart(ref.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#b8b0a0",
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
      },
      grid: {
        vertLines: { color: "rgba(184,176,160,0.08)" },
        horzLines: { color: "rgba(184,176,160,0.08)" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "rgba(184,176,160,0.2)" },
      timeScale: { borderColor: "rgba(184,176,160,0.2)", timeVisible: true },
      autoSize: true,
    })

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#d4a017",
      downColor: "#c45c26",
      borderUpColor: "#d4a017",
      borderDownColor: "#c45c26",
      wickUpColor: "#d4a017",
      wickDownColor: "#c45c26",
    })

    candleSeries.setData(
      candles.map(
        (c) =>
          ({
            time: c.time as CandlestickData["time"],
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
          }) satisfies CandlestickData,
      ),
    )

    const addLine = (pts: { time: number; value: number }[], color: string, width = 1) => {
      const s = chart.addSeries(LineSeries, {
        color,
        lineWidth: width as 1 | 2 | 3 | 4,
        priceLineVisible: false,
        lastValueVisible: false,
      })
      s.setData(pts.map((p) => ({ time: p.time as LineData["time"], value: p.value })))
    }

    if (analysis) {
      if (show.ema) {
        addLine(analysis.series.ema7, "#f0c75e", 2)
        addLine(analysis.series.ema25, "#e8a07a", 2)
        addLine(analysis.series.ema99, "#8f9e8b", 2)
      }
      if (show.bb) {
        addLine(analysis.series.bbUpper, "rgba(140,160,180,0.7)")
        addLine(analysis.series.bbMid, "rgba(140,160,180,0.4)")
        addLine(analysis.series.bbLower, "rgba(140,160,180,0.7)")
      }
      if (show.vwap) addLine(analysis.series.vwap, "#6ec6c0", 2)
      if (show.fib) {
        for (const f of analysis.fib.filter((x) =>
          [0, 0.382, 0.5, 0.618, 1, 1.272, 1.618].includes(x.ratio),
        )) {
          candleSeries.createPriceLine({
            price: f.price,
            color: f.ratio === 0.618 || f.ratio === 1.618 ? "#d4a017" : "rgba(212,160,23,0.35)",
            lineWidth: f.ratio === 0.618 || f.ratio === 1.618 ? 2 : 1,
            lineStyle: 2,
            axisLabelVisible: true,
            title: String(f.ratio),
          })
        }
      }
    }

    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [candles, analysis, show])

  return <div className="chart-shell" ref={ref} />
}
