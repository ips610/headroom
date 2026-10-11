#!/usr/bin/env python3
"""Runs the scans and the actions against a throwaway home folder. Nothing real is touched:
Finder, the Trash, Spotlight and administrator commands are swapped for a recorder.

    python3 test_cleaner.py
"""
import contextlib
import http.client
import io
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

import cleaner

box = os.path.realpath(tempfile.mkdtemp(prefix="headroom-test-"))
cleaner.HOME = os.path.join(box, "home")
cleaner.TMP = os.path.join(box, "tmp")
cleaner.APPLICATIONS = os.path.join(box, "Applications")
cleaner.SYSTEM_APPLICATIONS = os.path.join(box, "System", "Applications")
cleaner.TOOL_DIRS = [os.path.join(box, "bin")]
cleaner.PORTS = []                  # a random port, so the real headroom keeps 8460
cleaner.MIN_KB, cleaner.SMALL_KB, cleaner.BUILD_MIN_KB = 8, 64, 64
outside = os.path.join(box, "outside")
real_run = cleaner.run
# What `ps` shows. Real apps running on this Mac must not change what the tests delete.
RUNNING = [""]
cleaner.processes = lambda: RUNNING[0]


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
    os.utime(path, (then, then))


def named(rows):
    return {row["name"]: row for row in rows}


def app(folder, name, ident, exe=None, kb=200):
    """A fake .app: an Info.plist and a program file."""
    path = os.path.join(folder, f"{name}.app")
    put("Contents", "MacOS", exe or name, kb=kb, root=path)
    with open(os.path.join(path, "Contents", "Info.plist"), "wb") as handle:
        plistlib.dump({"CFBundleIdentifier": ident, "CFBundleName": name, "CFBundleExecutable": exe or name,
                       "CFBundleShortVersionString": "2.1"}, handle)
    return path


def agent(label, program):
    path = os.path.join(cleaner.HOME, "Library", "LaunchAgents", label + ".plist")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        plistlib.dump({"Label": label, "ProgramArguments": [program, "--run"]}, handle)
    return path


class Recorder:
    """Stands in for run(): records what would reach Finder, Spotlight or an admin prompt,
    and passes harmless programs (du, ps, git, rm inside the test folder) through."""
    def __init__(self, returncode=0, stderr=""):
        self.calls, self.returncode, self.stderr = [], returncode, stderr

    def __call__(self, command, timeout=None):
        if command[0] in ("/usr/bin/osascript", "/usr/bin/open", "/usr/bin/qlmanage", "/usr/bin/mdls"):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)
        assert command[0] in ("/usr/bin/du", "/bin/ps", "/bin/rm") or command[0].endswith("/git"), command
        return real_run(command, timeout)


@contextlib.contextmanager
def recording(recorder):
    cleaner.run = recorder
    try:
        yield recorder
    finally:
        cleaner.run = real_run


def fresh():
    cleaner._ORPHANS["at"] = 0      # leftovers are worked out again on the next scan


# --- a small fake Mac -------------------------------------------------------
put("Library", "Caches", "com.example.big", "blob")
put("Library", "Caches", "tiny", "blob", kb=10)
put("Library", "Caches", "com.apple.thing", "blob")
put("Library", "Caches", "ms-playwright", "chromium")
put("Library", "Caches", "CloudKit", "blob")                            # Apple's, without the prefix
kept_outside = put("precious", "file", root=outside)
os.symlink(os.path.join(outside, "precious"), os.path.join(cleaner.HOME, "Library", "Caches", "link"))
put(".cache", "lm-studio", "models", "model.gguf")
put(".cache", "uv", "wheel")
put("Library", "Application Support", "Slack", "Code Cache", "js")
put("Library", "Application Support", "Slack", "Cache", "data")
slack_data = put("Library", "Application Support", "Slack", "Local Storage", "leveldb")
put("Library", "Application Support", "Plain", "Cache", "data")       # not a Chromium app
put("Library", "Logs", "app.log")
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
put(".npm", "_cacache", "tarball")
npm_config = put(".npm", "config", kb=1)
GIT = cleaner.git()
os.makedirs(os.path.join(cleaner.HOME, "Desktop", "repo"))
if GIT:
    real_run([GIT, "init", "-q", os.path.join(cleaner.HOME, "Desktop", "repo")])
else:
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
put("Desktop", "plain", "dist", "bundle.js")                            # no package.json beside it
put("Desktop", "swifty", "Package.swift", kb=1)
put("Desktop", "swifty", ".build", "debug", "obj")
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
put(".m2", "repository", "org", "lib.jar")
put(".deno", "bin", "deno")                                             # deno itself stays
put(".deno", "deps", "https", "mod.ts")
put(".expo", "state.json", kb=1)                                        # its login stays
put(".expo", "versions-cache", "x")
# Editor extensions: two versions of one, an old platform build, one marked obsolete, one in use.
ext = os.path.join(cleaner.HOME, ".vscode", "extensions")
for name in ("ms-python.python-2024.1.0", "ms-python.python-2024.2.1", "rust-lang.rust-analyzer-0.3.1-darwin-arm64",
             "rust-lang.rust-analyzer-0.3.2-darwin-arm64", "gone.thing-1.0.0", "pinned.ext-1.0.0", "pinned.ext-2.0.0"):
    put(name, "out.js", root=ext)
with open(os.path.join(ext, ".obsolete"), "w") as handle:
    json.dump({"gone.thing-1.0.0": True}, handle)
with open(os.path.join(ext, "extensions.json"), "w") as handle:
    json.dump([{"identifier": {"id": "pinned.ext"}, "relativeLocation": "pinned.ext-1.0.0",
                "location": {"path": os.path.join(ext, "pinned.ext-1.0.0")}}], handle)

# Apps: one installed (with things in ~/Library), one Apple app, one gone that left things behind.
keep_app = app(cleaner.APPLICATIONS, "Keeper", "com.keeper.keep")
app(cleaner.SYSTEM_APPLICATIONS, "Notes", "com.apple.Notes")
app(os.path.join(cleaner.APPLICATIONS, "Utilities"), "Tool", "org.tooling.tool")
os.symlink(os.path.join(cleaner.SYSTEM_APPLICATIONS, "Notes.app"), os.path.join(cleaner.APPLICATIONS, "Notes.app"))
put("toolname", kb=1, root=os.path.join(box, "bin"))                     # a command-line tool
keeper_pref = put("Library", "Preferences", "com.keeper.keep.plist", kb=4)
keeper_support = put("Library", "Application Support", "Keeper", "data.db")
put("Library", "Containers", "com.keeper.keep", "Data", "Library", "Caches", "blob")
put("Library", "Group Containers", "ABCDE12345.com.keeper.keep", "Library", "Caches", "shared")
put("Library", "Group Containers", "ABCDE12345.com.keeper.keep", "Library", "Preferences", "p.plist", kb=1)
put("Library", "Preferences", "com.keeper.helper.plist", kb=4)          # same maker: not a leftover
put("Library", "Saved Application State", "com.keeper.helper.savedState", "w", kb=4)
gone_paths = [
    put("Library", "Preferences", "com.goneco.oldapp.plist", kb=4),
    put("Library", "Saved Application State", "com.goneco.oldapp.savedState", "windows", kb=4),
    put("Library", "Caches", "com.goneco.oldapp", "blob", kb=200),
    put("Library", "Containers", "com.goneco.oldapp", "Data", "stuff", kb=20),
]
put("Library", "Preferences", "com.apple.something.plist", kb=4)
put("Library", "Preferences", "com.lonely.cli.plist", kb=4)             # a lone preferences file
put("Library", "Preferences", "com.someone.toolname.plist", kb=4)       # names an installed tool
put("Library", "HTTPStorages", "com.someone.toolname", "x", kb=4)
put("Library", "Preferences", "net.sealed.thing.plist", kb=4)
put("Library", "Saved Application State", "net.sealed.thing.savedState", "w", kb=4)
put("Library", "Containers", "net.sealed.thing", "Data", "secret", kb=4)
os.chmod(os.path.join(cleaner.HOME, "Library", "Containers", "net.sealed.thing", "Data"), 0)
dead_agent = agent("org.gone.updater", "/nonexistent/headroom-test/updater")
agent("org.alive.agent", "/bin/ls")
agent("org.disk.agent", "/Volumes/Unplugged/thing")

# Trash and browsers.
put(".Trash", "old-photo.jpg", kb=300)
put(".Trash", "idk", "Data", "x", kb=10)
os.chmod(os.path.join(cleaner.HOME, ".Trash", "idk", "Data"), 0)         # like another app's protected data
put("Library", "Caches", "Firefox", "Profiles", "abc.default", "cache2", "entries", kb=300)
firefox_keep = put("Library", "Caches", "Firefox", "Profiles", "abc.default", "settings", "data.json", kb=1)
put("Library", "Application Support", "Arc", "User Data", "Default", "Cache", "data", kb=200)
arc_history = put("Library", "Application Support", "Arc", "User Data", "Default", "History", kb=50)
put("Library", "Application Support", "Arc", "User Data", "GrShaderCache", "x", kb=50)
put("Library", "Caches", "com.apple.Safari", "WebKitCache", "x", kb=100)
put("Library", "Application Support", "Google", "Chrome", "Default", "Cache", "x", kb=10)
os.chmod(os.path.join(cleaner.HOME, "Library", "Application Support", "Google", "Chrome"), 0)   # needs Full Disk Access

# Installers.
old_dmg = put("Downloads", "Tool-1.0.dmg", kb=200)
age(old_dmg, 40)
put("Downloads", "nested", "Thing.pkg", "Contents", "Archive.pax", kb=100)     # a bundle-style package
with zipfile.ZipFile(os.path.join(cleaner.HOME, "Downloads", "App.zip"), "w") as archive:
    archive.writestr("App.app/Contents/Info.plist", "x" * 200_000)
with zipfile.ZipFile(os.path.join(cleaner.HOME, "Downloads", "notes.zip"), "w") as archive:
    archive.writestr("readme.txt", "x" * 200_000)
put("Desktop", "fresh.iso", kb=100)
put("Desktop", "deep", "hidden.dmg", kb=100)                            # Desktop is looked at one level only

try:
    # --- what the scan lists ------------------------------------------------
    caches = named(cleaner.scan_caches())
    assert "big" in caches and caches["big"]["group"] == "apps", caches.keys()
    assert caches["Playwright browsers"]["group"] == "dev"
    assert not any("apple" in name.lower() or "CloudKit" in name for name in caches), caches.keys()
    assert "oldapp" not in caches and not any("goneco" in p for r in caches.values() for p in r["paths"]), \
        "a deleted app's cache is listed once, with its leftovers"
    assert not any("Firefox" in p for r in caches.values() for p in r["paths"]), "browser caches have their own rows"
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
    assert [os.path.basename(p) for p in dev["Deno cache"]["paths"]] == ["deps"], "deno's own binary stays"
    assert [os.path.basename(p) for p in dev["Expo cache"]["paths"]] == ["versions-cache"], "Expo's login stays"
    assert "Maven downloads" in dev
    old_ext = sorted(os.path.basename(p) for p in dev["Old VS Code extension versions"]["paths"])
    # The newest copy and the one extensions.json points at always stay.
    assert old_ext == ["gone.thing-1.0.0", "ms-python.python-2024.1.0",
                       "rust-lang.rust-analyzer-0.3.1-darwin-arm64"], old_ext
    vscode = dev["Old VS Code extension versions"]
    assert cleaner.blocked(vscode, "/Applications/Visual Studio Code.app/Contents/MacOS/Code\n") == "Quit VS Code first"

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

    # --- leftovers of deleted apps -------------------------------------------
    fresh()
    leftovers = named(cleaner.scan_leftovers())
    oldapp = leftovers["oldapp"]
    library = os.path.join(cleaner.HOME, "Library")
    assert sorted(oldapp["paths"]) == sorted(os.path.join(library, *p) for p in (
        ("Preferences", "com.goneco.oldapp.plist"), ("Saved Application State", "com.goneco.oldapp.savedState"),
        ("Caches", "com.goneco.oldapp"), ("Containers", "com.goneco.oldapp"))), oldapp["paths"]
    assert oldapp["group"] == "leftovers" and "com.goneco.oldapp" in oldapp["detail"]
    sealed = leftovers["thing"]
    assert not any("Containers" in p for p in sealed["paths"]) and "protected by macOS" in sealed["note"], sealed
    assert leftovers["updater"]["paths"] == [dead_agent], "a launch agent whose program is gone"
    everything = " ".join(p for r in leftovers.values() for p in r["paths"])
    for kept in ("com.keeper", "com.apple.", "com.lonely.cli", "toolname", "org.alive", "org.disk", "Notes"):
        assert kept not in everything, kept

    # --- Trash and browser caches ---------------------------------------------
    trash_rows, notice = cleaner.scan_trash()
    trash = named(trash_rows)
    assert [os.path.basename(p) for p in trash["Trash"]["paths"]] == ["old-photo.jpg"], trash["Trash"]["paths"]
    assert "idk" in trash["Trash"]["note"] and "protects" in trash["Trash"]["note"]
    assert [os.path.basename(p) for p in trash["Firefox cache"]["paths"]] == ["cache2"]
    assert sorted(os.path.basename(p) for p in trash["Arc cache"]["paths"]) == ["Cache", "GrShaderCache"]
    assert [os.path.basename(p) for p in trash["Safari cache"]["paths"]] == ["WebKitCache"]
    assert "Chrome's cache" in notice and "Full Disk Access" in notice, notice
    assert cleaner.blocked(trash["Firefox cache"], "/Applications/Firefox.app/Contents/MacOS/firefox -foreground\n")
    assert not cleaner.blocked(trash["Firefox cache"],
                               "/Applications/Firefox.app/Contents/MacOS/plugin-container.app/Contents/MacOS/plugin-container\n")
    out = cleaner.delete(trash["Trash"])
    assert out["errors"] == [] and os.listdir(os.path.join(cleaner.HOME, ".Trash")) == ["idk"], out
    out = cleaner.delete(trash["Arc cache"])
    assert out["errors"] == [] and os.path.exists(arc_history), "the browser's history stays"
    cleaner.delete(trash["Firefox cache"])
    assert os.path.exists(firefox_keep)
    # Had the scan missed it, the protected folder still reports plainly instead of failing the row.
    stuck = cleaner.item("trash", "Trash", [os.path.join(cleaner.HOME, ".Trash", "idk")], 10)
    out = cleaner.delete(stuck)
    assert out["errors"] == ["macOS protects idk, so it stayed."], out

    # --- installer files --------------------------------------------------------
    installers = named(cleaner.scan_installers())
    assert sorted(installers) == ["App.zip", "Thing.pkg", "Tool-1.0.dmg", "fresh.iso"], sorted(installers)
    assert installers["Tool-1.0.dmg"]["safe"] and not installers["Tool-1.0.dmg"]["recent"]
    assert installers["fresh.iso"]["recent"] and not installers["fresh.iso"]["safe"]
    assert "~/Downloads" in installers["Tool-1.0.dmg"]["detail"] and "added 5 weeks ago" in installers["Tool-1.0.dmg"]["detail"]

    # --- qBittorrent ------------------------------------------------------------------
    def bencode(value):
        if isinstance(value, int):
            return b"i%de" % value
        if isinstance(value, str):
            value = value.encode()
        if isinstance(value, bytes):
            return b"%d:%s" % (len(value), value)
        if isinstance(value, list):
            return b"l" + b"".join(map(bencode, value)) + b"e"
        return b"d" + b"".join(bencode(k) + bencode(value[k]) for k in sorted(value)) + b"e"

    def write(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    support = os.path.join(cleaner.HOME, "Library", "Application Support", "qBittorrent")
    backup = os.path.join(support, "BT_backup")
    downloads, research = os.path.join(cleaner.HOME, "Downloads"), os.path.join(cleaner.HOME, "Desktop", "Research", "VirusShare")

    def in_session(digest, name, save, **resume):
        write(os.path.join(backup, digest + ".torrent"),
              bencode({"announce": "x", "info": {"name": name, "piece length": 16384, "pieces": b"p" * 20}}))
        write(os.path.join(backup, digest + ".fastresume"),
              bencode({"save_path": save, "qBt-savePath": save, "name": name, "file-format": "libtorrent resume file", **resume}))

    put("logs", "qbittorrent.log", kb=100, root=support)
    put("GeoDB", "dbip-country-lite.mmdb", kb=900, root=support)
    rss = put("rss", "feeds.json", kb=1, root=support)
    config = write(os.path.join(cleaner.HOME, ".config", "qBittorrent", "qBittorrent.ini"), (
        "[AddNewTorrentDialog]\nSavePathHistory=%s, %s\n[BitTorrent]\nSession\\TempPath=%s\n"
        % (research, downloads, os.path.join(downloads, "incomplete"))).encode())
    in_session("a" * 40, "Movie Pack", downloads)
    in_session("b" * 40, "VirusShare_00373.zip", research)
    in_session("c" * 40, "Renamed", os.path.join(downloads, "renamed-place"), mapped_files=["other.bin"])
    finished = put("Movie Pack", "done.mkv", kb=50, root=downloads)                    # a finished file: never offered
    kept_pieces = [
        put("Movie Pack", "part1.mkv.!qB", kb=50, root=downloads),                     # in the session
        put("incomplete", "Movie Pack", "part2.mkv.!qB", kb=50, root=downloads),       # its temp path
        put("VirusShare_00373.zip.!qB", kb=50, root=research),                         # single-file torrent
        put("renamed-place", "whatever.bin.!qB", kb=50, root=downloads),               # renamed files: whole folder stays
        put("." + "a" * 40 + ".parts", kb=50, root=downloads),                         # parts file of a live torrent
    ]
    orphans = [
        put("Old Show", "season 1", "ep1.mkv.!qB", kb=50, root=downloads),
        put("single.iso.!qB", kb=50, root=downloads),
        put("VirusShare_00100.zip.!qB", kb=50, root=research),
        put("." + "d" * 40 + ".parts", kb=50, root=downloads),
    ]
    dataset = put("VirusShare_00001.zip", kb=50, root=research)                          # finished dataset file
    for n in range(60):                                                                 # a dataset folder with no pieces...
        put("big-dataset", f"sample{n:03}", kb=0, root=research)
    hidden = put("big-dataset", "deeper", "late.zip.!qB", kb=50, root=research)        # ...isn't walked on
    used_torrent = write(os.path.join(downloads, "movie.torrent"), bencode({"info": {"name": "Movie Pack"}}))
    fresh_torrent = write(os.path.join(downloads, "fresh.torrent"), bencode({"info": {"name": "Fresh"}}))
    old_torrent = write(os.path.join(downloads, "ancient.torrent"), bencode({"info": {"name": "Ancient"}}))
    age(old_torrent, 40)
    deep_torrent = write(os.path.join(research, "list.torrent"), bencode({"info": {"name": "Movie Pack"}}))
    cleaner.PIECE_FOLDER_LIMIT = 50
    qbit, notice = cleaner.scan_qbittorrent()
    qbit = named(qbit)
    assert notice == "", notice
    assert qbit["qBittorrent logs"]["group"] == "temp" and qbit["qBittorrent logs"]["tidy"]
    assert qbit["qBittorrent location database"]["group"] == "apps"
    pieces = {name: row for name, row in qbit.items() if name.startswith("Unfinished torrent")}
    assert sorted(pieces) == sorted(["Unfinished torrent pieces (dddddddd)", "Unfinished torrent Old Show",
                                     "Unfinished torrent VirusShare_00100.zip", "Unfinished torrent single.iso"]), sorted(pieces)
    assert all(row["group"] == "big" for row in pieces.values())
    assert sorted(p for row in pieces.values() for p in row["paths"]) == sorted(orphans), "only orphaned pieces"
    assert "VirusShare" in pieces["Unfinished torrent VirusShare_00100.zip"]["detail"]
    assert sorted(qbit["2 .torrent files"]["paths"]) == sorted([used_torrent, old_torrent])
    assert qbit["2 .torrent files"]["group"] == "installers"
    offered = {p for row in qbit.values() for p in row["paths"]}
    for never in [finished, dataset, hidden, config, rss, fresh_torrent, deep_torrent] + kept_pieces:
        assert never not in offered, never
    assert not any(p.startswith(backup) for p in offered), "the session itself is never offered"
    running = "/Applications/qBittorrent.app/Contents/MacOS/qbittorrent\n"
    assert all(cleaner.blocked(row, running) == "Quit qBittorrent first" for row in qbit.values())
    assert not any(cleaner.blocked(row, "/Applications/qBittorrent.app/Contents/Frameworks/x\n") for row in qbit.values())
    for row in pieces.values():
        assert cleaner.delete(row)["errors"] == []
    assert not any(os.path.exists(p) for p in orphans)
    assert all(os.path.exists(p) for p in kept_pieces + [finished, dataset, hidden, config, rss])
    # A session headroom can't read in full means no unfinished file is offered at all.
    broken = write(os.path.join(backup, "e" * 40 + ".fastresume"), b"d9:save_path")
    put("Lost", "x.bin.!qB", kb=50, root=downloads)
    qbit, notice = cleaner.scan_qbittorrent()
    assert not any(row["name"].startswith("Unfinished") for row in qbit) and "could not read" in notice
    os.remove(broken)

    # --- caches inside sandboxed apps -------------------------------------------
    sandboxed, notice = cleaner.scan_sandboxed()
    keeper = named(sandboxed)["Keeper cache"]
    assert sorted(os.path.basename(p) for p in keeper["paths"]) == ["blob", "shared"], keeper["paths"]
    assert notice == ""
    assert cleaner.blocked(keeper, keep_app + "/Contents/MacOS/Keeper\n") == "Quit Keeper first"

    # --- project build folders --------------------------------------------------
    git = GIT
    project = os.path.join(cleaner.HOME, "Desktop", "lib")
    put("Desktop", "lib", "package.json", kb=1)
    put("Desktop", "lib", "dist", "index.js")                               # committed: source, not output
    put("Desktop", "lib", "node_modules", "dep", "index.js")
    if git:
        real_run([git, "init", "-q", project])
        real_run([git, "-C", project, "add", "package.json", "dist"])
    put("Desktop", "app2", "package.json", kb=1)
    put("Desktop", "app2", "dist", "bundle.js")                             # beside package.json, untracked
    age(os.path.join(cleaner.HOME, "Desktop", "app2"), 30)
    age(os.path.join(cleaner.HOME, "Desktop", "rusty"), 30)
    builds, notice = cleaner.scan_builds()
    names = sorted(row["name"] for row in builds)
    expected = ["app2 / dist", "repo/apps/web / node_modules", "rusty / target", "swifty / .build",
                "web / .next", "web / node_modules"] if git else ["app2 / dist", "rusty / target",
                                                                   "swifty / .build", "web / .next", "web / node_modules"]
    expected += ["lib / node_modules"] if git else []
    assert names == sorted(expected), names
    if git:
        assert "git tracks" in notice, notice
    rows = named(builds)
    assert rows["web / node_modules"]["recent"] and "this week" in rows["web / node_modules"]["note"]
    assert not rows["rusty / target"]["recent"] and "last used 4 weeks ago" in rows["rusty / target"]["detail"]
    assert os.path.exists(os.path.join(cleaner.state_dir(), "build-folders.json")), "found folders are remembered"
    assert names == sorted(r["name"] for r in cleaner.scan_builds()[0]), "a second scan finds the same"

    # --- what a delete removes, and what it leaves ---------------------------
    out = cleaner.delete(rows["web / node_modules"])
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
    out = cleaner.delete(vscode)
    assert out["errors"] == [] and sorted(os.listdir(ext)) == [
        ".obsolete", "extensions.json", "ms-python.python-2024.2.1", "pinned.ext-1.0.0", "pinned.ext-2.0.0",
        "rust-lang.rust-analyzer-0.3.2-darwin-arm64"], os.listdir(ext)

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
                          quit=("Quit it first", [re.escape("/Applications/It.app/Contents/MacOS/It")]))
    RUNNING[0] = "/Applications/It.app/Contents/MacOS/It\n"
    out = cleaner.delete(in_use)
    RUNNING[0] = ""
    assert out["errors"] == ["Quit it first"] and os.path.exists(caches["big"]["paths"][0]), out

    assert cleaner.size_kb("4.97GB (32%)") == int(4.97e9 / 1024)
    assert cleaner.size_kb("0B") == 0

    # --- moving to the Trash: the lock, and Finder doing the work ------------------
    assert cleaner.trashable(keep_app)
    assert not cleaner.trashable(os.path.join(cleaner.APPLICATIONS, "Notes.app")), "a symlink into the system"
    assert not cleaner.trashable(os.path.join(cleaner.SYSTEM_APPLICATIONS, "Notes.app"))
    assert not cleaner.trashable("/etc/hosts") and not cleaner.trashable(cleaner.APPLICATIONS)
    apple_app = app(cleaner.APPLICATIONS, "Pages", "com.apple.iWork.Pages")
    assert not cleaner.trashable(apple_app), "never Apple's apps"
    with recording(Recorder()) as recorder:
        assert cleaner.finder_trash(keeper_pref) == ""
    assert recorder.calls == [["/usr/bin/osascript", "-e", cleaner.TRASH_SCRIPT, keeper_pref]], recorder.calls
    assert 'delete target' in cleaner.TRASH_SCRIPT and keeper_pref not in cleaner.TRASH_SCRIPT
    with recording(Recorder(1, "execution error: User canceled. (-128)")):
        assert cleaner.finder_trash(keeper_pref) == "You cancelled, so it stayed."

    # --- the uninstaller ------------------------------------------------------
    listed = {a["name"]: a for a in cleaner.list_apps()}
    assert sorted(listed) == ["Keeper", "Tool"], sorted(listed)            # no Apple apps, no symlinks
    keeper_left = sorted(item["name"] for item in listed["Keeper"]["leftovers"])
    assert "~/Library/Preferences/com.keeper.keep.plist" in keeper_left, keeper_left
    assert "~/Library/Application Support/Keeper" in keeper_left, "found by the app's name"
    assert "~/Library/Containers/com.keeper.keep" in keeper_left
    assert not any("helper" in name for name in keeper_left), "another app of the same maker stays"
    assert listed["Keeper"]["kb"] >= 200 and listed["Keeper"]["version"] == "2.1" and not listed["Keeper"]["blocked"]
    real_programs = cleaner.program_paths
    cleaner.program_paths = lambda: {os.path.join(keep_app, "Contents", "MacOS", "Keeper")}
    try:
        assert {a["name"]: a for a in cleaner.list_apps()}["Keeper"]["blocked"] == "Quit Keeper first"
        with recording(Recorder()) as recorder:
            out = cleaner.uninstall(listed["Keeper"]["id"])
        assert out["moved"] == 0 and recorder.calls == [], "nothing moves while the app runs"
    finally:
        cleaner.program_paths = real_programs
    cleaner.list_apps()
    with recording(Recorder()) as recorder:
        out = cleaner.uninstall(listed["Keeper"]["id"])
    moved = [call[3] for call in recorder.calls]
    assert out == {"moved": len(moved), "failed": []} and moved[0] == keep_app, (out, moved)
    assert keeper_pref in moved and keeper_support.rsplit(os.sep, 1)[0] in moved
    assert all(call[:3] == ["/usr/bin/osascript", "-e", cleaner.TRASH_SCRIPT] for call in recorder.calls)
    assert os.path.exists(keep_app), "the recorder stood in for Finder"
    cleaner.list_apps()
    with recording(Recorder(1, "User canceled. (-128)")) as recorder:
        out = cleaner.uninstall(listed["Keeper"]["id"])
    assert len(recorder.calls) == 1 and out["moved"] == 0, "a cancelled app keeps its settings too"
    assert cleaner.uninstall("no-such-app") is None

    # --- the disk explorer -----------------------------------------------------
    put("Documents", "big-video.mov", kb=600)
    locked = put("Documents", "locked", "inner", "x", kb=10)
    os.chmod(os.path.dirname(locked), 0)
    top = cleaner.explore_places("")
    names = [row["name"] for row in top["items"]]
    assert "Documents" in names and "Applications" in names and top["partial"], names
    documents = next(row for row in top["items"] if row["name"] == "Documents")
    assert documents["folder"] and documents["partial"] and documents["trash"] is False, documents  # depth 1 is home's own
    inside = cleaner.explore_places(documents["id"])
    video = next(row for row in inside["items"] if row["name"] == "big-video.mov")
    assert video["trash"] and not video["folder"] and inside["trail"][0]["name"] == "Documents", inside
    library = next(row for row in top["items"] if row["name"] == "Library")
    prefs = next(row for row in cleaner.explore_places(library["id"])["items"] if row["name"] == "Caches")
    assert not prefs["trash"], "no Move to Trash inside ~/Library"
    assert cleaner.explore_places("no-such-id") is None
    with recording(Recorder()) as recorder:
        assert cleaner.explorer_trash(video["id"]) == ""
    assert recorder.calls[0][3] == os.path.join(cleaner.HOME, "Documents", "big-video.mov")
    with recording(Recorder()) as recorder:
        assert "only moves" in cleaner.explorer_trash(library["id"]) and recorder.calls == []
    cleaner.spotlight_big = lambda: [os.path.join(cleaner.HOME, "Documents", "big-video.mov")]
    found = cleaner.explore_big()["items"]
    assert [row["name"] for row in found] == ["big-video.mov"] and "~/Documents" in found[0]["detail"], found
    stale_download = put("Downloads", "old-stuff", "a.bin", kb=50)
    age(os.path.dirname(stale_download), 120)
    old_downloads = [row["name"] for row in cleaner.explore_old()["items"]]
    assert old_downloads == ["old-stuff"], old_downloads

    # --- maintenance, with the command runner swapped for a recorder ---------------
    with recording(Recorder()) as recorder:
        assert cleaner.maintain("quicklook")["ok"]
    assert recorder.calls == [["/usr/bin/qlmanage", "-r"], ["/usr/bin/qlmanage", "-r", "cache"]]
    for task, words in (("dns", "dscacheutil -flushcache"), ("icons", "killall Dock"), ("spotlight", "mdutil -E /")):
        with recording(Recorder()) as recorder:
            assert cleaner.maintain(task)["ok"]
        (call,) = recorder.calls
        assert call[:2] == ["/usr/bin/osascript", "-e"] and words in call[2], call
        assert call[2].endswith("with administrator privileges"), "macOS asks for the password, not headroom"
    with recording(Recorder(1, "execution error: User canceled. (-128)")):
        assert cleaner.maintain("dns") == {"ok": False, "text": "Cancelled. Nothing changed."}

    # --- simulators: simctl's answer is kept until a simulator changes -------------
    devices = os.path.join(cleaner.HOME, "Library", "Developer", "CoreSimulator", "Devices")
    for udid in ("AAAA-1111", "BBBB-2222"):
        put(udid, "device.plist", kb=1, root=devices)
        put(udid, "data", "Library", "Caches", "com.apple.thing", "c", kb=100, root=devices)
    developer = os.path.join(box, "Developer")
    counter = os.path.join(box, "simctl-calls")
    put("usr", "bin", "simctl", kb=0, root=developer)
    with open(os.path.join(developer, "usr", "bin", "simctl"), "w") as handle:
        handle.write("#!/bin/sh\necho x >> '%s'\ncat '%s'\n" % (counter, os.path.join(box, "devices.json")))
    os.chmod(os.path.join(developer, "usr", "bin", "simctl"), 0o755)
    with open(os.path.join(box, "devices.json"), "w") as handle:
        json.dump({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
            {"udid": "AAAA-1111", "name": "iPhone", "isAvailable": True, "state": "Shutdown"},
            {"udid": "BBBB-2222", "name": "iPad", "isAvailable": True, "state": "Booted"}]}}, handle)
    real_developer = cleaner.developer_dir
    cleaner.developer_dir = lambda: developer
    try:
        sims = named(cleaner.scan_simulators())
        inside = sims["Caches inside simulators"]["paths"]
        assert inside == [os.path.join(devices, "AAAA-1111", "data", "Library", "Caches", "com.apple.thing")], inside
        assert cleaner.blocked(sims["Caches inside simulators"], f"launchd_sim {devices}/AAAA-1111/data/var/run/x\n")
        cleaner.scan_simulators()
        assert open(counter).read().count("x") == 1, "the second scan used the saved answer"
        os.utime(os.path.join(devices, "AAAA-1111", "device.plist"), (1, 1))     # a simulator booted or shut down
        cleaner.scan_simulators()
        assert open(counter).read().count("x") == 2
    finally:
        cleaner.developer_dir = real_developer

    # --- the page's API: only this launch's page gets in ---------------------
    v4, v6 = cleaner.listen()
    assert v6 is not None and v6.server_port == v4.server_port
    for server in (v4, v6):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    port = v4.server_port
    host = f"headroom.localhost:{port}"
    cookie = f"headroom={cleaner.TOKEN}"

    def call(path, body=None, cookie=cookie, host=host, run=cleaner.RUN, address="127.0.0.1", raw=None, extra=None):
        connection = http.client.HTTPConnection(address, port, timeout=30)
        headers = {"Host": host, **(extra or {})}
        if cookie:
            headers["Cookie"] = cookie
        if run:
            headers["X-Headroom"] = run
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        connection.request("POST" if data is not None else "GET", path, body=data, headers=headers)
        reply = connection.getresponse()
        text = reply.read()
        connection.close()
        try:
            return reply.status, json.loads(text), reply
        except ValueError:
            return reply.status, text, reply

    status, _, reply = call(f"/?k={cleaner.TOKEN}", cookie=None, run=None)
    assert status == 303 and reply.getheader("Location") == "/", status
    assert reply.getheader("Set-Cookie") == f"headroom={cleaner.TOKEN}; Path=/; HttpOnly; SameSite=Strict"
    status, _, reply = call("/?k=wrong", cookie=None, run=None)
    assert status == 303 and reply.getheader("Set-Cookie") is None
    status, page, reply = call("/", cookie=None, run=None)
    assert status == 200 and cleaner.RUN.encode() in page and b"__RUN__" not in page
    assert call("/favicon.svg", cookie=None, run=None)[1].startswith(b"<svg")
    assert call("/", host="evil.example:80", cookie=None, run=None)[0] == 403, "DNS rebinding gets nothing"
    # Typing headroom.localhost (or using a bookmark, or the port-80 door's redirect) opens a session;
    # a link or redirect from another site gets the page, which says to open headroom from Applications.
    def visit(site, mode="navigate", dest="document", **options):
        sent = {k: v for k, v in (("Sec-Fetch-Site", site), ("Sec-Fetch-Mode", mode), ("Sec-Fetch-Dest", dest)) if v}
        return call("/", cookie=options.pop("cookie", None), run=None, extra=sent, **options)
    status, page, reply = visit("none")
    assert status == 200 and reply.getheader("Set-Cookie") == cleaner.COOKIE and b"headroom-run" in page
    for site, mode, dest in (("same-site", "navigate", "document"), ("same-origin", "navigate", "document"),
                             ("none", "navigate", None)):
        assert visit(site, mode, dest)[2].getheader("Set-Cookie") == cleaner.COOKIE, (site, mode, dest)
    for site, mode, dest in (("cross-site", "navigate", "document"), (None, "navigate", "document"),
                             ("same-site", "cors", "empty"), ("same-site", "no-cors", "image"),
                             ("same-site", "navigate", "iframe"), ("none", None, None), (None, None, None)):
        status, _, reply = visit(site, mode, dest)
        assert status == 200 and reply.getheader("Set-Cookie") is None, (site, mode, dest)
    assert visit("none", host="evil.example:80")[0] == 403, "the Host check comes first"
    assert visit("none", cookie=cookie)[2].getheader("Set-Cookie") is None, "already signed in"
    assert call("/api/start")[0] == 200
    assert call("/api/start", host=f"127.0.0.1:{port}")[0] == 200
    assert call("/api/start", host=f"[::1]:{port}", address="::1")[0] == 200, "the ::1 listener answers"
    assert call("/api/start", cookie=None)[0] == 403, "no cookie"
    assert call("/api/start", cookie="headroom=wrong")[0] == 403, "wrong cookie"
    assert call("/api/start", host="evil.example:80")[0] == 403, "foreign Host"
    assert call("/api/start", host=f"headroom.localhost:{port + 1}")[0] == 403
    assert call("/api/start", run="0123456789abcdef")[0] == 410, "a tab from an earlier launch"
    assert call("/api/delete", body={"ids": ["x"]}, run=None)[0] == 403, "POST without the custom header"
    assert call("/api/delete", body={"ids": ["x"]}, cookie=None)[0] == 403
    assert call("/api/delete", body={"ids": ["no-such-row"]})[0] == 404
    assert call("/api/delete", body={"paths": ["/etc/hosts"]})[0] == 400
    assert call("/api/delete", body={"ids": [["not", "a", "string"]]})[0] == 400
    assert call("/api/delete", body={"ids": []})[0] == 400
    assert call("/api/trash", body={"path": "/etc/hosts"})[0] == 400, "paths are never accepted"
    assert call("/api/maintain", body={"id": "rm -rf /"})[0] == 404
    before = cleaner.DEADLINE
    assert call("/api/bye", raw=b"not-this-run", run=None)[0] == 200 and cleaner.DEADLINE >= before
    call("/api/bye", raw=cleaner.RUN.encode(), cookie=None, run=None)
    assert cleaner.DEADLINE >= before, "a bye without the cookie is ignored"
    call("/api/bye", raw=cleaner.RUN.encode(), run=None)
    assert cleaner.DEADLINE < before, "the page's own bye shortens the wait"
    cleaner.DEADLINE = time.time() + cleaner.IDLE

    status, found = call("/api/scan?source=caches")[:2]
    row = next(r for r in found["items"] if r["name"] == "big")
    assert status == 200 and "paths" not in row and row["safe"]
    assert call("/api/blocked")[1][row["id"]] == ""
    last = call("/api/start")[1]["last"]["caches"]
    assert any(r["id"] == row["id"] for r in last["items"]) and last["at"] > 0, "the scan is saved for next time"
    status, out = call("/api/delete", body={"ids": [row["id"], row["id"]]})[:2]
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
    assert not any(r["id"] == row["id"] for r in call("/api/start")[1]["last"]["caches"]["items"]), \
        "what was deleted is dropped from the saved scan"

    # --- protect list and history ------------------------------------------------
    found = call("/api/scan?source=home_cache")[1]["items"]
    uv = next(r for r in found if r["name"] == "uv (Python) downloads")
    assert call("/api/protect", body={"id": uv["id"]})[0] == 200
    assert call("/api/protect", body={"id": uv["id"]})[0] == 404, "it left the list at once"
    assert call("/api/delete", body={"ids": [uv["id"]]})[0] == 404, "and can't be deleted any more"
    saved = json.load(open(os.path.join(cleaner.HOME, "Library", "Application Support", "headroom", "protected.json")))
    assert saved[0]["name"] == "uv (Python) downloads" and saved[0]["keys"] == [os.path.join(cleaner.HOME, ".cache", "uv")]
    assert call("/api/scan?source=home_cache")[1]["items"] == [], "kept off the next scan, across launches"
    entries = call("/api/protected")[1]["items"]
    assert [e["name"] for e in entries] == ["uv (Python) downloads"] and "keys" not in entries[0]
    assert call("/api/unprotect", body={"id": entries[0]["id"]})[0] == 200
    assert call("/api/unprotect", body={"id": entries[0]["id"]})[0] == 404
    assert [r["name"] for r in call("/api/scan?source=home_cache")[1]["items"]] == ["uv (Python) downloads"]

    history = call("/api/history")[1]
    assert history["file"] == "~/Library/Logs/headroom/history.jsonl"
    newest = history["items"][0]
    assert newest["what"] == "big" and newest["result"] == "deleted" and newest["kb"] > 0, newest
    assert newest["paths"] == ["~/Library/Caches/com.example.big"] and newest["section"] == "apps"
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$", newest["at"]), newest["at"]
    with open(cleaner.history_file()) as handle:
        lines = [json.loads(line) for line in handle]
    assert {"trashed", "failed", "deleted"} <= {line["result"] for line in lines}, "uninstall, explorer and deletes"
    assert any(line["section"] == "uninstall" and line["what"] == "Keeper" for line in lines)
    logs = named(cleaner.scan_logs())["App logs"]["paths"]
    assert not any(p.endswith("headroom") for p in logs), "App logs never deletes the history"

    # --- the explorer and maintenance over the API: ids only ------------------------
    with recording(Recorder()) as recorder:
        documents = next(r for r in call("/api/explore?view=places")[1]["items"] if r["name"] == "Documents")
        assert call("/api/reveal", body={"id": documents["id"]})[0] == 200
        assert call("/api/reveal", body={"id": "no-such-place"})[0] == 404
        assert call("/api/maintain", body={"id": "quicklook"})[1]["ok"]
    assert recorder.calls[0] == ["/usr/bin/open", "-R", os.path.join(cleaner.HOME, "Documents")], recorder.calls

    # A tab closing mid-reply is not worth a traceback in the log.
    noise = io.StringIO()
    with contextlib.redirect_stderr(noise):
        try:
            raise BrokenPipeError
        except BrokenPipeError:
            v4.handle_error(None, ("127.0.0.1", 1))
    assert noise.getvalue() == "", noise.getvalue()
    for server in (v4, v6):
        server.shutdown()
        server.server_close()

    # --- sockets launchd holds and hands over --------------------------------------
    held = [socket.socket(socket.AF_INET), socket.socket(socket.AF_INET6)]
    held[0].bind(("127.0.0.1", 0))
    held[1].bind(("::1", held[0].getsockname()[1]))
    for sock in held:
        sock.listen(16)
    port = held[0].getsockname()[1]
    os.environ["HEADROOM_LISTEN_FDS"] = f"{held[0].fileno()},{held[1].fileno()}"
    try:
        servers = cleaner.inherited()
    finally:
        del os.environ["HEADROOM_LISTEN_FDS"]
    assert [s.server_port for s in servers] == [port, port]
    assert [s.address_family for s in servers] == [socket.AF_INET, socket.AF_INET6]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    for address, name in (("127.0.0.1", f"127.0.0.1:{port}"), ("::1", f"headroom.localhost:{port}")):
        connection = http.client.HTTPConnection(address, port, timeout=30)
        connection.request("GET", "/favicon.svg", headers={"Host": name})
        assert connection.getresponse().status == 200, address
        connection.close()
    for server in servers:
        server.shutdown()
    assert cleaner.inherited() is None, "without the variable, headroom binds its own"

    # The whole server started the way the launcher starts it: handed sockets, told not to open
    # a browser. It names the address on its first line and answers on both sockets.
    held = [socket.socket(socket.AF_INET), socket.socket(socket.AF_INET6)]
    held[0].bind(("127.0.0.1", 0))
    held[1].bind(("::1", held[0].getsockname()[1]))
    for sock in held:
        sock.listen(16)
    port = held[0].getsockname()[1]
    fds = [sock.fileno() for sock in held]
    child = subprocess.Popen([sys.executable, cleaner.__file__, "--no-open"], stdout=subprocess.PIPE, text=True,
                             pass_fds=fds, env={**os.environ, "HEADROOM_LISTEN_FDS": ",".join(map(str, fds))})
    try:
        line = child.stdout.readline()
        assert re.match(rf"^headroom is open at http://headroom\.localhost:{port}/\?k=[\w-]+$", line.strip()), line
        for sock in held:
            sock.close()                    # only the child holds them now
        for address in ("127.0.0.1", "::1"):
            connection = http.client.HTTPConnection(address, port, timeout=30)
            connection.request("GET", "/" + line.strip().split("/", 3)[3], headers={"Host": f"headroom.localhost:{port}"})
            reply = connection.getresponse()
            assert reply.status == 303 and reply.getheader("Set-Cookie", "").startswith("headroom="), address
            connection.close()
    finally:
        child.terminate()
        child.wait(10)
    print("ok")
finally:
    listener.close()
    subprocess.run(["/bin/chmod", "-R", "u+rwx", box])
    shutil.rmtree(box)
