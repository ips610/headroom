#!/usr/bin/env python3
"""headroom: a cache cleaner for this Mac.

A page served only to this Mac that lists cache folders and deletes the ones
you tick. What can be deleted is decided here, by the scan: the page sends back
the ids of rows it was shown, never a path.
"""
import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
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
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = secrets.token_urlsafe(24)

MIN_KB = 1024              # a row under 1 MB is not worth a line
SMALL_KB = 5 * 1024        # cache folders under 5 MB share one row
BUILD_MIN_KB = 20 * 1024   # same idea for build folders inside projects
DAY = 24 * 60 * 60
IDLE = 5 * 60              # the server stops this long after the page goes quiet

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

# Folders a project can rebuild, each tied to the file that proves what kind of project it is.
PROJECT_DIRS = {
    "node_modules": "package.json", ".next": "package.json", "target": "Cargo.toml",
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


def count(number, one, many):
    return f"{number} {one if number == 1 else many}"


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
    shares = [s for s in (paths[i::4] for i in range(4)) if s]
    sizes = {}
    with ThreadPoolExecutor(4) as pool:
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
    }


def readable(folder_name):
    """com.colliderli.iina -> iina. A best guess; the row shows the real path underneath."""
    update = folder_name.endswith(".ShipIt")
    base = folder_name[:-len(".ShipIt")] if update else folder_name
    label = BUNDLES.get(base)
    if not label:
        label = base.rsplit(".", 1)[-1] if re.match(r"^[a-z]{2,6}\.[\w-]+\.[\w.-]+$", base) else base
    return f"{label} update download" if update else label


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


def scan_caches():
    return cache_rows(home("Library", "Caches"), "apps")


def scan_home_cache():
    return cache_rows(home(".cache"), "dev", skip=NOT_CACHE)


def scan_app_data():
    apps = {}
    for folder in children(home("Library", "Application Support")):
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


def scan_logs():
    logs = children(home("Library", "Logs"))
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
    sizes = du([p for _, paths, _ in table for p in paths])
    rows = []
    for name, paths, quit in table:
        present = [p for p in paths if p in sizes]
        if present:
            rows.append(item("dev", name, present, sum(sizes[p] for p in present),
                             tilde(os.path.commonpath(present)), quit=quit))
    return rows


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


def old_claude_versions():
    """Claude Code keeps every version it updates to. Keep the current one and any a session runs."""
    folder = home(".local", "share", "claude", "versions")
    current = os.path.realpath(home(".local", "bin", "claude"))
    running = {os.path.realpath(p) for p in program_paths()}
    return [p for p in children(folder) if os.path.realpath(p) != current and os.path.realpath(p) not in running]


def scan_simulators():
    # Straight to simctl: the xcrun shim in /usr/bin can stall for minutes on a full disk.
    developer = run(["/usr/bin/xcode-select", "-p"]).stdout.strip()
    simctl = os.path.join(developer, "usr", "bin", "simctl")
    if not os.path.isdir(home("Library", "Developer", "CoreSimulator", "Devices")) or not os.access(simctl, os.X_OK):
        return []
    detail = "For iOS versions that are no longer installed. The simulators you can still open are kept."
    try:
        # The simulator service can take minutes to answer when there are a lot of devices.
        listed = run([simctl, "list", "devices", "-j"], timeout=600)
        devices = json.loads(listed.stdout)["devices"]
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return [item("dev", "Simulators that no longer open", [], 0, detail,
                     blocked="Xcode's simulator service did not answer. Scan again in a minute.")]
    folder = home("Library", "Developer", "CoreSimulator", "Devices")
    dead = [d["udid"] for group in devices.values() for d in group if not d.get("isAvailable")]
    live = [(runtime, d) for runtime, group in devices.items() for d in group if d.get("isAvailable")]
    sizes = du([os.path.join(folder, udid) for udid in dead] + [os.path.join(folder, d["udid"]) for _, d in live])
    rows = []
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
            blocked="Shut this simulator down first" if device.get("state") == "Booted" else "",
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


def project_label(folder):
    """A project named by its path from the repository it sits in, so two folders called
    `web` do not read the same."""
    here = folder
    while len(here) > len(HOME):
        if os.path.lexists(os.path.join(here, ".git")):
            return os.path.relpath(folder, os.path.dirname(here))
        here = os.path.dirname(here)
    return os.path.basename(folder)


def scan_builds():
    shut = []
    for name in ("Desktop", "Documents", "Downloads"):
        try:
            os.listdir(home(name))
        except FileNotFoundError:
            pass
        except OSError:
            shut.append(name)
    rows = []
    for path, kb in du(find_build_folders()).items():
        if kb >= BUILD_MIN_KB:
            name = f"{project_label(os.path.dirname(path))} / {os.path.basename(path)}"
            rows.append(item("builds", name, [path], kb, tilde(path)))
    notice = ""
    if shut:
        notice = (f"macOS did not let headroom look inside {', '.join(shut)}. Allow it in System Settings, "
                  "under Privacy & Security, Files and Folders, then scan again.")
    return rows, notice


# source -> (the sections its rows land in, the scan)
SOURCES = {
    "caches": (("apps", "dev"), scan_caches),
    "app_data": (("apps",), scan_app_data),
    "logs": (("temp",), scan_logs),
    "temp": (("temp",), scan_temp),
    "dev_tools": (("dev",), scan_dev_tools),
    "home_cache": (("dev",), scan_home_cache),
    "simulators": (("dev", "big"), scan_simulators),
    "docker": (("dev", "big"), scan_docker),
    "builds": (("builds",), scan_builds),
    "big": (("big",), scan_big),
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
            "kb": row["kb"], "blocked": blocked(row, running)}


def scan(source):
    found = SOURCES[source][1]()
    rows, notice = found if isinstance(found, tuple) else (found, "")
    rows = [r for r in rows if r["kb"] >= MIN_KB or r["blocked"]]
    with LOCK:
        FOUND[source] = {r["id"]: r for r in rows}
    running = processes()
    return {"items": [public(r, running) for r in rows], "notice": notice}


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
            done = run(["/bin/rm", "-rf", "--"] + allowed[start:start + 200])
            if done.returncode:
                errors.append(first_line(done.stderr) or "Some files could not be removed.")
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
        JOB["results"].append({"id": row["id"], "name": row["name"], **result})
        JOB.update(done_kb=JOB["done_kb"] + size, item_kb=0)
    JOB.update(running=False, current=None)


BUSY = 0
DEADLINE = time.time() + IDLE


def watchdog():
    """Stop once the page has gone and nothing is being deleted: nobody is left to press anything."""
    while True:
        time.sleep(2)
        if BUSY == 0 and not JOB["running"] and time.time() > DEADLINE:
            os._exit(0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "img-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        # A page on another site that points its own name at 127.0.0.1 still sends its own Host.
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def trusted(self):
        """Only the page this launch opened may use the API: no other site or local page knows the token."""
        return self.local() and secrets.compare_digest(self.headers.get("X-Token", "").encode(), TOKEN.encode())

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
        if url.path == "/" and self.local():
            DEADLINE = time.time() + IDLE
            with open(os.path.join(HERE, "ui.html"), "rb") as page:
                return self.reply(200, page.read(), "text/html; charset=utf-8")
        if url.path == "/favicon.svg" and self.local():
            return self.reply(200, ICON.encode(), "image/svg+xml")
        if not self.trusted():
            return self.reply(403, {"error": "forbidden"})
        DEADLINE = time.time() + IDLE
        if url.path == "/api/start":
            sources = [{"id": name, "groups": list(groups)} for name, (groups, _) in SOURCES.items()]
            return self.reply(200, {"sources": sources, "disk": disk()})
        if url.path == "/api/progress":
            return self.reply(200, {**JOB, "disk": disk()})
        if url.path == "/api/blocked":
            # Asked when the page comes back into view: an app may have been quit meanwhile.
            running = processes()
            with LOCK:
                rows = [r for found in FOUND.values() for r in found.values()]
            return self.reply(200, {r["id"]: blocked(r, running) for r in rows})
        if url.path == "/api/scan":
            source = parse_qs(url.query).get("source", [""])[0]
            if source in SOURCES:
                try:
                    return self.reply(200, scan(source))
                except OSError as error:    # a Mac out of processes cannot even start `du`
                    return self.reply(500, {"error": error.strerror or str(error)})
        if url.path == "/api/ping":
            return self.reply(200, {})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        global DEADLINE
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/bye" and self.local():
            # Sent as the tab closes, where a custom header is not possible. A reload sends it
            # too, so wait a moment for the page to come back before stopping.
            if secrets.compare_digest(body, TOKEN.encode()):
                DEADLINE = time.time() + 4
            return self.reply(200, {})
        if not self.trusted():
            return self.reply(403, {"error": "forbidden"})
        DEADLINE = time.time() + IDLE
        if self.path == "/api/delete":
            try:
                wanted = json.loads(body).get("ids")
            except (ValueError, AttributeError):
                wanted = None
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
        self.reply(404, {"error": "not found"})


class Server(ThreadingHTTPServer):
    # The page asks for every section at once; the default backlog of 5 makes the browser's
    # sixth and later requests fail with a connection reset.
    request_queue_size = 64


def main():
    if "--detach" in sys.argv:
        # Started by the app in ~/Applications. Hand over to a copy that outlives the launcher,
        # so the app counts as closed and can be opened again while a tab is still up.
        log = os.open(home("Library", "Logs", "headroom.log"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.posix_spawn(sys.executable, [sys.executable, os.path.abspath(__file__)], os.environ, setsid=True,
                       file_actions=[(os.POSIX_SPAWN_DUP2, log, 1), (os.POSIX_SPAWN_DUP2, log, 2)])
        return
    server = Server(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/#{TOKEN}"
    print("headroom is open at", url, flush=True)
    threading.Thread(target=watchdog, daemon=True).start()
    if "--no-open" not in sys.argv:
        run(["/usr/bin/open", url])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
