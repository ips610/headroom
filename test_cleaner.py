#!/usr/bin/env python3
"""Runs the scan and the delete against a throwaway home folder. Nothing real is touched.

    python3 test_cleaner.py
"""
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import cleaner

box = os.path.realpath(tempfile.mkdtemp(prefix="headroom-test-"))
cleaner.HOME = os.path.join(box, "home")
cleaner.TMP = os.path.join(box, "tmp")
cleaner.MIN_KB, cleaner.SMALL_KB, cleaner.BUILD_MIN_KB = 8, 64, 64
outside = os.path.join(box, "outside")


def put(*parts, kb=100, root=None):
    path = os.path.join(root or cleaner.HOME, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(os.urandom(kb * 1024))
    return path


def age(path, days=2):
    then = time.time() - days * cleaner.DAY
    for folder, _, files in os.walk(path):
        for name in files:
            os.utime(os.path.join(folder, name), (then, then))
        os.utime(folder, (then, then))


def named(rows):
    return {row["name"]: row for row in rows}


# --- a small fake Mac -------------------------------------------------------
put("Library", "Caches", "com.example.big", "blob")
put("Library", "Caches", "tiny", "blob", kb=10)
put("Library", "Caches", "com.apple.thing", "blob")
put("Library", "Caches", "ms-playwright", "chromium")
kept_outside = put("precious", "file", root=outside)
os.symlink(os.path.join(outside, "precious"), os.path.join(cleaner.HOME, "Library", "Caches", "link"))
put(".cache", "lm-studio", "models", "model.gguf")
put(".cache", "uv", "wheel")
put("Library", "Application Support", "Slack", "Code Cache", "js")
put("Library", "Application Support", "Slack", "Cache", "data")
slack_data = put("Library", "Application Support", "Slack", "Local Storage", "leveldb")
put("Library", "Application Support", "Plain", "Cache", "data")       # not a Chromium app
put("Library", "Logs", "app.log")
put(".npm", "_cacache", "tarball")
npm_config = put(".npm", "config", kb=1)
put("old-dir", "file", root=cleaner.TMP)
age(os.path.join(cleaner.TMP, "old-dir"))
put("new-dir", "file", root=cleaner.TMP)
put("old-sock-dir", "file", root=cleaner.TMP)
here = os.getcwd()
os.chdir(os.path.join(cleaner.TMP, "old-sock-dir"))   # a relative name keeps under the socket path limit
listener = socket.socket(socket.AF_UNIX)
listener.bind("s")
os.chdir(here)
age(os.path.join(cleaner.TMP, "old-sock-dir"))
os.makedirs(os.path.join(cleaner.HOME, "Desktop", "repo", ".git"))
put("Desktop", "repo", "apps", "web", "package.json", kb=1)
put("Desktop", "repo", "apps", "web", "node_modules", "dep", "index.js")
put("Desktop", "web", "package.json", kb=1)
source = put("Desktop", "web", "src", "app.js", kb=1)
put("Desktop", "web", "node_modules", "dep", "index.js")
put("Desktop", "web", ".next", "chunk")
put("Desktop", "rusty", "Cargo.toml", kb=1)
put("Desktop", "rusty", "target", "debug", "bin")
put("Desktop", "plain", "build", "output")                              # no pubspec.yaml beside it
put("Desktop", "sdk", "bin", "flutter", kb=1)
put("Desktop", "sdk", "packages", "tool", "pubspec.yaml", kb=1)
put("Desktop", "sdk", "packages", "tool", ".dart_tool", "snapshot")
put("Library", "Developer", "node_modules", "x")                        # ~/Library is never walked
put(".android", "avd", "Pixel_3a.avd", "userdata.img")
put(".android", "avd", "Pixel_3a.ini", kb=1)
put("Library", "Android", "sdk", "system-images", "android-33", "google_apis", "arm64-v8a", "system.img")
put("Library", "Android", "sdk", "ndk", "26.3.1", "toolchain")
put(".cache", "lm-studio", "models", "community", "Qwen-9B-GGUF", "weights.gguf")
put("Library", "Developer", "Xcode", "Archives", "2026-07-30", "Runner 30-07-26, 14.06.xcarchive", "dSYM")
put("anaconda3", "pkgs", "numpy-1.0.conda")
put("anaconda3", "pkgs", "scipy-1.0.tar.bz2")
conda_unpacked = put("anaconda3", "pkgs", "numpy-1.0", "lib.so")
put("fvm", "versions", "stable", "bin", "cache", "dart-sdk")
put(".local", "share", "claude", "versions", "2.1.296")
put(".local", "share", "claude", "versions", "2.1.200")
os.makedirs(os.path.join(cleaner.HOME, ".local", "bin"))
os.symlink(os.path.join(cleaner.HOME, ".local", "share", "claude", "versions", "2.1.296"),
           os.path.join(cleaner.HOME, ".local", "bin", "claude"))
os.makedirs(os.path.join(cleaner.HOME, "Documents"))
os.makedirs(os.path.join(cleaner.HOME, "Downloads"))

try:
    # --- what the scan lists ------------------------------------------------
    caches = named(cleaner.scan_caches())
    assert "big" in caches and caches["big"]["group"] == "apps", caches.keys()
    assert caches["Playwright browsers"]["group"] == "dev"
    assert not any("apple" in name for name in caches), caches.keys()
    small = caches["2 smaller caches"]
    assert sorted(os.path.basename(p) for p in small["paths"]) == ["link", "tiny"]

    home_cache = named(cleaner.scan_home_cache())
    assert list(home_cache) == ["uv (Python) downloads"], home_cache.keys()

    apps = named(cleaner.scan_app_data())
    assert list(apps) == ["Slack cache"], apps.keys()
    assert sorted(os.path.basename(p) for p in apps["Slack cache"]["paths"]) == ["Cache", "Code Cache"]

    temp = cleaner.scan_temp()[0]
    assert [os.path.basename(p) for p in temp["paths"]] == ["old-dir"], temp["paths"]

    dev = named(cleaner.scan_dev_tools())
    assert [os.path.basename(p) for p in dev["npm downloads"]["paths"]] == ["_cacache"]
    conda = dev["Conda package downloads"]["paths"]
    assert sorted(os.path.basename(p) for p in conda) == ["numpy-1.0.conda", "scipy-1.0.tar.bz2"], conda
    assert dev["Flutter SDK downloads (stable)"]["paths"][0].endswith(os.path.join("stable", "bin", "cache"))
    old = dev["Older Claude Code versions"]["paths"]
    assert [os.path.basename(p) for p in old] == ["2.1.200"], "the version `claude` points at stays"

    big = named(cleaner.scan_big())
    assert sorted(big) == [
        "Android NDK 26.3.1", "Android emulator Pixel 3a",
        "Android system image android-33 (google_apis, arm64-v8a)",
        "LM Studio model Qwen-9B-GGUF", "Xcode archive Runner 30-07-26, 14.06"], sorted(big)
    assert all(row["group"] == "big" for row in big.values())
    assert sorted(os.path.basename(p) for p in big["Android emulator Pixel 3a"]["paths"]) == [
        "Pixel_3a.avd", "Pixel_3a.ini"]

    # Only the app itself counts as running, not helpers inside its bundle that outlive it.
    whatsapp = named(cleaner.scan_app_data())["Slack cache"]
    helper = "/Applications/Slack.app/Contents/PlugIns/Share.appex/Contents/MacOS/Share\n"
    assert cleaner.blocked(whatsapp, helper) == ""
    assert cleaner.blocked(whatsapp, helper + "/Applications/Slack.app/Contents/MacOS/Slack\n") == "Quit Slack first"

    builds, notice = cleaner.scan_builds()
    assert notice == ""
    assert sorted(row["name"] for row in builds) == [
        "repo/apps/web / node_modules", "rusty / target", "web / .next", "web / node_modules"], builds

    # --- what a delete removes, and what it leaves ---------------------------
    out = cleaner.delete(named(builds)["web / node_modules"])
    assert out["errors"] == [] and out["left_kb"] == 0 and out["freed_kb"] >= 100, out
    assert not os.path.exists(os.path.join(cleaner.HOME, "Desktop", "web", "node_modules"))
    assert os.path.exists(source)

    out = cleaner.delete(apps["Slack cache"])
    assert out["errors"] == [], out
    assert os.listdir(os.path.join(cleaner.HOME, "Library", "Application Support", "Slack")) == ["Local Storage"]
    assert os.path.exists(slack_data)

    out = cleaner.delete(small)
    assert out["errors"] == [], out
    assert not os.path.lexists(os.path.join(cleaner.HOME, "Library", "Caches", "link"))
    assert os.path.exists(kept_outside), "a symlink in a cache folder must not reach what it points at"

    cleaner.delete(dev["npm downloads"])
    assert os.path.exists(npm_config)
    out = cleaner.delete(dev["Conda package downloads"])
    assert out["errors"] == [] and os.path.exists(conda_unpacked), "unpacked packages stay"
    cleaner.delete(big["Android emulator Pixel 3a"])
    assert os.listdir(os.path.join(cleaner.HOME, ".android", "avd")) == []
    cleaner.delete(temp)
    assert sorted(os.listdir(cleaner.TMP)) == ["new-dir", "old-sock-dir"]

    # --- the second lock ---------------------------------------------------
    assert not cleaner.deletable("/etc/hosts")
    assert not cleaner.deletable(cleaner.HOME)
    assert not cleaner.deletable(os.path.join(cleaner.HOME, "Desktop"))
    assert not cleaner.deletable(os.path.join(cleaner.HOME, "Desktop", "..", "..", "outside"))
    os.symlink(outside, os.path.join(cleaner.HOME, "Desktop", "way-out"))
    assert not cleaner.deletable(os.path.join(cleaner.HOME, "Desktop", "way-out", "precious"))
    assert cleaner.deletable(os.path.join(cleaner.HOME, "Library", "Caches", "com.example.big"))
    assert cleaner.deletable(os.path.join(cleaner.TMP, "new-dir"))

    stray = cleaner.item("apps", "stray", [os.path.join(outside, "precious")], 100)
    out = cleaner.delete(stray)
    assert out["errors"] and os.path.exists(kept_outside), out

    in_use = cleaner.item("apps", "in use", [caches["big"]["paths"][0]], 100,
                          quit=("Quit it first", [re.escape("/bin/ps")]))    # ps always sees itself
    out = cleaner.delete(in_use)
    assert out["errors"] == ["Quit it first"] and os.path.exists(caches["big"]["paths"][0]), out

    assert cleaner.size_kb("4.97GB (32%)") == int(4.97e9 / 1024)
    assert cleaner.size_kb("0B") == 0

    # --- the page's API: only the page this launch opened gets in -------------
    server = ThreadingHTTPServer(("127.0.0.1", 0), cleaner.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path, token=cleaner.TOKEN, host=None, body=None):
        headers = {"X-Token": token}
        if host:
            headers["Host"] = host
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers)) as reply:
                return reply.status, json.loads(reply.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    with urllib.request.urlopen(base + "/favicon.svg") as reply:      # the tab icon needs no token
        assert reply.status == 200 and reply.headers["Content-Type"] == "image/svg+xml"
        assert reply.read().startswith(b"<svg")
    assert call("/api/start")[0] == 200
    assert call("/api/start", token="wrong")[0] == 403
    assert call("/api/start", token="")[0] == 403
    assert call("/api/start", host="evil.example:80")[0] == 403
    assert call("/api/delete", token="wrong", body={"ids": ["x"]})[0] == 403
    assert call("/api/delete", body={"ids": ["no-such-row"]})[0] == 404
    assert call("/api/delete", body={"paths": ["/etc/hosts"]})[0] == 400
    assert call("/api/delete", body={"ids": [["not", "a", "string"]]})[0] == 400
    assert call("/api/delete", body={"ids": []})[0] == 400

    status, found = call("/api/scan?source=caches")
    row = next(r for r in found["items"] if r["name"] == "big")
    assert status == 200 and "paths" not in row
    assert call("/api/blocked")[1][row["id"]] == ""
    status, out = call("/api/delete", body={"ids": [row["id"], row["id"]]})
    assert status == 200 and out == {"started": True}, out
    for _ in range(100):
        progress = call("/api/progress")[1]
        if not progress["running"]:
            break
        time.sleep(0.1)
    assert progress["count"] == 1 and progress["done_kb"] == progress["total_kb"] > 0, progress
    assert progress["results"][0]["left_kb"] == 0 and progress["results"][0]["errors"] == [], progress
    assert progress["disk"]["total_kb"] > 0
    assert not os.path.exists(os.path.join(cleaner.HOME, "Library", "Caches", "com.example.big"))
    assert os.path.exists(os.path.join(cleaner.HOME, "Library", "Caches", "com.apple.thing"))
    assert os.path.exists(os.path.join(cleaner.HOME, ".cache", "lm-studio", "models", "model.gguf"))
    server.shutdown()
    print("ok")
finally:
    listener.close()
    shutil.rmtree(box)
