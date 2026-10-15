#!/usr/bin/env python3
"""Native messaging host for the M3U8 Downloader Chrome extension.

Runs in two modes:

  native_host.py [chrome-extension://<id>/]   Chrome mode: handle exactly one
                                              native message and exit.
  native_host.py --worker <job.json>          Worker mode: download every
                                              segment of a playlist, merge them
                                              with ffmpeg and move the result
                                              into the download directory.

Chrome's sendNativeMessage() starts a fresh host process per message, so the
"start" action spawns a detached worker and returns immediately.  The worker
reports progress through <tempRoot>/<jobId>.json, which the popup polls with
the "status" action.  Cancellation is signalled through <tempRoot>/<jobId>.cancel.

Only the Python standard library is used (certifi is picked up if present).
"""
import http.client
import json
import os
import platform
import re
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

HOST_VERSION = "1.0.0"

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"

if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent

LOG_FILE = SCRIPT_DIR / "native_host_py.log"
LOG_MAX_BYTES = 1024 * 1024

DEFAULT_FFMPEG_COMMAND = (
    'ffmpeg -hide_banner -y -allowed_extensions ALL '
    '-protocol_whitelist file,crypto,data -i "{input}" -c copy "{output}"'
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

TERMINAL_PHASES = {"done", "error", "cancelled"}
STALE_SECONDS = 30
HEARTBEAT_SECONDS = 1.0

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Segment extensions ffmpeg's HLS demuxer accepts as-is. Anything else
# (.jpg/.png/.html disguises, no extension, ...) is renamed.
SEGMENT_EXTENSIONS = {".ts", ".m4s", ".mp4", ".m4v", ".m4a", ".aac", ".mp3", ".ac3", ".ec3"}


# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg):
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE.with_suffix(".log.1"))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


# ── Environment helpers ──────────────────────────────────────────────────────

def augment_path():
    """Chrome starts native hosts with a minimal PATH on macOS/Linux, which
    hides Homebrew/user installs of ffmpeg. Append the usual locations."""
    if IS_WINDOWS:
        return
    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
             "/snap/bin", str(Path.home() / ".local/bin"), str(Path.home() / "bin")]
    current = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(current + [p for p in extra if p not in current])


def expand_path(p):
    return Path(os.path.expandvars(os.path.expanduser(p.strip())))


def default_temp_root():
    return Path(tempfile.gettempdir()) / "m3u8-dl"


def default_download_dir():
    return Path.home() / "Downloads"


def ensure_writable(directory):
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".write_probe_{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        msg = f"Cannot write to {directory}: {e}"
        if IS_MACOS and e.errno == 1:
            msg += (" — grant Full Disk Access to Python (or native_host) in System Settings → "
                    "Privacy & Security → Full Disk Access.")
        raise RuntimeError(msg)


def sanitize_job_id(value):
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("Invalid job id")
    return value


def job_paths(temp_root, job_id):
    root = Path(temp_root)
    return {
        "status": root / f"{job_id}.json",
        "job": root / f"{job_id}.job.json",
        "cancel": root / f"{job_id}.cancel",
        "log": root / f"{job_id}.log",
        "work": root / job_id,
    }


def write_json_atomic(path, data):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    # On Windows os.replace fails while a reader has the target open.
    for _ in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def read_json(path):
    for _ in range(5):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, PermissionError):
            time.sleep(0.05)
    return None


def is_active(status):
    return (
        status is not None
        and status.get("phase") not in TERMINAL_PHASES
        and time.time() - status.get("updatedAt", 0) < STALE_SECONDS
    )


def safe_filename(name, fallback="video"):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:120].strip(" .")
    if not name:
        name = fallback
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name = "_" + name
    return name


def unique_path(directory, stem, ext):
    candidate = directory / f"{stem}.{ext}"
    n = 1
    while candidate.exists():
        candidate = directory / f"{stem} ({n}).{ext}"
        n += 1
    return candidate


def split_command(template):
    """Split a command line into arguments. Quotes group words; backslashes
    are literal so Windows paths work unchanged on every platform."""
    tokens, current, quote, started = [], [], None, False
    for ch in template:
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in "\"'":
            quote, started = ch, True
        elif ch.isspace():
            if started or current:
                tokens.append("".join(current))
                current, started = [], False
        else:
            current.append(ch)
    if quote:
        raise ValueError("Unbalanced quote in ffmpeg command")
    if started or current:
        tokens.append("".join(current))
    return tokens


def build_ffmpeg_cmd(template, input_path, output_path):
    if "{input}" not in template or "{output}" not in template:
        raise ValueError("ffmpeg command must contain {input} and {output}")
    tokens = split_command(template)
    if not tokens:
        raise ValueError("ffmpeg command is empty")
    tokens = [t.replace("{input}", input_path).replace("{output}", output_path) for t in tokens]
    exe = tokens[0]
    if "/" in exe or "\\" in exe:
        tokens[0] = str(expand_path(exe))
    else:
        tokens[0] = shutil.which(exe) or exe
    return tokens


def subprocess_flags():
    return {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}


# ── HLS parsing ──────────────────────────────────────────────────────────────

ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
URI_ATTR_RE = re.compile(r'URI="[^"]*"')


def parse_attrs(text):
    return {k: v[1:-1] if v.startswith('"') else v for k, v in ATTR_RE.findall(text)}


def tag_value(line):
    return line.split(":", 1)[1] if ":" in line else ""


def parse_byterange(spec, default_offset):
    length, _, offset = spec.strip().partition("@")
    return int(offset) if offset else default_offset, int(length)


def segment_ext(url, fmp4, fallback=None):
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in SEGMENT_EXTENSIONS:
        return ext
    return fallback or (".m4s" if fmp4 else ".ts")


def parse_master(text, base):
    variants, media = [], []
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attrs(tag_value(line))
        elif line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attrs(tag_value(line))
            if attrs.get("URI"):
                attrs["URI"] = urljoin(base, attrs["URI"])
                media.append(attrs)
        elif line.startswith("#"):
            continue
        elif pending is not None:
            pending["URI"] = urljoin(base, line)
            variants.append(pending)
            pending = None
    return variants, media


def variant_sort_key(v):
    try:
        bandwidth = int(v.get("BANDWIDTH") or v.get("AVERAGE-BANDWIDTH") or 0)
    except ValueError:
        bandwidth = 0
    m = re.match(r"(\d+)x(\d+)", v.get("RESOLUTION", ""))
    pixels = int(m.group(1)) * int(m.group(2)) if m else 0
    return bandwidth, pixels


def choose_audio(variant, media):
    group = variant.get("AUDIO")
    if not group:
        return None
    candidates = [m for m in media if m.get("TYPE") == "AUDIO" and m.get("GROUP-ID") == group]
    if not candidates:
        return None
    for m in candidates:
        if m.get("DEFAULT", "").upper() == "YES":
            return m
    return candidates[0]


class Task:
    __slots__ = ("url", "rel_path", "byterange")

    def __init__(self, url, rel_path, byterange=None):
        self.url = url
        self.rel_path = rel_path
        self.byterange = byterange


def plan_media(text, base, subdir):
    """Rewrite a media playlist so every URI points at a local file.

    Returns (local playlist lines, download tasks, total duration seconds)."""
    out, tasks = [], []
    keys, maps = {}, {}
    last_end = {}
    pending_range = None
    fmp4 = False
    has_endlist = False
    duration = 0.0
    index = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("#"):
            tag = line.split(":", 1)[0]
            if tag == "#EXT-X-KEY":
                attrs = parse_attrs(tag_value(line))
                method = attrs.get("METHOD", "NONE").upper()
                keyformat = attrs.get("KEYFORMAT", "identity").lower()
                if method != "NONE" and keyformat != "identity":
                    raise RuntimeError(
                        f"DRM-protected stream ({attrs.get('KEYFORMAT')}) cannot be downloaded")
                uri = attrs.get("URI")
                if method != "NONE" and uri and not uri.startswith("data:"):
                    key_url = urljoin(base, uri)
                    if key_url not in keys:
                        keys[key_url] = f"key{len(keys)}.key"
                        tasks.append(Task(key_url, f"{subdir}/{keys[key_url]}"))
                    line = URI_ATTR_RE.sub(f'URI="{keys[key_url]}"', line, count=1)
            elif tag == "#EXT-X-MAP":
                attrs = parse_attrs(tag_value(line))
                map_url = urljoin(base, attrs.get("URI", ""))
                rng = parse_byterange(attrs["BYTERANGE"], 0) if attrs.get("BYTERANGE") else None
                if (map_url, rng) not in maps:
                    name = f"init{len(maps)}{segment_ext(map_url, True, '.mp4')}"
                    maps[(map_url, rng)] = name
                    tasks.append(Task(map_url, f"{subdir}/{name}", rng))
                line = f'#EXT-X-MAP:URI="{maps[(map_url, rng)]}"'
                fmp4 = True
            elif tag == "#EXT-X-BYTERANGE":
                pending_range = tag_value(line)
                continue
            elif tag == "#EXTINF":
                m = re.match(r"\s*([\d.]+)", tag_value(line))
                if m:
                    duration += float(m.group(1))
            elif tag == "#EXT-X-ENDLIST":
                has_endlist = True
            elif tag in ("#EXT-X-PART", "#EXT-X-PRELOAD-HINT", "#EXT-X-RENDITION-REPORT", "#EXT-X-SKIP"):
                continue  # low-latency HLS parts reference remote URIs
            out.append(line)
            continue

        seg_url = urljoin(base, line)
        rng = None
        if pending_range:
            rng = parse_byterange(pending_range, last_end.get(seg_url, 0))
            last_end[seg_url] = rng[0] + rng[1]
            pending_range = None
        name = f"seg{index:05d}{segment_ext(seg_url, fmp4)}"
        index += 1
        tasks.append(Task(seg_url, f"{subdir}/{name}", rng))
        out.append(name)

    if index == 0:
        raise RuntimeError("Playlist contains no media segments")
    if not has_endlist:
        # Live playlist: download what is listed now; without ENDLIST ffmpeg
        # would keep reloading the local playlist forever.
        out.append("#EXT-X-ENDLIST")
    return out, tasks, duration


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Cancelled(Exception):
    pass


def make_ssl_context():
    try:
        import certifi  # bundled in PyInstaller builds; optional otherwise
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class Http:
    RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self, headers, playlist_url, stop_event, retries=5, timeout=30):
        self.captured = {k.lower(): v for k, v in (headers or {}).items()}
        self.origin_host = urlparse(playlist_url).hostname
        self.stop_event = stop_event
        self.retries = retries
        self.timeout = timeout
        self.ssl_context = make_ssl_context()

    def headers_for(self, url, byterange=None):
        h = {
            "User-Agent": self.captured.get("user-agent", DEFAULT_USER_AGENT),
            "Accept": "*/*",
        }
        if "referer" in self.captured:
            h["Referer"] = self.captured["referer"]
        if "origin" in self.captured:
            h["Origin"] = self.captured["origin"]
        # Credentials only go back to the host they were captured for.
        if urlparse(url).hostname == self.origin_host:
            if "cookie" in self.captured:
                h["Cookie"] = self.captured["cookie"]
            if "authorization" in self.captured:
                h["Authorization"] = self.captured["authorization"]
        if byterange:
            offset, length = byterange
            h["Range"] = f"bytes={offset}-{offset + length - 1}"
        return h

    def request(self, url, consume, byterange=None):
        """Open url and pass the response to consume(resp); retry transient
        failures. consume must be restartable."""
        short = url if len(url) <= 120 else url[:117] + "..."
        for attempt in range(self.retries):
            if self.stop_event.is_set():
                raise Cancelled()
            req = urllib.request.Request(url, headers=self.headers_for(url, byterange))
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as resp:
                    return consume(resp)
            except Cancelled:
                raise
            except urllib.error.HTTPError as e:
                if e.code not in self.RETRY_STATUS or attempt == self.retries - 1:
                    raise RuntimeError(f"HTTP {e.code} {e.reason} — {short}")
                err = e
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
                reason = getattr(e, "reason", None) or e
                if isinstance(reason, ssl.SSLCertVerificationError):
                    hint = (" — on macOS run 'Install Certificates.command' from your Python folder"
                            if IS_MACOS else "")
                    raise RuntimeError(f"SSL certificate error: {reason.verify_message}{hint}")
                if attempt == self.retries - 1:
                    raise RuntimeError(f"Network error: {reason} — {short}")
                err = e
            delay = min(2 ** attempt, 15)
            log(f"retry {attempt + 1} in {delay}s for {short}: {err}")
            if self.stop_event.wait(delay):
                raise Cancelled()

    def get_text(self, url):
        def consume(resp):
            data = resp.read()
            return data.decode("utf-8", errors="replace").lstrip("﻿"), resp.geturl()
        return self.request(url, consume)

    def download(self, url, dest, byterange, on_bytes):
        part = dest.with_name(dest.name + ".part")

        def consume(resp):
            written = 0
            try:
                with open(part, "wb") as f:
                    if byterange and resp.status == 200:
                        # Server ignored the Range header — slice it ourselves.
                        offset, length = byterange
                        data = resp.read()[offset:offset + length]
                        f.write(data)
                        written = len(data)
                        on_bytes(written)
                    else:
                        while True:
                            if self.stop_event.is_set():
                                raise Cancelled()
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            written += len(chunk)
                            on_bytes(len(chunk))
            except BaseException:
                on_bytes(-written)
                raise
            return written

        try:
            self.request(url, consume, byterange)
        except BaseException:
            try:
                part.unlink()
            except OSError:
                pass
            raise
        os.replace(part, dest)


# ── Worker ───────────────────────────────────────────────────────────────────

FFMPEG_TIME_RE = re.compile(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class Worker:
    def __init__(self, job_file):
        self.cfg = json.loads(Path(job_file).read_text(encoding="utf-8"))
        self.paths = job_paths(self.cfg["tempRoot"], self.cfg["jobId"])
        self.work = self.paths["work"]
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.finished = threading.Event()
        self.cancel_requested = False
        self.samples = deque(maxlen=6)
        self.http = Http(self.cfg.get("headers"), self.cfg["url"], self.stop_event)
        self.status = {
            "jobId": self.cfg["jobId"],
            "phase": "starting",
            "pid": os.getpid(),
            "startedAt": time.time(),
            "title": self.cfg.get("title", ""),
            "variant": None,
            "segmentsTotal": 0,
            "segmentsDone": 0,
            "segmentsResumed": 0,
            "bytes": 0,
            "speed": 0,
            "duration": 0,
            "mergePercent": None,
            "outputPath": None,
            "outputSize": None,
            "error": None,
        }

    @staticmethod
    def out(msg):
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

    def _write(self):
        self.status["updatedAt"] = time.time()
        write_json_atomic(self.paths["status"], self.status)

    def update(self, **fields):
        with self.lock:
            self.status.update(fields)
            self._write()

    def add_bytes(self, n):
        with self.lock:
            self.status["bytes"] += n

    def check_cancel(self):
        if self.cancel_requested:
            raise Cancelled()

    def heartbeat(self):
        while not self.finished.wait(HEARTBEAT_SECONDS):
            if self.paths["cancel"].exists() and not self.cancel_requested:
                self.out("Cancel requested")
                self.cancel_requested = True
                self.stop_event.set()
            with self.lock:
                if self.status["phase"] in TERMINAL_PHASES:
                    continue
                now = time.time()
                self.samples.append((now, self.status["bytes"]))
                t0, b0 = self.samples[0]
                self.status["speed"] = (self.status["bytes"] - b0) / (now - t0) if now > t0 else 0
                try:
                    self._write()
                except OSError as e:
                    self.out(f"status write failed: {e}")

    # Steps ------------------------------------------------------------------

    def write_playlist(self, rel_path, lines):
        path = self.work / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def prepare(self):
        text, base = self.http.get_text(self.cfg["url"])
        self.check_cancel()
        if "#EXT" not in text[:4096]:
            raise RuntimeError("Response is not an HLS playlist")

        if "#EXT-X-STREAM-INF" not in text:
            lines, tasks, duration = plan_media(text, base, "video")
            return self.write_playlist("video/index.m3u8", lines), tasks, duration

        variants, media = parse_master(text, base)
        if not variants:
            raise RuntimeError("Master playlist has no variants")
        variants.sort(key=variant_sort_key)
        variant = variants[0] if self.cfg.get("quality") == "lowest" else variants[-1]
        bandwidth = variant_sort_key(variant)[0]
        label = " · ".join(x for x in (
            variant.get("RESOLUTION"),
            f"{bandwidth // 1000} kbps" if bandwidth else None,
        ) if x)
        self.out(f"Variant: {label or variant['URI']}")
        self.update(variant=label)

        vtext, vbase = self.http.get_text(variant["URI"])
        self.check_cancel()
        vlines, tasks, duration = plan_media(vtext, vbase, "video")
        video_playlist = self.write_playlist("video/index.m3u8", vlines)

        audio = choose_audio(variant, media)
        if not audio or audio["URI"] == variant["URI"]:
            return video_playlist, tasks, duration

        self.out(f"Separate audio rendition: {audio.get('NAME') or audio['URI']}")
        atext, abase = self.http.get_text(audio["URI"])
        self.check_cancel()
        alines, atasks, _ = plan_media(atext, abase, "audio")
        self.write_playlist("audio/index.m3u8", alines)

        inf = [f"BANDWIDTH={bandwidth or 1}", 'AUDIO="aud"']
        for attr in ("CODECS", "RESOLUTION"):
            if variant.get(attr):
                inf.append(f'{attr}="{variant[attr]}"' if attr == "CODECS" else f"{attr}={variant[attr]}")
        master = [
            "#EXTM3U",
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="audio",DEFAULT=YES,AUTOSELECT=YES,URI="audio/index.m3u8"',
            "#EXT-X-STREAM-INF:" + ",".join(inf),
            "video/index.m3u8",
        ]
        return self.write_playlist("master.m3u8", master), tasks + atasks, duration

    def download_all(self, tasks):
        pending = [t for t in tasks if not (self.work / t.rel_path).exists()]
        resumed = len(tasks) - len(pending)
        if resumed:
            self.out(f"Resuming: {resumed} of {len(tasks)} files already downloaded")
        self.update(phase="downloading", segmentsTotal=len(tasks),
                    segmentsDone=resumed, segmentsResumed=resumed)

        def fetch(task):
            dest = self.work / task.rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            self.http.download(task.url, dest, task.byterange, self.add_bytes)

        concurrency = max(1, min(16, int(self.cfg.get("concurrency") or 4)))
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(fetch, t) for t in pending]
            try:
                for future in as_completed(futures):
                    future.result()
                    with self.lock:
                        self.status["segmentsDone"] += 1
            except BaseException:
                self.stop_event.set()
                for f in futures:
                    f.cancel()
                raise
        self.check_cancel()

    def merge(self, input_path, duration):
        ext = self.cfg["outputExtension"]
        output = self.work / f"output.{ext}"
        if output.exists():
            output.unlink()
        cmd = build_ffmpeg_cmd(self.cfg["ffmpegCommand"], str(input_path), str(output))
        self.out("Running: " + subprocess.list2cmdline(cmd))
        self.update(phase="merging", duration=duration, mergePercent=0 if duration else None)

        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, cwd=str(input_path.parent),
                                    **subprocess_flags())
        except OSError as e:
            raise RuntimeError(f"Cannot run ffmpeg '{cmd[0]}' ({e.strerror}) — "
                               "install it or set its full path in the settings")

        tail = deque(maxlen=40)

        def read_stderr():
            buf = b""
            while True:
                chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, "read1") else proc.stderr.read(1)
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for raw in parts:
                    handle_line(raw)
            if buf:
                handle_line(buf)

        def handle_line(raw):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                return
            m = FFMPEG_TIME_RE.search(line)
            if m and duration:
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                with self.lock:
                    self.status["mergePercent"] = min(99.9, secs / duration * 100)
            if not line.startswith(("frame=", "size=")):
                tail.append(line)
                self.out("ffmpeg: " + line)

        reader = threading.Thread(target=read_stderr, daemon=True)
        reader.start()
        while proc.poll() is None:
            if self.cancel_requested:
                proc.kill()
                proc.wait()
                raise Cancelled()
            time.sleep(0.3)
        reader.join(timeout=5)

        if proc.returncode != 0:
            last = tail[-1] if tail else "no output"
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}: {last}")
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("ffmpeg did not produce an output file")
        return output

    def move_output(self, merged):
        target_dir = Path(self.cfg["downloadDir"])
        target_dir.mkdir(parents=True, exist_ok=True)
        target = unique_path(target_dir, self.cfg["fileStem"], self.cfg["outputExtension"])
        self.out(f"Moving to {target}")
        shutil.move(str(merged), str(target))
        return target

    def remove_workdir(self):
        if not self.cfg.get("keepTemp"):
            shutil.rmtree(self.work, ignore_errors=True)

    def run(self):
        self.out(f"Job {self.cfg['jobId']} — {self.cfg['url']}")
        threading.Thread(target=self.heartbeat, daemon=True).start()
        try:
            self.work.mkdir(parents=True, exist_ok=True)
            self.update(phase="resolving")
            input_path, tasks, duration = self.prepare()
            self.download_all(tasks)
            merged = self.merge(input_path, duration)
            self.update(phase="finalizing", mergePercent=100)
            final = self.move_output(merged)
            self.remove_workdir()
            self.update(phase="done", speed=0, outputPath=str(final), outputSize=final.stat().st_size)
            self.out("Done")
        except Cancelled:
            self.stop_event.set()
            self.remove_workdir()
            self.update(phase="cancelled", speed=0)
            self.out("Cancelled")
        except Exception as e:
            self.stop_event.set()
            traceback.print_exc()
            # Downloaded segments are kept so pressing ▶ again resumes.
            self.update(phase="error", speed=0, error=str(e) or e.__class__.__name__)
        finally:
            self.finished.set()
            # The job file holds captured cookies — don't leave it lying around.
            for key in ("cancel", "job"):
                try:
                    self.paths[key].unlink()
                except OSError:
                    pass


def run_worker(job_file):
    # stdout/stderr go to the job log; don't let the console code page
    # (cp1252 on Windows) crash the worker on non-ASCII paths or titles.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    augment_path()
    Worker(job_file).run()


# ── Chrome native messaging ──────────────────────────────────────────────────

def read_message():
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    msg_len = struct.unpack("<I", raw_len)[0]
    return json.loads(sys.stdin.buffer.read(msg_len).decode("utf-8"))


def send_message(payload):
    encoded = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def spawn_worker(job_file, log_path):
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--worker", str(job_file)]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", str(job_file)]

    with open(log_path, "ab") as log_f:
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_f,
            "stderr": log_f,
            "cwd": str(job_file.parent),
            "close_fds": True,
        }
        if not IS_WINDOWS:
            return subprocess.Popen(cmd, start_new_session=True, **kwargs)
        # Try to leave Chrome's job object so the worker survives the host
        # exiting; not every job object allows breakaway.
        base = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(cmd, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            return subprocess.Popen(cmd, creationflags=base, **kwargs)


def resolve_dirs(settings):
    temp_root = expand_path(settings.get("tempDir") or "") if settings.get("tempDir") else default_temp_root()
    download_dir = (expand_path(settings.get("downloadDir") or "")
                    if settings.get("downloadDir") else default_download_dir())
    return temp_root, download_dir


def handle_start(msg):
    job_id = sanitize_job_id(msg.get("jobId"))
    url = (msg.get("url") or "").strip()
    if not re.match(r"https?://", url, re.I):
        return {"ok": False, "error": "Invalid playlist URL"}

    settings = msg.get("settings") or {}
    temp_root, download_dir = resolve_dirs(settings)
    template = (settings.get("ffmpegCommand") or DEFAULT_FFMPEG_COMMAND).strip()
    build_ffmpeg_cmd(template, "in", "out")  # validates placeholders and quoting

    ext = str(settings.get("outputExtension") or "mp4").lstrip(".")
    if not re.fullmatch(r"[A-Za-z0-9]{1,8}", ext):
        return {"ok": False, "error": f"Invalid output extension: {ext}"}

    ensure_writable(temp_root)
    ensure_writable(download_dir)

    paths = job_paths(temp_root, job_id)
    if is_active(read_json(paths["status"])):
        return {"ok": False, "error": "This download is already running"}
    try:
        paths["cancel"].unlink()
    except OSError:
        pass

    parsed = urlparse(url)
    fallback = f"{parsed.hostname or 'video'}-{datetime.now():%Y%m%d-%H%M%S}"
    cfg = {
        "jobId": job_id,
        "url": url,
        "headers": msg.get("headers") or {},
        "title": msg.get("title") or "",
        "fileStem": safe_filename(msg.get("title"), fallback),
        "tempRoot": str(temp_root),
        "downloadDir": str(download_dir),
        "ffmpegCommand": template,
        "outputExtension": ext,
        "quality": settings.get("quality") or "highest",
        "concurrency": settings.get("concurrency") or 4,
        "keepTemp": bool(settings.get("keepTemp")),
    }
    write_json_atomic(paths["job"], cfg)
    write_json_atomic(paths["status"], {"jobId": job_id, "phase": "starting", "updatedAt": time.time()})

    proc = spawn_worker(paths["job"], paths["log"])
    log(f"Started worker PID {proc.pid} for job {job_id}")
    return {"ok": True, "jobId": job_id, "tempRoot": str(temp_root), "pid": proc.pid}


def handle_status(msg):
    job_id = sanitize_job_id(msg.get("jobId"))
    paths = job_paths(msg.get("tempRoot") or default_temp_root(), job_id)
    status = read_json(paths["status"])
    if status and status.get("phase") not in TERMINAL_PHASES and not is_active(status):
        status["phase"] = "error"
        status["error"] = "The download process stopped unexpectedly — see the log"
        status["speed"] = 0
        try:
            write_json_atomic(paths["status"], status)
        except OSError:
            pass
    return {"ok": True, "status": status, "logPath": str(paths["log"])}


def handle_cancel(msg):
    job_id = sanitize_job_id(msg.get("jobId"))
    paths = job_paths(msg.get("tempRoot") or default_temp_root(), job_id)
    status = read_json(paths["status"])
    if is_active(status):
        paths["cancel"].write_text("cancel", encoding="utf-8")
    elif status and status.get("phase") not in TERMINAL_PHASES:
        status.update(phase="cancelled", speed=0, updatedAt=time.time())
        write_json_atomic(paths["status"], status)
    return {"ok": True}


def handle_cleanup(msg):
    job_id = sanitize_job_id(msg.get("jobId"))
    paths = job_paths(msg.get("tempRoot") or default_temp_root(), job_id)
    if is_active(read_json(paths["status"])):
        return {"ok": False, "error": "Download is still running"}
    for key in ("status", "job", "cancel", "log"):
        try:
            paths[key].unlink()
        except OSError:
            pass
    shutil.rmtree(paths["work"], ignore_errors=True)
    return {"ok": True}


def handle_check(msg):
    temp_root, download_dir = resolve_dirs(msg)
    result = {
        "ok": True,
        "hostVersion": HOST_VERSION,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "defaultTempDir": str(default_temp_root()),
        "defaultDownloadDir": str(default_download_dir()),
        "tempDir": str(temp_root),
        "downloadDir": str(download_dir),
    }
    for key, directory in (("tempDirError", temp_root), ("downloadDirError", download_dir)):
        try:
            ensure_writable(directory)
        except Exception as e:
            result[key] = str(e)
    try:
        cmd = build_ffmpeg_cmd(msg.get("ffmpegCommand") or DEFAULT_FFMPEG_COMMAND, "in", "out")
        result["ffmpegPath"] = cmd[0]
        proc = subprocess.run([cmd[0], "-version"], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=15, **subprocess_flags())
        first = proc.stdout.decode("utf-8", errors="replace").splitlines()
        if proc.returncode != 0 or not first:
            result["ffmpegError"] = f"'{cmd[0]} -version' failed (exit {proc.returncode})"
        else:
            result["ffmpegVersion"] = first[0]
    except OSError as e:
        result["ffmpegError"] = (f"cannot run '{result.get('ffmpegPath')}' ({e.strerror}) — "
                                 "install ffmpeg or put its full path in the command")
    except Exception as e:
        result["ffmpegError"] = str(e)
    return result


def handle_open_file(msg):
    p = Path(msg.get("path") or "")
    if not p.is_file():
        return {"ok": False, "error": "File not found"}
    if IS_WINDOWS:
        os.startfile(str(p))
    elif IS_MACOS:
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)], start_new_session=True)
    return {"ok": True}


def handle_open_folder(msg):
    p = Path(msg.get("path") or "")
    if not p.exists():
        return {"ok": False, "error": "File not found"}
    if IS_WINDOWS:
        subprocess.Popen(["explorer", "/select,", str(p)])
    elif IS_MACOS:
        subprocess.Popen(["open", "-R", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent if p.is_file() else p)], start_new_session=True)
    return {"ok": True}


HANDLERS = {
    "start": handle_start,
    "status": handle_status,
    "cancel": handle_cancel,
    "cleanup": handle_cleanup,
    "check": handle_check,
    "openFile": handle_open_file,
    "openFolder": handle_open_folder,
}


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        run_worker(sys.argv[2])
        return

    augment_path()
    try:
        msg = read_message()
    except Exception as e:
        log(f"ERROR reading message: {e}")
        send_message({"ok": False, "error": f"read error: {e}"})
        return
    if not msg:
        return

    action = msg.get("action")
    if action != "status":
        log(f"Received: {action}")
    handler = HANDLERS.get(action)
    if not handler:
        send_message({"ok": False, "error": f"Unknown action: {action}"})
        return
    try:
        send_message(handler(msg))
    except Exception as e:
        log(f"ERROR in {action}: {traceback.format_exc()}")
        send_message({"ok": False, "error": str(e)})


if __name__ == "__main__":
    main()
