#!/usr/bin/env python3
"""Builds "headroom.app" in /Applications, so it opens from Finder, Spotlight or Launchpad.

Run it again after changing cleaner.py or ui.html: the app holds its own copies.
"""
import os
import platform
import plistlib
import re
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "headroom"
with open(os.path.join(HERE, "cleaner.py")) as source:
    VERSION = re.search(r'^VERSION = "([^"]+)"', source.read(), re.M).group(1)

# Used only when there is no Swift compiler for the Dock launcher in launcher/.
LAUNCHER = """#!/bin/sh
# Shell built-ins and exec only, nothing that forks: a Mac that is out of memory cannot fork,
# and that is when this app gets opened. Finder also starts apps with a bare PATH, and
# /usr/bin/python3 is an Xcode shim that can stall for minutes, so a real Python is tried first.
res="${0%/*}/../Resources"
for py in /opt/homebrew/bin/python3 /usr/local/bin/python3 \\
          /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
  [ -x "$py" ] && exec "$py" "$res/cleaner.py" --detach
done
exec /usr/bin/osascript -e 'display alert "headroom needs Python 3" message "Install it with: brew install python"'
"""

INFO = {
    "CFBundleName": NAME,
    "CFBundleDisplayName": NAME,
    "CFBundleIdentifier": "local.headroom",
    "CFBundleExecutable": "headroom",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": VERSION,
    "CFBundleIconFile": "icon",
}


def spawn(*command):
    """Run a tool without forking (see the launcher above); True when it succeeded."""
    try:
        _, status = os.waitpid(os.posix_spawn(command[0], list(command), os.environ), 0)
    except OSError:
        return False
    return status == 0


def build(app):
    contents = os.path.join(app, "Contents")
    shutil.rmtree(app, ignore_errors=True)
    os.makedirs(os.path.join(contents, "MacOS"))
    os.makedirs(os.path.join(contents, "Resources"))
    for name in ("cleaner.py", "ui.html", "icon.icns", "favicon-32.png", "apple-touch-icon.png"):
        shutil.copy(os.path.join(HERE, name), os.path.join(contents, "Resources"))
    info = dict(INFO)
    # The Dock launcher gives headroom a Dock icon and a Quit; it is compiled for this Mac's chip.
    shell = os.path.join(contents, "MacOS", "headroom-app")
    with tempfile.TemporaryDirectory() as scratch:
        built = os.path.join(scratch, "headroom-app")
        if spawn("/bin/sh", os.path.join(HERE, "launcher", "build.sh"), platform.machine(), built):
            shutil.copy(built, shell)
            info["CFBundleExecutable"] = "headroom-app"
    if not os.path.exists(shell):
        launcher = os.path.join(contents, "MacOS", "headroom")
        with open(launcher, "w") as script:
            script.write(LAUNCHER)
        os.chmod(launcher, 0o755)
        info["LSUIElement"] = True      # a shell script cannot hold a Dock icon, so show none
    with open(os.path.join(contents, "Info.plist"), "wb") as plist:
        plistlib.dump(info, plist)
    # macOS remembers permissions (Full Disk Access, notifications) against the signature. Signing
    # with the same certificate every time keeps them across rebuilds; an ad-hoc signature changes
    # with every build, so macOS asks again. No certificate on this Mac → ad-hoc, which still runs.
    sign_id = os.environ.get("SIGN_ID", "IPS Local Apps")
    return (spawn("/usr/bin/codesign", "--force", "--sign", sign_id, app)
            or spawn("/usr/bin/codesign", "--force", "--sign", "-", app))


def main():
    # /Applications is where Finder's Applications folder looks. Admin accounts can write there;
    # anyone else gets the personal ~/Applications, which Spotlight still finds.
    folder = "/Applications" if os.access("/Applications", os.W_OK) else os.path.expanduser("~/Applications")
    app = os.path.join(folder, f"{NAME}.app")
    signed = build(app)
    old = os.path.expanduser(f"~/Applications/{NAME}.app")
    if folder == "/Applications" and os.path.isdir(old):
        shutil.rmtree(old)      # an earlier install went there; one copy is enough
    print("Installed:", app, "" if signed else "(not signed)")


if __name__ == "__main__":
    main()
