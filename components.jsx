// Shared UI components — register on window for cross-script use
const { useState, useEffect, useRef, useMemo } = React;

const fmt = (n, d = 1) => (n == null || isNaN(n)) ? '—' : Number(n).toFixed(d);
const fmtSign = (n, d = 1) => (n == null) ? '—' : (n >= 0 ? '+' : '') + Number(n).toFixed(d);
const fmtPct = (n, d = 1) => (n == null) ? '—' : (n * 100).toFixed(d) + '%';
const fmtUSD = (n) => (n == null) ? '—' : (n < 0 ? '-' : '') + '$' + Math.abs(n).toFixed(2);
const ago = (ts) => {
  const m = Math.floor((Date.now() - ts) / 60000);
  if (m < 1) return 'now';
  if (m < 60) return m + 'm';
  return Math.floor(m / 60) + 'h';
};

// Tiny sparkline
function Sparkline({ data, width = 80, height = 22, stroke = 'currentColor', fill = false }) {
  if (!data || data.length < 2) return null;
  const min = Math.min(...data), max = Math.max(...data);
  const range = max - min || 1;
  const step = width / (data.length - 1);
  const pts = data.map((v, i) => `${(i * step).toFixed(1)},${(height - ((v - min) / range) * height).toFixed(1)}`).join(' ');
  const last = data[data.length - 1], first = data[0];
  const trend = last > first ? 'pos' : 'neg';
  return (
    <svg className={`sparkmini ${trend}`} width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      {fill && <polygon points={`0,${height} ${pts} ${width},${height}`} fill="currentColor" opacity="0.15" />}
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function Pill({ kind = 'muted', children, dot = false }) {
  return (
    <span className={`pill ${kind}`}>
      {dot && <span className={`dot ${kind}`} />}
      {children}
    </span>
  );
}

function Dot({ kind = 'muted' }) { return <span className={`dot ${kind}`} />; }

// Data freshness 5-tick indicator
function FreshBars({ ageMin }) {
  const ticks = [];
  const filled = ageMin < 5 ? 5 : ageMin < 15 ? 4 : ageMin < 30 ? 3 : ageMin < 60 ? 2 : ageMin < 120 ? 1 : 0;
  for (let i = 0; i < 5; i++) {
    let cls = 'fresh-tick';
    if (i < filled) cls += filled <= 1 ? ' off' : filled <= 2 ? ' warn' : ' on';
    ticks.push(<span key={i} className={cls} />);
  }
  return <div className="fresh">{ticks}</div>;
}

// Brackets ladder — Kalshi% vs Model%
function BracketLadder({ ladder, modelMax, recommendedIdx, onSelect, selectedIdx }) {
  return (
    <div className="ladder">
      <div className="ladder-row" style={{ background: 'transparent', borderBottom: '1px solid var(--line)', height: '28px' }}>
        <div className="lcell label">Range</div>
        <div className="lcell label">Distribution</div>
        <div className="lcell label num-r" style={{ justifyContent: 'flex-end' }}>YES ¢</div>
        <div className="lcell label num-r" style={{ justifyContent: 'flex-end' }}>Edge</div>
        <div className="lcell label num-r" style={{ justifyContent: 'flex-end' }}>Vol</div>
        <div className="lcell label num-r" style={{ justifyContent: 'flex-end' }}>Action</div>
      </div>
      {ladder.map((b, i) => {
        const recommended = i === recommendedIdx;
        const selected = i === selectedIdx;
        const edgePct = b.edge * 100;
        return (
          <div
            key={i}
            className={`ladder-row ${recommended ? 'recommended' : ''}`}
            style={selected ? { outline: '1px solid var(--accent)', outlineOffset: '-1px' } : {}}
            onClick={() => onSelect(i)}
          >
            <div className="lcell ladder-range">
              {b.lo}–{b.hi}°
              {recommended && <span style={{ marginLeft: 6, fontSize: 9, color: 'var(--pos)' }}>★</span>}
            </div>
            <div className="lcell ladder-bars">
              <div className="bar-row">
                <span className="bar-label">M</span>
                <div className="bar-track"><div className="bar-fill model" style={{ width: `${b.modelPct * 100 * 2.5}%` }} /></div>
                <span className="bar-pct">{(b.modelPct * 100).toFixed(0)}%</span>
              </div>
              <div className="bar-row">
                <span className="bar-label">K</span>
                <div className="bar-track"><div className="bar-fill kalshi" style={{ width: `${b.kalshiPct * 100 * 2.5}%` }} /></div>
                <span className="bar-pct">{(b.kalshiPct * 100).toFixed(0)}%</span>
              </div>
            </div>
            <div className="lcell num-r" style={{ justifyContent: 'flex-end' }}>{b.yesPrice}¢</div>
            <div className={`lcell num-r ${edgePct > 0 ? 'pos' : edgePct < 0 ? 'neg' : ''}`} style={{ justifyContent: 'flex-end', fontWeight: 600 }}>
              {fmtSign(edgePct, 1)}%
            </div>
            <div className="lcell num-r" style={{ justifyContent: 'flex-end', color: 'var(--fg-2)' }}>
              {(b.volume / 1000).toFixed(1)}k
            </div>
            <div className="lcell" style={{ justifyContent: 'flex-end' }}>
              <button className={`btn sm ${recommended ? 'success' : 'ghost'}`} onClick={(e) => { e.stopPropagation(); onSelect(i, true); }}>
                {recommended ? 'BUY YES' : 'Bet'}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// Edge-disagreement panel: MOS vs NWS vs Kalshi implied
function EdgePanel({ state }) {
  const items = [
    { label: 'NWS Forecast', value: state.nwsForecast, color: 'var(--fg-1)' },
    { label: 'GFS-MOS', value: state.mosMax, color: 'var(--info)' },
    { label: 'NAM-MOS', value: state.namMos, color: 'var(--info)' },
    { label: 'Our Model', value: state.modelMax, color: 'var(--accent)' },
    { label: 'Kalshi Implied', value: state.brackets.reduce((s, b) => s + (b.lo + b.hi) / 2 * b.kalshiPct, 0), color: 'var(--neg)' },
  ];
  const min = Math.min(...items.map(i => i.value)) - 1;
  const max = Math.max(...items.map(i => i.value)) + 1;
  const range = max - min;
  return (
    <div>
      <div className="kv" style={{ marginBottom: 12 }}>
        <dt>NWS Official</dt><dd>{fmt(state.nwsForecast)}°F</dd>
        <dt>GFS-MOS</dt><dd className="info">{fmt(state.mosMax)}°F</dd>
        <dt>NAM-MOS</dt><dd className="info">{fmt(state.namMos)}°F</dd>
        <dt>Our model</dt><dd style={{ color: 'var(--accent)' }}>{fmt(state.modelMax)}°F</dd>
        <dt>Kalshi implied</dt><dd className="neg">{fmt(items[4].value)}°F</dd>
        <dt style={{ paddingTop: 6, borderTop: '1px solid var(--line)' }}>MOS vs NWS</dt>
        <dd style={{ paddingTop: 6, borderTop: '1px solid var(--line)' }} className={state.mosMax - state.nwsForecast > 0 ? 'pos' : 'neg'}>
          {fmtSign(state.mosMax - state.nwsForecast, 1)}°F
        </dd>
        <dt>Model vs Kalshi</dt>
        <dd className={state.modelMax - items[4].value > 0 ? 'pos' : 'neg'}>
          {fmtSign(state.modelMax - items[4].value, 1)}°F
        </dd>
      </div>
      <div style={{ marginTop: 8 }}>
        <div className="label" style={{ marginBottom: 8 }}>Forecast spread (°F)</div>
        <div style={{ position: 'relative', height: 60, background: 'var(--bg-2)', borderRadius: 4, border: '1px solid var(--line)' }}>
          {items.map((it, i) => {
            const left = ((it.value - min) / range) * 100;
            return (
              <div key={i} style={{ position: 'absolute', left: `${left}%`, top: 8, transform: 'translateX(-50%)', textAlign: 'center' }}>
                <div style={{ width: 2, height: 28, background: it.color, margin: '0 auto' }} />
                <div className="mono" style={{ fontSize: 9, color: it.color, marginTop: 2, whiteSpace: 'nowrap' }}>
                  {it.value.toFixed(1)}
                </div>
              </div>
            );
          })}
          <div style={{ position: 'absolute', left: 0, right: 0, bottom: 4, display: 'flex', justifyContent: 'space-between', padding: '0 4px', fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--fg-3)' }}>
            <span>{min.toFixed(0)}°</span><span>{max.toFixed(0)}°</span>
          </div>
        </div>
      </div>
    </div>
  );
}

// AFD with keyword highlighting
const HIGH_CONF = ['high confidence', 'good agreement', 'good confidence'];
const LOW_CONF = ['low confidence', 'uncertain', 'models disagree', 'spread', 'significant model spread'];
const SUPP = ['marine layer', 'sea breeze', 'smoke', 'haze', 'cloud', 'overcast'];
const ENH = ['sunny', 'clear', 'strong heating', 'offshore flow', 'unseasonably warm', 'above normal', 'well above'];
function AFDView({ text }) {
  const highlight = (str) => {
    let s = str;
    const apply = (arr, cls) => {
      arr.forEach(kw => {
        const re = new RegExp(`(${kw})`, 'gi');
        s = s.replace(re, `<span class="${cls}">$1</span>`);
      });
    };
    apply(HIGH_CONF, 'hl-conf-high');
    apply(LOW_CONF, 'hl-conf-low');
    apply(SUPP, 'hl-supp');
    apply(ENH, 'hl-enh');
    return s;
  };
  return <div className="afd-box" dangerouslySetInnerHTML={{ __html: highlight(text) }} />;
}

// Confidence meter
function ConfMeter({ value, max = 5 }) {
  return (
    <div style={{ display: 'flex', gap: 3, alignItems: 'center' }}>
      {Array.from({ length: max }, (_, i) => (
        <div key={i} style={{
          width: 14, height: 6, borderRadius: 1,
          background: i < value ? 'var(--accent)' : 'var(--bg-3)'
        }} />
      ))}
      <span className="mono" style={{ marginLeft: 6, fontSize: 11, color: 'var(--fg-1)' }}>{value}/{max}</span>
    </div>
  );
}

// Equity curve chart
function EquityChart({ history, height = 180 }) {
  const ref = useRef(null);
  const [w, setW] = useState(600);
  useEffect(() => {
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width));
    if (ref.current) ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const data = history.map(h => h.equity);
  const dd = history.map(h => h.drawdown);
  const min = Math.min(...data, ...history.map(h => h.equity + h.drawdown)) - 100;
  const max = Math.max(...data) + 100;
  const range = max - min || 1;
  const step = w / (data.length - 1);
  const pts = data.map((v, i) => `${i * step},${height - ((v - min) / range) * height}`).join(' ');
  const ddPts = history.map((h, i) => `${i * step},${height - ((h.equity + h.drawdown - min) / range) * height}`).join(' ');
  return (
    <div ref={ref} className="chart-wrap" style={{ height }}>
      <svg width={w} height={height} style={{ display: 'block' }}>
        {[0.25, 0.5, 0.75].map(t => (
          <line key={t} x1="0" y1={height * t} x2={w} y2={height * t} stroke="var(--grid)" />
        ))}
        <polyline points={`0,${height} ${pts} ${w},${height}`} fill="var(--accent)" opacity="0.08" />
        <polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth="1.6" />
        <polyline points={ddPts} fill="none" stroke="var(--neg)" strokeWidth="1" strokeDasharray="3,2" opacity="0.7" />
      </svg>
      <div style={{ position: 'absolute', top: 4, right: 8, fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--fg-2)' }}>
        ${data[data.length - 1].toFixed(0)}
      </div>
    </div>
  );
}

// Bar chart for daily P&L
function PLBars({ history, height = 80 }) {
  const ref = useRef(null);
  const [w, setW] = useState(600);
  useEffect(() => {
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width));
    if (ref.current) ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const max = Math.max(...history.map(h => Math.abs(h.pl)));
  const bw = Math.max(1, (w / history.length) - 1);
  return (
    <div ref={ref} className="chart-wrap" style={{ height }}>
      <svg width={w} height={height}>
        <line x1="0" y1={height / 2} x2={w} y2={height / 2} stroke="var(--line)" />
        {history.map((h, i) => {
          const bh = (Math.abs(h.pl) / max) * (height / 2 - 4);
          const y = h.pl >= 0 ? height / 2 - bh : height / 2;
          return (
            <rect key={i} x={i * (w / history.length)} y={y} width={bw} height={bh}
              fill={h.pl >= 0 ? 'var(--pos)' : 'var(--neg)'} opacity="0.85" />
          );
        })}
      </svg>
    </div>
  );
}

// Distribution chart with model + kalshi curves
function DistChart({ ladder, height = 140 }) {
  const ref = useRef(null);
  const [w, setW] = useState(600);
  useEffect(() => {
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width));
    if (ref.current) ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const max = Math.max(...ladder.map(b => Math.max(b.modelPct, b.kalshiPct))) * 1.1;
  const step = w / ladder.length;
  const mPath = ladder.map((b, i) => `${i * step + step / 2},${height - 16 - (b.modelPct / max) * (height - 24)}`).join(' L');
  const kPath = ladder.map((b, i) => `${i * step + step / 2},${height - 16 - (b.kalshiPct / max) * (height - 24)}`).join(' L');
  return (
    <div ref={ref} className="chart-wrap" style={{ height }}>
      <svg width={w} height={height}>
        {ladder.map((b, i) => {
          const edge = b.edge;
          const fillH = Math.abs(edge) * 600;
          return (
            <rect key={i} x={i * step + 1} y={height - 16 - fillH}
              width={step - 2} height={fillH}
              fill={edge > 0 ? 'var(--pos)' : 'var(--neg)'} opacity="0.18" />
          );
        })}
        <path d={`M${kPath}`} fill="none" stroke="var(--info)" strokeWidth="2" />
        <path d={`M${mPath}`} fill="none" stroke="var(--accent)" strokeWidth="2" />
        {ladder.map((b, i) => (
          <text key={i} x={i * step + step / 2} y={height - 4}
            textAnchor="middle" fontFamily="var(--mono)" fontSize="9" fill="var(--fg-3)">
            {b.lo}
          </text>
        ))}
      </svg>
      <div style={{ position: 'absolute', top: 6, right: 8, display: 'flex', gap: 12, fontFamily: 'var(--mono)', fontSize: 10 }}>
        <span><span className="dot" style={{ background: 'var(--accent)' }} /> Model</span>
        <span><span className="dot" style={{ background: 'var(--info)' }} /> Kalshi</span>
      </div>
    </div>
  );
}

Object.assign(window, {
  Sparkline, Pill, Dot, FreshBars, BracketLadder, EdgePanel, AFDView, ConfMeter,
  EquityChart, PLBars, DistChart,
  fmt, fmtSign, fmtPct, fmtUSD, ago,
});
