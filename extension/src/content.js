/* FundingHub Cart Tracker — content script.
 *
 * Runs on every page. Two capture triggers:
 *   1. Add-to-cart button clicks (heuristic across arbitrary sites).
 *   2. Cart / checkout page contents (scraped once per page view).
 *
 * For Amazon we extract the ASIN; for every other site we send the relevant
 * HTML so the backend can scrape title / price / etc. Captures are handed to
 * the background service worker, which owns the network + retry logic.
 */
(() => {
  "use strict";

  // Phrases that indicate an "add to cart" control. Lowercased substring match.
  const ADD_KEYWORDS = [
    "add to cart", "add to bag", "add to basket", "add to trolley",
    "add to my bag", "add to order", "add item", "buy now", "buy it now",
    "add-to-cart", "addtocart", "add_to_cart",
    "in den warenkorb", "ajouter au panier", "anadir al carrito",
    "anadir a la cesta", "aggiungi al carrello"
  ];

  // Controls that mention "cart" but are NOT add actions — ignore these.
  const NEGATIVE_KEYWORDS = [
    "view cart", "go to cart", "your cart", "cart total", "edit cart",
    "update cart", "empty cart", "remove", "view bag", "checkout",
    "continue shopping", "saved for later", "wish list", "wishlist"
  ];

  // URL shapes that mean "this is a cart / checkout page".
  const CART_URL_RE =
    /(\/cart|\/checkout|\/basket|\/bag|\/shopping[-_]?cart|\/panier|\/warenkorb|\/carrito)(\/|\?|#|$)/i;

  const PRICE_RE = /(?:[$£€¥]|usd|eur|gbp)\s?\d/i;
  const ASIN_URL_RE =
    /(?:\/dp\/|\/gp\/product\/|\/gp\/aw\/d\/|\/gp\/aw\/ya\/|\/product\/|[?&]asin=)([A-Z0-9]{10})(?:[/?&#]|$)/i;
  const MAX_HTML = 400_000; // cap payload size (~400 KB)

  let enabled = true;
  let lastCartHref = "";

  init();

  async function init() {
    const s = await getSettings();
    enabled = s.enabled !== false;
    if (!enabled) return;

    // Capture-phase so we still see the click even if the site stops propagation.
    document.addEventListener("click", onClick, true);
    hookSpaNavigation();
    // Defer initial cart scrape until the page has settled.
    setTimeout(maybeCaptureCartPage, 1200);
  }

  function getSettings() {
    return new Promise((resolve) => {
      try {
        chrome.storage.local.get(["enabled"], (r) => resolve(r || {}));
      } catch {
        resolve({});
      }
    });
  }

  /* ------------------------------ add to cart ------------------------------ */

  function onClick(e) {
    let node = e.target;
    for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
      if (isAddToCartControl(node)) {
        captureAddToCart(node);
        return;
      }
    }
  }

  function isAddToCartControl(node) {
    if (!(node instanceof Element)) return false;
    const tag = node.tagName.toLowerCase();
    const looksClickable =
      tag === "button" ||
      tag === "a" ||
      (tag === "input" && /submit|button|image/i.test(node.getAttribute("type") || "")) ||
      node.getAttribute("role") === "button" ||
      node.hasAttribute("onclick");
    if (!looksClickable) return false;

    const hay = controlSignals(node);
    if (!hay) return false;
    if (NEGATIVE_KEYWORDS.some((k) => hay.includes(k))) return false;
    return ADD_KEYWORDS.some((k) => hay.includes(k));
  }

  // Text + accessible labels + identifying attributes, lowercased.
  function controlSignals(node) {
    const parts = [
      (node.innerText || node.textContent || "").slice(0, 120),
      node.getAttribute("aria-label") || "",
      node.getAttribute("title") || "",
      node.getAttribute("name") || "",
      node.getAttribute("value") || "",
      node.id || "",
      typeof node.className === "string" ? node.className : "",
      node.getAttribute("data-action") || "",
      node.getAttribute("data-testid") || ""
    ];
    return parts.join(" ").toLowerCase().replace(/\s+/g, " ").trim();
  }

  function captureAddToCart(control) {
    const payload = basePayload("add_to_cart");
    if (payload.site === "amazon") {
      payload.amazon = extractAmazon(control);
    } else {
      const container = findProductContainer(control);
      payload.html = trimHtml((container || document.body).outerHTML);
      payload.structured = extractStructured();
    }
    send(payload);
  }

  // Climb to the smallest ancestor that looks like a product card
  // (contains a price and an image), falling back to a few levels up.
  function findProductContainer(node) {
    let best = null;
    let cur = node;
    for (let depth = 0; cur && cur !== document.body && depth < 10; depth++, cur = cur.parentElement) {
      const txt = cur.textContent || "";
      const hasPrice = PRICE_RE.test(txt);
      const hasImg = !!cur.querySelector("img");
      const isProductish =
        /product|item|sku|listing/i.test(cur.id + " " + (typeof cur.className === "string" ? cur.className : "")) ||
        /product/i.test(cur.getAttribute("itemtype") || "");
      if ((hasPrice && hasImg) || isProductish) {
        best = cur;
        if (hasPrice && hasImg) break;
      }
    }
    return best;
  }

  /* ------------------------------ cart pages ------------------------------- */

  function maybeCaptureCartPage() {
    if (!enabled) return;
    if (!CART_URL_RE.test(location.pathname + location.search) && !looksLikeCartDom()) return;
    if (location.href === lastCartHref) return; // once per page view
    lastCartHref = location.href;

    const payload = basePayload("cart_snapshot");
    if (payload.site === "amazon") {
      payload.items = scrapeAmazonCart();
    } else {
      const container = findCartContainer();
      payload.html = trimHtml((container || document.body).outerHTML);
      payload.structured = extractStructured();
      payload.items = scrapeGenericCart(container || document.body);
    }
    send(payload);
  }

  function looksLikeCartDom() {
    return !!document.querySelector(
      "[class*='cart'],[id*='cart'],[class*='basket'],[id*='basket'],[data-cart]"
    ) && /cart|basket|checkout/i.test(document.title);
  }

  function findCartContainer() {
    const sel = [
      "[data-cart]", "#cart", ".cart", "#shopping-cart", ".shopping-cart",
      "#basket", ".basket", "[class*='CartItems']", "[class*='cart-items']",
      "form[action*='cart']", "main"
    ];
    for (const s of sel) {
      const el = document.querySelector(s);
      if (el && (el.textContent || "").trim().length > 40) return el;
    }
    return null;
  }

  function scrapeGenericCart(root) {
    const items = [];
    const rows = root.querySelectorAll(
      "[class*='cart-item'],[class*='cartItem'],[class*='line-item'],[class*='lineItem'],li,tr"
    );
    rows.forEach((row) => {
      const txt = (row.textContent || "").replace(/\s+/g, " ").trim();
      const price = (txt.match(/[$£€¥]\s?\d[\d.,]*/) || [])[0] || null;
      const link = row.querySelector("a[href]");
      const img = row.querySelector("img[src]");
      const name =
        row.querySelector("[class*='name'],[class*='title'],h2,h3,h4,a")?.textContent?.trim() || null;
      if (name && (price || link)) {
        items.push({
          name: name.slice(0, 200),
          price,
          url: link ? link.href : null,
          image: img ? img.src : null
        });
      }
    });
    // De-dup by name+price, cap the list.
    const seen = new Set();
    return items
      .filter((i) => {
        const k = i.name + "|" + i.price;
        if (seen.has(k)) return false;
        seen.add(k);
        return true;
      })
      .slice(0, 50);
  }

  /* -------------------------------- amazon -------------------------------- */

  function isAmazon() {
    return /(^|\.)amazon\.([a-z.]{2,})$/i.test(location.hostname) ||
           /(^|\.)amzn\./i.test(location.hostname);
  }

  function extractAsinFromUrl(url) {
    const m = (url || "").match(ASIN_URL_RE);
    return m ? m[1].toUpperCase() : null;
  }

  function extractAmazon(control) {
    let asin = extractAsinFromUrl(location.href);
    if (!asin) {
      const el =
        document.querySelector("input#ASIN, input[name='ASIN'], input[name='asin.0']") ||
        control.closest("[data-asin]") ||
        document.querySelector("[data-asin]");
      if (el) asin = (el.value || el.getAttribute("data-asin") || "").toUpperCase() || null;
    }
    const title =
      document.querySelector("#productTitle")?.textContent?.trim() ||
      document.title.replace(/\s*[:|-]\s*Amazon.*$/i, "").trim();
    const price =
      document.querySelector(".a-price .a-offscreen")?.textContent?.trim() ||
      document.querySelector("#priceblock_ourprice, #price_inside_buybox")?.textContent?.trim() ||
      null;
    return { asin: asin || null, title, price };
  }

  function scrapeAmazonCart() {
    const items = [];
    document.querySelectorAll("[data-asin]").forEach((el) => {
      const asin = (el.getAttribute("data-asin") || "").toUpperCase();
      if (!/^[A-Z0-9]{10}$/.test(asin)) return;
      const title =
        el.querySelector(".sc-product-title, [class*='product-title'], a")?.textContent?.trim() || null;
      const price = (el.textContent.match(/[$£€¥]\s?\d[\d.,]*/) || [])[0] || null;
      const qtyEl = el.querySelector("[name='quantity'], .sc-quantity-textfield, select");
      const quantity = qtyEl ? Number(qtyEl.value) || 1 : 1;
      items.push({ asin, title, price, quantity });
    });
    // De-dup ASINs.
    const seen = new Set();
    return items.filter((i) => (seen.has(i.asin) ? false : (seen.add(i.asin), true))).slice(0, 50);
  }

  /* ------------------------------ structured ------------------------------ */

  // Pull schema.org JSON-LD Products + OpenGraph meta as scraping hints.
  function extractStructured() {
    const out = { jsonld: [], og: {} };
    document.querySelectorAll('script[type="application/ld+json"]').forEach((s) => {
      try {
        const data = JSON.parse(s.textContent);
        const arr = Array.isArray(data) ? data : [data];
        arr.forEach((d) => {
          const t = d && d["@type"];
          if (t && /product/i.test(Array.isArray(t) ? t.join() : String(t))) out.jsonld.push(d);
        });
      } catch {
        /* ignore malformed JSON-LD */
      }
    });
    document.querySelectorAll('meta[property^="og:"], meta[property^="product:"]').forEach((m) => {
      const k = m.getAttribute("property");
      const v = m.getAttribute("content");
      if (k && v) out.og[k] = v;
    });
    return out;
  }

  /* ------------------------------- plumbing ------------------------------- */

  function basePayload(type) {
    return {
      type,
      capturedAt: new Date().toISOString(),
      url: location.href,
      hostname: location.hostname,
      title: document.title,
      site: isAmazon() ? "amazon" : "generic"
    };
  }

  function trimHtml(html) {
    if (!html) return "";
    return html.length > MAX_HTML ? html.slice(0, MAX_HTML) : html;
  }

  function send(payload) {
    try {
      chrome.runtime.sendMessage({ type: "capture", payload }, () => void chrome.runtime.lastError);
    } catch {
      /* extension context invalidated (e.g. on reload) — ignore */
    }
  }

  // Re-check for a cart page after SPA route changes.
  function hookSpaNavigation() {
    const fire = () => setTimeout(maybeCaptureCartPage, 600);
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
})();
