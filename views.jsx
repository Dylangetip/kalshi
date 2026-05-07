// Views — Opportunities, Dashboard, Terminal, Signals, P&L

const { useState: useState_v, useEffect: useEffect_v, useMemo: useMemo_v, useRef: useRef_v } = React;

// Plain-English explanation for the current liveStatus, based on the
// position fields and the city's full state.
function liveStatusReason(p, cs) {
  const max = p.maxSoFarF;
  const rem = cs?.forecastRemainderMaxF ?? null;
  // Use the bet's own bracket bounds — the kalshi ladder may have rotated
  // since the bet was placed, so cs.brackets.find() is unreliable.
  const lo = p.bracketLo;
  const hi = p.bracketHi;
  const isUpperTail = (p.bracket || '').startsWith('≥');
  const isLowerTail = (p.bracket || '').startsWith('≤');
  const showHi = isUpperTail ? '∞' : `${hi}°`;
  const showLo = isLowerTail ? '−∞' : `${lo}°`;
  if (p.liveStatus === 'awaiting_settlement') {
    if (p.marketClosed)
      return `Market closed on ${p.targetDate}. Awaiting the NWS Climate Report to settle.`;
    return `Bet is for ${p.targetDate}; today's weather doesn't apply yet.`;
  }
  if (p.liveStatus === 'locked_win')
    return `The day's max is already inside ${p.bracket} and the remaining forecast peak (${rem != null ? rem.toFixed(1) + '°' : 'n/a'}) can't push it out.`;
  if (p.liveStatus === 'locked_loss') {
    if (max != null && hi != null && !isUpperTail && max > hi)
      return `Day's max already hit ${max.toFixed(1)}°, exceeding the ${hi}° cap. Bracket can't be reached.`;
    return `Both today's max (${max?.toFixed(1) ?? '—'}°) and remaining forecast peak (${rem?.toFixed(1) ?? '—'}°) fall below the bracket floor — can't reach it.`;
  }
  if (p.liveStatus === 'in_bracket')
    return `Currently inside ${p.bracket} at ${max?.toFixed(1) ?? '—'}°. Forecast remainder (${rem?.toFixed(1) ?? '—'}°) could still push beyond ${showHi}.`;
  if (p.liveStatus === 'pending') {
    if (p.degreesFromBracket == null) return 'Awaiting first observation today.';
    const need = Math.abs(p.degreesFromBracket).toFixed(1);
    if (p.degreesFromBracket < 0) return `Need to climb +${need}° to reach ${showLo} (bracket floor). Forecast peak: ${rem?.toFixed(1) ?? '—'}°.`;
    return `Day's max already ${need}° above ${showHi} (bracket cap).`;
  }
  return '—';
}

// Drill-down row content — rich detail when an open-position row is expanded.
function PositionDrilldown({ position, cityState, colSpan }) {
  const cs = cityState;
  const p = position;
  const fmt = (x, suf = '°') => x == null ? '—' : `${Number(x).toFixed(1)}${suf}`;
  const sources = cs ? [
    ['GFS MOS', cs.mosMax],
    ['NAM MOS', cs.namMos],
    ['NWS forecast', cs.nwsForecast],
    ['ECMWF',  cs.ecmwfMax],
    ['Open-Meteo', cs.omMax],
    ['ML model', cs.mlMax],
    ['Ensemble', cs.modelMax],
    ['Active model', cs.activeMax],
  ] : [];
  return (
    <tr style={{ background: 'var(--bg-2)' }}>
      <td colSpan={colSpan} style={{ padding: '12px 16px', borderBottom: '2px solid var(--border)' }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 18, fontSize: 12 }}>
          <div>
            <div className="label" style={{ marginBottom: 6 }}>Today's progress</div>
            <table className="tbl" style={{ width: '100%', fontSize: 11 }}>
              <tbody>
                <tr><td>Today's max so far</td><td className="num-r mono">{fmt(p.maxSoFarF)}</td></tr>
                <tr><td>Forecast remainder peak</td><td className="num-r mono">{fmt(cs?.forecastRemainderMaxF)}</td></tr>
                <tr><td>Current observed temp</td><td className="num-r mono">{fmt(cs?.obsCurrent)}</td></tr>
                <tr><td>Bracket window</td><td className="num-r mono">{p.bracket}</td></tr>
                <tr><td>Distance from bracket</td><td className="num-r mono">{p.degreesFromBracket == null ? '—' : (p.degreesFromBracket === 0 ? 'inside' : (p.degreesFromBracket > 0 ? `+${p.degreesFromBracket}° over` : `${p.degreesFromBracket}° below`))}</td></tr>
              </tbody>
            </table>
          </div>
          <div>
            <div className="label" style={{ marginBottom: 6 }}>Forecast sources for day max</div>
            <table className="tbl" style={{ width: '100%', fontSize: 11 }}>
              <tbody>
                {sources.map(([name, val]) => (
                  <tr key={name}><td>{name}</td><td className="num-r mono">{fmt(val)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div style={{ marginTop: 12, padding: 10, background: 'var(--bg-1)', borderRadius: 4, border: '1px solid var(--border)', fontSize: 12, color: 'var(--fg-1)' }}>
          <span className="label" style={{ marginRight: 8 }}>Status reason:</span>
          {liveStatusReason(p, cs)}
        </div>
      </td>
    </tr>
  );
}

// Live "are we hitting?" cell for the open-positions tables. Reads
// liveStatus / maxSoFarF / degreesFromBracket fields populated by
// /api/positions and renders a colored badge or distance hint.
function LiveStatusCell({ position }) {
  const status = position.liveStatus;
  const max = position.maxSoFarF;
  const deg = position.degreesFromBracket;
  if (status === 'awaiting_settlement') {
    if (position.marketClosed) {
      // Trading window closed; bet is sitting waiting on NWS Climate Report.
      return <Pill kind="warn">needs settlement · {position.targetDate || ''}</Pill>;
    }
    // Bet for a future date — today's weather doesn't apply yet.
    return <Pill kind="muted">future · {position.targetDate || ''}</Pill>;
  }
  if (status == null || max == null) {
    return <span className="mono" style={{ color: 'var(--fg-3)' }}>—</span>;
  }
  const maxStr = `${max.toFixed(1)}°`;
  if (status === 'locked_win')  return <Pill kind="pos" dot>WON · {maxStr}</Pill>;
  if (status === 'locked_loss') return <Pill kind="neg" dot>LOST · {maxStr}</Pill>;
  if (status === 'in_bracket')  return <Pill kind="info" dot>IN · {maxStr}</Pill>;
  // pending — show how far we still need to move
  if (deg == null || deg === 0) return <Pill kind="muted">pending · {maxStr}</Pill>;
  const sign = deg > 0 ? '−' : '+';   // we exceeded by deg → need to come down; we're below → need to climb
  const need = Math.abs(deg).toFixed(1);
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <span className="mono" style={{ color: 'var(--fg-2)', fontSize: 11 }}>{maxStr}</span>
      <Pill kind="muted">need {sign}{need}°</Pill>
    </span>
  );
}

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
function PnLView({ history, positions, closedPositions = [], states = [], liveHistory = false, stats = null }) {
  const stateByCity = React.useMemo(() => {
    const m = {};
    for (const s of states || []) if (s?.city?.code) m[s.city.code] = s;
    return m;
  }, [states]);
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
        {(() => {
          // Account-balance model: behaves like a real bank account. The
          // headline number is total equity (starting cash + realized P/L)
          // — your true net worth in the system. Open stakes are still
          // "yours" (just locked until settlement), so they're shown in the
          // subtitle along with deployable cash.
          const baseline = 10000;
          const realized = (stats && typeof stats.realizedPl === 'number') ? stats.realizedPl : 0;
          const openStakes = (stats && typeof stats.openStakes === 'number') ? stats.openStakes : 0;
          const totalEquity = baseline + realized;        // settled net worth
          const cash = totalEquity - openStakes;          // deployable right now
          const ret = (totalEquity - baseline) / baseline;
          const cls = ret > 0 ? 'pos' : (ret < 0 ? 'neg' : '');
          return (
            <div className="signal-card">
              <div className="label">Account balance</div>
              <div className={`big-num ${cls || ''}`}>{fmtUSD(totalEquity)}</div>
              <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
                {fmtUSD(cash)} cash · ${Math.round(openStakes).toLocaleString()} open · {fmtSign(realized, 0)} settled
              </div>
            </div>
          );
        })()}
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
        {(() => {
          // "Locked P/L" — sum of guaranteed wins minus guaranteed losses
          // from positions whose outcome is already mathematically certain.
          const lockedProfit = positions.reduce((s, p) => p.liveStatus === 'locked_win' ? s + (p.ifWin || 0) : s, 0);
          const lockedLoss   = positions.reduce((s, p) => p.liveStatus === 'locked_loss' ? s + (p.ifLose || 0) : s, 0);
          const net = lockedProfit + lockedLoss;
          const cls = net > 0 ? 'pos' : (net < 0 ? 'neg' : '');
          const lockedW = positions.filter(p => p.liveStatus === 'locked_win').length;
          const lockedL = positions.filter(p => p.liveStatus === 'locked_loss').length;
          return (
            <div className="signal-card">
              <div className="label">Locked profit (today)</div>
              <div className={`big-num ${cls}`}>{net >= 0 ? '+' : ''}${Math.round(net).toLocaleString()}</div>
              <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
                <span className="pos">+${Math.round(lockedProfit).toLocaleString()}</span>
                <span style={{ color: 'var(--fg-3)' }}> · </span>
                <span className="neg">${Math.round(lockedLoss).toLocaleString()}</span>
                <span style={{ color: 'var(--fg-3)' }}> · {lockedW}W/{lockedL}L</span>
              </div>
            </div>
          );
        })()}
      </div>

      {(() => {
        const lockedW = positions.filter(p => p.liveStatus === 'locked_win').length;
        const lockedL = positions.filter(p => p.liveStatus === 'locked_loss').length;
        const inB     = positions.filter(p => p.liveStatus === 'in_bracket').length;
        if (positions.length === 0) return null;
        return (
          <div style={{ padding: '6px 12px', background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12, display: 'flex', gap: 16, alignItems: 'center' }}>
            <span style={{ color: 'var(--fg-2)' }}>Already decided:</span>
            <span className="pos mono">{lockedW} locked WIN</span>
            <span className="neg mono">{lockedL} locked LOSS</span>
            <span style={{ color: 'var(--accent)' }} className="mono">{inB} currently in bracket</span>
            <span style={{ color: 'var(--fg-3)' }} className="mono">{positions.length - lockedW - lockedL - inB} pending</span>
            <span style={{ flex: 1 }} />
            <span className="mono" style={{ color: 'var(--fg-3)' }}>{positions.length} open</span>
          </div>
        );
      })()}

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
          {(() => {
            const [pageSize, setPageSize] = React.useState(20);
            const [page, setPage] = React.useState(0);
            const [expandedId, setExpandedId] = React.useState(null);
            // Open positions = anything not yet decided. Locked wins/losses
            // live in their own "Closed positions" panel below.
            const liveOpen = positions.filter(p =>
              p.liveStatus !== 'locked_win' && p.liveStatus !== 'locked_loss'
            );
            const totalPages = Math.ceil(liveOpen.length / pageSize);
            const slice = liveOpen.slice(page * pageSize, (page + 1) * pageSize);
            const sliceExposed = slice.reduce((s, p) => s + p.size, 0);
            return (
              <>
                <div className="panel-header">
                  <span>Open positions</span>
                  <span className="panel-title-actions" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span>{liveOpen.length} undecided · ${liveOpen.reduce((s, p) => s + p.size, 0).toLocaleString()} exposed</span>
                    <select
                      value={pageSize}
                      onChange={e => { setPageSize(+e.target.value); setPage(0); }}
                      style={{ background: 'var(--bg-2)', color: 'var(--fg-1)', border: '1px solid var(--border)', borderRadius: 4, padding: '1px 4px', fontSize: 11 }}
                    >
                      {[20, 40, 60, 80, 100].map(n => <option key={n} value={n}>{n} / page</option>)}
                    </select>
                  </span>
                </div>
                <table className="tbl">
                  <thead>
                    <tr>
                      <th></th>
                      <th>City</th><th>Bracket</th><th>Live</th><th>Side</th>
                      <th className="num-r">Stake</th><th className="num-r">Entry</th>
                      <th className="num-r pos">If WIN</th><th className="num-r neg">If LOSE</th>
                    </tr>
                  </thead>
                  <tbody>
                    {slice.map(p => {
                      const isOpen = expandedId === p.id;
                      return (
                        <React.Fragment key={p.id}>
                          <tr style={{ cursor: 'pointer' }} onClick={() => setExpandedId(isOpen ? null : p.id)}>
                            <td style={{ width: 18, textAlign: 'center', color: 'var(--fg-3)' }}>{isOpen ? '▾' : '▸'}</td>
                            <td className="city-cell">{p.city}</td>
                            <td>{p.bracket}</td>
                            <td><LiveStatusCell position={p} /></td>
                            <td className={p.side === 'YES' ? 'pos' : 'neg'}>{p.side}</td>
                            <td className="num-r">${p.size}</td>
                            <td className="num-r">{(p.entry * 100).toFixed(0)}¢</td>
                            <td className="num-r pos">+${Math.round(p.ifWin ?? (p.size * (1 - p.entry) / p.entry)).toLocaleString()}</td>
                            <td className="num-r neg">-${p.size}</td>
                          </tr>
                          {isOpen && <PositionDrilldown position={p} cityState={stateByCity[p.city]} colSpan={9} />}
                        </React.Fragment>
                      );
                    })}
                  </tbody>
                </table>
                {totalPages > 1 && (
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 10px', borderTop: '1px solid var(--border)', fontSize: 11, color: 'var(--fg-2)' }}>
                    <span>Showing {page * pageSize + 1}–{Math.min((page + 1) * pageSize, liveOpen.length)} of {liveOpen.length} · ${sliceExposed.toLocaleString()} on this page</span>
                    <span style={{ display: 'flex', gap: 4 }}>
                      <button className="btn-sm" onClick={() => setPage(0)} disabled={page === 0}>«</button>
                      <button className="btn-sm" onClick={() => setPage(p => p - 1)} disabled={page === 0}>‹</button>
                      <span style={{ padding: '0 6px', lineHeight: '22px' }}>pg {page + 1} / {totalPages}</span>
                      <button className="btn-sm" onClick={() => setPage(p => p + 1)} disabled={page >= totalPages - 1}>›</button>
                      <button className="btn-sm" onClick={() => setPage(totalPages - 1)} disabled={page >= totalPages - 1}>»</button>
                    </span>
                  </div>
                )}
              </>
            );
          })()}
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

      <ClosedPositionsPanel
        positions={positions}
        closedPositions={closedPositions}
        stateByCity={stateByCity}
      />
    </div>
  );
}

function ClosedPositionsPanel({ positions, closedPositions, stateByCity }) {
  const [pageSize, setPageSize] = React.useState(20);
  const [page, setPage] = React.useState(0);
  const [expandedId, setExpandedId] = React.useState(null);
  const [tab, setTab] = React.useState('locked');
  const [sortKey, setSortKey] = React.useState('recent');

  const positionsArr = Array.isArray(positions) ? positions : [];
  const closedArr = Array.isArray(closedPositions) ? closedPositions : [];
  // Locked-still-open: positions whose outcome is decided but the daily
  // settlement job hasn't moved them to status='settled' yet.
  const lockedLive = positionsArr.filter(
    p => p.liveStatus === 'locked_win' || p.liveStatus === 'locked_loss'
  );
  // De-dupe: if a settled row and a locked-live row share an id, prefer the
  // settled one (post-settlement is the canonical record).
  const settledIds = new Set(closedArr.map(p => p.id));
  const closed = [
    ...closedArr,
    ...lockedLive.filter(p => !settledIds.has(p.id)),
  ];
  const awaiting = positionsArr.filter(p => p.liveStatus === 'awaiting_settlement');

  const outcomeOf = p => p.liveStatus === 'locked_win' ? (p.ifWin || 0) : (p.ifLose || 0);
  const dateKey = p => {
    if (p.settledAt) return p.settledAt * 1000;
    if (p.placedAt) return p.placedAt;
    if (p.targetDate) return new Date(p.targetDate).getTime();
    return 0;
  };

  const sorted = React.useMemo(() => {
    const arr = [...(tab === 'locked' ? closed : awaiting)];
    if (sortKey === 'recent')   arr.sort((a, b) => dateKey(b) - dateKey(a));
    if (sortKey === 'oldest')   arr.sort((a, b) => dateKey(a) - dateKey(b));
    if (sortKey === 'biggest')  arr.sort((a, b) => outcomeOf(b) - outcomeOf(a));
    if (sortKey === 'worst')    arr.sort((a, b) => outcomeOf(a) - outcomeOf(b));
    if (sortKey === 'stake')    arr.sort((a, b) => (b.size || 0) - (a.size || 0));
    if (sortKey === 'city')     arr.sort((a, b) => (a.city || '').localeCompare(b.city || ''));
    return arr;
  }, [tab, closed, awaiting, sortKey]);

  if (closed.length === 0 && awaiting.length === 0) return null;

  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const slice = sorted.slice(page * pageSize, (page + 1) * pageSize);
  const closedNet = closed.reduce((s, p) => s + outcomeOf(p), 0);
  const wins = closed.filter(p => p.liveStatus === 'locked_win').length;
  const losses = closed.length - wins;

  return (
    <div className="panel">
      <div className="panel-header">
        <span>Closed positions</span>
        <span className="panel-title-actions" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <button className="btn-sm" onClick={() => { setTab('locked'); setPage(0); }}
            style={{ fontWeight: tab === 'locked' ? 600 : 400 }}>
            Closed ({closed.length})
          </button>
          <button className="btn-sm" onClick={() => { setTab('awaiting'); setPage(0); }}
            style={{ fontWeight: tab === 'awaiting' ? 600 : 400 }}>
            Awaiting settlement ({awaiting.length})
          </button>
          {tab === 'locked' && closed.length > 0 && (
            <span className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
              {wins}W / {losses}L ·{' '}
              <span className={closedNet >= 0 ? 'pos' : 'neg'}>
                {closedNet >= 0 ? '+' : ''}${Math.round(closedNet).toLocaleString()}
              </span>
            </span>
          )}
          <select
            value={sortKey}
            onChange={e => { setSortKey(e.target.value); setPage(0); }}
            style={{ background: 'var(--bg-2)', color: 'var(--fg-1)', border: '1px solid var(--border)', borderRadius: 4, padding: '1px 4px', fontSize: 11 }}
          >
            <option value="recent">Most recent</option>
            <option value="oldest">Oldest first</option>
            <option value="biggest">Biggest win</option>
            <option value="worst">Biggest loss</option>
            <option value="stake">Largest stake</option>
            <option value="city">City (A–Z)</option>
          </select>
          <select
            value={pageSize}
            onChange={e => { setPageSize(+e.target.value); setPage(0); }}
            style={{ background: 'var(--bg-2)', color: 'var(--fg-1)', border: '1px solid var(--border)', borderRadius: 4, padding: '1px 4px', fontSize: 11 }}
          >
            {[20, 40, 60, 80, 100].map(n => <option key={n} value={n}>{n} / page</option>)}
          </select>
        </span>
      </div>
      <table className="tbl">
        <thead>
          <tr>
            <th></th>
            <th>City</th><th>Bracket</th><th>Date</th><th>Live</th><th>Side</th>
            <th className="num-r">Stake</th><th className="num-r">Entry</th>
            <th className="num-r">Outcome P/L</th>
          </tr>
        </thead>
        <tbody>
          {slice.map(p => {
            const isOpen = expandedId === p.id;
            const win = p.liveStatus === 'locked_win';
            const isLocked = win || p.liveStatus === 'locked_loss';
            const outcome = isLocked ? (win ? p.ifWin : p.ifLose) : null;
            return (
              <React.Fragment key={p.id}>
                <tr style={{ cursor: 'pointer' }} onClick={() => setExpandedId(isOpen ? null : p.id)}>
                  <td style={{ width: 18, textAlign: 'center', color: 'var(--fg-3)' }}>{isOpen ? '▾' : '▸'}</td>
                  <td className="city-cell">{p.city}</td>
                  <td>{p.bracket}</td>
                  <td className="mono" style={{ fontSize: 11, color: 'var(--fg-3)' }}>{p.targetDate || '—'}</td>
                  <td><LiveStatusCell position={p} /></td>
                  <td className={p.side === 'YES' ? 'pos' : 'neg'}>{p.side}</td>
                  <td className="num-r">${p.size}</td>
                  <td className="num-r">{(p.entry * 100).toFixed(0)}¢</td>
                  <td className={`num-r ${outcome != null ? (outcome > 0 ? 'pos' : 'neg') : ''}`}>
                    {outcome != null ? (outcome > 0 ? '+$' : '-$') + Math.abs(Math.round(outcome)).toLocaleString() : '—'}
                  </td>
                </tr>
                {isOpen && <PositionDrilldown position={p} cityState={stateByCity[p.city]} colSpan={9} />}
              </React.Fragment>
            );
          })}
        </tbody>
      </table>
      {totalPages > 1 && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 10px', borderTop: '1px solid var(--border)', fontSize: 11, color: 'var(--fg-2)' }}>
          <span>Showing {page * pageSize + 1}–{Math.min((page + 1) * pageSize, sorted.length)} of {sorted.length}</span>
          <span style={{ display: 'flex', gap: 4 }}>
            <button className="btn-sm" onClick={() => setPage(0)} disabled={page === 0}>«</button>
            <button className="btn-sm" onClick={() => setPage(p => p - 1)} disabled={page === 0}>‹</button>
            <span style={{ padding: '0 6px', lineHeight: '22px' }}>pg {page + 1} / {totalPages}</span>
            <button className="btn-sm" onClick={() => setPage(p => p + 1)} disabled={page >= totalPages - 1}>›</button>
            <button className="btn-sm" onClick={() => setPage(totalPages - 1)} disabled={page >= totalPages - 1}>»</button>
          </span>
        </div>
      )}
    </div>
  );
}

// ====== AUTO-TRADE ======

function AccuracyBlock({ title, stats }) {
  if (!stats || stats.n === 0) {
    return (
      <div className="signal-card">
        <div className="label">{title}</div>
        <div className="big-num" style={{ color: 'var(--fg-3)' }}>—</div>
        <div className="mono" style={{ fontSize: 11, color: 'var(--fg-3)' }}>no data yet</div>
      </div>
    );
  }
  const buckets = stats.buckets || {};
  // Mutually-exclusive bins — each maps to an outcome on a 1°F-wide
  // Kalshi bracket. Sum to 100%.
  const rows = [
    { key: 'exact',      label: 'Exact bracket',  range: '0–1°F', pct: buckets.exact,      cls: 'pos' },
    { key: 'one_off',    label: 'One neighbor',   range: '1–2°F', pct: buckets.one_off,    cls: 'info' },
    { key: 'two_off',    label: 'Two off',        range: '2–3°F', pct: buckets.two_off,    cls: 'warn' },
    { key: 'three_plus', label: 'Far miss',       range: '>3°F',  pct: buckets.three_plus, cls: 'neg' },
  ];
  return (
    <div className="signal-card">
      <div className="label">{title}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <span className="big-num">{stats.mae}°F</span>
        <span className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>MAE · n={stats.n}</span>
      </div>
      <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 3 }}>
        {rows.map(r => (
          <div key={r.key} style={{ display: 'grid', gridTemplateColumns: '90px 1fr 50px', alignItems: 'center', gap: 6, fontSize: 10, fontFamily: 'var(--mono)' }}>
            <span>
              <span className={r.cls}>{r.label}</span>
              <span style={{ color: 'var(--fg-3)', marginLeft: 4 }}>{r.range}</span>
            </span>
            <div style={{ background: 'var(--bg-3)', borderRadius: 1, height: 6, overflow: 'hidden' }}>
              <div className={`bar-fill ${r.cls}`}
                   style={{ height: '100%', width: `${Math.max(0, Math.min(100, r.pct || 0))}%`, transition: 'width .3s' }} />
            </div>
            <span className={r.cls} style={{ textAlign: 'right', fontWeight: 600 }}>
              {r.pct != null ? r.pct + '%' : '—'}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// Sub-component so its useState_v doesn't conflict with hook ordering
// inside the parent's IIFE-rendered branches. Always shows the seeded
// pill + "force re-seed" expander when paired ≥ 1000; otherwise the
// initial-seed input/button row.
function BackfillSeedRow({ data, years, setYears, busy, backfillRunning, progress, click, onMlBackfill }) {
  const [forceShowSeed, setForceShowSeed] = useState_v(false);
  const seeded = (data?.paired || 0) >= 1000;
  return (
    <div style={{ padding: '0 14px 14px', display: 'flex', gap: 8,
                  alignItems: 'center', flexWrap: 'wrap' }}>
      {seeded ? (
        <>
          <Pill kind="pos" dot>Historical seed: {data.paired?.toLocaleString() || 0} pairs</Pill>
          <span className="mono" style={{ fontSize: 11, color: 'var(--fg-3)' }}>
            {data.earliest_target_date} → {data.latest_target_date}
          </span>
          <button className="btn-sm" onClick={() => setForceShowSeed(s => !s)}>
            {forceShowSeed ? 'cancel re-seed' : 'force re-seed'}
          </button>
          {forceShowSeed && (
            <>
              <input
                type="number" min={1} max={20} value={years}
                disabled={!!busy || backfillRunning}
                onChange={(e) => setYears(Math.max(1, Math.min(20, +e.target.value || 3)))}
                style={{ width: 60, fontSize: 13, padding: '6px 8px' }} />
              <span className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>yrs</span>
              <button
                className="btn primary"
                disabled={!!busy || backfillRunning}
                onClick={() => click('backfill', () => onMlBackfill({ years }))}>
                {backfillRunning ? <><span className="spinner" />Re-seeding…</> :
                 busy === 'backfill' ? <><span className="spinner" />starting…</> :
                 `Re-seed ${years} yr${years === 1 ? '' : 's'}`}
              </button>
            </>
          )}
        </>
      ) : (
        <>
          <span className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
            Initial historical seed:
          </span>
          <input
            type="number" min={1} max={20} value={years}
            disabled={!!busy || backfillRunning}
            onChange={(e) => setYears(Math.max(1, Math.min(20, +e.target.value || 3)))}
            style={{ width: 60, fontSize: 13, padding: '6px 8px' }}
            title="Years of historical data to pull." />
          <span className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>yrs</span>
          <button
            className="btn primary"
            disabled={!!busy || backfillRunning}
            onClick={() => click('backfill', () => onMlBackfill({ years }))}>
            {backfillRunning ? <><span className="spinner" />Backfilling {progress.cities_done || 0}/5</> :
             busy === 'backfill' ? <><span className="spinner" />starting…</> : `Pull initial ${years} yr${years === 1 ? '' : 's'}`}
          </button>
        </>
      )}
    </div>
  );
}


function MlTrainingPanel({ mlInfo, onMlBackfill, onMlTrain }) {
  const [busy, setBusy] = useState_v(null);  // 'backfill' | 'train-linear' | 'train-gbm' | null
  const [busyStartedAt, setBusyStartedAt] = useState_v(null);
  const [lastResult, setLastResult] = useState_v(null);
  const [tick, setTick] = useState_v(0);  // forces elapsed-seconds re-render
  const [years, setYears] = useState_v(3);  // backfill lookback

  const data = mlInfo?.data || {};
  const run = mlInfo?.latest_run || {};
  const recentRuns = mlInfo?.recent_runs || [];
  const progress = mlInfo?.backfill_progress || {};
  const trained = run.trained;
  const trainedAt = run.trained_at ? new Date(run.trained_at * 1000).toLocaleString() : 'never';
  const backfillRunning = !!progress.running;

  // Elapsed-seconds counter while a sync action is running
  useEffect_v(() => {
    if (!busy) return;
    const t = setInterval(() => setTick(x => x + 1), 250);
    return () => clearInterval(t);
  }, [busy]);
  const elapsedSec = busyStartedAt ? Math.floor((Date.now() - busyStartedAt) / 1000) : 0;

  const click = async (kind, fn) => {
    setBusy(kind);
    setBusyStartedAt(Date.now());
    setLastResult(null);
    try {
      const r = await fn();
      setLastResult({ kind, result: r, ok: !(r && r.error), at: Date.now() });
    } catch (exc) {
      setLastResult({ kind, result: { error: String(exc) }, ok: false, at: Date.now() });
    } finally {
      setBusy(null);
      setBusyStartedAt(null);
    }
  };

  // ── Status banner content ──
  let banner = null;
  if (backfillRunning) {
    banner = (
      <div className="status-banner busy">
        <span className="spinner" />
        <span className="b-title">Backfilling historical data…</span>
        <span style={{ marginLeft: 8 }}>{progress.stage}</span>
        <span className="b-detail">
          {progress.cities_done || 0}/5 cities · {progress.predictions_inserted || 0} preds · {progress.actuals_inserted || 0} actuals
        </span>
      </div>
    );
  } else if (busy === 'train-linear' || busy === 'train-gbm') {
    const algo = busy === 'train-gbm' ? 'gradient boosting' : 'linear regression';
    banner = (
      <div className="status-banner busy">
        <span className="spinner" />
        <span className="b-title">Training {algo} on {data.paired || 0} pairs…</span>
        <span className="b-detail">elapsed {elapsedSec}s</span>
      </div>
    );
  } else if (busy === 'backfill') {
    banner = (
      <div className="status-banner busy">
        <span className="spinner" />
        <span className="b-title">Starting backfill…</span>
        <span className="b-detail">contacting Open-Meteo + IEM</span>
      </div>
    );
  } else if (lastResult) {
    const r = lastResult.result || {};
    if (!lastResult.ok) {
      banner = (
        <div className="status-banner error">
          <span className="b-title">✕ {lastResult.kind} failed:</span>
          <span style={{ marginLeft: 8 }}>{r.error || 'unknown error'}</span>
        </div>
      );
    } else if (lastResult.kind.startsWith('train') && r.test_mae != null) {
      const beat = r.holdout_mae_ensemble != null && r.test_mae < r.holdout_mae_ensemble;
      const cands = r.auto_candidates;
      banner = (
        <div className="status-banner success" style={{ flexWrap: 'wrap' }}>
          <span className="b-title">✓ Trained {r.algorithm}</span>
          <span style={{ marginLeft: 8 }}>
            test MAE <strong>{r.test_mae}°F</strong>{' '}
            {r.holdout_mae_ensemble != null && (
              <>vs ensemble <strong>{r.holdout_mae_ensemble}°F</strong>{' · '}</>
            )}
            on {r.n_test} holdout days
            {r.holdout_mae_ensemble != null && (
              <span className={beat ? 'pos' : 'neg'} style={{ marginLeft: 4 }}>
                {beat ? `ML wins by ${(r.holdout_mae_ensemble - r.test_mae).toFixed(2)}°F` : 'ensemble still wins'}
              </span>
            )}
          </span>
          {cands && cands.length > 0 && (
            <span style={{ width: '100%', marginTop: 6, fontSize: 11, color: 'var(--fg-2)' }}>
              swept {cands.length}: {cands.map(c => `${c.algorithm}=${c.test_mae}°`).join(' · ')}
            </span>
          )}
        </div>
      );
    } else if (lastResult.kind === 'backfill' && r.started) {
      banner = (
        <div className="status-banner success">
          <span className="b-title">✓ Backfill started</span>
          <span style={{ marginLeft: 8 }}>
            {r.start_date} → {r.end_date} · watch progress above
          </span>
        </div>
      );
    }
  }

  return (
    <div className="panel">
      <div className="panel-header">
        <span>Machine learning</span>
        <span className="panel-title-actions">
          {trained ? `${run.algorithm} · trained ${trainedAt}` : 'no model trained yet'}
        </span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10, padding: 14 }}>
        <div className="signal-card">
          <div className="label">Training pairs</div>
          <div className="big-num">{data.paired || 0}</div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
            {data.predictions || 0} preds · {data.actuals || 0} actuals
          </div>
        </div>
        <div className="signal-card">
          <div className="label">Date range</div>
          <div className="mono" style={{ fontSize: 13, marginTop: 6 }}>
            {data.earliest_target_date || '—'}
          </div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
            → {data.latest_target_date || '—'}
          </div>
        </div>
        <div className="signal-card">
          <div className="label">Holdout MAE</div>
          {trained ? (
            <>
              <div className="big-num">{run.test_mae}°F</div>
              <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
                vs ensemble {run.holdout_mae_ensemble}°F
              </div>
            </>
          ) : (
            <>
              <div className="big-num" style={{ color: 'var(--fg-3)' }}>—</div>
              <div className="mono" style={{ fontSize: 11, color: 'var(--fg-3)' }}>train first</div>
            </>
          )}
        </div>
        <div className="signal-card">
          <div className="label">vs Ensemble</div>
          {trained && run.holdout_mae_ensemble != null ? (
            <>
              <div className={`big-num ${run.test_mae < run.holdout_mae_ensemble ? 'pos' : 'neg'}`}>
                {fmtSign(run.holdout_mae_ensemble - run.test_mae, 2)}°F
              </div>
              <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
                {run.test_mae < run.holdout_mae_ensemble ? 'ML beats ensemble' : 'ensemble still wins'}
              </div>
            </>
          ) : (
            <>
              <div className="big-num" style={{ color: 'var(--fg-3)' }}>—</div>
              <div className="mono" style={{ fontSize: 11, color: 'var(--fg-3)' }}>—</div>
            </>
          )}
        </div>
      </div>

      {banner}

      <BackfillSeedRow data={data} years={years} setYears={setYears}
        busy={busy} backfillRunning={backfillRunning}
        progress={progress} click={click} onMlBackfill={onMlBackfill} />
      <div style={{ padding: '0 14px 14px', display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <button
          className="btn success"
          disabled={!!busy || backfillRunning || (data.paired || 0) < 50}
          onClick={() => click('train-auto', () => onMlTrain('auto'))}
          title="Sweeps Ridge + Random Forest + GBM grid; keeps the lowest-MAE">
          {busy === 'train-auto' ? <><span className="spinner" />sweeping {elapsedSec}s</> : 'Auto-train (sweep all)'}
        </button>
        <button
          className="btn"
          disabled={!!busy || backfillRunning || (data.paired || 0) < 20}
          onClick={() => click('train-linear', () => onMlTrain('linear'))}>
          {busy === 'train-linear' ? <><span className="spinner" />training {elapsedSec}s</> : 'Train (linear)'}
        </button>
        <button
          className="btn"
          disabled={!!busy || backfillRunning || (data.paired || 0) < 50}
          onClick={() => click('train-gbm', () => onMlTrain('gbm'))}>
          {busy === 'train-gbm' ? <><span className="spinner" />training {elapsedSec}s</> : 'Train (gbm)'}
        </button>
        <button
          className="btn"
          disabled={!!busy || backfillRunning || (data.paired || 0) < 50}
          onClick={() => click('train-rf', () => onMlTrain('rf'))}>
          {busy === 'train-rf' ? <><span className="spinner" />training {elapsedSec}s</> : 'Train (rf)'}
        </button>
        <button
          className="btn"
          disabled={!!busy || backfillRunning || (data.paired || 0) < 100}
          onClick={() => click('train-stack', () => onMlTrain('stack'))}
          title="Stacking: combines linear + rf + gbm via a meta-model. Often beats any individual algo by 1-3%.">
          {busy === 'train-stack' ? <><span className="spinner" />stacking {elapsedSec}s</> : 'Train (stack)'}
        </button>
        <button
          className="btn"
          disabled={!!busy || backfillRunning || (data.paired || 0) < 250}
          onClick={() => click('train-per-city', () => onMlTrain('per-city'))}
          title="One model per city. Captures microclimate patterns the global model can't.">
          {busy === 'train-per-city' ? <><span className="spinner" />per-city {elapsedSec}s</> : 'Train (per-city)'}
        </button>
      </div>

      <div style={{ padding: '0 14px 14px', display: 'flex', gap: 14, fontSize: 11, color: 'var(--fg-2)', fontFamily: 'var(--mono)' }}>
        <span>
          retrain loop: {mlInfo?.retrain_loop_disabled ? <span className="neg">disabled</span> :
            <span className="pos">every {Math.round((mlInfo?.retrain_interval_seconds || 86400) / 3600)}h</span>}
        </span>
        <span>·</span>
        <span>
          incremental backfill: {mlInfo?.backfill_loop_disabled ? <span className="neg">disabled</span> :
            <span className="pos">every {Math.round((mlInfo?.backfill_interval_seconds || 86400) / 3600)}h</span>}
        </span>
        <span>·</span>
        <span>both algos trained each cycle, predictor loads newest run</span>
      </div>

      {recentRuns.length >= 2 && (
        <div style={{ padding: '0 14px 14px' }}>
          <div className="label" style={{ marginBottom: 8 }}>Test MAE trend (newest → oldest)</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <Sparkline
              data={recentRuns.slice().reverse().map(r => r.test_mae).filter(v => v != null)}
              width={240} height={40} fill={true}
            />
            <div className="mono" style={{ fontSize: 11, color: 'var(--fg-2)' }}>
              {(() => {
                const xs = recentRuns.slice().reverse().map(r => r.test_mae).filter(v => v != null);
                if (xs.length < 2) return '';
                const first = xs[0], last = xs[xs.length - 1];
                const delta = last - first;
                return `${first.toFixed(2)}° → ${last.toFixed(2)}° · ${delta < 0 ? 'improving by' : 'worse by'} ${Math.abs(delta).toFixed(2)}° over ${xs.length} runs`;
              })()}
            </div>
          </div>
        </div>
      )}

      {recentRuns.length > 0 && (
        <div style={{ padding: '0 14px 14px' }}>
          <div className="label" style={{ marginBottom: 8 }}>Training history</div>
          <table className="tbl">
            <thead>
              <tr>
                <th>When</th><th>Algorithm</th>
                <th className="num-r">N train</th><th className="num-r">N test</th>
                <th className="num-r">Train MAE</th>
                <th className="num-r">Test MAE</th>
                <th className="num-r">Ens MAE</th>
                <th className="num-r">Δ</th>
              </tr>
            </thead>
            <tbody>
              {recentRuns.map(r => {
                const beat = r.test_mae != null && r.holdout_mae_ensemble != null && r.test_mae < r.holdout_mae_ensemble;
                const delta = r.holdout_mae_ensemble != null && r.test_mae != null
                  ? r.holdout_mae_ensemble - r.test_mae : null;
                return (
                  <tr key={r.id}>
                    <td>{ago(r.trained_at * 1000)} ago</td>
                    <td>{r.algorithm}</td>
                    <td className="num-r">{r.n_train}</td>
                    <td className="num-r">{r.n_test}</td>
                    <td className="num-r">{r.train_mae != null ? r.train_mae + '°' : '—'}</td>
                    <td className="num-r">{r.test_mae != null ? r.test_mae + '°' : '—'}</td>
                    <td className="num-r">{r.holdout_mae_ensemble != null ? r.holdout_mae_ensemble + '°' : '—'}</td>
                    <td className={`num-r ${delta != null ? (beat ? 'pos' : 'neg') : ''}`}>
                      {delta != null ? fmtSign(delta, 2) + '°' : '—'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Model source picker — choose which prediction drives the auto-trader
// edge calculations and the bracket-probability ladder.
function ModelSourcePanel({ mlInfo, onModelConfig }) {
  const [busy, setBusy] = useState_v(false);
  const [src, setSrc] = useState_v(null);
  const [alpha, setAlpha] = useState_v(null);
  // Optimistic override: after Apply succeeds we hold the new value here
  // until the next /api/ml/info poll catches up, so the UI doesn't flash
  // back to the previous active source for ~60s.
  const [appliedSrc, setAppliedSrc] = useState_v(null);
  const [appliedAlpha, setAppliedAlpha] = useState_v(null);
  const liveSrc = appliedSrc ?? mlInfo?.modelmax_source ?? 'auto';
  const liveAlpha = appliedAlpha ?? mlInfo?.blend_alpha ?? 0.7;
  const curSrc = src ?? liveSrc;
  const curAlpha = alpha ?? liveAlpha;
  const dirty = curSrc !== liveSrc || curAlpha !== liveAlpha;

  // Once mlInfo catches up to our optimistic override, drop the override
  // so future server-side changes (e.g. from another tab) flow through.
  React.useEffect(() => {
    if (appliedSrc != null && mlInfo?.modelmax_source === appliedSrc) setAppliedSrc(null);
    if (appliedAlpha != null && mlInfo?.blend_alpha === appliedAlpha) setAppliedAlpha(null);
  }, [mlInfo, appliedSrc, appliedAlpha]);

  const apply = async () => {
    setBusy(true);
    try {
      const r = await fetch((window.__BETS_API__ || '') + '/api/model/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ modelmax_source: curSrc, blend_alpha: curAlpha }),
      });
      const body = r.ok ? await r.json().catch(() => null) : null;
      setAppliedSrc(body?.modelmax_source ?? curSrc);
      setAppliedAlpha(body?.blend_alpha ?? curAlpha);
      setSrc(null); setAlpha(null);
      // Trigger a parent refresh so other panels (ml info, accuracy) reflect
      // the swap and so the optimistic override clears as soon as possible.
      if (onModelConfig) onModelConfig();
    } finally { setBusy(false); }
  };

  const sources = [
    { key: 'auto',     label: 'Auto',     desc: 'Use the blend if ML is trained, else fall back to the ensemble. Safest default.' },
    { key: 'ml',       label: 'ML only',  desc: 'Pure trained scikit-learn model. Best raw accuracy on holdout but doesn\'t see live MOS data.' },
    { key: 'blend',    label: 'Blend',    desc: 'α · ML + (1−α) · Ensemble. Captures both the trained model and the live MOS signal.' },
    { key: 'ensemble', label: 'Ensemble', desc: 'Naive weighted average of MOS / NWS / ECMWF / Open-Meteo. Disables ML entirely.' },
  ];
  const curSource = sources.find(s => s.key === curSrc) || sources[0];

  return (
    <div className="panel">
      <div className="panel-header">
        <span>Active prediction model</span>
        <span className="panel-title-actions" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <span className="mono" style={{ fontSize: 11, color: 'var(--fg-3)' }}>
            currently: {liveSrc}{liveSrc === 'blend' || liveSrc === 'auto' ? ` · α=${liveAlpha.toFixed(2)}` : ''}
          </span>
          <button className="btn primary sm" disabled={busy || !dirty} onClick={apply}>
            {busy ? 'applying…' : 'Apply'}
          </button>
        </span>
      </div>
      <div style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 8 }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="label" style={{ minWidth: 70 }}>Source</span>
          <select
            value={curSrc}
            onChange={e => setSrc(e.target.value)}
            style={{
              background: 'var(--bg-2)', color: 'var(--fg-1)',
              border: '1px solid var(--border)', borderRadius: 4,
              padding: '4px 8px', fontSize: 13, minWidth: 160,
            }}
          >
            {sources.map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
          {curSrc !== liveSrc && (
            <span className="mono" style={{ fontSize: 11, color: 'var(--accent)' }}>
              unsaved — Apply to switch
            </span>
          )}
        </label>
        <div style={{ fontSize: 11, color: 'var(--fg-3)', lineHeight: 1.4, paddingLeft: 80 }}>
          {curSource.desc}
        </div>
      </div>
      {(curSrc === 'blend' || curSrc === 'auto') && (
        <div style={{ padding: '0 14px 14px' }}>
          <div className="label" style={{ marginBottom: 4 }}>
            Blend α (ML weight) — {(curAlpha * 100).toFixed(0)}% ML / {((1 - curAlpha) * 100).toFixed(0)}% Ensemble
          </div>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={curAlpha}
            onChange={e => setAlpha(+e.target.value)}
            style={{ width: '100%' }}
          />
          <div style={{ fontSize: 11, color: 'var(--fg-3)', marginTop: 4 }}>
            Higher α = more weight on the trained ML model. Lower α = more weight on the live ensemble. 0.7 is a sensible default.
          </div>
        </div>
      )}
      <div style={{ padding: '0 14px 14px', fontSize: 11, color: 'var(--fg-3)' }}>
        Changing the model invalidates the city-state cache so new edges show up immediately. The auto-trader uses the same active prediction.
      </div>
    </div>
  );
}


// Diagnostics panel — feature importance, correlations, MAE-by-week,
// per-city accuracy, residual scatter. R-studio-style introspection so
// the user can see what the model is actually learning + how it's
// improving over time.
function MlDiagnosticsPanel({ diagnostics }) {
  if (!diagnostics) {
    return (
      <div className="panel">
        <div className="panel-header"><span>Diagnostics</span></div>
        <div style={{ padding: 14, fontSize: 12, color: 'var(--fg-3)' }}>
          loading… (or no training data yet — run a backfill + train to populate).
        </div>
      </div>
    );
  }
  const fi = diagnostics.feature_importances || [];
  const corr = diagnostics.correlations || [];
  const week = diagnostics.mae_by_week || [];
  const city = diagnostics.per_city_mae || [];
  const resid = diagnostics.recent_residuals || [];

  const maxFi = Math.max(...fi.map(f => f.importance), 0.01);
  const maxAbsCorr = Math.max(...corr.map(c => Math.abs(c.r)), 0.01);
  const maxBy = Math.max(...week.map(w => w.mae), 0.01);
  const maxCityMae = Math.max(...city.map(c => c.mae), 0.01);

  // Residuals scatter — small SVG, predicted on x, actual on y, perfect
  // line at y=x. Points colored by abs error.
  const residualScatter = () => {
    if (resid.length === 0) return null;
    const xs = resid.map(r => r.predicted);
    const ys = resid.map(r => r.actual);
    const all = xs.concat(ys);
    const lo = Math.min(...all) - 2;
    const hi = Math.max(...all) + 2;
    const W = 320, H = 220, pad = 24;
    const x = v => pad + (v - lo) / (hi - lo) * (W - 2 * pad);
    const y = v => H - pad - (v - lo) / (hi - lo) * (H - 2 * pad);
    return (
      <svg width={W} height={H} style={{ display: 'block' }}>
        <line x1={x(lo)} y1={y(lo)} x2={x(hi)} y2={y(hi)}
              stroke="var(--fg-3)" strokeDasharray="4 4" strokeWidth="1" />
        {resid.map((r, i) => {
          const ae = Math.abs(r.error);
          const fill = ae < 1 ? 'var(--pos)' : ae < 3 ? 'var(--warn)' : 'var(--neg)';
          return <circle key={i} cx={x(r.predicted)} cy={y(r.actual)} r="3" fill={fill} opacity="0.7" />;
        })}
        <text x={pad} y={H - 4} fontSize="10" fill="var(--fg-3)">predicted →</text>
        <text x={4} y={pad - 6} fontSize="10" fill="var(--fg-3)">↑ actual</text>
      </svg>
    );
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <span>Diagnostics</span>
        <span className="panel-title-actions" style={{ fontSize: 11, color: 'var(--fg-3)' }}>
          {fi.length} features · {resid.length} recent residuals · {week.length} weeks of history
        </span>
      </div>
      <div style={{ padding: 14, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 18 }}>
        <div>
          <div className="label" style={{ marginBottom: 6 }}>
            Feature importance — what the trained model leans on
          </div>
          {fi.length === 0 ? (
            <div style={{ fontSize: 11, color: 'var(--fg-3)' }}>
              not available for this model type (linear / pipeline / stack)
            </div>
          ) : (
            <table className="tbl" style={{ width: '100%', fontSize: 11 }}>
              <tbody>
                {fi.slice(0, 15).map(f => (
                  <tr key={f.feature}>
                    <td style={{ width: '40%' }}>{f.feature}</td>
                    <td style={{ width: '50%', padding: '2px 6px' }}>
                      <div style={{ height: 8, background: 'var(--accent)',
                        width: `${(f.importance / maxFi) * 100}%`, opacity: 0.8 }} />
                    </td>
                    <td className="num-r mono" style={{ width: '10%' }}>{(f.importance * 100).toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div>
          <div className="label" style={{ marginBottom: 6 }}>
            Pearson correlation with actual high — raw signal strength
          </div>
          {corr.length === 0 ? (
            <div style={{ fontSize: 11, color: 'var(--fg-3)' }}>need ≥ 60 paired rows</div>
          ) : (
            <table className="tbl" style={{ width: '100%', fontSize: 11 }}>
              <tbody>
                {corr.map(c => {
                  const cls = c.r > 0 ? 'pos' : 'neg';
                  return (
                    <tr key={c.feature}>
                      <td style={{ width: '40%' }}>{c.feature}</td>
                      <td style={{ width: '40%', padding: '2px 6px' }}>
                        <div style={{
                          height: 8, background: c.r > 0 ? 'var(--pos)' : 'var(--neg)',
                          width: `${(Math.abs(c.r) / maxAbsCorr) * 100}%`, opacity: 0.8,
                        }} />
                      </td>
                      <td className={`num-r mono ${cls}`}>{c.r >= 0 ? '+' : ''}{c.r.toFixed(3)}</td>
                      <td className="num-r mono" style={{ color: 'var(--fg-3)' }}>n={c.n}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        <div>
          <div className="label" style={{ marginBottom: 6 }}>
            MAE by week — is the model getting better?
          </div>
          {week.length === 0 ? (
            <div style={{ fontSize: 11, color: 'var(--fg-3)' }}>not enough history yet</div>
          ) : (
            <svg width="320" height="120" style={{ display: 'block' }}>
              {week.map((w, i) => {
                const x = 8 + (i / Math.max(1, week.length - 1)) * 304;
                const h = (w.mae / maxBy) * 88;
                return <rect key={i} x={x - 5} y={108 - h} width="9" height={h}
                  fill="var(--accent)" opacity="0.8" />;
              })}
              <text x={8} y={118} fontSize="10" fill="var(--fg-3)">
                {week[0]?.week} … {week[week.length - 1]?.week}
              </text>
              <text x={280} y={10} fontSize="10" fill="var(--fg-3)">
                max {maxBy.toFixed(2)}°
              </text>
            </svg>
          )}
        </div>

        <div>
          <div className="label" style={{ marginBottom: 6 }}>
            Per-city ensemble accuracy
          </div>
          <table className="tbl" style={{ width: '100%', fontSize: 11 }}>
            <thead><tr><th>City</th><th className="num-r">N</th><th className="num-r">MAE</th><th className="num-r">Bias</th></tr></thead>
            <tbody>
              {city.map(c => (
                <tr key={c.city}>
                  <td className="city-cell">{c.city}</td>
                  <td className="num-r mono">{c.n}</td>
                  <td className="num-r mono">{c.mae.toFixed(2)}°</td>
                  <td className={`num-r mono ${c.bias > 0 ? 'pos' : c.bias < 0 ? 'neg' : ''}`}>
                    {c.bias >= 0 ? '+' : ''}{c.bias.toFixed(2)}°
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div style={{ gridColumn: '1 / -1' }}>
          <div className="label" style={{ marginBottom: 6 }}>
            Residual scatter — predicted vs actual (last {resid.length}). Green: |err|&lt;1°, yellow: &lt;3°, red: ≥3°.
          </div>
          {residualScatter()}
        </div>
      </div>
    </div>
  );
}


// ── Activity feed: live timeline of what the ML pipeline is doing.
// Polls /api/ml/events every 10s. Each event renders with a kind-icon,
// timestamp, message, and a chip strip pulled from the JSON payload so
// the user can see exactly what changed (n_train, test_mae, delta, etc).
function MlActivityPanel({ events }) {
  const data = events || { events: [], in_flight: { backfill: false, retrain: false } };
  const list = data.events || [];
  const flight = data.in_flight || {};

  const kindMeta = {
    backfill_start: { icon: '↘', cls: 'info' },
    backfill_done:  { icon: '✓', cls: 'pos' },
    retrain_start:  { icon: '▶', cls: 'info' },
    retrain_done:   { icon: '✓', cls: 'pos' },
    model_swap:     { icon: '↻', cls: 'pos' },
    drift_alert:    { icon: '⚠', cls: 'warn' },
    prune:          { icon: '✂', cls: 'muted' },
    data_quality:   { icon: '⚠', cls: 'warn' },
    dist_shift:     { icon: '⚠', cls: 'warn' },
    model_promoted: { icon: '★', cls: 'pos' },
    model_rolled_back: { icon: '↺', cls: 'neg' },
    online_update:  { icon: '·', cls: 'muted' },
  };

  const fmtTs = (ts) => {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };

  const renderChips = (payload) => {
    if (!payload || typeof payload !== 'object') return null;
    return Object.entries(payload).slice(0, 6).map(([k, v]) => {
      if (v == null || typeof v === 'object') return null;
      const sv = typeof v === 'number' ? (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(3)) : String(v);
      return <span key={k} className="mono" style={{
        fontSize: 10, padding: '1px 5px', background: 'var(--bg-2)',
        border: '1px solid var(--border)', borderRadius: 3, color: 'var(--fg-3)',
      }}>{k}={sv}</span>;
    });
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <span>Activity feed</span>
        <span className="panel-title-actions" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          {flight.backfill && <Pill kind="info" dot>BACKFILLING</Pill>}
          {flight.retrain && <Pill kind="pos" dot>TRAINING</Pill>}
          {!flight.backfill && !flight.retrain && (
            <span className="mono" style={{ fontSize: 11, color: 'var(--fg-3)' }}>idle</span>
          )}
          <span className="mono" style={{ fontSize: 11, color: 'var(--fg-3)' }}>{list.length} events</span>
        </span>
      </div>
      {list.length === 0 ? (
        <div style={{ padding: 14, fontSize: 12, color: 'var(--fg-3)' }}>
          No ML events yet. The first backfill or retrain run will populate this feed.
        </div>
      ) : (
        <div style={{ maxHeight: 320, overflowY: 'auto', padding: '4px 0' }}>
          {list.map(ev => {
            const meta = kindMeta[ev.kind] || { icon: '·', cls: 'muted' };
            return (
              <div key={ev.id} style={{
                display: 'grid', gridTemplateColumns: '70px 22px 1fr',
                gap: 10, padding: '6px 14px', fontSize: 12,
                borderBottom: '1px solid var(--border)', alignItems: 'baseline',
              }}>
                <span className="mono" style={{ fontSize: 11, color: 'var(--fg-3)' }}>{fmtTs(ev.ts)}</span>
                <span className={meta.cls} style={{ textAlign: 'center' }}>{meta.icon}</span>
                <div>
                  <div>{ev.message}</div>
                  <div style={{ display: 'flex', gap: 4, marginTop: 3, flexWrap: 'wrap' }}>
                    {renderChips(ev.payload)}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}


// ── Model-to-model diff: side-by-side compare of the two most recent
// persisted ML runs, showing test/train MAE delta and the per-feature
// importance shift. Tells the user "what changed when I retrained."
function MlModelDiffPanel({ modelDiff }) {
  const d = modelDiff;
  if (!d || d.available === false || !d.previous || !d.current) {
    return (
      <div className="panel">
        <div className="panel-header"><span>Model diff (previous → current)</span></div>
        <div style={{ padding: 14, fontSize: 12, color: 'var(--fg-3)' }}>
          Need at least two persisted models to compute a diff. Trigger another retrain to populate.
        </div>
      </div>
    );
  }
  const fmtTs = (ts) => ts ? new Date(ts * 1000).toLocaleString() : '—';
  const maeDelta = d.test_mae_delta;
  const maeCls = maeDelta == null ? '' : (maeDelta < 0 ? 'pos' : maeDelta > 0 ? 'neg' : '');
  const maePct = (d.previous.test_mae && maeDelta != null)
    ? (maeDelta / d.previous.test_mae * 100) : null;
  const importance = d.importance_delta || [];
  const maxAbs = Math.max(...importance.map(r => Math.abs(r.delta)), 0.005);

  return (
    <div className="panel">
      <div className="panel-header">
        <span>Model diff (previous → current)</span>
        <span className="panel-title-actions" style={{ fontSize: 11, color: 'var(--fg-3)' }}>
          test MAE {d.previous.test_mae?.toFixed(3) ?? '—'}° → {d.current.test_mae?.toFixed(3) ?? '—'}°
          {maeDelta != null && maePct != null && (
            <span className={maeCls} style={{ marginLeft: 6 }}>
              ({maeDelta >= 0 ? '+' : ''}{maeDelta.toFixed(3)}, {maePct >= 0 ? '+' : ''}{maePct.toFixed(1)}%)
            </span>
          )}
        </span>
      </div>
      <div style={{ padding: 14, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 18 }}>
        <div>
          <div className="label" style={{ marginBottom: 6 }}>Previous</div>
          <table className="tbl" style={{ width: '100%', fontSize: 11 }}>
            <tbody>
              <tr><td>Algorithm</td><td className="num-r mono">{d.previous.algorithm}</td></tr>
              <tr><td>Trained at</td><td className="num-r mono">{fmtTs(d.previous.trained_at)}</td></tr>
              <tr><td>Train MAE</td><td className="num-r mono">{d.previous.train_mae?.toFixed(3) ?? '—'}°</td></tr>
              <tr><td>Test MAE</td><td className="num-r mono">{d.previous.test_mae?.toFixed(3) ?? '—'}°</td></tr>
              <tr><td>n_train / n_test</td><td className="num-r mono">{d.previous.n_train} / {d.previous.n_test}</td></tr>
            </tbody>
          </table>
        </div>
        <div>
          <div className="label" style={{ marginBottom: 6 }}>Current</div>
          <table className="tbl" style={{ width: '100%', fontSize: 11 }}>
            <tbody>
              <tr><td>Algorithm</td><td className="num-r mono">{d.current.algorithm}</td></tr>
              <tr><td>Trained at</td><td className="num-r mono">{fmtTs(d.current.trained_at)}</td></tr>
              <tr><td>Train MAE</td><td className="num-r mono">{d.current.train_mae?.toFixed(3) ?? '—'}°</td></tr>
              <tr><td>Test MAE</td><td className="num-r mono">{d.current.test_mae?.toFixed(3) ?? '—'}°</td></tr>
              <tr><td>n_train / n_test</td><td className="num-r mono">{d.current.n_train} / {d.current.n_test}</td></tr>
            </tbody>
          </table>
        </div>
        {importance.length > 0 && (
          <div style={{ gridColumn: '1 / -1' }}>
            <div className="label" style={{ marginBottom: 6 }}>
              Feature importance shift (Δ ≥ 0.5pp shown)
            </div>
            <table className="tbl" style={{ width: '100%', fontSize: 11 }}>
              <thead>
                <tr><th>Feature</th><th className="num-r">Before</th><th></th><th className="num-r">After</th><th className="num-r">Δ</th></tr>
              </thead>
              <tbody>
                {importance.slice(0, 12).map(r => {
                  const cls = r.delta > 0 ? 'pos' : 'neg';
                  return (
                    <tr key={r.feature}>
                      <td>{r.feature}</td>
                      <td className="num-r mono">{(r.before * 100).toFixed(1)}%</td>
                      <td style={{ width: '40%', padding: '2px 6px' }}>
                        <div style={{
                          height: 8, background: r.delta > 0 ? 'var(--pos)' : 'var(--neg)',
                          width: `${(Math.abs(r.delta) / maxAbs) * 100}%`, opacity: 0.8,
                        }} />
                      </td>
                      <td className="num-r mono">{(r.after * 100).toFixed(1)}%</td>
                      <td className={`num-r mono ${cls}`}>{r.delta >= 0 ? '+' : ''}{(r.delta * 100).toFixed(1)}pp</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}


// Compact data-freshness strip — answers "is fresh data flowing?"
// at a glance. Shows total paired rows, latest target_date, hours since
// last successful backfill_done, and ETA to next scheduled retrain.
function MlDataFreshnessBadge({ mlInfo, events }) {
  const data = mlInfo?.data || {};
  const list = events?.events || [];
  const lastBackfillDone = list.find(e => e.kind === 'backfill_done' && !(e.payload || {}).error);
  const lastRetrainDone = list.find(e => e.kind === 'retrain_done' && !(e.payload || {}).error);
  const now = Date.now() / 1000;
  const fmtAgo = (ts) => {
    if (!ts) return 'never';
    const sec = Math.max(0, now - ts);
    if (sec < 60) return `${Math.round(sec)}s ago`;
    if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
    if (sec < 86400) return `${(sec / 3600).toFixed(1)}h ago`;
    return `${Math.round(sec / 86400)}d ago`;
  };
  const retrainInterval = mlInfo?.retrain_interval_seconds || 86400;
  const nextRetrain = lastRetrainDone
    ? Math.max(0, retrainInterval - (now - lastRetrainDone.ts))
    : null;
  const fmtIn = (sec) => {
    if (sec == null) return '—';
    if (sec < 60) return `<1m`;
    if (sec < 3600) return `${Math.round(sec / 60)}m`;
    return `${(sec / 3600).toFixed(1)}h`;
  };

  return (
    <div style={{
      padding: '6px 12px', background: 'var(--bg-2)',
      border: '1px solid var(--border)', borderRadius: 4,
      fontSize: 12, display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap',
    }}>
      <span style={{ color: 'var(--fg-2)' }}>Data:</span>
      <span className="mono pos">{(data.paired || 0).toLocaleString()} paired rows</span>
      <span style={{ color: 'var(--fg-3)' }}>·</span>
      <span className="mono" style={{ color: 'var(--fg-2)' }}>
        latest {data.latest_target_date || '—'}
      </span>
      <span style={{ color: 'var(--fg-3)' }}>·</span>
      <span className="mono" style={{ color: 'var(--fg-2)' }}>
        last backfill {fmtAgo(lastBackfillDone?.ts)}
      </span>
      <span style={{ color: 'var(--fg-3)' }}>·</span>
      <span className="mono" style={{ color: 'var(--fg-2)' }}>
        last retrain {fmtAgo(lastRetrainDone?.ts)}
      </span>
      <span style={{ flex: 1 }} />
      <span className="mono" style={{ color: 'var(--fg-3)' }}>
        next scheduled retrain in {fmtIn(nextRetrain)}
      </span>
    </div>
  );
}


function MlView({ mlInfo, accuracy, diagnostics, events, modelDiff, onMlBackfill, onMlTrain, onModelConfig }) {
  return (
    <div className="pnl-layout">
      <MlDataFreshnessBadge mlInfo={mlInfo} events={events} />

      <MlActivityPanel events={events} />

      <MlTrainingPanel mlInfo={mlInfo} onMlBackfill={onMlBackfill} onMlTrain={onMlTrain} />

      <ModelSourcePanel mlInfo={mlInfo} onModelConfig={onModelConfig} />

      <MlModelDiffPanel modelDiff={modelDiff} />

      <MlDiagnosticsPanel diagnostics={diagnostics} />

      <div className="panel">
        <div className="panel-header">
          <span>Model accuracy</span>
          <span className="panel-title-actions">
            ensemble vs ML · {accuracy?.n_predictions || 0} settled day{(accuracy?.n_predictions || 0) === 1 ? '' : 's'}
          </span>
        </div>
        {!accuracy || accuracy.n_predictions === 0 ? (
          <div style={{ padding: 14, fontSize: 12, color: 'var(--fg-3)' }}>
            no settled predictions yet — accuracy populates after the first
            bet settles tomorrow morning when the NWS Daily Climate Report posts.
          </div>
        ) : (
          <div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 14, padding: 14 }}>
              <AccuracyBlock title="Ensemble (naive weighted)" stats={accuracy.ensemble} />
              <AccuracyBlock title="ML (trained model)" stats={accuracy.ml} />
              <AccuracyBlock title="Blended (α·ML + (1−α)·Ens)" stats={accuracy.blended} />
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.4fr', gap: 14, padding: '0 14px 14px' }}>
              <div>
                <div className="label" style={{ marginBottom: 8 }}>By city — MAE (°F)</div>
                <table className="tbl">
                  <thead>
                    <tr><th>City</th><th className="num-r">N</th><th className="num-r">Ens</th><th className="num-r">ML</th><th className="num-r">Blend</th></tr>
                  </thead>
                  <tbody>
                    {accuracy.by_city.map(r => {
                      const best = (() => {
                        const xs = [r.ensemble_mae, r.ml_mae, r.blend_mae].filter(v => v != null);
                        return xs.length ? Math.min(...xs) : null;
                      })();
                      const cls = (v) => (v != null && v === best ? 'pos' : '');
                      return (
                        <tr key={r.city}>
                          <td className="city-cell">{r.city}</td>
                          <td className="num-r">{r.n}</td>
                          <td className={`num-r ${cls(r.ensemble_mae)}`}>{r.ensemble_mae != null ? r.ensemble_mae + '°' : '—'}</td>
                          <td className={`num-r ${cls(r.ml_mae)}`}>{r.ml_mae != null ? r.ml_mae + '°' : '—'}</td>
                          <td className={`num-r ${cls(r.blend_mae)}`}>{r.blend_mae != null ? r.blend_mae + '°' : '—'}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div>
                <div className="label" style={{ marginBottom: 8 }}>Recent predictions</div>
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Date</th><th>City</th>
                      <th className="num-r">Ens</th>
                      <th className="num-r">ML</th>
                      <th className="num-r">Blend</th>
                      <th className="num-r">Actual</th>
                      <th className="num-r">Ens Δ</th>
                      <th className="num-r">ML Δ</th>
                      <th className="num-r">Bl Δ</th>
                    </tr>
                  </thead>
                  <tbody>
                    {accuracy.recent.slice(0, 10).map((r, i) => {
                      const ec = (e) => Math.abs(e) <= 1 ? 'pos' : Math.abs(e) <= 2 ? 'info' : 'neg';
                      return (
                        <tr key={i}>
                          <td>{r.date}</td>
                          <td className="city-cell">{r.city}</td>
                          <td className="num-r">{r.ensemble}°</td>
                          <td className="num-r">{r.ml != null ? r.ml + '°' : '—'}</td>
                          <td className="num-r">{r.blended != null ? r.blended + '°' : '—'}</td>
                          <td className="num-r">{r.actual}°</td>
                          <td className={`num-r ${ec(r.ensemble_error)}`}>{fmtSign(r.ensemble_error, 1)}°</td>
                          <td className={`num-r ${r.ml_error != null ? ec(r.ml_error) : ''}`}>
                            {r.ml_error != null ? fmtSign(r.ml_error, 1) + '°' : '—'}
                          </td>
                          <td className={`num-r ${r.blend_error != null ? ec(r.blend_error) : ''}`}>
                            {r.blend_error != null ? fmtSign(r.blend_error, 1) + '°' : '—'}
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
    </div>
  );
}


// ── Auto-trader settings panel — every tunable with a plain-English
// explanation. Each row: label + numeric input + helper text describing
// what it does and a sensible range. Hitting "Apply" sends a single
// PATCH to /api/auto-trade/config; preset buttons swap the entire draft
// to opinionated defaults so users can pick a stance without thinking.
function AutoTradeSettings({ cfg, draft, setDraft, apply, busy }) {
  const v = (key, def) => draft[key] ?? cfg[key] ?? def;

  const fields = [
    {
      key: 'min_edge_cents', label: 'Minimum edge (¢)', def: 5, step: 1, min: 0, max: 50,
      help: 'Only fire when our model thinks the bracket is at least this many cents under-priced. Lower = more bets, lower conviction. Higher = fewer, stronger bets. Sweet spot 5-8¢.',
    },
    {
      key: 'min_model_pct', label: 'Minimum model probability', def: 0.12, step: 0.01, min: 0, max: 1,
      help: 'Refuse to take YES bets where our model thinks the outcome is below this probability. Filters out long-shot lottery tickets. 0.12 = 12%. Raise to 0.20+ to only bet plausible outcomes.',
    },
    {
      key: 'skip_entry_below_cents', label: 'Skip entries below (¢)', def: 5, step: 1, min: 0, max: 50,
      help: 'Hard skip on bets priced below this. Tiny entries (1-3¢) are pure variance — even when our edge is real, you lose 95%+ of the time and one big drawdown can blow up the account.',
    },
    {
      key: 'min_kelly', label: 'Minimum Kelly fraction', def: 0.005, step: 0.001, min: 0, max: 0.5,
      help: 'Refuse trades where Kelly says risk less than this fraction of bankroll. 0.005 = 0.5%. Filters out marginal edges. Raise to 0.01-0.02 for only meaningful bets.',
    },
    {
      key: 'max_bets_per_city_per_day', label: 'Max bets per city per day', def: 2, step: 1, min: 1, max: 10,
      help: 'Cap concurrent bets in one city for a settlement date. Stops the auto-trader from taking 5 overlapping brackets in the same city. 1 = highest-conviction only; 2-3 = balanced.',
    },
    {
      key: 'max_usd', label: 'Max stake per bet ($)', def: 500, step: 50, min: 10, max: 100000,
      help: 'Hard cap on a single bet size, regardless of what Kelly suggests. Default is 5% of bankroll. Lower = smoother equity curve, less ruin risk.',
    },
    {
      key: 'min_usd', label: 'Min stake per bet ($)', def: 50, step: 10, min: 1, max: 10000,
      help: 'Skip a tick if Kelly suggests a stake below this — too small to be worth the position. Default $50 keeps fees-and-friction reasonable.',
    },
    {
      key: 'interval_seconds', label: 'Tick interval (seconds)', def: 600, step: 30, min: 10, max: 86400,
      help: 'How often the auto-trader scans for bets. 600 = every 10 minutes. Lower = more reactive, more API load. The model+market only update slowly (hourly), so 5-15 minutes is plenty.',
    },
    {
      key: 'bankroll', label: 'Configured bankroll ($)', def: 10000, step: 100, min: 100, max: 10000000,
      help: 'Reference bankroll for Kelly sizing. The actual cash deployed scales with this — bigger bankroll = bigger bets at the same Kelly fraction.',
    },
  ];

  const setPreset = (preset) => {
    setDraft(d => ({ ...d, ...preset }));
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <span>Settings</span>
        <span className="panel-title-actions" style={{ display: 'flex', gap: 6 }}>
          <button className="btn-sm" onClick={() => setPreset({
            min_edge_cents: 3, min_model_pct: 0.05, skip_entry_below_cents: 1,
            min_kelly: 0.001, max_bets_per_city_per_day: 5,
          })}>Aggressive</button>
          <button className="btn-sm" onClick={() => setPreset({
            min_edge_cents: 5, min_model_pct: 0.12, skip_entry_below_cents: 5,
            min_kelly: 0.005, max_bets_per_city_per_day: 2,
          })}>Balanced</button>
          <button className="btn-sm" onClick={() => setPreset({
            min_edge_cents: 8, min_model_pct: 0.20, skip_entry_below_cents: 10,
            min_kelly: 0.01, max_bets_per_city_per_day: 1,
          })}>Conservative</button>
          <button className="btn primary sm" disabled={busy || Object.keys(draft).length === 0}
            onClick={() => apply(draft)}>
            Apply
          </button>
        </span>
      </div>
      <div style={{ padding: 14, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
        {fields.map(f => {
          const cur = v(f.key, f.def);
          const dirty = draft[f.key] !== undefined && draft[f.key] !== (cfg[f.key] ?? f.def);
          return (
            <div key={f.key} style={{ display: 'grid', gridTemplateColumns: '1fr 110px', gap: 8, alignItems: 'start' }}>
              <div>
                <div className="label" style={{ marginBottom: 2 }}>
                  {f.label}
                  {dirty && <span className="warn" style={{ marginLeft: 6, fontSize: 10 }}>● modified</span>}
                </div>
                <div style={{ fontSize: 11, color: 'var(--fg-3)', lineHeight: 1.4 }}>{f.help}</div>
              </div>
              <input
                type="number"
                value={cur}
                step={f.step}
                min={f.min}
                max={f.max}
                onChange={e => setDraft(d => ({ ...d, [f.key]: +e.target.value }))}
                style={{ fontSize: 13, textAlign: 'right' }}
              />
            </div>
          );
        })}
      </div>
      <div style={{ padding: '0 14px 14px', fontSize: 11, color: 'var(--fg-3)' }}>
        Changes take effect on the next auto-trader tick. Presets reset all relevant fields; "Apply" sends only the modified fields.
      </div>
    </div>
  );
}


function AutoTradeView({ info, bets, onSetConfig, onTriggerNow, positions = [], states = [] }) {
  // Build a lookup so each bet row can show its live "are we hitting?" status.
  // Key = city + bracket label; positions only includes open bets, so settled
  // rows just won't have a match and render "—".
  const liveByKey = React.useMemo(() => {
    const m = {};
    for (const p of positions) m[`${p.city}|${p.bracket}`] = p;
    return m;
  }, [positions]);
  const lookupLive = (b) => liveByKey[`${b.city}|${(b.bracket?.label || b.bracket)}`];
  const stateByCity = React.useMemo(() => {
    const m = {};
    for (const s of states || []) if (s?.city?.code) m[s.city.code] = s;
    return m;
  }, [states]);
  const [expandedBetId, setExpandedBetId] = useState_v(null);
  const [busy, setBusy] = useState_v(false);
  const [lastRunResult, setLastRunResult] = useState_v(null);
  const [draft, setDraft] = useState_v({});

  const [betSort, setBetSort] = useState_v('recent');
  const [betPage, setBetPage] = useState_v(0);
  const [betPageSize, setBetPageSize] = useState_v(20);

  const allBets = bets || [];
  const sortedBets = React.useMemo(() => {
    const arr = [...allBets];
    if (betSort === 'recent')    arr.sort((a, b) => (b.id || 0) - (a.id || 0));
    if (betSort === 'soonest')   arr.sort((a, b) => {
      const ta = a.closeAt ? new Date(a.closeAt).getTime() : Infinity;
      const tb = b.closeAt ? new Date(b.closeAt).getTime() : Infinity;
      return ta - tb;
    });
    if (betSort === 'biggest')   arr.sort((a, b) => {
      const win = p => p.size * (1 - (p.entry || 10) / 100) / ((p.entry || 10) / 100);
      return win(b) - win(a);
    });
    if (betSort === 'likely')    arr.sort((a, b) => (b.entry || 0) - (a.entry || 0));
    if (betSort === 'open')      arr.sort((a, b) => (a.status === 'open' ? -1 : 1) - (b.status === 'open' ? -1 : 1));
    if (betSort === 'locked')    arr.sort((a, b) => {
      const rank = x => {
        const live = liveByKey[`${x.city}|${(x.bracket?.label || x.bracket)}`];
        if (live?.liveStatus === 'locked_win')  return 0;
        if (live?.liveStatus === 'in_bracket')  return 1;
        if (live?.liveStatus === 'pending')     return 2;
        if (live?.liveStatus === 'locked_loss') return 3;
        return 4;
      };
      return rank(a) - rank(b);
    });
    return arr;
  }, [allBets, betSort, liveByKey]);

  const betTotalPages = Math.ceil(sortedBets.length / betPageSize);
  const recent = sortedBets.slice(betPage * betPageSize, (betPage + 1) * betPageSize);

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

      <AutoTradeSettings cfg={cfg} draft={draft} setDraft={setDraft} apply={apply} busy={busy} />

      <div className="panel">
        <div className="panel-header">
          <span>Bets</span>
          <span className="panel-title-actions" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>{allBets.length} total</span>
            <select
              value={betSort}
              onChange={e => { setBetSort(e.target.value); setBetPage(0); }}
              style={{ background: 'var(--bg-2)', color: 'var(--fg-1)', border: '1px solid var(--border)', borderRadius: 4, padding: '1px 4px', fontSize: 11 }}
            >
              <option value="recent">Most recent</option>
              <option value="soonest">First to hit</option>
              <option value="biggest">Biggest win</option>
              <option value="likely">Most likely</option>
              <option value="open">Open first</option>
              <option value="locked">Locked wins first</option>
            </select>
            <select
              value={betPageSize}
              onChange={e => { setBetPageSize(+e.target.value); setBetPage(0); }}
              style={{ background: 'var(--bg-2)', color: 'var(--fg-1)', border: '1px solid var(--border)', borderRadius: 4, padding: '1px 4px', fontSize: 11 }}
            >
              {[20, 40, 60, 80, 100].map(n => <option key={n} value={n}>{n} / page</option>)}
            </select>
          </span>
        </div>
        {allBets.length === 0 ? (
          <div style={{ padding: 14, fontSize: 12, color: 'var(--fg-3)' }}>
            no bets yet — turn auto-trade on, or hit "Trigger Now"
          </div>
        ) : (
          <>
            <table className="tbl">
              <thead>
                <tr>
                  <th></th>
                  <th>Time</th><th>City</th><th>Bracket</th><th>Live</th><th>Side</th>
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
                  const live = lookupLive(b);
                  const isOpen = expandedBetId === b.id;
                  const expandable = !!live;
                  return (
                    <React.Fragment key={b.id}>
                      <tr
                        style={{ cursor: expandable ? 'pointer' : 'default' }}
                        onClick={expandable ? () => setExpandedBetId(isOpen ? null : b.id) : undefined}
                      >
                        <td style={{ width: 18, textAlign: 'center', color: 'var(--fg-3)' }}>
                          {expandable ? (isOpen ? '▾' : '▸') : ''}
                        </td>
                        <td>{b.time}</td>
                        <td className="city-cell">{b.city}</td>
                        <td>{b.bracket?.label || b.bracket}</td>
                        <td>{settled ? <span className="mono" style={{ color: 'var(--fg-3)' }}>—</span>
                                     : (live ? <LiveStatusCell position={live} />
                                             : <span className="mono" style={{ color: 'var(--fg-3)' }}>—</span>)}</td>
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
                      {isOpen && live && <PositionDrilldown position={live} cityState={stateByCity[b.city]} colSpan={11} />}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
            {betTotalPages > 1 && (
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 10px', borderTop: '1px solid var(--border)', fontSize: 11, color: 'var(--fg-2)' }}>
                <span>Showing {betPage * betPageSize + 1}–{Math.min((betPage + 1) * betPageSize, sortedBets.length)} of {sortedBets.length}</span>
                <span style={{ display: 'flex', gap: 4 }}>
                  <button className="btn-sm" onClick={() => setBetPage(0)} disabled={betPage === 0}>«</button>
                  <button className="btn-sm" onClick={() => setBetPage(p => p - 1)} disabled={betPage === 0}>‹</button>
                  <span style={{ padding: '0 6px', lineHeight: '22px' }}>pg {betPage + 1} / {betTotalPages}</span>
                  <button className="btn-sm" onClick={() => setBetPage(p => p + 1)} disabled={betPage >= betTotalPages - 1}>›</button>
                  <button className="btn-sm" onClick={() => setBetPage(betTotalPages - 1)} disabled={betPage >= betTotalPages - 1}>»</button>
                </span>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ====== SETTINGS ======
//
// Centralizes runtime config that used to live scattered across the
// Auto-Trade and ML Model tabs:
//   - Bet sizing (NEW: confidence tiers + manual override)
//   - Active prediction model (mirrors the ML Model panel)
//   - Auto-trader guardrails (min edge, max stake, cadence, etc.)
//
// Each section persists via the same /api/auto-trade/config or
// /api/model/config endpoints those other tabs already use, so changes
// here propagate everywhere automatically.

function BetSizingPanel({ info, onSetConfig }) {
  const liveMode = info?.bet_sizing_mode || 'tiers';
  const liveTiers = info?.size_tiers || [];
  const liveMin = info?.min_usd ?? 50;
  const liveMax = info?.max_usd ?? 500;

  const [mode, setMode] = useState_v(null);
  const [tiers, setTiers] = useState_v(null);
  const [minUsd, setMinUsd] = useState_v(null);
  const [maxUsd, setMaxUsd] = useState_v(null);
  const [busy, setBusy] = useState_v(false);

  const curMode = mode ?? liveMode;
  const curTiers = tiers ?? liveTiers;
  const curMin = minUsd ?? liveMin;
  const curMax = maxUsd ?? liveMax;
  const dirty = (
    curMode !== liveMode ||
    JSON.stringify(curTiers) !== JSON.stringify(liveTiers) ||
    curMin !== liveMin ||
    curMax !== liveMax
  );

  const updateTier = (i, patch) => {
    setTiers((tiers ?? liveTiers).map((t, j) => j === i ? { ...t, ...patch } : t));
  };
  const addTier = () => {
    const next = [...(tiers ?? liveTiers), { min_edge_cents: 0, size_usd: 100 }];
    setTiers(next);
  };
  const removeTier = (i) => {
    setTiers((tiers ?? liveTiers).filter((_, j) => j !== i));
  };
  const reset = () => {
    setTiers([
      { min_edge_cents: 12, size_usd: 1000 },
      { min_edge_cents: 8,  size_usd: 750 },
      { min_edge_cents: 5,  size_usd: 500 },
      { min_edge_cents: 3,  size_usd: 250 },
    ]);
  };

  const apply = async () => {
    setBusy(true);
    try {
      // Sort tiers descending so the API stores them in the order the
      // backend evaluates them (highest-edge first wins).
      const sortedTiers = [...curTiers].sort((a, b) => +b.min_edge_cents - +a.min_edge_cents);
      const patch = {
        bet_sizing_mode: curMode,
        size_tiers: sortedTiers.map(t => ({
          min_edge_cents: +t.min_edge_cents,
          size_usd: +t.size_usd,
        })),
      };
      if (curMin !== liveMin) patch.min_usd = +curMin;
      if (curMax !== liveMax) patch.max_usd = +curMax;
      await onSetConfig(patch);
      setMode(null); setTiers(null); setMinUsd(null); setMaxUsd(null);
    } finally { setBusy(false); }
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <span>Bet sizing</span>
        <span className="panel-title-actions" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <span className="mono" style={{ fontSize: 11, color: 'var(--fg-3)' }}>
            mode: {liveMode}
          </span>
          <button className="btn primary sm" disabled={busy || !dirty} onClick={apply}>
            {busy ? 'applying…' : 'Apply'}
          </button>
        </span>
      </div>
      <div style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="label" style={{ minWidth: 90 }}>Sizing mode</span>
          <select
            value={curMode}
            onChange={e => setMode(e.target.value)}
            style={{
              background: 'var(--bg-2)', color: 'var(--fg-1)',
              border: '1px solid var(--border)', borderRadius: 4,
              padding: '4px 8px', fontSize: 13, minWidth: 180,
            }}
          >
            <option value="tiers">Confidence tiers (fixed $ per edge)</option>
            <option value="kelly">Kelly (fraction of bankroll)</option>
          </select>
        </label>

        {curMode === 'tiers' && (
          <div>
            <div className="label" style={{ marginBottom: 6 }}>
              Confidence tiers — first matching tier (highest min-edge ≤ candidate edge) wins
            </div>
            <table className="tbl" style={{ marginBottom: 8 }}>
              <thead>
                <tr>
                  <th>Min edge (¢)</th>
                  <th>Stake ($)</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {[...curTiers]
                  .map((t, i) => ({ ...t, _i: i }))
                  .sort((a, b) => +b.min_edge_cents - +a.min_edge_cents)
                  .map(t => (
                    <tr key={t._i}>
                      <td>
                        <input
                          type="number"
                          min={0} max={100} step={0.5}
                          value={t.min_edge_cents}
                          onChange={e => updateTier(t._i, { min_edge_cents: e.target.value })}
                          style={{ width: 80, background: 'var(--bg-2)', color: 'var(--fg-1)', border: '1px solid var(--border)', borderRadius: 4, padding: '2px 6px' }}
                        />
                      </td>
                      <td>
                        <input
                          type="number"
                          min={0} step={50}
                          value={t.size_usd}
                          onChange={e => updateTier(t._i, { size_usd: e.target.value })}
                          style={{ width: 100, background: 'var(--bg-2)', color: 'var(--fg-1)', border: '1px solid var(--border)', borderRadius: 4, padding: '2px 6px' }}
                        />
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <button className="btn-sm" onClick={() => removeTier(t._i)}>remove</button>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
            <div style={{ display: 'flex', gap: 6 }}>
              <button className="btn-sm" onClick={addTier}>+ add tier</button>
              <button className="btn-sm" onClick={reset}>reset defaults</button>
            </div>
            <div style={{ fontSize: 11, color: 'var(--fg-3)', marginTop: 8, lineHeight: 1.5 }}>
              Edges below every threshold fall through to Kelly sizing (still capped by max stake). Tiers are sorted highest-first on save.
            </div>
          </div>
        )}

        {curMode === 'kelly' && (
          <div style={{ fontSize: 11, color: 'var(--fg-3)', lineHeight: 1.5 }}>
            Stake = kelly_fraction × deployable cash, clamped to [min, max]. Smooth and theoretically optimal but the dollar amount changes with every tick.
          </div>
        )}

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="label">Min stake ($)</span>
            <input
              type="number"
              min={0} step={10}
              value={curMin}
              onChange={e => setMinUsd(+e.target.value)}
              style={{ background: 'var(--bg-2)', color: 'var(--fg-1)', border: '1px solid var(--border)', borderRadius: 4, padding: '4px 8px' }}
            />
            <span style={{ fontSize: 11, color: 'var(--fg-3)' }}>Below this, the bet is skipped entirely.</span>
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="label">Max stake ($)</span>
            <input
              type="number"
              min={0} step={50}
              value={curMax}
              onChange={e => setMaxUsd(+e.target.value)}
              style={{ background: 'var(--bg-2)', color: 'var(--fg-1)', border: '1px solid var(--border)', borderRadius: 4, padding: '4px 8px' }}
            />
            <span style={{ fontSize: 11, color: 'var(--fg-3)' }}>Hard cap regardless of tier or Kelly suggestion.</span>
          </label>
        </div>
      </div>
    </div>
  );
}


function AutoTradeQuickPanel({ info, onSetConfig }) {
  const enabled = !!info?.enabled;
  const [busy, setBusy] = useState_v(false);
  const toggle = async () => {
    setBusy(true);
    try { await onSetConfig({ enabled: !enabled }); }
    finally { setBusy(false); }
  };
  return (
    <div className="panel">
      <div className="panel-header">
        <span>Auto-trader</span>
        <span className="panel-title-actions">
          <button
            className={`btn primary sm`}
            disabled={busy}
            onClick={toggle}
            style={{ background: enabled ? 'var(--neg)' : undefined }}
          >
            {busy ? '…' : (enabled ? 'Disable' : 'Enable')}
          </button>
        </span>
      </div>
      <div style={{ padding: 14, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 10, fontSize: 12 }}>
        <div><span className="label">Status</span><div className={enabled ? 'pos' : 'neg'}>{enabled ? 'enabled' : 'disabled'}</div></div>
        <div><span className="label">Min edge</span><div className="mono">{info?.min_edge_cents ?? '—'}¢</div></div>
        <div><span className="label">Min model %</span><div className="mono">{info?.min_model_pct != null ? (info.min_model_pct * 100).toFixed(0) + '%' : '—'}</div></div>
        <div><span className="label">Min Kelly</span><div className="mono">{info?.min_kelly != null ? (info.min_kelly * 100).toFixed(2) + '%' : '—'}</div></div>
        <div><span className="label">Cadence</span><div className="mono">every {Math.round((info?.interval_seconds || 600) / 60)}m</div></div>
        <div><span className="label">Bets / city / day</span><div className="mono">{info?.max_bets_per_city_per_day ?? '—'}</div></div>
      </div>
      <div style={{ padding: '0 14px 14px', fontSize: 11, color: 'var(--fg-3)' }}>
        Per-trader detail (recent fills, run history) lives on the Auto-Trade tab. This card is for at-a-glance status + quick on/off.
      </div>
    </div>
  );
}


function SettingsView({ autoInfo, mlInfo, onSetConfig, onModelConfig, setTab }) {
  return (
    <div className="pnl-layout">
      <BetSizingPanel info={autoInfo} onSetConfig={onSetConfig} />
      <ModelSourcePanel mlInfo={mlInfo} onModelConfig={onModelConfig} />
      <AutoTradeQuickPanel info={autoInfo} onSetConfig={onSetConfig} />

      <div className="panel">
        <div className="panel-header"><span>Jump to</span></div>
        <div style={{ padding: 14, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button className="btn-sm" onClick={() => setTab && setTab('auto')}>Auto-Trade dashboard →</button>
          <button className="btn-sm" onClick={() => setTab && setTab('ml')}>ML Model dashboard →</button>
          <button className="btn-sm" onClick={() => setTab && setTab('db')}>SQL console →</button>
        </div>
      </div>
    </div>
  );
}


// ====== DB / SQL CONSOLE ======
// Read-only SQLite console for ad-hoc data inspection. Backend rejects
// anything that isn't a single SELECT/WITH and runs against a ?mode=ro
// connection, so even a craftily-encoded write attempt can't mutate.

const DB_PRESET_QUERIES = [
  {
    label: 'bets by status',
    sql: "SELECT status, COUNT(*) AS n, ROUND(SUM(size), 0) AS total_stake\nFROM bets\nGROUP BY status",
  },
  {
    label: 'recent settled bets',
    sql: "SELECT id, city, bracket_label, bracket_lo, bracket_hi, side, size,\n       entry_cents, settled_pl, settled_max_f, target_date,\n       datetime(settled_at, 'unixepoch') AS settled\nFROM bets\nWHERE status='settled'\nORDER BY settled_at DESC\nLIMIT 50",
  },
  {
    label: 'closed by city · win rate',
    sql: "SELECT city,\n       COUNT(*) AS n,\n       SUM(CASE WHEN settled_pl > 0 THEN 1 ELSE 0 END) AS wins,\n       SUM(CASE WHEN settled_pl <= 0 THEN 1 ELSE 0 END) AS losses,\n       ROUND(AVG(CASE WHEN settled_pl > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_pct,\n       ROUND(SUM(settled_pl), 2) AS net_pl\nFROM bets\nWHERE status='settled'\nGROUP BY city\nORDER BY net_pl DESC",
  },
  {
    label: 'open exposure by city',
    sql: "SELECT city, COUNT(*) AS n, SUM(size) AS exposure\nFROM bets\nWHERE status='open'\nGROUP BY city\nORDER BY exposure DESC",
  },
  {
    label: 'suspect ≤X wins (max_f below bracket_lo)',
    sql: "SELECT id, city, bracket_label, bracket_lo, bracket_hi,\n       side, settled_pl, settled_max_f, target_date\nFROM bets\nWHERE status='settled'\n  AND settled_pl > 0\n  AND (settled_max_f < bracket_lo OR settled_max_f > bracket_hi)\nORDER BY settled_at DESC",
  },
  {
    label: 'ml runs (recent)',
    sql: "SELECT id, ts, algorithm, role, n_train, n_test,\n       ROUND(test_mae, 3) AS test_mae,\n       ROUND(walk_forward_mae, 3) AS wf_mae\nFROM ml_runs\nORDER BY ts DESC\nLIMIT 30",
  },
];

function DbView() {
  const [query, setQuery] = useState_v(DB_PRESET_QUERIES[0].sql);
  const [result, setResult] = useState_v(null);
  const [busy, setBusy] = useState_v(false);
  const [schema, setSchema] = useState_v(null);
  const [showSchema, setShowSchema] = useState_v(true);

  React.useEffect(() => {
    let cancelled = false;
    MOCK.fetchSqlSchema().then(s => { if (!cancelled) setSchema(s); });
    return () => { cancelled = true; };
  }, []);

  const run = async () => {
    setBusy(true);
    try {
      const r = await MOCK.runSql(query);
      setResult(r);
    } finally { setBusy(false); }
  };

  const onKey = e => {
    // Ctrl/Cmd+Enter runs the query — much faster than reaching for the button.
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      run();
    }
  };

  return (
    <div className="pnl-layout">
      <div className="panel">
        <div className="panel-header">
          <span>SQL console <span style={{ color: 'var(--fg-3)', fontSize: 11, marginLeft: 8 }}>read-only · single SELECT · {schema?.row_cap || 1000} row cap</span></span>
          <span className="panel-title-actions" style={{ display: 'flex', gap: 6 }}>
            <button className="btn-sm" onClick={() => setShowSchema(s => !s)}>
              {showSchema ? 'hide schema' : 'show schema'}
            </button>
            <button className="btn primary sm" disabled={busy || !query.trim()} onClick={run}>
              {busy ? 'running…' : 'Run (Ctrl+Enter)'}
            </button>
          </span>
        </div>
        <div style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {DB_PRESET_QUERIES.map(p => (
              <button
                key={p.label}
                className="btn-sm"
                onClick={() => { setQuery(p.sql); }}
                title={p.sql}
              >
                {p.label}
              </button>
            ))}
          </div>
          <textarea
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={onKey}
            spellCheck={false}
            style={{
              minHeight: 140, fontFamily: 'ui-monospace, Menlo, monospace',
              fontSize: 12, lineHeight: 1.5, padding: 10,
              background: 'var(--bg-2)', color: 'var(--fg-1)',
              border: '1px solid var(--border)', borderRadius: 4,
              resize: 'vertical', whiteSpace: 'pre',
            }}
          />
          {result?.error && (
            <div className="neg" style={{ fontSize: 12, fontFamily: 'ui-monospace, Menlo, monospace' }}>
              error: {result.error}
            </div>
          )}
          {result && !result.error && (
            <div style={{ fontSize: 11, color: 'var(--fg-3)' }}>
              {result.row_count.toLocaleString()} rows in {result.query_ms}ms
              {result.truncated && <span className="neg"> · truncated at {result.row_cap}</span>}
            </div>
          )}
        </div>
        {result && !result.error && result.rows && (
          <div style={{ overflowX: 'auto', borderTop: '1px solid var(--border)' }}>
            <table className="tbl">
              <thead>
                <tr>
                  {result.columns.map(c => <th key={c}>{c}</th>)}
                </tr>
              </thead>
              <tbody>
                {result.rows.map((row, i) => (
                  <tr key={i}>
                    {row.map((v, j) => (
                      <td key={j} className="mono" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>
                        {v === null ? <span style={{ color: 'var(--fg-3)' }}>NULL</span>
                         : typeof v === 'number' ? v.toLocaleString()
                         : String(v)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {showSchema && schema && schema.tables && (
        <div className="panel">
          <div className="panel-header">
            <span>Schema</span>
            <span className="panel-title-actions">
              <span style={{ color: 'var(--fg-3)', fontSize: 11 }}>{schema.tables.length} tables</span>
            </span>
          </div>
          <div style={{ padding: 14, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 10 }}>
            {schema.tables.map(t => (
              <div key={t.name} style={{ border: '1px solid var(--border)', borderRadius: 4, padding: 10 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
                  <button
                    className="btn-sm"
                    onClick={() => setQuery(`SELECT * FROM ${t.name} LIMIT 50`)}
                    title="load a SELECT * for this table"
                    style={{ fontWeight: 600 }}
                  >
                    {t.name}
                  </button>
                  <span className="mono" style={{ fontSize: 11, color: 'var(--fg-3)' }}>
                    {t.row_count.toLocaleString()} rows
                  </span>
                </div>
                <div style={{ fontSize: 11, fontFamily: 'ui-monospace, Menlo, monospace', lineHeight: 1.6 }}>
                  {t.columns.map(c => (
                    <div key={c.name} style={{ display: 'flex', gap: 6, color: c.pk ? 'var(--accent)' : 'var(--fg-2)' }}>
                      <span style={{ minWidth: 18, color: 'var(--accent)' }}>{c.pk ? 'PK' : ''}</span>
                      <span style={{ flex: 1 }}>{c.name}</span>
                      <span style={{ color: 'var(--fg-3)' }}>{c.type || ''}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

Object.assign(window, { OpportunitiesView, DashboardView, TerminalView, SignalsView, PnLView, AutoTradeView, MlView, DbView, SettingsView });
