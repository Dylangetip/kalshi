# FundingHub Cart Tracker

A Chrome (Manifest V3) extension that captures what a user adds to their cart on
**any** shopping site and forwards it to your FundingHub platform.

- **Amazon** → sends the **ASIN** (plus title/price when available).
- **Every other site** → sends the relevant **HTML** so the backend can scrape
  title, price, image, etc.

Two capture triggers (per the agreed scope):

1. **Add-to-cart clicks** — a heuristic click listener that recognises
   "Add to cart / bag / basket / Buy now" controls across arbitrary sites.
2. **Cart / checkout pages** — when the user lands on a `/cart`, `/checkout`,
   `/basket`, … page, the cart contents are scraped once per view.

```
extension/
├── manifest.json
├── icons/                 generated PNG icons
├── src/
│   ├── content.js         detection + scraping (runs on every page)
│   ├── background.js      config, network POST, retry queue, badge
│   ├── options.html/js    set endpoint URL + API key, send a test capture
│   └── popup.html/js      status, recent captures, pause/resume, flush
└── server/                reference receiving endpoint (FastAPI)
    ├── app.py             POST /api/captures, GET /api/captures
    ├── scrape.py          JSON-LD / OpenGraph / heuristic product scraper
    └── requirements.txt
```

## Install the extension (load unpacked)

1. Open `chrome://extensions`.
2. Toggle **Developer mode** (top right).
3. Click **Load unpacked** and select the `extension/` folder.
4. Click the extension icon → **Settings**, set your **Endpoint URL** (e.g.
   `https://fundinghubdev.braintree4me.com/api/captures`) and an **API key**,
   then **Save**. Use **Send test capture** to confirm connectivity.

The extension works offline: if the endpoint is unreachable, captures are
queued in local storage and retried automatically (and on demand via the popup
"Send now" button). The toolbar badge shows the pending count.

## Endpoint contract

The extension `POST`s JSON to your configured endpoint with these headers:

```
Content-Type: application/json
Authorization: Bearer <your api key>     (only if a key is set)
X-Client-Id: <anonymous per-install uuid>
```

> Requests are sent with `credentials: "omit"` — the visited site's cookies are
> never attached. Respond `2xx` to acknowledge; any other status makes the
> extension re-queue and retry.

### Payload shapes

**Amazon — add to cart**
```json
{
  "type": "add_to_cart",
  "site": "amazon",
  "capturedAt": "2026-06-04T12:00:00.000Z",
  "url": "https://www.amazon.com/dp/B0CXYZ1234",
  "hostname": "www.amazon.com",
  "title": "Acme Widget",
  "amazon": { "asin": "B0CXYZ1234", "title": "Acme Widget", "price": "$19.99" },
  "clientId": "…", "extensionVersion": "0.1.0"
}
```

**Non-Amazon — add to cart** (HTML sent for scraping; capped at ~400 KB)
```json
{
  "type": "add_to_cart",
  "site": "generic",
  "url": "https://shop.example.com/p/blue-shoe",
  "hostname": "shop.example.com",
  "title": "Blue Shoe",
  "html": "<div class='product'>…</div>",
  "structured": { "jsonld": [ … ], "og": { "og:title": "…", "product:price:amount": "49.95" } },
  "clientId": "…"
}
```

**Cart / checkout snapshot** (`type: "cart_snapshot"`)
- Amazon: `items: [{ "asin", "title", "price", "quantity" }]`
- Generic: `html` of the cart container + best-effort
  `items: [{ "name", "price", "url", "image" }]`

`structured` carries any schema.org `Product` JSON-LD and OpenGraph/`product:`
meta tags the page exposed, so your scraper often doesn't need to parse the raw
HTML at all.

## Reference server

A runnable implementation of the contract above — stores captures in SQLite and
scrapes generic HTML.

```bash
cd extension/server
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
API_TOKEN=dev-secret uvicorn app:app --reload --port 8787
```

- `POST /api/captures` — ingest one capture (Amazon → stores ASIN; generic →
  runs `scrape_product` on the HTML).
- `GET  /api/captures?limit=50&site=amazon` — list recent captures.
- `GET  /health`

Set the extension's endpoint to `http://localhost:8787/api/captures` and API key
to `dev-secret` to try it locally. Leave `API_TOKEN` empty to disable auth in
dev.

### Wiring into the real platform (PHP/Laravel sketch)

The extension only cares about the JSON shape + Bearer token, so on
`fundinghubdev` you'd add roughly:

```php
// routes/api.php
Route::post('/captures', [CaptureController::class, 'store']);

// CaptureController@store
public function store(Request $r) {
    if ($r->bearerToken() !== config('services.tracker.token')) abort(401);
    $p = $r->all();
    if (($p['site'] ?? '') === 'amazon') {
        $asin = $p['amazon']['asin'] ?? null;     // store the ASIN
    } else {
        $html = $p['html'] ?? '';                 // queue for scraping
    }
    Capture::create([
        'type'      => $p['type'] ?? null,
        'site'      => $p['site'] ?? null,
        'hostname'  => $p['hostname'] ?? null,
        'url'       => $p['url'] ?? null,
        'asin'      => $asin ?? null,
        'client_id' => $r->header('X-Client-Id'),
        'payload'   => json_encode($p),
    ]);
    return response()->json(['ok' => true]);
}
```

## Notes & limitations

- Add-to-cart detection is heuristic (text/aria/class/id keyword matching with a
  negative list to skip "view cart"/"remove"). Sites with unlabelled custom
  buttons may be missed; tune `ADD_KEYWORDS` / selectors in `content.js`.
- HTML payloads are capped at ~400 KB to keep requests reasonable.
- `clientId` is a random per-install UUID (anonymous), not tied to a user.
- Make sure users are informed and consent — this extension reports browsing/cart
  activity to your servers, which Chrome Web Store policy and privacy law require
  you to disclose.
