#!/usr/bin/env python3
"""Assemble a self-contained distribution for one operating system.

Usage:
    python package.py --platform linux-amd64 --native-host dist/native_host
                      [--version 1.2.3] [--key extension.pem] [--crx path.crx]
                      [--out release]

Produces <out>/m3u8-downloader-<version>-<platform>.zip (Windows) or .tar.gz
(macOS/Linux) containing:

    m3u8-downloader/
        extension/              unpacked extension for "Load unpacked"
        native_host[.exe]       self-contained native messaging host
        install.sh | install.bat
        extension_id.txt        only when --key is given
        m3u8-dl-chrome-plugin.crx  only when --crx is given
        README.md, INSTALL.md, LICENSE

With --key, the public key is embedded in the unpacked manifest so the
extension ID is the same on every machine (and equal to the .crx ID); the
installer then registers the native host without asking for the ID.
The private key itself is never copied into the package.
"""

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from build import EXTENSION_FILES

ROOT = Path(__file__).parent
DIST_NAME = "m3u8-downloader"
DOC_FILES = ["README.md", "INSTALL.md", "LICENSE"]
EXECUTABLES = {"native_host", "install.sh"}


def public_key_der(key_file):
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return subprocess.run(
            ["openssl", "pkey", "-in", str(key_file), "-pubout", "-outform", "DER"],
            check=True, capture_output=True,
        ).stdout
    key = serialization.load_pem_private_key(key_file.read_bytes(), None, default_backend())
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def extension_id(der):
    # Chrome: first 128 bits of SHA-256(public key), hex digits mapped 0-f -> a-p.
    return "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(der).hexdigest()[:32])


def copy_extension(dest, version, key_b64):
    dest.mkdir(parents=True)
    for name in EXTENSION_FILES:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, dest / name)
        else:
            shutil.copy2(src, dest / name)
    manifest_path = dest / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if version:
        manifest["version"] = version
    if key_b64:
        manifest["key"] = key_b64
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def make_archive(src_dir, out_base, windows):
    if windows:
        archive = out_base.with_suffix(".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(src_dir.rglob("*")):
                zf.write(path, path.relative_to(src_dir.parent))
    else:
        archive = out_base.with_name(out_base.name + ".tar.gz")

        def normalize(info):
            executable = info.isdir() or Path(info.name).name in EXECUTABLES
            info.mode = 0o755 if executable else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            return info

        with tarfile.open(archive, "w:gz") as tf:
            tf.add(src_dir, arcname=src_dir.name, filter=normalize)
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform", required=True, help="e.g. windows-amd64, macos-arm64, linux-amd64")
    parser.add_argument("--native-host", required=True, type=Path, help="PyInstaller-built native host binary")
    parser.add_argument("--version", default="", help="numeric version for manifest.json (e.g. 1.2.3)")
    parser.add_argument("--label", default="", help="version label used in the archive name")
    parser.add_argument("--key", type=Path, help="extension.pem — pins the extension ID")
    parser.add_argument("--crx", type=Path, help="signed .crx to include")
    parser.add_argument("--out", type=Path, default=ROOT / "release")
    args = parser.parse_args()

    windows = args.platform.startswith("windows")
    if not args.native_host.is_file():
        sys.exit(f"ERROR: native host binary not found: {args.native_host}")

    key_b64 = ext_id = None
    if args.key and args.key.is_file():
        der = public_key_der(args.key)
        key_b64 = base64.b64encode(der).decode("ascii")
        ext_id = extension_id(der)
        print(f"Extension ID : {ext_id}")
    else:
        print("Extension ID : not pinned (no key) — installer will ask for it")

    args.out.mkdir(parents=True, exist_ok=True)
    label = args.label or args.version or "dev"
    with tempfile.TemporaryDirectory(prefix="pkg-") as tmp:
        root = Path(tmp) / DIST_NAME
        copy_extension(root / "extension", args.version, key_b64)

        host_name = "native_host.exe" if windows else "native_host"
        shutil.copy2(args.native_host, root / host_name)
        installer = "install.bat" if windows else "install.sh"
        shutil.copy2(ROOT / installer, root / installer)
        if not windows:
            for name in (host_name, installer):
                (root / name).chmod(0o755)

        for name in DOC_FILES:
            if (ROOT / name).is_file():
                shutil.copy2(ROOT / name, root / name)
        if ext_id:
            (root / "extension_id.txt").write_text(ext_id + "\n", encoding="ascii")
        if args.crx and args.crx.is_file():
            shutil.copy2(args.crx, root / args.crx.name)

        archive = make_archive(root, args.out / f"{DIST_NAME}-{label}-{args.platform}", windows)
    print(f"Output       : {archive} ({archive.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
