import math
from typing import List, Dict, Optional, Tuple


def gauss(x: float, mean: float, sigma: float) -> float:
    z = (x - mean) / sigma
    return math.exp(-0.5 * z * z) / (sigma * math.sqrt(2 * math.pi))


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
    """Realized P/L using the prototype's mark-to-market formula at the
    settlement boundary. `in_bracket` is whether the actual high fell in
    [lo, hi] — that's the YES outcome regardless of which side was bet.
    YES bets profit when in_bracket; NO bets profit when not — the sign
    flip handles both. Matches the (current - entry) × size × sign × 100
    formula used by /api/positions and the offline tick, so settlement
    is just the same formula evaluated at the terminal price (1 or 0)."""
    entry = entry_cents / 100.0
    sign = 1 if side == "YES" else -1
    yes_outcome = 1.0 if in_bracket else 0.0
    return (yes_outcome - entry) * size * sign * 100
