"""Best-effort product scraper for non-Amazon captures.

The extension sends a chunk of HTML for any non-Amazon site. We try, in order:
JSON-LD `Product` data, OpenGraph/`product:` meta tags, then visible-text
heuristics (price regex + likely title element). Everything is best-effort —
callers should treat missing fields as `None`.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from bs4 import BeautifulSoup

PRICE_RE = re.compile(r"(?:[$£€¥]|USD|EUR|GBP)\s?\d[\d.,]*", re.I)


def scrape_product(html: str, structured: Optional[dict] = None) -> dict:
    """Return {title, price, currency, image, brand, sku, source} from HTML."""
    result: dict[str, Any] = {
        "title": None, "price": None, "currency": None,
        "image": None, "brand": None, "sku": None, "source": None,
    }
    soup = BeautifulSoup(html or "", "html.parser")

    # 1) JSON-LD passed by the extension, or parsed from the HTML itself.
    products = list(structured.get("jsonld", []) if structured else [])
    if not products:
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(tag.string or "")
            except (ValueError, TypeError):
                continue
            for d in data if isinstance(data, list) else [data]:
                t = d.get("@type") if isinstance(d, dict) else None
                t = " ".join(t) if isinstance(t, list) else str(t or "")
                if "product" in t.lower():
                    products.append(d)

    for p in products:
        if not isinstance(p, dict):
            continue
        result["title"] = result["title"] or p.get("name")
        result["brand"] = result["brand"] or _brand(p.get("brand"))
        result["sku"] = result["sku"] or p.get("sku") or p.get("mpn")
        img = p.get("image")
        result["image"] = result["image"] or (img[0] if isinstance(img, list) and img else img if isinstance(img, str) else None)
        offers = p.get("offers")
        offer = (offers[0] if isinstance(offers, list) and offers else offers) or {}
        if isinstance(offer, dict):
            result["price"] = result["price"] or offer.get("price") or offer.get("lowPrice")
            result["currency"] = result["currency"] or offer.get("priceCurrency")
        if result["title"] and result["price"]:
            result["source"] = "json-ld"
            return result

    # 2) OpenGraph / product meta tags.
    og = (structured or {}).get("og", {}) if structured else {}
    og = {**_meta(soup), **og}  # extension-provided values win
    result["title"] = result["title"] or og.get("og:title")
    result["image"] = result["image"] or og.get("og:image")
    result["price"] = result["price"] or og.get("product:price:amount") or og.get("og:price:amount")
    result["currency"] = result["currency"] or og.get("product:price:currency") or og.get("og:price:currency")
    if result["title"] and result["price"]:
        result["source"] = "opengraph"
        return result

    # 3) Visible-text heuristics.
    if not result["title"]:
        h1 = soup.find(["h1"]) or soup.find(attrs={"id": re.compile("title|product", re.I)})
        if h1:
            result["title"] = h1.get_text(strip=True)[:300]
    if not result["price"]:
        m = PRICE_RE.search(soup.get_text(" ", strip=True))
        if m:
            result["price"] = m.group(0)
    result["source"] = "heuristic"
    return result


def _brand(b: Any) -> Optional[str]:
    if isinstance(b, dict):
        return b.get("name")
    return b if isinstance(b, str) else None


def _meta(soup: BeautifulSoup) -> dict:
    out: dict[str, str] = {}
    for m in soup.find_all("meta"):
        key = m.get("property") or m.get("name")
        val = m.get("content")
        if key and val and (key.startswith("og:") or key.startswith("product:")):
            out[key] = val
    return out
