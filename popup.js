const POLL_INTERVAL_MS = 1000;

let entries = [];
let jobs = {};                   // entryId -> { jobId, tempRoot, startedAt, final?, logPath? }
const rows = new Map();          // entryId -> DOM refs
const statusCache = new Map();   // entryId -> last status object (or { localError })
const active = new Set();        // entryIds whose job is still running

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function sourceLabel(url) {
  try {
    const u = new URL(url);
    const file = u.pathname.split("/").filter(Boolean).pop() || "";
    return u.hostname + (file ? " · " + decodeURIComponent(file) : "");
  } catch (_) {
    return url;
  }
}

function entryTitle(entry) {
  if (entry.title) return entry.title;
  try { return new URL(entry.url).hostname; } catch (_) { return "Untitled"; }
}

async function saveJobs() {
  await chrome.storage.local.set({ [JOBS_KEY]: jobs });
}

async function removeEntries(ids) {
  const set = new Set(ids);
  const data = await chrome.storage.local.get(ENTRIES_KEY);
  const list = (data[ENTRIES_KEY] || []).filter((e) => !set.has(e.id));
  for (const id of ids) {
    delete jobs[id];
    statusCache.delete(id);
  }
  await chrome.storage.local.set({ [ENTRIES_KEY]: list, [JOBS_KEY]: jobs });
}

// ── Row rendering ───────────────────────────────────────────────────────────

function buildRow(entry) {
  const li = el("li");

  const actionBtn = el("button", "icon-btn run-btn", "▶");
  actionBtn.title = "Download";

  const info = el("div", "entry-info");
  const title = el("span", "title", entryTitle(entry));
  title.title = entry.pageUrl || entry.title || "";
  const source = el("span", "source", sourceLabel(entry.url));
  source.title = entry.url + "\n(click to copy)";
  source.addEventListener("click", async () => {
    await navigator.clipboard.writeText(entry.url);
    source.textContent = "URL copied ✓";
    setTimeout(() => { source.textContent = sourceLabel(entry.url); }, 1200);
  });
  const date = el("span", "date", new Date(entry.timestamp).toLocaleString());

  const progress = el("div", "progress");
  progress.hidden = true;
  const fill = el("div", "progress-fill");
  progress.appendChild(fill);

  const statusRow = el("div", "status-row");
  const status = el("span", "status");
  const stats = el("span", "stats");
  statusRow.append(status, stats);

  const result = el("div", "result");

  info.append(title, source, date, progress, statusRow, result);

  const removeBtn = el("button", "icon-btn remove-btn", "✕");
  removeBtn.title = "Remove from list";

  li.append(actionBtn, info, removeBtn);

  const refs = { li, actionBtn, progress, fill, status, stats, result };

  actionBtn.addEventListener("click", () => {
    if (active.has(entry.id)) stopJob(entry.id);
    else startJob(entry);
  });
  removeBtn.addEventListener("click", () => removeEntry(entry.id));

  return refs;
}

function setActionButton(refs, running) {
  refs.actionBtn.disabled = false;
  if (running) {
    refs.actionBtn.textContent = "⏹";
    refs.actionBtn.title = "Stop download";
    refs.actionBtn.className = "icon-btn stop-btn";
  } else {
    refs.actionBtn.textContent = "▶";
    refs.actionBtn.title = "Download";
    refs.actionBtn.className = "icon-btn run-btn";
  }
}

function setProgress(refs, mode, pct) {
  refs.progress.hidden = mode == null;
  refs.progress.className = "progress" + (mode ? " " + mode : "");
  if (pct != null) refs.fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
}

function setStatus(refs, text, kind, stats) {
  refs.status.textContent = text || "";
  refs.status.className = "status" + (kind ? " " + kind : "");
  refs.stats.textContent = stats || "";
}

function nativeButton(label, className, title, msg) {
  const btn = el("button", className, label);
  btn.title = title;
  btn.addEventListener("click", async () => {
    const resp = await nativeMessage(msg);
    if (!resp.ok) btn.textContent = resp.error || "Failed";
  });
  return btn;
}

function downloadStats(st) {
  const parts = [];
  if (st.segmentsTotal) parts.push(`${st.segmentsDone}/${st.segmentsTotal}`);
  parts.push(formatBytes(st.bytes));
  if (st.speed > 0) parts.push(formatBytes(st.speed) + "/s");
  const fetched = st.segmentsDone - (st.segmentsResumed || 0);
  if (st.speed > 0 && fetched > 0 && st.segmentsTotal) {
    const remaining = (st.bytes / fetched) * (st.segmentsTotal - st.segmentsDone);
    const eta = formatDuration(remaining / st.speed);
    if (eta) parts.push("ETA " + eta);
  }
  return parts.join(" · ");
}

function updateRow(id) {
  const refs = rows.get(id);
  if (!refs) return;
  const st = statusCache.get(id);
  const job = jobs[id];
  refs.result.innerHTML = "";

  if (!st) {
    setActionButton(refs, false);
    setProgress(refs, null);
    setStatus(refs, "");
    return;
  }
  if (st.localError) {
    setActionButton(refs, false);
    setProgress(refs, null);
    setStatus(refs, st.localError, "err");
    return;
  }

  const running = !TERMINAL_PHASES.has(st.phase);
  setActionButton(refs, running);
  if (st.cancelRequested && running) {
    refs.actionBtn.disabled = true;
  }

  switch (st.phase) {
    case "starting":
      setProgress(refs, "indeterminate");
      setStatus(refs, "Starting…");
      break;
    case "resolving":
      setProgress(refs, "indeterminate");
      setStatus(refs, "Reading playlist…", null, st.variant || "");
      break;
    case "downloading": {
      const pct = st.segmentsTotal ? (st.segmentsDone / st.segmentsTotal) * 100 : 0;
      setProgress(refs, "", pct);
      setStatus(refs, `Downloading ${Math.floor(pct)}%`, null, downloadStats(st));
      break;
    }
    case "merging":
      if (st.mergePercent == null) {
        setProgress(refs, "merging indeterminate");
        setStatus(refs, "Merging with ffmpeg…");
      } else {
        setProgress(refs, "merging", st.mergePercent);
        setStatus(refs, `Merging with ffmpeg ${Math.floor(st.mergePercent)}%`);
      }
      break;
    case "finalizing":
      setProgress(refs, "merging indeterminate");
      setStatus(refs, "Moving file to download directory…");
      break;
    case "done": {
      setProgress(refs, "done", 100);
      setStatus(refs, "Done ✓", "ok", st.outputSize ? formatBytes(st.outputSize) : "");
      if (st.outputPath) {
        const name = st.outputPath.split(/[\\/]/).pop();
        refs.result.append(
          nativeButton(name, "file-btn", "Open " + st.outputPath, { action: "openFile", path: st.outputPath }),
          nativeButton("📂", "folder-btn", "Show in folder", { action: "openFolder", path: st.outputPath })
        );
      }
      break;
    }
    case "cancelled":
      setProgress(refs, null);
      setStatus(refs, "Stopped");
      break;
    case "error":
    default:
      setProgress(refs, "error", 100);
      setStatus(refs, st.error || "Download failed", "err");
      if (job && job.logPath) {
        refs.result.append(
          nativeButton("Open log", "log-btn", job.logPath, { action: "openFile", path: job.logPath })
        );
      }
      break;
  }
}

async function render() {
  const data = await chrome.storage.local.get([ENTRIES_KEY, JOBS_KEY]);
  entries = data[ENTRIES_KEY] || [];
  jobs = data[JOBS_KEY] || {};

  const ul = document.getElementById("list");
  ul.innerHTML = "";
  rows.clear();

  if (entries.length === 0) {
    ul.appendChild(el("li", "empty", "No .m3u8 playlists detected yet. Play a video on a page that uses HLS."));
    return;
  }

  for (const entry of entries) {
    const refs = buildRow(entry);
    rows.set(entry.id, refs);
    ul.appendChild(refs.li);
    const job = jobs[entry.id];
    if (!statusCache.has(entry.id) && job && job.final) statusCache.set(entry.id, job.final);
    updateRow(entry.id);
  }
}

// ── Job control ─────────────────────────────────────────────────────────────

async function refreshJob(id) {
  const job = jobs[id];
  if (!job) {
    active.delete(id);
    return;
  }
  const resp = await nativeMessage({ action: "status", jobId: job.jobId, tempRoot: job.tempRoot });
  if (!jobs[id]) return; // removed meanwhile
  if (!resp.ok) {
    active.delete(id);
    statusCache.set(id, { localError: describeHostError(resp) });
    updateRow(id);
    return;
  }
  if (resp.logPath && job.logPath !== resp.logPath) {
    job.logPath = resp.logPath;
    await saveJobs();
  }
  const st = resp.status;
  if (!st) {
    // Status file is gone (temp dir cleaned up) — forget the job.
    active.delete(id);
    delete jobs[id];
    statusCache.delete(id);
    await saveJobs();
    updateRow(id);
    return;
  }
  const prev = statusCache.get(id);
  if (prev && prev.cancelRequested && !TERMINAL_PHASES.has(st.phase)) st.cancelRequested = true;
  statusCache.set(id, st);
  if (TERMINAL_PHASES.has(st.phase)) {
    active.delete(id);
    job.final = st;
    await saveJobs();
  } else {
    active.add(id);
  }
  updateRow(id);
}

async function startJob(entry) {
  const refs = rows.get(entry.id);
  if (refs) refs.actionBtn.disabled = true;
  statusCache.set(entry.id, { phase: "starting" });
  updateRow(entry.id);

  const settings = await getSettings();
  const resp = await nativeMessage({
    action: "start",
    jobId: entry.id,
    url: entry.url,
    headers: entry.headers || {},
    title: entry.title || "",
    settings,
  });

  if (!resp.ok) {
    statusCache.set(entry.id, { localError: describeHostError(resp) });
    updateRow(entry.id);
    return;
  }
  jobs[entry.id] = { jobId: resp.jobId, tempRoot: resp.tempRoot, startedAt: Date.now() };
  await saveJobs();
  active.add(entry.id);
  updateRow(entry.id);
}

async function stopJob(id) {
  const job = jobs[id];
  if (!job) return;
  const st = statusCache.get(id);
  if (st) st.cancelRequested = true;
  const refs = rows.get(id);
  if (refs) {
    refs.actionBtn.disabled = true;
    setStatus(refs, "Stopping…");
  }
  const resp = await nativeMessage({ action: "cancel", jobId: job.jobId, tempRoot: job.tempRoot });
  if (!resp.ok && refs) {
    setStatus(refs, describeHostError(resp), "err");
    refs.actionBtn.disabled = false;
  }
}

async function cleanupJob(id) {
  const job = jobs[id];
  if (!job) return;
  if (active.has(id)) {
    await nativeMessage({ action: "cancel", jobId: job.jobId, tempRoot: job.tempRoot });
  } else {
    await nativeMessage({ action: "cleanup", jobId: job.jobId, tempRoot: job.tempRoot });
  }
}

async function removeEntry(id) {
  await cleanupJob(id);
  active.delete(id);
  await removeEntries([id]);
}

document.getElementById("clearAll").addEventListener("click", async () => {
  const ids = entries.map((e) => e.id).filter((id) => !active.has(id));
  await Promise.all(ids.map(cleanupJob));
  await removeEntries(ids);
});

document.getElementById("openSettings").addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes[ENTRIES_KEY]) render();
});

async function pollLoop() {
  for (;;) {
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    if (active.size) await Promise.all([...active].map(refreshJob));
  }
}

(async () => {
  await render();
  // Query every job that has not finished yet (the popup may have been
  // closed while it was running).
  const pending = Object.keys(jobs).filter((id) => !jobs[id].final && rows.has(id));
  await Promise.all(pending.map(refreshJob));
  pollLoop();
})();
