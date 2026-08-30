import type { Verdict } from "../lib/ta/verdict"

function fmt(n: number, last: number) {
  if (last >= 1000) return n.toFixed(2)
  if (last >= 1) return n.toFixed(4)
  return n.toFixed(6)
}

export function VerdictBoard({
  verdict,
  last,
  symbol,
}: {
  verdict: Verdict
  last: number
  symbol: string
}) {
  return (
    <section className={`verdict bias-${verdict.bias}`}>
      <div className="verdict-top">
        <div>
          <p className="eyebrow">OTOMATİK ANALİZ MOTORU</p>
          <h2>{verdict.headline}</h2>
          <p className="muted">
            {symbol} · skor <strong>{verdict.score}</strong> (−100…+100) · güven{" "}
            <strong>%{verdict.confidence}</strong>
          </p>
        </div>
        <div className={`stamp ${verdict.bias}`}>
          {verdict.bias === "bull" ? "LONG AĞIR" : verdict.bias === "bear" ? "SHORT AĞIR" : "NÖTR"}
        </div>
      </div>

      <div className="verdict-grid">
        <div>
          <h4>Yöntem oyları</h4>
          <ul>
            {verdict.votes.map((v) => (
              <li key={v.method} className={v.bias}>
                <span>
                  {v.method} <em>w{v.weight}</em>
                </span>
                <strong>{v.bias.toUpperCase()}</strong>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h4>Hedefler</h4>
          <ul>
            <li>
              <span>−5%</span>
              <strong>{fmt(verdict.targets.down5, last)}</strong>
            </li>
            <li>
              <span>+10%</span>
              <strong>{fmt(verdict.targets.up10, last)}</strong>
            </li>
            <li>
              <span>Fib 0.618</span>
              <strong className="gold">{fmt(verdict.targets.fib618, last)}</strong>
            </li>
            <li>
              <span>Pivot PP</span>
              <strong>{fmt(verdict.targets.pivot, last)}</strong>
            </li>
          </ul>
          <h4>Özet maddeler</h4>
          <ul className="bullets">
            {verdict.bullets.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </div>
        <div className="hourly">
          <h4>10 saatlik bant (ATR + oy)</h4>
          <table>
            <thead>
              <tr>
                <th>+saat</th>
                <th>low</th>
                <th>high</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {verdict.hourly.map((h) => (
                <tr key={h.hour}>
                  <td>{h.hour}</td>
                  <td>{fmt(h.low, last)}</td>
                  <td>{fmt(h.high, last)}</td>
                  <td className="muted">{h.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  )
}
