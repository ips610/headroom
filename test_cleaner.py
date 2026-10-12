#!/usr/bin/env python3
"""Runs the scans and the actions against a throwaway home folder. Nothing real is touched:
Finder, the Trash, Spotlight and administrator commands are swapped for a recorder.

    python3 test_cleaner.py
"""
import contextlib
import datetime
import http.client
import io
import json
import os
import platform
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
cleaner.SIZER = None                # du measures here, and a made-up measurer where a test says so
outside = os.path.join(box, "outside")
spawn = cleaner.run


def real_run(command, timeout=None, input=None):
    """Every program the tests start goes through here. A login code must never be on a command
    line, where `ps` shows it to every account on the Mac."""
    assert not any("?k=" in str(part) for part in command), command
    return spawn(command, timeout, input)


cleaner.run = real_run
# What `ps` shows. Real apps running on this Mac must not change what the tests delete.
RUNNING = [""]
cleaner.processes = lambda: RUNNING[0]
# No test opens a browser tab: the addresses headroom would open are kept here instead.
OPENED = []
real_open_page = cleaner.open_page
cleaner.open_page = OPENED.append


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
    """Stands in for run(): records what would reach Finder, Spotlight, launchctl, Homebrew, Docker
    or an admin prompt, and passes harmless programs (du, ps, git, rm inside the test folder) through.
    `answer(command)` gives a recorded command's (returncode, output); `fakes` are fake programs;
    `never` are programs that must not be started at all."""
    def __init__(self, returncode=0, stderr="", answer=None, fakes=(), never=()):
        self.calls, self.returncode, self.stderr, self.answer, self.fakes = [], returncode, stderr, answer, fakes
        self.inputs = []        # what each recorded command was given to read
        self.never = never

    def __call__(self, command, timeout=None, input=None):
        assert not any("?k=" in str(part) for part in command), command
        assert command[0] not in self.never, command
        if command[0] in ("/usr/bin/osascript", "/usr/bin/open", "/usr/bin/qlmanage", "/usr/bin/mdls",
                          "/bin/launchctl", "/usr/bin/env") + tuple(self.fakes):
            self.calls.append(command)
            self.inputs.append(input)
            code, out = self.answer(command) if self.answer else (self.returncode, "")
            if command[:3] == ["/usr/bin/osascript", "-e", cleaner.TRASH_SCRIPT] and not code and not out:
                out = "0\n" * len(command[3:])       # Finder: every one of them moved
            return subprocess.CompletedProcess(command, code, out, self.stderr)
        assert command[0] in ("/usr/bin/du", "/bin/ps", "/bin/rm") or command[0].endswith("/git"), command
        return real_run(command, timeout, input)


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
        "Xcode archive Runner 30-07-26, 14.06"], (sorted(big), "models have a section of their own")
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

    # --- a folder's name can't add a line to what `du` says ------------------------
    # `du` prints "size<TAB>path" on a line, and a name may hold a line break. These nested folders
    # spell a second line: a real folder somewhere else, with a made-up size. Rows come from what
    # du() returns and a delete removes them, so only the paths that were asked about may come back.
    thesis = os.path.dirname(put("Documents", "thesis", "draft.txt"))
    clone = os.path.join(cleaner.HOME, "Desktop", "clone")
    spelled = thesis.lstrip(os.sep).split(os.sep)
    hostile = os.path.join(clone, "x\n99999\t", *spelled[:-1], spelled[-1] + "\ny", "node_modules")
    put("dep.js", root=hostile)
    put("package.json", kb=1, root=os.path.dirname(hostile))
    assert cleaner.deletable(thesis), "the second lock would let it through, so du() must not offer it"
    assert thesis not in cleaner.du([hostile]) and set(cleaner.du([hostile, thesis])) == {thesis}
    assert all(os.path.lexists(p) for p in cleaner.du_children(clone)[0]), "nor does the Disk tab list one"
    # The walk for build folders doesn't go into such a name in the first place.
    odd = [os.path.join(cleaner.HOME, "Desktop", name) for name in ("line\nbreak", "carriage\rreturn")]
    for folder in odd:
        put("package.json", kb=1, root=folder)
        put("node_modules", "dep.js", root=folder)
    assert not any("\n" in folder or "\r" in folder for folder in cleaner.find_build_folders())
    for folder in odd + [clone, thesis]:
        shutil.rmtree(folder)

    in_use = cleaner.item("apps", "in use", [caches["big"]["paths"][0]], 100,
                          quit=("Quit it first", [re.escape("/Applications/It.app/Contents/MacOS/It")]))
    RUNNING[0] = "/Applications/It.app/Contents/MacOS/It\n"
    out = cleaner.delete(in_use)
    RUNNING[0] = ""
    assert out["errors"] == ["Quit it first"] and os.path.exists(caches["big"]["paths"][0]), out

    # By the calendar, a minute before midnight was yesterday, however few hours ago that is.
    midnight = time.mktime(datetime.date.today().timetuple())
    assert cleaner.ago(midnight - 60, calendar=True) == "yesterday" and cleaner.ago(midnight + 1, calendar=True) == "today"
    assert cleaner.ago(time.time() - 3600) == "today" and cleaner.sentence("Too many open files") == "Too many open files."
    assert cleaner.sentence("Done.") == "Done." and cleaner.sentence("") == ""
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
    assert 'delete theItem' in cleaner.TRASH_SCRIPT and keeper_pref not in cleaner.TRASH_SCRIPT
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

    # --- iPhone and iPad backups: your data, so they go to the Trash ------------------------
    backups = os.path.join(cleaner.HOME, "Library", "Application Support", "MobileSync", "Backup")
    put("00008110-000A", "Manifest.db", kb=300, root=backups)
    with open(os.path.join(backups, "00008110-000A", "Info.plist"), "wb") as handle:
        plistlib.dump({"Device Name": "Ishpuneet's iPhone", "Product Type": "iPhone14,2",
                       "Last Backup Date": datetime.datetime(2026, 3, 2, 9, 30)}, handle)
    phone = named(cleaner.scan_backups()[0])["Ishpuneet's iPhone backup"]
    assert phone["group"] == "big" and phone["trash"] and "only copy" in phone["note"], phone
    assert "iPhone14,2" in phone["detail"] and "2 Mar 2026" in phone["detail"], phone["detail"]
    assert cleaner.public(phone, "")["trash"] and not cleaner.public(phone, "")["safe"]
    with recording(Recorder()) as recorder:
        out = cleaner.delete(phone)
    assert recorder.calls == [["/usr/bin/osascript", "-e", cleaner.TRASH_SCRIPT, phone["paths"][0]]], recorder.calls
    assert out["errors"] == [] and os.path.exists(phone["paths"][0]), "Finder moves it; the recorder stood in"
    with recording(Recorder()):
        cleaner.JOB.update(results=[], done_kb=0)
        cleaner.run_job([phone])
    assert cleaner.history()[0]["result"] == "trashed" and cleaner.history()[0]["what"] == "Ishpuneet's iPhone backup"
    os.chmod(backups, 0)
    rows, notice = cleaner.scan_backups()
    assert rows == [] and "backups" in notice and "Full Disk Access" in notice, notice
    os.chmod(backups, 0o755)
    ipsw = put("Library", "iTunes", "iPhone Software Updates", "iPhone15,2_27.0_Restore.ipsw", kb=200)
    assert named(cleaner.scan_installers())["iPhone15,2_27.0_Restore.ipsw"]["paths"] == [ipsw]

    # --- Homebrew: Homebrew's own commands, run through a stand-in ------------------------------
    brew_home = os.path.join(box, "brew")
    brew = put("bin", "brew", kb=0, root=brew_home)
    os.chmod(brew, 0o755)
    put("Cellar", "libfoo", "1.0", "lib.dylib", kb=300, root=brew_home)
    cleaner.BREWS = (brew,)

    def brew_says(command):
        if "cleanup" in command and "-n" in command:
            return 0, (f"Would remove: {brew_home}/Cellar/node/20.1.0 (2,014 files, 54.3MB)\n"
                       f"Would remove: {cleaner.HOME}/Library/Caches/Homebrew/downloads/abc--node.tar.gz (12.0MB)\n"
                       "==> This operation would free approximately 66.3MB of disk space.\n")
        if "autoremove" in command and "-n" in command:
            return 0, "==> Would autoremove 1 unneeded formula:\nlibfoo\n"
        return 0, ""
    with recording(Recorder(answer=brew_says)) as recorder:
        homebrew = named(cleaner.scan_homebrew())
    old_versions = homebrew["Homebrew old versions"]
    assert old_versions["kb"] == cleaner.size_kb("54.3MB"), "Homebrew's downloads are counted under Dev tools"
    assert old_versions["cmd"][-3:] == [brew, "cleanup", "--prune=all"] and old_versions["paths"] == []
    assert "HOMEBREW_NO_AUTO_UPDATE=1" in old_versions["cmd"]
    assert all("-n" in call for call in recorder.calls), "a scan only ever runs dry runs"
    unneeded = homebrew["Homebrew packages nothing needs"]
    assert unneeded["kb"] >= 300 and "libfoo" in unneeded["detail"] and unneeded["cmd"][-1] == "autoremove"
    assert cleaner.blocked(unneeded, f"{brew_home}/Library/Homebrew/vendor/ruby {brew_home}/Library/Homebrew/brew.rb install x\n")
    with recording(Recorder(answer=brew_says)) as recorder:
        assert cleaner.delete(old_versions)["errors"] == []
    assert recorder.calls == [old_versions["cmd"]]
    cleaner.BREWS = ("/nonexistent/brew",)
    assert cleaner.scan_homebrew() == []

    # --- Docker leftovers, one row each, removed by Docker ----------------------------------------
    docker = put("docker", kb=0, root=os.path.join(box, "fake-docker"))
    os.chmod(docker, 0o755)
    cleaner.DOCKERS = (docker,)
    docker_up = [True]

    def docker_says(command):
        if not docker_up[0]:
            return 1, ""
        if command[1:3] == ["system", "df"] and "-v" not in command:
            return 0, "Images\t3.1GB (50%)\nBuild Cache\t1.2GB\n"
        if command[1:3] == ["ps", "-a"]:
            return 0, ("8a83ec757c0c\tweb-db-1\tpostgres:17\t63MB (virtual 291MB)\tExited (0) 11 days ago\n"
                       "887b83f57d24\tapi-1\tapi:latest\t0B (virtual 288MB)\tExited (137) 9 days ago\n")
        if "-v" in command:
            return 0, json.dumps([{"Name": "old_pgdata", "Links": "0", "Size": "48.4MB"},
                                  {"Name": "live_pgdata", "Links": "1", "Size": "90MB"}])
        return 0, ""
    with recording(Recorder(answer=docker_says, fakes=[docker])) as recorder:
        rows = named(cleaner.scan_docker())
    assert {"Stopped container web-db-1", "Stopped container api-1", "Docker volume old_pgdata"} <= set(rows), rows.keys()
    assert "Docker volume live_pgdata" not in rows, "a volume a container uses stays"
    assert rows["Docker volume old_pgdata"]["group"] == "big" and "database" in rows["Docker volume old_pgdata"]["detail"]
    assert rows["Stopped container api-1"]["tidy"] and rows["Stopped container web-db-1"]["kb"] == cleaner.size_kb("63MB")
    with recording(Recorder(answer=docker_says, fakes=[docker])) as recorder:
        cleaner.delete(rows["Stopped container web-db-1"])
        cleaner.delete(rows["Docker volume old_pgdata"])
    assert recorder.calls == [[docker, "rm", "8a83ec757c0c"], [docker, "volume", "rm", "old_pgdata"]], recorder.calls
    docker_up[0] = False
    with recording(Recorder(answer=docker_says, fakes=[docker])):
        rows, unsure = cleaner.scan_docker()
    assert [r["blocked"] for r in rows] == ["Start Docker to measure this"] * 2, "Docker off: no leftovers listed"
    assert isinstance(unsure, cleaner.Unsure) and unsure == "", "and \"What grew\" is told nothing was measured"

    # --- duplicate files --------------------------------------------------------------------------
    content = os.urandom(2 * 1024 * 1024)
    different = content[:65536] + os.urandom(len(content) - 131072) + content[-65536:]   # same ends, other middle

    def write_copy(path, data, days):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        age(path, days)
        return path
    original = write_copy(os.path.join(cleaner.HOME, "Documents", "dupes", "a.bin"), content, 30)
    older = write_copy(os.path.join(cleaner.HOME, "Desktop", "copy of a.bin"), content, 60)
    newest = write_copy(os.path.join(cleaner.HOME, "Downloads", "a again.bin"), content, 1)
    write_copy(os.path.join(cleaner.HOME, "Documents", "dupes", "b.bin"), different, 5)
    cloud = write_copy(os.path.join(cleaner.HOME, "Documents", "dupes", "in iCloud.bin"), content, 5)
    for hidden in (("Documents", "Photos.photoslibrary", "originals", "a.bin"), ("Documents", "proj", "node_modules", "a.bin"),
                   ("Documents", "proj", ".git", "objects", "a.bin"), ("Documents", "Tool.app", "Contents", "a.bin")):
        write_copy(os.path.join(cleaner.HOME, *hidden), content, 5)
    os.symlink(original, os.path.join(cleaner.HOME, "Desktop", "link to a.bin"))
    write_copy(os.path.join(cleaner.HOME, "Documents", "dupes", "small-1.txt"), b"x" * 1000, 5)
    write_copy(os.path.join(cleaner.HOME, "Documents", "dupes", "small-2.txt"), b"x" * 1000, 5)
    placeholder = os.stat(cloud).st_ino
    cleaner.dataless = lambda info: info.st_ino == placeholder     # stands in for SF_DATALESS, which only iCloud sets
    cleaner.find_dupes()
    shown = cleaner.dupes_public()
    assert len(shown["groups"]) == 1 and not shown["running"] and not shown["capped"], shown
    group = shown["groups"][0]
    assert sorted(f["name"] for f in group["files"]) == ["a again.bin", "a.bin", "copy of a.bin"], group
    assert group["files"][0]["name"] == "a again.bin" and group["keep"] == group["files"][0]["id"], "keeps the newest"
    assert shown["done"] == 3 * len(content) + 0 or shown["done"] >= 3 * len(content)
    reads = []
    real_read = cleaner.read_hash
    cleaner.read_hash = lambda path, size, whole, progress=None: reads.append((path, whole)) or real_read(path, size, whole, progress)
    cleaner.find_dupes()
    assert reads == [] and len(cleaner.DUPES["groups"]) == 1, ("a second search reads nothing it already knows", reads[:4])
    os.remove(os.path.join(cleaner.state_dir(), "hashes.json"))
    cleaner.DUPE_CAP = 3 * 1024 * 1024
    cleaner.find_dupes()
    assert cleaner.DUPES["capped"] and sum(1 for _, whole in reads if whole) == 1, "stops reading at the cap"
    cleaner.read_hash = real_read
    cleaner.DUPE_CAP = 8 * 1000 ** 3
    cleaner.find_dupes()
    group = cleaner.dupes_public()["groups"][0]
    with recording(Recorder()) as recorder:
        out = cleaner.trash_extras([(group["id"], group["keep"])])
    moved = sorted(call[3] for call in recorder.calls)
    assert moved == sorted([original, older]) and out["moved"] == 2 and newest not in moved, (out, moved)
    assert all(os.path.exists(p) for p in (original, older, newest)), "the recorder stood in for Finder"
    assert cleaner.DUPES["groups"] == {}, "a group with one copy left is done"
    cleaner.find_dupes()
    group = cleaner.dupes_public()["groups"][0]
    keep_path = next(f for f in group["files"] if f["id"] == group["keep"])["name"]
    os.utime(newest, (time.time(), time.time() + 5))                                      # the copy to keep changed
    with recording(Recorder()) as recorder:
        out = cleaner.trash_extras([(group["id"], group["keep"])])
    assert recorder.calls == [] and out["moved"] == 0 and "changed" in out["problems"][0], out
    with recording(Recorder()) as recorder:
        out = cleaner.trash_extras([("no-such-group", "x")])
    assert recorder.calls == [] and out["problems"], out

    # --- startup items ----------------------------------------------------------------------------
    agents = os.path.join(cleaner.HOME, "Library", "LaunchAgents")
    system_dir = os.path.join(box, "Library", "LaunchDaemons")
    for folder, label, program in ((agents, "com.example.updater", "/Applications/Example.app/Contents/MacOS/Updater"),
                                   (agents, "local.headroom.door", "/Applications/headroom.app/Contents/MacOS/headroom-app"),
                                   (agents, "homebrew.mxcl.redis", "/opt/homebrew/opt/redis/bin/redis-server"),
                                   (agents, "com.apple.hidden", "/usr/libexec/thing"),
                                   (agents, "com.keeper.updater", "/Library/Keeper/updater"),          # same maker, not the app
                                   (agents, "com.keeper.keep.helper", "/Library/Keeper/helper"),       # the app's own
                                   (system_dir, "com.vendor.daemon", "/Library/Vendor/daemon")):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, label + ".plist"), "wb") as handle:
            plistlib.dump({"Label": label, "ProgramArguments": [program]}, handle)
    real_dirs = cleaner.startup_dirs
    cleaner.startup_dirs = lambda: ((agents, "user"), (system_dir, "system"))

    def launchd_says(command):
        if command[1:] == ["list"]:
            return 0, "PID\tStatus\tLabel\n4242\t0\thomebrew.mxcl.redis\n-\t0\tlocal.headroom.door\n"
        if command[1] == "print-disabled":
            return 0, 'disabled services = {\n\t"com.example.updater" => disabled\n\t"local.headroom.door" => enabled\n}\n'
        return 0, ""
    with recording(Recorder(answer=launchd_says)) as recorder:
        items = {i["label"]: i for i in cleaner.list_startup()}
    assert {"com.example.updater", "com.vendor.daemon", "homebrew.mxcl.redis", "local.headroom.door"} <= set(items)
    assert "com.apple.hidden" not in items, "Apple's own are left out unless asked for"
    assert items["homebrew.mxcl.redis"]["state"] == "running" and items["local.headroom.door"]["state"] == "on"
    assert items["com.example.updater"]["state"] == "off" and items["com.example.updater"]["app"] == "Example"
    assert items["local.headroom.door"]["own"] == "headroom" and "headroom.localhost" in items["local.headroom.door"]["explain"]
    assert items["com.vendor.daemon"]["scope"] == "system" and items["homebrew.mxcl.redis"]["app"] == "Homebrew"
    assert items["com.keeper.keep.helper"]["app"] == "Keeper" and items["com.keeper.updater"]["app"] == "", \
        "an app is named only when the agent is surely its own"
    assert all(call[0] == "/bin/launchctl" and call[1] in ("list", "print-disabled") for call in recorder.calls)
    with recording(Recorder(answer=launchd_says)):
        assert "com.apple.hidden" in {i["label"] for i in cleaner.list_startup(apple=True)}
        items = {i["label"]: i for i in cleaner.list_startup()}
    uid = os.getuid()
    with recording(Recorder(answer=launchd_says)) as recorder:
        assert cleaner.switch_startup(items["homebrew.mxcl.redis"]["id"], False) == ""
        assert "every user" in cleaner.switch_startup(items["com.vendor.daemon"]["id"], False)
        assert cleaner.switch_startup(items["com.example.updater"]["id"], True) == ""
        assert cleaner.switch_startup("no-such-item", True)
    assert recorder.calls == [
        ["/bin/launchctl", "bootout", f"gui/{uid}/homebrew.mxcl.redis"],
        ["/bin/launchctl", "disable", f"gui/{uid}/homebrew.mxcl.redis"],
        ["/bin/launchctl", "enable", f"gui/{uid}/com.example.updater"],
        ["/bin/launchctl", "bootstrap", f"gui/{uid}", os.path.join(agents, "com.example.updater.plist")]], recorder.calls
    assert cleaner.history()[1]["result"] == "turned-off" and os.path.exists(os.path.join(agents, "homebrew.mxcl.redis.plist"))
    cleaner.startup_dirs = real_dirs

    # --- reminders: the agent's check, with a stand-in for statfs ---------------------------------
    assert cleaner.settings() == {"alert": True, "weekly": True, "alert_gb": 20, "trash": False}, \
        "reminders on by default, below 20 GB; Move to Trash off"
    free = [12e9 / 1024]
    cleaner.disk_free_kb = lambda: int(free[0])
    measured = [3.2e9 / 1024]
    cleaner.measure_safe = lambda: int(measured[0])
    monday = time.mktime((2026, 10, 12, 11, 0, 0, 0, 0, -1))
    assert cleaner.watch_check(monday) == [("Only 12.0 GB free", "Open headroom to make room.")]
    free[0] = 11.5e9 / 1024
    assert cleaner.watch_check(monday + 600) == [], "no nagging: once in six hours"
    free[0] = 6.5e9 / 1024
    assert cleaner.watch_check(monday + 1800)[0][0] == "Only 6.5 GB free", "unless 5 GB more went"
    free[0] = 6.4e9 / 1024
    assert cleaner.watch_check(monday + 3600) == []
    assert len(cleaner.watch_check(monday + 1800 + 6 * 3600 + 1)) == 1, "and again after six hours"
    free[0] = 80e9 / 1024
    assert cleaner.watch_check(monday + 7 * 86400 - 3600) == [], "plenty of room, and the week isn't up"
    assert cleaner.watch_check(monday + 7 * 86400) == [("Safe clean would free 3.2 GB",
                                                        "Open headroom to clear it. Nothing goes until you say so.")]
    assert cleaner.watch_check(monday + 7 * 86400 + 1800) == [], "weekly means weekly"
    measured[0] = 100e6 / 1024
    assert cleaner.watch_check(monday + 14 * 86400) == [], "under 500 MB isn't worth a notification"
    night = time.mktime((2026, 11, 3, 3, 0, 0, 0, 0, -1))
    measured[0] = 3.2e9 / 1024
    assert cleaner.watch_check(night) == [], "a week is up, but not at three in the morning"
    cleaner.save_state("settings.json", {"alert": False, "weekly": False, "alert_gb": 20})
    free[0] = 1e9 / 1024
    assert cleaner.watch_check(night + 7 * 3600) == []
    samples = cleaner.space_history()["samples"]
    assert len(samples) == 11 and samples[0] == [round(monday), int(12e9 / 1024)], samples[:2]
    cleaner.save_state("settings.json", {"alert": True, "weekly": True, "alert_gb": 20})
    printed = io.StringIO()
    real_argv, real_nice = sys.argv, os.nice
    sys.argv, os.nice = ["cleaner.py", "--watch"], lambda n: 0
    cleaner.save_state("watch.json", {})
    free[0] = 1.5e9 / 1024
    with contextlib.redirect_stdout(printed):
        cleaner.main()
    sys.argv, os.nice = real_argv, real_nice
    assert printed.getvalue() == "notify\tOnly 1.5 GB free\tOpen headroom to make room.\n", printed.getvalue()
    # The agent itself, written and loaded by the server, and only for an installed app.
    watch_plist = os.path.join(agents, cleaner.WATCH_LABEL + ".plist")
    with recording(Recorder(returncode=1)) as recorder:
        cleaner.sync_watch()
    assert recorder.calls == [] and not os.path.exists(watch_plist), "no app launcher here: left alone"
    cleaner.launcher_program = lambda: "/Applications/headroom.app/Contents/MacOS/headroom-app"
    with recording(Recorder(returncode=1)) as recorder:
        cleaner.sync_watch()
    with open(watch_plist, "rb") as handle:
        agent_plist = plistlib.load(handle)
    assert agent_plist["ProgramArguments"] == ["/Applications/headroom.app/Contents/MacOS/headroom-app", "--watch"]
    assert agent_plist["StartInterval"] == 1800 and agent_plist["ProcessType"] == "Background"
    assert [c[1] for c in recorder.calls] == ["bootout", "bootstrap"], recorder.calls
    cleaner.save_state("settings.json", {"alert": False, "weekly": False, "alert_gb": 20})
    with recording(Recorder()) as recorder:
        cleaner.sync_watch()
    assert [c[1] for c in recorder.calls] == ["bootout"] and not os.path.exists(watch_plist), "both off: agent gone"
    cleaner.save_state("settings.json", {"alert": True, "weekly": True, "alert_gb": 20})

    # --- space history: deletes close together are one mark ---------------------------------------
    marks = cleaner.space_history()["marks"]
    assert marks and all(kb > 0 and n >= 1 for _, kb, n in marks), marks

    # --- Xcode: device support by age, previews, and nothing listed twice ---------------------------
    xcode = os.path.join(cleaner.HOME, "Library", "Developer", "Xcode")
    for name in ("iPhone14,2 15.7 (19H12)", "16.4 (20E247)", "iPhone15,2 17.4.1 (21E236)", "iPad13,4 17.4.1 (21E236)",
                 "iPhone16,1 18.0 (22A3354)"):
        put("iOS DeviceSupport", name, "Symbols", "dyld", root=xcode)
    put("watchOS DeviceSupport", "Watch6,1 9.4 (20T253)", "Symbols", "dyld", root=xcode)
    put("UserData", "Previews", "Simulator Devices", "ABCD", "data", root=xcode)
    xcode_theme = put("UserData", "FontAndColorThemes", "mine.xccolortheme", kb=1, root=xcode)
    put("Library", "Developer", "CoreSimulator", "Caches", "dyld", "x")
    assert cleaner.support_version("iPhone15,2 17.4.1 (21E236)") == (17, 4, 1) and cleaner.support_version("odd") == ()
    dev = named(cleaner.scan_dev_tools())
    old_support, new_support = dev["Old iOS device support"], dev["Newest iOS device support"]
    assert sorted(os.path.basename(p) for p in old_support["paths"]) == ["16.4 (20E247)", "iPhone14,2 15.7 (19H12)"]
    assert sorted(os.path.basename(p) for p in new_support["paths"]) == [
        "iPad13,4 17.4.1 (21E236)", "iPhone15,2 17.4.1 (21E236)", "iPhone16,1 18.0 (22A3354)"], "the two newest versions"
    assert new_support["recent"] and "leaves it out" in new_support["note"] and not old_support["recent"]
    assert "copies them again" in old_support["detail"] and "iOS DeviceSupport" in old_support["detail"]
    assert "Old watchOS device support" not in dev and dev["Newest watchOS device support"]["recent"], "one version: in use"
    assert "Xcode device support files" not in dev, "the one row for all of it is gone"
    previews = dev["Xcode previews"]
    assert [os.path.basename(p) for p in previews["paths"]] == ["Simulator Devices"] and "open a preview" in previews["detail"]
    assert cleaner.blocked(previews, "/Applications/Xcode.app/Contents/MacOS/Xcode\n") == "Quit Xcode first"
    assert "Simulator caches" in dev and not any("rchive" in name for name in dev), "archives are rows under Backups already"
    assert sum(name.startswith("Xcode archive") for name in named(cleaner.scan_big())) == 1
    assert cleaner.delete(old_support)["errors"] == []
    assert sorted(os.listdir(os.path.join(xcode, "iOS DeviceSupport"))) == sorted(os.path.basename(p) for p in new_support["paths"])
    assert os.path.exists(xcode_theme), "Xcode's own settings stay"

    # --- big .git folders: one `git gc`, never in the middle of a rebase, nothing removed by headroom --
    if GIT:
        cleaner.GIT_MIN_KB = 300        # a new repository's .git is about 100 KB of sample hooks
        heavy = os.path.join(cleaner.HOME, "Desktop", "heavy")
        put("Desktop", "heavy", "package.json", kb=1)
        put("Desktop", "heavy", "node_modules", "dep", "index.js")
        real_run([GIT, "init", "-q", heavy])
        for n in range(6):
            put("Desktop", "heavy", "assets", f"blob{n}.bin", kb=100)
        real_run([GIT, "-C", heavy, "add", "assets"])
        dangling = real_run([GIT, "-C", heavy, "hash-object", os.path.join(heavy, "assets", "blob0.bin")]).stdout.strip()
        real_run([GIT, "-C", heavy, "rm", "-r", "-q", "--cached", "assets"])       # nothing points at them now
        store = os.path.join(heavy, ".git")
        found = [row for row in cleaner.scan_builds()[0] if row["git"]]
        assert [row["name"] for row in found] == ["heavy git history"], "only a .git that is big"
        packing = found[0]
        safe_git = ["/usr/bin/env", "GIT_OPTIONAL_LOCKS=0", "GIT_TERMINAL_PROMPT=0", "GIT_NO_LAZY_FETCH=1", GIT,
                    "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never", "-C", heavy]
        assert packing["cmd"] == safe_git + ["gc", "--quiet"] and packing["group"] == "dev", packing["cmd"]
        assert not any("prune" in part for part in packing["cmd"]), "the repository's own grace period for pruning stands"
        assert packing["paths"] == [] and packing["kb"] == 0, "no path for headroom to remove, and no made-up saving"
        assert "/usr/bin/git" != GIT and ".git folder" in packing["detail"] and cleaner.GC_KEEPS in packing["detail"]
        shown = cleaner.public(packing, "")
        assert shown["action"] == "git gc" and shown["kb"] == 0 and shown["blocked"] == "" and not shown["safe"]
        assert shown["weighs"] >= 300, "sorted by how big .git is, since it has no size of its own"
        assert "dev" in cleaner.SOURCES["builds"][0]
        gc_ran = lambda recorder: [call for call in recorder.calls if "gc" in call]
        for busy, word in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge"),
                           ("index.lock", "lock"), ("gc.pid", "lock"), (os.path.join("worktrees", "wt", "rebase-merge"), "rebase"),
                           (os.path.join("refs", "heads", "main.lock"), "lock"), ("commondir", "shares")):
            os.makedirs(os.path.join(store, busy))
            assert word in cleaner.blocked(packing, ""), busy
            with recording(Recorder(never=("/bin/rm",))) as recorder:
                out = cleaner.delete(packing)
            assert recorder.calls == [] and word in out["errors"][0], (busy, "git must not be started at all", recorder.calls)
            shutil.rmtree(os.path.join(store, busy.split(os.sep)[0]) if busy.split(os.sep)[0] != "refs"
                          else os.path.join(store, busy))
        # A stray file among the linked worktrees is not a worktree, and blocks nothing.
        put("worktrees", "notes.txt", kb=1, root=store)
        assert cleaner.blocked(packing, "") == "", cleaner.blocked(packing, "")
        shutil.rmtree(os.path.join(store, "worktrees"))
        # Objects or refs kept somewhere else: packing here could strand the other repository.
        os.rename(os.path.join(store, "refs"), os.path.join(store, "refs-real"))
        os.symlink(os.path.join(store, "refs-real"), os.path.join(store, "refs"))
        assert "shares" in cleaner.blocked(packing, "")
        os.remove(os.path.join(store, "refs"))
        os.rename(os.path.join(store, "refs-real"), os.path.join(store, "refs"))
        # A program the repository names for gc, and a clone that would fetch what it lacks: refused.
        for setting, word in (("gc.recentObjectsHook", "program of its own"), ("extensions.partialClone", "partial clone"),
                              ("remote.origin.promisor", "partial clone")):
            real_run([GIT, "-C", heavy, "config", setting, "true"])
            assert word in cleaner.blocked(packing, ""), setting

            def settings_for_real(command):         # reading them runs nothing; anything else is only recorded
                done = real_run(command) if "config" in command else None
                return (done.returncode, done.stdout) if done else (0, "")
            with recording(Recorder(answer=settings_for_real, never=("/bin/rm",))) as recorder:
                out = cleaner.delete(packing)
            assert gc_ran(recorder) == [] and word in out["errors"][0], "the settings are read, gc is not run"
            real_run([GIT, "-C", heavy, "config", "--unset", setting])
        assert cleaner.blocked(packing, "") == ""
        with recording(Recorder(never=("/bin/rm",))) as recorder:
            assert "Move to Trash is on" in cleaner.delete(packing, to_trash=True)["errors"][0]
        assert gc_ran(recorder) == [], "a command can't go to the Trash, so it doesn't run in that mode"
        with recording(Recorder(never=("/bin/rm",))) as recorder:
            assert cleaner.delete(packing)["errors"] == []
        assert gc_ran(recorder) == [packing["cmd"]], "one gc, and nothing else touches .git"
        assert all(call[:len(safe_git)] == safe_git for call in recorder.calls), "every git call goes the guarded way"
        # A .git that is a link, or the file a linked worktree has, is never handed to git.
        os.rename(store, store + "-real")
        os.symlink(store + "-real", store)
        assert "leaves it alone" in cleaner.git_busy(heavy) and not [r for r in cleaner.scan_builds()[0] if r["git"]]
        os.remove(store)
        os.rename(store + "-real", store)
        # For real. An object nothing points at is younger than git's grace period, so it stays:
        # headroom does not tell git to prune now.
        cleaner.JOB.update(results=[], done_kb=0)
        cleaner.run_job([packing])
        done = cleaner.JOB["results"][0]
        assert done["errors"] == [] and not done["trashed"], done
        assert re.fullmatch(r"\.git was \d+ KB and is \d+ KB now\.", done["note"]), done["note"]
        assert real_run([GIT, "-C", heavy, "cat-file", "-e", dangling]).returncode == 0, "inside the grace period: kept"
        entry = cleaner.history()[0]
        assert entry["result"] == "compacted" and entry["note"] == done["note"] and entry["paths"] == [], entry
        assert entry["command"] == "git -C ~/Desktop/heavy gc", entry["command"]
        # When the repository itself says to prune at once, git does, and the result says what that freed.
        real_run([GIT, "-C", heavy, "config", "gc.pruneExpire", "now"])
        cleaner.JOB.update(results=[], done_kb=0)
        cleaner.run_job([packing])
        done = cleaner.JOB["results"][0]
        assert done["errors"] == [] and done["freed_kb"] >= 500, done
        assert os.path.isfile(os.path.join(store, "HEAD")) and os.path.exists(os.path.join(heavy, "assets", "blob0.bin"))

        # Two repositories with one folder name are two rows, each said with its parent.
        for parent in ("one", "two"):
            twin = os.path.join(cleaner.HOME, "Documents", parent, "app")
            put("Documents", parent, "app", "package.json", kb=1)
            put("Documents", parent, "app", "node_modules", "dep", "index.js")
            real_run([GIT, "init", "-q", twin])
            put("Documents", parent, "app", ".git", "padding", kb=400)
        twins = [row for row in cleaner.scan_builds()[0] if row["git"] and row["name"].endswith("app git history")]
        assert sorted(row["name"] for row in twins) == ["one/app git history", "two/app git history"], [r["name"] for r in twins]
        assert len({row["id"] for row in twins}) == 2 and len({row["cmd"][-3] for row in twins}) == 2, "one id each"
        for parent in ("one", "two"):
            shutil.rmtree(os.path.join(cleaner.HOME, "Documents", parent))

        # A repository that names programs for git to run: an unpacked download can hold a ready-made
        # .git. Neither the scan (git ls-files) nor git gc starts any of them.
        marks = os.path.join(box, "marks")
        os.makedirs(marks)
        crafted = os.path.join(cleaner.HOME, "Downloads", "crafted")
        put("Downloads", "crafted", "package.json", kb=1)
        put("Downloads", "crafted", "node_modules", "dep", "index.js")
        put("Downloads", "crafted", "a.txt", kb=1)
        real_run([GIT, "init", "-q", crafted])
        real_run([GIT, "-C", crafted, "add", "a.txt"])
        real_run([GIT, "-C", crafted, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one"])
        real_run([GIT, "-C", crafted, "update-index", "--fsmonitor"])
        put("Downloads", "crafted", ".git", "padding", kb=400)
        for name in ("fsmonitor.sh", os.path.join("hooks", "reference-transaction"), os.path.join("hooks", "pre-auto-gc")):
            script = os.path.join(crafted, ".git", name)
            with open(script, "w") as handle:
                handle.write(f"#!/bin/sh\necho ran >> '{marks}/{os.path.basename(name)}'\n")
            os.chmod(script, 0o755)
        with open(os.path.join(crafted, ".git", "config"), "a") as handle:
            handle.write(f"[core]\n\tfsmonitor = {os.path.join(crafted, '.git', 'fsmonitor.sh')}\n")
        rows = [row for row in cleaner.scan_builds()[0] if row["git"] == crafted]
        assert len(rows) == 1 and os.listdir(marks) == [], ("the scan ran a program the repository named", os.listdir(marks))
        cleaner.JOB.update(results=[], done_kb=0)
        cleaner.run_job(rows)
        assert cleaner.JOB["results"][0]["errors"] == [] and os.listdir(marks) == [], \
            ("git gc ran a program the repository named", os.listdir(marks), cleaner.JOB["results"])
        shutil.rmtree(crafted)

    # --- screenshots and screen recordings: only macOS's own, only in its one folder ----------------
    desktop = os.path.join(cleaner.HOME, "Desktop")
    month = lambda days: time.strftime("%B %Y", time.localtime(time.time() - days * cleaner.DAY))

    def shot(name, days, folder=desktop, kb=40):
        made = put(name, kb=kb, root=folder)
        age(made, days)
        return made
    old_shots = [shot("Screenshot 2026-08-14 at 10.32.11.png", 45), shot("Screenshot 2026-08-14 at 10.32.11 AM (2).png", 45),
                 shot("Screen Shot 2020-01-01 at 10.00.00 AM.png", 45)]
    older_shot = shot("Screenshot 2026-06-01 at 08.00.00.jpg", 100)
    new_shot = shot("Screenshot 2026-10-10 at 09.15.00.png", 2)
    old_clip = shot("Screen Recording 2026-08-14 at 11.00.00.mov", 45, kb=300)
    renamed = shot("invoice proof.png", 45)                                   # only the metadata flag says so
    custom = shot("Capture 2026-08-14 at 10.32.11.png", 45)                   # the name set in macOS's settings
    not_shots = [shot("holiday.png", 45), shot("screenshot.png", 45), shot("Screenshot of the plan.png", 45),
                 shot("Screenshot 2026-08-14 at 10.32.11.txt", 45), shot("notes.mov", 45),
                 shot("Screenshot 2026-08-14 at 10.32.12.png", 45, folder=os.path.join(desktop, "kept")),   # a subfolder
                 shot("Screenshot 2026-08-14 at 10.32.11.png", 45, folder=os.path.join(cleaner.HOME, "Documents")),
                 put("Screenshot 2026-08-14 at 10.32.13.png", "inside.png", kb=40, root=desktop)]          # a folder
    os.symlink(kept_outside, os.path.join(desktop, "Screenshot 2026-08-14 at 10.32.14.png"))               # a link out
    cloud_shot = shot("Screenshot 2026-08-14 at 10.32.15.png", 45)                                         # only in iCloud
    not_shots.append(cloud_shot)
    earlier_dataless, placeholders = cleaner.dataless, {os.stat(cloud_shot).st_ino}
    cleaner.dataless = lambda info: info.st_ino in placeholders         # stands in for SF_DATALESS, which only iCloud sets
    chosen, flagged, garbled = {}, {renamed}, []

    def capture_says(command):
        if command[0] == "/usr/bin/defaults":
            assert command[1:3] == ["read", "com.apple.screencapture"], command
            return (0, chosen[command[3]] + "\n") if command[3] in chosen else (1, "")
        if command[0] == "/usr/bin/mdls":
            if garbled:
                return 0, "1\0" * (len(command) - 3)      # more answers than files
            return 0, "\0".join("1" if p in flagged else "(null)" for p in command[4:])
        return 0, ""

    def scan_shots():
        with recording(Recorder(answer=capture_says, fakes=["/usr/bin/defaults"], never=("/bin/rm",))) as recorder:
            rows, notice = cleaner.scan_screenshots()
        return named(rows), notice, recorder
    shots, notice, recorder = scan_shots()
    assert sorted(shots) == sorted([f"Screenshots from {month(45)}", f"Screenshots from {month(100)}",
                                    "Screenshots from the last 30 days", f"Screen recordings from {month(45)}"]), sorted(shots)
    assert notice == "" and sorted(shots[f"Screenshots from {month(45)}"]["paths"]) == sorted(old_shots + [renamed])
    assert shots[f"Screenshots from {month(100)}"]["paths"] == [older_shot]
    assert shots[f"Screen recordings from {month(45)}"]["paths"] == [old_clip], "recordings get their own rows"
    assert shots[f"Screenshots from {month(45)}"]["detail"] == "4 files · ~/Desktop"
    recent = shots["Screenshots from the last 30 days"]
    assert recent["paths"] == [new_shot] and recent["recent"] and "leaves it out" in recent["note"]
    offered = {p for row in shots.values() for p in row["paths"]}
    assert custom not in offered and not offered & set(not_shots), "nothing a person named, nothing outside the folder"
    assert not any(os.path.islink(p) or os.path.isdir(p) for p in offered)
    for row in shots.values():
        assert row["group"] == "shots" and row["trash"] and not row["cmd"], row
        assert not cleaner.public(row, "")["safe"], "Safe clean never picks them"
        assert row["recent"] == (row is recent) and (row["note"] == "") == (row is not recent), "the blurb says where they go"
    # Newest first: the last 30 days, then month by month, a month's recordings beside its screenshots.
    ranked = sorted(shots.values(), key=lambda row: -cleaner.public(row, "")["rank"])
    assert ranked[0] is recent and ranked[-1]["name"] == f"Screenshots from {month(100)}", [row["name"] for row in ranked]
    assert shots[f"Screen recordings from {month(45)}"]["rank"] == shots[f"Screenshots from {month(45)}"]["rank"]
    assert "screenshots" not in cleaner.SAFE_SOURCES and "shots" not in cleaner.SAFE_GROUPS
    asked = [p for call in recorder.calls if call[0] == "/usr/bin/mdls" for p in call[4:]]
    assert asked and all(os.path.dirname(p) == desktop for p in asked), "Spotlight is asked about that folder's files only"
    # They always go to the Trash through Finder, whatever the Move to Trash switch says.
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        out = cleaner.delete(shots[f"Screenshots from {month(100)}"])
    assert recorder.calls == [["/usr/bin/osascript", "-e", cleaner.TRASH_SCRIPT, older_shot]] and out["errors"] == []
    assert os.path.exists(older_shot), "the recorder stood in for Finder"
    # Only the very files the scan looked at: one that has since become a link or a folder stays.
    swapped = shots[f"Screenshots from {month(45)}"]
    os.rename(old_shots[0], old_shots[0] + ".aside")
    os.symlink(kept_outside, old_shots[0])
    os.rename(old_shots[1], old_shots[1] + ".aside")
    os.makedirs(old_shots[1])
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        out = cleaner.delete(swapped)
    assert recorder.calls == [["/usr/bin/osascript", "-e", cleaner.TRASH_SCRIPT] + sorted([old_shots[2], renamed])], recorder.calls
    assert len(out["errors"]) == 2 and all("not what the scan found" in why for why in out["errors"]), out
    os.remove(old_shots[0])
    os.rmdir(old_shots[1])
    for moved in old_shots[:2]:
        os.rename(moved + ".aside", moved)
    # The name chosen in macOS's settings counts once it is set there.
    chosen["name"] = "Capture"
    assert custom in {p for row in scan_shots()[0].values() for p in row["paths"]}
    del chosen["name"]
    # Spotlight answering with anything but one answer per file: nothing it says is used, names decide.
    garbled.append(True)
    rows, notice, _ = scan_shots()
    offered = {p for row in rows.values() for p in row["paths"]}
    garbled.clear()
    assert renamed not in offered and not offered & set(not_shots) and set(old_shots) <= offered
    assert "went by their names" in notice and "another language" in notice, notice
    # The same when Spotlight knows nothing at all about files that are named like screenshots.
    flagged.clear()
    assert "went by their names" in scan_shots()[1]
    flagged.add(renamed)
    # Another folder chosen in macOS's settings: only that one is looked at.
    elsewhere = shot("Screenshot 2026-08-14 at 10.32.11.png", 45, folder=os.path.join(cleaner.HOME, "Pictures", "Shots"))
    chosen["location"] = "~/Pictures/Shots"
    assert {p for row in scan_shots()[0].values() for p in row["paths"]} == {elsewhere}
    # A folder outside the home folder, or reached through a link, is left alone and said so.
    shot("Screenshot 2026-08-14 at 10.32.11.png", 45, folder=outside)
    for place in (outside, os.path.join(desktop, "way-out"), cleaner.HOME):
        chosen["location"] = place
        rows, notice, recorder = scan_shots()
        assert rows == {} and "leaves them alone" in notice, place
        assert not any(call[0] == "/usr/bin/mdls" for call in recorder.calls), place
    del chosen["location"]

    # --- Move cleaned files to the Trash: Finder moves every path, and nothing is removed for good --
    switch_paths = [os.path.dirname(put("Library", "Caches", "com.switch.one", "blob", kb=100)),
                    os.path.dirname(put("Library", "Caches", "com.switch.two", "blob", kb=100))]
    switch_link = os.path.join(cleaner.HOME, "Library", "Caches", "com.switch.link")
    os.symlink(os.path.join(outside, "precious"), switch_link)
    switched = cleaner.item("apps", "switch cache", switch_paths + [switch_link], 200)
    finder = ["/usr/bin/osascript", "-e", cleaner.TRASH_SCRIPT]
    assert cleaner.public(switched, "", True)["blocked"] == "" and not switched["trash"]
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        out = cleaner.delete(switched, to_trash=True)
    assert recorder.calls == [finder + switch_paths], ("one call to Finder for the row, not one a path", recorder.calls)
    assert out["errors"] == [], "a link among them is left where it is, without failing the row every time"
    assert all(os.path.exists(p) for p in switch_paths) and os.path.islink(switch_link) and os.path.exists(kept_outside)
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        assert "is a link" in cleaner.finder_trash(switch_link) and recorder.calls == [], "never handed to Finder, which follows it"
        assert "is a link" in cleaner.finder_trash_many(switch_paths + [switch_link])[switch_link]
    assert recorder.calls == [finder + switch_paths]
    # A hundred paths to a call, an answer for each: 0 when it moved, else Finder's error number.
    many = [os.path.join(cleaner.HOME, "Library", "Caches", "com.switch.many", f"f{n:03}") for n in range(230)]
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        whys = cleaner.finder_trash_many(many[:3])
        assert cleaner.delete(cleaner.item("apps", "many", many, 100), to_trash=True)["errors"] == []
    assert whys == dict.fromkeys(many[:3], "") and [len(call) - 3 for call in recorder.calls] == [3, 100, 100, 30]
    assert [p for call in recorder.calls[1:] for p in call[3:]] == many
    for said, expected in (("0\n-128\n0\n", ["", "You cancelled, so it stayed.", ""]),
                           ("0\n-1700\n-10010\n", ["", "It was already gone.", "Finder could not move it (error -10010)."]),
                           ("0\n0\n", ["Finder could not move it."] * 3),            # not an answer for each: none is believed
                           ("", ["Finder could not move it."] * 3)):
        with recording(Recorder(answer=lambda command, said=said: (0, said or "\n"), never=("/bin/rm",))):
            assert list(cleaner.finder_trash_many(many[:3]).values()) == expected, said
    with recording(Recorder(1, "execution error: boom (-99)", never=("/bin/rm",))):
        assert list(cleaner.finder_trash_many(many[:2]).values()) == ["execution error: boom (-99)."] * 2, "said for every path"
    # What Finder may not move fails with a reason. It is never removed some other way.
    stray = cleaner.item("apps", "stray", [os.path.join(outside, "precious")], 100)
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        out = cleaner.delete(stray, to_trash=True)
    assert recorder.calls == [] and "outside the folders" in out["errors"][0] and os.path.exists(kept_outside)
    with recording(Recorder(1, "execution error: Not authorized to send Apple events to Finder. (-1743)",
                            never=("/bin/rm",))) as recorder:
        out = cleaner.delete(cleaner.item("apps", "many", many, 100), to_trash=True)
    assert out["errors"] == [cleaner.NOT_ALLOWED] and len(recorder.calls) == 1, "asked once, not batch after batch"
    assert "isn't allowed" in cleaner.NOT_ALLOWED and all(os.path.exists(p) for p in switch_paths)
    # Stop is asked between batches, before the next one: a row that had gone already is not "partly".
    cleaner.JOB["stopping"] = True
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        whole = cleaner.delete(switched, to_trash=True)
        cut_short = cleaner.delete(cleaner.item("apps", "many", many, 100), to_trash=True)
    cleaner.JOB["stopping"] = False
    assert whole["errors"] == [] and cut_short["errors"] == ["Stopped before all of it was moved."], (whole, cut_short)
    assert [len(call) - 3 for call in recorder.calls] == [2, 100], "the batch under way finishes, the next never starts"
    # Rows only a command can clear, and the Trash itself, stay put while the switch is on.
    put(".Trash", "junk.bin", kb=100)
    in_trash = named(cleaner.scan_trash()[0])["Trash"]
    for row, why in ((old_versions, "Can't go to the Trash"), (in_trash, "It is the Trash already")):
        assert why in cleaner.public(row, "", True)["blocked"] and cleaner.public(row, "")["blocked"] == ""
        assert "while Move to Trash is on" in cleaner.public(row, "", True)["blocked"]
        with recording(Recorder(never=("/bin/rm",))) as recorder:
            out = cleaner.delete(row, to_trash=True)
        assert recorder.calls == [] and why in out["errors"][0] and out["freed_kb"] == 0, (row["name"], recorder.calls)
    assert os.path.exists(os.path.join(cleaner.HOME, ".Trash", "junk.bin"))
    # The job records it as moved, and a row that could not go as failed.
    with recording(Recorder(never=("/bin/rm",))):
        cleaner.JOB.update(results=[], done_kb=0)
        cleaner.run_job([cleaner.item("apps", "switch one", switch_paths[:1], 100), old_versions], True)
    assert [r["trashed"] for r in cleaner.JOB["results"]] == [True, True]
    assert [e["result"] for e in cleaner.history()[:2]] == ["failed", "trashed"], cleaner.history()[:2]
    # Off, the same row is removed as before.
    assert cleaner.delete(cleaner.item("apps", "switch one", switch_paths[:1], 100))["errors"] == []
    assert not os.path.exists(switch_paths[0]) and os.path.exists(switch_paths[1])
    # The switch is a file. One that is there but can't be read says nothing about where things
    # should go, and the page's API refuses to delete on a guess (further down, over HTTP).
    settings_file = os.path.join(cleaner.state_dir(), "settings.json")
    was = open(settings_file).read()
    assert not cleaner.settings_broken()
    for broken in ("{", "[]", '{"trash": "yes"}', ""):
        with open(settings_file, "w") as handle:
            handle.write(broken)
        assert cleaner.settings_broken(), broken
    with open(settings_file, "w") as handle:
        handle.write(was)
    os.rename(settings_file, settings_file + ".aside")
    assert not cleaner.settings_broken(), "no file at all is the first launch: the switch is off"
    os.rename(settings_file + ".aside", settings_file)

    # --- browser copies a scripted Chrome leaves behind: the unused ones, and nothing else in X ------
    x = cleaner.clone_root()
    assert x == os.path.join(box, "X"), "the X folder beside the temp folder"

    def clone(ident, name):
        folder = os.path.join(x, ident + ".code_sign_clone", name)
        return folder, put("Browser.app", "Contents", "MacOS", "browser", kb=200, root=folder)
    idle = [clone("com.google.chrome.for.testing", "code_sign_clone.aaa111")[0],
            clone("com.google.chrome.for.testing", "code_sign_clone.bbb222")[0]]
    live, live_program = clone("com.google.chrome.for.testing", "code_sign_clone.ccc333")
    edge, edge_program = clone("com.microsoft.edgemac", "code_sign_clone.ddd444")
    unknown = clone("org.example.Unknown", "code_sign_clone.eee555")[0]            # any bundle id, not a fixed list
    not_copies = [put("com.apple.something", "data", root=x), put("loose-file", root=x),
                  put("com.google.Chrome.code_sign_clone", "notes.txt", root=x),
                  put("org.other.thing", "code_sign_clone.fff666", "x", root=x)]
    spelled = live_program.replace("/private/", "/", 1)        # /var/folders/…: the same file through the link
    assert spelled != live_program and os.path.samefile(spelled, live_program)
    real_programs = cleaner.program_paths
    cleaner.program_paths = lambda: {spelled, "/Applications/Other.app/Contents/MacOS/Other"}
    try:
        copies = named(cleaner.scan_browser_copies())
        testing = copies["Browser copies left by automated Chrome for Testing"]
        assert sorted(testing["paths"]) == sorted(idle), "the copy a running browser was started from stays"
        assert testing["note"] == "1 in use, left alone." and testing["group"] == "temp" and not testing["blocked"]
        assert "Selenium, Puppeteer and Playwright" in testing["detail"] and "up to" in testing["detail"]
        assert cleaner.public(testing, "")["safe"], "pure leftovers: Safe clean may take them"
        assert copies["Browser copies left by automated Edge"]["paths"] == [edge]
        assert copies["Browser copies left by automated org.example.Unknown"]["paths"] == [unknown]
        offered = {p for row in copies.values() for p in row["paths"]}
        assert offered == set(idle + [edge, unknown]) and all(cleaner.is_clone(p) for p in offered)
        cleaner.program_paths = lambda: {spelled, edge_program}
        assert "Browser copies left by automated Edge" not in named(cleaner.scan_browser_copies()), \
            "a browser with no copy to spare gets no row"
        # The copy's program is a hard link to the app's own, and the kernel can name the running
        # program by the app's path. The file is the same one, so the copy is in use all the same.
        app_program = put("Browser.app", "Contents", "MacOS", "browser", kb=50, root=cleaner.APPLICATIONS)
        linked, linked_program = clone("org.linked.Browser", "code_sign_clone.ggg777")
        os.remove(linked_program)
        os.link(app_program, linked_program)
        cleaner.program_paths = lambda: {app_program}
        assert cleaner.clones_in_use([linked, unknown]) == {linked}, "by the file itself, whichever name it runs under"
        assert "Browser copies left by automated org.linked.Browser" not in named(cleaner.scan_browser_copies())
        cleaner.program_paths = lambda: set()
        assert cleaner.clones_in_use([linked, unknown]) == set()
        # The lock lets exactly those folders through, and nothing else in X or beside it.
        assert all(cleaner.deletable(p) for p in idle + [live, edge, unknown])
        for never in [x, os.path.dirname(idle[0]), os.path.join(x, "com.apple.something"), os.path.join(box, "C", "cache"),
                      os.path.join(x, "org.other.thing", "code_sign_clone.fff666"),
                      os.path.join(idle[0], "..", "..", "com.apple.something")] + not_copies:
            assert not cleaner.deletable(never), never
        os.symlink(outside, os.path.join(x, "linked.code_sign_clone"))
        assert not cleaner.deletable(os.path.join(x, "linked.code_sign_clone", "code_sign_clone.zzz999"))
        assert "linked.code" not in " ".join(named(cleaner.scan_browser_copies()))
        # A browser started from a copy after the scan: asked again at delete time, and it stays.
        cleaner.program_paths = lambda: {os.path.join(idle[1], "Browser.app", "Contents", "MacOS", "browser")}
        out = cleaner.delete(testing)
        assert not os.path.exists(idle[0]) and os.path.exists(idle[1]) and os.path.exists(live), out
        assert out["errors"] == ["1 copy is in use by a running browser, so it stayed."] and out["left_kb"] >= 200, out
        assert all(os.path.exists(p) for p in not_copies)
    finally:
        cleaner.program_paths = real_programs
    # With a real process, where there is a C compiler: a program of the test's own that only
    # sleeps, started under the app's name with a second name inside a copy. What the kernel calls
    # it is the kernel's business; the copy counts as in use either way.
    tools = cleaner.developer_dir()
    clang = os.path.join(tools, "Toolchains", "XcodeDefault.xctoolchain", "usr", "bin", "clang")
    sdk = next((p for p in (os.path.join(tools, "Platforms", "MacOSX.platform", "Developer", "SDKs", "MacOSX.sdk"),
                            os.path.join(tools, "SDKs", "MacOSX.sdk")) if os.path.isdir(p)), None)
    if os.access(clang, os.X_OK) and sdk:
        napper = os.path.join(cleaner.APPLICATIONS, "Napper.app", "Contents", "MacOS", "napper")
        os.makedirs(os.path.dirname(napper))
        with open(os.path.join(box, "napper.c"), "w") as handle:
            handle.write("#include <unistd.h>\nint main(void) { sleep(120); return 0; }\n")
        built = subprocess.run([clang, "-isysroot", sdk, "-o", napper, os.path.join(box, "napper.c")], capture_output=True, text=True)
        assert built.returncode == 0, built.stderr
        napping, napping_program = clone("org.napper.Browser", "code_sign_clone.hhh888")
        os.remove(napping_program)
        os.link(napper, napping_program)
        asleep = subprocess.Popen([napper])
        try:
            for _ in range(100):
                if any(os.path.basename(p) in ("napper", "browser") and box in os.path.realpath(p) for p in cleaner.program_paths()):
                    break
                time.sleep(0.05)
            assert cleaner.clones_in_use([napping, unknown]) == {napping}, "a running program's copy, found by the file"
            assert "Browser copies left by automated org.napper.Browser" not in named(cleaner.scan_browser_copies())
        finally:
            asleep.kill()
            asleep.wait()

    # --- model weights: the stores, and loose files Spotlight knows of, every hit checked ------------
    cleaner.MODEL_MIN_KB, cleaner.ARCHIVE_MIN_KB, cleaner.PACKED_MIN_KB = 64, 32, 64
    cleaner.OLLAMAS = ()
    docs = os.path.join(cleaner.HOME, "Documents")
    hf_model = os.path.dirname(os.path.dirname(put(".cache", "huggingface", "hub", "models--org--tiny-llm", "blobs", "abc", kb=100)))
    hf_data = os.path.dirname(os.path.dirname(put(".cache", "huggingface", "hub", "datasets--org--set", "blobs", "d", kb=100)))
    hf_token = put(".cache", "huggingface", "token", kb=1)
    hf_xet = os.path.dirname(put(".cache", "huggingface", "xet", "chunk", kb=50))
    # Other tools keep their own data beside Hugging Face's downloads: not downloads, never offered.
    not_downloads = [put(".cache", "huggingface", "lerobot", "calibration", "arm.json", kb=50),
                     put(".cache", "huggingface", "trackio", "runs.db", kb=50), put(".cache", "huggingface", "stored_tokens", kb=1),
                     put(".cache", "huggingface", "hub", "version.txt", kb=1),
                     put(".cache", "torch", "sentence_transformers", "mine", "model.bin", kb=50),
                     put(".cache", "torch", "hub", "trusted_list", kb=1), put(".cache", "torch", "hub", "notes", "mine.txt", kb=50)]
    put(".cache", "torch", "hub", "checkpoints", "resnet50-0676ba61.pth", kb=100)
    torch_code = os.path.dirname(put(".cache", "torch", "hub", "pytorch_vision_main", "hubconf.py", kb=20))
    # A store that is really somewhere else, reached through a link: headroom could never clear it.
    put("jan-data", "models", "phi", "model.gguf", kb=100, root=outside)
    os.makedirs(os.path.join(cleaner.HOME, "Library", "Application Support", "Jan"))
    os.symlink(os.path.join(outside, "jan-data"), os.path.join(cleaner.HOME, "Library", "Application Support", "Jan", "data"))
    put("anaconda3", "conda-meta", "history", kb=1)
    put(".ollama", "models", "blobs", "sha256-abc", kb=100)
    gpt4all = put("Library", "Application Support", "nomic.ai", "GPT4All", "mistral.gguf", kb=100)
    gpt4all_db = put("Library", "Application Support", "nomic.ai", "GPT4All", "localdocs_v2.db", kb=50)
    jan_file = put("jan", "models", "llama3", "model.gguf", kb=100)
    for stored in (".cache", ".ollama", "jan", os.path.join("Library", "Application Support", "nomic.ai")):
        age(os.path.join(cleaner.HOME, stored), 60)
    put(".lmstudio", "models", "pub", "Model-B", "b.gguf", kb=100)                          # downloaded today
    trained = put("Documents", "study", "weights", "final.pt", kb=100)        # not a git project, unlike proj
    hf_bin = put("Documents", "study", "hf", "pytorch_model.bin", kb=100)
    put("Documents", "study", "hf", "config.json", kb=1)
    ckpt_bin = put("Documents", "study", "checkpoints", "step-100.bin", kb=100)
    keras_h5 = put("Documents", "study", "models", "net.h5", kb=100)                        # .h5 where the folder says model
    plain_bin = put("Documents", "study", "data", "blob.bin", kb=100)                        # .bin, and nothing says model
    plain_h5 = put("Documents", "study", "data", "measurements.h5", kb=100)                  # .h5 is any kind of data
    runs = [put("Documents", "runs", f"run{n:02}", "out", "concept_predictor.pth", kb=100) for n in range(7)]
    age(os.path.join(docs, "runs"), 60)
    in_repo = put("Desktop", "repo", "weights", "in-repo.pt", kb=100)                        # part of a git project
    age(in_repo, 60)
    small_model = put("Documents", "small.onnx", kb=20)
    package = os.path.join(docs, "Classifier.mlpackage")
    inside_package = put("Data", "weights.bin", kb=100, root=package)
    age(os.path.join(docs, "study"), 60)
    age(package, 60)
    fresh_weights = put("Documents", "study", "weights", "fresh.safetensors", kb=100)
    outside_model = put("model.gguf", kb=100, root=outside)
    model_link = os.path.join(docs, "linked.gguf")
    os.symlink(outside_model, model_link)
    cloud_model = put("Documents", "in-icloud.gguf", kb=100)
    left_out = [
        put("Documents", "proj", "node_modules", "x", "model.onnx", kb=100), put("Documents", "proj", ".git", "objects", "big.pt", kb=100),
        put("Documents", "Tool.app", "Contents", "model.gguf", kb=100), put("Documents", "Photos.photoslibrary", "originals", "x.pt", kb=100),
        put("Documents", "proj", "venv", "lib", "m.onnx", kb=100), put("Library", "Application Support", "SomeApp", "model.gguf", kb=100),
        put("Desktop", "rusty", "target", "model.onnx", kb=100),               # inside a project's build folder
        put("anaconda3", "lib", "big.pt", kb=100), put("model-at-top.gguf", kb=100),
        put("Documents", "tools", "mamba", "conda-meta", "history", kb=1),      # a conda install, wherever it lives
        put("Documents", "tools", "mamba", "pkgs", "torch", "weights.pt", kb=100),
        put("Documents", "sdks", "flutter", "bin", "flutter", kb=1), put("Documents", "sdks", "flutter", "bin", "cache", "model.tflite", kb=100),
        outside_model, os.path.join(cleaner.HOME, "Desktop", "way-out", "model.gguf"),      # outside, also by way of a link
        model_link, cloud_model, plain_bin, plain_h5, small_model, inside_package, jan_file, gpt4all,
        "/etc/hosts", os.path.join(docs, "gone.pt"), os.path.join(docs, "study", "weights", "..", "weights", "final.pt")]
    spot = {"hits": [trained, fresh_weights, hf_bin, ckpt_bin, keras_h5, package, in_repo] + runs + left_out, "code": 0}
    placeholders.add(os.stat(cloud_model).st_ino)
    real_go, cleaner.go_caches = cleaner.go_caches, lambda: (None, [os.path.join(cleaner.HOME, "go", "pkg", "mod")])

    def spot_says(command):
        if command[0] == "/usr/bin/mdfind":
            assert command[1:4] == ["-0", "-onlyin", cleaner.HOME] and command[4].startswith("kMDItemFSSize >= "), command
            return spot["code"], "\0".join(spot["hits"])
        return capture_says(command)

    def spotlit(scan):
        with recording(Recorder(answer=spot_says, fakes=["/usr/bin/mdfind", "/usr/bin/defaults"], never=("/bin/rm",))):
            return scan()
    found, notice = spotlit(cleaner.scan_models)
    models = named(found)
    downloaded = ["Hugging Face model org/tiny-llm", "PyTorch checkpoint resnet50-0676ba61.pth"]
    imported = ["LM Studio model Qwen-9B-GGUF", "LM Studio model Model-B", "GPT4All model mistral.gguf", "Jan model llama3",
                "Ollama models"]          # these apps also take models you bring yourself
    loose_names = ["weights / final.pt", "weights / fresh.safetensors", "hf / pytorch_model.bin", "checkpoints / step-100.bin",
                   "models / net.h5", "Documents / Classifier.mlpackage", "weights / in-repo.pt",
                   "7 files named concept_predictor.pth in runs"]
    assert sorted(models) == sorted(downloaded + imported + loose_names + ["Hugging Face downloads", "PyTorch downloads"]), sorted(models)
    assert notice == "" and len(found) == len(models)
    for name in downloaded + imported + loose_names:
        assert models[name]["group"] == "models" and not cleaner.public(models[name], "")["safe"], name
        assert models[name]["trash"] == (name not in downloaded), (name, "only what a tool downloaded is deleted outright")
    assert all("again" in models[name]["detail"] for name in downloaded + imported)
    assert all(models[name]["note"] == cleaner.IMPORTED for name in imported if name != "LM Studio model Model-B")
    assert "`" not in models["Ollama models"]["detail"] and models["Ollama models"]["quit"]
    final = models["weights / final.pt"]
    assert final["paths"] == [trained] and final["note"] == "Goes to the Trash, in case you trained it yourself."
    assert final["detail"] == "~/Documents/study/weights · added or changed 2 months ago", final["detail"]
    assert not final["recent"] and not models["Hugging Face model org/tiny-llm"]["recent"]
    for name in ("weights / fresh.safetensors", "LM Studio model Model-B"):
        assert models[name]["recent"] and "Added or changed in the last 30 days" in models[name]["note"], name
    assert models["hf / pytorch_model.bin"]["paths"] == [hf_bin] and models["checkpoints / step-100.bin"]["paths"] == [ckpt_bin]
    assert models["models / net.h5"]["paths"] == [keras_h5], ".bin and .h5 only where the folder says model"
    assert models["Documents / Classifier.mlpackage"]["paths"] == [package] and models["Documents / Classifier.mlpackage"]["kb"] >= 100
    # A model inside a git project is listed, and left out when the whole section is ticked.
    assert models["weights / in-repo.pt"]["recent"] and "git project" in models["weights / in-repo.pt"]["note"]
    # A training run's same file in many folders is one row, and stays one row as their number changes.
    group = models["7 files named concept_predictor.pth in runs"]
    assert sorted(group["paths"]) == sorted(runs) and group["kb"] >= 700 and group["trash"] and not group["recent"]
    assert group["detail"].startswith("~/Documents/runs · ") and group["same"] == "named:concept_predictor.pth:" + os.path.join(docs, "runs")
    assert all(row["seen"] == dict.fromkeys(row["paths"], row["seen"][row["paths"][0]]) for row in found if row["trash"] and row["seen"])
    # Only what is known to be a download, by name: not everything that isn't a model.
    assert sorted(models["Hugging Face downloads"]["paths"]) == sorted([hf_data, hf_xet]) and models["Hugging Face downloads"]["group"] == "dev"
    assert models["Hugging Face downloads"]["detail"] == "Datasets and other downloads. Models and your login stay. ~/.cache/huggingface"
    assert models["PyTorch downloads"]["paths"] == [torch_code] and "torch.hub" in models["PyTorch downloads"]["detail"]
    offered = [p for row in found for p in row["paths"]]
    assert len(offered) == len(set(offered)) and hf_token not in offered, "your Hugging Face login stays"
    assert not any(kept == p or kept.startswith(p + os.sep) for kept in not_downloads for p in offered), "other tools' own data stays"
    assert not {p for row in found if row["trash"] and row["seen"] for p in row["paths"]} & set(left_out), "no hit that failed a check"
    assert not any("phi" in name for name in models) and not any(outside in p for p in offered), "a store behind a link is not listed"
    assert all(p.startswith(cleaner.HOME + os.sep) and not os.path.islink(p) and cleaner.deletable(p) for p in offered)
    assert not {"huggingface", "torch"} & {os.path.basename(p) for r in cleaner.scan_home_cache() for p in r["paths"]}, \
        "~/.cache leaves both to the model rows"
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        assert cleaner.delete(final)["errors"] == []
        assert cleaner.delete(models["GPT4All model mistral.gguf"])["errors"] == [], "may be one you imported: the Trash"
    assert recorder.calls == [finder + [trained], finder + [gpt4all]] and os.path.exists(trained) and os.path.exists(gpt4all)
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        assert cleaner.delete(models["Hugging Face model org/tiny-llm"], to_trash=True)["errors"] == []
    assert recorder.calls == [finder + [hf_model]], "a download follows the switch"
    assert cleaner.delete(models["Hugging Face model org/tiny-llm"])["errors"] == []
    assert not os.path.exists(hf_model) and os.path.exists(hf_token) and all(os.path.exists(p) for p in not_downloads)
    # A file that is no longer the file the scan saw is not moved.
    os.rename(ckpt_bin, ckpt_bin + ".aside")
    os.symlink(kept_outside, ckpt_bin)
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        out = cleaner.delete(models["checkpoints / step-100.bin"])
    assert recorder.calls == [] and "not what the scan found" in out["errors"][0], out
    os.remove(ckpt_bin)
    os.rename(ckpt_bin + ".aside", ckpt_bin)
    # Spotlight finding nothing says nothing (the section is simply empty of them); not answering
    # is said, and "What grew" is told the source could not be measured. The stores are listed all the same.
    spot["hits"] = []
    found, notice = spotlit(cleaner.scan_models)
    assert notice == "" and not isinstance(notice, cleaner.Unsure)
    assert {"Jan model llama3", "Ollama models"} <= set(named(found)) and not any(row["seen"] for row in found)
    spot["code"] = 1
    for scan in (cleaner.scan_models, cleaner.scan_archives):
        notice = spotlit(scan)[1]
        assert "did not answer" in notice and isinstance(notice, cleaner.Unsure), scan
    spot["code"] = 0
    # Ollama itself, when it is there: its own command removes a model.
    ollama = put("ollama", kb=0, root=os.path.join(box, "fake-ollama"))
    os.chmod(ollama, 0o755)
    cleaner.OLLAMAS = (ollama,)
    listing = ("NAME            ID              SIZE      MODIFIED\n"
               "llama3:latest   365c0bd3c000    4.7 GB    2 days ago\nphi3:mini       4f2222927938    2.2 GB    5 months ago\n")
    ollama_up = [True]
    with recording(Recorder(answer=lambda command: ((0, listing) if ollama_up[0] else (1, "")) if command[0] == ollama else spot_says(command),
                            fakes=[ollama, "/usr/bin/mdfind"])) as recorder:
        ollamas = named(cleaner.scan_models()[0])
        ollama_up[0] = False
        asleep_rows, asleep_notice = cleaner.scan_models()
    assert ollamas["Ollama model llama3:latest"]["cmd"] == [ollama, "rm", "llama3:latest"] and "Ollama models" not in ollamas
    assert ollamas["Ollama model llama3:latest"]["group"] == "models" and ollamas["Ollama model llama3:latest"]["recent"]
    assert "`" not in ollamas["Ollama model llama3:latest"]["detail"] and "ollama pull llama3:latest" in ollamas["Ollama model llama3:latest"]["detail"]
    assert not ollamas["Ollama model phi3:mini"]["recent"] and ollamas["Ollama model phi3:mini"]["kb"] == cleaner.size_kb("2.2GB")
    assert named(asleep_rows)["Ollama models"]["blocked"] and isinstance(asleep_notice, cleaner.Unsure) and asleep_notice == ""
    cleaner.OLLAMAS = ()

    # --- zip files and other archives: Spotlight's hits, checked, and always to the Trash ------------
    unzipped = put("Documents", "work", "project.zip", kb=64)
    os.makedirs(os.path.join(docs, "work", "project"))
    packed = put("Documents", "work", "data.csv.gz", kb=100)
    put("Documents", "work", "data.csv", kb=1)
    tarball = put("Documents", "work", "bundle.tar.gz", kb=40)
    notes_zip = os.path.join(cleaner.HOME, "Downloads", "notes.zip")
    sets = [put("Documents", "sets", f"part{n:02}.zip", kb=40) for n in range(7)]
    done_sets = [put("Documents", "done", f"set{n}.7z", kb=40) for n in range(6)]
    for n in range(6):
        os.makedirs(os.path.join(docs, "done", f"set{n}"))
    outside_zip = put("backup.zip", kb=64, root=outside)
    zip_link = os.path.join(docs, "linked.zip")
    os.symlink(outside_zip, zip_link)
    cloud_zip = put("Documents", "in-icloud.zip", kb=64)
    placeholders.add(os.stat(cloud_zip).st_ino)
    in_project = put("Desktop", "repo", "fixtures", "data.zip", kb=64)          # a git project's test data
    not_archives = [
        in_project, put("go", "pkg", "mod", "cache", "download", "mod.zip", kb=64),              # Go's module cache
        put(".lmstudio", "models", "pub", "Model-B", "extra.zip", kb=64), os.path.join(os.path.dirname(jan_file), "pack.zip"),
        put("Documents", "tools", "mamba", "pkgs", "big-1.0.tar.bz2", kb=100),  # a conda install somewhere else
        put("Documents", "sdks", "flutter", "bin", "cache", "artifacts.zip", kb=64),
        put("Documents", "work", "small.log.gz", kb=40),                        # a plain .gz counts only when big
        put("Documents", "work", "tiny.zip", kb=10), put("Documents", "work", "report.zip.txt", kb=64),
        os.path.join(cleaner.HOME, "Downloads", "App.zip"),                     # a zipped app: Installer files has it
        put("anaconda3", "pkgs", "again-1.0.tar.bz2", kb=100),                  # Conda package downloads has it
        put("Documents", "proj", "node_modules", "x", "dep.zip", kb=64), put("Library", "Caches", "com.example.kept", "c.zip", kb=64),
        put("Documents", "Tool.app", "Contents", "res.zip", kb=64), put(".Trash", "old.zip", kb=64),
        os.path.join(docs, "work", "project"), outside_zip, zip_link, cloud_zip, "/etc/hosts"]
    write(os.path.join(os.path.dirname(jan_file), "pack.zip"), os.urandom(64 * 1024))
    spot["hits"] = [unzipped, packed, tarball, notes_zip] + sets + done_sets + not_archives
    found, notice = spotlit(cleaner.scan_archives)
    archives = named(found)
    assert sorted(archives) == sorted(["work / project.zip", "work / data.csv.gz", "work / bundle.tar.gz", "Downloads / notes.zip",
                                       "7 archives in sets", "6 archives already unzipped in done"]), sorted(archives)
    assert notice == "" and all(row["group"] == "archives" and row["trash"] and row["seen"] for row in found)
    assert not any(cleaner.public(row, "")["safe"] or row["cmd"] for row in found), "never Safe clean, never a command"
    assert archives["work / project.zip"]["note"] == "A folder named project is next to it."
    assert archives["work / data.csv.gz"]["note"] == "A file named data.csv is next to it."
    assert archives["work / bundle.tar.gz"]["note"] == "" and "~/Documents/work · added" in archives["work / bundle.tar.gz"]["detail"]
    assert {name for name, row in archives.items() if cleaner.public(row, "")["rank"]} == {
        "work / project.zip", "work / data.csv.gz", "6 archives already unzipped in done"}, "the unzipped ones come first"
    assert archives["7 archives in sets"]["paths"] == sets, "a folder full of them is one row"
    assert archives["7 archives in sets"]["detail"] == "~/Documents/sets · part00.zip, part01.zip, part02.zip and 4 more"
    assert archives["7 archives in sets"]["same"] == "archives:" + os.path.join(docs, "sets") + ":False", "known by its folder, not its count"
    assert archives["6 archives already unzipped in done"]["paths"] == done_sets
    offered = [p for row in found for p in row["paths"]]
    assert len(offered) == len(set(offered)) and not set(offered) & set(not_archives)
    assert all(p.startswith(cleaner.HOME + os.sep) and os.path.isfile(p) and not os.path.islink(p) for p in offered)
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        assert cleaner.delete(archives["7 archives in sets"])["errors"] == []
    assert recorder.calls == [finder + sets], recorder.calls
    assert all(os.path.exists(p) for p in sets), "the recorder stood in for Finder"
    spot["hits"] = []
    assert spotlit(cleaner.scan_archives) == ([], ""), "nothing found says nothing: an empty section is enough"

    # --- a path is offered once: no row holds another row's path, or anything inside one --------------
    spot["hits"] = [trained, fresh_weights, hf_bin, ckpt_bin, keras_h5, package, in_repo, unzipped, packed, tarball, notes_zip] \
        + runs + sets + done_sets + left_out + not_archives
    fresh()
    seen = {}
    for name, (_, source_scan) in cleaner.SOURCES.items():
        if name in ("simulators", "homebrew", "docker"):                # their rows are commands, without paths
            continue
        found = spotlit(source_scan) if name in ("screenshots", "models", "archives") else source_scan()
        for row in found[0] if isinstance(found, tuple) else found:
            for p in row["paths"]:
                assert p not in seen, (p, "is in two rows", (name, row["name"]), seen[p])
                seen[p] = (name, row["name"])
    assert {"models", "archives", "screenshots", "browser_copies"} <= {name for name, _ in seen.values()}
    for p in seen:
        above = os.path.dirname(p)
        while len(above) > len(box):
            assert above not in seen, (p, seen[p], "is inside", seen.get(above))
            above = os.path.dirname(above)
    cleaner.dataless, cleaner.go_caches = earlier_dataless, real_go

    # --- what grew: one snapshot of the sizes a day, set against one from about a week ago ----------
    sizes_file = os.path.join(cleaner.state_dir(), "sizes.json")
    noon = time.mktime((2026, 10, 12, 12, 0, 0, 0, 0, -1))
    cleaner.clock = lambda: noon        # else these days are "the future" wherever the real date is earlier
    cleaner.GREW_KB = 1000
    grew = lambda: cleaner.growth(noon)

    def sized_row(group, name, path, kb, blocked="", same=""):
        made = cleaner.item(group, name, [path], kb, blocked=blocked)
        made["same"] = same
        return made

    def snap(days_ago, source, *rows, failed=False):
        cleaner.keep_sizes(source, [sized_row(*row) for row in rows], noon - days_ago * cleaner.DAY, failed)

    def forget():
        if os.path.exists(sizes_file):
            os.remove(sizes_file)
    nothing = {"span": "", "sections": {}, "top": []}
    forget()
    assert grew() == nothing, "no history: no numbers"
    snap(0, "dev_tools", ("dev", "npm downloads", "/a", 9000))
    assert grew() == nothing, "today alone is nothing to compare with"
    snap(0, "dev_tools", ("dev", "npm downloads", "/a", 9500))
    assert json.load(open(sizes_file)) == {"2026-10-12": {"dev_tools": {
        cleaner.item("dev", "npm downloads", ["/a"], 0)["id"]: ["dev", "npm downloads", 9500]}}}, "a day holds one snapshot"
    snap(1, "dev_tools", ("dev", "npm downloads", "/a", 8000))
    assert grew()["span"] == "in 1 day" and grew()["sections"] == {"dev": 1500}, grew()
    snap(3, "dev_tools", ("dev", "npm downloads", "/a", 7000))
    assert grew()["span"] == "in 3 days" and grew()["sections"] == {"dev": 2500}, "the oldest there is"
    snap(12, "dev_tools", ("dev", "npm downloads", "/a", 1000))
    snap(20, "dev_tools", ("dev", "npm downloads", "/a", 500))
    assert grew()["span"] == "in 12 days" and grew()["sections"] == {"dev": 8500}, "nearest past a week"
    snap(6, "dev_tools", ("dev", "npm downloads", "/a", 6000))
    snap(8, "dev_tools", ("dev", "npm downloads", "/a", 5000))
    week = grew()
    assert week["span"] == "this week" and week["sections"] == {"dev": 4500}, ("the oldest of 5 to 9 days back", week)
    snap(10, "dev_tools", ("dev", "npm downloads", "/a", 2000))
    assert grew() == week, "not one from further back"
    # A day after today (the clock was wrong for a while) is never the newest scan, and is dropped at the next one.
    far = max(noon, time.time()) + 3 * cleaner.DAY
    cleaner.keep_sizes("dev_tools", [sized_row("dev", "npm downloads", "/a", 99000)], far)
    assert cleaner.day_of(far) in json.load(open(sizes_file)) and grew() == week, "ignored when reading"
    snap(0, "dev_tools", ("dev", "npm downloads", "/a", 9500))
    assert cleaner.day_of(far) not in json.load(open(sizes_file)) and grew() == week, "and gone when the file is next written"
    # Rows: by id, by name when a row's paths (and so its id) changed, new ones marked, shrunk ones left out.
    forget()
    snap(7, "dev_tools", ("dev", "npm downloads", "/a", 5000), ("dev", "Gradle caches", "/g", 4000),
         ("dev", "Xcode build data", "/x/one", 2000), ("dev", "Yarn cache", "/y", 900))
    snap(7, "docker", ("big", "Docker images no container uses", "/d", 0, "Start Docker to measure this"),
         ("dev", "Docker build cache", "/b", 1000))
    snap(7, "homebrew", failed=True)                                    # Homebrew did not answer that day
    snap(7, "caches", *[("apps", f"cache {n}", f"/c{n}", 100) for n in range(5)])
    snap(7, "archives", ("archives", "9 archives in sets", "/s/one", 9000, "", "archives:/s:False"))
    snap(0, "dev_tools", ("dev", "npm downloads", "/a", 9000), ("dev", "Gradle caches", "/g", 1000),
         ("dev", "Xcode build data", "/x/two", 6000), ("dev", "Yarn cache", "/y", 1800), ("dev", "pip downloads", "/p", 3000))
    snap(0, "docker", ("big", "Docker images no container uses", "/d", 50000), ("dev", "Docker build cache", "/b", 3000))
    snap(0, "homebrew", ("dev", "Homebrew old versions", "/h", 40000))
    snap(0, "caches", *[("apps", f"cache {n}", f"/c{n}", 400) for n in range(5)])
    snap(0, "archives", ("archives", "12 archives in sets", "/s/two", 12000, "", "archives:/s:False"))
    snap(0, "builds", ("builds", "web / node_modules", "/w", 70000))    # not measured a week ago
    week = grew()
    assert week["sections"] == {"dev": 10900, "apps": 1500, "archives": 3000}, ("only what both days measured", week["sections"])
    assert week["top"] == [
        {"name": "npm downloads", "group": "dev", "kb": 4000, "new": False},
        {"name": "Xcode build data", "group": "dev", "kb": 4000, "new": False},    # found by its name
        {"name": "12 archives in sets", "group": "archives", "kb": 3000, "new": False},   # the same row, three more in it
        {"name": "pip downloads", "group": "dev", "kb": 3000, "new": True},
        {"name": "Docker build cache", "group": "dev", "kb": 2000, "new": False},  # beside a row that could not be measured
        {"name": "", "group": "apps", "kb": 1500, "new": False}], week["top"]    # a section itself, when none of its rows is listed
    saved = json.load(open(sizes_file))
    assert saved["2026-10-05"]["homebrew"] is None, "a scan that failed is not a scan that found nothing"
    assert ["big", "Docker images no container uses", None] in saved["2026-10-05"]["docker"].values(), \
        "a row that could not be measured has no size, not 0"
    assert "archives:/s:False" in saved["2026-10-12"]["archives"], "kept under what stays the same about it"
    # A source that fails today is unknown today: not compared, and not counted as gone.
    snap(0, "docker", failed=True)
    assert grew()["sections"] == {"dev": 8900, "apps": 1500, "archives": 3000}, grew()["sections"]
    # A later day that holds only what a delete touched must not hide the sources scanned the day before.
    forget()
    snap(8, "dev_tools", ("dev", "npm downloads", "/a", 1000))
    snap(8, "caches", ("apps", "big", "/c", 9000))
    snap(1, "dev_tools", ("dev", "npm downloads", "/a", 6000))
    snap(1, "caches", ("apps", "big", "/c", 9500))
    snap(0, "caches", ("apps", "big", "/c", 0))                         # deleted just after midnight
    assert grew() == {"span": "this week", "sections": {"dev": 5000},
                      "top": [{"name": "npm downloads", "group": "dev", "kb": 5000, "new": False}]}, grew()
    # The file is compacted: one snapshot a day, and nothing over 30 days old.
    forget()
    for days_ago in range(45, -1, -1):
        for hour in (0, 3600):
            cleaner.keep_sizes("dev_tools", [cleaner.item("dev", "npm downloads", ["/a"], 1000 + hour)],
                               noon - days_ago * cleaner.DAY + hour)
    kept = json.load(open(sizes_file))
    assert len(kept) == 31 and min(kept) == "2026-09-12" and max(kept) == "2026-10-12", (len(kept), min(kept), max(kept))
    assert all(list(day) == ["dev_tools"] and list(day["dev_tools"].values()) == [["dev", "npm downloads", 4600]]
               for day in kept.values()), "the later scan of a day replaces the earlier one"
    assert os.path.getsize(sizes_file) < 4000
    # A file that isn't what headroom wrote claims nothing and breaks nothing.
    for broken in ("[1, 2]", "{", json.dumps({"2026-10-12": "x", "not-a-day": {}, "2026-10-05": {"dev_tools": {"id": ["dev", 3]}}}),
                   json.dumps({"2026-10-12": {"dev_tools": {"a": ["dev", "x", "9"]}}, "2026-10-05": {"dev_tools": 4}})):
        with open(sizes_file, "w") as handle:
            handle.write(broken)
        assert grew()["sections"] == {} and grew()["top"] == [], broken
    forget()
    cleaner.GREW_KB = 100 * 1024

    cleaner.clock = time.time

    # --- the folder measurer: its numbers when it is there, du's for whatever it leaves out ----------
    assert cleaner.find_sizer() == (os.path.realpath(os.environ["HEADROOM_SIZE"]) if os.environ.get("HEADROOM_SIZE") else None), \
        "from source there is none, unless HEADROOM_SIZE points at one"
    # It is looked for inside an app bundle only: a MacOS folder that happens to sit beside a
    # plain folder of source is somebody else's.
    real_here, wanted = cleaner.HERE, os.environ.pop("HEADROOM_SIZE", None)
    for layout, found in ((("app", "Contents", "Resources"), True), (("plain", "headroom"), False)):
        cleaner.HERE = os.path.join(box, "layouts", *layout)
        os.makedirs(cleaner.HERE)
        beside = put("MacOS", "headroom-size", kb=1, root=os.path.dirname(cleaner.HERE))
        os.chmod(beside, 0o755)
        assert (cleaner.find_sizer() == beside) == found, layout
    cleaner.HERE = real_here
    if wanted:
        os.environ["HEADROOM_SIZE"] = wanted
    fake_sizer = put("headroom-size", kb=0, root=os.path.join(box, "fake-sizer"))
    os.chmod(fake_sizer, 0o755)
    one, two = (os.path.dirname(put("Documents", "measure", name, "f", kb=100)) for name in ("one", "two"))
    never_asked = os.path.dirname(put("Documents", "thesis", "draft.txt"))
    measure = os.path.join(docs, "measure")
    plain, whole = cleaner.du([one, two]), cleaner.du([measure])[measure]
    assert plain[one] >= 100 and plain[two] >= 100
    # A line ends at a line feed and nowhere else. A folder name can hold U+2028, which Python's
    # splitlines() also breaks at, and what follows it would read as another folder's size.
    odd = f"{two}\u2028999\t{one}"
    assert cleaner.measured_lines(f"5\t{one}\n7\t{odd}\n", {one, odd}) == {one: 5, odd: 7}

    def sizer_says(command):
        assert command[1] == "--", command
        return 0, f"777\t{one}\n99999\t{never_asked}\nnot a line\n\t{two}\nabc\t{two}\n"
    try:
        cleaner.SIZER = fake_sizer
        with recording(Recorder(answer=sizer_says, fakes=[fake_sizer])) as recorder:
            sizes = cleaner.du([one, two, os.path.join(docs, "not-there")])
        assert recorder.calls == [[fake_sizer, "--", one, two]], "one call: it works on several threads by itself"
        assert sizes == {one: 777, two: plain[two]}, ("its number, du's for the path it left out, nothing unasked", sizes)
        with recording(Recorder(answer=sizer_says, stderr=f"headroom-size: {one}/locked: Permission denied\n",
                                fakes=[fake_sizer])):
            sizes, total, denied = cleaner.du_children(os.path.join(docs, "measure"))
        assert sizes == {one: 777, two: plain[two]} and denied == [one + "/locked"], (sizes, denied)
        assert total == whole, "the folder's own total is the folder measured as one thing, not its children added up"
        # A folder with thousands of subfolders: 400 to a call, as for any other measuring.
        crowd = os.path.join(docs, "crowd")
        for n in range(900):
            os.makedirs(os.path.join(crowd, f"{n:04}-" + "x" * 60))
        with recording(Recorder(answer=lambda command: (0, ""), fakes=[fake_sizer])) as recorder:
            sizes, total, _ = cleaner.du_children(crowd)
        assert len(sizes) == 900 and [len(call) - 2 for call in recorder.calls] == [400, 400, 100, 1], [len(c) for c in recorder.calls]
        shutil.rmtree(crowd)
        cleaner.SIZER = os.path.join(box, "no-such-measurer")
        assert cleaner.du([one, two]) == plain, "a measurer that won't start: du measures everything"
        assert cleaner.du_children(measure)[0] == plain
    finally:
        cleaner.SIZER = None
    shutil.rmtree(never_asked)
    # du gives a number even for a folder it could not get to the bottom of (names longer than a
    # path may be). That number is too small, and the row says so instead of showing it as the size.
    deep = os.path.join(docs, "deep")
    os.makedirs(deep)
    os.chdir(deep)
    for _ in range(6):
        os.mkdir("d" * 200)
        os.chdir("d" * 200)
    with open("payload", "wb") as handle:
        handle.write(os.urandom(900 * 1024))
    os.chdir(here)
    assert cleaner.du([deep, one]) == {deep: 0, one: plain[one]} and cleaner.PARTLY == {deep}, cleaner.PARTLY
    cleaner.SOURCES["probe"] = (("dev",), lambda: [cleaner.item("dev", "deep", [deep], cleaner.du([deep])[deep]),
                                                   cleaner.item("dev", "one", [one], cleaner.du([one])[one])])
    probe = {row["name"]: row for row in cleaner.scan("probe")["items"]}
    assert probe["deep"]["note"] == "At least this much: part of it could not be measured." and probe["one"]["note"] == "", probe
    assert [entry[2] for entry in json.load(open(sizes_file))[time.strftime("%Y-%m-%d")]["probe"].values()] == [None, plain[one]]
    del cleaner.SOURCES["probe"]
    cleaner.FOUND.pop("probe", None)
    subprocess.run(["/bin/rm", "-rf", "--", deep], check=True)
    assert cleaner.du([one]) == {one: plain[one]} and cleaner.du([deep]) == {}
    cleaner.PARTLY.clear()
    forget()

    # The real one, where there is a Swift compiler to build it with: the numbers `du -sk` prints.
    tools = cleaner.developer_dir()
    swiftc = next((p for p in (os.path.join(tools, "Toolchains", "XcodeDefault.xctoolchain", "usr", "bin", "swiftc"),
                               os.path.join(tools, "usr", "bin", "swiftc")) if os.access(p, os.X_OK)), None)
    sdk = next((p for p in (os.path.join(tools, "Platforms", "MacOSX.platform", "Developer", "SDKs", "MacOSX.sdk"),
                            os.path.join(tools, "SDKs", "MacOSX.sdk")) if os.path.isdir(p)), None)
    # Built for the chip this Mac has, and never when this Python itself is being translated.
    translated = subprocess.run(["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"], capture_output=True, text=True).stdout.strip() == "1"
    if swiftc and sdk and not translated:
        built = os.path.join(box, "built", "headroom-size")
        os.makedirs(os.path.dirname(built))
        made = subprocess.run([swiftc, "-O", "-sdk", sdk, "-target", f"{platform.machine()}-apple-macos12", "-o", built,
                               os.path.join(os.path.dirname(os.path.abspath(cleaner.__file__)), "launcher", "size.swift")],
                              capture_output=True, text=True)
        assert made.returncode == 0, made.stderr
        tree = os.path.join(box, "sized")
        put("a", "one.bin", kb=300, root=tree)
        put("a", "b", "two.bin", kb=5, root=tree)
        put("a", "b", "c", "tiny", kb=1, root=tree)
        os.makedirs(os.path.join(tree, "empty"))
        elsewhere = os.path.dirname(put("big", "huge.bin", kb=2000, root=os.path.join(box, "sized-outside")))
        os.symlink(elsewhere, os.path.join(tree, "a", "link-out"))               # never followed
        os.link(put("hard-a", kb=700, root=tree), os.path.join(tree, "a", "b", "hard-b"))    # one file, two names
        with open(os.path.join(tree, "sparse.img"), "wb") as handle:           # 50 MB long, almost none of it on disk
            handle.truncate(50 * 1024 * 1024)
            handle.seek(40 * 1024 * 1024)
            handle.write(b"x" * 5000)
        put("locked", "inner", "secret", kb=90, root=tree)
        os.chmod(os.path.join(tree, "locked"), 0)
        put("-dash", "x", kb=9, root=tree)
        many = [os.path.dirname(put("many", f"d{n:03}", "f", kb=n % 7, root=tree)) for n in range(450)]

        def du_says(roots, cwd=None):
            done = subprocess.run(["/usr/bin/du", "-sk", "--"] + roots, capture_output=True, text=True, cwd=cwd)
            return cleaner.measured_lines(done.stdout, set(roots))
        roots = [os.path.join(tree, *parts) for parts in ((), ("a",), ("a", "b"), ("empty",), ("sparse.img",), ("a", "link-out"),
                                                           ("hard-a",), ("many",), ("a", "b", "hard-b"))]
        unsure = [os.path.join(tree, "locked"), os.path.join(tree, "not-there")]
        expected = {}
        for root in roots + unsure:
            expected.update(du_says([root]))                # a run each: du counts a file's second name once a run
        expected.update(du_says(many))
        assert set(expected) == set(roots + many) and expected[os.path.join(tree, "a", "link-out")] == 0, "du's own answers"
        assert 700 <= expected[os.path.join(tree, "a", "b")] < 1400 and expected[os.path.join(tree, "sparse.img")] < 50 * 1024
        ran = subprocess.run([built, "--"] + roots + unsure + many, capture_output=True, text=True)
        assert cleaner.measured_lines(ran.stdout, set(roots + unsure + many)) == expected, "the same numbers as du"
        assert len(ran.stdout.splitlines()) == len(expected), "and no line for a path it may not read or that isn't there"
        assert ran.returncode == 1 and f"headroom-size: {unsure[0]}: Permission denied" in ran.stderr
        ran = subprocess.run([built, "--", "-dash", "empty"], capture_output=True, text=True, cwd=tree)
        assert ran.stdout == f"{du_says(['-dash'], cwd=tree)['-dash']}\t-dash\n0\tempty\n" and ran.returncode == 0, ran.stdout
        assert subprocess.run([built, "-dash"], capture_output=True, cwd=tree).returncode == 64, "an option it doesn't have"
        try:
            cleaner.SIZER = built
            assert cleaner.du(roots + unsure + many) == expected, "through headroom, 400 paths at a time"
            # The Disk tab. A file with names in two subfolders counts in each of them, and once
            # in the folder's own total, which is the folder measured as one thing.
            os.makedirs(os.path.join(tree, "twin"))
            os.link(os.path.join(tree, "hard-a"), os.path.join(tree, "twin", "hard-c"))
            sizes, total, denied = cleaner.du_children(tree)
            assert total == expected[tree] and sizes[os.path.join(tree, "twin")] >= 700 and sizes[os.path.join(tree, "a")] >= 700
            assert sum(sizes.values()) > total + 700, "adding the children up would count that file three times"
            os.remove(os.path.join(tree, "hard-a"))
            os.remove(os.path.join(tree, "twin", "hard-c"))
            sizes, _, denied = cleaner.du_children(tree)
            cleaner.SIZER = None
            assert (sizes, denied) == (cleaner.du_children(tree)[0], cleaner.du_children(tree)[2]), "the same listing as du's"
            assert denied == [os.path.join(tree, "locked")]
        finally:
            cleaner.SIZER = None
        os.chmod(os.path.join(tree, "locked"), 0o755)
        shutil.rmtree(tree)

    # --- the port the app's launcher keeps for headroom is tried first ---------------------------------
    port_file = os.path.join(cleaner.state_dir(), "port")
    assert cleaner.reserved_port() is None, "no file: nothing kept, the usual ports"
    for text in ("", "abc", "8459", "8480", "99999", "84 71", "-8461", "\u0668\u0664\u0666\u0661", "8461x"):
        with open(port_file, "w") as handle:
            handle.write(text)
        assert cleaner.reserved_port() is None, repr(text)

    def unused(candidate):
        try:
            for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
                with socket.socket(family) as probe:
                    probe.bind((address, candidate))
        except OSError:
            return False
        return True
    spare = next((candidate for candidate in range(8479, 8460, -1) if unused(candidate)), None)
    if spare:
        with open(port_file, "w") as handle:
            handle.write(f"{spare}\n")
        assert cleaner.reserved_port() == spare
        kept4, kept6 = cleaner.listen()
        assert kept4.server_port == spare and kept6.server_port == spare, "the kept port first, though the usual list is empty here"
        for server in (kept4, kept6):
            server.server_close()
    os.remove(port_file)

    # --- the page's API: only this launch's page gets in ---------------------
    v4, v6 = cleaner.listen()
    assert v6 is not None and v6.server_port == v4.server_port
    for server in (v4, v6):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    port = v4.server_port
    host = f"headroom.localhost:{port}"
    # --- signing in: the session, kept in a file only this account can read ------------------------
    # Two secrets: the token that rides in the cookie, and the key the page keeps for its own origin.
    session_file = os.path.join(cleaner.state_dir(), "session.json")
    assert not os.path.exists(session_file), "nothing makes a session before one is needed"
    open(session_file + ".tmp", "w").close()        # a leftover that anyone could read
    token, key = cleaner.session()["token"], cleaner.session()["key"]
    assert len(token) >= 43 and len(key) >= 43 and token != key, "two secrets"
    assert (cleaner.session()["token"], cleaner.session()["key"]) == (token, key), "kept"
    assert os.stat(session_file).st_mode & 0o777 == 0o600, oct(os.stat(session_file).st_mode)
    made = json.load(open(session_file))["created"]
    assert cleaner.session(made + 29 * cleaner.DAY)["token"] == token
    renewed = cleaner.session(made + 31 * cleaner.DAY)
    assert renewed["token"] != token and renewed["key"] != key, "both are new after 30 days"
    assert os.stat(session_file).st_mode & 0o777 == 0o600, "and the new file is as private"
    for broken in ("{", json.dumps({"token": "short", "key": "k" * 43, "created": time.time()}),
                   json.dumps({"token": "t" * 43, "created": time.time()}), json.dumps(["not", "a", "session"])):
        with open(session_file, "w") as handle:
            handle.write(broken)
        token, earlier = cleaner.session()["token"], token
        assert token != earlier and json.load(open(session_file))["token"] == token, "an unreadable file is replaced"
    os.remove(session_file)
    assert cleaner.session()["token"] != token, "and so is a missing one"
    # A disk too full to save one: this launch's session goes on working, and is saved once it can be.
    os.remove(session_file)
    os.chmod(cleaner.state_dir(), 0o500)
    try:
        unsaved = cleaner.session()
        assert cleaner.session() == unsaved and not os.path.exists(session_file)
    finally:
        os.chmod(cleaner.state_dir(), 0o700)
    assert cleaner.session() == unsaved == json.load(open(session_file))
    assert os.stat(session_file).st_mode & 0o777 == 0o600
    token, key = unsaved["token"], unsaved["key"]
    cookie = f"headroom={token}"

    def call(path, body=None, cookie=cookie, host=host, run=cleaner.RUN, address="127.0.0.1", raw=None, extra=None,
             key=key):
        connection = http.client.HTTPConnection(address, port, timeout=30)
        headers = {"Host": host}
        if cookie:
            headers["Cookie"] = cookie
        if run:
            headers["X-Headroom"] = run
        if key:
            headers["X-Headroom-Key"] = key
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        connection.request("POST" if data is not None else "GET", path, body=data, headers=headers)
        reply = connection.getresponse()
        text = reply.read()
        connection.close()
        try:
            return reply.status, json.loads(text), reply
        except ValueError:
            return reply.status, text, reply

    # The page is the same for everyone on this Mac and holds no secret: this launch's id, the
    # nonce of its scripts, and whether a link on another site led here. Signed in or not, with a
    # login code on the address or not, loading it gives nothing away and changes nothing.
    code = cleaner.login_code()
    for path, sent_cookie, sent_key in (("/", None, None), ("/", cookie, key), (f"/?k={code}", None, None)):
        status, page, reply = call(path, cookie=sent_cookie, key=sent_key, run=None)
        assert status == 200 and cleaner.RUN.encode() in page and b"__RUN__" not in page, path
        assert token.encode() not in page and key.encode() not in page, "neither half of the session is in the page"
        assert reply.getheader("Set-Cookie") is None and reply.getheader("Location") is None, path
    assert code in cleaner.CODES, "loading the address does not spend its code"
    marked = b'<script nonce="' + cleaner.NONCE.encode() + b'">'
    assert page.count(marked) == page.count(b"<script") == 2 and b"__NONCE__" not in page
    policy = reply.getheader("Content-Security-Policy")
    assert f"script-src 'nonce-{cleaner.NONCE}';" in policy and "script-src 'unsafe-inline'" not in policy, policy
    assert not re.search(rb"<[^>]+\son\w+\s*=|javascript:", page, re.I), "nothing inline for the policy to stop"
    assert b'name="headroom-arrived" content=""' in page
    crossed = call("/", cookie=None, key=None, run=None, extra={"Sec-Fetch-Site": "cross-site"})[1]
    assert b'name="headroom-arrived" content="cross-site"' in crossed, "the page is told another site's link led here"

    # Signing in is the page's own call: it posts the code from its address and gets both halves
    # back, the cookie and the origin key. Only headroom's page may ask: not another site's page,
    # not a form, not anything without this launch's id. A code is good once, for two minutes.
    def login(code, **options):
        return call("/api/login", body={"code": code}, **{"cookie": None, "key": None, **options})

    assert 118 < cleaner.CODES[code] - time.time() <= 120
    for refused in ({"extra": {"Sec-Fetch-Site": "cross-site"}}, {"run": None}, {"host": "evil.example:80"},
                    {"extra": {"Content-Type": "text/plain"}}, {"extra": {"Content-Type": "application/x-www-form-urlencoded"}}):
        status, got, reply = login(code, **refused)
        assert status == 403 and "key" not in got and reply.getheader("Set-Cookie") is None, refused
    assert login(code, run="0123456789abcdef")[0] == 410, "a tab from an earlier launch"
    assert code in cleaner.CODES, "a caller that is turned away does not spend the code"
    status, got, reply = login(code)
    assert status == 200 and got == {"key": key}, got
    assert reply.getheader("Set-Cookie") == f"headroom={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict"
    status, got, reply = login(code)
    assert status == 403 and "key" not in got and reply.getheader("Set-Cookie") is None, "a code works once"
    assert login(code, cookie=cookie, key=key)[0] == 403, "also for a browser that is signed in already"
    late = cleaner.login_code()
    cleaner.CODES[late] = time.time() - 1
    assert login(late)[0] == 403, "and not after two minutes"
    assert late not in cleaner.CODES and code not in cleaner.CODES
    for wrong in ("wrong", token, key, "", None, ["a", "list"]):
        status, got, reply = login(wrong)
        assert status == 403 and "key" not in got and reply.getheader("Set-Cookie") is None, wrong

    # Every other call needs both halves, and this launch's id. The cookie alone is what a program
    # listening on another port of headroom.localhost can be sent, so by itself it opens nothing.
    assert call("/api/start")[0] == 200, "the cookie and the key"
    assert call("/api/start", key=None)[0] == 403, "the cookie alone"
    assert call("/api/start", cookie=None)[0] == 403, "the key alone"
    for sent_cookie, sent_key in ((cookie, "wrong"), (cookie, token), ("headroom=wrong", key), (f"headroom={key}", key),
                                  (f"headroom={key}", token), (None, None)):
        assert call("/api/start", cookie=sent_cookie, key=sent_key)[0] == 403, (sent_cookie, sent_key)
    assert call("/api/delete", body={"ids": ["x"]}, key=None)[0] == 403
    assert call("/api/delete", body={"ids": ["x"]}, cookie=None)[0] == 403
    assert call("/api/start", host=f"127.0.0.1:{port}")[0] == 200
    assert call("/api/start", host=f"[::1]:{port}", address="::1")[0] == 200, "the ::1 listener answers"
    assert call("/api/start", host="evil.example:80")[0] == 403, "foreign Host"
    assert call("/api/start", host=f"headroom.localhost:{port + 1}")[0] == 403
    assert call("/api/start", run="0123456789abcdef")[0] == 410, "a tab from an earlier launch"
    assert call("/api/delete", body={"ids": ["x"]}, run=None)[0] == 403, "POST without the custom header"
    assert call("/api/delete", body={"ids": ["no-such-row"]})[0] == 404
    assert call("/api/delete", body={"paths": ["/etc/hosts"]})[0] == 400
    assert call("/api/delete", body={"ids": [["not", "a", "string"]]})[0] == 400
    assert call("/api/delete", body={"ids": []})[0] == 400
    assert call("/api/trash", body={"path": "/etc/hosts"})[0] == 400, "paths are never accepted"
    assert call("/api/maintain", body={"id": "rm -rf /"})[0] == 404
    assert call("/", host="evil.example:80", cookie=None, key=None, run=None)[0] == 403, "DNS rebinding gets nothing"

    # A page that isn't signed in asks for a tab. All it gets is that tab, opened in this account's
    # own browser on a fresh login code, at most once in 15 seconds: another account on this Mac
    # can ask too. Another site's page can't, and loading the page opens nothing by itself.
    def invite(**options):
        return call("/api/invite", body={}, **{"cookie": None, "key": None, **options})

    def opened(count):
        for _ in range(100):
            if len(OPENED) >= count:
                break
            time.sleep(0.02)
        time.sleep(0.1)         # long enough for one too many to show up
        return len(OPENED) == count

    del OPENED[:]
    cleaner.OPENED = 0
    for refused in ({"extra": {"Sec-Fetch-Site": "cross-site"}}, {"run": None}, {"host": "evil.example:80"},
                    {"extra": {"Content-Type": "text/plain"}}):
        assert invite(**refused)[0] == 403, refused
    assert invite(run="0123456789abcdef")[0] == 410
    for site in ("none", "same-origin", "cross-site", None):
        assert call("/", cookie=None, key=None, run=None, extra={"Sec-Fetch-Site": site} if site else None)[0] == 200
    assert opened(0), "none of those opened anything"
    status, got, reply = invite()
    assert status == 200 and got == {} and reply.getheader("Set-Cookie") is None, got
    assert opened(1) and re.match(rf"^http://headroom\.localhost:{port}/\?k=[\w-]+$", OPENED[0]), OPENED
    for _ in range(5):
        assert invite()[0] == 200
    assert opened(1), "one tab in 15 seconds, however often it is asked for"
    assert login(OPENED[0].split("?k=")[1])[1] == {"key": key}, "the tab it opened signs in"
    cleaner.OPENED = 0
    assert invite(host=f"127.0.0.1:{port}")[0] == 200
    assert opened(2) and OPENED[1].startswith(f"http://127.0.0.1:{port}/?k="), "the tab opens where it was asked for"

    # --- the address headroom opens never sits on a command line ----------------------------------
    # It carries a login code, and every account on a Mac can read every process's arguments
    # (`ps`). So it goes to LaunchServices inside this process, or into osascript's input.
    assert cleaner.launch("x-headroom-selftest://x") == -10814, "LaunchServices was asked: no app opens such an address"
    url = f"http://headroom.localhost:{port}/?k={cleaner.login_code()}"
    real_launch, handed = cleaner.launch, []
    try:
        cleaner.launch = lambda address: handed.append(address) or 0
        with recording(Recorder()) as recorder:
            real_open_page(url)
        assert handed == [url] and recorder.calls == [], "handed over in this process: no program is started"
        for failure in (OSError("no such framework"), AttributeError("no such call"), -10814):
            def launch(address, failure=failure):
                if isinstance(failure, Exception):
                    raise failure
                return failure
            cleaner.launch = launch
            with recording(Recorder()) as recorder:
                real_open_page(url)
            assert recorder.calls == [["/usr/bin/osascript"]], recorder.calls
            assert recorder.inputs == [f'open location "{url}"'], recorder.inputs
        # Only an address of headroom's own shape: nothing that could end the script's quotes.
        for odd in (url + '" & (do shell script "id") & "', url + "\n", "file:///etc/hosts", "http://headroom.localhost/"):
            with recording(Recorder()) as recorder:
                try:
                    real_open_page(odd)
                    raise AssertionError(odd)
                except ValueError:
                    pass
            assert recorder.calls == [] and odd not in handed, odd
        # The whole way through: the page asks, the server opens, and still nothing on a command line.
        cleaner.OPENED, cleaner.open_page = 0, real_open_page
        with recording(Recorder()) as recorder:
            assert invite()[0] == 200
            for _ in range(100):
                if recorder.calls:
                    break
                time.sleep(0.02)
        assert recorder.calls == [["/usr/bin/osascript"]] and "?k=" in recorder.inputs[0], recorder.calls
    finally:
        cleaner.launch, cleaner.open_page = real_launch, OPENED.append

    # The tab icons can be cached (Safari shows a letter otherwise); nothing else can. A page on
    # another site gets none of them, so it can't tell from an icon that headroom is here.
    assert call("/favicon.svg", cookie=None, key=None, run=None)[1].startswith(b"<svg")
    for icon_path in ("/favicon.svg", "/favicon-32.png", "/favicon.ico", "/apple-touch-icon.png"):
        status, _, reply = call(icon_path, cookie=None, key=None, run=None)
        assert status == 200 and reply.getheader("Cache-Control") == "public, max-age=604800", icon_path
        assert call(icon_path, cookie=None, key=None, run=None, extra={"Sec-Fetch-Site": "same-origin"})[0] == 200, icon_path
        assert call(icon_path, extra={"Sec-Fetch-Site": "cross-site"})[0] == 404, icon_path
    assert call("/", run=None)[2].getheader("Cache-Control") == "no-store"
    assert call("/api/start")[2].getheader("Cache-Control") == "no-store"
    for path, sent in (("/", cookie), ("/", None), ("/api/start", cookie), ("/api/start", None), ("/favicon.svg", None)):
        reply = call(path, cookie=sent)[2]
        assert reply.getheader("X-Content-Type-Options") == "nosniff", path
        assert reply.getheader("Cross-Origin-Resource-Policy") == "same-origin", path
    # A POST body is read only for this Mac's page, and only a short one that says how long it is.
    for length, expected in (("abc", 400), ("-1", 400), (str(cleaner.MAX_BODY + 1), 413)):
        assert call("/api/ping", raw=b"", extra={"Content-Length": length})[0] == expected, length
    assert call("/api/ping", raw=b"{}", host="evil.example:80")[0] == 403

    # The page says goodbye as its tab closes, with a beacon: no custom headers, so the launch and
    # the key go in the body and the cookie comes by itself. All three, or nothing happens.
    def bye(run=cleaner.RUN, key=key, cookie=cookie):
        return call("/api/bye", raw=json.dumps({"run": run, "key": key}).encode(), cookie=cookie, run=None, key=None)[0]

    before = cleaner.DEADLINE
    for missing in ({"run": "not-this-run"}, {"run": None}, {"key": "wrong"}, {"key": None}, {"key": token},
                    {"cookie": None}, {"cookie": "headroom=wrong"}):
        assert bye(**missing) == 200 and cleaner.DEADLINE >= before, missing
    assert call("/api/bye", raw=cleaner.RUN.encode(), run=None, key=None)[0] == 200 and cleaner.DEADLINE >= before
    for body in ({}, {"run": cleaner.RUN}, {"key": key}):       # the rest in headers, where a beacon has none
        assert call("/api/bye", raw=json.dumps(body).encode())[0] == 200 and cleaner.DEADLINE >= before, body
    assert bye() == 200 and cleaner.DEADLINE < before, "the page's own bye shortens the wait"
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

    # Duplicates, startup items and reminders over the API: ids only.
    assert call("/api/dupes", body={})[0] == 200
    for _ in range(100):
        found = call("/api/dupes")[1]
        if not found["running"]:
            break
        time.sleep(0.1)
    assert len(found["groups"]) == 1 and "path" not in json.dumps(found["groups"]), found
    assert call("/api/dupes/trash", body={"paths": ["/etc/hosts"]})[0] == 400
    assert call("/api/dupes/trash", body={"groups": [{"id": "x", "keep": ["y"]}]})[0] == 400
    assert call("/api/startup", body={"id": "x"})[0] == 400, "on or off must be said"
    assert call("/api/settings")[1]["alert_gb"] == 20
    with recording(Recorder()) as recorder:
        assert call("/api/settings", body={"alert": False, "alert_gb": 15})[1] == {
            "alert": False, "weekly": True, "alert_gb": 15, "trash": False, "available": True}
        assert call("/api/settings", body={"alert_gb": 99999})[1]["alert_gb"] == 15, "a silly number is ignored"
    assert recorder.calls and all(c[0] == "/bin/launchctl" for c in recorder.calls)
    space = call("/api/space")[1]
    assert space["samples"] and space["total_kb"] > 0

    # Slow sources are named, so the page can call the scan finished without them.
    sources = {s["id"]: s for s in call("/api/start")[1]["sources"]}
    assert {name: s["slow"] for name, s in sources.items() if s["slow"]} == {
        "homebrew": "Homebrew", "docker": "Docker", "simulators": "the simulators"}, sources
    assert {"screenshots", "models", "archives", "browser_copies"} <= set(sources) and sources["builds"]["groups"] == ["builds", "dev"]

    # Move to Trash over the API: a setting, read once when a delete starts. Finder moves the row, rm never runs.
    def wait_for_job():
        for _ in range(200):
            progress = call("/api/progress")[1]
            if not progress["running"]:
                return progress
            time.sleep(0.05)
        raise AssertionError("the delete did not finish")

    def api_cache(name):
        folder = os.path.dirname(put("Library", "Caches", "com.switch." + name, "blob", kb=200))
        return folder, next(r for r in call("/api/scan?source=caches")[1]["items"] if r["name"] == name)
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        assert call("/api/settings", body={"trash": True})[1]["trash"] is True
        assert call("/api/start")[1]["trash"] is True
        first_cache, row = api_cache("api")
        assert row["blocked"] == "" and row["action"] == "" and row["rank"] == 0 and row["weighs"] == 0 and "paths" not in row
        assert call("/api/delete", body={"ids": [row["id"]]})[0] == 200
        progress = wait_for_job()
        assert call("/api/settings", body={"trash": "yes"})[1]["trash"] is True, "only true or false changes it"
        assert call("/api/settings", body={"trash": False})[1]["trash"] is False
        # The question the page asked promised the Trash. The switch has been turned off since (in
        # another tab, or while an earlier delete ran): the promise is kept, and rm does not run.
        promised_cache, promised = api_cache("promised")
        assert call("/api/delete", body={"ids": [promised["id"]], "trash": True})[0] == 200
        kept_promise = wait_for_job()
    assert progress["trash"] is True and progress["results"][0]["trashed"] and progress["results"][0]["errors"] == [], progress
    assert kept_promise["trash"] is True and kept_promise["results"][0]["trashed"], kept_promise
    assert [c for c in recorder.calls if c[0] == "/usr/bin/osascript"] == [finder + [first_cache], finder + [promised_cache]]
    assert os.path.exists(first_cache) and os.path.exists(promised_cache), "the recorder stood in for Finder"
    assert call("/api/history")[1]["items"][0]["result"] == "trashed" and call("/api/start")[1]["trash"] is False
    # Never the reverse: with the switch on, a request that says "not the Trash" still goes to the Trash.
    with recording(Recorder(never=("/bin/rm",))) as recorder:
        assert call("/api/settings", body={"trash": True})[1]["trash"] is True
        on_cache, on_row = api_cache("stays-on")
        assert call("/api/delete", body={"ids": [on_row["id"]], "trash": False})[0] == 200
        assert wait_for_job()["results"][0]["trashed"]
        # While a delete runs, where things go can't be changed under it.
        cleaner.JOB["running"] = True
        assert call("/api/settings", body={"trash": False, "alert_gb": 25})[1] == {
            "alert": False, "weekly": True, "alert_gb": 25, "trash": True, "available": True}
        cleaner.JOB["running"] = False
        assert call("/api/settings", body={"trash": False, "alert_gb": 15})[1]["trash"] is False
    assert [c for c in recorder.calls if c[0] == "/usr/bin/osascript"] == [finder + [on_cache]]
    # A settings file that is there but can't be read: nobody knows where things were meant to go,
    # so nothing is deleted on a guess. Setting the switch again writes a good file.
    api_file = os.path.join(cleaner.state_dir(), "settings.json")
    good = open(api_file).read()
    doubt_cache, doubt = api_cache("doubt")
    for broken in ("{not json", '{"trash": 1}'):
        with open(api_file, "w") as handle:
            handle.write(broken)
        status, said, _ = call("/api/delete", body={"ids": [doubt["id"]]})
        assert status == 500 and "can't read its settings" in said["error"] and os.path.exists(doubt_cache), (broken, status, said)
        assert call("/api/progress")[1]["running"] is False
    with recording(Recorder()):
        assert call("/api/settings", body={"trash": False})[1]["trash"] is False
    assert not cleaner.settings_broken() and json.load(open(api_file))["trash"] is False
    with open(api_file, "w") as handle:
        handle.write(good)

    # What grew over the API: every scan leaves the day's sizes behind, and a delete updates them.
    assert call("/api/growth")[1] == {"span": "", "sections": {}, "top": []}, "one day of history claims nothing"
    today = time.strftime("%Y-%m-%d")
    kept = json.load(open(sizes_file))
    assert list(kept) == [today] and kept[today]["caches"][doubt["id"]] == ["apps", "doubt", doubt["kb"]]
    assert call("/api/delete", body={"ids": [doubt["id"]]})[0] == 200
    progress = wait_for_job()
    assert not os.path.exists(doubt_cache) and progress["trash"] is False and not progress["results"][0]["trashed"]
    assert json.load(open(sizes_file))[today]["caches"][doubt["id"]] == ["apps", "doubt", 0]

    # One scan of a source at a time: a second request while the first is running waits for it and
    # gets the same answer, so "Scan again" never starts a slow source's programs twice over.
    started, release = [], threading.Event()

    def slow_scan():
        started.append(time.time())
        release.wait(10)
        return [cleaner.item("dev", "slow thing", [os.path.join(cleaner.HOME, "Library", "Caches", "com.switch.two")], 5000)]
    cleaner.SOURCES["slowpoke"] = (("dev",), slow_scan)
    answers = []
    askers = [threading.Thread(target=lambda: answers.append(call("/api/scan?source=slowpoke")[1])) for _ in range(3)]
    for asker in askers:
        asker.start()
    time.sleep(0.5)
    release.set()
    for asker in askers:
        asker.join(20)
    assert len(started) == 1 and len(answers) == 3 and answers[0] == answers[1] == answers[2], (len(started), answers)
    assert answers[0]["items"][0]["name"] == "slow thing" and cleaner.SCANNING == {}
    del cleaner.SOURCES["slowpoke"]
    cleaner.FOUND.pop("slowpoke", None)

    # The Disk tab answers with an error when measuring cannot even start; it does not go quiet.
    real_places = cleaner.explore_places

    def no_room(pid):
        raise OSError(7, "Argument list too long")
    cleaner.explore_places = no_room
    status, said, _ = call("/api/explore?view=places")
    cleaner.explore_places = real_places
    assert status == 500 and said == {"error": "Argument list too long."}, (status, said)

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
    # a browser. It names the address on its first line and answers on both sockets. Its home is
    # the throwaway one too, so the session it hands out is this test's, never the real one.
    held = [socket.socket(socket.AF_INET), socket.socket(socket.AF_INET6)]
    held[0].bind(("127.0.0.1", 0))
    held[1].bind(("::1", held[0].getsockname()[1]))
    for sock in held:
        sock.listen(16)
    port = held[0].getsockname()[1]
    fds = [sock.fileno() for sock in held]
    child = subprocess.Popen([sys.executable, cleaner.__file__, "--no-open"], stdout=subprocess.PIPE, text=True,
                             pass_fds=fds, env={**os.environ, "HOME": cleaner.HOME,
                                                "HEADROOM_LISTEN_FDS": ",".join(map(str, fds))})
    try:
        line = child.stdout.readline()
        assert re.match(rf"^headroom is open at http://headroom\.localhost:{port}/\?k=[\w-]+$", line.strip()), line
        assert token not in line and key not in line, "the address carries a login code, never the session"
        for sock in held:
            sock.close()                    # only the child holds them now
        name, code = f"headroom.localhost:{port}", line.strip().split("?k=")[1]
        for address, expected in (("127.0.0.1", 200), ("::1", 403)):       # the code works once, on either socket
            connection = http.client.HTTPConnection(address, port, timeout=30)
            connection.request("GET", f"/?k={code}", headers={"Host": name})
            shell = connection.getresponse()
            run_id = re.search(r'name="headroom-run" content="(\w+)"', shell.read().decode()).group(1)
            assert shell.status == 200 and shell.getheader("Set-Cookie") is None, address
            connection = http.client.HTTPConnection(address, port, timeout=30)
            connection.request("POST", "/api/login", body=json.dumps({"code": code}),
                               headers={"Host": name, "X-Headroom": run_id, "Content-Type": "application/json"})
            reply = connection.getresponse()
            got = json.loads(reply.read())
            assert reply.status == expected, (address, reply.status)
            if expected == 200:
                assert got == {"key": key} and reply.getheader("Set-Cookie").startswith(f"headroom={token};")
            connection.close()
    finally:
        child.terminate()
        child.wait(10)

    # Update check: GitHub is asked once a day unless forced, a failure keeps the last answer,
    # and a mangled saved answer is ignored.
    real_newest, asked = cleaner.newest_release, []
    def newest(version):
        def answer():
            asked.append(version)
            if isinstance(version, Exception):
                raise version
            return version
        return answer
    try:
        cleaner.newest_release = newest("9.0")
        info = cleaner.update_info(force=True)
        assert info["newer"] and info["latest"] == "9.0" and info["url"].endswith("/tag/v9.0"), info
        cleaner.newest_release = newest("9.1")
        assert cleaner.update_info()["latest"] == "9.0" and asked == ["9.0"], "checked again within a day"
        cleaner.newest_release = newest(OSError("offline"))
        info = cleaner.update_info(force=True)
        assert info["error"] and info["latest"] == "9.0" and info["newer"], info
        cleaner.newest_release = newest(cleaner.VERSION)
        assert not cleaner.update_info(force=True)["newer"], "same version shown as an update"
        cleaner.save_state("update.json", {"checked": time.time(), "latest": "../evil"})
        info = cleaner.update_info()
        assert info["latest"] is None and not info["newer"], info
    finally:
        cleaner.newest_release = real_newest
    # The Finder script once called its variable `target`, a word of Finder's own: inside the tell,
    # Finder looked up its own "target" and every move failed (-1728). The stand-in Finder here
    # can't show that, so the word itself is checked. Verified against the real Finder by hand.
    assert not re.search(r"\bset target\b|\bdelete target\b", cleaner.TRASH_SCRIPT), cleaner.TRASH_SCRIPT
    print("ok")
finally:
    listener.close()
    subprocess.run(["/bin/chmod", "-R", "u+rwx", box])
    shutil.rmtree(box)
