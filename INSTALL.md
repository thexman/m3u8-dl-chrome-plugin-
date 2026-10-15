# Installation Guide

## Prerequisites

- Google Chrome (or Chromium) 111 or later
- **ffmpeg**
  - Windows: `winget install Gyan.FFmpeg`, or download it from <https://www.gyan.dev/ffmpeg/builds/>
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg` (or your distribution's package)
- **Python 3.8+.** Only needed for Option B; release packages include a self-contained binary.

---

## Option A — Release package (recommended)

Download the archive for your OS from the GitHub Releases page:

| OS | Archive |
|---|---|
| Windows | `m3u8-downloader-<version>-windows-amd64.zip` |
| macOS (Apple Silicon) | `m3u8-downloader-<version>-macos-arm64.tar.gz` |
| macOS (Intel) | `m3u8-downloader-<version>-macos-amd64.tar.gz` |
| Linux x86-64 | `m3u8-downloader-<version>-linux-amd64.tar.gz` |
| Linux ARM64 | `m3u8-downloader-<version>-linux-arm64.tar.gz` |

1. Extract it to a permanent folder (the native host runs from there).
2. In `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and select the `m3u8-downloader/extension` folder.
3. Run `install.bat` (Windows) or `./install.sh` (macOS / Linux) from the extracted folder. If the package contains `extension_id.txt`, the ID is filled in automatically. Otherwise paste the ID shown on the extension's card.
4. Continue with **Step 3 — Configure** below.

No Python is needed. The package contains a self-contained `native_host` binary.

> macOS may block the downloaded binary. If so, run `xattr -dr com.apple.quarantine m3u8-downloader` once in the folder that contains it.

---

## Option B — From source

### Step 1 — Load the extension

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and select this repository folder.
4. Copy the **extension ID** shown on the card (32 letters, e.g. `abcdefghijklmnopabcdefghijklmnop`).

---

### Step 2 — Register the native host

The native host is the program that downloads segments and runs ffmpeg. Chrome finds it through a small JSON manifest. The installer writes that manifest and registers it. Run from the repository folder, it uses `native_host.py` (Python 3.8+ required).

#### Windows

```bat
install.bat <EXTENSION_ID>
```

(or double-click `install.bat` and paste the ID when asked)

It writes `com.m3u8.downloader.json` next to the script and registers it under
`HKCU\Software\Google\Chrome\NativeMessagingHosts\com.m3u8.downloader`.

#### macOS / Linux

```bash
./install.sh <EXTENSION_ID>
```

It writes `com.m3u8.downloader.json` to:

| OS | Location |
|---|---|
| macOS | `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/` |
| Linux | `~/.config/google-chrome/NativeMessagingHosts/` |

If Chromium is installed, the manifest is written to its directory as well.

> If you move the folder or the extension ID changes, run the installer again.

---

## Step 3 — Configure

1. Click the extension icon → **⚙ Settings**.
2. Optionally set the **Temporary directory** and **Download directory**. The defaults are shown as placeholders.
3. Check the **ffmpeg command line**. If ffmpeg is not on the `PATH`, give its full path:
   - Windows: `"C:\ffmpeg\bin\ffmpeg.exe" -hide_banner -y -allowed_extensions ALL -protocol_whitelist file,crypto,data -i "{input}" -c copy "{output}"`
   - macOS (Homebrew): `/opt/homebrew/bin/ffmpeg ...`
4. Click **Test native host & ffmpeg**. Every line should show ✓.
5. Click **Save**.

---

## Step 4 — Use it

1. Open a page with an HLS video and start playback.
2. The badge shows how many playlists were detected.
3. Open the popup and click **▶** next to a playlist. The progress bar fills as segments download, and then while ffmpeg merges them.
4. When it finishes, click the file name to play the video, or 📂 to open its folder.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Native host not installed" | Run the installer, then reload the extension |
| "Native host rejects this extension ID" | The ID changed. Run the installer again with the new ID |
| ffmpeg ✗ in the settings test | Install ffmpeg or put its full path in the command |
| HTTP 403 / 410 | The playlist URL expired. Reload the page and use the newly detected entry |
| macOS: "Cannot write to …" | Give Python (or `native_host`) **Full Disk Access** in System Settings → Privacy & Security, or choose directories it can write to |
| macOS: SSL certificate error | Run `Install Certificates.command` from your Python folder, or use the prebuilt binary |
| Anything else | Click **Open log** on the failed entry. The host's own log is `native_host_py.log` next to `native_host.py` |

---

## Uninstall

- Windows: `reg delete "HKCU\Software\Google\Chrome\NativeMessagingHosts\com.m3u8.downloader" /f`
- macOS / Linux: delete `com.m3u8.downloader.json` from the directory listed in Step 2.

Then remove the extension in `chrome://extensions`.
