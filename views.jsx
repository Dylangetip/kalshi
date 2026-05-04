// Views — Opportunities, Dashboard, Terminal, Signals, P&L

const { useState: useState_v, useEffect: useEffect_v, useMemo: useMemo_v, useRef: useRef_v } = React;

// ====== OPPORTUNITIES BOARD ======
function OpportunitiesView({ states, selectedCity, setSelectedCity, onPlaceBet, history }) {
  const ranked = [...states].sort((a, b) => b.bestEdgeCents - a.bestEdgeCents);
  const top = ranked[0];
  const [selectedIdx, setSelectedIdx] = useState_v(0);
  const focused = ranked[selectedIdx] || ranked[0];
  const totalEdge = ranked.reduce((s, r) => s + Math.max(0, r.bestEdgeCents), 0);
  const todayPL = history[history.length - 1]?.pl || 0;

  return (
    <div className="opp-layout">
      <div className="opp-main">
        {/* Hero strip */}
        <div style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr 1fr 1fr', borderBottom: '1px solid var(--line)', background: 'var(--bg-1)' }}>
          <div style={{ padding: '16px 18px', borderRight: '1px solid var(--line)' }}>
            <div className="label" style={{ marginBottom: 8 }}>Top opportunity</div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
              <div className="huge-num pos">{fmtSign(top.bestEdgeCents)}¢</div>
              <div>
                <div style={{ fontWeight: 600, fontSize: 14 }}>{top.city.label}</div>
                <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
                  {top.settlementBracket.label} · YES @ {top.settlementBracket.yesPrice}¢
                </div>
              </div>
              <button className="btn success" style={{ marginLeft: 'auto' }}
                onClick={() => onPlaceBet(top.city.code, top.settlementBracket, 'YES', 250)}>
                BUY YES · $250
              </button>
            </div>
          </div>
          <div style={{ padding: '16px 18px', borderRight: '1px solid var(--line)' }}>
            <div className="label" style={{ marginBottom: 8 }}>Live edges</div>
            <div className="big-num">{ranked.filter(r => r.bestEdgeCents > 3).length} <span style={{ fontSize: 12, color: 'var(--fg-2)', fontWeight: 400 }}>/ {states.length}</span></div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--fg-2)', marginTop: 4 }}>edges &gt; 3¢ · ∑ {totalEdge}¢</div>
          </div>
          <div style={{ padding: '16px 18px', borderRight: '1px solid var(--line)' }}>
            <div className="label" style={{ marginBottom: 8 }}>P/L today</div>
            <div className={`big-num ${todayPL >= 0 ? 'pos' : 'neg'}`}>{fmtUSD(todayPL)}</div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--fg-2)', marginTop: 4 }}>4 open positions</div>
          </div>
          <div style={{ padding: '16px 18px' }}>
            <div className="label" style={{ marginBottom: 8 }}>Model agreement</div>
            <div className="big-num info">{(states.reduce((s, st) => s + st.confidence, 0) / states.length * 100).toFixed(0)}%</div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--fg-2)', marginTop: 4 }}>avg MOS / model / sounding</div>
          </div>
        </div>

        {/* Opp table */}
        <div style={{ padding: 0 }}>
          <div className="opp-row" style={{ background: 'var(--bg-1)', position: 'sticky', top: 0, zIndex: 1, fontSize: 10, fontWeight: 500, color: 'var(--fg-2)', textTransform: 'uppercase', letterSpacing: '0.08em', cursor: 'default' }}>
            <div>#</div>
            <div>City</div>
            <div>Recommended bracket</div>
            <div className="num-r">YES ¢</div>
            <div className="num-r">Edge</div>
            <div className="num-r">EV / $100</div>
            <div className="num-r">Conf</div>
          </div>
          {ranked.map((r, i) => (
            <div key={r.city.code}
              className={`opp-row ${i === selectedIdx ? 'selected' : ''}`}
              onClick={() => { setSelectedIdx(i); setSelectedCity(r.city.code); }}>
              <div className={`rank ${i === 0 ? 'gold' : ''}`}>{i + 1}</div>
              <div>
                <div style={{ fontFamily: 'var(--sans)', fontWeight: 600, fontSize: 13 }}>{r.city.label}</div>
                <div className="mono" style={{ fontSize: 10, color: 'var(--fg-3)' }}>{r.city.station}</div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span className="mono" style={{ fontWeight: 600, fontSize: 13 }}>{r.settlementBracket.label}</span>
                <Pill kind={r.bestEdgeCents > 5 ? 'pos' : r.bestEdgeCents > 0 ? 'info' : 'muted'}>
                  {r.bestEdgeCents > 5 ? 'STRONG' : r.bestEdgeCents > 0 ? 'EDGE' : 'FLAT'}
                </Pill>
                <span className="mono" style={{ fontSize: 10, color: 'var(--fg-3)' }}>updated {ago(r.asof)}</span>
              </div>
              <div className="num-r mono" style={{ fontSize: 13 }}>{r.settlementBracket.yesPrice}¢</div>
              <div className={`num-r mono ${r.bestEdgeCents >= 0 ? 'pos' : 'neg'}`} style={{ fontSize: 13, fontWeight: 600 }}>
                {fmtSign(r.bestEdgeCents)}¢
              </div>
              <div className={`num-r mono ${r.bestEdgeCents >= 0 ? 'pos' : 'neg'}`} style={{ fontSize: 13 }}>
                {fmtSign(r.bestEdgeCents * 1.0)}
              </div>
              <div className="num-r"><ConfMeter value={r.afdConfScore} /></div>
            </div>
          ))}
        </div>
      </div>

      {/* Right rail: drill into focused city */}
      <div className="opp-side">
        <div style={{ padding: 14, borderBottom: '1px solid var(--line)' }}>
          <div className="label" style={{ marginBottom: 6 }}>Focus</div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
            <div>
              <div style={{ fontSize: 18, fontWeight: 600 }}>{focused.city.label}</div>
              <div className="mono" style={{ fontSize: 10, color: 'var(--fg-3)' }}>{focused.city.station} · {focused.city.office} office</div>
            </div>
            <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>{focused.city.tz}</div>
          </div>
        </div>

        <div style={{ padding: 14, borderBottom: '1px solid var(--line)' }}>
          <div className="label" style={{ marginBottom: 8 }}>Forecast snapshot</div>
          <EdgePanel state={focused} />
        </div>

        <div style={{ padding: 14, borderBottom: '1px solid var(--line)' }}>
          <div className="label" style={{ marginBottom: 8 }}>Distribution</div>
          <DistChart ladder={focused.brackets} />
        </div>

        <div style={{ padding: 14, borderBottom: '1px solid var(--line)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <span className="label">Recommended bet</span>
            <Pill kind="pos">KELLY {(focused.kellyPct * 100).toFixed(1)}%</Pill>
          </div>
          <div className="kv">
            <dt>Bracket</dt><dd>{focused.settlementBracket.label}</dd>
            <dt>Side</dt><dd className="pos">YES @ {focused.settlementBracket.yesPrice}¢</dd>
            <dt>Size (¼ Kelly)</dt><dd>${(focused.kellyPct * 4000).toFixed(0)}</dd>
            <dt>Edge</dt><dd className="pos">{fmtSign(focused.bestEdgeCents)}¢</dd>
            <dt>Expected ROI</dt><dd className="pos">{fmtSign(focused.bestEdgeCents / focused.settlementBracket.yesPrice * 100, 0)}%</dd>
          </div>
          <button className="btn success" style={{ width: '100%', marginTop: 12 }}
            onClick={() => onPlaceBet(focused.city.code, focused.settlementBracket, 'YES', Math.round(focused.kellyPct * 4000))}>
            Place bet
          </button>
        </div>
      </div>
    </div>
  );
}

// ====== CITY DASHBOARD ======
function DashboardView({ state, history }) {
  const trendData = useMemo_v(() => Array.from({ length: 24 }, (_, i) => state.modelMax + Math.sin(i / 3) * 1.4 + Math.random() * 0.4), [state.city.code]);
  return (
    <div className="dash-layout" style={{ gridTemplateColumns: '1.2fr 1fr', gridTemplateRows: 'auto 1fr', padding: 1 }}>
      {/* Hero — modelMax + brackets distribution */}
      <div className="dash-cell" style={{ gridColumn: '1 / 2', gridRow: '1 / 2' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 16 }}>
          <div>
            <div className="label" style={{ marginBottom: 6 }}>Tomorrow's max — {state.city.label} ({state.city.station})</div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 14 }}>
              <span className="huge-num" style={{ fontSize: 56 }}>{fmt(state.modelMax)}<span style={{ fontSize: 24, color: 'var(--fg-2)' }}>°F</span></span>
              <Pill kind="warn">MODEL</Pill>
            </div>
            <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)', marginTop: 8 }}>
              vs NWS {fmt(state.nwsForecast)}° · MOS {fmt(state.mosMax)}° · current obs {fmt(state.obsCurrent)}°
            </div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <div className="label" style={{ marginBottom: 6 }}>Confidence</div>
            <ConfMeter value={state.afdConfScore} />
            <div className="mono" style={{ fontSize: 10, color: 'var(--fg-3)', marginTop: 6 }}>AFD parsed · 12Z run</div>
          </div>
        </div>
        <DistChart ladder={state.brackets} height={160} />
      </div>

      {/* MOS / NWS / Kalshi disagreement */}
      <div className="dash-cell" style={{ gridColumn: '2 / 3', gridRow: '1 / 2' }}>
        <div className="label" style={{ marginBottom: 12 }}>MOS · NWS · Kalshi · Model</div>
        <EdgePanel state={state} />
      </div>

      {/* Upper air + sounding */}
      <div className="dash-cell" style={{ gridColumn: '1 / 2', gridRow: '2 / 3' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <span className="label">Upper air & sounding (12Z)</span>
          <span className="mono" style={{ fontSize: 10, color: 'var(--fg-3)' }}>open-meteo · uwyo</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          <div className="signal-card">
            <div className="label">850mb</div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              <span className="big-num">{fmt(state.upperAir.t850)}°C</span>
              <span className={`mono ${state.sounding.t850Delta > 0 ? 'pos' : 'neg'}`} style={{ fontSize: 11 }}>
                Δ {fmtSign(state.sounding.t850Delta, 2)}
              </span>
            </div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--fg-2)' }}>
              wind {state.upperAir.windDir}° / {state.upperAir.windKt}kt · RH {state.upperAir.rh850}%
            </div>
          </div>
          <div className="signal-card">
            <div className="label">700mb</div>
            <div className="big-num">{fmt(state.upperAir.t700)}°C</div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--fg-2)' }}>cap layer</div>
          </div>
          <div className="signal-card">
            <div className="label">500mb height</div>
            <div className="big-num">{state.upperAir.h500}<span style={{ fontSize: 12 }}>m</span></div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--fg-2)' }}>ridge anomaly +{(state.upperAir.h500 - 5760)}m</div>
          </div>
          <div className="signal-card">
            <div className="label">Lapse rate</div>
            <div className="big-num">{fmt(state.upperAir.lapse)}<span style={{ fontSize: 12 }}>°C/km</span></div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--fg-2)' }}>
              {state.upperAir.lapse > 7 ? 'unstable — strong heating' : 'stable'}
            </div>
          </div>
        </div>
      </div>

      {/* AFD parsed */}
      <div className="dash-cell" style={{ gridColumn: '2 / 3', gridRow: '2 / 3' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
          <span className="label">NWS AFD — {state.city.office}</span>
          <div style={{ display: 'flex', gap: 6 }}>
            {state.flags.seaBreeze && <Pill kind="neg">SEA BREEZE</Pill>}
            {state.flags.marineLayer && <Pill kind="neg">MARINE LAYER</Pill>}
            {state.flags.smoke && <Pill kind="warn">SMOKE</Pill>}
            {state.flags.offshore && <Pill kind="info">OFFSHORE</Pill>}
            {state.flags.overcast && <Pill kind="warn">OVERCAST</Pill>}
          </div>
        </div>
        <AFDView text={state.afdText} />
      </div>
    </div>
  );
}

// ====== TRADING TERMINAL ======
function TerminalView({ state, onPlaceBet, betLog }) {
  const recIdx = state.brackets.findIndex(b => b === state.settlementBracket);
  const [selectedIdx, setSelectedIdx] = useState_v(recIdx);
  const [size, setSize] = useState_v(250);
  const [side, setSide] = useState_v('YES');
  const sel = state.brackets[selectedIdx];

  const handleSelect = (i, immediate = false) => {
    setSelectedIdx(i);
    if (immediate) {
      onPlaceBet(state.city.code, state.brackets[i], 'YES', size);
    }
  };

  const cost = side === 'YES' ? sel.yesPrice : (100 - sel.yesPrice);
  const shares = (size / cost * 100).toFixed(0);
  const maxPayout = (size / cost * 100).toFixed(0);

  return (
    <div className="term-layout">
      <div className="term-left">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
          <div>
            <div className="label">Market</div>
            <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2 }}>
              KXHIGH-{state.city.code} · {new Date(Date.now() + 86400000).toISOString().slice(5, 10)}
            </div>
            <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
              {state.city.label} ({state.city.station}) — daily max temperature
            </div>
          </div>
          <div style={{ display: 'flex', gap: 24, alignItems: 'center' }}>
            <div>
              <div className="label">Settlement</div>
              <div className="mono" style={{ fontSize: 13 }}>NWS Daily Climate Report</div>
            </div>
            <div>
              <div className="label">Closes</div>
              <div className="mono" style={{ fontSize: 13 }}>tomorrow 23:59 ET</div>
            </div>
          </div>
        </div>

        <BracketLadder
          ladder={state.brackets}
          modelMax={state.modelMax}
          recommendedIdx={recIdx}
          selectedIdx={selectedIdx}
          onSelect={handleSelect}
        />

        <div style={{ marginTop: 18, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
          <div className="panel">
            <div className="panel-header">
              <span>Recent fills · {state.city.code}</span>
              <span className="mono" style={{ fontSize: 10, color: 'var(--fg-3)' }}>last 30m</span>
            </div>
            <div style={{ maxHeight: 180, overflow: 'auto' }}>
              {Array.from({ length: 8 }, (_, i) => {
                const time = new Date(Date.now() - i * 60000 * 3 - Math.random() * 60000);
                const b = state.brackets[Math.floor(Math.random() * state.brackets.length)];
                const isYes = Math.random() > 0.5;
                return (
                  <div key={i} style={{ display: 'grid', gridTemplateColumns: '70px 80px 1fr 60px 60px', padding: '6px 12px', borderBottom: '1px solid var(--line)', fontSize: 11, fontFamily: 'var(--mono)' }}>
                    <span style={{ color: 'var(--fg-3)' }}>{time.toTimeString().slice(0, 8)}</span>
                    <span>{b.label}</span>
                    <span className={isYes ? 'pos' : 'neg'}>{isYes ? 'YES' : 'NO'} {Math.round(b.yesPrice + (Math.random() - 0.5) * 4)}¢</span>
                    <span style={{ textAlign: 'right' }}>{Math.floor(Math.random() * 200 + 20)}</span>
                    <span style={{ textAlign: 'right', color: 'var(--fg-3)' }}>${Math.floor(Math.random() * 60 + 10)}</span>
                  </div>
                );
              })}
            </div>
          </div>
          <div className="panel">
            <div className="panel-header">
              <span>Your bet log</span>
              <span className="mono" style={{ fontSize: 10, color: 'var(--fg-3)' }}>session</span>
            </div>
            <div style={{ maxHeight: 180, overflow: 'auto' }}>
              {betLog.length === 0 && <div style={{ padding: 14, fontSize: 11, color: 'var(--fg-3)' }}>No bets yet — place one →</div>}
              {betLog.map((b, i) => {
                const settled = b.status === 'settled' && b.settledPl != null;
                const won = settled && b.settledPl > 0;
                const closesIn = settled ? null : until(b.closeAt);
                return (
                  <div key={i} style={{ display: 'grid', gridTemplateColumns: '60px 50px 1fr 60px 80px', padding: '6px 12px', borderBottom: '1px solid var(--line)', fontSize: 11, fontFamily: 'var(--mono)', opacity: settled ? 0.85 : 1 }}>
                    <span style={{ color: 'var(--fg-3)' }}>{b.time}</span>
                    <span>{b.city}</span>
                    <span>
                      {b.bracket.label} <span className={b.side === 'YES' ? 'pos' : 'neg'}>{b.side}</span>
                      {settled && (
                        <span className={won ? 'pos' : 'neg'} style={{ marginLeft: 6, fontSize: 9, fontWeight: 600 }}>
                          · {won ? 'WON' : 'LOST'} @ {b.settledMaxF}°
                        </span>
                      )}
                      {closesIn && (
                        <span style={{ marginLeft: 6, fontSize: 9, color: 'var(--fg-3)' }}>
                          · closes {closesIn}
                        </span>
                      )}
                    </span>
                    <span style={{ textAlign: 'right' }}>${b.size}</span>
                    {settled ? (
                      <span style={{ textAlign: 'right' }} className={won ? 'pos' : 'neg'}>
                        {fmtSign(b.settledPl, 0)}
                      </span>
                    ) : (
                      <span style={{ textAlign: 'right' }} className="pos">{b.entry}¢</span>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>

      <div className="term-right">
        <div style={{ padding: 14, borderBottom: '1px solid var(--line)' }}>
          <div className="label" style={{ marginBottom: 6 }}>Selected bracket</div>
          <div style={{ fontSize: 22, fontWeight: 600 }}>{sel.label}</div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)', marginTop: 4 }}>
            Model {(sel.modelPct * 100).toFixed(1)}% · Kalshi {(sel.kalshiPct * 100).toFixed(1)}% · Edge <span className={sel.edge > 0 ? 'pos' : 'neg'}>{fmtSign(sel.edge * 100, 1)}%</span>
          </div>
        </div>

        <div style={{ padding: 14, borderBottom: '1px solid var(--line)' }}>
          <div className="label" style={{ marginBottom: 8 }}>Side</div>
          <div style={{ display: 'flex', gap: 6 }}>
            <button className={`btn ${side === 'YES' ? 'success' : 'ghost'}`} style={{ flex: 1 }} onClick={() => setSide('YES')}>
              YES {sel.yesPrice}¢
            </button>
            <button className={`btn ${side === 'NO' ? 'danger' : 'ghost'}`} style={{ flex: 1 }} onClick={() => setSide('NO')}>
              NO {100 - sel.yesPrice}¢
            </button>
          </div>
        </div>

        <div style={{ padding: 14, borderBottom: '1px solid var(--line)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
            <span className="label">Stake</span>
            <span className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>¼ Kelly: ${(state.kellyPct * 4000).toFixed(0)}</span>
          </div>
          <input type="number" value={size} onChange={(e) => setSize(+e.target.value)} style={{ width: '100%', fontSize: 16, marginBottom: 8 }} />
          <div className="size-stepper">
            {[50, 100, 250, 500, 1000].map(v => (
              <button key={v} onClick={() => setSize(v)}>${v}</button>
            ))}
          </div>
        </div>

        <div style={{ padding: 14, borderBottom: '1px solid var(--line)' }}>
          <div className="kv">
            <dt>Cost / contract</dt><dd>{cost}¢</dd>
            <dt>Contracts</dt><dd>{shares}</dd>
            <dt>Max payout</dt><dd className="pos">${maxPayout}</dd>
            <dt>Max loss</dt><dd className="neg">${size}</dd>
            <dt>Implied prob</dt><dd>{cost}%</dd>
            <dt>Model prob</dt><dd>{(side === 'YES' ? sel.modelPct : 1 - sel.modelPct).toFixed(2) * 100 | 0}%</dd>
            <dt style={{ paddingTop: 6, borderTop: '1px solid var(--line)' }}>Expected value</dt>
            <dd style={{ paddingTop: 6, borderTop: '1px solid var(--line)' }} className={sel.edge > 0 ? 'pos' : 'neg'}>
              {fmtSign((side === 'YES' ? sel.edge : -sel.edge) * size * 100 / cost, 2)}
            </dd>
          </div>
        </div>

        <div className="bet-form">
          <button className="btn success" style={{ fontSize: 13, padding: '10px 14px' }}
            onClick={() => onPlaceBet(state.city.code, sel, side, size)}>
            Place {side} · ${size} @ {cost}¢
          </button>
          <div className="mono" style={{ fontSize: 9, color: 'var(--fg-3)', textAlign: 'center' }}>
            paper trading mode · no real funds
          </div>
        </div>
      </div>
    </div>
  );
}

// ====== SIGNAL MONITOR ======
function SignalsView({ signals, state }) {
  const [filter, setFilter] = useState_v('all');
  const cats = useMemo_v(() => Array.from(new Set(signals.map(s => s.cat))), [signals]);
  const filtered = filter === 'all' ? signals : signals.filter(s => s.impact === filter);
  const grouped = filtered.reduce((acc, s) => { (acc[s.cat] = acc[s.cat] || []).push(s); return acc; }, {});

  const okCount = signals.filter(s => s.status === 'ok').length;
  const warnCount = signals.filter(s => s.status === 'warn').length;
  const staleCount = signals.filter(s => s.status === 'stale').length;

  return (
    <div className="signals-layout">
      <div className="signals-grid">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10, marginBottom: 14 }}>
          <div className="signal-card">
            <div className="label">Total signals</div>
            <div className="big-num">{signals.length}</div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--fg-2)' }}>{cats.length} categories</div>
          </div>
          <div className="signal-card">
            <div className="label">Healthy</div>
            <div className="big-num pos">{okCount}</div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--fg-2)' }}>fresh &lt; 30m</div>
          </div>
          <div className="signal-card">
            <div className="label">Warning</div>
            <div className="big-num warn">{warnCount}</div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--fg-2)' }}>30m - 2h</div>
          </div>
          <div className="signal-card">
            <div className="label">Stale</div>
            <div className="big-num neg">{staleCount}</div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--fg-2)' }}>investigate</div>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 6, marginBottom: 12 }}>
          {[['all', 'All'], ['H', 'High impact'], ['M', 'Medium'], ['L', 'Low']].map(([k, l]) => (
            <button key={k} className={`btn sm ${filter === k ? 'primary' : 'ghost'}`} onClick={() => setFilter(k)}>{l}</button>
          ))}
        </div>

        {Object.entries(grouped).map(([cat, sigs]) => (
          <div key={cat} className="panel" style={{ marginBottom: 10 }}>
            <div className="panel-header">
              <span>{cat}</span>
              <span className="panel-title-actions">{sigs.length} signals</span>
            </div>
            <div>
              <div className="signal-row-grid" style={{ background: 'var(--bg-2)', fontSize: 10, color: 'var(--fg-2)', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 500, cursor: 'default' }}>
                <div>Signal</div>
                <div>Impact</div>
                <div>Source · trend</div>
                <div className="num-r">Δ24h</div>
                <div className="num-r">Updated</div>
              </div>
              {sigs.map(s => (
                <div key={s.id} className="signal-row-grid">
                  <div>
                    <div style={{ fontFamily: 'var(--sans)', fontWeight: 500 }}>
                      <Dot kind={s.status === 'ok' ? 'pos' : s.status === 'warn' ? 'warn' : 'neg'} />
                      <span style={{ marginLeft: 8 }}>{s.name}</span>
                    </div>
                    <div className="mono" style={{ fontSize: 10, color: 'var(--fg-3)', marginLeft: 14 }}>{s.id}</div>
                  </div>
                  <div>
                    <Pill kind={s.impact === 'H' ? 'pos' : s.impact === 'M' ? 'info' : 'muted'}>
                      {s.impact === 'H' ? 'HIGH' : s.impact === 'M' ? 'MED' : 'LOW'}
                    </Pill>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-2)' }}>
                    <span>{s.source}</span>
                    <Sparkline data={Array.from({ length: 12 }, () => Math.random() * 10 + s.value)} />
                  </div>
                  <div className={`num-r ${s.delta > 0 ? 'pos' : s.delta < 0 ? 'neg' : ''}`} style={{ fontFamily: 'var(--mono)' }}>
                    {fmtSign(s.delta, 2)}
                  </div>
                  <div className="num-r" style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-2)' }}>
                    <FreshBars ageMin={s.ageMin} />
                    <span style={{ marginLeft: 6 }}>{s.ageMin}m</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className="signals-side">
        <div style={{ padding: 14, borderBottom: '1px solid var(--line)' }}>
          <div className="label" style={{ marginBottom: 8 }}>Top contributors → today's edge</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {[...signals].sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution)).slice(0, 8).map(s => (
              <div key={s.id} style={{ display: 'grid', gridTemplateColumns: '1fr 60px', alignItems: 'center', fontSize: 11 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, overflow: 'hidden' }}>
                  <Pill kind={s.impact === 'H' ? 'pos' : s.impact === 'M' ? 'info' : 'muted'}>{s.impact}</Pill>
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.name}</span>
                </div>
                <div className={`num-r mono ${s.contribution > 0 ? 'pos' : 'neg'}`} style={{ fontSize: 11, fontWeight: 600 }}>
                  {fmtSign(s.contribution, 2)}°
                </div>
              </div>
            ))}
          </div>
        </div>
        <div style={{ padding: 14, borderBottom: '1px solid var(--line)' }}>
          <div className="label" style={{ marginBottom: 8 }}>Pipeline</div>
          {['Ingest', 'Feature build', 'Model run', 'Edge calc', 'Order check'].map((stage, i) => (
            <div key={i} style={{ display: 'grid', gridTemplateColumns: '1fr auto', padding: '5px 0', borderBottom: i < 4 ? '1px solid var(--line)' : 'none', fontSize: 12 }}>
              <span><Dot kind="pos" /> <span style={{ marginLeft: 8 }}>{stage}</span></span>
              <span className="mono" style={{ fontSize: 10, color: 'var(--fg-2)' }}>{Math.floor(Math.random() * 800 + 80)}ms</span>
            </div>
          ))}
        </div>
        <div style={{ padding: 14 }}>
          <div className="label" style={{ marginBottom: 8 }}>Audit log</div>
          <div className="mono" style={{ fontSize: 10, color: 'var(--fg-2)', lineHeight: 1.7 }}>
            <div><span style={{ color: 'var(--fg-3)' }}>14:32:08</span> <span className="pos">OK</span> GFS-MOS bulletin {state.city.station} ingested</div>
            <div><span style={{ color: 'var(--fg-3)' }}>14:31:55</span> <span className="pos">OK</span> AFD {state.city.office} parsed (conf=4)</div>
            <div><span style={{ color: 'var(--fg-3)' }}>14:31:42</span> <span className="warn">WARN</span> sounding {state.city.station} delta &gt; 1.0°</div>
            <div><span style={{ color: 'var(--fg-3)' }}>14:31:30</span> <span className="pos">OK</span> Open-Meteo 850mb refresh</div>
            <div><span style={{ color: 'var(--fg-3)' }}>14:31:14</span> <span className="pos">OK</span> Kalshi orderbook polled</div>
            <div><span style={{ color: 'var(--fg-3)' }}>14:30:01</span> <span className="info">INFO</span> 1m tick — model rerun</div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ====== P&L ======
function PnLView({ history, positions, liveHistory = false, stats = null }) {
  const total = history[history.length - 1].equity;
  const start = history[0].equity;
  const ret = (total - start) / start;
  const wins = history.filter(h => h.pl > 0).length;
  const winRate = wins / history.length;
  const sharpe = useMemo_v(() => {
    const rets = history.map(h => h.pl);
    const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
    const sd = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / rets.length);
    return mean / sd * Math.sqrt(252);
  }, [history]);
  const peak = Math.max(...history.map(h => h.equity));
  const maxDD = Math.min(...history.map(h => h.equity - peak));

  return (
    <div className="pnl-layout">
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 10 }}>
        <div className="signal-card">
          <div className="label">Equity</div>
          <div className="big-num">{fmtUSD(total)}</div>
          <div className={`mono ${ret > 0 ? 'pos' : 'neg'}`} style={{ fontSize: 11 }}>{fmtSign(ret * 100, 2)}% · {liveHistory ? 'session' : '60d'}</div>
        </div>
        <div className="signal-card">
          <div className="label">Win rate {stats && stats.settled > 0 && <Pill kind="pos">REAL</Pill>}</div>
          {stats && stats.settled > 0 ? (
            <>
              <div className="big-num">{(stats.winRate * 100).toFixed(0)}%</div>
              <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
                {stats.won}W / {stats.settled - stats.won}L · {stats.open} open
              </div>
            </>
          ) : (
            <>
              <div className="big-num" style={{ color: stats ? 'var(--fg-2)' : undefined }}>
                {stats ? '—' : (winRate * 100).toFixed(0) + '%'}
              </div>
              <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
                {stats ? `${stats.open} open · awaiting settlement` : `${wins}W / ${history.length - wins}L`}
              </div>
            </>
          )}
        </div>
        <div className="signal-card">
          <div className="label">Sharpe (ann.)</div>
          <div className="big-num">{sharpe.toFixed(2)}</div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>252-day</div>
        </div>
        <div className="signal-card">
          <div className="label">Max drawdown</div>
          <div className="big-num neg">{fmtUSD(maxDD)}</div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>peak-to-trough</div>
        </div>
        <div className="signal-card">
          <div className="label">Avg edge / bet</div>
          <div className="big-num pos">+4.2¢</div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>{history.reduce((s, h) => s + h.trades, 0)} trades</div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">
          <span>Equity curve {liveHistory && <Pill kind="pos">LIVE · SESSION</Pill>}</span>
          <span className="panel-title-actions">
            <span><span className="dot" style={{ background: 'var(--accent)' }} /> equity</span>
            <span><span className="dot" style={{ background: 'var(--neg)' }} /> drawdown</span>
          </span>
        </div>
        <div style={{ padding: 14 }}>
          <EquityChart history={history} height={220} />
        </div>
      </div>

      <div className="panel">
        <div className="panel-header"><span>Daily P/L</span></div>
        <div style={{ padding: 14 }}>
          <PLBars history={history} height={90} />
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr', gap: 14 }}>
        <div className="panel">
          <div className="panel-header"><span>Open positions</span><span className="panel-title-actions">{positions.length} · ${positions.reduce((s, p) => s + p.size, 0)} exposed</span></div>
          <table className="tbl">
            <thead>
              <tr>
                <th>City</th><th>Bracket</th><th>Side</th>
                <th className="num-r">Size</th><th className="num-r">Entry</th>
                <th className="num-r">Mark</th><th className="num-r">P/L</th>
              </tr>
            </thead>
            <tbody>
              {positions.map(p => (
                <tr key={p.id}>
                  <td className="city-cell">{p.city}</td>
                  <td>{p.bracket}</td>
                  <td className={p.side === 'YES' ? 'pos' : 'neg'}>{p.side}</td>
                  <td className="num-r">${p.size}</td>
                  <td className="num-r">{(p.entry * 100).toFixed(0)}¢</td>
                  <td className="num-r">{(p.current * 100).toFixed(0)}¢</td>
                  <td className={`num-r ${p.pl > 0 ? 'pos' : 'neg'}`}>{fmtSign(p.pl, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="panel">
          <div className="panel-header"><span>By city</span></div>
          <table className="tbl">
            <thead><tr><th>City</th><th className="num-r">Trades</th><th className="num-r">Win%</th><th className="num-r">P/L</th></tr></thead>
            <tbody>
              {[
                { c: 'NYC', t: 22, w: 0.59, pl: 412 },
                { c: 'CHI', t: 18, w: 0.50, pl: 88 },
                { c: 'MIA', t: 14, w: 0.43, pl: -54 },
                { c: 'AUS', t: 16, w: 0.56, pl: 167 },
                { c: 'LAX', t: 19, w: 0.63, pl: 298 },
              ].map(r => (
                <tr key={r.c}>
                  <td className="city-cell">{r.c}</td>
                  <td className="num-r">{r.t}</td>
                  <td className="num-r">{(r.w * 100).toFixed(0)}%</td>
                  <td className={`num-r ${r.pl > 0 ? 'pos' : 'neg'}`}>{fmtSign(r.pl, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ====== AUTO-TRADE ======
function AutoTradeView({ info, accuracy, bets, onSetConfig, onTriggerNow }) {
  const [busy, setBusy] = useState_v(false);
  const [lastRunResult, setLastRunResult] = useState_v(null);
  const [draft, setDraft] = useState_v({});

  // Auto-bets are the ones placed via this view's trigger / loop. We
  // can't distinguish them from manual bets in the schema yet, so
  // "recent" just means the live session bet log — same data the
  // Terminal shows.
  const recent = (bets || []).slice(0, 20);

  const cfg = info || {};
  const enabled = draft.enabled ?? cfg.enabled ?? false;
  const minEdge = draft.min_edge_cents ?? cfg.min_edge_cents ?? 5;
  const bankroll = draft.bankroll ?? cfg.bankroll ?? 10000;
  const maxUsd = draft.max_usd ?? cfg.max_usd ?? 500;
  const lastRun = cfg.last_run_ts ? ago(cfg.last_run_ts * 1000) : 'never';

  const apply = async (patch) => {
    setBusy(true);
    await onSetConfig(patch);
    setDraft({});
    setBusy(false);
  };
  const trigger = async () => {
    setBusy(true);
    const r = await onTriggerNow();
    setLastRunResult(r);
    setBusy(false);
  };

  return (
    <div className="pnl-layout">
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 10 }}>
        <div className="signal-card">
          <div className="label">Status</div>
          <div className={`big-num ${enabled ? 'pos' : 'neg'}`}>{enabled ? 'ON' : 'OFF'}</div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
            interval {Math.round((cfg.interval_seconds || 3600) / 60)}m
          </div>
        </div>
        <div className="signal-card">
          <div className="label">Min edge</div>
          <div className="big-num">{minEdge}¢</div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>fires above</div>
        </div>
        <div className="signal-card">
          <div className="label">Bankroll</div>
          <div className="big-num">{fmtUSD(bankroll)}</div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>¼-Kelly base</div>
        </div>
        <div className="signal-card">
          <div className="label">Last run</div>
          <div className="big-num">{lastRun}</div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
            {cfg.last_run_placed != null ? `${cfg.last_run_placed} placed` : '—'}
          </div>
        </div>
        <div className="signal-card">
          <div className="label">Total placed</div>
          <div className="big-num pos">{cfg.total_placed || 0}</div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
            across {cfg.total_runs || 0} runs
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">
          <span>Set total, flip on</span>
          <span className="panel-title-actions">
            picks the single highest-edge bracket each tick
          </span>
        </div>
        <div style={{ padding: 14, display: 'grid', gridTemplateColumns: '1fr auto auto', gap: 14, alignItems: 'end' }}>
          <div>
            <div className="label" style={{ marginBottom: 6 }}>
              Bankroll ($) — max per bet auto-set to 5% (¼-Kelly cap)
            </div>
            <input type="number" value={bankroll} min={100} step={100}
              onChange={(e) => setDraft(d => ({ ...d, bankroll: +e.target.value }))}
              style={{ width: '100%', fontSize: 16 }} />
          </div>
          <button
            className={`btn ${enabled ? 'danger' : 'success'}`}
            onClick={() => apply({ ...draft, enabled: !enabled })}
            disabled={busy}
            style={{ minWidth: 120, fontSize: 13 }}>
            {enabled ? 'TURN OFF' : 'TURN ON'}
          </button>
          <button className="btn primary" disabled={busy} onClick={trigger}
            style={{ minWidth: 120 }}>
            {busy ? 'running…' : 'Trigger Now'}
          </button>
        </div>
        <div style={{ padding: '0 14px 14px', fontSize: 11, color: 'var(--fg-3)', fontFamily: 'var(--mono)' }}>
          edge threshold {minEdge}¢ · max-per-bet ${maxUsd.toFixed(0)} · {enabled ? `next tick in ≤${Math.round((cfg.interval_seconds || 3600)/60)}m` : 'loop idle'}
        </div>
        {lastRunResult && (
          <div style={{ padding: '0 14px 14px', fontSize: 11, color: 'var(--fg-2)' }}>
            <span className="mono">
              last trigger: {lastRunResult.count > 0 ? 'placed 1 bet' : 'no eligible candidate'}
              {lastRunResult.last_run_considered > 0 && ` (considered ${lastRunResult.last_run_considered})`}
            </span>
            {lastRunResult.placed && lastRunResult.placed.length > 0 && (
              <div style={{ marginTop: 6 }}>
                {lastRunResult.placed.map((p, i) => (
                  <div key={i} className="mono" style={{ fontSize: 11 }}>
                    <span className="pos">★</span> {p.city} {p.bracket} ${p.size_usd} @ {p.entry_cents}¢
                    <span className="pos" style={{ marginLeft: 8 }}>edge +{p.edge_cents}¢</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-header">
          <span>Model accuracy</span>
          <span className="panel-title-actions">
            modelMax vs actual NWS high · {accuracy?.n_predictions || 0} settled day{(accuracy?.n_predictions || 0) === 1 ? '' : 's'}
          </span>
        </div>
        {!accuracy || accuracy.n_predictions === 0 ? (
          <div style={{ padding: 14, fontSize: 12, color: 'var(--fg-3)' }}>
            no settled predictions yet — accuracy populates after the first
            bet settles tomorrow morning when the NWS Daily Climate Report posts.
          </div>
        ) : (
          <div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 10, padding: 14 }}>
              <div className="signal-card">
                <div className="label">Mean abs error</div>
                <div className="big-num">{accuracy.mean_absolute_error}°F</div>
                <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>median {accuracy.median_abs_error}°</div>
              </div>
              <div className="signal-card">
                <div className="label">Within 1°F</div>
                <div className="big-num pos">{accuracy.within_1_pct}%</div>
                <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>bullseye</div>
              </div>
              <div className="signal-card">
                <div className="label">Within 2°F</div>
                <div className="big-num pos">{accuracy.within_2_pct}%</div>
                <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>good</div>
              </div>
              <div className="signal-card">
                <div className="label">Within 3°F</div>
                <div className="big-num">{accuracy.within_3_pct}%</div>
                <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>acceptable</div>
              </div>
              <div className="signal-card">
                <div className="label">N predictions</div>
                <div className="big-num">{accuracy.n_predictions}</div>
                <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>settled days</div>
              </div>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, padding: '0 14px 14px' }}>
              <div>
                <div className="label" style={{ marginBottom: 8 }}>By city</div>
                <table className="tbl">
                  <thead>
                    <tr><th>City</th><th className="num-r">N</th><th className="num-r">MAE</th><th className="num-r">≤2°F</th></tr>
                  </thead>
                  <tbody>
                    {accuracy.by_city.map(r => (
                      <tr key={r.city}>
                        <td className="city-cell">{r.city}</td>
                        <td className="num-r">{r.n}</td>
                        <td className="num-r">{r.mae}°</td>
                        <td className="num-r">{r.within_2_pct}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div>
                <div className="label" style={{ marginBottom: 8 }}>Recent predictions</div>
                <table className="tbl">
                  <thead>
                    <tr><th>Date</th><th>City</th><th className="num-r">Model</th><th className="num-r">Actual</th><th className="num-r">Δ</th></tr>
                  </thead>
                  <tbody>
                    {accuracy.recent.slice(0, 10).map((r, i) => {
                      const close = Math.abs(r.error) <= 1;
                      const ok = Math.abs(r.error) <= 2;
                      return (
                        <tr key={i}>
                          <td>{r.date}</td>
                          <td className="city-cell">{r.city}</td>
                          <td className="num-r">{r.prediction}°</td>
                          <td className="num-r">{r.actual}°</td>
                          <td className={`num-r ${close ? 'pos' : ok ? 'info' : 'neg'}`}>
                            {fmtSign(r.error, 1)}°
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-header">
          <span>Recent bets</span>
          <span className="panel-title-actions">
            session log · {recent.length} shown
          </span>
        </div>
        {recent.length === 0 ? (
          <div style={{ padding: 14, fontSize: 12, color: 'var(--fg-3)' }}>
            no bets yet — turn auto-trade on, or hit "Trigger Now"
          </div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Time</th><th>City</th><th>Bracket</th><th>Side</th>
                <th className="num-r">Size</th><th className="num-r">Entry</th>
                <th className="num-r">Closes in</th>
                <th>Status</th><th className="num-r">Settled P/L</th>
              </tr>
            </thead>
            <tbody>
              {recent.map(b => {
                const settled = b.status === 'settled' && b.settledPl != null;
                const won = settled && b.settledPl > 0;
                const closesIn = settled ? '—' : until(b.closeAt);
                const isClosingSoon = !settled && b.closeAt &&
                  (new Date(b.closeAt).getTime() - Date.now()) < 3 * 3600 * 1000;
                return (
                  <tr key={b.id}>
                    <td>{b.time}</td>
                    <td className="city-cell">{b.city}</td>
                    <td>{b.bracket?.label || b.bracket}</td>
                    <td className={b.side === 'YES' ? 'pos' : 'neg'}>{b.side}</td>
                    <td className="num-r">${b.size}</td>
                    <td className="num-r">{b.entry}¢</td>
                    <td className={`num-r ${isClosingSoon ? 'warn' : ''}`}>{closesIn}</td>
                    <td>
                      {settled ? (
                        <Pill kind={won ? 'pos' : 'neg'}>
                          {won ? 'WON' : 'LOST'} @ {b.settledMaxF}°
                        </Pill>
                      ) : (
                        <Pill kind="info">OPEN</Pill>
                      )}
                    </td>
                    <td className={`num-r ${settled ? (won ? 'pos' : 'neg') : ''}`}>
                      {settled ? fmtSign(b.settledPl, 0) : '—'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

Object.assign(window, { OpportunitiesView, DashboardView, TerminalView, SignalsView, PnLView, AutoTradeView });
