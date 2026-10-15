// Shared constants and helpers — loaded by background.js (importScripts),
// popup.html and settings.html.

const HOST_NAME = "com.m3u8.downloader";
const ENTRIES_KEY = "m3u8Entries";
const JOBS_KEY = "m3u8Jobs";
const SETTINGS_KEY = "m3u8Settings";

const DEFAULT_FFMPEG_COMMAND =
  'ffmpeg -hide_banner -y -allowed_extensions ALL -protocol_whitelist file,crypto,data -i "{input}" -c copy "{output}"';

const DEFAULT_SETTINGS = {
  tempDir: "",
  downloadDir: "",
  ffmpegCommand: DEFAULT_FFMPEG_COMMAND,
  outputExtension: "mp4",
  quality: "highest",
  concurrency: 4,
  keepTemp: false,
};

const TERMINAL_PHASES = new Set(["done", "error", "cancelled"]);

async function getSettings() {
  const data = await chrome.storage.local.get(SETTINGS_KEY);
  return { ...DEFAULT_SETTINGS, ...(data[SETTINGS_KEY] || {}) };
}

async function saveSettings(settings) {
  await chrome.storage.local.set({ [SETTINGS_KEY]: settings });
}

// Promise wrapper around chrome.runtime.sendNativeMessage.
// Never rejects: transport errors come back as { ok: false, error, hostError: true }.
function nativeMessage(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(HOST_NAME, msg, (response) => {
      const err = chrome.runtime.lastError;
      if (err) {
        resolve({ ok: false, error: err.message, hostError: true });
      } else {
        resolve(response || { ok: false, error: "Empty response from native host" });
      }
    });
  });
}

function describeHostError(resp) {
  const msg = (resp && resp.error) || "Unknown error";
  if (/native messaging host not found|host not found/i.test(msg)) {
    return "Native host not installed — run install.sh / install.bat (see INSTALL.md).";
  }
  if (/Access to the specified native messaging host is forbidden/i.test(msg)) {
    return "Native host rejects this extension ID — re-run the installer with the current ID.";
  }
  return msg;
}

function formatBytes(n) {
  if (!n || n < 0) return "0 B";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KiB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MiB";
  return (n / 1073741824).toFixed(2) + " GiB";
}

function formatDuration(sec) {
  if (!isFinite(sec) || sec < 0) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
}
