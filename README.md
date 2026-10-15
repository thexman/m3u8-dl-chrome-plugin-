# M3U8 Downloader

A Chrome extension that detects HLS playlists (`.m3u8`) loaded by web pages and downloads them into a single video file. It downloads every segment into a temporary directory, merges them with **ffmpeg**, and moves the result into your download directory. It works on Windows, macOS and Linux.

---

## How it works

1. `background.js` watches network requests. Any request whose path ends in `.m3u8`, or whose response is served as `application/vnd.apple.mpegurl` / `application/x-mpegURL`, is added to the list. The page title and the request's `Referer`, `Origin`, `User-Agent`, `Cookie` and `Authorization` headers are saved with it.
2. Click **▶** in the popup. The extension sends the URL to the native host.
3. The native host starts a separate worker process that:
   - reads the playlist. For a master playlist it picks the highest (or lowest) bandwidth variant and its separate audio rendition, if there is one;
   - downloads all segments, AES-128 keys and fMP4 init segments in parallel into `<temp dir>/<job id>/`;
   - writes local `.m3u8` playlists that point at the downloaded files;
   - runs your ffmpeg command with `{input}` set to the local playlist and `{output}` set to a file in the temp directory;
   - moves the finished file to the download directory, named after the page title, and deletes the temporary sub-directory.
4. The popup polls the worker's status file and shows a progress bar, segment count, bytes, speed and ETA. During the merge it shows ffmpeg's progress.

Downloads keep running after the popup closes. When you open it again, it picks up the current progress.

---

## Features

- **Automatic detection:** matches `.m3u8` URLs and HLS content types
- **Progress bar:** shows download progress per segment, speed and ETA, then merge progress
- **Stop:** **⏹** cancels the download or the ffmpeg merge and deletes the temporary files
- **Resume:** if a download fails (expired URL, network error), the downloaded segments are kept. Press **▶** again to continue from where it stopped.
- **Master playlists:** picks the highest or lowest bandwidth variant. Separate audio renditions are downloaded and muxed in.
- **AES-128 encryption:** keys are downloaded and ffmpeg decrypts the segments. DRM streams (FairPlay, Widevine, PlayReady) are detected and rejected.
- **fMP4 / CMAF:** supports `#EXT-X-MAP` init segments and `#EXT-X-BYTERANGE`
- **Headers:** captured cookies and auth headers are sent only to the playlist's own host
- **Finished files:** click the file name to open the video, or 📂 to show it in its folder. If a download fails, **Open log** shows the worker log.

---

## Settings

Open the popup → **⚙ Settings** (or right-click the extension icon → Options).

| Setting | Default | Description |
|---|---|---|
| Temporary directory | `<system temp>/m3u8-dl` | Each download creates its own sub-directory here |
| Download directory | `~/Downloads` | Finished files are moved here |
| ffmpeg command line | see below | `{input}` = local playlist, `{output}` = file to create |
| Output file extension | `mp4` | Extension of the merged file (`mp4`, `mkv`, `ts`, …) |
| Stream quality | Highest bandwidth | Which variant to take from a master playlist |
| Parallel segment downloads | `4` | 1–16 |
| Keep temporary files | off | Keep the segment directory after a successful merge |

Default ffmpeg command:

```
ffmpeg -hide_banner -y -allowed_extensions ALL -protocol_whitelist file,crypto,data -i "{input}" -c copy "{output}"
```

- `-protocol_whitelist file,crypto,data` lets ffmpeg decrypt AES-128 segments with the downloaded key.
- `-allowed_extensions ALL` lets the HLS demuxer open the local key and segment files.
- If ffmpeg is not on the `PATH`, give its full path, for example `"C:\ffmpeg\bin\ffmpeg.exe" -i "{input}" ...`. Backslashes are literal, and arguments containing spaces go in quotes.
- To re-encode instead of copying, replace `-c copy` with something like `-c:v libx264 -crf 20 -c:a aac`.

**Test native host & ffmpeg** in the settings page checks that the host is installed, that ffmpeg runs, and that both directories are writable.

---

## Architecture

```
Chrome extension (Manifest V3)
  common.js            shared constants / helpers
  background.js        webRequest listener, list of detected playlists, badge
  popup.html/js        list, ▶/⏹, progress bars
  settings.html/js     options page

Native messaging host (Python 3.8+, stdlib only)
  native_host.py       Chrome mode: one message → one response
                       --worker mode: download + merge job (detached process)
  native_host.bat      Windows launcher
  install.sh / .bat    register the host with Chrome
```

Chrome's `sendNativeMessage` starts a new host process for every message. The `start` action therefore launches a detached worker and returns at once. The worker and the popup communicate through files in the temporary directory:

| File | Purpose |
|---|---|
| `<jobId>.json` | Status (phase, segments, bytes, speed, merge %, output path, error). Heartbeat every second. |
| `<jobId>.log` | Worker and ffmpeg log |
| `<jobId>.cancel` | Present = stop requested |
| `<jobId>.job.json` | Job parameters. Deleted when the worker finishes because it contains captured cookies. |
| `<jobId>/` | Segments, keys and local playlists |

Native host actions: `start`, `status`, `cancel`, `cleanup`, `check`, `openFile`, `openFolder`.

---

## Installation

See [INSTALL.md](INSTALL.md).

---

## Building / CI

### Local

- `python build.py` packs the extension into `dist/m3u8-dl-chrome-plugin.crx`. The first run creates `extension.pem`. Keep that file: it determines the extension ID.
- `python package.py --platform <name> --native-host <binary> [--key extension.pem]` builds a distribution archive for one OS (see below).

### GitHub Actions

**`build.yml`** runs on pushes to `main`, on pull requests, on `v*` tags and on manual dispatch.

| Job | Runner(s) | Output |
|---|---|---|
| `version` | ubuntu | `1.2.3` for tag `v1.2.3`; otherwise `<manifest version>-dev.<sha>` |
| `crx` | ubuntu | Signed `m3u8-dl-chrome-plugin.crx`. Skipped with a warning if the `EXTENSION_PEM` secret is not set. Not run for pull requests. |
| `package` (matrix) | windows-latest, macos-latest, macos-15-intel, ubuntu-22.04, ubuntu-22.04-arm | One distribution per OS (below). Each runner builds `native_host` with PyInstaller, smoke-tests it over the native messaging protocol, then packages it. |
| `release` | ubuntu | On `v*` tags only: a GitHub Release with every distribution, the `.crx` and `SHA256SUMS.txt` |

Distributions:

| Asset | For |
|---|---|
| `m3u8-downloader-<version>-windows-amd64.zip` | Windows x64 (also runs on ARM through emulation) |
| `m3u8-downloader-<version>-macos-arm64.tar.gz` | macOS on Apple Silicon |
| `m3u8-downloader-<version>-macos-amd64.tar.gz` | macOS on Intel |
| `m3u8-downloader-<version>-linux-amd64.tar.gz` | Linux x86-64, glibc 2.35+ |
| `m3u8-downloader-<version>-linux-arm64.tar.gz` | Linux ARM64, glibc 2.35+ |

Each archive contains:

```
m3u8-downloader/
  extension/              unpacked extension → chrome://extensions → Load unpacked
  native_host[.exe]       self-contained native host (no Python needed)
  install.bat | install.sh
  extension_id.txt        present when the build had EXTENSION_PEM
  m3u8-dl-chrome-plugin.crx  present when the build had EXTENSION_PEM
  README.md  INSTALL.md  LICENSE
```

If `EXTENSION_PEM` is set, its public key is written into `extension/manifest.json` as `"key"`. The unpacked extension then has the same ID on every machine, matching the `.crx`, so the installer reads the ID from `extension_id.txt` and doesn't ask for it. The private key is never packaged.

**First-time setup for the signing key:** run `python build.py` locally once. Then add the contents of the generated `extension.pem` as a repository secret named `EXTENSION_PEM` (Settings → Secrets and variables → Actions).

**`trigger-release.yml`** creates the next `vX.Y.Z` tag (or the tag you enter) and starts `build.yml` on it.

---

## Limitations

- **Live streams:** only the segments listed when ▶ is pressed are downloaded.
- **DRM:** DRM-protected streams cannot be downloaded.
- **Expiring URLs:** many CDNs sign playlist URLs with an expiry time. If a download fails with HTTP 403/410, reload the page so a fresh URL is detected.
- **Cookies on other hosts:** cookies are only sent to the playlist's own host. Segments on a different CDN host that need cookies will fail.
