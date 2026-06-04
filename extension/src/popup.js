/* Popup — Start/Pause the capture session, pick the active draft, view
 * recently added items, and flush anything pending. */

const $ = (id) => document.getElementById(id);

function timeAgo(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return Math.floor(s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function send(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (r) => {
      void chrome.runtime.lastError;
      resolve(r);
    });
  });
}

async function render() {
  const st = (await send({ type: "getState" })) || {};
  const { recent } = await chrome.storage.local.get(["recent"]);

  // Start / Pause
  const btn = $("start");
  if (st.capturing) {
    btn.textContent = "⏸ Pause capturing";
    btn.className = "start on";
    const since = st.startedAt ? " · started " + timeAgo(new Date(st.startedAt).toISOString()) : "";
    $("session").textContent = "Capturing is on" + since;
  } else {
    btn.textContent = "▶ Start capturing";
    btn.className = "start off";
    $("session").textContent = "Paused — nothing is captured";
  }

  // Draft selector
  const sel = $("draft");
  const drafts = (st.drafts && st.drafts.length) ? st.drafts : [{ id: "default", name: "My Draft" }];
  const currentId = (st.currentDraft && st.currentDraft.id) || drafts[0].id;
  sel.innerHTML = "";
  drafts.forEach((d) => {
    const o = document.createElement("option");
    o.value = d.id;
    o.textContent = d.name;
    if (d.id === currentId) o.selected = true;
    sel.appendChild(o);
  });

  // Queue + endpoint
  $("queue").textContent = st.queueLen || 0;
  $("endpoint").textContent = st.baseUrl ? new URL(st.baseUrl).host : "no platform set";

  // Recently added
  const ul = $("recent");
  const list = Array.isArray(recent) ? recent : [];
  if (!list.length) {
    ul.innerHTML = '<li class="empty">Nothing yet this session.</li>';
  } else {
    ul.innerHTML = "";
    list.forEach((r) => {
      const li = document.createElement("li");
      const what =
        r.site === "amazon" && r.asin ? "ASIN " + r.asin : r.title ? String(r.title).slice(0, 48) : r.host;
      li.innerHTML = '<span class="t"></span><div class="m"></div>';
      li.querySelector(".t").textContent = what;
      li.querySelector(".m").textContent =
        `${r.source === "auto" ? "auto" : "added"} · qty ${r.qty || 1} · ${r.draft || ""} · ${timeAgo(r.t)}`;
      ul.appendChild(li);
    });
  }
}

$("start").addEventListener("click", async () => {
  const st = (await send({ type: "getState" })) || {};
  await send({ type: "setCapturing", on: !st.capturing });
  render();
});

$("draft").addEventListener("change", async (e) => {
  const id = e.target.value;
  const opt = e.target.selectedOptions[0];
  await send({ type: "setDraft", draft: { id, name: opt.textContent } });
});

$("flush").addEventListener("click", async () => {
  await send({ type: "flushNow" });
  setTimeout(render, 400);
});

$("opts").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

// Try to refresh the draft list from the platform, then render.
send({ type: "refreshDrafts" }).finally(render);
