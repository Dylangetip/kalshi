"""FundingHub / AYL Vendor Capture — reference receiving server.

A minimal, self-contained FastAPI app implementing the endpoint contract the
extension expects:

    POST /api/draft-lines     create a draft line from a captured product
    GET  /api/preapproval     pre-approval lookup by url / asin
    GET  /api/drafts          list the parent's drafts
    GET  /api/draft-lines     (debug) list stored lines

For non-Amazon captures it scrapes the submitted HTML for any fields the
extension couldn't read client-side. This is a *reference* of the shape your
real platform needs — see extension/README.md for a Laravel sketch. The AI
grading / staff-review pieces (Workstream F) are represented only as stubs.

Run:
    pip install -r requirements.txt
    API_TOKEN=dev-secret uvicorn app:app --reload --port 8787
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

# Demo pre-approved catalog: substrings matched against url, plus ASINs.
PREAPPROVED_HOSTS = {"iew.com"}
PREAPPROVED_ASINS = {"B0PREAPPROVED"}

# Demo drafts returned to the extension's draft selector.
DEMO_DRAFTS = [
    {"id": "fall-2026", "name": "Fall 2026 supplies"},
    {"id": "art-class", "name": "Art class"},
]

app = FastAPI(title="FundingHub Vendor Capture — receiver")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
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
            CREATE TABLE IF NOT EXISTS draft_lines (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at   REAL NOT NULL,
                captured_at   TEXT,
                source        TEXT,    -- auto | manual | test
                site          TEXT,
                hostname      TEXT,
                url           TEXT,
                title         TEXT,
                quantity      INTEGER,
                draft_id      TEXT,
                draft_name    TEXT,
                client_id     TEXT,
                asin          TEXT,
                status        TEXT,    -- preapproved | review
                product_json  TEXT,
                raw_json      TEXT
            )
            """
        )


init_db()


# --------------------------------------------------------------------------- #
# models — permissive; the extension is the source of truth
# --------------------------------------------------------------------------- #
class DraftLine(BaseModel):
    type: str = "draft_line"
    source: Optional[str] = None
    capturedAt: Optional[str] = None
    url: Optional[str] = None
    hostname: Optional[str] = None
    title: Optional[str] = None
    site: Optional[str] = None
    quantity: int = 1
    draft: Optional[dict] = None
    clientId: Optional[str] = None
    extensionVersion: Optional[str] = None
    amazon: Optional[dict] = None
    product: Optional[dict] = None
    html: Optional[str] = None
    structured: Optional[dict] = None

    class Config:
        extra = "allow"


def _check_auth(authorization: Optional[str]) -> None:
    if not API_TOKEN:
        return
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def _preapproval_status(url: Optional[str], asin: Optional[str]) -> str:
    if asin and asin.upper() in PREAPPROVED_ASINS:
        return "preapproved"
    if url and any(h in url for h in PREAPPROVED_HOSTS):
        return "preapproved"
    return "review"


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/drafts")
def drafts(authorization: Optional[str] = Header(default=None)) -> dict:
    _check_auth(authorization)
    # Real platform: return the authenticated parent's open drafts.
    return {"drafts": DEMO_DRAFTS}


@app.get("/api/preapproval")
def preapproval(
    url: Optional[str] = None,
    asin: Optional[str] = None,
    site: Optional[str] = None,
    authorization: Optional[str] = Header(default=None),
) -> dict:
    _check_auth(authorization)
    status = _preapproval_status(url, asin)
    return {"preapproved": status == "preapproved", "status": status}


@app.post("/api/draft-lines")
def create_draft_line(
    line: DraftLine, authorization: Optional[str] = Header(default=None)
) -> dict:
    _check_auth(authorization)

    product = dict(line.product or {})
    asin: Optional[str] = product.get("asin") or (line.amazon or {}).get("asin")

    if line.site == "amazon":
        # Amazon → ASIN only; trust the client-extracted fields.
        product = {**(line.amazon or {}), **product, "asin": asin}
    elif line.html:
        # Fill any gaps by scraping the submitted HTML.
        scraped = scrape_product(line.html, line.structured)
        for k, v in scraped.items():
            if v and not product.get(k):
                product[k] = v

    status = _preapproval_status(line.url, asin)
    draft = line.draft or {}

    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO draft_lines
               (received_at, captured_at, source, site, hostname, url, title,
                quantity, draft_id, draft_name, client_id, asin, status,
                product_json, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                time.time(), line.capturedAt, line.source, line.site, line.hostname,
                line.url, product.get("title") or line.title, line.quantity,
                draft.get("id"), draft.get("name"), line.clientId, asin, status,
                json.dumps(product), line.model_dump_json(),
            ),
        )
        new_id = cur.lastrowid

    # Non-catalog items would be enqueued for AI grading + staff review here (F2/F3).
    return {
        "ok": True,
        "id": new_id,
        "status": status,
        "asin": asin,
        "product": product,
        "draft": draft,
        "quantity": line.quantity,
    }


@app.get("/api/draft-lines")
def list_draft_lines(
    limit: int = Query(50, le=500),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    _check_auth(authorization)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM draft_lines ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out: List[dict] = []
    for r in rows:
        d = dict(r)
        d.pop("raw_json", None)
        d["product"] = json.loads(d.pop("product_json") or "{}")
        out.append(d)
    return {"count": len(out), "lines": out}
