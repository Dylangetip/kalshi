/* Popup — status at a glance, enable/disable toggle, manual flush. */

const $ = (id) => document.getElementById(id);

function timeAgo(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return Math.floor(s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function describe(r) {
  if (r.site === "amazon") {
    if (r.asin) return "Amazon · ASIN " + r.asin;
    if (r.items != null) return "Amazon cart · " + r.items + " items";
    return "Amazon";
  }
  if (r.items != null) return "Cart · " + r.items + " items";
  return "Product HTML";
}

async function render() {
  const cfg = await chrome.storage.local.get(["enabled", "endpoint", "recent", "queue"]);
  const enabled = cfg.enabled !== false;

  const state = $("state");
  state.textContent = enabled ? "Tracking on" : "Paused";
  state.className = "pill " + (enabled ? "on" : "off");
  $("toggle").textContent = enabled ? "Pause" : "Resume";

  $("endpoint").textContent = cfg.endpoint ? "→ " + new URL(cfg.endpoint).host : "no endpoint set";
  $("queue").textContent = Array.isArray(cfg.queue) ? cfg.queue.length : 0;

  const recent = Array.isArray(cfg.recent) ? cfg.recent : [];
  const ul = $("recent");
  if (!recent.length) {
    ul.innerHTML = '<li class="empty">No captures yet.</li>';
    return;
  }
  ul.innerHTML = "";
  recent.forEach((r) => {
    const li = document.createElement("li");
    const tag = r.type === "cart_snapshot" ? "cart" : r.type === "add_to_cart" ? "add" : r.type;
    li.innerHTML =
      '<span class="host"></span> <span class="meta"></span>';
    li.querySelector(".host").textContent = r.host || "";
    li.querySelector(".meta").textContent = `· ${tag} · ${describe(r)} · ${timeAgo(r.t)}`;
    ul.appendChild(li);
  });
}

$("toggle").addEventListener("click", async () => {
  const { enabled } = await chrome.storage.local.get(["enabled"]);
  await chrome.storage.local.set({ enabled: !(enabled !== false) });
  render();
});

$("flush").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "flushNow" }, () => {
    void chrome.runtime.lastError;
    setTimeout(render, 400);
  });
});

$("opts").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

render();
