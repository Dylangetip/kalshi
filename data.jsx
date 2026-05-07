// ====== MOCK DATA LAYER ======
// Deterministic-ish PRNG so seeds are stable across reloads
const seedRand = (seed) => {
  let s = seed;
  return () => {
    s = (s * 9301 + 49297) % 233280;
    return s / 233280;
  };
};

const CITIES = [
  { code: 'NYC', station: 'KNYC', label: 'New York', office: 'OKX', tz: 'ET' },
  { code: 'CHI', station: 'KORD', label: 'Chicago', office: 'LOT', tz: 'CT' },
  { code: 'MIA', station: 'KMIA', label: 'Miami', office: 'MFL', tz: 'ET' },
  { code: 'AUS', station: 'KATT', label: 'Austin', office: 'EWX', tz: 'CT' },
  { code: 'LAX', station: 'KLAX', label: 'Los Angeles', office: 'LOX', tz: 'PT' },
];

// Build brackets centered around model max temp prediction
const makeBrackets = (modelMax, count = 9, step = 2) => {
  const center = Math.round(modelMax / step) * step;
  const out = [];
  for (let i = -Math.floor(count / 2); i <= Math.floor(count / 2); i++) {
    const lo = center + i * step - 1;
    const hi = lo + step - 1;
    out.push({ lo, hi, label: `${lo}–${hi}°F` });
  }
  return out;
};

// Normal-ish distribution helper
const gauss = (x, mean, sigma) => {
  const z = (x - mean) / sigma;
  return Math.exp(-0.5 * z * z) / (sigma * Math.sqrt(2 * Math.PI));
};

const buildCityState = (city, seed) => {
  const rng = seedRand(seed);
  const baseTemp = { NYC: 67, CHI: 62, MIA: 84, AUS: 79, LAX: 71 }[city.code];
  const modelMax = baseTemp + (rng() - 0.5) * 6;
  const nwsForecast = modelMax + (rng() - 0.5) * 3;
  const mosMax = modelMax + (rng() - 0.5) * 1.4;
  const namMos = mosMax + (rng() - 0.5) * 1.6;
  const brackets = makeBrackets(modelMax);

  // Model probabilities — narrower distribution
  const modelSigma = 1.6 + rng() * 0.6;
  const modelProbs = brackets.map(b => {
    const mid = (b.lo + b.hi) / 2;
    return gauss(mid, modelMax, modelSigma) * 2;
  });
  const mSum = modelProbs.reduce((a, b) => a + b, 0);
  const modelPct = modelProbs.map(p => p / mSum);

  // Kalshi prices — slightly biased + noisier (center overpricing)
  const kalshiSigma = modelSigma * 1.25;
  const kalshiBias = (rng() - 0.5) * 1.2;
  const kalshiProbs = brackets.map(b => {
    const mid = (b.lo + b.hi) / 2;
    return gauss(mid, modelMax + kalshiBias, kalshiSigma) * 2;
  });
  const kSum = kalshiProbs.reduce((a, b) => a + b, 0);
  const kalshiPct = kalshiProbs.map(p => p / kSum);

  // Edge per bracket — model% minus kalshi%
  const ladder = brackets.map((b, i) => ({
    ...b,
    modelPct: modelPct[i],
    kalshiPct: kalshiPct[i],
    edge: modelPct[i] - kalshiPct[i],
    yesPrice: Math.round(kalshiPct[i] * 100),
    volume: Math.round(rng() * 14000 + 800),
  }));

  // Recommended bracket = max edge
  const best = ladder.reduce((a, b) => (b.edge > a.edge ? b : a), ladder[0]);
  const evCents = Math.round(best.edge * 100);
  const kellyPct = Math.max(0, Math.min(0.1, best.edge / (best.kalshiPct * (1 - best.kalshiPct)) * 0.25));

  return {
    city,
    asof: Date.now() - Math.floor(rng() * 1000 * 60 * 5),
    settlementBracket: best,
    modelMax: +modelMax.toFixed(1),
    nwsForecast: +nwsForecast.toFixed(1),
    mosMax: +mosMax.toFixed(1),
    namMos: +namMos.toFixed(1),
    obsCurrent: +(modelMax - 4 - rng() * 4).toFixed(1),
    confidence: 0.45 + rng() * 0.5,
    afdConfScore: Math.round(2 + rng() * 3),
    brackets: ladder,
    bestEdgeCents: evCents,
    kellyPct: +kellyPct.toFixed(3),
    upperAir: {
      t850: +(8 + (rng() - 0.5) * 8).toFixed(1),
      t700: +(0 + (rng() - 0.5) * 6).toFixed(1),
      t500: +(-15 + (rng() - 0.5) * 6).toFixed(1),
      h500: Math.round(5760 + (rng() - 0.5) * 80),
      lapse: +(6 + rng() * 2).toFixed(1),
      windDir: Math.round(rng() * 360),
      windKt: Math.round(8 + rng() * 30),
      rh850: Math.round(30 + rng() * 60),
    },
    sounding: {
      t850Obs: +(8 + (rng() - 0.5) * 8).toFixed(1),
      t850Delta: +((rng() - 0.5) * 1.4).toFixed(2),
      lapse: +(6 + rng() * 2).toFixed(1),
      depression: +(2 + rng() * 6).toFixed(1),
    },
    afdText: makeAFD(city, rng, modelMax),
    flags: {
      seaBreeze: city.code === 'NYC' && rng() > 0.6,
      marineLayer: city.code === 'LAX' && rng() > 0.4,
      smoke: rng() > 0.85,
      overcast: rng() > 0.7,
      offshore: rng() > 0.7,
    },
  };
};

const makeAFD = (city, rng, modelMax) => {
  const high = rng() > 0.5;
  const phrases = [
    `.SYNOPSIS...`,
    `An upper level ridge centered over the central plains will continue to`,
    `dominate the forecast area through tomorrow afternoon.`,
    high
      ? `Models are in good agreement on tomorrow's temperatures with high confidence`
      : `Significant model spread persists for tomorrow's max temps; low confidence`,
    `in the magnitude of afternoon heating. The 12Z GFS suggests strong heating`,
    `with offshore flow and clear skies, while the ECMWF shows`,
    rng() > 0.5 ? `marine layer intrusion limiting the eastern half of the area.` : `sunny skies allowing for unimpeded heating.`,
    ``,
    `.SHORT TERM /TODAY THROUGH THURSDAY/...`,
    `Expect highs in the ${Math.round(modelMax - 2)}-${Math.round(modelMax + 3)} range.`,
    `850mb temperatures progged to reach +14C, supporting`,
    `unseasonably warm temperatures well above normal for the date.`,
    rng() > 0.5 ? `Sea breeze front may keep coastal sites cooler.` : ``,
    `Confidence is high in inland locations, lower along the coast.`,
  ];
  return phrases.filter(Boolean).join(' ');
};

const STATE_SEEDS = { NYC: 1, CHI: 7, MIA: 13, AUS: 19, LAX: 23 };

const initialState = () => CITIES.map(c => buildCityState(c, STATE_SEEDS[c.code] + Math.floor(Date.now() / 100000)));

// ====== SIGNAL CATALOG ======
// 34 signals across 17 categories per the doc
const SIGNAL_CATALOG = [
  // Surface model
  { id: 'gfs_max', cat: 'Surface Models', name: 'GFS surface max', impact: 'M', source: 'NOAA NOMADS' },
  { id: 'hrrr_max', cat: 'Surface Models', name: 'HRRR surface max', impact: 'M', source: 'Herbie' },
  { id: 'nam_max', cat: 'Surface Models', name: 'NAM surface max', impact: 'M', source: 'NOAA' },
  { id: 'ecmwf_max', cat: 'Surface Models', name: 'ECMWF surface max', impact: 'H', source: 'Open-Meteo' },
  // MOS (high impact — v3)
  { id: 'gfs_mos', cat: 'MOS (v3)', name: 'GFS-MOS max temp', impact: 'H', source: 'IEM Mesonet' },
  { id: 'nam_mos', cat: 'MOS (v3)', name: 'NAM-MOS max temp', impact: 'H', source: 'IEM Mesonet' },
  { id: 'mos_dev', cat: 'MOS (v3)', name: 'MOS vs NWS deviation', impact: 'H', source: 'derived' },
  { id: 'mos_spread', cat: 'MOS (v3)', name: 'MOS ensemble spread', impact: 'H', source: 'derived' },
  // Upper air (v3)
  { id: 't850', cat: 'Upper Air (v3)', name: '850mb temperature', impact: 'H', source: 'Open-Meteo' },
  { id: 't700', cat: 'Upper Air (v3)', name: '700mb temperature', impact: 'M', source: 'Open-Meteo' },
  { id: 'h500', cat: 'Upper Air (v3)', name: '500mb geopotential', impact: 'M', source: 'Open-Meteo' },
  { id: 'lapse', cat: 'Upper Air (v3)', name: '850mb lapse rate', impact: 'M', source: 'derived' },
  { id: 'rh850', cat: 'Upper Air (v3)', name: '850mb RH', impact: 'L', source: 'Open-Meteo' },
  // AFD (v3)
  { id: 'afd_conf', cat: 'AFD NLP (v3)', name: 'AFD confidence score', impact: 'H', source: 'NWS API' },
  { id: 'afd_supp', cat: 'AFD NLP (v3)', name: 'Suppressor flag', impact: 'M', source: 'NWS API' },
  { id: 'afd_enh', cat: 'AFD NLP (v3)', name: 'Enhancer flag', impact: 'M', source: 'NWS API' },
  { id: 'afd_above', cat: 'AFD NLP (v3)', name: 'Above-normal language', impact: 'M', source: 'NWS API' },
  // Soundings
  { id: 'snd_850', cat: 'Soundings (v3)', name: '12Z sounding 850mb', impact: 'H', source: 'U. Wyoming' },
  { id: 'snd_delta', cat: 'Soundings (v3)', name: 'Sounding vs model 850 delta', impact: 'H', source: 'derived' },
  { id: 'snd_lapse', cat: 'Soundings (v3)', name: 'Observed lapse rate', impact: 'M', source: 'U. Wyoming' },
  // Radar
  { id: 'nexrad', cat: 'Radar (v3)', name: 'NEXRAD composite', impact: 'M', source: 'NOAA' },
  { id: 'pbl', cat: 'PBL (v3)', name: 'PBL height', impact: 'M', source: 'HRRR/Herbie' },
  // Climatology
  { id: 'climo', cat: 'Climatology', name: 'Station 30yr climo', impact: 'L', source: 'NCEI' },
  { id: 'analog', cat: 'Climatology', name: 'Analog day matcher', impact: 'M', source: 'derived' },
  // Observations
  { id: 'asos', cat: 'Observations', name: 'ASOS hourly', impact: 'H', source: 'IEM' },
  { id: 'metar', cat: 'Observations', name: 'METAR current', impact: 'M', source: 'aviationweather' },
  // Market
  { id: 'kalshi_book', cat: 'Market (v3)', name: 'Kalshi orderbook', impact: 'H', source: 'Kalshi REST' },
  { id: 'kalshi_drift', cat: 'Market (v3)', name: 'Price drift since open', impact: 'M', source: 'derived' },
  { id: 'kalshi_vol', cat: 'Market (v3)', name: 'Volume profile', impact: 'M', source: 'Kalshi REST' },
  { id: 'kalshi_overprice', cat: 'Market (v3)', name: 'Center overpricing', impact: 'M', source: 'historical' },
  // Misc
  { id: 'satellite', cat: 'Satellite', name: 'GOES-16 IR', impact: 'L', source: 'NOAA' },
  { id: 'aqi', cat: 'Air Quality', name: 'PM2.5 / smoke', impact: 'L', source: 'AirNow' },
  { id: 'urban', cat: 'Urban', name: 'Urban heat anomaly', impact: 'L', source: 'derived' },
  { id: 'soilmoist', cat: 'Surface', name: 'Soil moisture', impact: 'L', source: 'NLDAS' },
];

// Generate signal status per city
const buildSignals = (city, seed) => {
  const rng = seedRand(seed * 31 + 17);
  return SIGNAL_CATALOG.map((s, i) => {
    const r = rng();
    const status = r > 0.92 ? 'stale' : r > 0.83 ? 'warn' : 'ok';
    const ageMin = Math.floor(r * 90);
    const value = (Math.round((rng() * 200 - 100) * 10) / 10);
    const direction = rng() > 0.5 ? 'up' : 'down';
    const delta = +((rng() - 0.5) * 2.4).toFixed(2);
    return { ...s, status, ageMin, value, direction, delta, contribution: +((rng() - 0.5) * 1.8).toFixed(2) };
  });
};

// ====== HISTORICAL P&L ======
const buildHistory = () => {
  const rng = seedRand(99);
  const days = 60;
  const out = [];
  let equity = 10000;
  let peak = 10000;
  for (let i = 0; i < days; i++) {
    const date = new Date(Date.now() - (days - i) * 86400000);
    const trades = Math.floor(rng() * 4 + 1);
    const dailyPL = (rng() - 0.42) * 320;
    equity += dailyPL;
    peak = Math.max(peak, equity);
    out.push({
      date: date.toISOString().slice(0, 10),
      trades,
      pl: +dailyPL.toFixed(2),
      equity: +equity.toFixed(2),
      drawdown: +(equity - peak).toFixed(2),
      winRate: +(0.48 + rng() * 0.16).toFixed(2),
    });
  }
  return out;
};

const buildOpenPositions = () => {
  const rng = seedRand(1234);
  return [
    { id: 'P1', city: 'NYC', bracket: '67–68°F', side: 'YES', size: 250, entry: 0.34, current: 0.41, pl: 17.5 },
    { id: 'P2', city: 'CHI', bracket: '60–61°F', side: 'YES', size: 180, entry: 0.28, current: 0.31, pl: 5.4 },
    { id: 'P3', city: 'AUS', bracket: '78–79°F', side: 'NO', size: 120, entry: 0.62, current: 0.55, pl: 8.4 },
    { id: 'P4', city: 'MIA', bracket: '85–86°F', side: 'YES', size: 300, entry: 0.39, current: 0.36, pl: -9.0 },
  ];
};

// ====== BACKEND BRIDGE ======
// Frontend tries to fetch real data from the FastAPI backend. Defaults to
// a relative URL ('') so it Just Works when the page is served by the
// same FastAPI process (which mounts the static files). Override via
// window.__BETS_API__ = 'http://other:port' if you split frontend and
// backend across different origins.
// On any fetch failure we silently keep using the synthetic state, so
// the prototype remains playable offline.
const API_BASE = window.__BETS_API__ != null ? window.__BETS_API__ : '';

async function fetchMlModelDiff() {
  try {
    const r = await fetch(API_BASE + '/api/ml/model-diff', { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

async function fetchMlEvents(limit = 100) {
  try {
    const r = await fetch(API_BASE + `/api/ml/events?limit=${limit}`, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

async function fetchMlDiagnostics() {
  try {
    const r = await fetch(API_BASE + '/api/ml/diagnostics', { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

async function fetchLiveState() {
  try {
    const r = await fetch(API_BASE + '/api/state', { cache: 'no-store' });
    if (!r.ok) return null;
    const data = await r.json();
    if (!Array.isArray(data) || data.length === 0) return null;
    return data;
  } catch {
    return null;
  }
}

async function fetchBets() {
  try {
    const r = await fetch(API_BASE + '/api/bets', { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function fetchPositions() {
  try {
    const r = await fetch(API_BASE + '/api/positions', { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function fetchClosedPositions() {
  try {
    const r = await fetch(API_BASE + '/api/closed-positions', { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function fetchSqlSchema() {
  try {
    const r = await fetch(API_BASE + '/api/admin/sql/schema', { cache: 'no-store' });
    if (!r.ok) return { error: `HTTP ${r.status}` };
    return await r.json();
  } catch (e) {
    return { error: String(e) };
  }
}

async function runSql(query) {
  try {
    const r = await fetch(API_BASE + '/api/admin/sql', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return { error: body.detail || `HTTP ${r.status}` };
    return body;
  } catch (e) {
    return { error: String(e) };
  }
}

async function fetchStats() {
  try {
    const r = await fetch(`${API_BASE}/api/stats`, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function fetchMlInfo() {
  try {
    const r = await fetch(`${API_BASE}/api/ml/info`, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function triggerMlBackfill(opts = {}) {
  // opts: { years, startDate, endDate }
  try {
    const params = new URLSearchParams();
    if (opts.startDate) params.set('start_date', opts.startDate);
    if (opts.endDate) params.set('end_date', opts.endDate);
    if (opts.years != null) params.set('years', String(opts.years));
    const r = await fetch(`${API_BASE}/api/ml/backfill?${params.toString()}`, { method: 'POST' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function triggerMlTrain(algorithm = 'linear') {
  try {
    const r = await fetch(`${API_BASE}/api/ml/train?algorithm=${algorithm}`, { method: 'POST' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function fetchAccuracy() {
  try {
    const r = await fetch(`${API_BASE}/api/accuracy`, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function fetchAutoTradeInfo() {
  try {
    const r = await fetch(`${API_BASE}/api/auto-trade/info`, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function setAutoTradeConfig(patch) {
  try {
    const r = await fetch(`${API_BASE}/api/auto-trade/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function triggerAutoTradeNow() {
  try {
    const r = await fetch(`${API_BASE}/api/auto-trade/now`, { method: 'POST' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function fetchEquity(hours = 72) {
  try {
    const r = await fetch(`${API_BASE}/api/equity?hours=${hours}`, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function fetchEdgeHistory(cityCode, hours = 4) {
  try {
    const r = await fetch(`${API_BASE}/api/snapshots/${cityCode}?hours=${hours}`, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function persistBet({ cityCode, bracket, side, size, entry }) {
  try {
    // entry from frontend is in cents and side-specific (YES price for YES bets,
    // NO price for NO bets). Backend stores YES price regardless of side.
    const entryYesCents = side === 'YES' ? entry : 100 - entry;
    const r = await fetch(API_BASE + '/api/bets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        city: cityCode,
        bracket_label: bracket.label,
        bracket_lo: bracket.lo,
        bracket_hi: bracket.hi,
        side,
        size,
        entry_cents: entryYesCents,
      }),
    });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

window.MOCK = {
  CITIES, SIGNAL_CATALOG,
  initialState, buildSignals, buildHistory, buildOpenPositions,
  seedRand, gauss, makeBrackets, buildCityState,
  fetchLiveState, fetchBets, fetchPositions, fetchClosedPositions, fetchEdgeHistory, fetchEquity, fetchStats, fetchAccuracy,
  fetchSqlSchema, runSql,
  fetchAutoTradeInfo, setAutoTradeConfig, triggerAutoTradeNow,
  fetchMlInfo, triggerMlBackfill, triggerMlTrain, fetchMlDiagnostics, fetchMlEvents, fetchMlModelDiff,
  persistBet, API_BASE,
};
