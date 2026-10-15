#!/usr/bin/env python3
"""Smoke-test a built native host: speak the Chrome native messaging
protocol to it and check that the 'check' action answers.

Usage: python smoke_test.py <path-to-native_host>
"""
import json
import struct
import subprocess
import sys


def call(host, msg):
    data = json.dumps(msg).encode("utf-8")
    proc = subprocess.run(
        [host, "chrome-extension://smoketest/"],
        input=struct.pack("<I", len(data)) + data,
        capture_output=True,
        timeout=120,
    )
    if len(proc.stdout) < 4:
        sys.exit(f"No response (exit {proc.returncode}): {proc.stderr.decode(errors='replace')}")
    length = struct.unpack("<I", proc.stdout[:4])[0]
    return json.loads(proc.stdout[4:4 + length])


def main():
    host = sys.argv[1]
    resp = call(host, {"action": "check"})
    print(json.dumps(resp, indent=2))
    if not resp.get("ok") or not resp.get("hostVersion"):
        sys.exit("check action failed")
    if resp.get("tempDirError"):
        sys.exit("temp dir not writable: " + resp["tempDirError"])

    resp = call(host, {"action": "nope"})
    if resp.get("ok") is not False:
        sys.exit("unknown action should fail")
    print("Smoke test passed")


if __name__ == "__main__":
    main()
