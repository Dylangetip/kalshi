/* FundingHub / AYL Vendor Capture — background service worker.
 *
 * Owns configuration, the capture-session state (Start / Pause), and all
 * network I/O. Content scripts ask for state, run pre-approval checks, and
 * submit draft lines through here. Submissions that can't be delivered are
 * buffered to an on-disk retry queue and flushed by a periodic alarm.
 *
 * Endpoints (relative to the configured platform base URL):
 *   POST <base>/api/draft-lines       create a draft line
 *   GET  <base>/api/preapproval       pre-approval lookup (url / asin)
 *   GET  <base>/api/drafts            list the parent's drafts
 */

const RECENT_MAX = 20;
const QUEUE_MAX = 300;
const FLUSH_ALARM = "fh-flush";

const DEFAULT_DRAFT = { id: "default", name: "My Draft" };

chrome.runtime.onInstalled.addListener(async () => {
  const cur = await chrome.storage.local.get(["clientId", "capturing", "currentDraft"]);
  const patch = {};
  if (!cur.clientId) patch.clientId = crypto.randomUUID();
  if (cur.capturing === undefined) patch.capturing = false; // off until the parent starts
  if (!cur.currentDraft) patch.currentDraft = DEFAULT_DRAFT;
  if (Object.keys(patch).length) await chrome.storage.local.set(patch);
  chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: 1 });
  refreshBadge();
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: 1 });
  flushQueue();
  refreshBadge();
});

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === FLUSH_ALARM) flushQueue();
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const handlers = {
    getState: () => getState(),
    setCapturing: () => setCapturing(!!msg.on),
    setDraft: () => setDraft(msg.draft),
    refreshDrafts: () => fetchDrafts(),
    preapproval: () => preapproval(msg),
    addToDraft: () => addToDraft(msg.payload, sender),
    flushNow: () => flushQueue().then((sent) => ({ ok: true, sent })),
    sendTest: () => sendTest()
  };
  const h = handlers[msg && msg.type];
  if (!h) return false;
  Promise.resolve(h())
    .then((r) => sendResponse(r))
    .catch((e) => sendResponse({ ok: false, error: String(e) }));
  return true; // async response
});

/* ------------------------------- session -------------------------------- */

async function getState() {
  const c = await chrome.storage.local.get(["capturing", "currentDraft", "drafts", "baseUrl", "queue", "startedAt"]);
  return {
    ok: true,
    capturing: !!c.capturing,
    currentDraft: c.currentDraft || DEFAULT_DRAFT,
    drafts: Array.isArray(c.drafts) && c.drafts.length ? c.drafts : [DEFAULT_DRAFT],
    baseUrl: c.baseUrl || "",
    queueLen: Array.isArray(c.queue) ? c.queue.length : 0,
    startedAt: c.startedAt || null
  };
}

async function setCapturing(on) {
  await chrome.storage.local.set({
    capturing: on,
    startedAt: on ? Date.now() : null,
    sessionSeen: [] // reset the per-session auto-capture de-dup set
  });
  refreshBadge();
  return { ok: true, capturing: on };
}

async function setDraft(draft) {
  if (draft && draft.id) await chrome.storage.local.set({ currentDraft: draft });
  return { ok: true };
}

/* ------------------------------- drafts --------------------------------- */

async function fetchDrafts() {
  const { baseUrl, apiKey } = await chrome.storage.local.get(["baseUrl", "apiKey"]);
  if (!baseUrl) return { ok: false, error: "No base URL configured" };
  try {
    const res = await fetch(baseUrl.replace(/\/$/, "") + "/api/drafts", {
      headers: authHeaders(apiKey),
      credentials: "omit"
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    const drafts = Array.isArray(data.drafts) ? data.drafts : [];
    if (drafts.length) await chrome.storage.local.set({ drafts });
    return { ok: true, drafts };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

/* ----------------------------- pre-approval ----------------------------- */

async function preapproval({ url, asin, site }) {
  const { baseUrl, apiKey } = await chrome.storage.local.get(["baseUrl", "apiKey"]);
  if (!baseUrl) return { status: "unknown" };
  try {
    const q = new URLSearchParams();
    if (url) q.set("url", url);
    if (asin) q.set("asin", asin);
    if (site) q.set("site", site);
    const res = await fetch(baseUrl.replace(/\/$/, "") + "/api/preapproval?" + q.toString(), {
      headers: authHeaders(apiKey),
      credentials: "omit"
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    // Normalize to: preapproved | review | unknown
    return { status: data.preapproved ? "preapproved" : (data.status || "review") };
  } catch {
    return { status: "unknown" };
  }
}

/* --------------------------- submit draft line -------------------------- */

async function addToDraft(payload, sender) {
  const cfg = await chrome.storage.local.get(["baseUrl", "apiKey", "clientId", "currentDraft", "recent"]);
  payload.clientId = cfg.clientId || null;
  payload.extensionVersion = chrome.runtime.getManifest().version;
  if (!payload.draft) payload.draft = cfg.currentDraft || DEFAULT_DRAFT;
  if (sender && sender.tab && sender.tab.url) payload.tabUrl = sender.tab.url;

  // Recent log for the popup.
  const recent = Array.isArray(cfg.recent) ? cfg.recent : [];
  recent.unshift({
    t: payload.capturedAt,
    source: payload.source || "manual",
    site: payload.site,
    host: payload.hostname,
    title: payload.product && payload.product.title,
    asin: payload.product && payload.product.asin,
    qty: payload.quantity || 1,
    draft: payload.draft && payload.draft.name
  });
  recent.splice(RECENT_MAX);
  await chrome.storage.local.set({ recent });

  if (!cfg.baseUrl) {
    await enqueue(payload);
    return { ok: true, queued: true, status: payload.preapproval || "unknown" };
  }
  try {
    const result = await postDraftLine(cfg.baseUrl, cfg.apiKey, payload);
    refreshBadge();
    return { ok: true, status: result.status || payload.preapproval || "review", draftName: (payload.draft || {}).name };
  } catch (e) {
    await enqueue(payload);
    return { ok: false, queued: true, error: String(e) };
  }
}

async function postDraftLine(baseUrl, apiKey, payload) {
  const res = await fetch(baseUrl.replace(/\/$/, "") + "/api/draft-lines", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(apiKey, payload.clientId) },
    body: JSON.stringify(payload),
    credentials: "omit"
  });
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json().catch(() => ({}));
}

/* ------------------------------ retry queue ----------------------------- */

async function enqueue(payload) {
  const { queue } = await chrome.storage.local.get(["queue"]);
  const q = Array.isArray(queue) ? queue : [];
  q.push(payload);
  if (q.length > QUEUE_MAX) q.splice(0, q.length - QUEUE_MAX);
  await chrome.storage.local.set({ queue: q });
  refreshBadge();
}

async function flushQueue() {
  const cfg = await chrome.storage.local.get(["baseUrl", "apiKey", "queue"]);
  let q = Array.isArray(cfg.queue) ? cfg.queue : [];
  if (!cfg.baseUrl || q.length === 0) return 0;
  let sent = 0;
  while (q.length) {
    try {
      await postDraftLine(cfg.baseUrl, cfg.apiKey, q[0]);
      q.shift();
      sent++;
    } catch {
      break;
    }
  }
  await chrome.storage.local.set({ queue: q });
  refreshBadge();
  return sent;
}

async function sendTest() {
  const cfg = await chrome.storage.local.get(["baseUrl", "apiKey", "clientId", "currentDraft"]);
  if (!cfg.baseUrl) return { ok: false, error: "No base URL configured" };
  const payload = {
    type: "draft_line",
    source: "test",
    capturedAt: new Date().toISOString(),
    url: "https://example.com/product/test",
    hostname: "example.com",
    site: "generic",
    quantity: 1,
    draft: cfg.currentDraft || DEFAULT_DRAFT,
    product: { title: "AYL test item", price: "1.00", currency: "USD" },
    clientId: cfg.clientId || null,
    extensionVersion: chrome.runtime.getManifest().version
  };
  try {
    await postDraftLine(cfg.baseUrl, cfg.apiKey, payload);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

/* -------------------------------- helpers ------------------------------- */

function authHeaders(apiKey, clientId) {
  const h = {};
  if (apiKey) h["Authorization"] = "Bearer " + apiKey;
  if (clientId) h["X-Client-Id"] = clientId;
  return h;
}

async function refreshBadge() {
  const { capturing, queue } = await chrome.storage.local.get(["capturing", "queue"]);
  const pending = Array.isArray(queue) ? queue.length : 0;
  try {
    if (capturing) {
      await chrome.action.setBadgeBackgroundColor({ color: "#0d9488" });
      await chrome.action.setBadgeText({ text: pending ? String(pending) : "ON" });
    } else if (pending) {
      await chrome.action.setBadgeBackgroundColor({ color: "#b45309" });
      await chrome.action.setBadgeText({ text: String(pending) });
    } else {
      await chrome.action.setBadgeText({ text: "" });
    }
  } catch {
    /* action not ready */
  }
}
