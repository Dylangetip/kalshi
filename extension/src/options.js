/* Options — connect the extension to the FundingHub platform. */

const $ = (id) => document.getElementById(id);

function setStatus(msg, ok = true) {
  const el = $("status");
  el.textContent = msg;
  el.style.color = ok ? "#0d9488" : "#dc2626";
  if (msg) setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, 4000);
}

async function load() {
  const cfg = await chrome.storage.local.get(["baseUrl", "apiKey", "clientId", "queue"]);
  $("baseUrl").value = cfg.baseUrl || "";
  $("apiKey").value = cfg.apiKey || "";
  $("clientId").textContent = cfg.clientId || "(set on install)";
  $("queue").textContent = Array.isArray(cfg.queue) ? cfg.queue.length : 0;
}

async function save() {
  let baseUrl = $("baseUrl").value.trim().replace(/\/$/, "");
  if (baseUrl && !/^https?:\/\//i.test(baseUrl)) {
    setStatus("Base URL must start with http(s)://", false);
    return;
  }
  await chrome.storage.local.set({ baseUrl, apiKey: $("apiKey").value.trim() });
  setStatus("Saved ✓");
  chrome.runtime.sendMessage({ type: "flushNow" }, () => void chrome.runtime.lastError);
}

function test() {
  setStatus("Sending…");
  chrome.runtime.sendMessage({ type: "sendTest" }, (r) => {
    if (chrome.runtime.lastError) { setStatus(chrome.runtime.lastError.message, false); return; }
    if (r && r.ok) setStatus("Test draft line delivered ✓");
    else setStatus("Failed: " + ((r && r.error) || "unknown"), false);
  });
}

$("save").addEventListener("click", save);
$("test").addEventListener("click", test);
load();
