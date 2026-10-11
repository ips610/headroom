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
import http.client
import http.cookies
import json
import os
import plistlib
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def run(command, timeout=None, input=None):
    """Start a program and collect what it prints. `input` is text for it to read.

    close_fds=False lets Python use posix_spawn rather than fork. A Mac with a full disk runs
    out of memory to fork with, which is exactly when this tool gets opened, and spawn still works.
    """
    return subprocess.run(command, capture_output=True, text=True, close_fds=False, timeout=timeout, input=input)


HOME = os.path.realpath(os.path.expanduser("~"))
TMP = os.path.realpath(run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"]).stdout.strip() or "/nonexistent")
# Where ui.html sits: beside this file, or inside the bundle when package.sh froze it into an app.
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
FROZEN = getattr(sys, "frozen", False)

# The page lives at http://headroom.localhost:8460. Anyone on this Mac may load it: it holds no
# secret. What it may do is decided call by call, by a session the browser gets from the address
# headroom itself opens, which carries a login code once (?k=...). "Signing in" below has it all.
# RUN marks this launch, so a tab left over from an earlier one is turned away instead of acting
# on this one; NONCE marks the page's own scripts, the only ones the browser is told to run.
NAME = "headroom.localhost"
PORTS = list(range(8460, 8480))
RUN = secrets.token_hex(8)
NONCE = secrets.token_urlsafe(16)

# install.py and package.sh read the version from here. A newer release on GitHub shows up in the app.
VERSION = "1.5"
RELEASES = "https://github.com/ips610/headroom/releases"

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

INSTALLERS = (".dmg", ".pkg", ".mpkg", ".iso", ".xip", ".ipsw")

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
        batch = paths[start:start + 400]
        asked = set(batch)
        with DU_SLOTS:
            done = run(["/usr/bin/du", "-sk", "--"] + batch)
        for line in done.stdout.splitlines():
            kb, _, path = line.partition("\t")
            # Only what was asked about. A folder name can hold a line break and a made-up
            # "size<TAB>path" after it, and callers delete what comes back from here.
            if kb.strip().isdigit() and path in asked:
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
        "trash": False,      # goes to the Trash through Finder, not straight off (your own data)
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
    return [builds, images] + docker_leftovers(docker)


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
        # A name with a line break in it is nobody's project, and would read as two lines of `du`.
        dirs[:] = [d for d in dirs if "\n" not in d and "\r" not in d]
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
            (home("Library", "Containers", "com.apple.mail", "Data", "Library", "Mail Downloads"), 2),
            (home("Library", "iTunes"), 2)]           # iPhone/iPad Software Updates: .ipsw files


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


# --- iPhone and iPad backups ------------------------------------------------------------------------

def scan_backups():
    """Device backups Finder made. Your data, so they go to the Trash, one row per device."""
    shut, found = [], []
    for path in listing(home("Library", "Application Support", "MobileSync", "Backup"), shut,
                        "your iPhone and iPad backups"):
        if os.path.isdir(path) and not os.path.islink(path):
            try:
                with open(os.path.join(path, "Info.plist"), "rb") as handle:
                    info = plistlib.load(handle)
            except Exception:
                info = {}
            found.append((path, info if isinstance(info, dict) else {}))
    sizes = du([path for path, _ in found])
    rows = []
    for path, info in found:
        device = str(info.get("Device Name") or info.get("Display Name") or "iPhone or iPad")
        when = info.get("Last Backup Date")
        facts = [str(info.get("Product Type") or "")]
        if isinstance(when, datetime.datetime):
            moment = calendar.timegm(when.utctimetuple())      # plists keep dates in UTC
            facts.append(f"backed up {when.day} {when:%b %Y} ({ago(moment)})")
        row = item("big", f"{device} backup", [path], sizes.get(path, 0),
                   " · ".join(f for f in facts if f) + f" · {tilde(path)}",
                   quit=("A backup is running. Wait for it to finish", [r"AppleMobileBackup"]))
        row["trash"] = True
        row["note"] = "Goes to the Trash. It may be your only copy of that device if it isn't in iCloud."
        rows.append(row)
    return rows, full_disk_access(shut) if shut else ""


# --- Homebrew --------------------------------------------------------------------------------------

BREWS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
BREW_BUSY = ("Homebrew is busy. Wait for it to finish", [r"/Homebrew/brew\.rb", r"/bin/brew( |$)"])


def brew_command(brew, *args):
    # No self-update or hints: a dry run should only look.
    return ["/usr/bin/env", "HOMEBREW_NO_AUTO_UPDATE=1", "HOMEBREW_NO_INSTALL_CLEANUP=1", "HOMEBREW_NO_ENV_HINTS=1",
            brew, *args]


def scan_homebrew():
    """Old versions Homebrew keeps, and packages nothing needs any more. Homebrew lives outside
    the home folder, so these rows run Homebrew's own commands; headroom deletes no paths."""
    brew = next((b for b in BREWS if os.access(b, os.X_OK)), None)
    if not brew:
        return []
    prefix = os.path.dirname(os.path.dirname(brew))
    caches = home("Library", "Caches", "Homebrew") + os.sep     # already a row under Dev tools
    rows = []
    try:
        cleanup = run(brew_command(brew, "cleanup", "-n", "--prune=all"), timeout=600)
        autoremove = run(brew_command(brew, "autoremove", "-n"), timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return [item("dev", "Homebrew old versions", [], 0, prefix, blocked="Homebrew did not answer. Scan again later.")]
    kb, found = 0, 0
    for line in cleanup.stdout.splitlines():
        match = re.match(r"^Would remove: (.+?) \((?:[\d,]+ files?, )?([\d.]+\s*[kKMGT]?B)\)$", line.strip())
        if match and not match.group(1).startswith(caches):
            kb, found = kb + size_kb(match.group(2).replace("K", "k")), found + 1
    if found:
        rows.append(item("dev", "Homebrew old versions", [], kb,
                         f"{count(found, 'older version or leftover', 'older versions and leftovers')} Homebrew keeps. "
                         f"The packages you use stay. {prefix}",
                         cmd=brew_command(brew, "cleanup", "--prune=all"), quit=BREW_BUSY))
    names, listing_started = [], False
    for line in autoremove.stdout.splitlines():
        if line.startswith("==> Would autoremove"):
            listing_started = True
        elif listing_started and re.match(r"^[\w@+.\-/]+$", line.strip()):
            names.append(line.strip())
    if names:
        cellar = [os.path.join(prefix, "Cellar", n.rsplit("/", 1)[-1]) for n in names]
        rows.append(item("dev", "Homebrew packages nothing needs", [], sum(du(cellar).values()),
                         "Installed only for packages you've since removed: " + ", ".join(names[:8])
                         + (f" and {len(names) - 8} more" if len(names) > 8 else ""),
                         cmd=brew_command(brew, "autoremove"), quit=BREW_BUSY))
    return rows


def docker_leftovers(docker):
    """Stopped containers and volumes no container uses, one row each. Docker removes them."""
    rows = []
    try:
        stopped = run([docker, "ps", "-a", "--size", "-f", "status=exited",
                       "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Size}}\t{{.Status}}"], timeout=30)
        volumes = run([docker, "system", "df", "-v", "--format", "{{json .Volumes}}"], timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return rows
    for line in stopped.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 5 and re.match(r"^[0-9a-f]{6,64}$", parts[0]):
            ident, name, image, size, status = parts
            row = item("big", f"Stopped container {name}", [], size_kb(size.split(" (")[0]),
                       f"{image} · {status.lower()}. Its own changes go; the image stays.", cmd=[docker, "rm", ident])
            row["tidy"] = True
            rows.append(row)
    try:
        found = json.loads(volumes.stdout or "[]")
    except ValueError:
        found = []
    for volume in found if isinstance(found, list) else []:
        if isinstance(volume, dict) and str(volume.get("Links")) == "0" and re.match(r"^[\w.\-]+$", str(volume.get("Name"))):
            row = item("big", f"Docker volume {volume['Name']}", [], size_kb(str(volume.get("Size", "0B"))),
                       "No container uses it. A volume can hold a database: removing it deletes that data for good.",
                       cmd=[docker, "volume", "rm", volume["Name"]])
            row["tidy"] = True
            rows.append(row)
    return rows


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
    "backups": (("big",), scan_backups),
    "home_cache": (("dev",), scan_home_cache),
    "docker": (("dev", "big"), scan_docker),
    "big": (("big",), scan_big),
    "dev_tools": (("dev",), scan_dev_tools),
    "simulators": (("dev", "big"), scan_simulators),
    "builds": (("builds",), scan_builds),
    "homebrew": (("dev",), scan_homebrew),      # slow: Homebrew's dry runs take a while
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
            "recent": row["recent"], "safe": row["safe"] or row["group"] in SAFE_GROUPS, "trash": row["trash"]}


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


def only_mine(path, flags):
    """An opener for open(): a new file that only this account can read, from the moment it exists."""
    return os.open(path, flags | os.O_EXCL, 0o600)


def save_state(name, data, private=False):
    """Write a small JSON file in headroom's folder, all at once (a crash never leaves half a file).
    `private` is for a secret: the file is never readable by another account, not even briefly.
    True when it was saved."""
    path = os.path.join(state_dir(), name)
    try:
        os.makedirs(state_dir(), exist_ok=True)
        if private and os.path.lexists(path + ".tmp"):
            os.remove(path + ".tmp")        # a leftover would keep the permissions it was made with
        with open(path + ".tmp", "w", opener=only_mine if private else None) as handle:
            json.dump(data, handle)
        os.replace(path + ".tmp", path)
    except OSError:
        return False
    return True


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
    elif row["trash"]:
        for path in row["paths"]:
            why = "" if trashable(path) else f"Left alone, it is outside the folders this tool clears: {tilde(path)}"
            why = why or finder_trash(path)
            if why:
                errors.append(why)
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
        outcome = "failed" if errors and not result["freed_kb"] else "partly" if errors else (
            "trashed" if row["trash"] else "deleted")
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
            opened = calendar.timegm(time.strptime(when[:19], "%Y-%m-%d %H:%M:%S"))   # mdls says +0000
            last, last_on = ago(opened), time.strftime("%-d %b %Y", time.localtime(opened))
        except ValueError:
            last = last_on = ""
        out.append({
            "id": aid, "name": app["name"], "version": app["version"], "kb": sizes.get(app["path"], 0),
            "used": last, "used_on": last_on, "password": not os.access(app["path"], os.W_OK),
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
    inside = children(folder)
    known = {folder, *inside}
    sizes = {}
    for line in done.stdout.splitlines():
        kb, _, path = line.partition("\t")
        if kb.strip().isdigit() and path in known:                  # as in _du: no made-up lines
            sizes[path] = int(kb)
    total = sizes.pop(folder, None)
    for path in inside:                                             # files, from their own blocks
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


# --- duplicate files -------------------------------------------------------------------------------

DUPE_MIN = 1024 * 1024              # smaller files aren't worth the reading
DUPE_CAP = 8 * 1000 ** 3            # bytes read in full per run; the cache carries the rest to the next
SF_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)   # an iCloud placeholder: reading it downloads it
PACKAGES = BUNDLE_SUFFIXES + (
    ".photoslibrary", ".musiclibrary", ".imovielibrary", ".tvlibrary", ".fcpbundle", ".logicx", ".band",
    ".pages", ".numbers", ".key", ".rtfd", ".bundle", ".plugin", ".kext", ".xcarchive", ".sparsebundle",
    ".pkg", ".mpkg", ".lrdata", ".lrlibrary", ".aplibrary", ".playground", ".scptd", ".app")
DUPES = {"running": False, "phase": "", "done": 0, "total": 0, "files": 0, "capped": False, "at": 0, "groups": {}}


def dataless(info):
    return bool(getattr(info, "st_flags", 0) & SF_DATALESS)


def dupe_files():
    """Files of 1 MB and up in Downloads, Desktop and Documents. Never inside apps, photo libraries
    and other packages, .git or node_modules, never through symlinks, never iCloud placeholders."""
    found = []
    for root in (home("Downloads"), home("Desktop"), home("Documents")):
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_ANYWHERE
                       and d not in PROJECT_DIRS and not d.lower().endswith(PACKAGES)]
            for name in files:
                path = os.path.join(folder, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_size >= DUPE_MIN and not dataless(info) \
                        and not name.startswith("."):
                    found.append((path, info))
    return found


def read_hash(path, size, whole, progress=None):
    """SHA-256 of the first and last 64 KB, or (whole) of everything."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        if not whole:
            digest.update(handle.read(65536))
            if size > 131072:
                handle.seek(-65536, os.SEEK_END)
                digest.update(handle.read(65536))
            return digest.hexdigest()
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            if progress:
                progress(len(chunk))
    return digest.hexdigest()


def find_dupes():
    """Same size, then the same first and last 64 KB, then the same SHA-256. Hashes are kept by
    path, size and change time, so a second run only reads what changed."""
    DUPES.update(running=True, phase="Listing files", done=0, total=0, files=0, capped=False, groups={})
    try:
        files = dupe_files()
        DUPES["files"] = len(files)
        by_size = {}
        for path, info in files:
            by_size.setdefault(info.st_size, []).append((path, info))
        cache, fresh = load_state("hashes.json", {}), {}

        def hashed(path, info, slot, work):
            entry = fresh.get(path) or cache.get(path)
            if not (isinstance(entry, list) and len(entry) == 4 and entry[:2] == [info.st_size, info.st_mtime_ns]):
                entry = [info.st_size, info.st_mtime_ns, None, None]
            if entry[slot] is None:
                entry[slot] = work()
            fresh[path] = entry
            return entry[slot]

        DUPES["phase"] = "Comparing the start and end of each file"
        alike = {}
        for size, same in by_size.items():
            for path, info in same if len(same) > 1 else []:
                try:
                    key = hashed(path, info, 2, lambda: read_hash(path, size, False))
                except OSError:
                    continue
                alike.setdefault((size, key), []).append((path, info))
        candidates = [f for group in alike.values() if len(group) > 1 for f in group]
        DUPES.update(phase="Reading candidates in full", total=sum(
            i.st_size for p, i in candidates if not (isinstance(cache.get(p), list) and cache[p][3]
                                                     and cache[p][:2] == [i.st_size, i.st_mtime_ns])))
        budget = DUPE_CAP
        same = {}
        for path, info in candidates:
            known = cache.get(path)
            if not (isinstance(known, list) and known[:2] == [info.st_size, info.st_mtime_ns] and known[3]):
                if info.st_size > budget:
                    DUPES["capped"] = True      # left for the next run
                    continue
                budget -= info.st_size
            try:
                key = hashed(path, info, 3, lambda: read_hash(path, info.st_size, True,
                                                              lambda n: DUPES.__setitem__("done", DUPES["done"] + n)))
            except OSError:
                continue
            same.setdefault(key, []).append((path, info))
        save_state("hashes.json", fresh)
        groups = {}
        for key, copies in same.items():
            if len(copies) > 1:
                gid = hashlib.sha1(key.encode()).hexdigest()[:16]
                groups[gid] = {"size": copies[0][1].st_size, "files": {
                    hashlib.sha1(p.encode()).hexdigest()[:16]: {"path": p, "size": i.st_size, "mtime": i.st_mtime_ns}
                    for p, i in copies}}
        DUPES["groups"] = groups
    finally:
        DUPES.update(running=False, phase="", at=time.time())


def dupes_public():
    groups = []
    for gid, group in DUPES["groups"].items():
        files = sorted(group["files"].items(), key=lambda kv: -kv[1]["mtime"])
        groups.append({"id": gid, "kb": group["size"] // 1024, "keep": files[0][0], "files": [
            {"id": fid, "name": os.path.basename(f["path"]), "folder": tilde(os.path.dirname(f["path"])),
             "when": ago(f["mtime"] / 1e9)} for fid, f in files]})
    groups.sort(key=lambda g: -g["kb"] * (len(g["files"]) - 1))
    return {k: DUPES[k] for k in ("running", "phase", "done", "total", "files", "capped", "at")} | {"groups": groups[:300]}


def unchanged(copy):
    try:
        info = os.lstat(copy["path"])
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size == copy["size"] and info.st_mtime_ns == copy["mtime"]


def trash_extras(choices):
    """Move every copy but the kept one to the Trash, for each (group id, file id to keep). The kept
    copy must still be there, unchanged, or nothing in that group moves."""
    moved, freed, problems = 0, 0, []
    for gid, keep in choices:
        group = DUPES["groups"].get(gid)
        if not group or keep not in group["files"]:
            problems.append("Some of these are not in the last search. Search again.")
            continue
        if not unchanged(group["files"][keep]):
            problems.append(f"{os.path.basename(group['files'][keep]['path'])} changed since the search, so its copies stay.")
            continue
        for fid, copy in list(group["files"].items()):
            if fid == keep:
                continue
            why = "" if unchanged(copy) else "It changed since the search."
            why = why or ("" if can_trash(copy["path"]) else "It is outside the folders headroom moves.")
            why = why or finder_trash(copy["path"])
            remember(os.path.basename(copy["path"]), "duplicates", [copy["path"]], 0 if why else copy["size"] // 1024,
                     "failed" if why else "trashed", why)
            if why:
                problems.append(f"{os.path.basename(copy['path'])}: {why}")
            else:
                moved, freed = moved + 1, freed + copy["size"] // 1024
                del group["files"][fid]
        if len(group["files"]) < 2:
            DUPES["groups"].pop(gid, None)
    return {"moved": moved, "freed_kb": freed, "problems": problems[:5]}


# --- startup items ------------------------------------------------------------------------------------

STARTUP = {}    # id -> the item the last listing showed: the only ones that can be switched
OWN_AGENTS = {"local.headroom.door": "headroom", "local.headroom.watch": "headroom",
              "local.localhost.door": "headroom and harbor", "local.harbor": "harbor"}
EXPLAIN = (     # label prefix -> what it does, in one line
    ("local.headroom.door", "Lets you open headroom by typing headroom.localhost:8460."),
    ("local.headroom.watch", "Checks free space every half hour, and reminds you about Safe clean."),
    ("local.localhost.door", "Lets plain headroom.localhost and harbor.localhost open their apps."),
    ("local.harbor", "Starts harbor."),
    ("homebrew.mxcl.", "Starts a Homebrew service at login."),
    ("com.google.keystone", "Keeps Google apps such as Chrome up to date."),
    ("com.google.GoogleUpdater", "Keeps Google apps such as Chrome up to date."),
    ("com.microsoft.update", "Keeps Microsoft Office up to date."),
    ("com.microsoft.autoupdate", "Keeps Microsoft Office up to date."),
    ("com.microsoft.OneDriveStandaloneUpdater", "Keeps OneDrive up to date."),
    ("com.adobe.ARMDC", "Keeps Adobe Acrobat up to date."),
    ("us.zoom", "Keeps Zoom up to date."),
    ("com.docker", "Lets Docker run containers and their networking."),
    ("com.grammarly", "Runs and updates Grammarly."),
    ("com.fortinet", "FortiClient's VPN and security agent."),
    ("com.kaspersky", "Kaspersky's antivirus."),
    ("org.gpgtools", "GPG Suite's helpers for encrypted mail."),
    ("com.oracle.java.Java-Updater", "Checks for Java updates."),
    ("org.xquartz", "Starts XQuartz for X11 apps."),
    ("com.mathworks", "MATLAB's background service."),
    ("jp.co.canon", "Canon's scanner helper."),
    ("com.autodesk", "Autodesk's licensing service."),
    ("com.teamviewer", "TeamViewer's remote access."),
    ("com.dropbox", "Keeps Dropbox running and up to date."),
)


def launchctl(*args):
    return run(["/bin/launchctl", *args], timeout=30)


def startup_dirs():
    return ((home("Library", "LaunchAgents"), "user"), ("/Library/LaunchAgents", "system"),
            ("/Library/LaunchDaemons", "system"))


def list_startup(apple=False):
    domain = f"gui/{os.getuid()}"
    loaded = {}
    for line in launchctl("list").stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3:
            loaded[parts[2]] = parts[0] != "-"
    disabled = dict(re.findall(r'^\s*"([^"]+)" => (disabled|enabled)', launchctl("print-disabled", domain).stdout, re.M))
    running = {os.path.realpath(p) for p in program_paths()}
    apps = {ident: os.path.basename(path)[:-len(".app")] for ident, path in installed_apps().items()}
    STARTUP.clear()
    out = []
    for folder, scope in startup_dirs():
        for plist in children(folder):
            if not plist.endswith(".plist"):
                continue
            try:
                with open(plist, "rb") as handle:
                    info = plistlib.load(handle)
            except Exception:
                continue
            if not isinstance(info, dict):
                continue
            label = info.get("Label") if isinstance(info.get("Label"), str) else os.path.basename(plist)[:-6]
            if label.startswith("com.apple.") and not apple:
                continue
            program = info.get("Program") or next(iter(info.get("ProgramArguments") or []), "")
            program = program if isinstance(program, str) else ""
            app = re.search(r"([^/]+)\.app/", program)
            # The app it belongs to: from its program's path, or an installed app whose id starts the
            # label. Never just the maker: Google has many apps, and guessing names the wrong one.
            owner = app.group(1) if app else next((apps[i] for i in sorted(apps, key=len, reverse=True)
                                                   if label.lower() == i or label.lower().startswith(i + ".")), "")
            owner = OWN_AGENTS.get(label, owner) or ("Homebrew" if label.startswith("homebrew.") else "")
            if scope == "user":
                state = "off" if disabled.get(label) == "disabled" else (
                    "running" if loaded.get(label) else "on" if label in loaded else "not loaded")
            else:
                state = "running" if program and os.path.realpath(program) in running else ""
            sid = hashlib.sha1(plist.encode()).hexdigest()[:16]
            STARTUP[sid] = {"label": label, "plist": plist, "scope": scope}
            out.append({"id": sid, "label": label, "program": tilde(program), "app": owner, "scope": scope,
                        "state": state, "own": OWN_AGENTS.get(label, ""),
                        "explain": next((text for prefix, text in EXPLAIN if label.startswith(prefix)),
                                        "Checks for updates." if "updat" in label.lower() else "")})
    out.sort(key=lambda i: (i["scope"] != "user", i["app"].lower() or "~", i["label"].lower()))
    return out


def switch_startup(sid, on):
    """Turn one of your own launch agents off (unload it, and keep it from loading at login) or on
    again. Reversible both ways; the plist stays where it is. Agents for every user stay read-only."""
    entry = STARTUP.get(sid)
    if not entry:
        return "List the startup items again."
    if entry["scope"] != "user":
        return "This one starts for every user on this Mac, so headroom only shows it."
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{entry['label']}"
    if on:
        done = launchctl("enable", target)
        if done.returncode == 0:
            launchctl("bootstrap", domain, entry["plist"])     # already loaded is fine
    else:
        launchctl("bootout", target)                          # not loaded is fine
        done = launchctl("disable", target)
    why = "" if done.returncode == 0 else first_line(done.stderr) or "launchctl refused."
    remember(entry["label"], "startup", [entry["plist"]], 0, ("turned-on" if on else "turned-off") if not why else "failed", why)
    return why


# --- reminders: low space, and a weekly Safe clean --------------------------------------------------

WATCH_LABEL = "local.headroom.watch"


def settings():
    saved = load_state("settings.json", {})
    saved = saved if isinstance(saved, dict) else {}
    gb = saved.get("alert_gb", 20)
    return {"alert": saved.get("alert", True) is not False, "weekly": saved.get("weekly", True) is not False,
            "alert_gb": gb if isinstance(gb, int) and 1 <= gb <= 1000 else 20}


def newest_release():
    """The version on the Releases page. GitHub answers /releases/latest with a redirect to
    /releases/tag/v1.4; reading that needs no API key and isn't held to the API's hourly limit,
    which everyone behind one campus or office address shares."""
    # The frozen app's Python has no certificates of its own; macOS keeps a bundle here.
    cafile = "/etc/ssl/cert.pem" if os.path.exists("/etc/ssl/cert.pem") else None
    host = http.client.HTTPSConnection("github.com", timeout=10, context=ssl.create_default_context(cafile=cafile))
    try:
        host.request("HEAD", urlparse(RELEASES).path + "/latest", headers={"User-Agent": f"headroom/{VERSION}"})
        location = host.getresponse().getheader("Location") or ""
    finally:
        host.close()
    match = re.search(r"/tag/v?(\d+(?:\.\d+)*)$", location)
    if not match:
        raise ValueError("no release")
    return match.group(1)


def update_info(force=False):
    """Is there a newer headroom? Asks GitHub at most once a day, or when the page says to."""
    saved = load_state("update.json", {})
    saved = saved if isinstance(saved, dict) else {}
    error = None
    if force or not isinstance(saved.get("checked"), (int, float)) or time.time() - saved["checked"] > 86400:
        try:
            saved = {"checked": time.time(), "latest": newest_release()}
            save_state("update.json", saved)
        except (OSError, http.client.HTTPException, ValueError):
            error = "GitHub didn't answer. Try again later."
    latest = saved.get("latest")
    if not (isinstance(latest, str) and re.fullmatch(r"\d+(\.\d+)*", latest)):
        latest = None
    newer = latest is not None and tuple(map(int, latest.split("."))) > tuple(map(int, VERSION.split(".")))
    return {"current": VERSION, "latest": latest, "newer": newer, "error": error,
            "url": f"{RELEASES}/tag/v{latest}" if newer else RELEASES}


def launcher_program():
    """The app's own launcher, which can post notifications. None when headroom isn't running
    from an installed app (./run.sh, the tests, a build folder, a copy still in Downloads):
    then reminders are left alone, so a stray copy can't point the agent at itself."""
    installed = ("/Applications/", home("Applications") + os.sep)
    for candidate in (os.path.join(HERE, "..", "MacOS", "headroom-app"),
                      os.path.join(os.path.dirname(sys.executable), "headroom-app")):
        if os.access(candidate, os.X_OK):
            program = os.path.realpath(candidate)
            return program if program.startswith(installed) else None
    return None


def sync_watch():
    """Install, update or remove the agent that runs the reminders, to match the settings."""
    program = launcher_program()
    if not program:
        return
    domain, path = f"gui/{os.getuid()}", home("Library", "LaunchAgents", WATCH_LABEL + ".plist")
    wanted_on = settings()["alert"] or settings()["weekly"]
    wanted = {"Label": WATCH_LABEL, "ProgramArguments": [program, "--watch"], "StartInterval": 1800,
              "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10}
    try:
        with open(path, "rb") as handle:
            current = plistlib.load(handle)
    except Exception:
        current = None
    if not wanted_on:
        if current is not None:
            launchctl("bootout", f"{domain}/{WATCH_LABEL}")
            os.remove(path)
        return
    if current == wanted and launchctl("print", f"{domain}/{WATCH_LABEL}").returncode == 0:
        return
    launchctl("bootout", f"{domain}/{WATCH_LABEL}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        plistlib.dump(wanted, handle)
    launchctl("bootstrap", domain, path)


def size_text(kb):
    """Sizes the way the page shows them: powers of ten, like Finder."""
    bytes_ = max(kb, 0) * 1024
    return f"{bytes_ / 1e9:.1f} GB" if bytes_ >= 1e9 else f"{round(bytes_ / 1e6)} MB" if bytes_ >= 1e6 else f"{round(bytes_ / 1e3)} KB"


def disk_free_kb():
    return shutil.disk_usage(HOME).free // 1024     # one statfs


def add_sample(free_kb, when=None):
    """One free-space reading for the History chart. The file is trimmed when it grows past ~400 KB."""
    path = os.path.join(state_dir(), "space.jsonl")
    with STATE_LOCK:
        try:
            os.makedirs(state_dir(), exist_ok=True)
            with open(path, "a") as handle:
                handle.write(json.dumps({"t": round(when or time.time()), "free": free_kb}) + "\n")
            if os.path.getsize(path) > 400_000:
                with open(path) as handle:
                    keep = handle.readlines()[-6000:]
                with open(path, "w") as handle:
                    handle.writelines(keep)
        except OSError:
            pass


SAFE_SOURCES = ("caches", "app_data", "app_extras", "sandboxed", "logs", "temp", "installers", "qbittorrent")


def measure_safe():
    """What Safe clean would free right now: the safe rows of the quick sources, as the page picks them."""
    running, total = processes(), 0
    for source in SAFE_SOURCES:
        found = SOURCES[source][1]()
        rows = found[0] if isinstance(found, tuple) else found
        total += sum(r["kb"] for r in unprotected(rows) if (r["safe"] or r["group"] in SAFE_GROUPS)
                     and not r["recent"] and r["kb"] >= MIN_KB and not blocked(r, running))
    return total


def watch_check(now=None):
    """The agent's half-hourly check: one free-space reading, and the notifications worth sending.
    Never deletes anything."""
    now = now or time.time()
    chosen, free = settings(), disk_free_kb()
    add_sample(free, now)
    state = load_state("watch.json", {})
    state = state if isinstance(state, dict) else {}
    notes = []
    if chosen["alert"] and free * 1024 < chosen["alert_gb"] * 1e9:
        # At most once in six hours, unless another 5 GB has gone since the last one.
        last_at, last_free = state.get("alert_at", 0), state.get("alert_free")
        if now - last_at >= 6 * 3600 or (isinstance(last_free, int) and free * 1024 <= last_free * 1024 - 5e9):
            notes.append((f"Only {size_text(free)} free", "Open headroom to make room."))
            state.update(alert_at=now, alert_free=free)
    if chosen["weekly"]:
        last = state.get("weekly_at")
        if not isinstance(last, (int, float)):
            state["weekly_at"] = now                     # the first week starts now
        elif now - last >= 7 * DAY and 10 <= time.localtime(now).tm_hour < 20:
            state["weekly_at"] = now
            kb = measure_safe()
            if kb * 1024 >= 500e6:
                notes.append((f"Safe clean would free {size_text(kb)}",
                              "Open headroom to clear it. Nothing goes until you say so."))
    save_state("watch.json", state)
    return notes


def space_history():
    """Free space over time, and the moments headroom freed some (deletes close together count as one)."""
    samples = []
    try:
        with open(os.path.join(state_dir(), "space.jsonl")) as handle:
            for line in collections.deque(handle, maxlen=6000):
                try:
                    entry = json.loads(line)
                    samples.append([int(entry["t"]), int(entry["free"])])
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    marks = []
    for entry in reversed(history(limit=3000)):
        if entry.get("result") not in ("deleted", "trashed", "partly") or not entry.get("kb"):
            continue
        try:
            when = datetime.datetime.fromisoformat(str(entry["at"])).timestamp()
        except (KeyError, ValueError):
            continue
        if marks and when - marks[-1][0] < 15 * 60:
            marks[-1] = [when, marks[-1][1] + int(entry["kb"]), marks[-1][2] + 1]
        else:
            marks.append([when, int(entry["kb"]), 1])
    return {"samples": sorted(samples), "marks": [[round(t), kb, n] for t, kb, n in marks], "total_kb": disk()["total_kb"]}


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


# The tab icons may be kept for a week: with no-store, Safari has nothing to show and draws a letter.
# Everything else stays no-store.
ICON_CACHE = {"Cache-Control": "public, max-age=604800"}


def payload(body):
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# --- signing in ---------------------------------------------------------------------------------------
#
# Anything on this Mac can connect to the page's port, another account included, so asking is
# never enough. A session has two halves, and every call needs both:
#   - a cookie that holds the session token. The browser keeps it for the name headroom.localhost;
#   - the origin key, which the page keeps in its own storage (localStorage) and sends as a header.
# A cookie alone isn't tied to a port: another program listening on a different port of the same
# name gets sent it. The page's storage is tied to the port, so that program never sees the key.
# Both are kept between launches (headroom quits when its tab closes, and typing the address next
# week should still just work) and replaced after 30 days.
# A browser gets them from a login code in the address headroom itself opens: good once, for two
# minutes. A page that isn't signed in asks for such a tab, and says so.

SESSION_FOR = 30 * DAY
CODE_FOR = 120
OPEN_EVERY = 15             # seconds between the tabs opened for pages that aren't signed in
MAX_BODY = 1024 * 1024      # the page posts a few ids; nothing it sends comes near this
SESSION = {}                # {token, key, created} while it could not be saved to session.json
CODES = {}                  # login code -> when it stops working
OPENED = 0.0                # when a tab was last opened for a page that isn't signed in
SAFE_URL = re.compile(r"http://[\w.\[\]:-]+/\?k=[\w-]+")     # the only kind of address headroom opens


def session(now=None):
    """Both halves of the session, {token, key, created}, from session.json, which only this
    account can read. New ones are made when that file is missing, unreadable or 30 days old."""
    now = time.time() if now is None else now

    def good(saved):
        return (isinstance(saved, dict) and all(isinstance(saved.get(k), str) and len(saved[k]) >= 32 for k in ("token", "key"))
                and isinstance(saved.get("created"), (int, float)) and 0 <= now - saved["created"] < SESSION_FOR)

    with STATE_LOCK:
        saved = load_state("session.json", None)
        if good(saved):
            SESSION.clear()
        else:
            # New ones, unless the last ones could not be saved (a full disk, which is when headroom
            # gets opened): those are tried again, so the browser stays signed in while space is freed.
            saved = dict(SESSION) if good(SESSION) else {
                "token": secrets.token_urlsafe(32), "key": secrets.token_urlsafe(32), "created": now}
            SESSION.clear()
            if not save_state("session.json", saved, private=True):
                SESSION.update(saved)
        return saved


def cookie():
    return f"headroom={session()['token']}; Path=/; Max-Age={SESSION_FOR}; HttpOnly; SameSite=Strict"


def login_code():
    """A code for an address headroom opens: swapped for the session once, within two minutes."""
    code = secrets.token_urlsafe(24)
    with LOCK:
        for old in [c for c, until in CODES.items() if until < time.time()]:
            del CODES[old]
        CODES[code] = time.time() + CODE_FOR
    return code


def redeem(sent):
    """True for a login code that is still good. It stops working here, used or expired."""
    with LOCK:
        code = next((c for c in CODES if secrets.compare_digest(c.encode(), sent.encode())), None)
        return code is not None and CODES.pop(code) >= time.time()


def launch(url):
    """Hand an address to LaunchServices from inside this process, which is what `open` does
    with one. macOS's answer: 0 when an app took it, -10814 when no app opens such addresses."""
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ls = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    cf.CFURLCreateWithBytes.restype = ctypes.c_void_p
    cf.CFURLCreateWithBytes.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32, ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    ls.LSOpenCFURLRef.restype = ctypes.c_int32
    ls.LSOpenCFURLRef.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    raw = url.encode()
    made = cf.CFURLCreateWithBytes(None, raw, len(raw), 0x08000100, None)      # 0x08000100: UTF-8
    if not made:
        raise OSError("not an address")
    try:
        return ls.LSOpenCFURLRef(made, None)
    finally:
        cf.CFRelease(made)


def open_page(url):
    """Open headroom's page in the default browser. The address carries a login code, and every
    account on a Mac can read every process's arguments (`ps`), so it never goes on a command
    line: LaunchServices takes it inside this process, or failing that osascript reads it from
    its input. Only an address of headroom's own shape, so nothing in it can end the script's quotes."""
    if not SAFE_URL.fullmatch(url):
        raise ValueError(f"not an address headroom opens: {url!r}")
    try:
        if launch(url) == 0:
            return
    except (OSError, AttributeError):       # the frameworks, or the calls in them, are not there
        pass
    run(["/usr/bin/osascript"], timeout=60, input=f'open location "{url}"')


def invite(host):
    """Open a signed-in tab for a page that isn't signed in. At most one in 15 seconds: another
    account on this Mac can ask too, and must not get to fill the screen."""
    global OPENED
    with LOCK:
        if time.time() - OPENED < OPEN_EVERY:
            return
        OPENED = time.time()
    threading.Thread(target=open_page, args=(f"http://{host}/?k={login_code()}",), daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, kind="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        headers = dict(headers or {})
        self.send_header("Cache-Control", headers.pop("Cache-Control", "no-store"))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")     # no other site may embed a reply
        self.send_header("Content-Security-Policy",
                         f"default-src 'none'; script-src 'nonce-{NONCE}'; style-src 'unsafe-inline'; "
                         "img-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        # A page on another site that points its own name at this Mac still sends its own Host.
        port = self.server.server_port
        return self.headers.get("Host") in (f"{NAME}:{port}", f"127.0.0.1:{port}", f"[::1]:{port}")

    def signed_in(self, key):
        """Both halves of the session: the cookie (other sites can't send it: SameSite=Strict)
        and the origin key the page sends with it. Either one alone is nothing."""
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return False
        sent, mine = jar.get("headroom"), session()
        return (sent is not None and isinstance(key, str)
                and secrets.compare_digest(sent.value.encode(), mine["token"].encode())
                & secrets.compare_digest(key.encode(), mine["key"].encode()))

    def this_launch(self):
        """The run id header: the page of this launch is asking. A tab from an earlier launch gets
        410 and says headroom closed; no header at all gets 403. False when it replied."""
        sent = self.headers.get("X-Headroom")
        if not sent:
            self.reply(403, {"error": "forbidden"})
        elif not secrets.compare_digest(sent.encode(), RUN.encode()):
            self.reply(410, {"error": "This window is from an earlier run of headroom."})
        return bool(sent) and secrets.compare_digest(sent.encode(), RUN.encode())

    def allowed(self):
        """Only this launch's signed-in page may use the API: the right Host, both halves of the
        session, and a header no cross-site form can send."""
        if not (self.local() and self.signed_in(self.headers.get("X-Headroom-Key"))):
            self.reply(403, {"error": "forbidden"})
            return False
        return self.this_launch()

    def own_page(self):
        """For the two calls a page makes before it is signed in: only headroom's own page may
        make them. The browser says when another site's page is asking, and a JSON body with a
        custom header is something no form or link on another site can send. False when it replied."""
        json_sent = self.headers.get("Content-Type", "").split(";")[0].strip().lower() == "application/json"
        if self.headers.get("Sec-Fetch-Site") == "cross-site" or not json_sent:
            self.reply(403, {"error": "forbidden"})
            return False
        return self.this_launch()

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
            # The page, the same for everyone: it holds this launch's run id and script nonce and
            # no secret. Signing in is the page's own business (see "Signing in"), with or without
            # ?k=... on the address. It is told one thing: whether a link on another site led
            # here, because such a visit must not get headroom to open tabs.
            arrived = b"cross-site" if self.headers.get("Sec-Fetch-Site") == "cross-site" else b""
            with open(os.path.join(HERE, "ui.html"), "rb") as page:
                html = (page.read().replace(b"__RUN__", RUN.encode()).replace(b"__NONCE__", NONCE.encode())
                        .replace(b"__ARRIVED__", arrived))
            return self.reply(200, html, "text/html; charset=utf-8")
        # Safari's tab bar skips SVG icons and shows a letter instead, so offer PNGs too.
        pngs = {"/favicon-32.png": "favicon-32.png", "/favicon.ico": "favicon-32.png",
                "/apple-touch-icon.png": "apple-touch-icon.png"}
        if (url.path == "/favicon.svg" or url.path in pngs) and self.local():
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                # Another site's page is asking. It must not learn headroom is here from an icon
                # that loads; the browser asks for the tab icons from headroom's own page.
                return self.reply(404, b"", "text/plain")
            if url.path == "/favicon.svg":
                return self.reply(200, ICON.encode(), "image/svg+xml", ICON_CACHE)
            try:
                with open(os.path.join(HERE, pngs[url.path]), "rb") as icon:
                    return self.reply(200, icon.read(), "image/png", ICON_CACHE)
            except OSError:
                return self.reply(404, b"", "text/plain")
        if not self.allowed():
            return
        DEADLINE = time.time() + IDLE
        if url.path == "/api/start":
            add_sample(disk()["free_kb"])           # one reading per scan, for the History chart
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
        if url.path == "/api/dupes":
            return self.reply(200, dupes_public())
        if url.path == "/api/startup":
            return self.reply(200, {"items": list_startup(query.get("apple", [""])[0] == "1")})
        if url.path == "/api/settings":
            return self.reply(200, {**settings(), "available": launcher_program() is not None})
        if url.path == "/api/space":
            return self.reply(200, space_history())
        if url.path == "/api/update":
            return self.reply(200, update_info(query.get("force", [""])[0] == "1"))
        if url.path == "/api/ping":
            return self.reply(200, {})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        global DEADLINE
        # Nothing is read from a caller the Host check turns away, and never more than a small
        # body: a length below zero would otherwise mean "read until the caller hangs up".
        if not self.local():
            return self.reply(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self.reply(400 if length < 0 else 413, {"error": "headroom reads a short JSON body, with its length."})
        data = payload(self.rfile.read(length))
        if self.path == "/api/bye":
            # Sent as the tab closes, where a custom header is not possible: the body carries the
            # launch and the origin key instead, and the cookie comes along by itself. All three,
            # or nothing happens. A reload sends it too, so wait a moment for the page to come back.
            run_id = data.get("run")
            if (self.signed_in(data.get("key")) and isinstance(run_id, str)
                    and secrets.compare_digest(run_id.encode(), RUN.encode())):
                DEADLINE = time.time() + 4
            return self.reply(200, {})
        if self.path in ("/api/login", "/api/invite"):
            if not self.own_page():
                return
            if self.path == "/api/invite":
                invite(self.headers.get("Host"))
                return self.reply(200, {})
            code = data.get("code")
            if not (isinstance(code, str) and redeem(code)):
                return self.reply(403, {"error": "That login code has been used, or is too old."})
            DEADLINE = time.time() + IDLE
            return self.reply(200, {"key": session()["key"]}, headers={"Set-Cookie": cookie()})
        if not self.allowed():
            return
        DEADLINE = time.time() + IDLE
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
        if self.path == "/api/dupes":
            with LOCK:
                busy, DUPES["running"] = DUPES["running"], True
            if not busy:
                threading.Thread(target=find_dupes, daemon=True).start()
            return self.reply(200, {"started": not busy})
        if self.path == "/api/dupes/trash":
            picks = data.get("groups")
            if not isinstance(picks, list) or not picks or not all(
                    isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("keep"), str) for p in picks):
                return self.reply(400, {"error": "Send the groups and the copy to keep in each."})
            return self.reply(200, trash_extras([(p["id"], p["keep"]) for p in picks]))
        if self.path == "/api/settings":
            current = settings()
            for key in ("alert", "weekly"):
                if isinstance(data.get(key), bool):
                    current[key] = data[key]
            if isinstance(data.get("alert_gb"), int) and 1 <= data["alert_gb"] <= 1000:
                current["alert_gb"] = data["alert_gb"]
            save_state("settings.json", current)
            try:
                sync_watch()
            except OSError as error:
                return self.reply(500, {"error": error.strerror or str(error)})
            return self.reply(200, {**settings(), "available": launcher_program() is not None})
        if not wanted and self.path in ("/api/protect", "/api/unprotect", "/api/uninstall",
                                        "/api/reveal", "/api/trash", "/api/maintain", "/api/startup"):
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
        if self.path == "/api/startup":
            if not isinstance(data.get("on"), bool):
                return self.reply(400, {"error": "Say whether to turn it on or off."})
            return self.reply(200, {"error": switch_startup(wanted, data["on"])})
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
    if "--watch" in sys.argv:
        # Run by the reminder agent through the app's launcher, which posts what this prints.
        os.nice(10)
        for title, body in watch_check():
            print(f"notify\t{title}\t{body}", flush=True)
        return
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
    # Always by name, also when only 127.0.0.1 could be bound: macOS gives the name both loopback
    # addresses, and a cookie for the bare address would go to every local server on any port.
    url = f"http://{NAME}:{servers[0].server_port}/?k={login_code()}"
    print("headroom is open at", url, flush=True)
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=sync_watch, daemon=True).start()     # reminders are on unless turned off
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    if "--no-open" not in sys.argv:
        open_page(url)
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
