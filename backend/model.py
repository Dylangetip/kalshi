import math
from typing import List, Dict, Optional, Tuple


def gauss(x: float, mean: float, sigma: float) -> float:
    z = (x - mean) / sigma
    return math.exp(-0.5 * z * z) / (sigma * math.sqrt(2 * math.pi))


def gauss_cdf(x: float, mean: float, sigma: float) -> float:
    """Cumulative distribution function: P(X ≤ x) for X ~ N(mean, sigma²)."""
    return 0.5 * (1 + math.erf((x - mean) / (sigma * math.sqrt(2))))


def bracket_prob(
    lo: float,
    hi: float,
    mean: float,
    sigma: float,
    lower_tail: bool = False,
    upper_tail: bool = False,
) -> float:
    """Probability the daily max falls in this Kalshi bracket.

    Kalshi events are mutually_exclusive=true. The B-brackets are
    integer intervals like {70, 71} → continuous [69.5, 71.5). The
    T-brackets are open tails — but defined to NOT overlap with the
    adjacent B-bracket, so T70 means "strictly below 70" (cap_strike
    is the threshold, not a member). For T70 with hi=70: integers
    [..., 69] = (-∞, 69.5) → CDF(hi - 0.5). Symmetric for upper tails."""
    if lower_tail:
        return gauss_cdf(hi - 0.5, mean, sigma)
    if upper_tail:
        return 1.0 - gauss_cdf(lo + 0.5, mean, sigma)
    return gauss_cdf(hi + 0.5, mean, sigma) - gauss_cdf(lo - 0.5, mean, sigma)


def make_brackets(model_max: float, count: int = 9, step: int = 2) -> List[Dict]:
    center = round(model_max / step) * step
    half = count // 2
    out = []
    for i in range(-half, half + 1):
        lo = center + i * step - 1
        hi = lo + step - 1
        out.append({"lo": int(lo), "hi": int(hi), "label": f"{int(lo)}–{int(hi)}°F"})
    return out


def ensemble_model_max(contributions: List[Tuple[Optional[float], float]]) -> Optional[float]:
    """Weighted average of (value, weight) pairs, dropping Nones."""
    valid = [(v, w) for v, w in contributions if v is not None]
    if not valid:
        return None
    total = sum(w for _, w in valid)
    return sum(v * w for v, w in valid) / total


def compute_ladder(
    model_max: float,
    model_sigma: float = 1.8,
    kalshi_bias: float = 0.0,
    kalshi_widening: float = 1.25,
) -> List[Dict]:
    """Bracket distribution: model gaussian + Kalshi-style widened/biased gaussian."""
    brackets = make_brackets(model_max)
    mids = [(b["lo"] + b["hi"]) / 2 for b in brackets]
    raw_m = [gauss(m, model_max, model_sigma) for m in mids]
    sm = sum(raw_m) or 1.0
    model_pct = [r / sm for r in raw_m]
    kalshi_sigma = model_sigma * kalshi_widening
    raw_k = [gauss(m, model_max + kalshi_bias, kalshi_sigma) for m in mids]
    sk = sum(raw_k) or 1.0
    kalshi_pct = [r / sk for r in raw_k]

    out = []
    for b, m, k in zip(brackets, model_pct, kalshi_pct):
        out.append({
            **b,
            "modelPct": round(m, 4),
            "kalshiPct": round(k, 4),
            "edge": round(m - k, 4),
            "yesPrice": int(round(k * 100)),
            "volume": 1200 + (abs(hash(b["label"])) % 13000),
        })
    return out


# AFD keyword bank — straight from the v3.0 doc, §2.3.
CONFIDENCE_HIGH = ["high confidence", "good confidence", "good agreement", "confidence is high"]
CONFIDENCE_LOW = ["low confidence", "uncertain", "models disagree", "model spread", "significant spread"]
SUPPRESSORS = ["marine layer", "sea breeze", "smoke", "haze", "overcast"]
ENHANCERS = ["sunny", "clear skies", "strong heating", "downsloping", "offshore flow", "unseasonably warm", "well above"]
TEMP_ABOVE = ["above normal", "well above", "unseasonably warm", "record high"]


def parse_afd(text: Optional[str]) -> Dict:
    if not text:
        return {
            "score": 3,
            "above_normal": False,
            "flags": {
                "seaBreeze": False, "marineLayer": False,
                "smoke": False, "overcast": False, "offshore": False,
            },
        }
    low = text.lower()
    score = 3
    score += sum(1 for kw in CONFIDENCE_HIGH if kw in low)
    score -= sum(1 for kw in CONFIDENCE_LOW if kw in low)
    score = max(1, min(5, score))
    return {
        "score": score,
        "above_normal": any(kw in low for kw in TEMP_ABOVE),
        "flags": {
            "seaBreeze": "sea breeze" in low,
            "marineLayer": "marine layer" in low,
            "smoke": "smoke" in low or "haze" in low,
            "overcast": "overcast" in low or "low clouds" in low,
            "offshore": "offshore" in low or "downslop" in low,
        },
    }


def best_bracket(ladder: List[Dict]) -> Dict:
    return max(ladder, key=lambda b: b["edge"])


def kelly_fraction(edge: float, kalshi_pct: float, fraction: float = 0.25, cap: float = 0.05) -> float:
    """Quarter-Kelly fraction of bankroll, capped at 5% per the system spec."""
    denom = max(0.01, kalshi_pct * (1 - kalshi_pct))
    raw = (edge / denom) * fraction
    return max(0.0, min(cap, raw))


import re as _re


def parse_climate_max_yesterday(text: Optional[str]) -> Optional[int]:
    """Extract YESTERDAY's MAX temperature (°F) from a CLI bulletin.

    CLI bodies always have a "TEMPERATURE (F)" block whose YESTERDAY column
    contains MAXIMUM <int>. We anchor on the YESTERDAY header and grab the
    next MAXIMUM line so we don't accidentally read TODAY's preliminary
    high from afternoon issuances."""
    if not text:
        return None
    m = _re.search(
        r"YESTERDAY[\s\S]{0,800}?^\s*MAXIMUM\s+(-?\d{1,3})\b",
        text,
        _re.MULTILINE,
    )
    if m:
        return int(m.group(1))
    # Fallback: first MAXIMUM in the body — accepted only if there's no
    # YESTERDAY anchor at all (some short CLIs).
    if "YESTERDAY" not in text:
        m2 = _re.search(r"^\s*MAXIMUM\s+(-?\d{1,3})\b", text, _re.MULTILINE)
        if m2:
            return int(m2.group(1))
    return None


def settle_pl(side: str, entry_cents: int, size: int, in_bracket: bool) -> float:
    """Realized P/L using Kalshi's actual binary-contract math.
    `size` is the USD stake. `in_bracket` is whether the actual high fell
    in [lo, hi] — that's the YES outcome regardless of which side was bet.

    On LOSS you lose only the stake. On WIN you get
        profit = contracts × (1 − entry_price)
    where contracts = size / entry_price. Matches _mark_bet_to_market's
    if_win / if_lose so the settlement number lines up with what the
    open-positions table preview promises.
    """
    won = (side == "YES" and in_bracket) or (side == "NO" and not in_bracket)
    if not won:
        return -float(size)
    entry = entry_cents / 100.0
    if side == "YES":
        contracts = size / entry if entry > 0 else 0.0
        return round(contracts * (1 - entry), 2)
    no_entry = 1 - entry  # what NO contracts cost
    if no_entry <= 0:
        return 0.0
    contracts = size / no_entry
    return round(contracts * (1 - no_entry), 2)
