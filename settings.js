const $ = (id) => document.getElementById(id);

function readForm() {
  return {
    tempDir: $("tempDir").value.trim(),
    downloadDir: $("downloadDir").value.trim(),
    ffmpegCommand: $("ffmpegCommand").value.trim(),
    outputExtension: $("outputExtension").value.trim().replace(/^\./, ""),
    quality: $("quality").value,
    concurrency: parseInt($("concurrency").value, 10),
    keepTemp: $("keepTemp").checked,
  };
}

function validate(s) {
  if (!s.ffmpegCommand.includes("{input}") || !s.ffmpegCommand.includes("{output}")) {
    return "The ffmpeg command must contain both {input} and {output}.";
  }
  if (!/^[A-Za-z0-9]{1,8}$/.test(s.outputExtension)) {
    return "Output extension must be 1–8 letters or digits (e.g. mp4, mkv, ts).";
  }
  if (!Number.isInteger(s.concurrency) || s.concurrency < 1 || s.concurrency > 16) {
    return "Parallel downloads must be between 1 and 16.";
  }
  return null;
}

function showStatus(text, kind) {
  const el = $("saveStatus");
  el.textContent = text;
  el.className = "status-msg " + (kind || "");
}

async function load() {
  const s = await getSettings();
  $("tempDir").value = s.tempDir;
  $("downloadDir").value = s.downloadDir;
  $("ffmpegCommand").value = s.ffmpegCommand;
  $("outputExtension").value = s.outputExtension;
  $("quality").value = s.quality;
  $("concurrency").value = s.concurrency;
  $("keepTemp").checked = !!s.keepTemp;
  runCheck(false);
}

async function runCheck(verbose) {
  const s = readForm();
  const info = $("info");
  if (verbose) info.textContent = "Checking…";
  const resp = await nativeMessage({
    action: "check",
    ffmpegCommand: s.ffmpegCommand || DEFAULT_FFMPEG_COMMAND,
    tempDir: s.tempDir,
    downloadDir: s.downloadDir,
  });

  if (!resp.ok) {
    info.innerHTML = "";
    const line = document.createElement("span");
    line.className = "err";
    line.textContent = "Native host: " + describeHostError(resp);
    info.appendChild(line);
    return;
  }

  $("tempDir").placeholder = resp.defaultTempDir || "";
  $("downloadDir").placeholder = resp.defaultDownloadDir || "";
  if (!verbose) return;

  const lines = [
    ["ok", `Native host ${resp.hostVersion} · Python ${resp.python} · ${resp.platform}`],
    resp.ffmpegError
      ? ["err", `ffmpeg: ${resp.ffmpegError}`]
      : ["ok", `ffmpeg: ${resp.ffmpegVersion} (${resp.ffmpegPath})`],
    resp.tempDirError
      ? ["err", `Temporary directory: ${resp.tempDirError}`]
      : ["ok", `Temporary directory: ${resp.tempDir}`],
    resp.downloadDirError
      ? ["err", `Download directory: ${resp.downloadDirError}`]
      : ["ok", `Download directory: ${resp.downloadDir}`],
  ];
  info.innerHTML = "";
  for (const [kind, text] of lines) {
    const line = document.createElement("div");
    line.className = kind;
    line.textContent = (kind === "ok" ? "✓ " : "✗ ") + text;
    info.appendChild(line);
  }
}

$("save").addEventListener("click", async () => {
  const s = readForm();
  const error = validate(s);
  if (error) {
    showStatus(error, "err");
    return;
  }
  await saveSettings(s);
  showStatus("Saved ✓", "ok");
  setTimeout(() => showStatus(""), 2000);
});

$("test").addEventListener("click", () => runCheck(true));

$("resetCommand").addEventListener("click", () => {
  $("ffmpegCommand").value = DEFAULT_FFMPEG_COMMAND;
});

load();
