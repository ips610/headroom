#!/usr/bin/env python3
"""Builds "headroom.app" in ~/Applications, so it opens from Spotlight or Launchpad.

Run it again after changing cleaner.py or ui.html: the app holds its own copies.
"""
import os
import plistlib
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "headroom"

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
    "CFBundleShortVersionString": "1.1",
    "LSUIElement": True,    # the browser tab is the window, so no Dock icon of its own
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
    for name in ("cleaner.py", "ui.html"):
        shutil.copy(os.path.join(HERE, name), os.path.join(contents, "Resources"))
    with open(os.path.join(contents, "Info.plist"), "wb") as plist:
        plistlib.dump(INFO, plist)
    launcher = os.path.join(contents, "MacOS", "headroom")
    with open(launcher, "w") as script:
        script.write(LAUNCHER)
    os.chmod(launcher, 0o755)
    # An ad-hoc signature gives macOS one stable name to remember folder permissions against.
    # The app runs without it, so a failure here is not worth stopping for.
    return spawn("/usr/bin/codesign", "--force", "--sign", "-", app)


def main():
    app = os.path.expanduser(f"~/Applications/{NAME}.app")
    signed = build(app)
    print("Installed:", app, "" if signed else "(not signed)")


if __name__ == "__main__":
    main()
