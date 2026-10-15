#!/usr/bin/env bash
# Registers the native messaging host for Google Chrome / Chromium on macOS and Linux.
# Usage: ./install.sh [EXTENSION_ID]
set -e

HOST_NAME="com.m3u8.downloader"

echo "============================================"
echo " M3U8 Downloader - Native Host Installer"
echo "============================================"
echo

EXT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Prefer a prebuilt binary (from the GitHub release) over the Python script.
if [[ -x "$EXT_DIR/native_host" && ! -d "$EXT_DIR/native_host" ]]; then
  HOST_PATH="$EXT_DIR/native_host"
else
  PYTHON="$(command -v python3 || true)"
  if [[ -z "$PYTHON" ]] || ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 8))'; then
    echo "ERROR: Python 3.8+ (python3) not found. Install it or use the prebuilt native_host binary."
    exit 1
  fi
  # Chrome starts hosts with a minimal PATH (macOS: /usr/bin:/bin:...), so pin
  # the interpreter found now instead of relying on '#!/usr/bin/env python3'.
  HOST_PATH="$EXT_DIR/native_host_launcher.sh"
  cat > "$HOST_PATH" <<EOF
#!/bin/sh
exec "$PYTHON" "$EXT_DIR/native_host.py" "\$@"
EOF
  chmod +x "$HOST_PATH" "$EXT_DIR/native_host.py"
fi

echo "Native host: $HOST_PATH"
echo

EXT_ID="$1"
# Release packages pin the extension ID (manifest "key") and ship it here.
if [[ -z "$EXT_ID" && -f "$EXT_DIR/extension_id.txt" ]]; then
  EXT_ID="$(cat "$EXT_DIR/extension_id.txt")"
  echo "Extension ID: $EXT_ID (from extension_id.txt)"
fi
if [[ -z "$EXT_ID" ]]; then
  echo "Open Chrome, go to chrome://extensions, enable Developer mode,"
  echo "find 'M3U8 Downloader' and copy its ID."
  echo
  read -rp "Paste the extension ID here: " EXT_ID
fi
EXT_ID="$(echo "$EXT_ID" | tr -d '[:space:]')"

if [[ ! "$EXT_ID" =~ ^[a-p]{32}$ ]]; then
  echo "ERROR: '$EXT_ID' does not look like a Chrome extension ID (32 letters a-p)."
  exit 1
fi

case "$(uname -s)" in
  Darwin)
    TARGET_DIRS=(
      "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
      "$HOME/Library/Application Support/Chromium/NativeMessagingHosts"
    )
    ;;
  Linux)
    TARGET_DIRS=(
      "$HOME/.config/google-chrome/NativeMessagingHosts"
      "$HOME/.config/chromium/NativeMessagingHosts"
    )
    ;;
  *)
    echo "ERROR: unsupported OS $(uname -s). On Windows run install.bat."
    exit 1
    ;;
esac

# JSON-escape the path (backslashes and quotes).
HOST_PATH_JSON="${HOST_PATH//\\/\\\\}"
HOST_PATH_JSON="${HOST_PATH_JSON//\"/\\\"}"

for DIR in "${TARGET_DIRS[@]}"; do
  # Only register Chromium if it is installed (its config dir exists).
  if [[ "$DIR" == *[Cc]hromium* && ! -d "$(dirname "$DIR")" ]]; then
    continue
  fi
  mkdir -p "$DIR"
  cat > "$DIR/$HOST_NAME.json" <<EOF
{
  "name": "$HOST_NAME",
  "description": "M3U8 Downloader native host",
  "path": "$HOST_PATH_JSON",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://$EXT_ID/"
  ]
}
EOF
  echo "Written: $DIR/$HOST_NAME.json"
done

echo
if ! command -v ffmpeg >/dev/null 2>&1 \
  && [[ ! -x /opt/homebrew/bin/ffmpeg && ! -x /usr/local/bin/ffmpeg ]]; then
  echo "WARNING: ffmpeg was not found. Install it (brew install ffmpeg / apt install ffmpeg)"
  echo "         or put its full path in the extension settings."
  echo
fi
echo "SUCCESS! Reload the extension in chrome://extensions and you're ready."
