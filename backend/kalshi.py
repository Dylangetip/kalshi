"""Kalshi REST client.

Auth: email + password → bearer token. Tokens are documented as 30-min
lifetime, refreshed automatically on 401 or after ~25 minutes.

Configuration (read from process env or backend/.env):
    KALSHI_API_BASE     defaults to demo (paper-trading) endpoint
    KALSHI_EMAIL        your account email
    KALSHI_PASSWORD     your account password

Production base:  https://trading-api.kalshi.com/trade-api/v2
Demo / paper:     https://demo-api.kalshi.co/trade-api/v2

Falls back silently to "not configured" when env vars are missing, so
the rest of the backend stays unaffected when you haven't set creds.
"""

import asyncio
import os
import time
from typing import Dict, List, Optional

import httpx

DEFAULT_BASE = "https://demo-api.kalshi.co/trade-api/v2"
TOKEN_LIFETIME_SECONDS = 1500  # refresh after 25 min — Kalshi tokens last ~30


class KalshiClient:
    def __init__(self) -> None:
        self.token: Optional[str] = None
        self.token_obtained_at: float = 0.0
        self.member_id: Optional[str] = None
        self._login_lock = asyncio.Lock()

    @property
    def base(self) -> str:
        return os.getenv("KALSHI_API_BASE", DEFAULT_BASE).rstrip("/")

    @property
    def email(self) -> Optional[str]:
        return os.getenv("KALSHI_EMAIL") or None

    @property
    def password(self) -> Optional[str]:
        return os.getenv("KALSHI_PASSWORD") or None

    def configured(self) -> bool:
        return bool(self.email and self.password)

    async def _login(self, client: httpx.AsyncClient) -> Optional[str]:
        """Exchange creds for a bearer token. Returns the token (also caches
        it on the instance) or None on failure."""
        if not self.configured():
            return None
        async with self._login_lock:
            r = await client.post(
                f"{self.base}/login",
                json={"email": self.email, "password": self.password},
                timeout=15,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            token = data.get("token")
            if not token:
                return None
            self.token = token
            self.member_id = data.get("member_id")
            self.token_obtained_at = time.time()
            return token

    async def _ensure_token(self, client: httpx.AsyncClient) -> Optional[str]:
        if self.token and (time.time() - self.token_obtained_at) < TOKEN_LIFETIME_SECONDS:
            return self.token
        return await self._login(client)

    async def fetch_event_markets(
        self,
        client: httpx.AsyncClient,
        event_ticker: str,
        limit: int = 100,
    ) -> Optional[List[Dict]]:
        """List all markets under an event (e.g. KXHIGHNY-25MAY03). Returns
        None on failure (incl. not-configured); empty list when no
        markets match."""
        token = await self._ensure_token(client)
        if not token:
            return None
        try:
            r = await client.get(
                f"{self.base}/markets",
                params={"event_ticker": event_ticker, "limit": limit},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            if r.status_code == 401:
                # Token expired mid-flight — re-auth and retry once.
                token = await self._login(client)
                if not token:
                    return None
                r = await client.get(
                    f"{self.base}/markets",
                    params={"event_ticker": event_ticker, "limit": limit},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=15,
                )
            r.raise_for_status()
            return r.json().get("markets") or []
        except Exception:
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
        "email": _client.email or None,
        "logged_in": _client.token is not None,
        "token_age_s": (
            int(time.time() - _client.token_obtained_at)
            if _client.token else None
        ),
        "member_id": _client.member_id,
    }


async def login_test(client: httpx.AsyncClient) -> Dict:
    """For /api/kalshi/test — attempts a fresh login and reports result.
    No secrets in the response."""
    if not _client.configured():
        return {
            "configured": False,
            "error": "KALSHI_EMAIL and KALSHI_PASSWORD not set in backend/.env",
        }
    token = await _client._login(client)
    return {
        "configured": True,
        "logged_in": token is not None,
        "base": _client.base,
        "member_id": _client.member_id,
        "error": None if token else "login failed (check creds and KALSHI_API_BASE)",
    }


async def fetch_markets(
    client: httpx.AsyncClient,
    event_ticker: str,
    limit: int = 100,
) -> Optional[List[Dict]]:
    return await _client.fetch_event_markets(client, event_ticker, limit=limit)
