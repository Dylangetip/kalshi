/* FundingHub Cart Tracker — background service worker.
 *
 * Owns configuration and all network I/O. Content scripts send capture
 * payloads here; we tag them with the install's client id, POST them to the
 * configured endpoint, and buffer to an on-disk retry queue when the network
 * (or config) isn't ready. A periodic alarm flushes the queue.
 */

const RECENT_MAX = 20;
const QUEUE_MAX = 300;
const FLUSH_ALARM = "fh-flush";

chrome.runtime.onInstalled.addListener(async () => {
  const cur = await chrome.storage.local.get(["clientId", "enabled"]);
  const patch = {};
  if (!cur.clientId) patch.clientId = crypto.randomUUID();
  if (cur.enabled === undefined) patch.enabled = true;
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
  if (msg && msg.type === "capture") {
    handleCapture(msg.payload, sender)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true; // keep the channel open for the async response
  }
  if (msg && msg.type === "flushNow") {
    flushQueue().then((n) => sendResponse({ ok: true, sent: n }));
    return true;
  }
  if (msg && msg.type === "sendTest") {
    sendTest().then((r) => sendResponse(r));
    return true;
  }
  return false;
});

async function handleCapture(payload, sender) {
  const cfg = await chrome.storage.local.get(["endpoint", "apiKey", "clientId", "recent"]);
  payload.clientId = cfg.clientId || null;
  payload.extensionVersion = chrome.runtime.getManifest().version;
  if (sender && sender.tab && sender.tab.url) payload.tabUrl = sender.tab.url;

  // Keep a small recent-captures log for the popup UI.
  const recent = Array.isArray(cfg.recent) ? cfg.recent : [];
  recent.unshift({
    t: payload.capturedAt,
    type: payload.type,
    site: payload.site,
    host: payload.hostname,
    asin: (payload.amazon && payload.amazon.asin) || null,
    items: Array.isArray(payload.items) ? payload.items.length : null
  });
  recent.splice(RECENT_MAX);
  await chrome.storage.local.set({ recent });

  if (!cfg.endpoint) {
    await enqueue(payload);
    return;
  }
  try {
    await postCapture(cfg.endpoint, cfg.apiKey, payload);
  } catch (e) {
    await enqueue(payload);
    throw e;
  }
  refreshBadge();
}

async function postCapture(endpoint, apiKey, payload) {
  const headers = { "Content-Type": "application/json" };
  if (apiKey) headers["Authorization"] = "Bearer " + apiKey;
  if (payload.clientId) headers["X-Client-Id"] = payload.clientId;

  const res = await fetch(endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
    // Captures are anonymous telemetry; don't attach the user's site cookies.
    credentials: "omit"
  });
  if (!res.ok) throw new Error("HTTP " + res.status);
}

async function enqueue(payload) {
  const { queue } = await chrome.storage.local.get(["queue"]);
  const q = Array.isArray(queue) ? queue : [];
  q.push(payload);
  if (q.length > QUEUE_MAX) q.splice(0, q.length - QUEUE_MAX); // drop oldest
  await chrome.storage.local.set({ queue: q });
  refreshBadge();
}

// Try to drain the retry queue. Returns the number of payloads sent.
async function flushQueue() {
  const cfg = await chrome.storage.local.get(["endpoint", "apiKey", "queue"]);
  let q = Array.isArray(cfg.queue) ? cfg.queue : [];
  if (!cfg.endpoint || q.length === 0) return 0;

  let sent = 0;
  while (q.length) {
    const item = q[0];
    try {
      await postCapture(cfg.endpoint, cfg.apiKey, item);
      q.shift();
      sent++;
    } catch {
      break; // still offline / erroring — stop and keep the rest
    }
  }
  await chrome.storage.local.set({ queue: q });
  refreshBadge();
  return sent;
}

async function sendTest() {
  const cfg = await chrome.storage.local.get(["endpoint", "apiKey", "clientId"]);
  if (!cfg.endpoint) return { ok: false, error: "No endpoint configured" };
  const payload = {
    type: "test",
    capturedAt: new Date().toISOString(),
    url: "https://example.com/test",
    hostname: "example.com",
    title: "FundingHub test capture",
    site: "generic",
    html: "<div>test</div>",
    clientId: cfg.clientId || null,
    extensionVersion: chrome.runtime.getManifest().version
  };
  try {
    await postCapture(cfg.endpoint, cfg.apiKey, payload);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

async function refreshBadge() {
  const { queue } = await chrome.storage.local.get(["queue"]);
  const n = Array.isArray(queue) ? queue.length : 0;
  try {
    await chrome.action.setBadgeBackgroundColor({ color: "#b45309" });
    await chrome.action.setBadgeText({ text: n ? String(n) : "" });
  } catch {
    /* action API not ready */
  }
}
