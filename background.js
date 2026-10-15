importScripts("common.js");

const MAX_ENTRIES = 300;
const MAX_PENDING = 500;

// Request headers worth replaying from the native host. Cookie/Authorization
// are only sent back to the playlist's own host (see native_host.py).
const FORWARD_HEADERS = new Set(["cookie", "referer", "origin", "user-agent", "authorization"]);

const HLS_CONTENT_TYPES = [
  "application/vnd.apple.mpegurl",
  "application/x-mpegurl",
  "audio/mpegurl",
  "audio/x-mpegurl",
];

// requestId -> captured headers, kept until the response arrives so that
// playlists served without a .m3u8 extension can be detected by Content-Type.
const pendingHeaders = new Map();

function isHttpUrl(url) {
  return /^https?:\/\//i.test(url);
}

function isM3u8Url(url) {
  try {
    const parsed = new URL(url);
    return isHttpUrl(url) && parsed.pathname.toLowerCase().endsWith(".m3u8");
  } catch (_) {
    return false;
  }
}

function isFromExtension(details) {
  return !!details.initiator && details.initiator.startsWith("chrome-extension://");
}

function pickHeaders(requestHeaders) {
  const out = {};
  for (const h of requestHeaders || []) {
    const name = h.name.toLowerCase();
    if (FORWARD_HEADERS.has(name) && h.value) out[name] = h.value;
  }
  return out;
}

// Serialise read-modify-write cycles on the entry list.
let queue = Promise.resolve();
function serialize(fn) {
  const run = queue.then(fn);
  queue = run.catch(() => {});
  return run;
}

async function tabInfo(tabId) {
  if (tabId == null || tabId < 0) return {};
  try {
    const tab = await chrome.tabs.get(tabId);
    return { title: tab.title || "", pageUrl: tab.url || "" };
  } catch (_) {
    return {};
  }
}

function addEntry(details, headers) {
  return serialize(async () => {
    const data = await chrome.storage.local.get(ENTRIES_KEY);
    const list = data[ENTRIES_KEY] || [];
    if (list.some((e) => e.url === details.url)) return;

    const { title, pageUrl } = await tabInfo(details.tabId);
    list.unshift({
      id: crypto.randomUUID(),
      url: details.url,
      title: title || "",
      pageUrl: pageUrl || details.initiator || "",
      headers,
      tabId: details.tabId,
      timestamp: Date.now(),
    });
    if (list.length > MAX_ENTRIES) list.length = MAX_ENTRIES;
    await chrome.storage.local.set({ [ENTRIES_KEY]: list });
  });
}

function updateBadge(count) {
  chrome.action.setBadgeText({ text: count ? String(count) : "" });
  chrome.action.setBadgeBackgroundColor({ color: "#1ab788" });
}

chrome.webRequest.onSendHeaders.addListener(
  (details) => {
    if (!isHttpUrl(details.url) || isFromExtension(details)) return;
    const headers = pickHeaders(details.requestHeaders);
    if (isM3u8Url(details.url)) {
      addEntry(details, headers);
      return;
    }
    if (!["xmlhttprequest", "media", "other"].includes(details.type)) return;
    if (pendingHeaders.size >= MAX_PENDING) {
      pendingHeaders.delete(pendingHeaders.keys().next().value);
    }
    pendingHeaders.set(details.requestId, headers);
  },
  { urls: ["<all_urls>"] },
  ["requestHeaders", "extraHeaders"]
);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    const headers = pendingHeaders.get(details.requestId);
    pendingHeaders.delete(details.requestId);
    if (headers === undefined || isM3u8Url(details.url)) return;
    const ct = (details.responseHeaders || []).find((h) => h.name.toLowerCase() === "content-type");
    const value = ct && ct.value ? ct.value.toLowerCase() : "";
    if (HLS_CONTENT_TYPES.some((t) => value.startsWith(t))) {
      addEntry(details, headers);
    }
  },
  { urls: ["<all_urls>"], types: ["xmlhttprequest", "media", "other"] },
  ["responseHeaders"]
);

chrome.webRequest.onErrorOccurred.addListener(
  (details) => pendingHeaders.delete(details.requestId),
  { urls: ["<all_urls>"], types: ["xmlhttprequest", "media", "other"] }
);

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes[ENTRIES_KEY]) {
    updateBadge((changes[ENTRIES_KEY].newValue || []).length);
  }
});

chrome.runtime.onStartup.addListener(async () => {
  const data = await chrome.storage.local.get(ENTRIES_KEY);
  updateBadge((data[ENTRIES_KEY] || []).length);
});
