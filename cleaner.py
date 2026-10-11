#!/usr/bin/env python3
"""headroom: a cache cleaner for this Mac.

A page served only to this Mac that lists cache folders and deletes the ones
you tick. What can be deleted is decided here, by the scan: the page sends back
the ids of rows it was shown, never a path.
"""
import calendar
import collections
import ctypes
import datetime
import errno
import functools
import hashlib
import http.cookies
import json
import os
import plistlib
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def run(command, timeout=None):
    """Start a program and collect what it prints.

    close_fds=False lets Python use posix_spawn rather than fork. A Mac with a full disk runs
    out of memory to fork with, which is exactly when this tool gets opened, and spawn still works.
    """
    return subprocess.run(command, capture_output=True, text=True, close_fds=False, timeout=timeout)


HOME = os.path.realpath(os.path.expanduser("~"))
TMP = os.path.realpath(run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"]).stdout.strip() or "/nonexistent")
# Where ui.html sits: beside this file, or inside the bundle when package.sh froze it into an app.
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
FROZEN = getattr(sys, "frozen", False)

# The page lives at http://headroom.localhost:8460. The address opened first carries TOKEN once
# (?k=...), which becomes a cookie; RUN marks this launch, so a tab left over from an earlier
# one is turned away instead of acting on this one.
NAME = "headroom.localhost"
PORTS = list(range(8460, 8480))
TOKEN = secrets.token_urlsafe(24)
RUN = secrets.token_hex(8)
COOKIE = f"headroom={TOKEN}; Path=/; HttpOnly; SameSite=Strict"

APPLICATIONS = "/Applications"
SYSTEM_APPLICATIONS = "/System/Applications"
# Command-line tools keep preferences too, without an app. Their names count as installed.
TOOL_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/homebrew/Caskroom", "/usr/local/Caskroom"]

MIN_KB = 1024              # a row under 1 MB is not worth a line
SMALL_KB = 5 * 1024        # cache folders under 5 MB share one row
BUILD_MIN_KB = 20 * 1024   # same idea for build folders inside projects
DAY = 24 * 60 * 60
IDLE = 5 * 60              # the server stops this long after the page goes quiet
RECENT = 7 * DAY           # used this recently: ticking a whole section leaves it out
SAFE_GROUPS = ("apps", "temp")   # what "Safe clean" ticks, with installers older than a month

# Folder names worth a plainer label, and the section they belong in.
KNOWN = {
    "ms-playwright": ("dev", "Playwright browsers"),
    "Homebrew": ("dev", "Homebrew downloads"),
    "pip": ("dev", "pip downloads"),
    "CocoaPods": ("dev", "CocoaPods cache"),
    "Yarn": ("dev", "Yarn cache"),
    "pnpm": ("dev", "pnpm cache"),
    "node-gyp": ("dev", "node-gyp headers"),
    "typescript": ("dev", "TypeScript type downloads"),
    "electron": ("dev", "Electron downloads"),
    "go-build": ("dev", "Go build cache"),
    "uv": ("dev", "uv (Python) downloads"),
    "torch": ("dev", "PyTorch downloads"),
    "huggingface": ("dev", "Hugging Face downloads"),
    "selenium": ("dev", "Selenium drivers"),
    "firebase": ("dev", "Firebase emulators"),
    "puppeteer": ("dev", "Puppeteer browsers"),
}
BUNDLES = {"com.todesktop.230313mzl4w4u92": "Cursor", "ai.elementlabs.lmstudio": "LM Studio"}
# Apple's caches without the com.apple. prefix: macOS looks after them like the rest.
APPLE_CACHES = {"CloudKit", "FamilyCircle", "GeoServices"}

# ~/.cache is disposable by convention, but LM Studio keeps its model library there.
NOT_CACHE = ("lm-studio",)

# What a Chromium-based app (Slack, VS Code, Notion ...) keeps beside its real data.
CHROMIUM_CACHES = (
    "Cache", "Code Cache", "GPUCache", "DawnWebGPUCache", "DawnGraphiteCache",
    "CachedData", "CachedExtensionVSIXs", "Crashpad", "logs",
    os.path.join("Service Worker", "CacheStorage"), os.path.join("Service Worker", "ScriptCache"),
)
# Application Support folder -> the app's name, where the two differ.
APP_NAMES = {
    "Code": "Visual Studio Code", "Docker Desktop": "Docker", "arduino-ide": "Arduino IDE",
    "UnityHub": "Unity Hub", "RedisInsight": "Redis Insight", "discord": "Discord",
    "obsidian": "Obsidian", "apidog": "Apidog",
}

# Browsers: the main program (not its helpers), cache folders in ~/Library/Caches, and the
# profile folder in ~/Library/Application Support. Only caches: history, logins and cookies stay.
BROWSERS = (
    ("Safari", r"/Safari\.app/Contents/MacOS/Safari\b", ("com.apple.Safari",), None),
    ("Chrome", r"/Google Chrome\.app/Contents/MacOS/Google Chrome\b", ("Google/Chrome",), "Google/Chrome"),
    ("Firefox", r"\.app/Contents/MacOS/firefox\b", (), None),
    ("Arc", r"/Arc\.app/Contents/MacOS/Arc\b", ("Arc", "company.thebrowser.Browser"), "Arc/User Data"),
    ("Brave", r"/Brave Browser\.app/Contents/MacOS/Brave Browser\b",
     ("BraveSoftware/Brave-Browser",), "BraveSoftware/Brave-Browser"),
    ("Edge", r"/Microsoft Edge\.app/Contents/MacOS/Microsoft Edge\b", ("Microsoft Edge",), "Microsoft Edge"),
)
PROFILE_CACHES = ("Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache")
SHARED_CACHES = ("GrShaderCache", "GraphiteDawnCache", "ShaderCache")
FIREFOX_CACHES = ("cache2", "startupCache", "thumbnails", "jumpListCache", "shader-cache")
# ~/Library/Caches folders the browser rows cover, so App caches leaves them out.
BROWSER_CACHES = {"Google", "Firefox", "Arc", "company.thebrowser.Browser", "BraveSoftware", "Microsoft Edge"}

INSTALLERS = (".dmg", ".pkg", ".mpkg", ".iso", ".xip")

# Where apps leave things in ~/Library, named by bundle id, and what to call each place.
TRACES = {
    "Preferences": "Preferences", "Application Support": "Application Support", "Caches": "Caches",
    "Saved Application State": "Saved windows", "HTTPStorages": "Web storage", "WebKit": "WebKit data",
    "Containers": "Container",
}
BUNDLE_ID = re.compile(r"^[A-Za-z]{2,10}(\.[\w-]+){2,}$")
# Words in a bundle id that say nothing about which app it is.
GENERIC = {"desktop", "app", "mac", "macos", "osx", "client", "helper", "agent", "shipit", "main",
           "application", "launcher", "plist", "savedstate"}

# Editors that keep every extension version they update to: name, folder in ~, main program.
EDITORS = (
    ("VS Code", ".vscode", r"/Visual Studio Code\.app/Contents/MacOS/"),
    ("Cursor", ".cursor", r"/Cursor\.app/Contents/MacOS/"),
    ("Windsurf", ".windsurf", r"/Windsurf\.app/Contents/MacOS/"),
)
EXTENSION = re.compile(r"^([\w-]+\.[\w-]+?)-(\d+(?:\.\d+)+)(?:-([a-z][\w-]*))?$", re.I)   # ms-python.python-2024.2.1

# Folders a project can rebuild, each tied to the file that proves what kind of project it is.
PROJECT_DIRS = {
    "node_modules": "package.json", ".next": "package.json", "dist": "package.json",
    "target": "Cargo.toml", ".build": "Package.swift",
    "build": "pubspec.yaml", ".dart_tool": "pubspec.yaml",
}
SKIP_HOME = {"Library", "Applications", "Pictures", "Music", "Movies", "Public"}
SKIP_ANYWHERE = {".git", "venv", ".venv", "Pods", "site-packages", "__pycache__", ".gradle", ".idea"}
BUNDLE_SUFFIXES = (".app", ".photoslibrary", ".xcodeproj", ".xcworkspace", ".framework")
MAX_DEPTH = 8

DOCKERS = (
    "/usr/local/bin/docker", "/opt/homebrew/bin/docker",
    "/Applications/Docker.app/Contents/Resources/bin/docker",
)
DU_SLOTS = threading.Semaphore(6)

# The headroom mark for the browser tab: three bars, the short mint one on top, on a dark tile
# so it reads on light and dark tab strips alike.
ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#0b0b0b"/>
<g transform="translate(10 10) scale(2)">
<rect x="2" y="2.5" width="9" height="4.5" rx="2.25" fill="#7be0a8"/>
<rect x="2" y="9" width="18" height="4.5" rx="2.25" fill="#f3f4f1"/>
<rect x="2" y="15.5" width="18" height="4.5" rx="2.25" fill="#f3f4f1"/>
</g>
</svg>
"""


def home(*parts):
    return os.path.join(HOME, *parts)


def tilde(path):
    return "~" + path[len(HOME):] if path.startswith(HOME + os.sep) else path


def now():
    """The local time as ISO 8601 with a colon in the offset, which every browser can parse."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def count(number, one, many):
    return f"{number} {one if number == 1 else many}"


def ago(when):
    """A timestamp as people say it: today, 3 weeks ago, 2 years ago."""
    days = (time.time() - when) / DAY
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    for limit, size, unit in ((14, 1, "day"), (60, 7, "week"), (730, 30, "month"), (None, 365, "year")):
        if limit is None or days < limit:
            return count(int(days // size), unit, unit + "s") + " ago"


def children(folder, skip=()):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [os.path.join(folder, n) for n in sorted(names) if n not in skip and n != ".DS_Store"]


def _du(paths):
    sizes = {}
    for start in range(0, len(paths), 400):
        with DU_SLOTS:
            done = run(["/usr/bin/du", "-sk", "--"] + paths[start:start + 400])
        for line in done.stdout.splitlines():
            kb, _, path = line.partition("\t")
            if kb.strip().isdigit():
                sizes[path] = int(kb)
    return sizes


def du(paths):
    """Kilobytes on disk for every path that exists, as {path: kb}."""
    paths = [p for p in paths if os.path.lexists(p)]
    shares = [s for s in (paths[i::6] for i in range(6)) if s]
    sizes = {}
    with ThreadPoolExecutor(6) as pool:
        for part in pool.map(_du, shares):
            sizes.update(part)
    return sizes


def item(group, name, paths, kb, detail="", cmd=None, quit=None, blocked=""):
    """One row. `paths` is exactly what gets removed; with `cmd`, the command does the removing."""
    key = "\0".join([group, name] + sorted(paths))
    return {
        "id": hashlib.sha1(key.encode()).hexdigest()[:16],
        "group": group, "name": name, "detail": detail, "paths": paths, "kb": kb,
        "cmd": cmd, "quit": quit, "blocked": blocked,
        "note": "",          # one plain line under the row
        "recent": False,     # used in the last week: ticking the whole section leaves it out
        "safe": False,       # "Safe clean" may tick it
        "tidy": False,       # worth a line even under MIN_KB: clutter you'd still want gone
    }


def readable(folder_name):
    """com.colliderli.iina -> iina. A best guess; the row shows the real path underneath."""
    update = folder_name.endswith(".ShipIt")
    base = folder_name[:-len(".ShipIt")] if update else folder_name
    label = BUNDLES.get(base)
    if not label:
        label = base.rsplit(".", 1)[-1] if re.match(r"^[a-z]{2,6}\.[\w-]+\.[\w.-]+$", base) else base
    return f"{label} update download" if update else label


def listing(folder, shut, who):
    """What a folder holds. A folder macOS keeps locked adds `who` to `shut`."""
    try:
        return [os.path.join(folder, n) for n in sorted(os.listdir(folder)) if n != ".DS_Store"]
    except FileNotFoundError:
        return []
    except OSError:
        shut.append(who)
        return []


def sealed(path):
    """True when macOS will not let this process look inside: another app's protected data."""
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return False
        for name in os.listdir(path)[:50]:
            inner = os.path.join(path, name)
            if os.path.isdir(inner) and not os.path.islink(inner):
                os.listdir(inner)
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def program_paths():
    """The program file of every running process, straight from the kernel (no `ps`)."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    count = libc.proc_listallpids(None, 0)
    pids = (ctypes.c_int * (count * 2))()
    count = libc.proc_listallpids(pids, ctypes.sizeof(pids))
    found, buffer = set(), ctypes.create_string_buffer(4096)
    for pid in pids[:count]:
        if libc.proc_pidpath(pid, buffer, 4096) > 0:
            found.add(buffer.value.decode(errors="replace"))
    return found


def launched_by():
    """The app macOS asks about permissions for: headroom when its app started this server."""
    buffer = ctypes.create_string_buffer(4096)
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    parent = buffer.value.decode(errors="replace") if libc.proc_pidpath(os.getppid(), buffer, 4096) > 0 else ""
    if FROZEN or "/headroom.app/" in parent:
        return "headroom"
    app = re.search(r"([^/]+)\.app/Contents/", parent)
    return app.group(1) if app else "the app you started headroom from"


def full_disk_access(places):
    places = ", ".join(places[:-1]) + " and " + places[-1] if len(places) > 1 else places[0]
    return (f"macOS keeps {places} behind Full Disk Access. To clear them, open System "
            f"Settings, Privacy & Security, Full Disk Access, switch on {launched_by()}, then quit and "
            "reopen headroom.")


# --- what the cleaning list finds -------------------------------------------------------------

def cache_rows(folder, group, skip=()):
    """One row for each sizeable folder in a cache directory, and one for all the small ones."""
    # Apple's own caches are tiny, partly protected, and looked after by macOS.
    sizes = du([p for p in children(folder, skip) if not os.path.basename(p).startswith("com.apple.")])
    rows, small = [], []
    for path, kb in sizes.items():
        if kb < SMALL_KB:
            small.append(path)
            continue
        named_group, label = KNOWN.get(os.path.basename(path), (group, readable(os.path.basename(path))))
        rows.append(item(named_group, label, [path], kb, tilde(path)))
    if small:
        name = count(len(small), "smaller cache", "smaller caches")
        rows.append(item(group, name, small, sum(sizes[p] for p in small), tilde(folder)))
    return rows


def orphan_names(folder):
    """Folder names in ~/Library/<folder> that the deleted-app leftovers row already covers."""
    return {os.path.basename(p) for group in orphans().values() for p in group["paths"]
            if os.path.dirname(p) == home("Library", folder)}


def scan_caches():
    skip = BROWSER_CACHES | APPLE_CACHES | orphan_names("Caches")
    return cache_rows(home("Library", "Caches"), "apps", skip)


def scan_home_cache():
    return cache_rows(home(".cache"), "dev", skip=NOT_CACHE)


def scan_app_data():
    apps = {}
    gone = orphan_names("Application Support")
    for folder in children(home("Library", "Application Support"), skip=gone):
        if os.path.isdir(os.path.join(folder, "Code Cache")) or os.path.isdir(os.path.join(folder, "GPUCache")):
            caches = (os.path.join(folder, c) for c in CHROMIUM_CACHES)
            apps[folder] = [p for p in caches if os.path.isdir(p) and not os.path.islink(p)]
    sizes = du([p for paths in apps.values() for p in paths])
    rows = []
    for folder, paths in apps.items():
        name = os.path.basename(folder)
        label = APP_NAMES.get(name, name)
        # A running Chromium app keeps these files open and writes them back on quit. Only the app
        # itself counts: helpers inside its bundle (WhatsApp's notification extension, say) keep
        # running after the app quits and do not touch these folders.
        running = [re.escape(f"/{label}.app/Contents/MacOS/"), re.escape(f"--user-data-dir={folder}") + r"(?= --|$)"]
        rows.append(item(
            "apps", f"{label} cache", paths, sum(sizes.get(p, 0) for p in paths), tilde(folder),
            quit=(f"Quit {label} first", running),
        ))
    return rows


# Media caches some apps keep outside ~/Library/Caches: name, folders in Application Support,
# what happens, and the app's main program.
APP_EXTRAS = (
    ("Adobe media cache", ["Adobe/Common/Media Cache Files", "Adobe/Common/Media Cache"],
     "Premiere and After Effects make previews again as you play.",
     ("Quit Premiere and After Effects first",
      [r"/Adobe (Premiere|After Effects|Media Encoder|Audition|Character Animator)[^/]*\.app/Contents/MacOS/"])),
    ("Spotify cache", ["Spotify/PersistentCache"],
     "Songs stream again, and songs you downloaded for offline listening download again.",
     ("Quit Spotify first", [r"/Spotify\.app/Contents/MacOS/"])),
)


def scan_app_extras():
    rows = []
    for name, folders, note, quit in APP_EXTRAS:
        paths = [home("Library", "Application Support", *f.split("/")) for f in folders]
        paths = [p for p in paths if os.path.isdir(p) and not os.path.islink(p)]
        if paths:
            rows.append(item("apps", name, paths, sum(du(paths).values()), f"{note} {tilde(paths[0])}", quit=quit))
    return rows


def installed_apps():
    """{lower-case bundle id: app path} for the apps in the Applications folders."""
    found = {}
    for app in app_bundles([APPLICATIONS, home("Applications")], depth=1):
        ident = bundle_info(app).get("CFBundleIdentifier")
        if isinstance(ident, str):
            found.setdefault(ident.lower(), app)
    return found


def scan_sandboxed():
    """Caches inside sandboxed apps' containers: ~/Library/Containers/<id>/Data/Library/Caches,
    and their shared group containers. macOS lets only Full Disk Access look in there."""
    apps = installed_apps()
    places, shut = {}, []
    for container in children(home("Library", "Containers")):
        ident = os.path.basename(container).lower()
        if ident in apps and not ident.startswith("com.apple."):
            places.setdefault(apps[ident], []).append(os.path.join(container, "Data", "Library", "Caches"))
    for group in children(home("Library", "Group Containers")):
        # group.net.whatsapp.WhatsApp.shared or 6N38VWS5BX.ru.keepcoder.Telegram: the app whose id starts it
        rest = re.sub(r"^(group\.|[A-Z0-9]{10}\.)", "", os.path.basename(group)).lower()
        owner = next((apps[i] for i in sorted(apps, key=len, reverse=True)
                      if rest == i or rest.startswith(i + ".")), None)
        if owner and not rest.startswith("com.apple."):
            places.setdefault(owner, []).append(os.path.join(group, "Library", "Caches"))
    rows = []
    for app, folders in places.items():
        paths = [p for folder in folders for p in listing(folder, shut, "the caches inside sandboxed apps")]
        if paths:
            name = os.path.basename(app)[:-len(".app")]
            rows.append(item("apps", f"{name} cache", paths, sum(du(paths).values()),
                             f"Inside its sandbox. {tilde(os.path.commonpath(folders))}",
                             quit=(f"Quit {name} first", [re.escape(f"{app}/Contents/MacOS/")])))
    return rows, full_disk_access([shut[0]]) if shut else ""


def scan_logs():
    logs = children(home("Library", "Logs"), skip={"headroom"})     # headroom keeps its history there
    return [item("temp", "App logs", logs, sum(du(logs).values()), "~/Library/Logs")]


def has_socket(folder):
    try:
        return any(stat.S_ISSOCK(e.stat(follow_symlinks=False).st_mode) for e in os.scandir(folder))
    except OSError:
        return True


def newest(path, info):
    """When a temp entry was last changed, looking one level into folders."""
    latest = info.st_mtime
    if stat.S_ISDIR(info.st_mode):
        try:
            for entry in os.scandir(path):
                latest = max(latest, entry.stat(follow_symlinks=False).st_mtime)
        except OSError:
            return time.time()
    return latest


def scan_temp():
    """Leftovers in the per-user temp folder that no running program still looks busy with."""
    old = []
    for path in children(TMP):
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISSOCK(info.st_mode) or os.path.basename(path).startswith("com.apple."):
            continue
        if stat.S_ISDIR(info.st_mode) and has_socket(path):
            continue
        if newest(path, info) < time.time() - DAY:
            old.append(path)
    detail = count(len(old), "leftover", "leftovers") + " that nothing has touched for over a day"
    return [item("temp", "Old temporary files", old, sum(du(old).values()), detail)]


@functools.lru_cache(maxsize=None)
def developer_dir():
    return run(["/usr/bin/xcode-select", "-p"]).stdout.strip()


def scan_dev_tools():
    xcode = home("Library", "Developer", "Xcode")
    gradle = ("Gradle is running. Stop it first: pkill -f GradleDaemon", ["GradleDaemon"])
    xcode_open = ("Quit Xcode first", [re.escape("/Xcode.app/Contents/MacOS/Xcode")])
    table = [
        ("npm downloads", [home(".npm", n) for n in ("_cacache", "_npx", "_logs")], None),
        ("Bun downloads", [home(".bun", "install", "cache")], None),
        ("pnpm store", [home("Library", "pnpm", "store"), home(".pnpm-store")], None),
        ("Yarn cache", [home(".yarn", "cache"), home(".yarn", "berry", "cache")], None),
        ("Gradle caches", [home(".gradle", "caches"), home(".gradle", "daemon"),
                           home(".gradle", "wrapper", "dists")], gradle),
        ("Dart and Flutter packages", [home(".pub-cache", n) for n in ("hosted", "hosted-hashes", "git")], None),
        ("Dart analysis cache", [home(".dartServer", ".analysis-driver")], None),
        ("Rust crate downloads", [home(".cargo", "registry"), home(".cargo", "git")], None),
        ("Xcode build data", children(os.path.join(xcode, "DerivedData")), xcode_open),
        ("Xcode device support files", [os.path.join(xcode, f"{system} DeviceSupport")
                                        for system in ("iOS", "macOS", "watchOS", "tvOS", "visionOS")], None),
        ("Simulator caches", [home("Library", "Developer", "CoreSimulator", "Caches")], None),
    ]
    for conda in ("anaconda3", "miniconda3", "miniforge3"):
        # Only the downloaded archives: the unpacked packages are linked into environments.
        tarballs = [p for p in children(home(conda, "pkgs")) if p.endswith((".conda", ".tar.bz2"))]
        table.append(("Conda package downloads", tarballs, None))
    for sdk in [home("Documents", "flutter")] + children(home("fvm", "versions")):
        cache = os.path.join(sdk, "bin", "cache")
        busy = ("Flutter or Dart is running from this SDK. Quit your editor first", [re.escape(cache)])
        table.append((f"Flutter SDK downloads ({os.path.basename(sdk)})", [cache], busy))
    table.append(("Older Claude Code versions", old_claude_versions(), None))
    table += [
        ("Unity cache", [home("Library", "Unity", "cache")], None),
        ("Arduino downloads", [home("Library", "Arduino15", "staging")], None),
        ("PlatformIO cache", [home(".platformio", ".cache")], None),
        ("Maven downloads", [home(".m2", "repository")], None),
        ("NuGet packages", [home(".nuget", "packages")], None),
        ("Ruby gem downloads", [os.path.join(v, "cache") for v in children(home(".gem", "ruby"))]
         + [home(".gem", "specs")], None),
        ("Composer cache", [home(".composer", "cache")], None),
        ("Ivy and sbt downloads", [home(".ivy2", "cache"), home(".sbt", "boot")], None),
        ("Deno cache", children(home(".deno"), skip={"bin"}), None),         # bin holds deno itself
        ("Expo cache", [p for p in children(home(".expo")) if os.path.isdir(p)], None),    # not its login
        ("Rust toolchain downloads", [home(".rustup", "downloads"), home(".rustup", "tmp")], None),
    ]
    for editor, folder, pattern in EDITORS:
        table.append((f"Old {editor} extension versions", old_extensions(home(folder, "extensions")),
                      (f"Quit {editor} first", [pattern])))
    sizes = du([p for _, paths, _ in table for p in paths])
    rows = []
    for name, paths, quit in table:
        present = [p for p in paths if p in sizes]
        if present:
            rows.append(item("dev", name, present, sum(sizes[p] for p in present),
                             tilde(os.path.commonpath(present)), quit=quit))
    return rows + go_rows()


def old_extensions(folder):
    """Extension folders an editor no longer loads: older copies of an extension it has since
    updated, and ones it marked obsolete. Whatever extensions.json points at always stays."""
    obsolete = load_json(os.path.join(folder, ".obsolete"), {})
    listed = load_json(os.path.join(folder, "extensions.json"), [])
    keep = set()
    for entry in listed if isinstance(listed, list) else []:
        if isinstance(entry, dict):
            keep.add(entry.get("relativeLocation"))
            location = entry.get("location")
            if isinstance(location, dict) and isinstance(location.get("path"), str):
                keep.add(os.path.basename(location["path"]))
    found, newest = [], {}
    for path in children(folder):
        name = os.path.basename(path)
        match = EXTENSION.match(name)
        if not match or not os.path.isdir(path) or os.path.islink(path):
            continue
        key = (match.group(1).lower(), (match.group(3) or "").lower())
        version = tuple(int(n) for n in match.group(2).split("."))
        found.append((path, name, key))
        if key not in newest or version > newest[key][0]:
            newest[key] = (version, name)
    return [path for path, name, key in found
            if name not in keep and (name in obsolete or name != newest[key][1])]


def load_json(path, default):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def go_rows():
    """Go's module downloads. They are read-only on disk, so Go itself removes them."""
    go = next((g for g in ("/opt/homebrew/bin/go", "/usr/local/go/bin/go", "/usr/local/bin/go")
               if os.access(g, os.X_OK)), None)
    if not go:
        return []
    try:
        found = run([go, "env", "GOMODCACHE", "GOCACHE"], timeout=20).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for path, name, flag in zip(found, ("Go module downloads", "Go build cache"), ("-modcache", "-cache")):
        # The build cache usually sits in ~/Library/Caches, which App caches already lists.
        if path.startswith(HOME + os.sep) and os.path.isdir(path) and not path.startswith(home("Library", "Caches")):
            rows.append(item("dev", name, [path], sum(du([path]).values()), tilde(path), cmd=[go, "clean", flag]))
    return rows


def old_claude_versions():
    """Claude Code keeps every version it updates to. Keep the current one and any a session runs."""
    folder = home(".local", "share", "claude", "versions")
    current = os.path.realpath(home(".local", "bin", "claude"))
    running = {os.path.realpath(p) for p in program_paths()}
    return [p for p in children(folder) if os.path.realpath(p) != current and os.path.realpath(p) not in running]


def simulator_stamp(folder):
    """Changes whenever a simulator is added, removed, booted or shut down, or a runtime comes or goes."""
    stamp = []
    for path in [folder, "/Library/Developer/CoreSimulator/Volumes", "/Library/Developer/CoreSimulator/Profiles/Runtimes"]:
        try:
            stamp.append(os.stat(path).st_mtime)
        except OSError:
            stamp.append(0)
    for device in children(folder):
        try:
            stamp.append(os.stat(os.path.join(device, "device.plist")).st_mtime)
        except OSError:
            pass
    return stamp


def simulator_devices(simctl, folder):
    """`simctl list` can take minutes, so its answer is kept until the simulators change."""
    stamp = simulator_stamp(folder)
    saved = load_state("simulators.json", {})
    if isinstance(saved, dict) and saved.get("stamp") == stamp and isinstance(saved.get("devices"), dict):
        return saved["devices"]
    listed = run([simctl, "list", "devices", "-j"], timeout=600)
    devices = json.loads(listed.stdout)["devices"]
    save_state("simulators.json", {"stamp": stamp, "devices": devices})
    return devices


def scan_simulators():
    # Straight to simctl: the xcrun shim in /usr/bin can stall for minutes on a full disk.
    simctl = os.path.join(developer_dir(), "usr", "bin", "simctl")
    folder = home("Library", "Developer", "CoreSimulator", "Devices")
    if not os.path.isdir(folder) or not os.access(simctl, os.X_OK):
        return []
    detail = "For iOS versions that are no longer installed. The simulators you can still open are kept."
    try:
        devices = simulator_devices(simctl, folder)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return [item("dev", "Simulators that no longer open", [], 0, detail,
                     blocked="Xcode's simulator service did not answer. Scan again in a minute.")]
    dead = [d["udid"] for group in devices.values() for d in group if not d.get("isAvailable")]
    live = [(runtime, d) for runtime, group in devices.items() for d in group if d.get("isAvailable")]
    # A booted simulator runs launchd_sim with its own folder on the command line.
    running = processes()
    booted = {d["udid"] for _, d in live if d["udid"] in running or d.get("state") == "Booted"}
    caches = {d["udid"]: children(os.path.join(folder, d["udid"], "data", "Library", "Caches"))
              for _, d in live if d["udid"] not in booted}
    sizes = du([os.path.join(folder, udid) for udid in dead] + [os.path.join(folder, d["udid"]) for _, d in live]
               + [p for paths in caches.values() for p in paths])
    rows = []
    inside = [p for paths in caches.values() for p in paths]
    if inside:
        rows.append(item("dev", "Caches inside simulators", inside, sum(sizes.get(p, 0) for p in inside),
                         f"From {count(len([u for u in caches if caches[u]]), 'shut-down simulator', 'shut-down simulators')}. "
                         "Their apps and data stay.",
                         quit=("Shut the simulators down first", [re.escape(u) for u in caches if caches[u]])))
    if dead:
        paths = [os.path.join(folder, udid) for udid in dead]
        name = count(len(dead), "simulator that no longer opens", "simulators that no longer open")
        rows.append(item("dev", name, paths, sum(sizes.get(p, 0) for p in paths), detail,
                         cmd=[simctl, "delete", "unavailable"]))
    for runtime, device in live:
        path = os.path.join(folder, device["udid"])
        system = runtime.rsplit(".", 1)[-1].replace("-", " ", 1).replace("-", ".")   # iOS-27-0 -> iOS 27.0
        rows.append(item(
            "big", f"{device['name']} simulator ({system})", [path], sizes.get(path, 0),
            "The apps and data inside it go. Xcode can make a fresh one.",
            cmd=[simctl, "delete", device["udid"]],
            quit=("Shut this simulator down first", [re.escape(device["udid"])]),
        ))
    return rows


def size_kb(text):
    """'4.97GB (32%)' -> kilobytes. Docker counts in powers of ten."""
    found = re.search(r"([\d.]+)\s*([kMGT]?)B", text)
    if not found:
        return 0
    return int(float(found.group(1)) * 1000 ** " kMGT".index(found.group(2) or " ") / 1024)


def scan_docker():
    docker = next((d for d in DOCKERS if os.access(d, os.X_OK)), None)
    if not docker:
        return []
    builds = item("dev", "Docker build cache", [], 0,
                  "Layers kept from earlier image builds. Images, containers and volumes stay.",
                  cmd=[docker, "builder", "prune", "--all", "--force"])
    images = item("big", "Docker images no container uses", [], 0,
                  "Pulled or built again the next time something needs them. Volumes stay.",
                  cmd=[docker, "image", "prune", "--all", "--force"])
    try:
        usage = run([docker, "system", "df", "--format", "{{.Type}}\t{{.Reclaimable}}"], timeout=20)
        lines = usage.stdout.splitlines() if usage.returncode == 0 else None
    except subprocess.TimeoutExpired:
        lines = None
    if lines is None:
        builds["blocked"] = images["blocked"] = "Start Docker to measure this"
        return [builds, images]
    for line in lines:
        kind, _, reclaimable = line.partition("\t")
        if kind == "Build Cache":
            builds["kb"] = size_kb(reclaimable)
        elif kind == "Images":
            images["kb"] = size_kb(reclaimable)
    return [builds, images]


def ollama_rows():
    ollama = next((p for p in ("/usr/local/bin/ollama", "/opt/homebrew/bin/ollama",
                               "/Applications/Ollama.app/Contents/Resources/ollama") if os.access(p, os.X_OK)), None)
    if not ollama or not os.path.isdir(home(".ollama", "models")):
        return []
    try:
        listed = run([ollama, "list"], timeout=20)
    except subprocess.TimeoutExpired:
        listed = None
    if not listed or listed.returncode:
        return [item("big", "Ollama models", [], 0, "~/.ollama/models", blocked="Open Ollama to see its models")]
    rows = []
    for line in listed.stdout.splitlines()[1:]:
        found = re.match(r"^(\S+)\s+[0-9a-f]{12}\s+([\d.]+\s*[kKMGT]?B)\s+(.*)$", line.strip())
        if found:
            name, size, used = found.groups()
            rows.append(item("big", f"Ollama model {name}", [], size_kb(size.replace("K", "k")),
                             f"Last changed {used.strip()}. `ollama pull {name}` brings it back.",
                             cmd=[ollama, "rm", name]))
    return rows


def scan_big():
    """Not caches, but downloads and copies that can be fetched or made again."""
    sdk = home("Library", "Android", "sdk")
    emulator = ("Close the Android emulator first", [r"qemu-system"])
    rows = []
    for avd in children(home(".android", "avd")):
        if avd.endswith(".avd"):
            name = os.path.basename(avd)[:-len(".avd")]
            paths = [avd] + [p for p in [avd[:-len(".avd")] + ".ini"] if os.path.exists(p)]
            rows.append(("Android emulator " + name.replace("_", " "), paths,
                         "Its apps and data go. Android Studio's Device Manager can make it again.",
                         ("Close this emulator first", [re.escape(f"-avd {name}") + r"\b"])))
    for image in [p for api in children(os.path.join(sdk, "system-images"))
                  for tag in children(api) for p in children(tag)]:
        api, tag = image.split(os.sep)[-3:-1]
        rows.append((f"Android system image {api} ({tag}, {os.path.basename(image)})", [image],
                     "Emulators need it to start. Android Studio's SDK Manager downloads it again.", emulator))
    for ndk in children(os.path.join(sdk, "ndk")):
        rows.append((f"Android NDK {os.path.basename(ndk)}", [ndk],
                     "Gradle downloads the version a project asks for when it builds.", None))
    for root in (home(".cache", "lm-studio", "models"), home(".lmstudio", "models")):
        for model in [m for publisher in children(root) for m in children(publisher)]:
            rows.append((f"LM Studio model {os.path.basename(model)}", [model],
                         "LM Studio can download it again.", None))
    for archive in [a for day in children(home("Library", "Developer", "Xcode", "Archives"))
                    for a in children(day) if a.endswith(".xcarchive")]:
        rows.append((f"Xcode archive {os.path.basename(archive)[:-len('.xcarchive')]}", [archive],
                     "Only needed to read crash reports from that build.", None))
    sizes = du([p for _, paths, _, _ in rows for p in paths])
    found = [item("big", name, paths, sum(sizes.get(p, 0) for p in paths), f"{note} {tilde(paths[0])}", quit=quit)
             for name, paths, note, quit in rows]
    return found + ollama_rows()


# --- project build folders ----------------------------------------------------------------------

def find_build_folders():
    found = []
    for folder, dirs, files in os.walk(HOME):
        depth = folder[len(HOME):].count(os.sep)
        if depth == 0:
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_HOME]
            continue
        if os.path.exists(os.path.join(folder, "bin", "flutter")) or "conda-meta" in dirs:
            dirs[:] = []    # a Flutter SDK or a conda install is a tool, and needs these folders
            continue
        deeper = []
        for name in dirs:
            if os.path.islink(os.path.join(folder, name)):
                continue
            if PROJECT_DIRS.get(name) in files:
                found.append(os.path.join(folder, name))
            elif (name not in PROJECT_DIRS and name not in SKIP_ANYWHERE
                  and not name.endswith(BUNDLE_SUFFIXES) and depth < MAX_DEPTH):
                deeper.append(name)
        dirs[:] = deeper
    return found


def repo_root(folder):
    """The git repository a folder sits in, if any."""
    here = folder
    while len(here) > len(HOME):
        if os.path.lexists(os.path.join(here, ".git")):
            return here
        here = os.path.dirname(here)
    return None


def project_label(folder):
    """A project named by its path from the repository it sits in, so two folders called
    `web` do not read the same."""
    root = repo_root(folder)
    return os.path.relpath(folder, os.path.dirname(root)) if root else os.path.basename(folder)


def git():
    # Never /usr/bin/git: without the developer tools it opens an "install them?" dialog.
    found = ["/opt/homebrew/bin/git", "/usr/local/bin/git", os.path.join(developer_dir(), "usr", "bin", "git")]
    return next((g for g in found if os.access(g, os.X_OK)), None)


def git_tracked(folders):
    """The build folders that hold files git tracks (a committed `dist` is source, not output),
    and the ones git could not check. One `git ls-files` per repository."""
    repos = {}
    for folder in folders:
        root = repo_root(folder)
        if root:
            repos.setdefault(root, []).append(folder)
    tracked, unchecked = set(), set()
    program = git() if repos else None
    for root, inside in repos.items():
        if not program:
            unchecked.update(inside)
            continue
        relative = [os.path.relpath(f, root) for f in inside]
        try:
            done = run([program, "-C", root, "ls-files", "-z", "--"] + relative, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            unchecked.update(inside)
            continue
        if done.returncode:
            unchecked.update(inside)
            continue
        listed = done.stdout.split("\0")
        for folder, rel in zip(inside, relative):
            prefix = rel.replace(os.sep, "/") + "/"
            if any(name.startswith(prefix) for name in listed):
                tracked.add(folder)
    return tracked, unchecked


def last_activity(folder, beside=True, budget=400):
    """When a folder (and, with `beside`, the project it sits in) last changed: the newest change
    among a bounded sample, a few hundred entries three levels down, never a full walk."""
    latest = 0.0
    try:
        for entry in os.scandir(os.path.dirname(folder)) if beside else ():
            if entry.name != ".git" and entry.path != folder:
                latest = max(latest, entry.stat(follow_symlinks=False).st_mtime)
    except OSError:
        pass
    level = [folder]
    for _ in range(3):
        deeper = []
        for path in level:
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        latest = max(latest, entry.stat(follow_symlinks=False).st_mtime)
                        if entry.is_dir(follow_symlinks=False):
                            deeper.append(entry.path)
                        budget -= 1
                        if budget <= 0:
                            return latest
            except OSError:
                pass
        level = deeper
    return latest


def scan_builds():
    shut = []
    for name in ("Desktop", "Documents", "Downloads"):
        try:
            os.listdir(home(name))
        except FileNotFoundError:
            pass
        except OSError:
            shut.append(name)
    # Measure the folders found last time while the walk looks for new ones; the walk decides.
    known = [f for f in load_state("build-folders.json", []) if isinstance(f, str) and os.path.isdir(f)]
    with ThreadPoolExecutor(1) as pool:
        early = pool.submit(du, known)
        folders = find_build_folders()
        sizes = early.result()
    save_state("build-folders.json", folders)
    sizes = {p: kb for p, kb in sizes.items() if p in folders}
    sizes.update(du([f for f in folders if f not in sizes]))
    tracked, unchecked = git_tracked(folders)
    rows = []
    for path, kb in sizes.items():
        if path in tracked or path in unchecked:
            continue
        if kb >= BUILD_MIN_KB:
            when = last_activity(path)
            name = f"{project_label(os.path.dirname(path))} / {os.path.basename(path)}"
            row = item("builds", name, [path], kb, f"{tilde(path)} · last used {ago(when)}")
            if when > time.time() - RECENT:
                row["recent"] = True
                row["note"] = "Used this week, so ticking the whole section leaves it out."
            rows.append(row)
    notes = []
    if shut:
        notes.append(f"macOS did not let headroom look inside {', '.join(shut)}. Allow it in System Settings, "
                     "under Privacy & Security, Files and Folders, then scan again.")
    if tracked:
        notes.append(f"{count(len(tracked), 'build folder holds', 'build folders hold')} files git tracks, "
                     f"so headroom leaves {'it' if len(tracked) == 1 else 'them'} alone.")
    if unchecked:
        notes.append(f"{count(len(unchecked), 'build folder', 'build folders')} in git projects could not be "
                     "checked with git, so headroom leaves them alone.")
    return rows, " ".join(notes)


# --- Trash and browser caches ------------------------------------------------------------------

def scan_trash():
    rows, shut = [], []
    trash = home(".Trash")
    things = listing(trash, shut, "the Trash")
    stuck = [p for p in things if sealed(p)]
    things = [p for p in things if p not in stuck]
    if things or stuck:
        row = item("trash", "Trash", things, sum(du(things).values()),
                   f"{count(len(things), 'item', 'items')} in ~/.Trash. Emptied for good.")
        if stuck:
            names = ", ".join(os.path.basename(p) for p in stuck[:3])
            row["note" if things else "blocked"] = (f"macOS protects {names} (another app's data), "
                                                    "so it stays in the Trash.")
        rows.append(row)
    for name, pattern, folders, profiles in BROWSERS:
        paths = []
        for folder in folders:
            path = home("Library", "Caches", *folder.split("/"))
            if name == "Safari":       # keep Safari's folder, empty what is in it
                paths += listing(path, shut, "Safari's cache")
            elif os.path.isdir(path) and not os.path.islink(path):
                paths.append(path)
        if name == "Safari":
            paths += listing(home("Library", "Containers", "com.apple.Safari", "Data", "Library", "Caches"),
                             shut, "Safari's cache")
        if name == "Firefox":
            for profile in children(home("Library", "Caches", "Firefox", "Profiles")):
                paths += [os.path.join(profile, c) for c in FIREFOX_CACHES if os.path.isdir(os.path.join(profile, c))]
        if profiles:
            root = home("Library", "Application Support", *profiles.split("/"))
            for path in listing(root, shut, f"{name}'s cache"):
                base = os.path.basename(path)
                if base in SHARED_CACHES:
                    paths.append(path)
                elif base == "Default" or base.startswith("Profile ") or base in ("Guest Profile", "System Profile"):
                    paths += [os.path.join(path, c) for c in PROFILE_CACHES if os.path.isdir(os.path.join(path, c))]
        if paths:
            sizes = du(paths)
            where = tilde(os.path.commonpath(paths)) if len(paths) > 1 else tilde(paths[0])
            rows.append(item("trash", f"{name} cache", paths, sum(sizes.values()),
                             f"Pages load from the web again. History, logins and cookies stay. {where}",
                             quit=(f"Quit {name} first", [pattern])))
    notice = full_disk_access(list(dict.fromkeys(shut))) if shut else ""
    return rows, notice


# --- installer files -----------------------------------------------------------------------------

def zipped_installer(path):
    """A zip with an app or an installer inside, read from its table of contents, not unpacked."""
    try:
        with zipfile.ZipFile(path) as archive:
            return any(re.search(r"\.app/|\.(pkg|mpkg|dmg)$", n, re.I) for n in archive.namelist())
    except (OSError, zipfile.BadZipFile, EOFError, ValueError, NotImplementedError):
        return False


def installer_places():
    # Homebrew's downloads are one row under Developer tool caches already.
    return [(home("Downloads"), 2), (home("Desktop"), 1),
            (home("Library", "Containers", "com.apple.mail", "Data", "Library", "Mail Downloads"), 2)]


def find_installers(wanted=None):
    """Installers (or, with `wanted`, other files it picks) in Downloads, on the Desktop and in Mail."""
    wanted = wanted or (lambda path, low: low.endswith(INSTALLERS) or (low.endswith(".zip") and zipped_installer(path)))
    found = []
    for root, depth in installer_places():
        level = [root]
        for _ in range(depth):
            deeper = []
            for path in [p for folder in level for p in children(folder)]:
                low = path.lower()
                if wanted(path, low):
                    found.append(path)
                elif (os.path.isdir(path) and not os.path.islink(path) and not low.endswith(BUNDLE_SUFFIXES)
                      and not os.path.basename(path).startswith(".")):
                    deeper.append(path)
            level = deeper
    return found


def added(path):
    info = os.lstat(path)
    return getattr(info, "st_birthtime", info.st_mtime)


def scan_installers():
    rows = []
    for path, kb in du(find_installers()).items():
        try:
            when = added(path)
        except OSError:
            continue
        row = item("installers", os.path.basename(path), [path], kb,
                   f"{tilde(os.path.dirname(path))} · added {ago(when)}")
        row["safe"] = when < time.time() - 30 * DAY
        if when > time.time() - RECENT:
            row["recent"] = True
            row["note"] = "Downloaded this week, so ticking the whole section leaves it out."
        rows.append(row)
    return rows


# --- qBittorrent ---------------------------------------------------------------------------------

QBIT = ("Quit qBittorrent first", [re.escape("/qBittorrent.app/Contents/MacOS/qbittorrent")])
PIECES = re.compile(r"\.!qB$|^\.[0-9a-f]{40}\.parts$")   # unfinished files, and libtorrent's parts files
PIECE_FOLDER_LIMIT = 5000   # a folder this full with no pieces is a dataset, not a download: not walked on
PIECE_BUDGET = 200_000      # entries looked at in all, so a huge tree can't stall the scan


def bdecode(data, at=0):
    """One bencoded value (the format of .torrent and .fastresume files): (value, where it ends)."""
    kind = data[at:at + 1]
    if kind == b"i":
        end = data.index(b"e", at)
        return int(data[at + 1:end]), end + 1
    if kind in (b"l", b"d"):
        found, at = [], at + 1
        while data[at:at + 1] != b"e":
            value, at = bdecode(data, at)
            found.append(value)
        return (dict(zip(found[::2], found[1::2])) if kind == b"d" else found), at + 1
    colon = data.index(b":", at)
    end = colon + 1 + int(data[at:colon])
    if end > len(data):
        raise ValueError("cut short")
    return data[colon + 1:end], end


def bfile(path):
    try:
        with open(path, "rb") as handle:
            value = bdecode(handle.read())[0]
    except (OSError, ValueError, IndexError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def btext(value):
    return value.decode("utf-8", "surrogateescape") if isinstance(value, bytes) else ""


def qbit_settings():
    """Folders qBittorrent's settings save into: save and temp paths, and the save path history."""
    found = []
    for ini in (home(".config", "qBittorrent", "qBittorrent.ini"),
                home("Library", "Preferences", "qBittorrent", "qBittorrent.ini")):
        try:
            with open(ini, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for line in lines:
            key, equals, value = line.partition("=")
            name = key.strip().rsplit("\\", 1)[-1]
            if equals and name in ("SavePathHistory", "DefaultSavePath", "SavePath", "TempPath", "DownloadPath"):
                found += value.split(", ") if name == "SavePathHistory" else [value]
    return [os.path.expanduser(p.strip().strip('"')) for p in found if p.strip().strip('"')]


def qbit_session():
    """What qBittorrent's current session uses, from BT_backup: (paths whose unfinished files stay,
    info hashes, torrent names, save folders). None when the session can't be read in full, so
    nothing unfinished is offered."""
    backup = home("Library", "Application Support", "qBittorrent", "BT_backup")
    try:
        names = os.listdir(backup)
    except FileNotFoundError:
        return [], set(), set(), []
    except OSError:
        return None
    configured = qbit_settings()
    keep, hashes, titles, folders = [], set(), set(), []
    for name in names:
        if not name.endswith(".fastresume"):
            continue
        digest = name[:-len(".fastresume")].lower()
        resume = bfile(os.path.join(backup, name))
        if resume is None:
            return None
        torrent = bfile(os.path.join(backup, digest + ".torrent")) or {}
        info = torrent.get(b"info") if isinstance(torrent.get(b"info"), dict) else {}
        title = btext(info.get(b"name")) or btext(resume.get(b"name"))
        # Where its files are: its save path and download path, and (for older versions) the
        # temp path from the settings.
        own = [btext(resume.get(k)) for k in (b"save_path", b"qBt-savePath", b"qBt-downloadPath")]
        own = [os.path.expanduser(p) for p in own if p]
        mapped = resume.get(b"mapped_files") if isinstance(resume.get(b"mapped_files"), list) else []
        hashes.add(digest)
        folders += own
        if title:
            titles.add(title)
            keep += [os.path.join(p, title) for p in own + configured]
        if mapped or not title:
            # Files renamed inside the torrent, or no name to go by: its own folders stay whole.
            keep += own + [os.path.join(p, btext(m)) for m in mapped if btext(m) for p in configured]
    return keep, hashes, titles, folders


def kept_piece(path, keep, hashes):
    found = re.match(r"^\.([0-9a-f]{40})\.parts$", os.path.basename(path))
    if found:
        return found.group(1) in hashes
    return any(path in (k, k + ".!qB") or path.startswith(k.rstrip(os.sep) + os.sep) for k in keep)


def find_pieces(roots):
    """Unfinished torrent files under these folders, six levels down at most. A folder with
    thousands of entries and no pieces is a dataset or an archive: it isn't walked any further."""
    found, budget = set(), PIECE_BUDGET
    for root in roots:
        level = [root]
        for _ in range(6):
            deeper = []
            for folder in level:
                inside, seen, hit = [], 0, False
                try:
                    with os.scandir(folder) as entries:
                        for entry in entries:
                            seen, budget = seen + 1, budget - 1
                            if PIECES.search(entry.name) and entry.is_file(follow_symlinks=False):
                                found.add(entry.path)
                                hit = True
                            elif (entry.is_dir(follow_symlinks=False) and entry.name not in SKIP_ANYWHERE
                                  and entry.name not in PROJECT_DIRS and not entry.name.endswith(BUNDLE_SUFFIXES)):
                                inside.append(entry.path)
                            if budget <= 0 or (seen > PIECE_FOLDER_LIMIT and not hit):
                                inside = []
                                break
                except OSError:
                    pass
                deeper += inside
            level = deeper
    return sorted(found)


def scan_qbittorrent():
    """What qBittorrent leaves lying about. Never its session (BT_backup), its settings or a
    finished download: only logs, the location database, old .torrent files and the unfinished
    pieces of torrents that are no longer in the session."""
    support = home("Library", "Application Support", "qBittorrent")
    rows = []

    def add(group, name, paths, detail, tidy=True):
        if paths:
            row = item(group, name, paths, sum(du(paths).values()), detail, quit=QBIT)
            row["tidy"] = tidy
            rows.append(row)

    add("temp", "qBittorrent logs", children(os.path.join(support, "logs")), tilde(os.path.join(support, "logs")))
    geo = os.path.join(support, "GeoDB")
    add("apps", "qBittorrent location database", [geo] if os.path.isdir(geo) and not os.path.islink(geo) else [],
        f"Shows which country each peer is in. qBittorrent downloads it again. {tilde(geo)}")
    session = qbit_session()
    notice = ""
    if session is None:
        notice = ("headroom could not read qBittorrent's list of torrents, so it leaves unfinished "
                  "downloads alone.")
        keep, hashes, titles, folders = [], set(), set(), []
    else:
        keep, hashes, titles, folders = session
        roots = [p for p in dict.fromkeys(qbit_settings() + folders + [home("Downloads")])
                 if p.startswith(HOME + os.sep) and os.path.isdir(p) and not os.path.islink(p)]
        groups = {}
        for path in find_pieces(roots):
            if not kept_piece(path, keep, hashes):
                root = max((r for r in roots if path.startswith(r + os.sep)), key=len)
                first = os.path.relpath(path, root).split(os.sep)[0]
                label = first[:-len(".!qB")] if first.endswith(".!qB") else first
                if PIECES.match(first) and first.endswith(".parts"):
                    label = f"pieces ({first[1:9]})"      # libtorrent names these after the info hash
                groups.setdefault((root, label), []).append(path)
        for (root, label), paths in sorted(groups.items()):
            add("big", f"Unfinished torrent {label}", paths,
                f"{count(len(paths), 'unfinished file', 'unfinished files')} of a torrent that is no longer in "
                f"qBittorrent. Adding it again starts over. {tilde(root)}", tidy=False)
    # .torrent files already added, or lying around for over a month.
    old = time.time() - 30 * DAY
    used = []
    for path in find_installers(lambda path, low: low.endswith(".torrent")):
        info = (bfile(path) or {}).get(b"info")
        title = btext(info.get(b"name")) if isinstance(info, dict) else ""
        try:
            stale = added(path) < old
        except OSError:
            continue
        if (title and title in titles) or stale:
            used.append(path)
    add("installers", count(len(used), ".torrent file", ".torrent files"), used,
        "Already added to qBittorrent, or downloaded over a month ago. What they downloaded stays. "
        + ", ".join(sorted({tilde(os.path.dirname(p)) for p in used})))
    return rows, notice


# --- leftovers of deleted apps -----------------------------------------------------------------

def bundle_info(app):
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as handle:
            info = plistlib.load(handle)
    except Exception:       # a broken or unreadable Info.plist: nothing to learn from it
        return {}
    return info if isinstance(info, dict) else {}


def app_bundles(roots, depth=2):
    """Every .app in these folders and their subfolders, not looking inside apps."""
    found = []
    for root in roots:
        level = [root]
        for _ in range(depth + 1):
            deeper = []
            for path in [p for folder in level for p in children(folder)]:
                if path.endswith(".app"):
                    found.append(path)
                elif os.path.isdir(path) and not os.path.islink(path):
                    deeper.append(path)
            level = deeper
    return found


def installed():
    """Bundle ids and lower-case names of everything installed or running on this Mac."""
    apps = app_bundles([APPLICATIONS, home("Applications"), SYSTEM_APPLICATIONS])
    apps += {p.split(".app/Contents/")[0] + ".app" for p in program_paths() if ".app/Contents/" in p}
    ids, names = set(), set()
    for app in apps:
        info = bundle_info(app)
        for key in ("CFBundleIdentifier", "CFBundleName", "CFBundleDisplayName", "CFBundleExecutable"):
            if isinstance(info.get(key), str):
                (ids if key == "CFBundleIdentifier" else names).add(info[key].lower())
        names.add(os.path.basename(app)[:-len(".app")].lower())
    for folder in TOOL_DIRS + [home(".local", "bin")]:
        names.update(os.path.basename(p).lower() for p in children(folder))
    return ids, names


def owned(ident, vendors, names):
    """True when an installed app could still use this id: same maker, or a word of it names
    an installed app or tool. Leaning this way keeps a live app's settings safe."""
    low = ident.lower()
    if ".".join(low.split(".")[:2]) in vendors:
        return True
    return bool({w for w in re.split(r"[.\-_ ]", low)[2:] if len(w) >= 3} & names)


def app_name(ident):
    """com.rstudio.desktop -> rstudio: the last part of the id that names something."""
    parts = ident.split(".")[1:]
    return next((p for p in reversed(parts) if p.lower() not in GENERIC and not p.isdigit() and len(p) > 1), parts[-1])


def library_traces():
    """Every entry in the places apps leave things that is named like a bundle id."""
    found = []
    for folder, label in TRACES.items():
        for path in children(home("Library", folder)):
            ident = re.sub(r"(\.LSSharedFileList)?\.(plist|savedState|binarycookies)$", "", os.path.basename(path))
            ident = re.sub(r"\.ShipIt$", "", ident)
            if BUNDLE_ID.match(ident) and not ident.lower().startswith("com.apple."):
                found.append((ident, path, label))
    return found


def gone(path):
    """True only when a file is surely missing, not merely hidden from this process."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def launch_agents():
    """Each user launch agent, with what it starts: [(label, plist, program)]."""
    found = []
    for plist in children(home("Library", "LaunchAgents")):
        if not plist.endswith(".plist") or os.path.basename(plist).startswith("com.apple."):
            continue
        try:
            with open(plist, "rb") as handle:
                info = plistlib.load(handle)
        except Exception:
            continue
        if not isinstance(info, dict):
            continue
        program = info.get("Program") or next(iter(info.get("ProgramArguments") or []), None)
        label = info.get("Label") if isinstance(info.get("Label"), str) else os.path.basename(plist)[:-6]
        found.append((label, plist, program if isinstance(program, str) else ""))
    return found


def find_orphans():
    """Apps no longer on this Mac, by name: {key: {name, ids, paths, labels, sealed}}."""
    ids, names = installed()
    vendors = {".".join(i.split(".")[:2]) for i in ids}
    groups = {}

    def add(ident, path, label):
        name = app_name(ident)
        group = groups.setdefault(name.lower(), {"name": name, "ids": set(), "paths": [], "labels": set(),
                                                 "sealed": False})
        group["ids"].add(ident)
        group["labels"].add(label)
        if label == "Container" and sealed(path):
            group["sealed"] = True      # macOS guards it; offering it would only fail
        else:
            group["paths"].append(path)

    for ident, path, label in library_traces():
        if not owned(ident, vendors, names):
            add(ident, path, label)
    # A launch agent whose program is gone starts nothing; one under /Volumes may be on a disk
    # that is simply not plugged in.
    for label, plist, program in launch_agents():
        if program.startswith("/") and not program.startswith("/Volumes/") and gone(program):
            add(label, plist, "Launch agent")
    # Sure enough to be an app's: seen in two kinds of places, or with saved windows, a sandbox
    # container or a dead launch agent. A lone preferences file is often a command-line tool's.
    sure = {"Saved windows", "Container", "Launch agent"}
    return {k: g for k, g in groups.items() if g["paths"] and (len(g["labels"]) >= 2 or g["labels"] & sure)}


_ORPHANS = {"at": 0.0, "found": {}}
_ORPHANS_LOCK = threading.Lock()


def orphans():
    """find_orphans(), shared by the scans that run side by side at the start."""
    with _ORPHANS_LOCK:
        if time.time() - _ORPHANS["at"] > 30:
            _ORPHANS.update(found=find_orphans(), at=time.time())
        return _ORPHANS["found"]


def scan_leftovers():
    groups = list(orphans().values())
    sizes = du([p for g in groups for p in g["paths"]])
    rows, small = [], []
    for group in groups:
        kb = sum(sizes.get(p, 0) for p in group["paths"])
        row = item("leftovers", group["name"], group["paths"], kb,
                   f"{', '.join(sorted(group['labels']))} · {', '.join(sorted(group['ids']))}")
        row["tidy"] = True
        if group["sealed"]:
            row["note"] = "Its sandbox container is protected by macOS, so that part stays."
        (rows if kb >= MIN_KB else small).append(row)
    if len(small) > 1:
        names = sorted((r["name"] for r in small), key=str.lower)
        detail = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
        rows.append(item("leftovers", count(len(small), "app with small leftovers", "apps with small leftovers"),
                         [p for r in small for p in r["paths"]], sum(r["kb"] for r in small), detail))
        rows[-1]["tidy"] = True
    else:
        rows += small
    return rows


# source -> (the sections its rows land in, the scan)
# Quick ones first: the page asks for them in this order, six at a time, so it fills in fast.
SOURCES = {
    "logs": (("temp",), scan_logs),
    "temp": (("temp",), scan_temp),
    "trash": (("trash",), scan_trash),
    "installers": (("installers",), scan_installers),
    "caches": (("apps", "dev"), scan_caches),
    "app_data": (("apps",), scan_app_data),
    "app_extras": (("apps",), scan_app_extras),
    "sandboxed": (("apps",), scan_sandboxed),
    "leftovers": (("leftovers",), scan_leftovers),
    "qbittorrent": (("temp", "apps", "installers", "big"), scan_qbittorrent),
    "home_cache": (("dev",), scan_home_cache),
    "docker": (("dev", "big"), scan_docker),
    "big": (("big",), scan_big),
    "dev_tools": (("dev",), scan_dev_tools),
    "simulators": (("dev", "big"), scan_simulators),
    "builds": (("builds",), scan_builds),
}

FOUND = {}   # source -> {id: row}. The only things a delete will touch.
LOCK = threading.Lock()


def processes():
    return run(["/bin/ps", "-axo", "command="]).stdout


def blocked(row, running):
    if row["blocked"]:
        return row["blocked"]
    if row["quit"] and any(re.search(p, running, re.I | re.M) for p in row["quit"][1]):
        return row["quit"][0]
    return ""


def public(row, running):
    return {"id": row["id"], "group": row["group"], "name": row["name"], "detail": row["detail"],
            "kb": row["kb"], "blocked": blocked(row, running), "note": row["note"],
            "recent": row["recent"], "safe": row["safe"] or row["group"] in SAFE_GROUPS}


# --- rows you asked never to see again, and what has gone ---------------------------------------

STATE_LOCK = threading.Lock()


def state_dir():
    return home("Library", "Application Support", "headroom")


def protected_file():
    return os.path.join(state_dir(), "protected.json")


def load_state(name, default):
    try:
        with open(os.path.join(state_dir(), name)) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def save_state(name, data):
    """Write a small JSON file in headroom's folder, all at once (a crash never leaves half a file)."""
    path = os.path.join(state_dir(), name)
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(path + ".tmp", "w") as handle:
            json.dump(data, handle)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def history_file():
    return home("Library", "Logs", "headroom", "history.jsonl")


def protected():
    entries = load_state("protected.json", [])
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("keys"), list)] if isinstance(entries, list) else []


def save_protected(entries):
    os.makedirs(state_dir(), exist_ok=True)     # unlike other state, failing to save this must show
    with open(protected_file() + ".tmp", "w") as handle:
        json.dump(entries, handle, indent=1)
    os.replace(protected_file() + ".tmp", protected_file())


def row_keys(row):
    """What a protect entry remembers a row by: its paths, or its name when a command does the work."""
    return list(row["paths"]) if row["paths"] and not row["cmd"] else [f"cmd:{row['group']}:{row['name']}"]


def protect(row):
    keys = row_keys(row)
    entry = {"id": hashlib.sha1("\0".join(sorted(keys)).encode()).hexdigest()[:16], "name": row["name"],
             "detail": row["detail"], "keys": keys, "at": now()}
    with STATE_LOCK:
        save_protected([e for e in protected() if e.get("id") != entry["id"]] + [entry])


def unprotect(entry_id):
    with STATE_LOCK:
        entries = protected()
        rest = [e for e in entries if e.get("id") != entry_id]
        if len(rest) == len(entries):
            return False
        save_protected(rest)
        return True


def unprotected(rows):
    """Rows minus whatever is on the protect list. A row that shares only some of its paths
    with the list keeps the others, measured again."""
    keep = {k for e in protected() for k in e["keys"]}
    if not keep:
        return rows
    out = []
    for row in rows:
        keys = row_keys(row)
        if not keep.intersection(keys):
            out.append(row)
        elif row["paths"] and not row["cmd"]:
            rest = [p for p in row["paths"] if p not in keep]
            if rest:
                row["paths"], row["kb"] = rest, sum(du(rest).values())
                out.append(row)
    return out


def remember(what, section, paths, kb, result, error="", command=None):
    """One line in the history: append-only, one JSON object per line."""
    entry = {"at": now(), "what": what, "section": section,
             "paths": [tilde(p) for p in paths], "kb": kb, "result": result}
    if command:
        entry["command"] = " ".join(command)
    if error:
        entry["error"] = error
    with STATE_LOCK:
        try:
            os.makedirs(os.path.dirname(history_file()), exist_ok=True)
            with open(history_file(), "a") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError:
            pass


def history(limit=400):
    try:
        with open(history_file()) as handle:
            lines = collections.deque(handle, maxlen=limit)
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def scan(source):
    found = SOURCES[source][1]()
    rows, notice = found if isinstance(found, tuple) else (found, "")
    rows = [r for r in unprotected(rows) if r["kb"] >= MIN_KB or r["blocked"] or r["tidy"]]
    with LOCK:
        FOUND[source] = {r["id"]: r for r in rows}
    running = processes()
    result = {"items": [public(r, running) for r in rows], "notice": notice}
    with STATE_LOCK:
        last = load_state("last-scan.json", {})
        last[source] = {"at": time.time(), **result}
        save_state("last-scan.json", last)
    return result


def forget_last(ids):
    """Drop rows that are gone from the saved scan, so the next launch doesn't show them first."""
    with STATE_LOCK:
        last = load_state("last-scan.json", {})
        for saved in last.values():
            if isinstance(saved, dict) and isinstance(saved.get("items"), list):
                saved["items"] = [r for r in saved["items"] if not (isinstance(r, dict) and r.get("id") in ids)]
        save_state("last-scan.json", last)


def last_scan():
    """The previous launch's numbers, shown while this launch measures again. Display only:
    a delete acts on rows this launch's scan found."""
    last = load_state("last-scan.json", {})
    return {k: v for k, v in last.items() if k in SOURCES and isinstance(v, dict)} if isinstance(last, dict) else {}


def deletable(path):
    """A second lock behind the scan: only inside the home folder (two levels down at least)
    or the per-user temp folder, and never by way of a symlinked parent."""
    real = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    if real != path:
        return False
    for root, depth in ((HOME, 2), (TMP, 1)):
        if path.startswith(root + os.sep) and len(os.path.relpath(path, root).split(os.sep)) >= depth:
            return True
    return False


def first_line(text):
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def removal_error(stderr, stuck):
    """rm's complaint in plain words. macOS-protected data is the usual reason."""
    if "Operation not permitted" in stderr or "Permission denied" in stderr:
        if stuck:
            return f"macOS protects {', '.join(os.path.basename(p) for p in stuck[:3])}, so it stayed."
        return "macOS protects part of this, so some files stayed."
    return first_line(stderr) or "Some files could not be removed."


def delete(row):
    why = blocked(row, processes())
    if why:
        return {"freed_kb": 0, "left_kb": row["kb"], "errors": [why]}
    errors, said = [], None
    if row["cmd"]:
        try:
            done = run(row["cmd"], timeout=3600)
            if done.returncode:
                errors.append(first_line(done.stderr) or "The clean-up command failed.")
            said = re.search(r"Total[^\n]*?([\d.]+\s*[kMGT]?B)", done.stdout)
        except (OSError, subprocess.TimeoutExpired):
            errors.append("The clean-up command did not finish.")
    else:
        allowed = []
        for path in row["paths"]:
            if deletable(path):
                allowed.append(path)
            else:
                errors.append(f"Left alone, it is outside the folders this tool clears: {tilde(path)}")
        for start in range(0, len(allowed), 200):     # a row can hold hundreds of small files
            batch = allowed[start:start + 200]
            done = run(["/bin/rm", "-rf", "--"] + batch)
            if done.returncode:
                errors.append(removal_error(done.stderr, [p for p in batch if os.path.lexists(p)]))
    if row["paths"]:
        left = sum(du(row["paths"]).values())
    else:
        left = row["kb"] if errors else 0
    freed = size_kb(said.group(1)) if said else max(row["kb"] - left, 0)
    row["kb"] = left
    return {"freed_kb": freed, "left_kb": left, "errors": errors[:3]}


def disk():
    usage = shutil.disk_usage(HOME)
    return {"free_kb": usage.free // 1024, "total_kb": usage.total // 1024}


# The one delete run at a time, as the page's progress bar reads it.
JOB = {"running": False, "stopping": False, "total_kb": 0, "done_kb": 0, "item_kb": 0,
       "index": 0, "count": 0, "current": None, "results": []}


def free_kb():
    return shutil.disk_usage(HOME).free // 1024


def watch_item(size, before, finished):
    """Progress inside one row, read off the disk's free space as it grows."""
    while not finished.wait(0.5):
        JOB["item_kb"] = max(JOB["item_kb"], min(size, free_kb() - before))


def run_job(rows):
    for index, row in enumerate(rows):
        if JOB["stopping"]:
            break
        size = row["kb"]
        JOB.update(index=index, item_kb=0, current={"id": row["id"], "name": row["name"], "kb": size})
        finished = threading.Event()
        threading.Thread(target=watch_item, args=(size, free_kb(), finished), daemon=True).start()
        try:
            result = delete(row)
        except OSError as error:
            result = {"freed_kb": 0, "left_kb": size, "errors": [error.strerror or str(error)]}
        finished.set()
        errors = result["errors"]
        outcome = "failed" if errors and not result["freed_kb"] else "partly" if errors else "deleted"
        remember(row["name"], row["group"], row["paths"], result["freed_kb"], outcome,
                 errors[0] if errors else "", row["cmd"])
        JOB["results"].append({"id": row["id"], "name": row["name"], **result})
        JOB.update(done_kb=JOB["done_kb"] + size, item_kb=0)
    forget_last({r["id"] for r in JOB["results"] if r["left_kb"] < MIN_KB})
    JOB.update(running=False, current=None)


# --- moving things to the Trash ------------------------------------------------------------------

# Finder moves the item and asks for an administrator password itself when the item needs one.
# The path travels as an argument, never inside the script text.
TRASH_SCRIPT = """on run argv
set target to (POSIX file (item 1 of argv)) as alias
tell application "Finder" to delete target
end run"""


def finder_trash(path):
    """Move one file or folder to the Trash through Finder. '' when it went, else why not."""
    try:
        done = run(["/usr/bin/osascript", "-e", TRASH_SCRIPT, path], timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        return "Finder did not answer."
    if done.returncode == 0:
        return ""
    if "-128" in done.stderr:
        return "You cancelled, so it stayed."
    if "-1743" in done.stderr:
        return ("headroom may not control Finder. Allow it in System Settings, Privacy & Security, "
                "Automation, then try again.")
    if "-43" in done.stderr:
        return "It was already gone."
    return first_line(done.stderr) or "Finder could not move it."


def in_applications(path):
    return any(path.startswith(root + os.sep) and path.count(os.sep) - root.count(os.sep) <= 2
               for root in (APPLICATIONS, home("Applications")))


def trashable(path):
    """The lock for moving to the Trash: what deletable() allows, or an app (not Apple's)
    in an Applications folder, never reached through a symlink."""
    if deletable(path):
        return True
    real = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    if real != path or os.path.islink(path) or not path.endswith(".app") or not in_applications(path):
        return False
    ident = bundle_info(path).get("CFBundleIdentifier")
    return not (isinstance(ident, str) and ident.lower().startswith("com.apple."))


# --- the uninstaller -------------------------------------------------------------------------------

APPS = {}   # id -> the app the last listing showed: the only apps an uninstall will touch


def app_traces(ident, names, traces, agents, app):
    """What an installed app keeps in ~/Library: by bundle id, and by its name for the folders
    Electron and cross-platform apps name after themselves."""
    low = ident.lower()
    found = [p for i, p, _ in traces if i.lower() == low or i.lower().startswith(low + ".")]
    for folder in ("Application Support", "Caches", "Logs"):
        for name in names:
            path = home("Library", folder, name)
            if len(name) >= 4 and os.path.lexists(path) and path not in found:
                found.append(path)
    for label, plist, program in agents:
        if label.lower().startswith(low) or program.startswith(app + os.sep):
            found.append(plist)
    return found


def list_apps():
    traces, agents = library_traces(), launch_agents()
    running = {os.path.realpath(p) for p in program_paths()}
    found = []
    for app in app_bundles([APPLICATIONS, home("Applications")], depth=1):
        info = bundle_info(app)
        ident = info.get("CFBundleIdentifier")
        if os.path.islink(app) or not isinstance(ident, str) or ident.lower().startswith("com.apple."):
            continue
        if ident == "local.headroom":
            continue
        name = os.path.basename(app)[:-len(".app")]
        names = {name, info.get("CFBundleName")} | {k for k, v in APP_NAMES.items() if v == name}
        names = {n for n in names if isinstance(n, str)}
        exe = os.path.join(app, "Contents", "MacOS", str(info.get("CFBundleExecutable") or name))
        found.append({"path": app, "name": name, "ident": ident, "exe": exe,
                      "version": str(info.get("CFBundleShortVersionString") or ""),
                      "leftovers": app_traces(ident, names, traces, agents, app)})
    sizes = du([a["path"] for a in found] + [p for a in found for p in a["leftovers"]])
    used = {}
    if found:
        try:
            # One mdls for every app: the answers come back separated by NUL, in order.
            done = run(["/usr/bin/mdls", "-raw", "-name", "kMDItemLastUsedDate"] + [a["path"] for a in found],
                       timeout=60)
            used = dict(zip((a["path"] for a in found), done.stdout.split("\0")))
        except (OSError, subprocess.TimeoutExpired):
            pass
    APPS.clear()
    out = []
    for app in found:
        aid = hashlib.sha1(app["path"].encode()).hexdigest()[:16]
        app["kb"] = sizes.get(app["path"], 0) + sum(sizes.get(p, 0) for p in app["leftovers"])
        APPS[aid] = app
        when = used.get(app["path"], "").strip()
        try:
            last = ago(calendar.timegm(time.strptime(when[:19], "%Y-%m-%d %H:%M:%S")))   # mdls says +0000
        except ValueError:
            last = ""
        out.append({
            "id": aid, "name": app["name"], "version": app["version"], "kb": sizes.get(app["path"], 0),
            "used": last, "password": not os.access(app["path"], os.W_OK),
            "blocked": f"Quit {app['name']} first" if os.path.realpath(app["exe"]) in running else "",
            "leftovers": [{"name": tilde(p), "kb": sizes.get(p, 0)} for p in app["leftovers"]],
        })
    out.sort(key=lambda a: -(a["kb"] + sum(x["kb"] for x in a["leftovers"])))
    return out


def uninstall(aid):
    app = APPS.get(aid)
    if app is None:
        return None
    if os.path.realpath(app["exe"]) in {os.path.realpath(p) for p in program_paths()}:
        return {"moved": 0, "failed": [[app["name"], f"Quit {app['name']} first"]]}
    moved, failed = [], []
    for path in [app["path"]] + app["leftovers"]:
        why = "" if trashable(path) else "Left alone, it is outside the folders headroom moves."
        why = why or finder_trash(path)
        if why:
            failed.append([tilde(path), why])
            if path == app["path"]:
                break       # the app stayed, so its settings stay with it
        else:
            moved.append(path)
    result = "trashed" if moved and not failed else "partly" if moved else "failed"
    remember(app["name"], "uninstall", moved or [app["path"]], app["kb"] if moved else 0, result, failed[0][1] if failed else "")
    if app["path"] in moved:
        APPS.pop(aid, None)
    return {"moved": len(moved), "failed": failed}


# --- the disk explorer ---------------------------------------------------------------------------

PLACES = {}     # id -> {path, kb}: what the explorer showed, the only things Reveal and Trash act on
SIZES = {}      # folder -> (when, listing), so going back up is instant
SIZES_FOR = 10 * 60


def du_children(folder):
    """Each thing a folder holds with its size, the total, and the paths du was not let into."""
    done = run(["/usr/bin/du", "-k", "-d", "1", "--", folder])     # folders; macOS du can't add -a here
    sizes = {}
    for line in done.stdout.splitlines():
        kb, _, path = line.partition("\t")
        if kb.strip().isdigit():
            sizes[path] = int(kb)
    total = sizes.pop(folder, None)
    for path in children(folder):                                   # files, from their own blocks
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            sizes[path] = info.st_blocks * 512 // 1024
    total = sum(sizes.values()) if total is None else total
    denied = re.findall(r"^du: (.+): (?:Permission denied|Operation not permitted)$", done.stderr, re.M)
    return sizes, total, denied


def can_trash(path):
    """Move to Trash is offered for things in the home folder outside ~/Library, and for apps."""
    # ~/Library is apps' own data (Docker's disk image, say): the Clean list handles what is safe there.
    if os.path.relpath(path, HOME).split(os.sep)[0] == "Library":
        return False
    return trashable(path)


def place(path, kb, detail="", partial=False):
    pid = hashlib.sha1(path.encode()).hexdigest()[:16]
    PLACES[pid] = {"path": path, "kb": kb}
    try:
        folder = stat.S_ISDIR(os.lstat(path).st_mode) and not path.endswith(BUNDLE_SUFFIXES)
    except OSError:
        folder = False
    return {"id": pid, "name": os.path.basename(path) or path, "kb": kb, "detail": detail,
            "folder": folder, "partial": partial, "trash": can_trash(path)}


def measured(folder):
    cached = SIZES.get(folder)
    if cached and time.time() - cached[0] < SIZES_FOR:
        return cached[1]
    found = du_children(folder)
    SIZES[folder] = (time.time(), found)
    return found


def place_rows(sizes, denied, limit=60, detail=lambda p: ""):
    order = sorted(sizes.items(), key=lambda kv: -kv[1])
    rows = [place(p, kb, detail(p), any(d == p or d.startswith(p + os.sep) for d in denied))
            for p, kb in order[:limit]]
    rest = order[limit:]
    return rows, ({"count": len(rest), "kb": sum(kb for _, kb in rest)} if rest else None)


def explore_places(pid):
    if not pid:
        with ThreadPoolExecutor(2) as pool:
            mine, apps = pool.map(measured, [HOME, APPLICATIONS])
        sizes = dict(mine[0])
        sizes[APPLICATIONS] = apps[1]
        rows, rest = place_rows(sizes, mine[2] + [APPLICATIONS] * bool(apps[2]), detail=tilde)
        return {"items": rows, "rest": rest, "trail": [], "partial": bool(mine[2] or apps[2])}
    target = PLACES.get(pid)
    if not target:
        return None
    folder = target["path"]
    sizes, _, denied = measured(folder)
    rows, rest = place_rows(sizes, denied)
    trail, here = [], folder
    while here not in (HOME, APPLICATIONS, os.path.dirname(here)):
        trail.insert(0, here)
        here = os.path.dirname(here)
    if here == APPLICATIONS:
        trail.insert(0, here)
    crumbs = [{"id": hashlib.sha1(p.encode()).hexdigest()[:16], "name": os.path.basename(p)} for p in trail]
    for crumb, path in zip(crumbs, trail):
        PLACES.setdefault(crumb["id"], {"path": path, "kb": 0})
    return {"items": rows, "rest": rest, "trail": crumbs, "partial": bool(denied)}


def spotlight_big():
    """Files over 500 MB in the home folder, as Spotlight knows them."""
    done = run(["/usr/bin/mdfind", "-onlyin", HOME, "kMDItemFSSize > 500000000"], timeout=60)
    return [p for p in done.stdout.splitlines() if p.startswith(HOME + os.sep)]


def explore_big():
    try:
        paths = spotlight_big()
    except (OSError, subprocess.TimeoutExpired):
        return {"items": [], "note": "Spotlight did not answer. Try again in a minute."}
    files = []
    for path in paths:
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            files.append((path, info.st_blocks * 512 // 1024, info.st_mtime))
    files.sort(key=lambda f: -f[1])
    rows = [place(p, kb, f"{tilde(os.path.dirname(p))} · changed {ago(when)}") for p, kb, when in files[:60]]
    note = "" if rows else "Spotlight knows no files over 500 MB here. If Spotlight is off for this disk, it can't tell."
    return {"items": rows, "note": note}


def explore_old():
    cutoff = time.time() - 90 * DAY
    old = {}
    for path in children(home("Downloads")):
        try:
            info = os.lstat(path)
        except OSError:
            continue
        touched = max(info.st_mtime, info.st_atime)
        if stat.S_ISDIR(info.st_mode):
            touched = max(touched, last_activity(path, beside=False))
        if touched < cutoff:
            old[path] = touched
    rows, rest = place_rows(du(list(old)), [], detail=lambda p: f"last touched {ago(old[p])}")
    note = "" if rows else "Nothing in Downloads has sat untouched for 90 days."
    return {"items": rows, "rest": rest, "note": note}


def explorer_trash(pid):
    target = PLACES.get(pid)
    if not target:
        return None
    path = target["path"]
    why = "" if can_trash(path) else "headroom only moves things in your home folder, or apps, to the Trash."
    why = why or finder_trash(path)
    remember(os.path.basename(path), "disk", [path], 0 if why else target["kb"], "failed" if why else "trashed", why)
    if not why:
        SIZES.clear()
        PLACES.pop(pid, None)
    return why


# --- maintenance ------------------------------------------------------------------------------------

# task -> (needs an administrator, the work). Admin work goes through macOS's own password dialog
# (osascript "with administrator privileges"); headroom never sees the password.
MAINTENANCE = {
    "quicklook": (False, [["/usr/bin/qlmanage", "-r"], ["/usr/bin/qlmanage", "-r", "cache"]],
                  "Quick Look is fresh. Previews redraw the next time you open them."),
    "dns": (True, "/usr/bin/dscacheutil -flushcache; /usr/bin/killall -HUP mDNSResponder",
            "Done. This Mac looks website addresses up afresh."),
    "icons": (True, "/bin/rm -rf /Library/Caches/com.apple.iconservices.store; "
                    "/usr/bin/find /private/var/folders -name com.apple.dock.iconcache -exec /bin/rm -f {} + 2>/dev/null; "
                    "/usr/bin/find /private/var/folders -name com.apple.iconservices -prune -exec /bin/rm -rf {} + 2>/dev/null; "
                    "/usr/bin/killall Dock",
              "Done. The Dock restarted, and icons redraw over the next minute."),
    "spotlight": (True, "/usr/bin/mdutil -E /",
                  "Spotlight started over. Search fills back in over the next hours."),
}


def maintain(task):
    admin, work, success = MAINTENANCE[task]
    commands = [["/usr/bin/osascript", "-e", f'do shell script "{work}" with administrator privileges']] if admin else work
    for command in commands:
        try:
            done = run(command, timeout=900)
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "text": "It did not finish."}
        if done.returncode:
            if "-128" in done.stderr:
                return {"ok": False, "text": "Cancelled. Nothing changed."}
            return {"ok": False, "text": first_line(done.stderr) or "It did not work."}
    return {"ok": True, "text": success}


# --- the server ---------------------------------------------------------------------------------------

BUSY = 0
DEADLINE = time.time() + IDLE


def watchdog():
    """Stop once the page has gone and nothing is being deleted: nobody is left to press anything."""
    while True:
        time.sleep(2)
        if BUSY == 0 and not JOB["running"] and time.time() > DEADLINE:
            os._exit(0)


def payload(body):
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, kind="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "img-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        # A page on another site that points its own name at this Mac still sends its own Host.
        port = self.server.server_port
        return self.headers.get("Host") in (f"{NAME}:{port}", f"127.0.0.1:{port}", f"[::1]:{port}")

    def signed_in(self):
        """The cookie the first address set. Other sites can't send it: SameSite=Strict."""
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return False
        cookie = jar.get("headroom")
        return cookie is not None and secrets.compare_digest(cookie.value.encode(), TOKEN.encode())

    def typed(self):
        """True for the person opening headroom themselves: a top-level navigation the browser
        says no other site started. Typed or bookmarked is `none`; arriving through the port-80
        door at http://headroom.localhost/ is `same-site`. A link or redirect from a website says
        cross-site, and a browser too old to say anything gets no session either."""
        dest, mode = self.headers.get("Sec-Fetch-Dest"), self.headers.get("Sec-Fetch-Mode")
        navigating = dest == "document" or (dest is None and mode == "navigate")
        return navigating and self.headers.get("Sec-Fetch-Site") in ("none", "same-origin", "same-site")

    def allowed(self):
        """Only this launch's page may use the API: the right Host, the cookie, and a header no
        cross-site form can send. A tab from an earlier launch gets 410 and says headroom closed."""
        sent = self.headers.get("X-Headroom")
        if not (self.local() and self.signed_in() and sent):
            self.reply(403, {"error": "forbidden"})
            return False
        if not secrets.compare_digest(sent.encode(), RUN.encode()):
            self.reply(410, {"error": "This window is from an earlier run of headroom."})
            return False
        return True

    def handle_one_request(self):
        global BUSY
        with LOCK:
            BUSY += 1
        try:
            super().handle_one_request()
        finally:
            with LOCK:
                BUSY -= 1

    def do_GET(self):
        global DEADLINE
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/" and self.local():
            DEADLINE = time.time() + IDLE
            if url.query:
                # The address headroom opened: swap the key for a cookie and show a clean URL.
                headers = {"Location": "/"}
                if secrets.compare_digest(query.get("k", [""])[0].encode(), TOKEN.encode()):
                    headers["Set-Cookie"] = COOKIE
                return self.reply(303, b"", "text/plain", headers)
            headers = {}
            if not self.signed_in() and self.typed():
                headers["Set-Cookie"] = COOKIE
            with open(os.path.join(HERE, "ui.html"), "rb") as page:
                return self.reply(200, page.read().replace(b"__RUN__", RUN.encode()), "text/html; charset=utf-8",
                                  headers)
        if url.path == "/favicon.svg" and self.local():
            return self.reply(200, ICON.encode(), "image/svg+xml")
        if not self.allowed():
            return
        DEADLINE = time.time() + IDLE
        if url.path == "/api/start":
            sources = [{"id": name, "groups": list(groups)} for name, (groups, _) in SOURCES.items()]
            return self.reply(200, {"sources": sources, "disk": disk(), "last": last_scan()})
        if url.path == "/api/progress":
            return self.reply(200, {**JOB, "disk": disk()})
        if url.path == "/api/blocked":
            # Asked when the page comes back into view: an app may have been quit meanwhile.
            running = processes()
            with LOCK:
                rows = [r for found in FOUND.values() for r in found.values()]
            return self.reply(200, {r["id"]: blocked(r, running) for r in rows})
        if url.path == "/api/scan":
            source = query.get("source", [""])[0]
            if source in SOURCES:
                try:
                    return self.reply(200, scan(source))
                except OSError as error:    # a Mac out of processes cannot even start `du`
                    return self.reply(500, {"error": error.strerror or str(error)})
        if url.path == "/api/protected":
            return self.reply(200, {"items": [{"id": e.get("id"), "name": e.get("name"), "detail": e.get("detail")}
                                              for e in protected()]})
        if url.path == "/api/history":
            return self.reply(200, {"items": history(), "file": tilde(history_file())})
        if url.path == "/api/apps":
            return self.reply(200, {"items": list_apps()})
        if url.path == "/api/explore":
            view, pid = query.get("view", ["places"])[0], query.get("id", [""])[0]
            found = explore_big() if view == "big" else explore_old() if view == "old" else explore_places(pid)
            if found is None:
                return self.reply(404, {"error": "That folder is not in the last listing. Go back to the top."})
            return self.reply(200, found)
        if url.path == "/api/ping":
            return self.reply(200, {})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        global DEADLINE
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/bye" and self.local() and self.signed_in():
            # Sent as the tab closes, where a custom header is not possible: the body names the
            # launch instead. A reload sends it too, so wait a moment for the page to come back.
            if secrets.compare_digest(body, RUN.encode()):
                DEADLINE = time.time() + 4
            return self.reply(200, {})
        if not self.allowed():
            return
        DEADLINE = time.time() + IDLE
        data = payload(body)
        wanted = data.get("id") if isinstance(data.get("id"), str) else None
        if self.path == "/api/delete":
            wanted = data.get("ids")
            if not isinstance(wanted, list) or not wanted or not all(isinstance(w, str) for w in wanted):
                return self.reply(400, {"error": "Send the ids of the rows to delete."})
            with LOCK:
                if JOB["running"]:
                    return self.reply(409, {"error": "A delete is already running."})
                known = {i: r for found in FOUND.values() for i, r in found.items()}
                if any(w not in known for w in wanted):
                    return self.reply(404, {"error": "Some rows are not in the last scan. Scan again."})
                rows = [known[w] for w in dict.fromkeys(wanted)]
                JOB.update(running=True, stopping=False, total_kb=sum(r["kb"] for r in rows), done_kb=0,
                           item_kb=0, index=0, count=len(rows), current=None, results=[])
            threading.Thread(target=run_job, args=(rows,), daemon=True).start()
            return self.reply(200, {"started": True})
        if self.path == "/api/stop":
            JOB["stopping"] = True     # finishes the row it is on, then stops
            return self.reply(200, {})
        if not wanted and self.path in ("/api/protect", "/api/unprotect", "/api/uninstall",
                                        "/api/reveal", "/api/trash", "/api/maintain"):
            return self.reply(400, {"error": "Send the id of what to act on."})
        if self.path == "/api/protect":
            with LOCK:
                source = next((s for s, found in FOUND.items() if wanted in found), None)
                row = FOUND[source].pop(wanted) if source else None
            if row is None:
                return self.reply(404, {"error": "That row is not in the last scan. Scan again."})
            protect(row)
            forget_last({wanted})
            return self.reply(200, {})
        if self.path == "/api/unprotect":
            return self.reply(200 if unprotect(wanted) else 404, {})
        if self.path == "/api/uninstall":
            done = uninstall(wanted)
            return self.reply(200, done) if done else self.reply(404, {"error": "List the apps again."})
        if self.path == "/api/reveal":
            target = PLACES.get(wanted)
            if not target:
                return self.reply(404, {"error": "List this folder again."})
            run(["/usr/bin/open", "-R", target["path"]])
            return self.reply(200, {})
        if self.path == "/api/trash":
            why = explorer_trash(wanted)
            if why is None:
                return self.reply(404, {"error": "List this folder again."})
            return self.reply(200, {"error": why})
        if self.path == "/api/maintain":
            if wanted not in MAINTENANCE:
                return self.reply(404, {"error": "No such task."})
            return self.reply(200, maintain(wanted))
        self.reply(404, {"error": "not found"})


class Server(ThreadingHTTPServer):
    # The page asks for every section at once; the default backlog of 5 makes the browser's
    # sixth and later requests fail with a connection reset.
    request_queue_size = 64

    def handle_error(self, request, client_address):
        # A tab closed mid-reply: nothing went wrong on this side, so no traceback in the log.
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


class Server6(Server):
    address_family = socket.AF_INET6


def listen():
    """One port on both loopback addresses: macOS sends headroom.localhost to ::1, and some
    setups to 127.0.0.1. Never the wildcard address, so nothing outside this Mac can connect."""
    for port in PORTS + [0] * 8:
        try:
            v4 = Server(("127.0.0.1", port), Handler)
        except OSError:
            continue
        try:
            return v4, Server6(("::1", v4.server_port), Handler)
        except OSError as error:
            if error.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
                return v4, None         # no IPv6 on this Mac's loopback
            v4.server_close()
    raise OSError("headroom found no free port.")


def inherited():
    """The listening sockets launchd holds on 8460 and hands over through the launcher, named in
    HEADROOM_LISTEN_FDS ("3,4"). Served as they are, never bound again. None when not set."""
    names = os.environ.get("HEADROOM_LISTEN_FDS", "").strip()
    if not names:
        return None
    servers = []
    for fd in names.split(","):
        sock = socket.socket(fileno=int(fd))        # IPv4 or IPv6, whichever launchd made
        sock.setblocking(True)
        server = (Server6 if sock.family == socket.AF_INET6 else Server)(
            sock.getsockname()[:2], Handler, bind_and_activate=False)
        server.socket.close()
        server.socket = sock
        server.server_address = sock.getsockname()
        server.server_name, server.server_port = server.server_address[:2]
        servers.append(server)
    return servers


def main():
    if "--detach" in sys.argv or (FROZEN and "--serve" not in sys.argv):
        # Started by the app in Applications. Hand over to a copy that outlives the launcher,
        # so the app counts as closed and can be opened again while a tab is still up.
        child = [sys.executable, "--serve"] if FROZEN else [sys.executable, os.path.abspath(__file__)]
        child += [arg for arg in sys.argv[1:] if arg == "--no-open"]
        log = os.open(home("Library", "Logs", "headroom.log"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.posix_spawn(child[0], child, os.environ, setsid=True,
                       file_actions=[(os.POSIX_SPAWN_DUP2, log, 1), (os.POSIX_SPAWN_DUP2, log, 2)])
        return
    servers = inherited() or [server for server in listen() if server]
    ipv6 = any(server.address_family == socket.AF_INET6 for server in servers)
    url = f"http://{NAME if ipv6 else '127.0.0.1'}:{servers[0].server_port}/?k={TOKEN}"
    print("headroom is open at", url, flush=True)
    threading.Thread(target=watchdog, daemon=True).start()
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    if "--no-open" not in sys.argv:
        run(["/usr/bin/open", url])
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
