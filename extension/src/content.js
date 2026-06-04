/* FundingHub / AYL Vendor Capture — content script.
 *
 * Parent-initiated capture. Nothing happens until the parent presses "Start"
 * (the session toggle). While a session is active and the current page looks
 * like a vendor product page, we:
 *   - auto-capture the product into the parent's current draft (once per
 *     product per session), and
 *   - show a floating "Add to Draft" button for a deliberate add with a
 *     quantity selector and a pre-approval badge.
 *
 * Amazon → URL/ASIN only (no scraping). Every other vendor → extract
 * title / price / currency / image / description / variant, plus the raw
 * structured data (JSON-LD / OpenGraph) so the backend can verify.
 *
 * Cart-level capture is intentionally NOT implemented (out of scope, §5).
 */
(() => {
  "use strict";

  const PRICE_RE = /(?:[$£€¥]|USD|EUR|GBP)\s?\d[\d.,]*/i;
  const ASIN_URL_RE =
    /(?:\/dp\/|\/gp\/product\/|\/gp\/aw\/d\/|\/gp\/aw\/ya\/|\/product\/|[?&]asin=)([A-Z0-9]{10})(?:[/?&#]|$)/i;
  const MAX_HTML = 400_000;

  let state = { capturing: false, currentDraft: { id: "default", name: "My Draft" } };
  let dismissedHosts = [];
  let mounted = false;
  let host, shadow; // floating UI
  let lastEvaluatedHref = "";

  init();

  async function init() {
    const s = await getState();
    if (s && s.ok) state = s;
    const { dismissedHosts: dh } = await chrome.storage.local.get(["dismissedHosts"]);
    dismissedHosts = Array.isArray(dh) ? dh : [];

    chrome.storage.onChanged.addListener(onStorageChanged);
    hookSpaNavigation();
    evaluate();
  }

  function getState() {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type: "getState" }, (r) => {
          void chrome.runtime.lastError;
          resolve(r);
        });
      } catch {
        resolve(null);
      }
    });
  }

  function onStorageChanged(changes, area) {
    if (area !== "local") return;
    if (changes.capturing) state.capturing = changes.capturing.newValue;
    if (changes.currentDraft) state.currentDraft = changes.currentDraft.newValue;
    if (changes.dismissedHosts) dismissedHosts = changes.dismissedHosts.newValue || [];
    evaluate();
  }

  /* ------------------------- decide what to show -------------------------- */

  function evaluate() {
    const onProduct = isProductPage();
    const dismissed = dismissedHosts.includes(location.hostname);

    if (state.capturing && onProduct && !dismissed) {
      mountButton();
      autoCaptureOnce();
    } else {
      unmountButton();
    }
  }

  function isProductPage() {
    if (isAmazon()) return !!extractAsin() || !!document.querySelector("#productTitle");
    const og = document.querySelector('meta[property="og:type"]');
    if (og && /product/i.test(og.getAttribute("content") || "")) return true;
    if (hasProductJsonLd()) return true;
    // Heuristic fallback: a single primary heading + a visible price.
    const h1 = document.querySelector("h1");
    return !!h1 && PRICE_RE.test((document.body.innerText || "").slice(0, 6000));
  }

  /* ----------------------------- auto-capture ----------------------------- */

  async function autoCaptureOnce() {
    const key = productKey();
    const { sessionSeen } = await chrome.storage.local.get(["sessionSeen"]);
    const seen = Array.isArray(sessionSeen) ? sessionSeen : [];
    if (seen.includes(key)) return; // already auto-captured this product this session
    seen.push(key);
    await chrome.storage.local.set({ sessionSeen: seen });
    submit("auto", 1);
  }

  function productKey() {
    return isAmazon() ? "amazon:" + (extractAsin() || location.pathname) : "url:" + location.href.split("#")[0];
  }

  /* --------------------------- product extraction ------------------------- */

  function buildProduct() {
    const structured = extractStructured();
    if (isAmazon()) {
      return {
        product: {
          asin: extractAsin(),
          title:
            document.querySelector("#productTitle")?.textContent?.trim() ||
            document.title.replace(/\s*[:|-]\s*Amazon.*$/i, "").trim(),
          price: document.querySelector(".a-price .a-offscreen")?.textContent?.trim() || null,
          currency: null,
          image: document.querySelector("#landingImage, #imgBlkFront")?.getAttribute("src") || null
        },
        html: null, // Amazon: ASIN only, no scraping
        structured
      };
    }
    const fromLd = productFromJsonLd(structured);
    return {
      product: {
        asin: null,
        title: fromLd.title || og("og:title") || document.querySelector("h1")?.textContent?.trim() || document.title,
        price: fromLd.price || og("product:price:amount") || og("og:price:amount") || priceFromText(),
        currency: fromLd.currency || og("product:price:currency") || og("og:price:currency") || null,
        image: fromLd.image || og("og:image") || document.querySelector("img")?.src || null,
        description:
          fromLd.description ||
          document.querySelector('meta[name="description"]')?.getAttribute("content") ||
          null,
        variant: extractVariant()
      },
      html: trimHtml(document.body.outerHTML),
      structured
    };
  }

  function extractVariant() {
    const parts = [];
    document.querySelectorAll("select").forEach((sel) => {
      const opt = sel.options && sel.options[sel.selectedIndex];
      if (opt && opt.value && !/choose|select/i.test(opt.textContent)) {
        const label = sel.getAttribute("aria-label") || sel.name || "";
        parts.push((label ? label + ": " : "") + opt.textContent.trim());
      }
    });
    document
      .querySelectorAll('[aria-pressed="true"],[aria-checked="true"],.swatch.selected,[class*="selected"][class*="swatch"]')
      .forEach((el) => {
        const t = (el.getAttribute("aria-label") || el.title || el.textContent || "").trim();
        if (t && t.length < 40) parts.push(t);
      });
    return parts.length ? Array.from(new Set(parts)).slice(0, 4).join(" / ") : null;
  }

  function priceFromText() {
    const m = (document.body.innerText || "").slice(0, 6000).match(PRICE_RE);
    return m ? m[0] : null;
  }

  /* -------------------------------- submit -------------------------------- */

  function submit(source, quantity, preapproval) {
    const built = buildProduct();
    const payload = {
      type: "draft_line",
      source, // "auto" | "manual" | "test"
      capturedAt: new Date().toISOString(),
      url: canonicalUrl(),
      hostname: location.hostname,
      title: document.title,
      site: isAmazon() ? "amazon" : "generic",
      quantity: Math.max(1, quantity || 1),
      draft: state.currentDraft,
      preapproval: preapproval || "unknown",
      ...built
    };
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type: "addToDraft", payload }, (r) => {
          void chrome.runtime.lastError;
          resolve(r || { ok: false });
        });
      } catch {
        resolve({ ok: false });
      }
    });
  }

  /* ------------------------------ floating UI ----------------------------- */

  function mountButton() {
    if (mounted) {
      updateDraftLabel();
      return;
    }
    mounted = true;
    host = document.createElement("div");
    host.id = "ayl-capture-host";
    host.style.cssText = "position:fixed;z-index:2147483647;right:20px;bottom:20px;";
    shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = TEMPLATE;
    document.documentElement.appendChild(host);

    qs(".close").addEventListener("click", dismissSite);
    qs(".pause").addEventListener("click", () =>
      chrome.runtime.sendMessage({ type: "setCapturing", on: false }, () => void chrome.runtime.lastError)
    );
    qs(".minus").addEventListener("click", () => bumpQty(-1));
    qs(".plus").addEventListener("click", () => bumpQty(1));
    qs(".add").addEventListener("click", onAdd);

    updateDraftLabel();
    runPreapproval();
  }

  function unmountButton() {
    if (host && host.parentNode) host.parentNode.removeChild(host);
    mounted = false;
    host = shadow = null;
  }

  function qs(sel) {
    return shadow.querySelector(sel);
  }

  function updateDraftLabel() {
    if (!mounted) return;
    qs(".draft-name").textContent = (state.currentDraft && state.currentDraft.name) || "My Draft";
  }

  function bumpQty(d) {
    const input = qs(".qty");
    input.value = String(Math.max(1, (parseInt(input.value, 10) || 1) + d));
  }

  function runPreapproval() {
    setBadge("checking", "Checking…");
    chrome.runtime.sendMessage(
      { type: "preapproval", url: canonicalUrl(), asin: extractAsin(), site: isAmazon() ? "amazon" : "generic" },
      (r) => {
        void chrome.runtime.lastError;
        const status = (r && r.status) || "unknown";
        if (status === "preapproved") setBadge("ok", "✓ Pre-approved");
        else if (status === "review") setBadge("review", "Will be reviewed");
        else setBadge("none", "");
        if (shadow) shadow.host.dataset.status = status;
      }
    );
  }

  function setBadge(kind, text) {
    if (!mounted) return;
    const b = qs(".badge");
    b.textContent = text;
    b.dataset.kind = kind;
    b.style.display = text ? "inline-flex" : "none";
  }

  async function onAdd() {
    const qty = parseInt(qs(".qty").value, 10) || 1;
    const status = (shadow.host.dataset.status) || "unknown";
    setState("adding");
    const r = await submit("manual", qty, status);
    if (r && r.ok) {
      const pre = (r.status || status) === "preapproved";
      setState("done", pre ? "✓ Pre-approved — added" : "✓ Added to draft");
    } else if (r && r.queued) {
      setState("done", "Saved — will sync");
    } else {
      setState("error", "Couldn’t add — paste URL instead");
    }
    setTimeout(() => setState("idle"), 2600);
  }

  function setState(s, msg) {
    if (!mounted) return;
    const add = qs(".add");
    if (s === "adding") {
      add.disabled = true;
      add.textContent = "Adding…";
    } else if (s === "done") {
      add.disabled = true;
      add.textContent = msg;
    } else if (s === "error") {
      add.disabled = false;
      qs(".fallback").style.display = "block";
      add.textContent = msg;
    } else {
      add.disabled = false;
      qs(".fallback").style.display = "none";
      add.textContent = "Add to Draft";
    }
  }

  function dismissSite() {
    if (!dismissedHosts.includes(location.hostname)) {
      dismissedHosts.push(location.hostname);
      chrome.storage.local.set({ dismissedHosts });
    }
    unmountButton();
  }

  /* -------------------------------- amazon -------------------------------- */

  function isAmazon() {
    return /(^|\.)amazon\.[a-z.]{2,}$/i.test(location.hostname) || /(^|\.)amzn\./i.test(location.hostname);
  }

  function extractAsin() {
    const m = location.href.match(ASIN_URL_RE);
    if (m) return m[1].toUpperCase();
    const el = document.querySelector("input#ASIN, input[name='ASIN'], [data-asin]");
    const v = el && (el.value || el.getAttribute("data-asin"));
    return v && /^[A-Z0-9]{10}$/i.test(v) ? v.toUpperCase() : null;
  }

  /* ------------------------------ structured ------------------------------ */

  function hasProductJsonLd() {
    return !!productFromJsonLd(extractStructured()).title || jsonLdProducts().length > 0;
  }

  function jsonLdProducts() {
    const out = [];
    document.querySelectorAll('script[type="application/ld+json"]').forEach((s) => {
      try {
        const data = JSON.parse(s.textContent);
        (Array.isArray(data) ? data : [data]).forEach((d) => {
          const t = d && d["@type"];
          if (t && /product/i.test(Array.isArray(t) ? t.join() : String(t))) out.push(d);
        });
      } catch {
        /* ignore */
      }
    });
    return out;
  }

  function productFromJsonLd(structured) {
    const p = (structured.jsonld && structured.jsonld[0]) || {};
    const offer = Array.isArray(p.offers) ? p.offers[0] : p.offers || {};
    const img = Array.isArray(p.image) ? p.image[0] : p.image;
    return {
      title: p.name || null,
      price: (offer && (offer.price || offer.lowPrice)) || null,
      currency: (offer && offer.priceCurrency) || null,
      image: typeof img === "string" ? img : null,
      description: typeof p.description === "string" ? p.description : null
    };
  }

  function extractStructured() {
    const out = { jsonld: jsonLdProducts(), og: {} };
    document.querySelectorAll('meta[property^="og:"], meta[property^="product:"]').forEach((m) => {
      const k = m.getAttribute("property");
      const v = m.getAttribute("content");
      if (k && v) out.og[k] = v;
    });
    return out;
  }

  function og(key) {
    const m = document.querySelector(`meta[property="${key}"]`);
    return m ? m.getAttribute("content") : null;
  }

  /* ------------------------------- plumbing ------------------------------- */

  function canonicalUrl() {
    const link = document.querySelector('link[rel="canonical"]');
    return (link && link.href) || og("og:url") || location.href.split("#")[0];
  }

  function trimHtml(html) {
    if (!html) return "";
    return html.length > MAX_HTML ? html.slice(0, MAX_HTML) : html;
  }

  function hookSpaNavigation() {
    const fire = () => {
      if (location.href === lastEvaluatedHref) return;
      lastEvaluatedHref = location.href;
      unmountButton(); // re-evaluate fresh on the new page
      setTimeout(evaluate, 500);
    };
    for (const m of ["pushState", "replaceState"]) {
      const orig = history[m];
      history[m] = function () {
        const r = orig.apply(this, arguments);
        fire();
        return r;
      };
    }
    window.addEventListener("popstate", fire);
  }

  const TEMPLATE = `
    <style>
      * { box-sizing: border-box; }
      .card {
        width: 300px; font: 13px/1.4 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
        background: #fff; color: #0f172a; border: 1px solid #e2e8f0;
        border-radius: 14px; box-shadow: 0 10px 30px rgba(2,6,23,.18); overflow: hidden;
      }
      .top { display: flex; align-items: center; gap: 8px; padding: 10px 12px; background: #0d9488; color: #fff; }
      .top .dot { width: 8px; height: 8px; border-radius: 50%; background: #fff; box-shadow: 0 0 0 3px rgba(255,255,255,.35); }
      .top .title { font-weight: 700; flex: 1; }
      .top .close { cursor: pointer; opacity: .85; font-size: 16px; line-height: 1; }
      .body { padding: 12px; }
      .draft { display: flex; align-items: center; gap: 6px; color: #475569; margin-bottom: 10px; }
      .draft b { color: #0f172a; }
      .badge { display: none; align-items: center; gap: 4px; font-weight: 700; font-size: 12px;
        padding: 3px 8px; border-radius: 999px; margin-bottom: 10px; }
      .badge[data-kind="ok"] { background: #ccfbf1; color: #0f766e; }
      .badge[data-kind="review"] { background: #fef3c7; color: #92400e; }
      .badge[data-kind="checking"] { background: #e2e8f0; color: #475569; }
      .qtyrow { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
      .qtyrow label { color: #475569; }
      .stepper { display: flex; align-items: center; border: 1px solid #cbd5e1; border-radius: 8px; overflow: hidden; }
      .stepper button { width: 30px; height: 30px; border: 0; background: #f1f5f9; cursor: pointer; font-size: 16px; }
      .stepper .qty { width: 44px; height: 30px; border: 0; text-align: center; font: inherit; }
      .add { width: 100%; padding: 11px; border: 0; border-radius: 10px; background: #0d9488;
        color: #fff; font-weight: 700; font-size: 14px; cursor: pointer; }
      .add:disabled { opacity: .9; cursor: default; }
      .fallback { display: none; margin-top: 8px; font-size: 12px; }
      .foot { display: flex; justify-content: space-between; margin-top: 10px; font-size: 12px; }
      .pause { color: #b45309; cursor: pointer; font-weight: 600; }
      a { color: #0d9488; }
    </style>
    <div class="card">
      <div class="top">
        <span class="dot"></span>
        <span class="title">Add to Draft</span>
        <span class="close" title="Hide on this site">×</span>
      </div>
      <div class="body">
        <div class="draft">Draft: <b class="draft-name">My Draft</b></div>
        <div class="badge"></div>
        <div class="qtyrow">
          <label>Qty</label>
          <div class="stepper">
            <button class="minus" type="button">−</button>
            <input class="qty" type="number" min="1" value="1" />
            <button class="plus" type="button">+</button>
          </div>
        </div>
        <button class="add" type="button">Add to Draft</button>
        <div class="fallback">Couldn’t read this page — <a href="#" target="_blank">paste the URL into your draft</a>.</div>
        <div class="foot">
          <span></span>
          <span class="pause">⏸ Pause capturing</span>
        </div>
      </div>
    </div>`;
})();
