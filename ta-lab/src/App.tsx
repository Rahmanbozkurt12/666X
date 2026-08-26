import { useEffect, useMemo, useState } from "react"
import { DEFAULT_SYMBOLS, INTERVALS, fetchKlines } from "./lib/binance"
import { analyze } from "./lib/ta/analyze"
import type { Candle } from "./lib/ta/types"
import { ChartView } from "./components/ChartView"
import { AnalysisPanels } from "./components/AnalysisPanels"
import "./App.css"

export default function App() {
  const [symbol, setSymbol] = useState("BTCUSDT")
  const [custom, setCustom] = useState("")
  const [interval, setInterval] = useState<(typeof INTERVALS)[number]>("1h")
  const [candles, setCandles] = useState<Candle[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [show, setShow] = useState({ ema: true, bb: true, vwap: true, fib: true })

  const activeSymbol = (custom.trim().toUpperCase() || symbol).replace("/", "")

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError(null)
    fetchKlines(activeSymbol, interval, 500)
      .then((c) => {
        if (alive) setCandles(c)
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "Veri alınamadı")
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [activeSymbol, interval])

  const analysis = useMemo(
    () => (candles.length ? analyze(activeSymbol, interval, candles) : null),
    [candles, activeSymbol, interval],
  )

  return (
    <div className="app">
      <header className="hero">
        <div className="hero-bg" />
        <div className="hero-copy">
          <p className="brand">RATIO</p>
          <h1>Tek ekranda trader araç seti</h1>
          <p className="lede">
            Price action, Fib/Gann/Pivot, formasyon, Elliot/Wyckoff, mumlar, indikatörler ve
            volume profile — Binance mumlarıyla.
          </p>
        </div>
      </header>

      <div className="toolbar">
        <label>
          Sembol
          <select value={symbol} onChange={(e) => { setSymbol(e.target.value); setCustom("") }}>
            {DEFAULT_SYMBOLS.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
        <label>
          Özel
          <input
            placeholder="ENAUSDT"
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
          />
        </label>
        <label>
          Zaman
          <select value={interval} onChange={(e) => setInterval(e.target.value as typeof interval)}>
            {INTERVALS.map((i) => (
              <option key={i} value={i}>{i}</option>
            ))}
          </select>
        </label>
        <div className="toggles">
          {(["ema", "bb", "vwap", "fib"] as const).map((k) => (
            <label key={k} className="chk">
              <input
                type="checkbox"
                checked={show[k]}
                onChange={() => setShow((s) => ({ ...s, [k]: !s[k] }))}
              />
              {k.toUpperCase()}
            </label>
          ))}
        </div>
        <button
          type="button"
          className="refresh"
          onClick={() => {
            setLoading(true)
            fetchKlines(activeSymbol, interval, 500)
              .then(setCandles)
              .catch((e) => setError(e instanceof Error ? e.message : "Hata"))
              .finally(() => setLoading(false))
          }}
        >
          Yenile
        </button>
      </div>

      {error && <div className="banner err">{error}</div>}
      {loading && <div className="banner">Yükleniyor…</div>}

      {analysis && (
        <div className="stats">
          <div>
            <em>{activeSymbol}</em>
            <strong>{analysis.last >= 1 ? analysis.last.toFixed(4) : analysis.last.toFixed(6)}</strong>
          </div>
          <div>
            <em>Trend</em>
            <strong>{analysis.trend.direction}</strong>
          </div>
          <div>
            <em>Wyckoff</em>
            <strong>{analysis.wyckoff.phase}</strong>
          </div>
          <div>
            <em>ATR%</em>
            <strong>{analysis.atrPct.toFixed(2)}</strong>
          </div>
        </div>
      )}

      <main className="stage">
        {candles.length > 0 && (
          <ChartView candles={candles} analysis={analysis} show={show} />
        )}
      </main>

      {analysis && <AnalysisPanels analysis={analysis} />}

      <footer className="foot">
        Eğitim / araştırma aracıdır. Yatırım tavsiyesi değildir. Elliot & Wyckoff sezgiseldir;
        tam manuel sayım değildir.
      </footer>
    </div>
  )
}
