/* Options page logic — load/save settings, run a test capture. */

const $ = (id) => document.getElementById(id);

function setStatus(msg, ok = true) {
  const el = $("status");
  el.textContent = msg;
  el.style.color = ok ? "#0d9488" : "#dc2626";
  if (msg) setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, 4000);
}

async function load() {
  const cfg = await chrome.storage.local.get(["endpoint", "apiKey", "enabled", "clientId", "queue"]);
  $("endpoint").value = cfg.endpoint || "";
  $("apiKey").value = cfg.apiKey || "";
  $("enabled").checked = cfg.enabled !== false;
  $("clientId").textContent = cfg.clientId || "(set on install)";
  $("queue").textContent = Array.isArray(cfg.queue) ? cfg.queue.length : 0;
}

async function save() {
  let endpoint = $("endpoint").value.trim();
  if (endpoint && !/^https?:\/\//i.test(endpoint)) {
    setStatus("Endpoint must start with http(s)://", false);
    return;
  }
  await chrome.storage.local.set({
    endpoint,
    apiKey: $("apiKey").value.trim(),
    enabled: $("enabled").checked
  });
  setStatus("Saved ✓");
  // Saving a working endpoint is a good moment to drain anything queued.
  chrome.runtime.sendMessage({ type: "flushNow" }, () => void chrome.runtime.lastError);
}

function test() {
  setStatus("Sending…");
  chrome.runtime.sendMessage({ type: "sendTest" }, (r) => {
    if (chrome.runtime.lastError) { setStatus(chrome.runtime.lastError.message, false); return; }
    if (r && r.ok) setStatus("Test capture delivered ✓");
    else setStatus("Failed: " + ((r && r.error) || "unknown"), false);
  });
}

$("save").addEventListener("click", save);
$("test").addEventListener("click", test);
load();
