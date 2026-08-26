import type { FullAnalysis } from "../lib/ta/types"

function fmt(n: number, last: number) {
  if (last >= 1000) return n.toFixed(2)
  if (last >= 1) return n.toFixed(4)
  return n.toFixed(6)
}

export function AnalysisPanels({ analysis }: { analysis: FullAnalysis }) {
  const last = analysis.last
  const poc = [...analysis.volumeProfile].sort((a, b) => b.volume - a.volume)[0]

  return (
    <div className="panels">
      <section className="panel">
        <h3>1 · Price Action</h3>
        <p className="muted">{analysis.trend.note}</p>
        <ul>
          {analysis.levels
            .filter((l) => l.kind === "support" || l.kind === "resistance")
            .slice(0, 6)
            .map((l) => (
              <li key={l.label + l.price}>
                <span>{l.label}</span>
                <strong>{fmt(l.price, last)}</strong>
              </li>
            ))}
        </ul>
        <h4>Order Blocks</h4>
        <ul>
          {analysis.orderBlocks.length === 0 && <li className="muted">Net OB yok</li>}
          {analysis.orderBlocks.map((l) => (
            <li key={l.label + l.meta}>
              <span>{l.label}</span>
              <strong>{fmt(l.price, last)}</strong>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel">
        <h3>2 · Fibonacci / Gann / Pivot</h3>
        <h4>Fib (φ 0.618 / 1.618)</h4>
        <ul>
          {analysis.fib
            .filter((f) => [0, 0.382, 0.5, 0.618, 0.786, 1, 1.272, 1.618].includes(f.ratio))
            .map((f) => (
              <li key={f.label}>
                <span>{f.ratio}</span>
                <strong className={f.ratio === 0.618 || f.ratio === 1.618 ? "gold" : ""}>
                  {fmt(f.price, last)}
                </strong>
              </li>
            ))}
        </ul>
        <h4>Pivot</h4>
        <ul>
          {Object.entries(analysis.pivots).map(([k, v]) => (
            <li key={k}>
              <span>{k}</span>
              <strong>{fmt(v, last)}</strong>
            </li>
          ))}
        </ul>
        <h4>Gann</h4>
        <ul>
          {analysis.gann.slice(0, 5).map((g) => (
            <li key={g.angle}>
              <span>{g.angle}</span>
              <strong>{fmt(g.price, last)}</strong>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel">
        <h3>3 · Formasyonlar</h3>
        <ul className="signals">
          {analysis.patterns.length === 0 && <li className="muted">Aktif pattern zayıf</li>}
          {analysis.patterns.map((s) => (
            <li key={s.title + s.detail} className={s.bias}>
              <strong>{s.title}</strong>
              <span>{s.detail}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel">
        <h3>4 · Elliot / Wyckoff</h3>
        <p>
          <strong>Elliot:</strong> {analysis.elliott.count}
        </p>
        <p className="muted">{analysis.elliott.note}</p>
        <p>
          <strong>Wyckoff:</strong> {analysis.wyckoff.phase}
        </p>
        <p className="muted">{analysis.wyckoff.note}</p>
      </section>

      <section className="panel">
        <h3>5 · Mum yüzleri</h3>
        <ul className="signals">
          {analysis.candles.slice(-6).reverse().map((s) => (
            <li key={s.time + s.title} className={s.bias}>
              <strong>{s.title}</strong>
              <span>{new Date(s.time * 1000).toLocaleString()}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel">
        <h3>6 · İndikatörler</h3>
        <ul className="signals">
          {analysis.signals
            .filter((s) => ["RSI", "MACD", "Golden", "Death"].some((k) => s.title.includes(k)))
            .map((s) => (
              <li key={s.title} className={s.bias}>
                <strong>{s.title}</strong>
                <span>{s.detail}</span>
              </li>
            ))}
        </ul>
        <p className="muted">
          ATR14 {fmt(analysis.atr, last)} ({analysis.atrPct.toFixed(2)}%) · son {fmt(last, last)}
        </p>
      </section>

      <section className="panel wide">
        <h3>7 · Volume Profile (VPVR)</h3>
        <div className="vp">
          {analysis.volumeProfile.map((b) => (
            <div className="vp-row" key={b.price}>
              <span>{fmt(b.price, last)}</span>
              <div className="vp-bar">
                <i style={{ width: `${Math.max(4, b.pct * 3)}%` }} />
              </div>
              <em>{b.pct.toFixed(1)}%</em>
            </div>
          ))}
        </div>
        {poc && (
          <p className="muted">
            POC (en çok hacim): <strong className="gold">{fmt(poc.price, last)}</strong>
          </p>
        )}
      </section>
    </div>
  )
}
