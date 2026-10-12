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
import fcntl
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
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urljoin, urlparse


def run(command, timeout=None, input=None, watch=None):
    """Start a program and collect what it prints. `input` is text for it to read.

    close_fds=False lets Python use posix_spawn rather than fork. A Mac with a full disk runs
    out of memory to fork with, which is exactly when this tool gets opened, and spawn still works.

    `watch` is asked many times a second while the program runs. When it answers True, or the
    time is up, the program is stopped there and its return code is -9. What the program did in
    the moment between two askings is done: the caller looks again afterwards.
    """
    if watch is None:
        return subprocess.run(command, capture_output=True, text=True, close_fds=False, timeout=timeout, input=input)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, close_fds=False)
    started = time.monotonic()
    while True:
        try:
            out, err = child.communicate(timeout=0.05)
            return subprocess.CompletedProcess(command, child.returncode, out, err)
        except subprocess.TimeoutExpired:
            if watch() or (timeout and time.monotonic() - started > timeout):
                child.kill()
                out, err = child.communicate()
                return subprocess.CompletedProcess(command, -9, out, err)


HOME = os.path.realpath(os.path.expanduser("~"))
TMP = os.path.realpath(run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"]).stdout.strip() or "/nonexistent")
# Where ui.html sits: beside this file, or inside the bundle when package.sh froze it into an app.
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
FROZEN = getattr(sys, "frozen", False)

# The page lives at http://headroom.localhost, on port 8460 unless the launcher had to keep
# another one for it (see reserved_port). Anyone on this Mac may load it: it holds no
# secret. What it may do is decided call by call, by a session the browser gets from the address
# headroom itself opens, which carries a login code once (?k=...). "Signing in" below has it all.
# RUN marks this launch, so a tab left over from an earlier one is turned away instead of acting
# on this one; NONCE marks the page's own scripts, the only ones the browser is told to run.
NAME = "headroom.localhost"
PORTS = list(range(8460, 8480))
RUN = secrets.token_hex(8)
NONCE = secrets.token_urlsafe(16)

# install.py and package.sh read the version from here. A newer release on GitHub shows up in the app.
VERSION = "1.9"
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
GIT_MIN_KB = 500 * 1024    # a .git folder this big is worth packing with `git gc`
GREW_KB = 100 * 1024       # "What grew": growth under 100 MB is not worth a line
SIZES_DAYS = 30            # how long the sizes of each day's scan are kept for "What grew"
SHOT_AGE = 30 * DAY        # screenshots newer than this: ticking the whole section leaves them out
MODEL_MIN_KB = 100 * 1000 * 1000 // 1024     # loose model files under 100 MB aren't listed
MODEL_RECENT = 30 * DAY    # a model used this recently: ticking the whole section leaves it out
ARCHIVE_MIN_KB = 20 * 1000 * 1000 // 1024    # nor archives under 20 MB
PACKED_MIN_KB = 100 * 1000 * 1000 // 1024    # a plain .gz, .bz2 or .xz is often one data file: only at 100 MB
MANY = 5                   # more archives in one folder, or model files of one name, than this fold into one line
CLOUD_MIN_KB = 10 * 1000 * 1000 // 1024      # local copies of iCloud Drive files under 10 MB aren't listed
CLOUD_RECENT = 30 * DAY    # one opened or changed this recently: ticking the whole section leaves it out
CLOUD_QUIET = 60 * 60      # one changed in the last hour is never offered: iCloud may not have the newest edit yet
MESSAGE_MIN_KB = 20 * 1000 * 1000 // 1024    # nor Messages attachments under 20 MB
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

# ~/.cache is disposable by convention, but LM Studio keeps its model library there. Hugging Face
# and PyTorch keep models there too: scan_models() lists those one by one, and the rest beside them.
NOT_CACHE = ("lm-studio", "huggingface", "torch")
CONDAS = ("anaconda3", "miniconda3", "miniforge3")

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


def ago(when, calendar=False):
    """A timestamp as people say it: today, 3 weeks ago, 2 years ago. With `calendar` it goes by
    the date, so "today" is only ever today's date, not yesterday evening seen in the morning."""
    days = (datetime.date.today() - datetime.date.fromtimestamp(when)).days if calendar else (time.time() - when) / DAY
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


def find_helper(name, variable):
    """One of the small programs that come with the app, beside its launcher. The environment
    variable points at one built elsewhere. None from ./run.sh or an app built without a Swift
    compiler."""
    # Only inside an app bundle (…/Contents/Resources or Frameworks): from a plain folder there is
    # no telling what a MacOS folder next door holds.
    in_app = os.path.basename(os.path.dirname(HERE)) == "Contents"
    beside = [os.path.join(HERE, "..", "MacOS", name)] if in_app else []
    beside += [os.path.join(os.path.dirname(sys.executable), name)] if FROZEN else []
    for candidate in [os.environ.get(variable, "")] + beside:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.realpath(candidate)
    return None


def find_sizer():
    """The app's own folder measurer (launcher/size.swift), which prints what `du -sk` prints many
    times faster. HEADROOM_SIZE points at one built elsewhere. Without it, du measures."""
    return find_helper("headroom-size", "HEADROOM_SIZE")


SIZER = find_sizer()
# launcher/cloud.swift: asks macOS about iCloud Drive files, and removes a file's local copy the
# way macOS means it to be done. Without it the iCloud section offers nothing.
CLOUD = find_helper("headroom-cloud", "HEADROOM_CLOUD")


def measured_lines(output, asked):
    """{path: kb} from "kb<TAB>path" lines, for the paths that were asked about and no others. A
    folder name can hold a line break and a made-up "size<TAB>path" after it, and callers delete
    what comes back from here."""
    sizes = {}
    # Split at line feeds only: splitlines() also breaks at U+2028 and its kin, which a folder
    # name may hold, and the rest of that name would then read as another folder's line.
    for line in output.split("\n"):
        kb, _, path = line.partition("\t")
        if kb.strip().isdigit() and path in asked:
            sizes[path] = int(kb)
    return sizes


PARTLY = set()      # paths du gave a number for without getting through all of them
SKIPPED = re.compile(r"^(?:du|headroom-size): (.+): (.+)$", re.M)
EXPECTED = ("Permission denied", "Operation not permitted", "No such file or directory")


def _du(paths, complaints=None):
    sizes = {}
    for start in range(0, len(paths), 400):
        batch = paths[start:start + 400]
        PARTLY.difference_update(batch)
        with DU_SLOTS:
            if SIZER:
                try:
                    done = run([SIZER, "--"] + batch)
                    sizes.update(measured_lines(done.stdout, set(batch)))
                    if complaints is not None:
                        complaints.append(done.stderr)
                except OSError:
                    pass
            # The measurer prints no line for a path it isn't sure of: du measures those.
            rest = [p for p in batch if p not in sizes]
            if rest:
                done = run(["/usr/bin/du", "-sk", "--"] + rest)
                sizes.update(measured_lines(done.stdout, set(rest)))
                if complaints is not None:
                    complaints.append(done.stderr)
                # du prints a number even when it could not get into part of a folder for another
                # reason than "not allowed" (a name too long, say). That number is too small.
                for inside, why in SKIPPED.findall(done.stderr):
                    if why not in EXPECTED:
                        PARTLY.update(p for p in rest if inside == p or inside.startswith(p + os.sep))
    return sizes


def du(paths, complaints=None):
    """Kilobytes on disk for every path that exists, as {path: kb}. With `complaints`, a list
    that gets what the measuring programs said on stderr (the folders they were not let into)."""
    paths = [p for p in paths if os.path.lexists(p)]
    # The measurer works on several threads by itself; du gets six processes.
    shares = [paths] if SIZER and paths else [s for s in (paths[i::6] for i in range(6)) if s]
    sizes = {}
    with ThreadPoolExecutor(6) as pool:
        for part in pool.map(lambda share: _du(share, complaints), shares):
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
        "git": "",           # a repository `cmd` packs with `git gc`: checked first, measured before and after
        "rank": 0,           # rows with a higher rank come first in their section, before bigger ones
        "weighs": 0,         # what a row without a size of its own is sorted by (a .git folder's size)
        "same": "",          # what "What grew" knows a row by when its name and paths change (a count in the name)
        "seen": {},          # path -> the kind of file the scan saw there; checked again before it moves
        # Rows that belong together (a month's screenshots, one file name in many folders) share
        # one of these, and the page folds them into a line that opens: {key, noun, detail, note,
        # folder, min}. Each is still a row of its own, picked and cleared by its own id.
        "bundle": None,
        "evict": False,      # an iCloud Drive file: only its copy on this Mac goes, by neither rm nor the Trash
        "inside": "",        # what the row is called inside its bundle: its path from the shared folder
        "when": 0,           # when it was added or changed, for sorting
    }


class Unsure(str):
    """A scan's notice when it could not look properly (Spotlight or another program did not
    answer). It reads like any other notice, and may be empty; "What grew" takes that day's sizes
    for the source as unknown instead of as nothing there."""


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
        rows[-1]["same"] = "small:" + folder
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


# --- copies of itself a scripted browser leaves behind ------------------------------------------------

CLONE_NAMES = {"com.google.chrome": "Chrome", "com.google.chrome.for.testing": "Chrome for Testing",
               "org.chromium.chromium": "Chromium", "com.microsoft.edgemac": "Edge", "com.brave.browser": "Brave"}


def clone_root():
    """The X folder beside the per-user temp folder, where a Chromium browser keeps the copy of
    itself it runs from: X/<bundle id>.code_sign_clone/code_sign_clone.XXXXXX."""
    return os.path.join(os.path.dirname(TMP), "X")


def is_clone(path):
    """True for exactly X/<bundle id>.code_sign_clone/code_sign_clone.<something>, and nothing else in X."""
    parent = os.path.dirname(path)
    return (os.path.dirname(parent) == clone_root() and parent.endswith(".code_sign_clone")
            and os.path.basename(path).startswith("code_sign_clone."))


def clones_in_use(paths):
    """The browser copies among these paths that a running program was started from: a running
    browser runs from its copy. Real paths on both sides, because /var is a link to /private/var
    and a program can be listed under either spelling."""
    clones = [path for path in paths if is_clone(path)]
    if not clones:
        return set()
    running, files = [], set()
    for program in program_paths():
        running.append(os.path.realpath(program))
        try:
            info = os.stat(program)
        except OSError:
            continue
        files.add((info.st_dev, info.st_ino))
    busy = set()
    for path in clones:
        real = os.path.realpath(path)
        # The copy's program is a hard link to the app's own, and the kernel names a running
        # program by either of them. So the file itself decides, not only where it is.
        programs = [p for app in children(path) for p in children(os.path.join(app, "Contents", "MacOS"))]
        for program in programs:
            try:
                info = os.stat(program)
            except OSError:
                continue
            if (info.st_dev, info.st_ino) in files:
                busy.add(path)
        if any(r.startswith(real + os.sep) for r in running):
            busy.add(path)
    return busy


def scan_browser_copies():
    """A Chromium browser started by a script (Selenium, Puppeteer, Playwright) copies itself, about
    1 GB by `du`, and leaves the copy behind when the script kills it. One row a browser, holding
    only the copies no running program was started from. A browser with none to spare gets no row."""
    rows = []
    for folder in children(clone_root()):
        if not folder.endswith(".code_sign_clone") or os.path.islink(folder):
            continue
        copies = [p for p in children(folder) if is_clone(p) and os.path.isdir(p) and not os.path.islink(p)]
        busy = clones_in_use(copies)
        unused = [p for p in copies if p not in busy]
        ident = os.path.basename(folder)[:-len(".code_sign_clone")]
        if unused:
            # They are APFS clones: `du` counts each in full, and how much comes back isn't known before they go.
            row = item("temp", f"Browser copies left by automated {CLONE_NAMES.get(ident.lower(), ident)}",
                       unused, sum(du(unused).values()),
                       "Selenium, Puppeteer and Playwright leave a copy of the browser behind on every run. "
                       f"{count(len(unused), 'unused copy', 'unused copies')}: frees up to the size shown.")
            row["note"] = f"{len(busy)} in use, left alone." if busy else ""
            row["same"] = "copies:" + ident
            rows.append(row)
    return rows


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
        ("Simulator caches", [home("Library", "Developer", "CoreSimulator", "Caches")], None),
    ]
    for conda in CONDAS:
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
    # Rows that say what they are: name, paths, quit, then what it is and what you lose, and why
    # ticking the whole section leaves it out ('' when it doesn't).
    table.append(("Xcode previews", children(os.path.join(xcode, "UserData", "Previews")), xcode_open,
                  "What SwiftUI previews built, and the simulators that draw them. Xcode makes them again "
                  "when you open a preview.", ""))
    table += device_support(xcode)
    sizes = du([p for _, paths, *_ in table for p in paths])
    rows = []
    for name, paths, quit, *told in table:
        present = [p for p in paths if p in sizes]
        if present:
            what, left_out = told or ("", "")
            row = item("dev", name, present, sum(sizes[p] for p in present),
                       f"{what} {tilde(os.path.commonpath(present))}".strip(), quit=quit)
            if left_out:
                row["recent"], row["note"] = True, left_out
            rows.append(row)
    return rows + go_rows()


def support_version(path):
    """'iPhone15,2 17.4.1 (21E236)' -> (17, 4, 1). A folder with no version in its name counts as the oldest."""
    found = re.search(r"(?:^| )(\d+(?:\.\d+)*)(?: |$)", os.path.basename(path))
    return tuple(int(n) for n in found.group(1).split(".")) if found else ()


def device_support(xcode):
    """Symbols Xcode copies off every iPhone, iPad, Watch, Apple TV or Vision Pro it meets: a folder
    for each system version, a few GB apiece. For each system, the versions before the newest two
    are one row. The newest two, the ones your devices are most likely on, are a row of their own
    that ticking the whole section leaves out."""
    rows = []
    for system in ("iOS", "macOS", "watchOS", "tvOS", "visionOS"):
        folders = [p for p in children(os.path.join(xcode, f"{system} DeviceSupport"))
                   if os.path.isdir(p) and not os.path.islink(p)]
        newest = sorted({support_version(p) for p in folders}, reverse=True)[:2]
        old = [p for p in folders if support_version(p) not in newest]
        new = [p for p in folders if support_version(p) in newest]
        if old:
            rows.append((f"Old {system} device support", old, None,
                         f"Symbols Xcode copied from devices on older {system} versions "
                         f"({count(len(old), 'folder', 'folders')}). It copies them again when such a device is "
                         "plugged in.", ""))
        if new:
            rows.append((f"Newest {system} device support", new, None,
                         f"Symbols Xcode copied from devices on the newest {system} versions. It copies them "
                         "again when a device is plugged in, which takes a few minutes.",
                         "The two newest versions, so ticking the whole section leaves it out."))
    return rows


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


def go_caches():
    """(the go program, [its module cache, its build cache]); (None, []) without Go."""
    go = next((g for g in ("/opt/homebrew/bin/go", "/usr/local/go/bin/go", "/usr/local/bin/go")
               if os.access(g, os.X_OK)), None)
    if not go:
        return None, []
    try:
        return go, run([go, "env", "GOMODCACHE", "GOCACHE"], timeout=20).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return go, []


def go_rows():
    """Go's module downloads. They are read-only on disk, so Go itself removes them."""
    go, found = go_caches()
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
                     blocked="Xcode's simulator service did not answer. Scan again in a minute.")], Unsure()
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
        rows[-1]["same"] = "simulators:gone"
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
        return [builds, images], Unsure()
    for line in lines:
        kind, _, reclaimable = line.partition("\t")
        if kind == "Build Cache":
            builds["kb"] = size_kb(reclaimable)
        elif kind == "Images":
            images["kb"] = size_kb(reclaimable)
    return [builds, images] + docker_leftovers(docker)


OLLAMAS = ("/usr/local/bin/ollama", "/opt/homebrew/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama")
LEFT_OUT = "Added or changed in the last 30 days, so ticking the whole section leaves it out."
IMPORTED = "Goes to the Trash, in case you added it yourself."


def ollama_rows():
    """(Ollama's models, one row each and removed by Ollama itself; whether Ollama answered).
    Without the program, its whole store is one row."""
    store = home(".ollama", "models")
    ollama = next((p for p in OLLAMAS if os.access(p, os.X_OK)), None)
    if not os.path.isdir(store) or os.path.islink(store):
        return [], True
    if not ollama:
        paths = children(store)
        row = item("models", "Ollama models", paths, sum(du(paths).values()),
                   f"Every model in Ollama's store. Ollama can pull them again. {tilde(store)}",
                   quit=("Quit Ollama first", [r"(^|/)ollama( |$)"]))
        # Without the program there is no telling a pulled model from one made here: to the Trash.
        row["trash"], row["note"] = True, IMPORTED
        return ([row] if all(deletable(p) for p in paths) else []), True
    try:
        listed = run([ollama, "list"], timeout=20)
    except subprocess.TimeoutExpired:
        listed = None
    if not listed or listed.returncode:
        return [item("models", "Ollama models", [], 0, "~/.ollama/models", blocked="Open Ollama to see its models")], False
    rows = []
    for line in listed.stdout.splitlines()[1:]:
        found = re.match(r"^(\S+)\s+[0-9a-f]{12}\s+([\d.]+\s*[kKMGT]?B)\s+(.*)$", line.strip())
        if found:
            name, size, used = found.groups()
            row = item("models", f"Ollama model {name}", [], size_kb(size.replace("K", "k")),
                       f"Last changed {used.strip()}. Run ollama pull {name} to get it back.",
                       cmd=[ollama, "rm", name])
            if not re.search(r"month|year", used):
                row["recent"], row["note"] = True, LEFT_OUT
            rows.append(row)
    return rows, True


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
    for archive in [a for day in children(home("Library", "Developer", "Xcode", "Archives"))
                    for a in children(day) if a.endswith(".xcarchive")]:
        rows.append((f"Xcode archive {os.path.basename(archive)[:-len('.xcarchive')]}", [archive],
                     "Only needed to read crash reports from that build.", None))
    sizes = du([p for _, paths, _, _ in rows for p in paths])
    return [item("big", name, paths, sum(sizes.get(p, 0) for p in paths), f"{note} {tilde(paths[0])}", quit=quit)
            for name, paths, note, quit in rows]


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


def git_command(program, root, *args):
    """A git command line for a repository headroom did not make and does not trust. git runs
    programs a repository names (a file-system monitor, hooks, an ext:: transport) and can fetch
    what a partial clone lacks: none of that happens, and nothing is asked for on the terminal.
    Every git call goes through here."""
    return ["/usr/bin/env", "GIT_OPTIONAL_LOCKS=0", "GIT_TERMINAL_PROMPT=0", "GIT_NO_LAZY_FETCH=1", program,
            "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never",
            "-C", root, *args]


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
        if not program or git_in_icloud(root):
            unchecked.update(inside)
            continue
        relative = [os.path.relpath(f, root) for f in inside]
        try:
            done = run(git_command(program, root, "ls-files", "-z", "--", *relative), timeout=30)
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


def git_in_icloud(root):
    """True when a file git reads first in this repository is only in iCloud: asking git anything
    would download it."""
    for name in ("config", "HEAD", "index"):
        try:
            if dataless(os.lstat(os.path.join(root, ".git", name))):
                return True
        except OSError:
            pass
    return False


GC_KEEPS = "Commits you can reach stay; git may remove unreachable ones older than its grace period."
GC_SETTINGS = r"^(gc\.recentobjectshook|extensions\.partialclone|remote\..*\.promisor)$"


def git_busy(root):
    """Why `git gc` must not run in a repository right now: '' when nothing in it is half-done and
    nothing about it is out of the ordinary. Asked when the row is listed and again just before
    the command runs."""
    store = os.path.join(root, ".git")
    if os.path.islink(store) or not os.path.isdir(store) or not deletable(store):
        return "Its .git folder isn't a plain folder inside your home folder, so headroom leaves it alone."
    # A repository that keeps its objects somewhere else: packing here could strand the other one.
    if any(os.path.islink(os.path.join(store, name)) for name in ("objects", "refs")) or os.path.lexists(
            os.path.join(store, "commondir")):
        return "Its .git folder shares its contents with another place, so headroom leaves it alone."
    worktrees = [p for p in children(os.path.join(store, "worktrees")) if os.path.isdir(p) and not os.path.islink(p)]
    for folder in [store] + worktrees:      # linked worktrees keep their own state
        try:
            names = set(os.listdir(folder))
        except OSError:
            return "headroom could not look inside its .git folder."
        if names & {"rebase-merge", "rebase-apply"}:
            return "A rebase is half-done here. Finish or abort it first."
        if names & {"MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG"}:
            return "A merge, cherry-pick, revert or bisect is half-done here. Finish it first."
        if "gc.pid" in names or any(name.endswith(".lock") for name in names):
            return "Git is at work in this repository (it holds a lock). Try again when it's done."
    for _, folders, names in os.walk(os.path.join(store, "refs")):
        if any(name.endswith(".lock") for name in folders + names):
            return "Git is at work in this repository (it holds a lock). Try again when it's done."
    if git_in_icloud(root):
        return "Part of its .git folder is only in iCloud, so headroom leaves it alone."
    # Reading the settings runs nothing. A program the repository names for gc would run, and a
    # partial clone could go to the network for what it lacks: neither is for headroom to start.
    program = git()
    try:
        done = run(git_command(program, root, "config", "--get-regexp", GC_SETTINGS), timeout=20) if program else None
    except (OSError, subprocess.TimeoutExpired):
        done = None
    if done is None or done.returncode not in (0, 1):
        return "headroom could not read this repository's settings, so it leaves it alone."
    if "recentobjectshook" in done.stdout.lower():
        return "This repository runs a program of its own during git gc, so headroom leaves it alone."
    if done.stdout.strip():
        return "This is a partial clone, which git gc could try to fill in from the network. headroom leaves it alone."
    return ""


def git_rows(folders):
    """The repositories the build folders sit in whose .git has grown big. `git gc` packs it:
    headroom runs that one command and removes nothing in there itself. What it saves isn't known
    until it has run, so the row carries no size; the result says what .git was and is."""
    program = git()
    stores = {os.path.join(root, ".git"): root for root in {repo_root(f) for f in folders} if root}
    stores = {store: root for store, root in stores.items() if os.path.isdir(store) and not os.path.islink(store)}
    big = {store: kb for store, kb in sorted(du(list(stores)).items()) if kb >= GIT_MIN_KB} if program else {}
    names = collections.Counter(os.path.basename(stores[store]) for store in big)
    rows = []
    for store, kb in big.items():
        root = stores[store]
        label = os.path.basename(root)
        if names[label] > 1:        # two repositories of one name: say whose
            label = os.path.join(os.path.basename(os.path.dirname(root)), label)
        row = item("dev", f"{label} git history", [], 0,
                   f"git gc packs its {size_text(kb)} .git folder into fewer files. {GC_KEEPS} {tilde(store)}",
                   cmd=git_command(program, root, "gc", "--quiet"))
        # No paths to tell two repositories of one name apart by, so the id is made from where it is.
        row["id"] = hashlib.sha1(("git\0" + root).encode()).hexdigest()[:16]
        row["git"], row["tidy"], row["weighs"] = root, True, kb
        rows.append(row)
    return rows


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
    rows += git_rows(folders)       # the same projects, no second walk
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
    """A zip with an app or an installer inside, read from its table of contents, not unpacked.
    Never one that is only in iCloud: opening it would download it."""
    try:
        if dataless(os.lstat(path)):
            return False
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


# --- screenshots and screen recordings ---------------------------------------------------------------

SHOT_TYPES = (".png", ".jpg", ".jpeg", ".heic", ".tiff", ".tif", ".gif", ".bmp", ".pdf")
CLIP_TYPES = (".mov", ".mp4")


def capture_setting(key):
    """One of macOS's screenshot settings (`defaults read com.apple.screencapture <key>`); '' when not set."""
    try:
        done = run(["/usr/bin/defaults", "read", "com.apple.screencapture", key], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def capture_folder():
    """Where macOS saves screenshots: the folder in its settings, or the Desktop when none is set."""
    chosen = capture_setting("location")
    if chosen == "~" or chosen.startswith("~/"):
        chosen = HOME + chosen[1:]
    return os.path.normpath(chosen) if chosen.startswith("/") else home("Desktop")


def flagged_captures(paths):
    """(The files Spotlight says macOS's own screen capture made: kMDItemIsScreenCapture is 1,
    whether it answered for every file asked about)."""
    found, answered = set(), True
    for start in range(0, len(paths), 400):
        batch = paths[start:start + 400]
        try:
            done = run(["/usr/bin/mdls", "-raw", "-name", "kMDItemIsScreenCapture"] + batch, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            answered = False
            continue
        answers = done.stdout.split("\0")       # one for every file, in order
        if len(answers) == len(batch):          # any other count can't be matched to the files
            found.update(path for path, answer in zip(batch, answers) if answer.strip() == "1")
        else:
            answered = False
    return found, answered


def capture_names():
    """The names macOS gives its own captures, without the ending: 'Screenshot 2026-08-14 at
    10.32.11', 'Screen Recording 2026-08-14 at 10.32.11 AM', or the name chosen in its settings in
    place of 'Screenshot'. Always with the date and the time, so a file someone called
    screenshot.png isn't one."""
    names = ["Screenshot", "Screen Shot", "Screen Recording"] + [n for n in [capture_setting("name")] if n]
    return re.compile(r"(?:%s) \d{4}-\d\d-\d\d \S{1,12} \d{1,2}\.\d\d\.\d\d(?:[   ]\S{1,6}){0,3}"
                      % "|".join(re.escape(name) for name in names))


def scan_screenshots():
    """Screenshots and screen recordings macOS made, in the one folder it saves them to and never
    its subfolders. A row for each file; a month's files are one bundle, the newest month first,
    and ticking the whole section leaves out the last 30 days. They are yours, not a cache, so
    every row goes to the Trash."""
    folder, shut = capture_folder(), []
    if os.path.realpath(folder) != folder or not deletable(os.path.join(folder, "x")):
        return [], (f"macOS saves screenshots in {tilde(folder)}, which isn't a plain folder inside your home "
                    "folder, so headroom leaves them alone.")
    files = []
    for path in listing(folder, shut, tilde(folder)):
        if path.lower().endswith(SHOT_TYPES + CLIP_TYPES) and not os.path.basename(path).startswith("."):
            try:
                info = os.lstat(path)
            except OSError:
                continue
            # A file itself: not a folder, not a link to one elsewhere, and not one that is only in
            # iCloud, which moving would download first.
            if stat.S_ISREG(info.st_mode) and not dataless(info):
                files.append((path, info))
    (flagged, answered), named = flagged_captures([path for path, _ in files]), capture_names()
    rows, months = [], {}
    for path, info in files:
        if path in flagged or named.fullmatch(os.path.splitext(os.path.basename(path))[0]):
            made = getattr(info, "st_birthtime", info.st_mtime)
            kind = "Screen recordings" if path.lower().endswith(CLIP_TYPES) else "Screenshots"
            month = time.strftime("%Y-%m", time.localtime(made)) if made < time.time() - SHOT_AGE else ""
            when = time.strftime("%B %Y", time.strptime(month, "%Y-%m")) if month else "the last 30 days"
            row = item("shots", os.path.basename(path), [path], info.st_blocks * 512 // 1024,
                       f"{tilde(folder)} · added {ago(made)}")
            row["trash"], row["recent"], row["when"], row["inside"] = True, not month, int(made), os.path.basename(path)
            row["note"] = "" if month else "From the last 30 days, so ticking the whole section leaves it out."
            row["rank"] = int(month.replace("-", "")) if month else 999999       # by month, the newest first
            row["seen"] = {path: stat.S_IFMT(info.st_mode)}
            row["bundle"] = months.setdefault((kind, month), {
                "key": f"shots:{kind}:{month or 'recent'}", "noun": f"{kind.lower()} from {when}", "detail": tilde(folder),
                "note": "" if month else "From the last 30 days, so ticking the whole section leaves them out.",
                "folder": folder, "min": 1})
            rows.append(row)
    if shut:
        return rows, (f"macOS did not let headroom look inside {shut[0]}. Allow it in System Settings, under "
                      "Privacy & Security, Files and Folders, then scan again.")
    # Spotlight not answering, or knowing nothing about files that are named like screenshots,
    # leaves only the names to go by. macOS names them in the Mac's own language.
    if files and (not answered or (rows and not flagged)):
        return rows, ("Spotlight had nothing to say about these files, so headroom went by their names. "
                      "Screenshots that macOS named in another language than English may be missing.")
    return rows, ""


# --- model weights and archives: Spotlight finds them, and every hit is checked ----------------------

MODEL_TYPES = (".gguf", ".safetensors", ".ckpt", ".pt", ".pth", ".onnx", ".keras", ".tflite", ".mlmodel")
MODEL_MAYBE = (".bin", ".h5")                       # any kind of data: a model only where the folder says so
MODEL_PACKAGES = (".mlpackage", ".mlmodelc")        # Core ML models that are folders
ARCHIVE_TYPES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip", ".tar", ".7z", ".rar")
PACKED_TYPES = (".gz", ".bz2", ".xz")
HF_DOWNLOADS = ("datasets", "modules", "metrics", "assets", "xet")      # in ~/.cache/huggingface, beside hub


def spotlight(endings, min_kb, also=()):
    """What Spotlight knows in the home folder with one of these endings and at least this size,
    plus anything ending in `also` whatever its size (Spotlight has no size for a folder). It
    answers from its index, so the disk isn't walked again. None when it did not answer."""
    query = "kMDItemFSSize >= %d && (%s)" % (min_kb * 1024, " || ".join(f'kMDItemFSName == "*{e}"c' for e in endings))
    query += "".join(f' || kMDItemFSName == "*{e}"c' for e in also)
    try:
        done = run(["/usr/bin/mdfind", "-0", "-onlyin", HOME, query], timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if done.returncode else [path for path in done.stdout.split("\0") if path]


def spoken_for(path):
    """True inside a folder another section offers, or that a tool needs whole: a project's build
    folder (node_modules beside package.json and so on), a conda install, a Flutter SDK, wherever
    they are. The same tests the walk for build folders uses."""
    here = os.path.dirname(path)
    while len(here) > len(HOME):
        proof = PROJECT_DIRS.get(os.path.basename(here))
        if ((proof and os.path.exists(os.path.join(os.path.dirname(here), proof)))
                or os.path.isdir(os.path.join(here, "conda-meta")) or os.path.exists(os.path.join(here, "bin", "flutter"))):
            return True
        here = os.path.dirname(here)
    return False


def loose(hits, taken=()):
    """Spotlight's hits that headroom may offer, as [(path, lstat)]. Spotlight is only a hint: each
    one must really be there, a file or folder itself (never a link) at least two levels inside the
    home folder and not reached through a link, and not kept only in iCloud. Left out: ~/Library,
    hidden folders (.git, .cache, the Trash), node_modules and virtualenvs, anything inside an app
    or another package (the Photos library), a conda or Flutter install, a project's build folder,
    and whatever is `taken`: paths other rows already offer."""
    found = []
    for path in sorted(set(hits)):
        if not path.startswith(HOME + os.sep) or os.path.normpath(path) != path or not deletable(path):
            continue
        parts = path[len(HOME) + 1:].split(os.sep)
        if (parts[0] == "Library" or any(part.startswith(".") for part in parts)
                or any(part in SKIP_ANYWHERE or part == "node_modules"
                       or part.lower().endswith(PACKAGES + MODEL_PACKAGES) for part in parts[:-1])
                or any(path == t or path.startswith(t + os.sep) for t in taken) or spoken_for(path)):
            continue
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) and not dataless(info):
            found.append((path, info))
    return found


def model_stores():
    """Models that tools keep in stores of their own, one model a row where the store keeps them
    apart: [(name, path, how it comes back, whether it may be one you put there yourself)].
    Hugging Face and torch.hub only hold what they downloaded. LM Studio, GPT4All and Jan also take
    models you import, so theirs go to the Trash. A store reached through a link is left out:
    headroom clears nothing by way of a link."""
    again, found = "Downloads again when a tool asks for it.", []
    for folder in children(home(".cache", "huggingface", "hub")):
        if os.path.basename(folder).startswith("models--"):
            label = os.path.basename(folder)[len("models--"):].replace("--", "/")
            found.append((f"Hugging Face model {label}", folder, again, False))
    for path in children(home(".cache", "torch", "hub", "checkpoints")):
        found.append((f"PyTorch checkpoint {os.path.basename(path)}", path, again, False))
    for root in (home(".cache", "lm-studio", "models"), home(".lmstudio", "models")):
        for model in [m for publisher in children(root) for m in children(publisher)]:
            found.append((f"LM Studio model {os.path.basename(model)}", model, "LM Studio can download it again.", True))
    for path in children(home("Library", "Application Support", "nomic.ai", "GPT4All")):
        if path.lower().endswith((".gguf", ".bin")) and os.path.isfile(path):
            found.append((f"GPT4All model {os.path.basename(path)}", path, "GPT4All can download it again.", True))
    for root in (home("jan", "models"), home("Library", "Application Support", "Jan", "data", "models")):
        for model in children(root):
            found.append((f"Jan model {os.path.basename(model)}", model, "Jan can download it again.", True))
    return [entry for entry in found if not os.path.islink(entry[1]) and deletable(entry[1])]


def store_paths():
    """Everything the model stores hold, for the scans that must not list it a second time."""
    return [path for _, path, _, _ in model_stores()] + [home(".ollama"), home("jan", "models")]


def weight_bin(path):
    """.bin and .h5 are any kind of data. One counts as a model only where it plainly is: beside a
    config.json or tokenizer.json, or in a folder named models or checkpoints."""
    folder = os.path.dirname(path)
    named = {part.lower() for part in folder[len(HOME) + 1:].split(os.sep)} & {"models", "checkpoints"}
    return bool(named) or any(os.path.isfile(os.path.join(folder, n)) for n in ("config.json", "tokenizer.json"))


def scan_models():
    """Model weights: what tools keep in their stores, and model files of 100 MB or more anywhere
    else in the home folder. One of those may be a model you trained, which nothing can download
    again, so they go to the Trash. A path is in one row only: Spotlight's hits inside a store are
    left to the store's rows."""
    month_ago = time.time() - MODEL_RECENT
    stores = model_stores()
    cache = home(".cache", "huggingface")
    hub, torch_hub = os.path.join(cache, "hub"), home(".cache", "torch", "hub")
    # Beside the models: only what is known to be a download. Anything else in those folders
    # (another tool's own data, your Hugging Face login) is not headroom's to offer.
    rest = [("Hugging Face downloads", [os.path.join(cache, name) for name in HF_DOWNLOADS]
             + [p for p in children(hub) if os.path.basename(p).startswith(("datasets--", "spaces--", ".locks"))],
             "Datasets and other downloads. Models and your login stay.", cache),
            ("PyTorch downloads", [p for p in children(torch_hub) if os.path.isfile(os.path.join(p, "hubconf.py"))],
             "Code that torch.hub downloaded. Models stay.", torch_hub)]
    rest = [(name, [p for p in paths if not os.path.islink(p) and deletable(p)], what, where)
            for name, paths, what, where in rest]
    sizes = du([path for _, path, _, _ in stores] + [p for _, paths, _, _ in rest for p in paths])
    rows = []
    for name, path, how, yours in stores:
        if path in sizes:
            when = max(os.lstat(path).st_mtime, last_activity(path, beside=False))
            row = item("models", name, [path], sizes[path], f"{how} {tilde(path)} · last changed {ago(when)}")
            row["recent"], row["trash"] = when > month_ago, yours
            row["note"] = " ".join(([LEFT_OUT] if row["recent"] else []) + ([IMPORTED] if yours else []))
            rows.append(row)
    for name, paths, what, where in rest:
        present = [p for p in paths if p in sizes]
        if present:
            rows.append(item("dev", name, present, sum(sizes[p] for p in present), f"{what} {tilde(where)}"))
    ollama, answered = ollama_rows()
    rows += ollama
    hits = spotlight(MODEL_TYPES + MODEL_MAYBE, MODEL_MIN_KB, also=MODEL_PACKAGES)
    if hits is None:
        return rows, Unsure("Spotlight did not answer, so headroom could not look for model files outside the "
                            "tools' own folders. Scan again in a minute.")
    files = loose(hits, taken=store_paths())
    packages = du([path for path, info in files if stat.S_ISDIR(info.st_mode) and path.lower().endswith(MODEL_PACKAGES)])
    named = {}
    for path, info in files:
        low = path.lower()
        if stat.S_ISDIR(info.st_mode) and low.endswith(MODEL_PACKAGES):
            kb, when = packages.get(path, 0), max(info.st_mtime, last_activity(path, beside=False))
        elif stat.S_ISREG(info.st_mode) and (low.endswith(MODEL_TYPES) or (low.endswith(MODEL_MAYBE) and weight_bin(path))):
            kb, when = info.st_blocks * 512 // 1024, max(info.st_mtime, getattr(info, "st_birthtime", 0))
        else:
            continue
        if kb >= MODEL_MIN_KB:
            named.setdefault(os.path.basename(path), []).append((path, kb, when, stat.S_IFMT(info.st_mode)))
    for base, found in sorted(named.items()):
        # A training run can leave the same file name in a hundred folders. Each is a row of its
        # own; together they are one bundle, named after the file and the folder they share.
        common = os.path.commonpath([os.path.dirname(path) for path, *_ in found])
        where = "your home folder" if common == HOME else os.path.basename(common)
        bundle = {"key": "named:" + base, "noun": f"files named {base} in {where}",
                  "detail": f"{tilde(common)} · newest added or changed {ago(max(when for _, _, when, _ in found))}",
                  "note": "They go to the Trash, in case you trained them yourself.", "folder": common, "min": MANY}
        for path, kb, when, kind in found:
            folder = os.path.dirname(path)
            row = item("models", f"{os.path.basename(folder)} / {base}", [path], kb,
                       f"{tilde(folder)} · added or changed {ago(when)}")
            row["trash"], row["seen"], row["when"], row["bundle"] = True, {path: kind}, int(when), bundle
            row["inside"] = os.path.relpath(path, common)
            if when > month_ago:
                row["recent"], row["note"] = True, LEFT_OUT
            elif repo_root(folder):
                # A model in a project is more likely part of the project than a leftover.
                row["recent"], row["note"] = True, "Inside a git project, so ticking the whole section leaves it out."
            row["note"] = f"{row['note']} Goes to the Trash, in case you trained it yourself.".strip()
            rows.append(row)
    return rows, "" if answered else Unsure("")


def scan_archives():
    """Archives of 20 MB or more (a plain .gz, .bz2 or .xz from 100 MB) that Spotlight knows in the
    home folder. Zipped apps and installers in Downloads and on the Desktop stay under Installer
    files and are left out here, and so is anything inside a git project (fixtures, test data) or a
    model store. An archive with a folder of its own name beside it looks unzipped already: those
    come first. Its contents are never read. Every row goes to the Trash."""
    hits = spotlight(ARCHIVE_TYPES + PACKED_TYPES, min(ARCHIVE_MIN_KB, PACKED_MIN_KB))
    if hits is None:
        return [], Unsure("Spotlight did not answer, so headroom could not look for archives. Scan again in a minute.")
    groups = {}
    for path, info in loose(hits, taken=find_installers() + store_paths() + go_caches()[1]):
        low, kb = path.lower(), info.st_blocks * 512 // 1024
        ending = next((e for e in ARCHIVE_TYPES if low.endswith(e)), None)
        if not ending:
            ending = next((e for e in PACKED_TYPES if low.endswith(e)), None)
            kb = kb if kb >= PACKED_MIN_KB else 0
        if not ending or kb < ARCHIVE_MIN_KB or not stat.S_ISREG(info.st_mode) or repo_root(os.path.dirname(path)):
            continue
        base = os.path.basename(path)[:-len(ending)]
        beside = os.path.join(os.path.dirname(path), base)
        kind = "" if not base or os.path.islink(beside) else "folder" if os.path.isdir(beside) else (
            "file" if os.path.isfile(beside) else "")
        groups.setdefault((os.path.dirname(path), bool(kind)), []).append((path, kb, base, kind, info))
    rows = []
    for (folder, unzipped), found in sorted(groups.items()):
        # A folder full of them is one bundle, not hundreds of lines; each archive is still its own row.
        newest = max(getattr(info, "st_birthtime", info.st_mtime) for *_, info in found)
        # The key goes to the page and into sizes.json: the folder is in it as a hash, not as a path.
        bundle = {"key": f"archives:{hashlib.sha1(folder.encode()).hexdigest()[:12]}:{int(unzipped)}",
                  "noun": f"archives {'already unzipped ' if unzipped else ''}in {os.path.basename(folder)}",
                  "detail": f"{tilde(folder)} · newest added {ago(newest)}",
                  "note": "Each has a folder or file of its own name next to it." if unzipped else "",
                  "folder": folder, "min": MANY}
        for path, kb, base, kind, info in found:
            made = getattr(info, "st_birthtime", info.st_mtime)
            row = item("archives", f"{os.path.basename(folder)} / {os.path.basename(path)}", [path], kb,
                       f"{tilde(folder)} · added {ago(made)}")
            row["note"] = f"A {kind} named {base} is next to it." if kind else ""
            row["trash"], row["rank"], row["when"], row["bundle"] = True, int(unzipped), int(made), bundle
            row["inside"], row["seen"] = os.path.basename(path), {path: stat.S_IFMT(info.st_mode)}
            rows.append(row)
    return rows, ""


# --- local copies of iCloud Drive files ----------------------------------------------------------------

CLOUD_ROOTS = []        # the iCloud folders the last listing named: the only places a local copy is removed from
CLOUD_STAYS = "They stay in iCloud and download again when you open them."


def cloud_folders():
    """The only folders that can be iCloud's: iCloud Drive itself, and Desktop and Documents."""
    return [home("Library", "Mobile Documents", "com~apple~CloudDocs"), home("Desktop"), home("Documents")]


def cloud_name(path):
    """A path inside iCloud Drive as Finder calls it: iCloud Drive/Projects, not the folder's real name."""
    drive = cloud_folders()[0]
    return "iCloud Drive" + path[len(drive):] if path == drive or path.startswith(drive + os.sep) else tilde(path)


def cloud_listing():
    """What the app's iCloud helper says: {drive, online, account, roots, files: [{path, bytes, used}]}.
    None when there is no helper here, it did not answer, or what it said isn't that."""
    if not CLOUD:
        return None
    try:
        done = run([CLOUD, "list", str(CLOUD_MIN_KB * 1024)], timeout=300)
        said = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(said, dict) or not isinstance(said.get("roots"), list) or not isinstance(said.get("files"), list):
        return None
    return said


def scan_cloud():
    """Files in iCloud Drive that also take up room on this Mac: 10 MB or more, uploaded in full
    and not changing. Removing the local copy frees the room; the file stays in iCloud and comes
    down again when it is opened. Nothing is deleted. The helper decides what is safe to name, and
    every path it names is looked at again here: only in iCloud's own folders, never by way of a
    link, and never a file another section already offers."""
    # Off unless switched on under Maintenance: then nothing is asked, listed or remembered.
    rows, notice, roots = cloud_rows() if settings()["cloud"] else ([], "", [])
    CLOUD_ROOTS[:] = roots      # what the rows of this scan may be removed from, and nothing older
    return rows, notice


def settled(info):
    """True when neither a file's contents nor anything else about it changed in the last hour.
    "Uploaded" only says iCloud has data for the file, not that it has the newest edit, so a file
    that changed a moment ago keeps its copy. The helper asks the same of macOS."""
    return max(info.st_mtime, info.st_ctime) < clock() - CLOUD_QUIET


def cloud_rows():
    if not CLOUD:
        return [], ("Removing local copies needs the small helper that comes with the headroom app. From source, "
                    "build it with launcher/build.sh and point HEADROOM_CLOUD at it."), []
    said = cloud_listing()
    if said is None:
        return [], Unsure("headroom's iCloud helper did not answer. Scan again in a minute."), []
    if not said.get("drive"):
        return [], "iCloud Drive isn't set up on this Mac, so there are no local copies to remove.", []
    if not said.get("online"):
        return [], Unsure("This Mac is offline. A local copy that goes could not come back until it is online "
                          "again, so nothing is offered now."), []
    roots = [root for root in said["roots"] if isinstance(root, str) and root in cloud_folders()
             and os.path.isdir(root) and os.path.realpath(root) == root]
    found = []
    for entry in said["files"]:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str) or os.path.normpath(path) != path or not deletable(path) \
                or not any(path.startswith(root + os.sep) for root in roots):
            continue
        try:
            info = os.lstat(path)
        except OSError:
            continue
        parts = path[len(HOME) + 1:].split(os.sep)
        hidden = any(part.startswith(".") for part in parts) or any(part.lower().endswith(PACKAGES + MODEL_PACKAGES) for part in parts[:-1])
        kb = info.st_blocks * 512 // 1024       # measured here: what the helper says is only a hint
        if stat.S_ISREG(info.st_mode) and not dataless(info) and not hidden and kb >= CLOUD_MIN_KB and settled(info):
            found.append((path, kb, max(info.st_mtime, info.st_atime, getattr(info, "st_birthtime", 0)), info))
    # A path is in one row only. Desktop and Documents are other sections' ground too: an archive,
    # a model file, an installer or a project's build folder there is theirs, and so is a
    # screenshot or screen recording wherever macOS saves those.
    drive, theirs = cloud_folders()[0], set()
    shared = [path for path, *_ in found if not path.startswith(drive + os.sep)]
    kept = {path for path, _ in loose(shared)} if shared else set()
    theirs.update(path for path in shared if path not in kept or path.lower().endswith(
        ARCHIVE_TYPES + PACKED_TYPES + MODEL_TYPES + MODEL_MAYBE + INSTALLERS))
    shots = [path for path, *_ in found if path.lower().endswith(SHOT_TYPES + CLIP_TYPES)]
    folder = capture_folder() if shots else ""
    shots = [path for path in shots if os.path.dirname(path) == folder]
    if shots:
        flagged, named = flagged_captures(shots)[0], capture_names()
        theirs.update(path for path in shots if path in flagged or named.fullmatch(os.path.splitext(os.path.basename(path))[0]))
    rows, folders = [], {}
    for path, kb, used, info in found:
        if path in theirs:
            continue
        folder = os.path.dirname(path)
        row = item("cloud", f"{os.path.basename(folder)} / {os.path.basename(path)}", [path], kb,
                   f"{cloud_name(folder)} · opened or changed {ago(used)}")
        row["evict"], row["seen"], row["when"], row["inside"] = True, {path: stat.S_IFMT(info.st_mode)}, int(used), os.path.basename(path)
        row["recent"] = used > time.time() - CLOUD_RECENT
        row["note"] = ("Opened or changed in the last 30 days, so ticking the whole section leaves it out. " if row["recent"] else "") \
            + "Only the copy on this Mac goes. The file stays in iCloud."
        row["bundle"] = folders.setdefault(folder, {
            "key": "cloud:" + hashlib.sha1(folder.encode()).hexdigest()[:12], "noun": f"files in {os.path.basename(folder)}",
            "detail": cloud_name(folder), "note": CLOUD_STAYS, "left": "opened or changed in the last 30 days",
            "folder": folder, "min": 1})
        rows.append(row)
    if not rows and said.get("account") is False:
        return [], "macOS says nobody is signed in to iCloud Drive on this Mac, so there are no local copies to remove.", roots
    return rows, "", roots


def cloud_evict_many(paths):
    """Remove the local copies of iCloud Drive files, through the app's helper: {path: '' when the
    copy went, else why it stayed}. The helper asks macOS about each file again right before, and
    macOS does the removing. A call that fails as a whole says so for every path."""
    gone = "headroom's iCloud helper did not answer, so the copy stayed."
    if not paths:
        return {}
    try:
        done = run([CLOUD, "evict", "--"] + list(paths), timeout=900) if CLOUD else None
        said = json.loads(done.stdout) if done and done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        said = None
    whys = dict.fromkeys(paths, gone)
    for answer in said if isinstance(said, list) else ():
        # Only what was asked about, and only a plain yes counts as gone. Anything else the
        # helper says (a path that is not a string, say) is no answer at all.
        if isinstance(answer, dict) and isinstance(answer.get("path"), str) and answer["path"] in whys:
            whys[answer["path"]] = "" if answer.get("ok") is True else sentence(str(answer.get("why") or "macOS kept the copy"))
    return whys


def evict_plan(row):
    """For a row of iCloud files: (why some stay, the paths to hand to the helper). Only with the
    switch under Maintenance on, and only a plain file the scan saw, in a folder the last listing
    named as iCloud's, not reached through a link and not changed in the last hour: the listing
    may be old by now."""
    if not settings()["cloud"]:         # also when the settings can't be read: off
        return ["Left alone: local copies of iCloud Drive files are switched off under Maintenance."], []
    errors, going = [], []
    for path in row["paths"]:
        try:
            info = os.lstat(path)
            kind = stat.S_IFMT(info.st_mode)
        except OSError:
            info = kind = None
        if kind != stat.S_IFREG or row["seen"].get(path) != kind:
            errors.append(f"Left alone, it is not what the scan found there any more: {tilde(path)}")
        elif not deletable(path) or not any(path.startswith(root + os.sep) for root in CLOUD_ROOTS):
            errors.append(f"Left alone, it is outside iCloud Drive: {tilde(path)}")
        elif not settled(info):
            errors.append(f"Left alone, it changed in the last hour: {tilde(path)}")
        else:
            going.append(path)
    return errors, going


# --- Mail and Messages attachments ----------------------------------------------------------------------

def messages_in_icloud():
    """True when Messages in iCloud is on (macOS keeps that in the com.apple.madrid settings)."""
    try:
        done = run(["/usr/bin/defaults", "read", "com.apple.madrid", "CloudKitSyncingEnabled"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and done.stdout.strip() == "1"


def scan_attachments():
    """Copies Mail made of attachments you opened (the messages keep the originals), and the big
    files inside Messages conversations (20 MB or more). Both are behind Full Disk Access, and both
    go to the Trash. A Messages attachment is part of a conversation: removing it leaves a blank
    spot there, so none of them is ever picked by ticking the section. chat.db is never read."""
    shut, rows = [], []
    mail = home("Library", "Containers", "com.apple.mail", "Data", "Library", "Mail Downloads")
    theirs = set(find_installers())     # installers and zipped apps in there are Installer files' rows
    copies = []
    for entry in listing(mail, shut, "the copies Mail keeps of opened attachments"):
        # Mail Downloads/<one folder an attachment>/<the file>
        copies += children(entry) if os.path.isdir(entry) and not os.path.islink(entry) else [entry]
    # Not a link, and not behind one: only what the Trash would take.
    copies = [p for p in copies if p not in theirs and not p.lower().endswith(".torrent") and not os.path.islink(p) and deletable(p)]
    sizes = du(copies)
    downloads = {"key": "mail:downloads", "noun": "copies of attachments Mail opened", "detail": tilde(mail),
                 "note": "The messages keep the originals.", "folder": mail, "min": 1}
    for path in copies:
        if path in sizes:
            try:
                info = os.lstat(path)       # once: a copy that goes missing meanwhile is just not listed
            except OSError:
                continue
            made = getattr(info, "st_birthtime", info.st_mtime)
            row = item("mail", os.path.basename(path), [path], sizes[path], f"A copy Mail made · added {ago(made)}")
            row["trash"], row["when"], row["inside"], row["bundle"] = True, int(made), os.path.basename(path), downloads
            row["seen"], row["rank"] = {path: stat.S_IFMT(info.st_mode)}, 999999      # Mail's copies first
            rows.append(row)
    attachments = home("Library", "Messages", "Attachments")
    cloud = " With Messages in iCloud on, it may still be in iCloud." if messages_in_icloud() else ""
    blank = "Removing it leaves a blank spot in its conversation in Messages on this Mac."
    months = {}
    for top in listing(attachments, shut, "your Messages attachments"):
        for folder, _, names in os.walk(top) if os.path.isdir(top) and not os.path.islink(top) else ():
            for name in names:
                path = os.path.join(folder, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                kb = info.st_blocks * 512 // 1024
                if not stat.S_ISREG(info.st_mode) or kb < MESSAGE_MIN_KB or dataless(info) or not deletable(path):
                    continue
                made = getattr(info, "st_birthtime", info.st_mtime)
                month = time.strftime("%Y-%m", time.localtime(made))
                row = item("mail", name, [path], kb, f"In a Messages conversation · added {ago(made)}")
                row["trash"], row["when"], row["inside"] = True, int(made), name
                row["seen"], row["rank"] = {path: stat.S_IFMT(info.st_mode)}, int(month.replace("-", ""))
                # Never by ticking the section: each one, or each month of them, by hand.
                row["recent"], row["note"] = True, f"{blank}{cloud} Tick it yourself: ticking the whole section leaves it out."
                row["bundle"] = months.setdefault(month, {
                    "key": "messages:" + month,
                    "noun": f"Messages attachments from {time.strftime('%B %Y', time.strptime(month, '%Y-%m'))}",
                    "detail": tilde(attachments),
                    "note": f"Removing one leaves a blank spot in its conversation in Messages on this Mac.{cloud.replace(' it ', ' they ')}",
                    "left": "each is part of a conversation", "folder": attachments, "min": 1})
                rows.append(row)
    return rows, full_disk_access(list(dict.fromkeys(shut))) if shut else ""


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
        if dataless(os.lstat(path)):        # only in iCloud: reading it would download it
            return None
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
            row["same"] = "torrents" if group == "installers" else ""      # its name holds a count
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
        failed = bool(cleanup.returncode or autoremove.returncode)
    except (OSError, subprocess.TimeoutExpired):
        failed = True
    if failed:      # no answer is not "nothing to clean"
        return [item("dev", "Homebrew old versions", [], 0, prefix,
                     blocked="Homebrew did not answer. Scan again later.")], Unsure()
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
        rows[-1]["same"] = "homebrew:old"
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
        rows[-1]["tidy"], rows[-1]["same"] = True, "leftovers:small"
    else:
        rows += small
    return rows


# source -> (the sections its rows land in, the scan)
# Quick ones first: the page asks for them in this order, six at a time, so it fills in fast.
SOURCES = {
    "logs": (("temp",), scan_logs),
    "temp": (("temp",), scan_temp),
    "browser_copies": (("temp",), scan_browser_copies),
    "trash": (("trash",), scan_trash),
    "installers": (("installers",), scan_installers),
    "screenshots": (("shots",), scan_screenshots),
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
    "models": (("models", "dev"), scan_models),
    "archives": (("archives",), scan_archives),
    "attachments": (("mail",), scan_attachments),
    "cloud": (("cloud",), scan_cloud),
    "dev_tools": (("dev",), scan_dev_tools),
    "simulators": (("dev", "big"), scan_simulators),
    "builds": (("builds", "dev"), scan_builds),     # and the big .git folders of the same projects
    "homebrew": (("dev",), scan_homebrew),      # slow: Homebrew's dry runs take a while
}

# Sources that wait on another program, which can take its time (Homebrew's dry runs take about
# 20 seconds): the page calls the scan finished without them and fills their rows in as they land.
SLOW = {"homebrew": "Homebrew", "docker": "Docker", "simulators": "the simulators", "cloud": "iCloud Drive"}

FOUND = {}   # source -> {id: row}. The only things a delete will touch.
LOCK = threading.Lock()


def processes():
    return run(["/bin/ps", "-axo", "command="]).stdout


def blocked(row, running):
    if row["blocked"]:
        return row["blocked"]
    if row["quit"] and any(re.search(p, running, re.I | re.M) for p in row["quit"][1]):
        return row["quit"][0]
    return git_busy(row["git"]) if row["git"] else ""


def safe_row(row, alone=False):
    """Whether Safe clean takes a row. The one rule: the button on the page goes by it (it is the
    row's `safe`), and so do the weekly reminder's sum and the weekly run. App caches, logs and
    temp files, and installers over a month old. Never one used this week, one that a command
    clears (git gc, Docker, Homebrew), or the local copy of an iCloud Drive file.

    `alone` is the weekly run by itself, which takes less: caches, logs and old temporary files
    only. An installer is a file in a folder you look at (Downloads, the Desktop), and nobody is
    watching that run: those wait for the button."""
    takes = row["group"] in SAFE_GROUPS or (row["safe"] and not alone)
    return bool(takes and not row["recent"] and not row["cmd"] and not row["git"] and not row["evict"])


def stays_put(row):
    """Why a row can't be cleaned while "Move cleaned files to the Trash" is on: '' when its files
    can go there. Nothing is removed for good in that mode, so what only a command can remove
    stays, and so does what is in the Trash already."""
    if row["cmd"]:
        return "Can't go to the Trash, so it waits while Move to Trash is on. Switch that off under Maintenance to clear it."
    if any(path.startswith(home(".Trash") + os.sep) for path in row["paths"]):
        return "It is the Trash already, so it waits while Move to Trash is on. Switch that off under Maintenance to empty it."
    return ""


def public(row, running, to_trash=False):
    return {"id": row["id"], "group": row["group"], "name": row["name"], "detail": row["detail"],
            "kb": row["kb"], "blocked": blocked(row, running) or (stays_put(row) if to_trash else ""),
            "note": row["note"], "recent": row["recent"], "safe": safe_row(row),
            "trash": row["trash"], "action": "git gc" if row["git"] else "", "rank": row["rank"],
            "weighs": row["weighs"], "bundle": row["bundle"]["key"] if row["bundle"] else "", "inside": row["inside"],
            "when": row["when"], "evict": row["evict"]}


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


def protected_broken():
    """True when the Never offer list is there but can't be read as one. protected() takes that
    for an empty list, which the page shows for what it is. Nobody sees it when Safe clean runs
    by itself, so that run asks here first."""
    if not os.path.lexists(protected_file()):
        return False
    try:
        with open(protected_file()) as handle:
            entries = json.load(handle)
    except (OSError, ValueError):
        return True
    return not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("keys"), list) and all(isinstance(k, str) for k in e["keys"]) for e in entries)


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


def remember(what, section, paths, kb, result, error="", command=None, note=""):
    """One line in the history: append-only, one JSON object per line."""
    entry = {"at": now(), "what": what, "section": section,
             "paths": [tilde(p) for p in paths], "kb": kb, "result": result}
    if command:
        entry["command"] = " ".join(command)
    if error:
        entry["error"] = error
    if note:
        entry["note"] = note
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


SCANNING = {}   # source -> the scan of it that is running now, which later callers wait for


def scan(source):
    """Scan one source for the page. A second call while the first is still running (the page
    reloaded, "Scan again") waits for that one and gets its answer: the programs behind a slow
    source are never started twice over."""
    with LOCK:
        mine = SCANNING.get(source)
        first = mine is None
        if first:
            mine = SCANNING[source] = {"done": threading.Event(), "result": None, "error": None}
    if not first:
        mine["done"].wait()
        if mine["error"]:
            raise mine["error"]
        return mine["result"]
    try:
        mine["result"] = scan_once(source)
        return mine["result"]
    except OSError as error:
        mine["error"] = error
        raise
    finally:
        with LOCK:
            SCANNING.pop(source, None)
        mine["done"].set()


def scan_once(source):
    found = SOURCES[source][1]()
    rows, notice = found if isinstance(found, tuple) else (found, "")
    rows = unprotected(rows)
    for row in rows:
        # du gave a number without getting through all of it (see _du): not to be read as the size.
        row["partly"] = bool(PARTLY.intersection(row["paths"]))
        if row["partly"]:
            row["note"] = f"{row['note']} At least this much: part of it could not be measured.".strip()
    # A row under 1 MB is not worth a line, unless it is one of a bundle that is: a month of
    # screenshots is mostly files smaller than that.
    together = collections.Counter()
    for row in rows:
        if row["bundle"]:
            together[row["bundle"]["key"]] += row["kb"]
    rows = [r for r in rows if r["kb"] >= MIN_KB or r["blocked"] or r["tidy"] or r["partly"]
            or (r["bundle"] and together[r["bundle"]["key"]] >= MIN_KB)]
    with LOCK:
        FOUND[source] = {r["id"]: r for r in rows}
    running, to_trash = processes(), settings()["trash"]
    # What the page needs to draw a bundle's one line, once for the bundle and not on every row.
    bundles = {r["bundle"]["key"]: {k: r["bundle"][k] for k in ("noun", "detail", "note", "min", "left") if k in r["bundle"]}
               for r in rows if r["bundle"]}
    result = {"items": [public(r, running, to_trash) for r in rows], "notice": notice, "bundles": bundles}
    with STATE_LOCK:
        last = load_state("last-scan.json", {})
        last[source] = {"at": time.time(), **result}
        save_state("last-scan.json", last)
    keep_sizes(source, rows, failed=isinstance(notice, Unsure))
    return result


# --- what grew: the sizes each day's scan measured ------------------------------------------------

clock = time.time       # what "now" is for the day's sizes and for "changed in the last hour"; the tests move it


def day_of(when):
    return time.strftime("%Y-%m-%d", time.localtime(when))


def saved_sizes(today):
    """{day: {source: {row: [section, name, kb]}}} as sizes.json holds it: days that are dates, and
    none after today (a clock that was wrong for a while must not become the newest scan for good)."""
    days = load_state("sizes.json", {})
    kept = {}
    for day, sources in days.items() if isinstance(days, dict) else ():
        try:
            datetime.date.fromisoformat(day)
        except ValueError:
            continue
        if len(day) == 10 and day <= today and isinstance(sources, dict):
            kept[day] = sources
    return kept


def keep_sizes(source, rows, when=None, failed=False):
    """Remember what a scan of one source measured, for "What grew". One snapshot a day: a later
    scan the same day replaces it, and days over 30 days old are dropped, so the file stays small.
    A row is kept under what stays the same about it (`same`, else its id), and the rows of a
    bundle as one entry: their sizes added up, under the bundle's key. A row the scan could
    not measure (Docker was off, say) is kept without a size, and a source whose scan `failed`
    without any: an unknown must not read as 0 and then as growth a week later."""
    when = clock() if when is None else when
    day, oldest = day_of(when), day_of(when - SIZES_DAYS * DAY)
    with STATE_LOCK:
        days = saved_sizes(max(day, day_of(clock())))
        today = days[day] if isinstance(days.get(day), dict) else {}
        entries = {}
        for r in rows:
            if r["bundle"]:
                # A bundle is kept as one thing under its own key: its files come and go, and three
                # thousand screenshots a day for a month would be a file of megabytes.
                entry = entries.setdefault(r["bundle"]["key"], [r["group"], r["name"], 0, 0])
                entry[2], entry[3] = entry[2] + r["kb"], entry[3] + 1
                if entry[3] == 2:
                    entry[1] = r["bundle"]["noun"][:1].upper() + r["bundle"]["noun"][1:]
            else:
                entries[r["same"] or r["id"]] = [r["group"], r["name"], None if r["blocked"] or r.get("partly") else r["kb"]]
        today[source] = None if failed else {key: entry[:3] for key, entry in entries.items()}
        days[day] = today
        save_state("sizes.json", {d: sources for d, sources in days.items() if d >= oldest})


def keep_found(ids):
    """After a delete: the day's sizes for the sources those rows came from, as they are now."""
    with LOCK:
        touched = {source: list(found.values()) for source, found in FOUND.items() if ids & set(found)}
    for source, rows in touched.items():
        keep_sizes(source, rows)


def sized(rows):
    """One source's saved rows as {row: [section, name, kb or None]}, or None when the scan failed
    that day or the entry isn't what keep_sizes() wrote. A row that isn't is left out."""
    if not isinstance(rows, dict):
        return None
    return {key: entry for key, entry in rows.items()
            if isinstance(entry, list) and len(entry) == 3 and isinstance(entry[0], str) and isinstance(entry[1], str)
            and (entry[2] is None or isinstance(entry[2], int))}


def growth(now=None):
    """What grew since about a week ago: {span, sections: {section: kb}, top: [...]}. Each source's
    newest sizes against an earlier day's. That day is the oldest one 5 to 9 days before the newest
    scan ("this week"). With none in that range it is the nearest older day, else the oldest newer
    one, and `span` says how many days back that is. With no earlier day there is nothing to
    compare: span is '' and no numbers. A source, or a single row, that could not be measured on
    either day is left out, so it never reads as growth."""
    days = saved_sizes(day_of(clock() if now is None else now))
    latest = max(days, default="")
    back = {d: (datetime.date.fromisoformat(latest) - datetime.date.fromisoformat(d)).days for d in days if d < latest}
    week = [d for d in back if 5 <= back[d] <= 9]
    older = [d for d in back if back[d] > 9]
    base = min(week) if week else max(older) if older else min(back, default="")
    if not base:
        return {"span": "", "sections": {}, "top": []}
    span = "this week" if week else "in " + count(back[base], "day", "days")
    sections, grown_rows = collections.Counter(), []
    for source in {source for day in days if day > base for source in days[day]}:
        # The newest day that scanned this source: a later day may hold only what a delete touched.
        newest = max(day for day in days if day > base and source in days[day])
        before, after = sized(days[base].get(source)), sized(days[newest][source])
        if before is None or after is None:
            continue
        # A row that holds many paths gets a new id when one of them changes: then its name finds it.
        by_name = {(group, name): key for key, (group, name, _) in before.items() if key not in after}
        matched = set()
        for key, (group, name, kb) in after.items():
            old = key if key in before else by_name.get((group, name))
            matched.add(old)
            if kb is None or (old is not None and before[old][2] is None):
                continue
            was = before[old][2] if old is not None else 0
            sections[group] += kb - was
            if kb - was >= GREW_KB:
                grown_rows.append({"name": name, "group": group, "kb": kb - was, "new": old is None})
        for key, (group, _, kb) in before.items():
            if key not in matched and kb is not None:
                sections[group] -= kb       # it was there then and isn't now
    grown = {group: kb for group, kb in sections.items() if kb >= GREW_KB}
    # A section's own line only when none of its rows is listed: the same growth isn't said twice.
    listed = {entry["group"] for entry in grown_rows}
    top = grown_rows + [{"name": "", "group": group, "kb": kb, "new": False}     # '' is the section itself
                        for group, kb in grown.items() if group not in listed]
    top.sort(key=lambda entry: (-entry["kb"], entry["name"].lower()))
    return {"span": span, "sections": grown, "top": top[:8]}


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
    or the per-user temp folder, and never by way of a symlinked parent. Beside the temp folder,
    only a browser's left-behind copy of itself (see is_clone): nothing else in X."""
    real = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    if real != path:
        return False
    if is_clone(path):
        return True
    for root, depth in ((HOME, 2), (TMP, 1)):
        if path.startswith(root + os.sep) and len(os.path.relpath(path, root).split(os.sep)) >= depth:
            return True
    return False


def sentence(text):
    """What the system said, as a sentence: "Too many open files" gets its full stop."""
    text = str(text).strip()
    return text if not text or text[-1] in ".!?" else text + "."


def first_line(text):
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def removal_error(stderr, stuck):
    """rm's complaint in plain words. macOS-protected data is the usual reason."""
    if "Operation not permitted" in stderr or "Permission denied" in stderr:
        if stuck:
            return f"macOS protects {', '.join(os.path.basename(p) for p in stuck[:3])}, so it stayed."
        return "macOS protects part of this, so some files stayed."
    return first_line(stderr) or "Some files could not be removed."


def refusal(row, to_trash):
    """Why a row can't be cleared right now: an app that must be quit first, a repository in the
    middle of something, or (with the switch on) a row that can't go to the Trash. '' when it can."""
    return blocked(row, processes() if row["quit"] else "") or (stays_put(row) if to_trash else "")


def trash_plan(row, to_trash):
    """For a row on its way to the Trash: (why some of it stays, the paths to hand to Finder).
    Every check is made here, path by path, before Finder hears of any of them."""
    errors, going = [], []
    busy = clones_in_use(row["paths"])      # asked again now: a browser may have started since the scan
    if busy:
        errors.append(f"{count(len(busy), 'copy is', 'copies are')} in use by a running browser, so "
                      f"{'it' if len(busy) == 1 else 'they'} stayed.")
    for path in row["paths"]:
        if path in busy:
            continue
        seen = row["seen"].get(path)
        try:
            kind = stat.S_IFMT(os.lstat(path).st_mode)
        except OSError:
            kind = None
        if seen is not None and kind != seen:
            # One of your own files: only the very thing the scan looked at goes.
            errors.append(f"Left alone, it is not what the scan found there any more: {tilde(path)}")
        elif kind == stat.S_IFLNK and not row["trash"]:
            pass        # a link among cache folders: Finder would move what it points at, so it just stays
        elif not trashable(path):
            errors.append(f"Left alone, it is outside the folders this tool clears: {tilde(path)}")
        else:
            going.append(path)
    return errors, going


def settle(row, errors, left=None):
    """What a row comes to once its paths were dealt with: what is left of it, and so what went."""
    if left is None:
        left = sum(du(row["paths"]).values()) if row["paths"] else (row["kb"] if errors else 0)
    freed = max(row["kb"] - left, 0)
    row["kb"] = left
    return {"freed_kb": freed, "left_kb": left, "errors": list(dict.fromkeys(errors))[:3], "note": ""}


def handed_over(row, errors, going, whys, left):
    """What a row comes to once its paths were handed to Finder or to the iCloud helper. `whys` is
    what that said for each path ('' for a plain yes), `left` what is there now as {path: kb}, or
    None when that could not be measured. Only a yes counts as freed: a path that was refused, or
    never handed over, frees nothing here whatever became of it since. (A row with some of its
    paths refused is measured as a whole.)"""
    said = {path: whys.get(path, "It was not answered for.") for path in going}
    went = [path for path in going if not said[path]]
    whole = len(went) == len(row["paths"])
    size = row["kb"]
    result = settle(row, errors + [why for why in said.values() if why],
                    (0 if whole else size) if left is None else sum(left.get(path, 0) for path in row["paths"]))
    result["unmeasured"] = bool(went) and left is None      # it went; only its size can't be said
    if not went or left is None:
        result["freed_kb"] = 0
    # macOS said yes and the room is not back yet (it may free it after the call returns): the
    # row is done, and no size is claimed for it.
    result["asked"] = bool(row["evict"] and whole and not result["errors"] and not result["freed_kb"])
    if result["asked"]:
        result["left_kb"] = row["kb"] = 0
    return result


def delete(row, to_trash=False):
    """Clear one row. With `to_trash` (the switch in Maintenance) its paths go to the Trash through
    Finder and nothing is removed for good: no `rm`, and no row that only a command can clear."""
    why = refusal(row, to_trash)
    if why:
        return {"freed_kb": 0, "left_kb": row["kb"], "errors": [why], "note": ""}
    if row["evict"]:
        # An iCloud Drive file: its copy on this Mac goes, through macOS. Never rm, never the Trash,
        # whatever the switch says: either would delete the file from iCloud too.
        errors, going = evict_plan(row)
        whys = cloud_evict_many(going)
        return handed_over(row, errors, going, whys, du(row["paths"]))
    if row["cmd"]:
        errors, said, note = [], None, ""
        store = os.path.join(row["git"], ".git") if row["git"] else None
        before = sum(du([store]).values()) if store else 0
        try:
            done = run(row["cmd"], timeout=3600)
            if done.returncode:
                errors.append(first_line(done.stderr) or "The clean-up command failed.")
            said = re.search(r"Total[^\n]*?([\d.]+\s*[kMGT]?B)", done.stdout)
        except (OSError, subprocess.TimeoutExpired):
            errors.append("The clean-up command did not finish.")
        result = settle(row, errors)
        if said:
            result["freed_kb"] = size_kb(said.group(1))
        if store:
            after = sum(du([store]).values())
            result["freed_kb"], result["note"] = max(before - after, 0), f".git was {size_text(before)} and is {size_text(after)} now."
        return result
    if row["trash"] or to_trash:
        errors, going = trash_plan(row, to_trash)
        for start in range(0, len(going), TRASH_BATCH):
            if start and JOB["stopping"]:       # asked before the next batch, never after the last one
                errors.append("Stopped before all of it was moved.")
                break
            whys = [why for why in finder_trash_many(going[start:start + TRASH_BATCH]).values() if why]
            errors += whys
            if NO_FINDER in whys or NOT_ALLOWED in whys:
                break       # the rest of the row would only hear the same
        return settle(row, errors)
    errors, allowed = [], []
    busy = clones_in_use(row["paths"])      # asked again now: a browser may have started since the scan
    if busy:
        errors.append(f"{count(len(busy), 'copy is', 'copies are')} in use by a running browser, so "
                      f"{'it' if len(busy) == 1 else 'they'} stayed.")
    for path in row["paths"]:
        if path in busy:
            continue
        if deletable(path):
            allowed.append(path)
        else:
            errors.append(f"Left alone, it is outside the folders this tool clears: {tilde(path)}")
    for start in range(0, len(allowed), 200):     # a row can hold hundreds of small files
        batch = allowed[start:start + 200]
        done = run(["/bin/rm", "-rf", "--"] + batch)
        if done.returncode:
            errors.append(removal_error(done.stderr, [p for p in batch if os.path.lexists(p)]))
    return settle(row, errors)


def disk():
    usage = shutil.disk_usage(HOME)
    return {"free_kb": usage.free // 1024, "total_kb": usage.total // 1024}


# The one delete run at a time, as the page's progress bar reads it.
JOB = {"running": False, "stopping": False, "total_kb": 0, "done_kb": 0, "item_kb": 0,
       "index": 0, "count": 0, "current": None, "results": [], "trash": False}


def free_kb():
    return shutil.disk_usage(HOME).free // 1024


def watch_item(size, before, finished):
    """Progress inside one row, read off the disk's free space as it grows."""
    while not finished.wait(0.5):
        JOB["item_kb"] = max(JOB["item_kb"], min(size, free_kb() - before))


def begin_job(rows, to_trash):
    """Mark a job as started, as the page's progress bar reads it. Whoever calls has made sure no other is running."""
    JOB.update(running=True, stopping=False, total_kb=sum(r["kb"] for r in rows), done_kb=0,
               item_kb=0, index=0, count=len(rows), current=None, results=[], trash=to_trash)


def job_lock(wait):
    """The lock a job holds from start to end. headroom is two programs at times (the page's
    server, and the half-hourly check that can run Safe clean by itself), and only one of them
    clears at a time. The open file: close it to let go. None when another job has it and `wait`
    is False, or when the lock can't be had at all."""
    try:
        os.makedirs(state_dir(), exist_ok=True)
        handle = open(os.path.join(state_dir(), "job.lock"), "w")
    except OSError:
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except OSError:
        handle.close()
        return None
    return handle


def run_job(rows, to_trash=False, held=None):
    """Clear the rows one after another. Rows on their way to the Trash don't each get a call to
    Finder: up to a hundred paths wait together and go in one, and every row still gets its own
    result. iCloud files whose local copy goes wait together the same way, for the iCloud helper.
    Stop is honoured between such calls: rows that were waiting stay as they were."""
    waiting, bundled = [], {}       # (index, row, its size, why some of it stays, paths to hand over); bundle -> results
    mine = None if held else job_lock(wait=True)      # `held`: the caller took the lock before it scanned

    def done(row, size, result):
        errors = result["errors"]
        # "asked": macOS agreed to remove an iCloud file's copy and no room has come back yet. That
        # is not called removed, and no size is given for it.
        asked, unmeasured = bool(result.get("asked")), bool(result.get("unmeasured"))
        evicted = bool(row["evict"]) and not asked
        trashed = not row["evict"] and bool(row["trash"] or to_trash)
        outcome = "failed" if errors and not result["freed_kb"] and not unmeasured else "partly" if errors else (
            "compacted" if row["git"] else "asked" if asked else "evicted" if evicted else "trashed" if trashed else "deleted")
        if row["bundle"]:
            bundled.setdefault(row["bundle"]["key"], []).append((row, result, outcome))     # one history line for them all
        else:
            # A git command is kept the short way: its real line is three settings and two paths long.
            command = ["git", "-C", tilde(row["git"]), "gc"] if row["git"] else row["cmd"]
            remember(row["name"], row["group"], row["paths"], result["freed_kb"], outcome, errors[0] if errors else "",
                     command, result["note"] or ("What it freed could not be measured." if unmeasured and not asked else ""))
        JOB["results"].append({"id": row["id"], "name": row["name"], "trashed": trashed, "evicted": evicted, **result})
        JOB.update(done_kb=JOB["done_kb"] + size, item_kb=0)

    def hand_over():
        if not waiting:
            return
        index, first = waiting[0][0], waiting[0][1]
        JOB.update(index=index, item_kb=0, current={"id": first["id"], "name": first["name"], "kb": waiting[0][2]})
        # All of one kind: Finder for the Trash, the helper for local copies of iCloud files.
        hand = cloud_evict_many if first["evict"] else finder_trash_many
        whys = hand([path for *_, going in waiting for path in going])
        try:
            left = du([path for _, row, *_ in waiting for path in row["paths"]])        # one measuring for all of them
        except OSError:
            left = None         # a Mac out of processes: what went has gone all the same, and is recorded
        for index, row, size, errors, going in waiting:
            JOB.update(index=index, current={"id": row["id"], "name": row["name"], "kb": size})
            # Each row hears about its own paths and no other's: the answers are kept by path.
            done(row, size, handed_over(row, errors, going, whys, left))
        waiting.clear()

    try:
        for index, row in enumerate(rows):
            if JOB["stopping"]:
                break
            size = row["kb"]
            if (row["evict"] or (not row["cmd"] and (row["trash"] or to_trash))) and not refusal(row, to_trash):
                errors, going = evict_plan(row) if row["evict"] else trash_plan(row, to_trash)
                if len(going) <= TRASH_BATCH:
                    if waiting and (waiting[0][1]["evict"] != row["evict"]
                                    or sum(len(g) for *_, g in waiting) + len(going) > TRASH_BATCH):
                        hand_over()
                    # A Stop asked for during that call is seen at the top of the next round, and
                    # at the end: a row that waits is only ever handed over while nobody said stop.
                    waiting.append((index, row, size, errors, going))
                    continue
            hand_over()         # in the order they were picked
            if JOB["stopping"]:
                break
            JOB.update(index=index, item_kb=0, current={"id": row["id"], "name": row["name"], "kb": size})
            finished = threading.Event()
            threading.Thread(target=watch_item, args=(size, free_kb(), finished), daemon=True).start()
            try:
                result = delete(row, to_trash)
            except OSError as error:
                result = {"freed_kb": 0, "left_kb": size, "errors": [sentence(error.strerror or error)], "note": ""}
            finally:
                finished.set()      # the watcher stops with the row, whatever became of it
            done(row, size, result)
        if not JOB["stopping"]:
            hand_over()
    finally:
        # Whatever went wrong above, the job ends: a job left "running" would turn every later
        # delete away until headroom is restarted.
        try:
            remember_bundles(bundled)
            forget_last({r["id"] for r in JOB["results"] if r["left_kb"] < MIN_KB})
            keep_found({r["id"] for r in JOB["results"]})
        finally:
            JOB.update(running=False, current=None)
            if mine:
                mine.close()


def remember_bundles(bundled):
    """The history line for the rows of a bundle that a job dealt with: one for all of them
    ("37 files named … in runs"), not one a file. A single row of a bundle is said by its own name."""
    for members in bundled.values():
        row, result, outcome = members[0]
        outcomes = {how for _, _, how in members}
        # A copy macOS has agreed to remove but not yet freed reads as the measured ones beside it do.
        outcomes = outcomes - {"asked"} or outcomes
        stayed = [m for m in members if m[1]["errors"]]
        outcome = "partly" if len(outcomes) > 1 or "partly" in outcomes else next(iter(outcomes))    # some went, or some of each
        why = stayed[0][1]["errors"][0] if stayed else ""
        if len(stayed) > 1:
            why = f"{len(stayed)} of them stayed. {why}"
        what = row["name"] if len(members) == 1 else f"{len(members)} {row['bundle']['noun']}"
        remember(what, row["group"], [path for member, _, _ in members for path in member["paths"]],
                 sum(r["freed_kb"] for _, r, _ in members), outcome, why)


# --- moving things to the Trash ------------------------------------------------------------------

# Finder moves each item and asks for an administrator password itself when one needs it. The
# paths travel as arguments, never inside the script text. One line comes back for each of them:
# 0 when it went, or Finder's error number.
TRASH_SCRIPT = """on run argv
set answers to ""
repeat with one in argv
try
set theItem to (POSIX file (contents of one)) as alias
tell application "Finder" to delete theItem
set answers to answers & "0" & linefeed
on error number code
set answers to answers & code & linefeed
end try
end repeat
return answers
end run"""
# The variable must not be called `target`: that is a word of Finder's own, and inside the tell
# Finder looks up its own "target" instead of the file (error -1728). Only the real Finder shows it.
TRASH_BATCH = 100       # paths handed to Finder in one go: one osascript for them, not one each
NO_FINDER = "Finder did not answer."
NOT_ALLOWED = ("headroom isn't allowed to control Finder. Allow it in System Settings, Privacy & Security, "
               "Automation, then try again.")
FINDER_SAYS = {"-128": "You cancelled, so it stayed.", "-1743": NOT_ALLOWED, "-43": "It was already gone.",
               "-1700": "It was already gone."}       # no file there to make an alias of


def finder_trash_many(paths):
    """Move files and folders to the Trash through Finder, all in one call. {path: '' when it
    went, else why not}. A link is never handed over: `as alias` follows it, and Finder would move
    what it points at, wherever that is. A call that fails as a whole says so for every path."""
    whys = {path: f"Left alone, it is a link and Finder would move what it points at: {tilde(path)}"
            for path in paths if os.path.islink(path)}
    going = [path for path in paths if path not in whys]
    if not going:
        return whys
    try:
        done = run(["/usr/bin/osascript", "-e", TRASH_SCRIPT] + going, timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        return {**whys, **dict.fromkeys(going, NO_FINDER)}
    answers = [line.strip() for line in done.stdout.split("\n") if line.strip()]
    if done.returncode or len(answers) != len(going):
        # Not one answer for each: nothing can be matched to its path, so none is taken as moved.
        why = next((text for code, text in FINDER_SAYS.items() if code in done.stderr), "")
        return {**whys, **dict.fromkeys(going, why or sentence(first_line(done.stderr) or "Finder could not move it"))}
    for path, code in zip(going, answers):
        whys[path] = "" if code == "0" else FINDER_SAYS.get(code, f"Finder could not move it (error {code}).")
    return whys


def finder_trash(path):
    """Move one file or folder to the Trash through Finder. '' when it went, else why not."""
    return finder_trash_many([path])[path]


def reveal_target(row, whole):
    """What "Show in Finder" may show for a row of the last scan: the row's own (first) path, or
    with `whole` the folder its bundle shares. Looked at again now: still there, inside the home
    folder and not reached through a link. None when there is nothing headroom would show."""
    if whole:
        folder = row["bundle"]["folder"] if row["bundle"] else ""
        inside = folder == HOME or folder.startswith(HOME + os.sep)
        return folder if inside and os.path.isdir(folder) and os.path.realpath(folder) == folder else None
    path = row["paths"][0] if row["paths"] else ""
    return path if path and os.path.lexists(path) and deletable(path) else None


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
            last, last_on = ago(opened, calendar=True), time.strftime("%-d %b %Y", time.localtime(opened))
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


DENIED = re.compile(r"^(?:du|headroom-size): (.+): (?:Permission denied|Operation not permitted)$", re.M)


def du_children(folder):
    """Each thing a folder holds with its size, the total, and the paths du was not let into."""
    inside = children(folder)
    folders = [p for p in inside if os.path.isdir(p) and not os.path.islink(p)]
    if SIZER and folders:
        said = []
        sizes = du(folders, said)       # 400 at a time, and du for what the measurer leaves out
        # The folder itself as one thing, so a file with names in two subfolders counts once.
        # ponytail: that walks it a second time; a mode in size.swift that prints a folder's
        # children and its total from one walk would halve this.
        total = du([folder]).get(folder)
        complaints = "\n".join(said)
    else:
        done = run(["/usr/bin/du", "-k", "-d", "1", "--", folder])     # folders; macOS du can't add -a here
        sizes, complaints = measured_lines(done.stdout, {folder, *inside}), done.stderr     # as in _du: no made-up lines
        total = sizes.pop(folder, None)
    for path in inside:                                             # files, from their own blocks
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            sizes[path] = info.st_blocks * 512 // 1024
    total = sum(sizes.values()) if total is None else total
    return sizes, total, list(dict.fromkeys(DENIED.findall(complaints)))


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
    ("local.headroom.door", "Lets you open headroom by typing headroom.localhost."),
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
            "alert_gb": gb if isinstance(gb, int) and 1 <= gb <= 1000 else 20,
            "trash": saved.get("trash") is True,     # cleaned files go to the Trash, not straight off: off unless chosen
            # Local copies of iCloud Drive files are offered: off unless chosen, and off when this can't be read.
            "cloud": saved.get("cloud") is True,
            # Safe clean runs by itself once a week: off unless chosen, and off when this can't be read.
            "auto": saved.get("auto") is True}


def settings_broken():
    """True when settings.json is there but can't be read as settings. Then nobody knows whether
    cleaned files were meant to go to the Trash, and a delete must not guess."""
    path = os.path.join(state_dir(), "settings.json")
    if not os.path.lexists(path):
        return False
    try:
        with open(path) as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return True
    return not isinstance(saved, dict) or not isinstance(saved.get("trash", False), bool)


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
    if not (isinstance(latest, str) and re.fullmatch(r"[0-9]+(\.[0-9]+)*", latest)):     # 0 to 9 only: it goes into an address
        latest = None
    newer = latest is not None and number(latest) > number(VERSION)
    return {"current": VERSION, "latest": latest, "newer": newer, "error": error,
            "url": f"{RELEASES}/tag/v{latest}" if newer else RELEASES,
            # "Update now" is offered only where it can be done safely: see update_offer().
            "can_update": bool(newer and update_offer())}


# --- Update now: the owner's click replaces this app with the newest release ---------------------------
#
# Never in the background: only the page's own POST starts it, with the full session. What makes it
# safe is one check, the signature: the app that comes down must satisfy the designated requirement
# of the app that is running (the same identifier, signed with the same certificate). The checksum
# before it only catches a broken download. Every step that fails leaves the installed app exactly
# as it was. Nothing a release says (a name, a version) reaches a shell: programs get argument
# lists, and the page gets text.

UPDATE_MAX = 60 * 1000 * 1000           # the zip is about 9 MB; anything this much bigger is not headroom
UNPACKED_MAX = 120 * 1000 * 1000        # nor is anything that unpacks to more than this (headroom is about 30 MB unpacked)
HTTPS = http.client.HTTPSConnection     # the tests put a stand-in here: no test reaches GitHub
move = os.rename                        # and here, to see the swap undone


class Refused(Exception):
    """Why an update stopped, in words for the page. Raised only while the installed app is as it was."""


def number(version):
    return tuple(map(int, version.split(".")))


def own_bundle():
    """The app bundle this program runs from (…/headroom.app). None from a plain folder: ./run.sh, the tests."""
    inside = os.path.realpath(os.path.dirname(sys.executable) if FROZEN else HERE)     # …/Contents/MacOS, or …/Contents/Resources
    contents = os.path.dirname(inside)
    app = os.path.dirname(contents)
    plain = os.path.basename(contents) == "Contents" and app.endswith(".app") and os.path.isdir(app) and not os.path.islink(app)
    return app if plain else None


def designated(app):
    """An app's designated requirement as codesign prints it ('identifier "local.headroom" and
    certificate leaf = H"…"'): what any later version must satisfy to count as the same app from
    the same signer. '' when codesign has none to give."""
    try:
        done = run(["/usr/bin/codesign", "-d", "-r-", app], timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    found = re.search(r"^(?:# )?designated => (.+)$", done.stdout, re.M) if done.returncode == 0 else None
    return found.group(1).strip() if found else ""


def update_offer():
    """(the app, its designated requirement, its bundle id) when this headroom can update itself,
    else None and the page keeps to the link to the Releases page. All of it must hold: it runs
    from an app bundle; that bundle is signed with a certificate, so there is a signer for the new
    one to be held to (an ad-hoc signature only names its own hash, which no other build has); and
    this account may rename things in the folder the app is in."""
    app = own_bundle()
    if not app or not os.access(os.path.dirname(app), os.W_OK | os.X_OK):
        return None
    requirement, ident = designated(app), bundle_info(app).get("CFBundleIdentifier")
    return (app, requirement, ident) if pins_signer(requirement) and isinstance(ident, str) and ident else None


def pins_signer(requirement):
    """True when a designated requirement holds its code to a signer: it has a certificate clause
    (certificate leaf, certificate root, certificate 1…) or an anchor, and no cdhash. Read with
    what is in quotes taken out: an identifier may be named anything, "local.certificate-test" too."""
    bare = re.sub(r'"[^"]*"', '""', requirement)
    return bool(re.search(r"\b(certificate\s+(leaf|root|\d+)|anchor)\b", bare)) and "cdhash" not in bare and "\n" not in requirement


def release_asset():
    """The zip for this Mac's chip, by what the hardware is and not by what this program was built
    for: an Intel build running under Rosetta moves to the native one."""
    try:
        arm = run(["/usr/sbin/sysctl", "-in", "hw.optional.arm64"], timeout=20).stdout.strip() == "1"
    except (OSError, subprocess.TimeoutExpired):
        arm = False
    return "headroom-apple-silicon.zip" if arm else "headroom-intel.zip"


def fetch(url, limit, into=None):
    """GET over HTTPS, from GitHub and nowhere else: the bytes, or with `into` a file written
    there. Each address on the way (the first, and every redirect) must be https on github.com or
    a name ending in .githubusercontent.com. Certificates are checked the default way. More than
    `limit` bytes, said or sent, is refused."""
    # The frozen app's Python has no certificates of its own; macOS keeps a bundle here.
    cafile = "/etc/ssl/cert.pem" if os.path.exists("/etc/ssl/cert.pem") else None
    deadline = time.monotonic() + 600
    for _ in range(6):
        parts = urlparse(url)
        host = parts.hostname or ""
        if (parts.scheme != "https" or not re.fullmatch(r"[a-z0-9.-]+", host)
                or not (host == "github.com" or host.endswith(".githubusercontent.com"))
                or parts.port not in (None, 443) or parts.username is not None or parts.password is not None):
            raise Refused("The download was sent somewhere that isn't GitHub over https, so it was stopped.")
        link = HTTPS(host, timeout=30, context=ssl.create_default_context(cafile=cafile))
        try:
            link.request("GET", (parts.path or "/") + ("?" + parts.query if parts.query else ""),
                         headers={"User-Agent": f"headroom/{VERSION}", "Accept": "application/octet-stream"})
            reply = link.getresponse()
            if reply.status in (301, 302, 303, 307, 308):
                url = urljoin(url, reply.getheader("Location") or "")
                continue
            if reply.status != 200:
                raise Refused(f"GitHub answered {reply.status} for the download.")
            said = reply.getheader("Content-Length") or ""
            if said.isdigit() and int(said) > limit:
                raise Refused("The download is far bigger than headroom is, so it was stopped.")
            got, out = 0, open(into, "wb") if into else None
            chunks = []
            try:
                while True:
                    chunk = reply.read(256 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > limit or time.monotonic() > deadline:
                        raise Refused("The download is far bigger than headroom is, so it was stopped." if got > limit
                                      else "The download took too long, so it was stopped.")
                    out.write(chunk) if out else chunks.append(chunk)
            finally:
                if out:
                    out.close()
            return None if into else b"".join(chunks)
        finally:
            link.close()
    raise Refused("The download was passed along too many times, so it was stopped.")


def unpack_release(archive, into):
    """Unpack the downloaded zip into the new folder `into` and return the app that came out,
    which must be exactly one real folder named headroom.app, not a link.

    The names are read first and an odd one is refused early, but that is only the zip's index:
    ditto unpacks from the entries themselves, which can say something else. What protects is
    this. ditto itself keeps what it writes inside `into`. The folder is measured while ditto
    runs, and ditto is stopped when it holds more than headroom could be. What came out is looked
    at, not what the zip said would. And nothing is installed without the signature check."""
    with zipfile.ZipFile(archive) as packed:
        total = 0
        for entry in packed.infolist():
            parts = entry.filename.rstrip("/").split("/")
            total += entry.file_size
            # Everything inside headroom.app. ditto also packs a "._name" beside what has extended attributes.
            inside = parts[0] == "headroom.app" or (parts == ["._headroom.app"] and not entry.is_dir())
            if entry.filename.startswith("/") or "\\" in entry.filename or not inside or any(p in ("", ".", "..") for p in parts):
                raise Refused("The download holds something other than headroom.app, so it was not unpacked.")
        if total > UNPACKED_MAX:
            raise Refused("The download unpacks to far more than headroom is, so it was not unpacked.")
    os.mkdir(into)

    def too_big():
        held = 0
        for folder, _, names in os.walk(into):
            for name in names:
                try:
                    held += os.lstat(os.path.join(folder, name)).st_size
                except OSError:
                    pass
            if held > UNPACKED_MAX:
                return True
        return False
    done = run(["/usr/bin/ditto", "-x", "-k", archive, into], timeout=600, watch=too_big)
    if done.returncode == -9 or too_big():
        raise Refused("The download unpacks to far more than headroom is, so it was stopped.")
    new = os.path.join(into, "headroom.app")
    if done.returncode or os.listdir(into) != ["headroom.app"] or os.path.islink(new) or not os.path.isdir(new):
        raise Refused("What came out of the download isn't a plain headroom.app, so it was not installed.")
    return new


def plain_modes(app):
    """Take away what the zip brought along beside the files themselves. ditto restores the modes
    and access lists a zip carries, and the signature seals contents, not those: a genuine build
    repacked with folders anyone may write to would pass it. So before the signature is checked,
    every access list goes, nobody but the owner may write to any of it, and nothing is setuid or
    setgid. A link's own mode is left alone, and no link is followed."""
    try:
        done = run(["/bin/chmod", "-RN", app], timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        done = None
    if done is None or done.returncode:
        raise Refused("The download's permissions could not be set right, so it was not installed.")
    try:
        for folder, _, names in os.walk(app):
            for path in [folder] + [os.path.join(folder, name) for name in names]:
                mode = os.lstat(path).st_mode
                if not stat.S_ISLNK(mode):
                    os.chmod(path, stat.S_IMODE(mode) & ~0o6022, follow_symlinks=False)
    except OSError:
        raise Refused("The download's permissions could not be set right, so it was not installed.")


def check_release(new, requirement, ident, latest):
    """THE security check. The unpacked app must satisfy the running app's own designated
    requirement: the same identifier, signed with the same certificate, every file in it sealed.
    Then: it is this app, it is the version the release says, and that version is newer than the
    one running. Never a step back, never sideways. Its modes and access lists are put right
    first (see plain_modes): what is checked here is what gets installed."""
    plain_modes(new)
    try:
        done = run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R=" + requirement, new], timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("macOS could not check the download's signature, so it was not installed.")
    if done.returncode:
        raise Refused("The download isn't signed the way this headroom is, so it was not installed.")
    info = bundle_info(new)
    version = info.get("CFBundleShortVersionString")
    if info.get("CFBundleIdentifier") != ident:
        raise Refused("The download isn't headroom, so it was not installed.")
    if not (isinstance(version, str) and re.fullmatch(r"\d+(\.\d+)*", version, re.A)) or version != latest:
        raise Refused(f"The download isn't version {latest}, so it was not installed.")
    if not number(version) > number(VERSION):
        raise Refused("The download is no newer than the headroom you have, so it was not installed.")


UPDATING = threading.Lock()


def update_now():
    """Replace this app with the newest release, because the owner pressed Update now: {ok, text,
    version, trashed}. Each step that fails stops it there, with the installed app exactly as it
    was and the reason in plain words. On success the new app is in its place, the old one is in
    the Trash (or the text says exactly where it is), and this server hands over to the new one."""
    global HELD_FOR_EXIT
    if JOB["running"]:
        return {"ok": False, "text": "A delete is running. Update when it is done."}
    if not UPDATING.acquire(blocking=False):
        return {"ok": False, "text": "An update is already under way."}
    made, handing_over = {}, False
    try:
        try:
            app, new, latest = fetch_release(made)
        except Refused as why:
            return {"ok": False, "text": str(why)}
        except Exception as error:      # a zip Python can't read, a network that drops: nothing has been swapped
            return {"ok": False, "text": f"The update did not finish: {sentence(getattr(error, 'strerror', None) or error)} headroom is as it was."}
        # The download took a while. One more look before anything moves: no delete may be running,
        # here or in headroom's other program. The lock is kept from here until this server stops.
        lock = job_lock(wait=False)
        if JOB["running"] or lock is None:
            if lock:
                lock.close()
            return {"ok": False, "text": "A delete started meanwhile. Update when it is done. headroom is as it was."}
        # The swap, by rename: the installed app steps aside into the scratch folder, the new one
        # takes its place. If the second rename fails, the first is undone.
        try:
            aside = os.path.join(made["scratch"], "previous")
            os.mkdir(aside)
            old = os.path.join(aside, os.path.basename(app))
            move(app, old)
        except OSError as error:
            lock.close()
            return {"ok": False, "text": f"The update did not finish: {sentence(error.strerror or error)} headroom is as it was."}
        try:
            move(new, app)
        except OSError as error:
            lock.close()
            try:
                move(old, app)
            except OSError:
                return {"ok": False, "text": f"The update stopped half-way, and headroom's app is now at {old}. "
                                             "Drag it back into the folder it was in."}
            return {"ok": False, "text": f"The new version could not be put in place: {sentence(error.strerror or error)} headroom is as it was."}
        where, trashed, said = retire(old, VERSION)
        remember(f"headroom {VERSION}, the version before {latest}", "update", [where], 0, "trashed" if trashed else "failed",
                 "" if trashed else said)
        # From here on this server only stops: the update lock and the job lock stay held, so no
        # delete can start in a server that is about to go.
        handing_over, HELD_FOR_EXIT = True, lock
        restart_into(app)
        return {"ok": True, "version": latest, "trashed": trashed, "text": said}
    finally:
        if made.get("scratch"):
            tidy_update(made["scratch"])
        if not handing_over:
            UPDATING.release()


HELD_FOR_EXIT = None        # the job lock, after a swap: kept until this server stops


def fetch_release(made):
    """Everything before the swap: is this headroom one that can update itself, what is the newest
    release, download it, check it. (the installed app, the new app unpacked and checked, its
    version). `made` gets the scratch folder as soon as there is one, for clearing away. Raises
    Refused, or whatever went wrong: nothing here has touched the installed app."""
    offer = update_offer()
    if not offer:
        raise Refused("This copy of headroom can't update itself. Get the new version from the Releases page.")
    app, requirement, ident = offer
    found = update_info(force=True)
    if not found["newer"]:
        raise Refused(found["error"] or "You have the newest version.")
    latest, asset = found["latest"], release_asset()
    # Beside the app, so the swap is a rename on one volume. mkdtemp makes it for this account only (0700).
    made["scratch"] = scratch = tempfile.mkdtemp(prefix=".headroom-update-", dir=os.path.dirname(app))
    archive = os.path.join(scratch, asset)
    try:
        fetch(f"{RELEASES}/download/v{latest}/{asset}", UPDATE_MAX, archive)
        # The checksum is published beside the zip, by whoever published the zip: it catches a
        # download that broke on the way and nothing else. The signature check below is the security.
        sums = fetch(f"{RELEASES}/download/v{latest}/SHA256SUMS", 64 * 1024).decode("ascii", "replace")
    except (OSError, http.client.HTTPException) as error:
        # Seen for real: a campus network that lets github.com through and resets every connection
        # to the server GitHub hands its downloads to. The Releases page fails there the same way.
        raise Refused(f"The download did not arrive: {sentence(getattr(error, 'strerror', None) or error)} Some networks block "
                      "GitHub's downloads; if it keeps happening, try on another network. headroom is as it was.")
    wanted = {name: digest for digest, name in re.findall(r"^([0-9a-f]{64})[ \t]+\*?(\S+)$", sums, re.M)}.get(asset)
    digest = hashlib.sha256()
    with open(archive, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if not wanted or digest.hexdigest() != wanted:
        raise Refused("The download didn't arrive whole (its checksum is wrong). Try again.")
    new = unpack_release(archive, os.path.join(scratch, "new"))
    check_release(new, requirement, ident, latest)
    return app, new, latest


def retire(old, version):
    """Put the app that was replaced where it can be found again: (where it is now, whether that
    is the Trash, what to tell the owner). The Trash is a folder: the app is renamed into it,
    under a name that is free. Not through Finder, as everything else of yours goes: the old app
    is still running at this moment (it is this server), and in a real update Finder held the
    request for two minutes and then refused it. If the rename can't be done the app stays where
    it is, and the words say exactly where. Never deleted."""
    why = ""
    try:
        os.makedirs(home(".Trash"), mode=0o700, exist_ok=True)
    except OSError:
        pass
    for n in range(1, 100):
        there = home(".Trash", f"headroom {version}.app" if n == 1 else f"headroom {version} {n}.app")
        if os.path.lexists(there):
            continue
        try:
            os.rename(old, there)
            return there, True, f"The version you had is in the Trash, as {os.path.basename(there)}."
        except OSError as error:
            # The Trash can't be looked into without Full Disk Access, so a name that is taken shows only here.
            if error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                continue
            why = sentence(error.strerror or error)
            break
    return old, False, f"The version you had could not be moved to the Trash. It is at {old}, and you can delete it yourself. {why}".strip()


def tidy_update(scratch):
    """Clear away what the update made: the zip and what was unpacked. Never the old app: if that
    is still in there (Finder did not take it), the folder stays, and the page has said where."""
    for name in ("headroom-apple-silicon.zip", "headroom-intel.zip"):
        try:
            os.remove(os.path.join(scratch, name))
        except OSError:
            pass
    shutil.rmtree(os.path.join(scratch, "new"), ignore_errors=True)
    for folder in (os.path.join(scratch, "previous"), scratch):
        try:
            os.rmdir(folder)        # only when empty
        except OSError:
            pass


def restart_into(app):
    """Hand over to the app that was just put in place, started the way it normally is, and stop
    this server. Started by the launch agent (the usual case), stopping is all it takes: the agent
    keeps the port, and the page's next request starts the app that is at that path now. Otherwise
    LaunchServices opens it, as a double click would. The page's address is never put on a command line."""
    def go():
        time.sleep(1.5)         # the page gets its answer first
        while JOB["running"]:   # none can be (the job lock is held), but a History line is never cut off
            time.sleep(0.2)
        if not os.environ.get("HEADROOM_LISTEN_FDS"):
            try:
                # -n: the launcher that started this server is still on its way out, and must not be taken for the app.
                os.posix_spawn("/usr/bin/open", ["/usr/bin/open", "-n", app], os.environ)
            except OSError:
                pass
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()


def launcher_program():
    """The app's own launcher, which can post notifications. None when headroom isn't running
    from an installed app (./run.sh, the tests, a build folder, a copy still in Downloads):
    then reminders are left alone, so a stray copy can't point the agent at itself."""
    for candidate in (os.path.join(HERE, "..", "MacOS", "headroom-app"),
                      os.path.join(os.path.dirname(sys.executable), "headroom-app")):
        if os.access(candidate, os.X_OK):
            program = os.path.realpath(candidate)
            # Installed: the app itself sits in an Applications folder, not somewhere below one. The
            # copy an update set aside lies in a folder beside the app, and must not count.
            app = os.path.dirname(os.path.dirname(os.path.dirname(program)))
            return program if os.path.dirname(app) in (APPLICATIONS, home("Applications")) else None
    return None


def sync_watch():
    """Install, update or remove the agent that runs the reminders, to match the settings."""
    program = launcher_program()
    if not program:
        return
    domain, path = f"gui/{os.getuid()}", home("Library", "LaunchAgents", WATCH_LABEL + ".plist")
    wanted_on = settings()["alert"] or settings()["weekly"] or settings()["auto"]
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


SAFE_SOURCES = ("caches", "app_data", "app_extras", "sandboxed", "logs", "temp", "browser_copies", "installers",
                "qbittorrent")
SAFE_WORTH = 500e6      # bytes. Under this the weekly reminder says nothing, and the weekly run does nothing


def measure_safe():
    """What Safe clean would free right now: the safe rows of the quick sources, as the page picks them."""
    running, total = processes(), 0
    for source in SAFE_SOURCES:
        found = SOURCES[source][1]()
        rows = found[0] if isinstance(found, tuple) else found
        total += sum(r["kb"] for r in unprotected(rows) if safe_row(r) and r["kb"] >= MIN_KB and not blocked(r, running))
    return total


def safe_rows(to_trash):
    """A scan made now of every place the weekly run's rows come from, and the rows it takes:
    (rows, how many places were left out). It is the page's own scan of each place (Never offer,
    the size floor, FOUND), so nothing here is an older listing, and the rows are picked by the
    button's own rule, less the installers (see safe_row), and less what an open app or the Move
    to Trash switch holds back. A place whose scan had anything to say (it was unsure, or macOS
    kept part of it shut) is left out whole. A place that can only hold what this run never takes
    isn't looked at."""
    running, rows, left_out = processes(), [], 0
    for source in SAFE_SOURCES:
        if not set(SOURCES[source][0]) & set(SAFE_GROUPS):
            continue                # installers: not this run's, so their folders aren't even listed
        notice = scan_once(source)["notice"]
        if notice or isinstance(notice, Unsure):        # an unsure scan with nothing to say is unsure all the same
            left_out += 1
            continue
        with LOCK:
            found = list(FOUND[source].values())
        rows += [row for row in found if safe_row(row, alone=True) and not blocked(row, running) and not (to_trash and stays_put(row))]
    return rows, left_out


BUSY_NOW = "busy"       # another job is running: the weekly run waits for the next check


def clean_alone(now, state):
    """The weekly Safe clean with nobody at the page: only with its switch on, at the moment the
    weekly reminder would have come. A (title, text) to post, None when there is nothing to say,
    or BUSY_NOW when another job is running and this should be tried again at the next check.

    Nothing here picks or removes on its own account. safe_rows() is the scan and the rule the
    button uses, and run_job() is the page's own removal with every guard it has: deletable() and
    trashable(), the `seen` checks, the Move to Trash switch. It touches no session: this runs in
    the reminder's half-hourly check, which has none.

    Whatever is in doubt stops it: settings or a Never offer list that can't be read, a date that
    can't be kept, a switch that moved while it was scanning."""
    last = state.get("auto_at")
    if isinstance(last, (int, float)) and not now - last >= 7 * DAY:
        return None             # never twice inside seven days, whatever became of the reminder's own date
    if settings_broken():
        return None             # nobody knows where cleaned files were meant to go: nothing is cleared on a guess
    if JOB["running"]:
        return BUSY_NOW
    lock = job_lock(wait=False)             # the page's server is another program: its jobs hold this
    if lock is None:
        return BUSY_NOW
    try:
        # With the lock in hand nothing else is clearing. What another check wrote while this one
        # was starting counts: two checks that overlap must not both run.
        saved = load_state("watch.json", {})
        other = saved.get("auto_at") if isinstance(saved, dict) else None
        if isinstance(other, (int, float)) and not now - other >= 7 * DAY:
            state["auto_at"] = other
            return None
        if protected_broken():
            # Read as empty, the list would hand over everything you asked to keep.
            remember("Safe clean, by itself", "auto", [], 0, "failed",
                     "It did not run this week: the Never offer list can't be read, and nothing is cleared on a guess at what you wanted kept.")
            return None
        rows, left_out = safe_rows(settings()["trash"])
        # The scan took a while. The switches count as they are now, not as they were when it began.
        chosen = settings()
        if settings_broken() or not chosen["auto"]:
            return None
        to_trash = chosen["trash"]
        rows = [row for row in rows if not (to_trash and stays_put(row))]
        if sum(row["kb"] for row in rows) * 1024 < SAFE_WORTH:
            return None         # under the reminder's own mark: nothing is done, and nothing is said
        state["auto_at"] = now
        if not save_state("watch.json", state):
            # The date can't be kept (a full disk, a folder that can't be written): run now, and
            # it would run again at every check. Before the first file goes, so nothing has.
            state.pop("auto_at") if last is None else state.update(auto_at=last)
            return None
        begin_job(rows, to_trash)
        run_job(rows, to_trash, held=lock)
        results = JOB["results"]
        freed, stayed = sum(r["freed_kb"] for r in results), [r for r in results if r["errors"]]
        said = [f"{count(len(results) - len(stayed), 'item', 'items')} cleared"]
        if stayed:
            said.append(f"{len(stayed)} could not be: {stayed[0]['errors'][0]}")
        if left_out:
            said.append(f"{count(left_out, 'place was', 'places were')} left out, because headroom could not look properly")
        remember("Safe clean, by itself", "auto", [], freed, "ran" if freed or not stayed else "failed", note=". ".join(said))
        if not freed:
            return "Safe clean ran and freed nothing", "headroom ran it by itself. History says why."
        if to_trash:
            return f"Safe clean moved {size_text(freed)} to the Trash", "headroom ran it by itself. Empty the Trash to get the space."
        return f"Safe clean freed {size_text(freed)}", "headroom ran it by itself. History has the list."
    except Exception as error:      # whatever it is, nobody is watching: it goes in History, and the week is over
        remember("Safe clean, by itself", "auto", [], 0, "failed",
                 f"It could not run this week: {sentence(getattr(error, 'strerror', None) or repr(error))}")
        return None
    finally:
        JOB.update(running=False, current=None)
        lock.close()


def watch_check(now=None):
    """The agent's half-hourly check: one free-space reading, and the notifications worth sending.
    It deletes nothing, with one exception that is off unless switched on: "Run Safe clean by
    itself once a week" (see clean_alone)."""
    now = now or clock()
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
    if chosen["weekly"] or chosen["auto"]:
        last = state.get("weekly_at")
        if not isinstance(last, (int, float)):
            state["weekly_at"] = now                     # the first week starts now
        elif now - last >= 7 * DAY and 10 <= time.localtime(now).tm_hour < 20:
            # With its switch on, the weekly run takes the reminder's place: there is nothing left to remind of.
            done = clean_alone(now, state) if chosen["auto"] else None
            if done == BUSY_NOW:
                # A delete is under way, or another check has this week in hand. Nothing is written:
                # what this check read a moment ago may be older than what that one has saved since.
                return notes
            state["weekly_at"] = now
            if done:
                notes.append(done)
            elif not chosen["auto"]:
                kb = measure_safe()
                if kb * 1024 >= SAFE_WORTH:
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
        if entry.get("result") not in ("deleted", "trashed", "compacted", "evicted", "partly") or not entry.get("kb"):     # "asked" has no size
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


# Time Machine keeps hourly snapshots of the startup disk on that disk itself for about a day, so a
# file can be restored without the backup disk. They count as used space until macOS clears them,
# which it does by itself when room runs short. Listing only reads. Removing asks tmutil, one
# snapshot at a time, by its date: the dates of the last listing and no others, and every one of
# them is digits and hyphens before it goes anywhere near a command. By date and not by disk, so
# a snapshot made after the listing can't go with them. tmutil removes a date's snapshots on every
# mounted disk, though, and only the startup disk is listed and counted here: the page says so.
SNAPSHOT = re.compile(r"com\.apple\.TimeMachine\.([0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6})\.local")
SNAPSHOT_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}")       # 0 to 9 only: \d would take any script's digits
SNAPSHOTS = []          # the dates the page was last shown: the only snapshots a removal names
# What tmutil prints when a command is for root only. Read from the strings inside /usr/bin/tmutil
# ("%s: %s requires root privileges." and "Must be run as root."), not seen live. Nothing else
# raises the password dialog: a snapshot that is busy stays, and tmutil's own words are shown.
NEEDS_ADMIN = re.compile(r"requires root privileges|Must be run as root")


def snapshot_time(date):
    """When a snapshot was made, from its date (2026-10-11-093012, this Mac's own time); None when it isn't one."""
    try:
        return int(time.mktime(time.strptime(date, "%Y-%m-%d-%H%M%S"))) if SNAPSHOT_DATE.fullmatch(date) else None
    except (ValueError, OverflowError):
        return None


def snapshot_dates():
    """The dates of Time Machine's local snapshots of the startup disk, oldest first. None when
    tmutil did not answer. Read-only: `tmutil listlocalsnapshots /`."""
    try:
        done = run(["/usr/bin/tmutil", "listlocalsnapshots", "/"], timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode:
        return None
    found = (SNAPSHOT.fullmatch(line.strip()) for line in done.stdout.split("\n"))
    return sorted({match.group(1) for match in found if match and snapshot_time(match.group(1)) is not None})


def list_snapshots():
    """For the Maintenance tab: how many there are and when the oldest was made. macOS doesn't say
    how big they are to anyone but root, so no size."""
    dates = snapshot_dates()
    SNAPSHOTS[:] = dates or []
    if dates is None:
        return {"count": 0, "oldest": 0, "error": "Time Machine did not answer."}
    return {"count": len(dates), "oldest": snapshot_time(dates[0]) if dates else 0, "error": ""}


def remove_snapshots():
    """Remove the local snapshots the page was last shown, if they are still there. tmutil is asked
    as you first; only when it says that takes an administrator does macOS's own password dialog
    come up, for a command made of nothing but tmutil's path and those dates. What went is counted
    by listing them again."""
    now = snapshot_dates()
    if now is None:
        return {"ok": False, "text": "Time Machine did not answer. Nothing changed."}
    asked = [date for date in now if date in SNAPSHOTS and SNAPSHOT_DATE.fullmatch(date)]
    if not asked:
        return {"ok": False, "text": "The snapshots that were listed are gone already."}
    said = []
    for date in asked:
        try:
            done = run(["/usr/bin/tmutil", "deletelocalsnapshots", date], timeout=300)
            said.append(done.stderr + done.stdout if done.returncode or "Failed" in done.stderr + done.stdout else "")
        except (OSError, subprocess.TimeoutExpired):
            said.append("Time Machine did not answer.")
    left = snapshot_dates()
    cancelled = False
    if left and any(date in left for date in asked) and any(NEEDS_ADMIN.search(words) for words in said):
        work = "; ".join(f"/usr/bin/tmutil deletelocalsnapshots {date}" for date in asked
                         if date in left and SNAPSHOT_DATE.fullmatch(date))
        try:
            done = run(["/usr/bin/osascript", "-e", f'do shell script "{work}" with administrator privileges'], timeout=900)
            cancelled = "-128" in done.stderr
            said = [done.stderr if done.returncode else ""]
        except (OSError, subprocess.TimeoutExpired):
            said = ["It did not finish."]
        left = snapshot_dates()
    if left is None:
        return {"ok": False, "text": "Time Machine did not answer afterwards, so headroom can't say what went."}
    stayed = [date for date in asked if date in left]
    gone = len(asked) - len(stayed)
    SNAPSHOTS[:] = left
    why = next((sentence(first_line(words)) for words in said if first_line(words)), "")
    if gone:
        remember(count(gone, "Time Machine local snapshot", "Time Machine local snapshots"), "snapshots", [], 0,
                 "partly" if stayed else "deleted", f"{len(stayed)} stayed. {why}".strip() if stayed else "")
    if not stayed:
        return {"ok": True, "text": f"Removed {count(gone, 'snapshot', 'snapshots')}. Free space can take a while to go up."}
    if cancelled:
        return {"ok": False, "text": "Cancelled. " + ("Nothing changed." if not gone else f"{count(gone, 'snapshot', 'snapshots')} went before that.")}
    return {"ok": False, "text": f"{'Removed ' + str(gone) + ' of ' + str(len(asked)) + '. ' if gone else ''}"
                                 f"macOS kept {count(len(stayed), 'snapshot', 'snapshots')}. {why}".strip()}


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
            chosen = settings()
            # iCloud Drive is not one of the places looked at unless its switch is on.
            sources = [{"id": name, "groups": list(groups), "slow": SLOW.get(name, "")}
                       for name, (groups, _) in SOURCES.items() if name != "cloud" or chosen["cloud"]]
            return self.reply(200, {"sources": sources, "disk": disk(), "last": last_scan(),
                                    "trash": chosen["trash"], "cloud": chosen["cloud"]})
        if url.path == "/api/progress":
            return self.reply(200, {**JOB, "disk": disk()})
        if url.path == "/api/blocked":
            # Asked when the page comes back into view: an app may have been quit meanwhile.
            # Or the Move to Trash switch was flipped, which changes what can be cleared.
            running, to_trash = processes(), settings()["trash"]
            with LOCK:
                rows = [r for found in FOUND.values() for r in found.values()]
            return self.reply(200, {r["id"]: blocked(r, running) or (stays_put(r) if to_trash else "") for r in rows})
        if url.path == "/api/scan":
            source = query.get("source", [""])[0]
            if source in SOURCES:
                try:
                    return self.reply(200, scan(source))
                except OSError as error:    # a Mac out of processes cannot even start `du`
                    return self.reply(500, {"error": sentence(error.strerror or error)})
        if url.path == "/api/protected":
            return self.reply(200, {"items": [{"id": e.get("id"), "name": e.get("name"), "detail": e.get("detail")}
                                              for e in protected()]})
        if url.path == "/api/history":
            return self.reply(200, {"items": history(), "file": tilde(history_file())})
        if url.path == "/api/apps":
            return self.reply(200, {"items": list_apps()})
        if url.path == "/api/explore":
            view, pid = query.get("view", ["places"])[0], query.get("id", [""])[0]
            try:
                found = explore_big() if view == "big" else explore_old() if view == "old" else explore_places(pid)
            except OSError as error:        # a folder too big for one command line, a Mac out of processes
                return self.reply(500, {"error": sentence(error.strerror or error)})
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
        if url.path == "/api/growth":
            return self.reply(200, growth())
        if url.path == "/api/snapshots":
            return self.reply(200, list_snapshots())
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
        if UPDATING.locked() and self.path not in ("/api/update", "/api/stop"):
            # A delete (or a move to the Trash, an uninstall, a changed switch) must not begin in a
            # server that is swapping its own app and about to stop.
            return self.reply(409, {"error": "headroom is updating itself. Try again in a moment."})
        wanted = data.get("id") if isinstance(data.get("id"), str) else None
        if self.path == "/api/delete":
            wanted = data.get("ids")
            if not isinstance(wanted, list) or not wanted or not all(isinstance(w, str) for w in wanted):
                return self.reply(400, {"error": "Send the ids of the rows to delete."})
            if settings_broken():
                # Guessing "off" would delete for good what was meant for the Trash.
                return self.reply(500, {"error": "headroom can't read its settings, so it doesn't know whether cleaned "
                                                 "files should go to the Trash. Set the switch under Maintenance "
                                                 "again, then try once more."})
            with LOCK:
                if JOB["running"]:
                    return self.reply(409, {"error": "A delete is already running."})
                known = {i: r for found in FOUND.values() for i, r in found.items()}
                if any(w not in known for w in wanted):
                    return self.reply(404, {"error": "Some rows are not in the last scan. Scan again."})
                rows = [known[w] for w in dict.fromkeys(wanted)]
                # Read once: the whole job goes one way. The page says what its question promised;
                # a promise of the Trash is kept even if the switch was turned off since, and a
                # switch that is on is never overruled.
                to_trash = settings()["trash"] or data.get("trash") is True
                begin_job(rows, to_trash)
            threading.Thread(target=run_job, args=(rows, to_trash), daemon=True).start()
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
            for key in ("alert", "weekly", "trash", "cloud", "auto"):
                # Where cleaned files go, and what may go, doesn't change under a delete that is running.
                if isinstance(data.get(key), bool) and not (key in ("trash", "cloud") and JOB["running"]):
                    current[key] = data[key]
            if not current["cloud"]:
                with LOCK:                  # switched off: its rows are not this launch's any more
                    FOUND.pop("cloud", None)
                CLOUD_ROOTS[:] = []
            if isinstance(data.get("alert_gb"), int) and 1 <= data["alert_gb"] <= 1000:
                current["alert_gb"] = data["alert_gb"]
            save_state("settings.json", current)
            try:
                sync_watch()
            except OSError as error:
                return self.reply(500, {"error": sentence(error.strerror or error)})
            return self.reply(200, {**settings(), "available": launcher_program() is not None})
        if self.path == "/api/snapshots":
            # No dates come from the page: the ones it was last shown are the ones that go.
            return self.reply(200, remove_snapshots())
        if self.path == "/api/update":
            # "Update now": the owner's click, with the full session like every call past this point.
            # Nothing comes from the page: which version, which file and from where are headroom's own.
            return self.reply(200, update_now())
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
            if target:
                run(["/usr/bin/open", "-R", target["path"]])
                return self.reply(200, {})
            # Or a row of the Clean list, by its id: its file, or the folder its bundle shares.
            with LOCK:
                row = next((found[wanted] for found in FOUND.values() if wanted in found), None)
            whole = data.get("bundle") is True
            path = reveal_target(row, whole) if row else None
            if not path:
                return self.reply(404, {"error": "That isn't in the last scan any more. Scan again."})
            run(["/usr/bin/open", path] if whole else ["/usr/bin/open", "-R", path])
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


def reserved_port():
    """The port the app's launcher keeps for headroom, which it writes to the `port` file beside
    the settings: 8460 unless something else had it. None when there is no such file, or it holds
    anything but one of headroom's own ports."""
    try:
        with open(os.path.join(state_dir(), "port")) as handle:
            text = handle.read(16).strip()
    except OSError:
        return None
    return int(text) if text.isascii() and text.isdigit() and 8460 <= int(text) <= 8479 else None


def listen():
    """One port on both loopback addresses: macOS sends headroom.localhost to ::1, and some
    setups to 127.0.0.1. Never the wildcard address, so nothing outside this Mac can connect.
    The port the launcher keeps for headroom is tried first, then the usual ones."""
    kept = reserved_port()
    for port in ([kept] if kept else []) + PORTS + [0] * 8:
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
