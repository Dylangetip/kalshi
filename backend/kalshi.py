"""Kalshi REST client.

Auth: RSA-PSS signed requests using an API key generated from your
Kalshi account. (The legacy email/password /login endpoint was removed
in late 2024/2025 — API keys are the only supported auth now.)

Per Kalshi docs, every request needs three headers:
    KALSHI-ACCESS-KEY:       <key id from your account>
    KALSHI-ACCESS-TIMESTAMP: <unix epoch ms, as string>
    KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS-SHA256(timestamp + method + path))

Where path is the request path INCLUDING the /trade-api/v2 prefix and
EXCLUDING the query string.

Configuration (read from process env or backend/.env):
    KALSHI_API_BASE          defaults to api.elections.kalshi.com
    KALSHI_API_KEY_ID        the key ID string from the Kalshi UI
    KALSHI_API_KEY_PEM_PATH  path to the downloaded .pem private key
                             (or KALSHI_API_KEY_PEM with the literal
                             PEM contents inline)

Falls back to "not configured" silently when env vars are missing, so
the rest of the backend stays unaffected when you haven't set creds.
"""

import asyncio
import base64
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx

DEFAULT_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Bracket ranges in Kalshi market metadata can come in three shapes:
#   1. floor_strike / cap_strike numeric fields (cleanest, when present)
#   2. subtitle text like "67-68°" / "67 to 68°F" / "between 67 and 68"
#   3. open-ended brackets: "above 80°", "below 50°"
import re as _re
_RANGE_RE = _re.compile(r"(-?\d+)\s*(?:°F|°|F)?\s*(?:to|-|–|—|and)\s*(-?\d+)", _re.IGNORECASE)
_ABOVE_RE = _re.compile(r"(?:above|over|≥|>=|>)\s*(-?\d+)", _re.IGNORECASE)
_BELOW_RE = _re.compile(r"(?:below|under|≤|<=|<)\s*(-?\d+)", _re.IGNORECASE)


def parse_bracket_range(market: Dict) -> Optional[tuple]:
    """Extract (lo, hi) integer Fahrenheit bounds from a Kalshi market.

    Three shapes Kalshi actually uses for daily-high temp brackets:
      - "B<x>.5" two-sided bracket: floor_strike + cap_strike both present
      - "T<x>" lower-tail (≤x°): only cap_strike present
      - "T<x>" upper-tail (≥x°): only floor_strike present
    Open-ended brackets get widened by 20° so they slot into the ladder."""
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")
    try:
        floor_i = int(round(float(floor))) if floor is not None else None
        cap_i = int(round(float(cap))) if cap is not None else None
    except (TypeError, ValueError):
        floor_i = cap_i = None

    if floor_i is not None and cap_i is not None:
        return (floor_i, cap_i)
    if cap_i is not None and floor_i is None:
        # Lower-tail "T<n>" → "≤ n°"
        return (cap_i - 20, cap_i)
    if floor_i is not None and cap_i is None:
        # Upper-tail → "≥ n°"
        return (floor_i, floor_i + 20)

    # Subtitle fallback for any market that doesn't expose strikes.
    for field in ("yes_sub_title", "sub_title", "subtitle", "rules_primary"):
        text = market.get(field)
        if not text:
            continue
        m = _RANGE_RE.search(text)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            return (min(lo, hi), max(lo, hi))
        m = _ABOVE_RE.search(text)
        if m:
            return (int(m.group(1)), int(m.group(1)) + 20)
        m = _BELOW_RE.search(text)
        if m:
            return (int(m.group(1)) - 20, int(m.group(1)))
    return None


def _market_yes_cents(m: Dict) -> Optional[int]:
    """Best-available YES price in cents, in priority order:
    last fill → midpoint of bid/ask → ask → bid. Kalshi exposes prices
    as decimal dollars in `*_dollars` fields (e.g. 0.04 = 4¢)."""
    last = m.get("last_price_dollars")
    if last and float(last) > 0:
        return int(round(float(last) * 100))
    bid = m.get("yes_bid_dollars")
    ask = m.get("yes_ask_dollars")
    bid_f = float(bid) if bid not in (None, "") else None
    ask_f = float(ask) if ask not in (None, "") else None
    if bid_f and ask_f:
        return int(round((bid_f + ask_f) / 2 * 100))
    if ask_f:
        return int(round(ask_f * 100))
    if bid_f:
        return int(round(bid_f * 100))
    return None


def _load_private_key():
    """Read the RSA private key from PEM_PATH or PEM env. Returns the
    cryptography key object or None if not configured."""
    pem_inline = os.getenv("KALSHI_API_KEY_PEM")
    pem_path = os.getenv("KALSHI_API_KEY_PEM_PATH")
    pem_bytes: Optional[bytes] = None
    if pem_inline:
        pem_bytes = pem_inline.encode("utf-8")
    elif pem_path:
        p = Path(pem_path).expanduser()
        if p.exists():
            pem_bytes = p.read_bytes()
    if not pem_bytes:
        return None
    try:
        from cryptography.hazmat.primitives import serialization
        return serialization.load_pem_private_key(pem_bytes, password=None)
    except Exception:
        return None


def _sign(private_key, timestamp_ms: int, method: str, path: str) -> str:
    """Produce the base64 RSA-PSS-SHA256 signature for one request."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    msg = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("ascii")


class KalshiClient:
    def __init__(self) -> None:
        self.last_error: Optional[str] = None
        self._key_cache = None
        self._key_pem_signature: Optional[str] = None  # detect env changes

    @property
    def base(self) -> str:
        return os.getenv("KALSHI_API_BASE", DEFAULT_BASE).rstrip("/")

    @property
    def base_path(self) -> str:
        """Path prefix used in the signature (e.g. /trade-api/v2)."""
        # Take the path component of base, ensure it starts with "/".
        from urllib.parse import urlparse
        p = urlparse(self.base).path or ""
        return p if p.startswith("/") else "/" + p

    @property
    def key_id(self) -> Optional[str]:
        return os.getenv("KALSHI_API_KEY_ID") or None

    def _private_key(self):
        # Hot-reload the key on env changes (so editing .env doesn't require
        # bouncing the worker).
        sig = (
            os.getenv("KALSHI_API_KEY_PEM_PATH", "")
            + "|"
            + os.getenv("KALSHI_API_KEY_PEM", "")
        )
        if sig != self._key_pem_signature:
            self._key_cache = _load_private_key()
            self._key_pem_signature = sig
        return self._key_cache

    def configured(self) -> bool:
        return bool(self.key_id and self._private_key())

    def _signed_headers(self, method: str, path: str) -> Optional[Dict[str, str]]:
        key = self._private_key()
        if not key or not self.key_id:
            return None
        ts_ms = int(time.time() * 1000)
        try:
            sig = _sign(key, ts_ms, method, path)
        except Exception as exc:
            self.last_error = f"sign error: {type(exc).__name__}: {exc}"
            return None
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Accept": "application/json",
        }

    async def _signed_get(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: Optional[Dict] = None,
    ) -> Optional[httpx.Response]:
        """GET <base><path> with signed headers. `path` is appended verbatim
        to base_path for signing — pass e.g. '/markets', not the full URL."""
        if not self.configured():
            self.last_error = (
                "API key not configured (set KALSHI_API_KEY_ID and "
                "KALSHI_API_KEY_PEM_PATH in backend/.env)"
            )
            return None
        full_path = self.base_path + path
        headers = self._signed_headers("GET", full_path)
        if headers is None:
            return None
        try:
            r = await client.get(
                f"{self.base}{path}",
                params=params,
                headers=headers,
                timeout=15,
            )
            return r
        except Exception as exc:
            self.last_error = f"network: {type(exc).__name__}: {exc}"
            return None

    async def fetch_event_markets(
        self,
        client: httpx.AsyncClient,
        event_ticker: str,
        limit: int = 100,
    ) -> Optional[List[Dict]]:
        """List all markets under an event (e.g. KXHIGHNY-25MAY03)."""
        r = await self._signed_get(
            client, "/markets", params={"event_ticker": event_ticker, "limit": limit}
        )
        if r is None:
            return None
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            return None
        try:
            return r.json().get("markets") or []
        except Exception as exc:
            self.last_error = f"non-JSON: {exc}"
            return None

    async def fetch_events(
        self,
        client: httpx.AsyncClient,
        series_ticker: Optional[str] = None,
        status: str = "open",
        limit: int = 50,
    ) -> Optional[List[Dict]]:
        """List events. Filter by series_ticker (e.g. KXHIGHNY) or by
        status (open / closed / settled)."""
        params: Dict = {"limit": limit, "status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        r = await self._signed_get(client, "/events", params=params)
        if r is None:
            return None
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            return None
        try:
            return r.json().get("events") or []
        except Exception as exc:
            self.last_error = f"non-JSON: {exc}"
            return None

    async def fetch_active_event(
        self,
        client: httpx.AsyncClient,
        series_ticker: str,
    ) -> Optional[Dict]:
        """Most recently opened active event under this series. For
        daily-frequency weather series this is today's market."""
        events = await self.fetch_events(
            client, series_ticker=series_ticker, status="open", limit=5
        )
        if not events:
            return None
        # Kalshi returns events newest first; the daily-high event is
        # whichever is currently open and unsettled.
        return events[0]

    async def fetch_brackets_for_city(
        self,
        client: httpx.AsyncClient,
        series_ticker: str,
    ) -> Optional[List[Dict]]:
        """One-shot helper: find today's active event for this series and
        return its bracket markets with parsed (lo, hi) ranges and current
        YES prices in cents."""
        event = await self.fetch_active_event(client, series_ticker)
        if not event:
            return None
        markets = await self.fetch_event_markets(client, event["event_ticker"])
        if not markets:
            return None
        out: List[Dict] = []
        for m in markets:
            rng = parse_bracket_range(m)
            if rng is None:
                continue
            lo, hi = rng
            yes_cents = _market_yes_cents(m)
            if yes_cents is None:
                continue
            # Tag the synthetic-edge brackets so the ladder builder integrates
            # them as proper tails (-inf or +inf) instead of fixed 20°-wide.
            has_floor = m.get("floor_strike") is not None
            has_cap = m.get("cap_strike") is not None
            out.append({
                "ticker": m.get("ticker"),
                "lo": lo,
                "hi": hi,
                "lower_tail": (has_cap and not has_floor),
                "upper_tail": (has_floor and not has_cap),
                "yes_cents": yes_cents,
                "yes_bid_cents": int(round(float(m["yes_bid_dollars"]) * 100))
                    if m.get("yes_bid_dollars") else None,
                "yes_ask_cents": int(round(float(m["yes_ask_dollars"]) * 100))
                    if m.get("yes_ask_dollars") else None,
                "volume": int(round(float(m.get("volume_fp") or 0))),
                "open_interest": int(round(float(m.get("open_interest_fp") or 0))),
            })
        out.sort(key=lambda b: b["lo"])
        return out or None

    async def fetch_series(
        self,
        client: httpx.AsyncClient,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> Optional[List[Dict]]:
        """List series (a series groups recurring events — e.g. all daily
        NYC high-temp events sit under one series). Filter by category to
        narrow to weather."""
        params: Dict = {"limit": limit}
        if category:
            params["category"] = category
        r = await self._signed_get(client, "/series", params=params)
        if r is None:
            return None
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            return None
        try:
            return r.json().get("series") or []
        except Exception as exc:
            self.last_error = f"non-JSON: {exc}"
            return None

    async def fetch_exchange_status(self, client: httpx.AsyncClient) -> Optional[Dict]:
        """Trivial signed call used by /api/kalshi/test to verify auth.
        /exchange/status is a small public endpoint that still requires
        valid signature headers, so a 200 confirms key + signing work."""
        r = await self._signed_get(client, "/exchange/status")
        if r is None:
            return None
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            return None
        try:
            return r.json()
        except Exception as exc:
            self.last_error = f"non-JSON: {exc}"
            return None


# Process-wide singleton — auth state is shared across requests.
_client = KalshiClient()


def configured() -> bool:
    return _client.configured()


def info() -> Dict:
    """Non-secret view of current auth state."""
    return {
        "configured": _client.configured(),
        "base": _client.base,
        "base_path": _client.base_path,
        "key_id_set": bool(_client.key_id),
        "key_id_preview": (_client.key_id[:8] + "…") if _client.key_id else None,
        "private_key_loaded": _client._private_key() is not None,
    }


async def login_test(client: httpx.AsyncClient) -> Dict:
    """Round-trip a signed GET /exchange/status to verify auth.
    A 200 means the key + signing both work."""
    if not _client.configured():
        return {
            "configured": False,
            "error": (
                "Set KALSHI_API_KEY_ID + KALSHI_API_KEY_PEM_PATH in "
                "backend/.env (generate a key in your Kalshi account → "
                "Settings → API)"
            ),
        }
    status = await _client.fetch_exchange_status(client)
    return {
        "configured": True,
        "logged_in": status is not None,
        "base": _client.base,
        "key_id": (_client.key_id[:8] + "…") if _client.key_id else None,
        "exchange_status": status,
        "error": None if status is not None else _client.last_error,
    }


async def fetch_markets(
    client: httpx.AsyncClient,
    event_ticker: str,
    limit: int = 100,
) -> Optional[List[Dict]]:
    return await _client.fetch_event_markets(client, event_ticker, limit=limit)


async def fetch_brackets_for_city(
    client: httpx.AsyncClient,
    series_ticker: str,
) -> Optional[List[Dict]]:
    """Module-level wrapper around the singleton client. Returns today's
    bracket markets with parsed (lo, hi) and yes_cents."""
    return await _client.fetch_brackets_for_city(client, series_ticker)
