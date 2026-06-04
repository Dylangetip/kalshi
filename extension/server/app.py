"""FundingHub Cart Tracker — reference receiving server.

A minimal, self-contained FastAPI app that accepts capture payloads from the
Chrome extension, stores them in SQLite, and (for non-Amazon captures) scrapes
the submitted HTML for title / price / etc.

Run:
    pip install -r requirements.txt
    API_TOKEN=dev-secret uvicorn app:app --reload --port 8787

This is a *reference* implementation of the endpoint contract documented in
extension/README.md. Drop the equivalent route into your real platform
(a PHP snippet is included in the README) — the extension only cares about the
JSON shape and the Bearer token.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from scrape import scrape_product

API_TOKEN = os.environ.get("API_TOKEN", "")  # empty == auth disabled (dev only)
DB_PATH = Path(os.environ.get("CAPTURES_DB", Path(__file__).parent / "captures.db"))

app = FastAPI(title="FundingHub Cart Tracker — receiver")

# The extension posts with credentials:"omit", but allow CORS for any
# dashboards you build on top of the read API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS captures (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at   REAL NOT NULL,
                captured_at   TEXT,
                type          TEXT,
                site          TEXT,
                hostname      TEXT,
                url           TEXT,
                title         TEXT,
                client_id     TEXT,
                asin          TEXT,
                product_json  TEXT,   -- scraped/derived product fields
                items_json    TEXT,   -- cart line items, if any
                raw_json      TEXT    -- full original payload
            )
            """
        )


init_db()


# --------------------------------------------------------------------------- #
# models — intentionally permissive; the extension is the source of truth
# --------------------------------------------------------------------------- #
class Capture(BaseModel):
    type: str
    capturedAt: Optional[str] = None
    url: Optional[str] = None
    hostname: Optional[str] = None
    title: Optional[str] = None
    site: Optional[str] = None
    clientId: Optional[str] = None
    extensionVersion: Optional[str] = None
    amazon: Optional[dict] = None
    html: Optional[str] = None
    structured: Optional[dict] = None
    items: Optional[List[dict]] = None

    class Config:
        extra = "allow"  # tolerate future fields without breaking ingest


def _check_auth(authorization: Optional[str]) -> None:
    if not API_TOKEN:
        return  # auth disabled — dev mode
    expected = f"Bearer {API_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/captures")
def ingest(cap: Capture, authorization: Optional[str] = Header(default=None)) -> dict:
    _check_auth(authorization)

    asin: Optional[str] = None
    product: dict[str, Any] = {}

    if cap.site == "amazon":
        # Amazon → we trust the ASIN; no HTML scrape needed.
        asin = (cap.amazon or {}).get("asin")
        product = {k: (cap.amazon or {}).get(k) for k in ("asin", "title", "price")}
    elif cap.html:
        # Everything else → scrape the submitted HTML.
        product = scrape_product(cap.html, cap.structured)

    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO captures
               (received_at, captured_at, type, site, hostname, url, title,
                client_id, asin, product_json, items_json, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                time.time(), cap.capturedAt, cap.type, cap.site, cap.hostname,
                cap.url, cap.title, cap.clientId, asin,
                json.dumps(product), json.dumps(cap.items or []),
                cap.model_dump_json(),
            ),
        )
        new_id = cur.lastrowid

    return {"ok": True, "id": new_id, "asin": asin, "product": product,
            "item_count": len(cap.items or [])}


@app.get("/api/captures")
def list_captures(
    limit: int = Query(50, le=500),
    site: Optional[str] = None,
    authorization: Optional[str] = Header(default=None),
) -> dict:
    _check_auth(authorization)
    sql = "SELECT * FROM captures"
    params: list[Any] = []
    if site:
        sql += " WHERE site = ?"
        params.append(site)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d.pop("raw_json", None)  # keep the list response lean
        d["product"] = json.loads(d.pop("product_json") or "{}")
        d["items"] = json.loads(d.pop("items_json") or "[]")
        out.append(d)
    return {"count": len(out), "captures": out}
