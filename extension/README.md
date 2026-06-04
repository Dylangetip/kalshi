# FundingHub Vendor Capture (AYL)

A browser extension that lets a parent add vendor products to their FundingHub
**draft** directly from any vendor's product page — no copy-pasting URLs or
retyping titles/prices. Aligns with the *Vendor Capture Browser Extension* scope
addendum (AYL — Automate Your Life).

- **Amazon** → captures the **URL / ASIN only** (no scraping).
- **Every other vendor** → extracts **title, price, currency, image,
  description, variant** (via JSON-LD / OpenGraph / heuristics), and also sends
  the raw structured data + page HTML so the backend can verify.

## Start / Pause — parent-initiated capture

Capture is **off until the parent presses Start**. The popup has the master
**Start / Pause** control:

- **Start** → a session begins. On vendor product pages the extension now:
  1. **auto-captures** the product into the parent's current draft (once per
     product per session), and
  2. shows a floating **"Add to Draft"** button for a deliberate add with a
     **quantity** stepper and a **pre-approval badge**.
- **Pause** → nothing is captured; the floating button disappears.

The toolbar badge shows `ON` while a session is active (or the pending-sync
count). The on-page button can be paused or dismissed per-site, and reflects the
currently selected draft.

> Cart-level capture (scraping a whole vendor checkout cart) is **out of scope**
> per §5 of the addendum and is intentionally not implemented. Capture is
> per-product, parent-initiated.

```
extension/
├── manifest.json          MV3
├── icons/
├── src/
│   ├── content.js         product detection, extraction, floating "Add to Draft" UI, auto-capture
│   ├── background.js      session state, draft submission, pre-approval, retry queue, badge
│   ├── popup.html/js      Start/Pause, draft selector, recently-added, flush
│   └── options.html/js    platform base URL + parent token, test submission
└── server/                reference receiver (FastAPI)
    ├── app.py             /api/draft-lines, /api/preapproval, /api/drafts
    ├── scrape.py          JSON-LD / OpenGraph / heuristic product scraper
    └── requirements.txt
```

## Install (load unpacked)

1. Open `chrome://extensions`, enable **Developer mode**, click **Load unpacked**,
   select the `extension/` folder.
2. Extension icon → **Settings**: set the **Platform base URL** (e.g.
   `https://fundinghubdev.braintree4me.com`) and a **parent token**, **Save**,
   then **Send test draft line** to confirm connectivity.
3. Open the popup, pick a **Draft**, press **▶ Start capturing**, and browse a
   vendor product page.

Works offline: submissions that can't be delivered are queued locally and retried
automatically (and on demand via the popup's **Send pending** button).

## Endpoint contract

Base URL is configured in options. The extension calls:

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/api/draft-lines` | create a draft line from a captured product |
| `GET`  | `/api/preapproval?url=&asin=&site=` | pre-approval lookup → `{ "preapproved": bool, "status": "preapproved"\|"review" }` |
| `GET`  | `/api/drafts` | the parent's drafts → `{ "drafts": [{ "id", "name" }] }` |

Headers on every request:

```
Authorization: Bearer <parent token>     (if set)
X-Client-Id: <anonymous per-install uuid>
```

Requests use `credentials: "omit"` — the vendor site's cookies are never
attached. Respond `2xx` to acknowledge; any other status re-queues the line.

### `POST /api/draft-lines` payload

**Amazon (ASIN only):**
```json
{
  "type": "draft_line", "source": "manual", "site": "amazon",
  "url": "https://www.amazon.com/dp/B0CXYZ1234", "hostname": "www.amazon.com",
  "quantity": 3,
  "draft": { "id": "art-class", "name": "Art class" },
  "product": { "asin": "B0CXYZ1234", "title": "Pencils", "price": "$9.99", "image": "…" }
}
```

**Other vendor (extracted + HTML for verification):**
```json
{
  "type": "draft_line", "source": "auto", "site": "generic",
  "url": "https://shop.example.com/p/glue", "hostname": "shop.example.com",
  "quantity": 1,
  "draft": { "id": "fall-2026", "name": "Fall 2026 supplies" },
  "product": {
    "title": "Glue Stick", "price": "2.50", "currency": "USD",
    "image": "…", "description": "…", "variant": "Color: Clear"
  },
  "structured": { "jsonld": [ … ], "og": { … } },
  "html": "<…trimmed page html…>"
}
```

`source` is `"auto"` (auto-captured on page view) or `"manual"` (the parent
clicked **Add to Draft**). The server returns
`{ "ok": true, "id", "status": "preapproved"|"review", "product", "quantity" }`.

## Reference server

```bash
cd extension/server
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
API_TOKEN=dev-secret uvicorn app:app --reload --port 8787
```

- Stores draft lines in SQLite; scrapes generic HTML to fill missing fields.
- Demo pre-approval: anything on `iew.com` (or ASIN `B0PREAPPROVED`) returns
  `preapproved`; everything else returns `review`.
- `GET /api/draft-lines` lists what's been received (debug).

Point the extension at `http://localhost:8787` with token `dev-secret` to try it
locally (leave `API_TOKEN` empty to disable auth in dev).

### Wiring into the real platform (Laravel sketch)

```php
// routes/api.php
Route::middleware('auth.parent')->group(function () {
    Route::get('/drafts',       [DraftController::class, 'index']);
    Route::get('/preapproval',  [CatalogController::class, 'check']);
    Route::post('/draft-lines', [DraftLineController::class, 'store']);
});

// DraftLineController@store
public function store(Request $r) {
    $p = $r->all();
    $asin = $p['product']['asin'] ?? ($p['amazon']['asin'] ?? null);
    $status = Catalog::isPreapproved($p['url'] ?? null, $asin) ? 'preapproved' : 'review';
    $line = DraftLine::create([
        'draft_id' => $p['draft']['id'] ?? null,
        'source'   => $p['source'] ?? null,
        'site'     => $p['site'] ?? null,
        'url'      => $p['url'] ?? null,
        'asin'     => $asin,
        'quantity' => $p['quantity'] ?? 1,
        'fields'   => $p['product'] ?? [],
        'status'   => $status,
    ]);
    if ($status !== 'preapproved') AiGrade::dispatch($line);   // F2 → staff review (F3)
    return response()->json(['ok' => true, 'id' => $line->id, 'status' => $status]);
}
```

## Scope status

Implemented in this repo (front-end + reference backend):

- **E3** floating "Add to Draft" button, current-draft display + inline switch
  (popup), quantity stepper, per-site dismiss.
- **E4** pre-approval badge before/after add.
- **E5** product extraction (title/price/currency/image/description/variant;
  Amazon via ASIN) with JSON-LD/OpenGraph + heuristic fallback.
- **E6** draft-line submission; non-catalog items land in the review path.
- **F1** pre-approval matching (reference catalog) and **F4** Amazon ASIN-only
  path.
- Plus the requested **Start / Pause** session control and **auto-capture**.

Larger workstreams represented as stubs / left for the platform team:

- **E1** Chrome Web Store + Firefox Add-ons publishing (needs school-owned
  developer accounts — §6).
- **E2** parent SSO into the extension (here: a pasted token).
- **F2/F3** AI grading + staff-review-queue UI (server marks `review`; grading
  job is a stub).

## Notes

- Product-page detection and variant extraction are heuristic; recognized
  vendors (Target, Best Buy, IEW, …) can get tailored paths server-side (F4)
  without re-shipping the extension.
- `clientId` is an anonymous per-install UUID.
- Because the extension reports captured product activity to your servers,
  ensure parents are informed and consent (Chrome/Firefox store policy + privacy
  law). Start/Pause keeps capture explicit and parent-controlled.
