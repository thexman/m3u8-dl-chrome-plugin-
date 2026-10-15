#!/usr/bin/env python3
"""Pack the Chrome extension into a signed .crx file.

Usage:
    python build.py

On the first run Chrome generates extension.pem next to this script.
Keep that file — it encodes the extension's identity and is required
to publish updates that share the same extension ID.

Set the CHROME_PATH environment variable to override Chrome detection.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
DIST_DIR = ROOT / "dist"
KEY_FILE = ROOT / "extension.pem"

# Only these files go into the .crx — no native host, no build artefacts.
EXTENSION_FILES = [
    "manifest.json",
    "common.js",
    "background.js",
    "popup.html",
    "popup.js",
    "settings.html",
    "settings.js",
    "icons",
]

CHROME_CANDIDATES = {
    "Windows": [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ],
    "Darwin": [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ],
    "Linux": [
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium-browser"),
        Path("/usr/bin/chromium"),
    ],
}


def find_chrome() -> str | None:
    if "CHROME_PATH" in os.environ:
        return os.environ["CHROME_PATH"]
    for path in CHROME_CANDIDATES.get(platform.system(), []):
        if path.is_file():
            return str(path)
    return shutil.which("google-chrome") or shutil.which("chromium")


def main() -> None:
    chrome = find_chrome()
    if not chrome:
        print(
            "ERROR: Chrome not found.\n"
            "  Install Google Chrome or set the CHROME_PATH environment variable."
        )
        sys.exit(1)

    print(f"Chrome : {chrome}")
    print(f"Source : {ROOT}")

    DIST_DIR.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="crx-build-") as tmp:
        build_dir = Path(tmp) / "extension"
        build_dir.mkdir()

        for name in EXTENSION_FILES:
            src = ROOT / name
            if src.is_dir():
                shutil.copytree(src, build_dir / name)
            elif src.is_file():
                shutil.copy2(src, build_dir / name)
            else:
                print(f"WARNING: {name} not found — skipping")

        cmd = [
            chrome,
            "--pack-extension=" + str(build_dir),
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",  # required on Ubuntu 24.04+ (AppArmor blocks sandbox in CI)
        ]
        if KEY_FILE.is_file():
            cmd.append("--pack-extension-key=" + str(KEY_FILE))
            print(f"Key    : {KEY_FILE} (existing)")
        else:
            print("Key    : none — Chrome will generate extension.pem on first build")

        print("Packing...")
        subprocess.run(cmd, check=False)

        crx_src = Path(tmp) / "extension.crx"
        pem_src = Path(tmp) / "extension.pem"

        if not crx_src.is_file():
            print(
                "ERROR: .crx was not produced.\n"
                "  Make sure Chrome supports --pack-extension (it must not be the Snap/Flatpak build)."
            )
            sys.exit(1)

        out_crx = DIST_DIR / "m3u8-dl-chrome-plugin.crx"
        shutil.copy2(crx_src, out_crx)
        size_kb = out_crx.stat().st_size // 1024
        print(f"Output : {out_crx} ({size_kb} KB)")

        if pem_src.is_file() and not KEY_FILE.is_file():
            shutil.copy2(pem_src, KEY_FILE)
            print(f"\nKey saved to {KEY_FILE}")
            print("IMPORTANT: back up extension.pem — it determines the extension ID.")
            print("           Add it to .gitignore if this repo is public.")
            print()
            print("To enable CI builds, add its contents as a GitHub Actions secret:")
            print("  1. Copy the file contents:")
            if platform.system() == "Windows":
                print(f"       type \"{KEY_FILE}\" | clip")
            else:
                print(f"       cat \"{KEY_FILE}\" | pbcopy   # macOS")
                print(f"       cat \"{KEY_FILE}\" | xclip    # Linux")
            print("  2. Go to your repo → Settings → Secrets → Actions → New secret")
            print("     Name: EXTENSION_PEM")


if __name__ == "__main__":
    main()
