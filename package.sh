#!/bin/sh
# Builds two zips of headroom.app with its own Python inside, for Macs that have no Python:
#   dist/headroom-apple-silicon.zip   M1 and newer
#   dist/headroom-intel.zip           Intel Macs
# One build per chip on purpose: a universal app makes macOS 27 warn that it "will not open in
# macOS 28" if the Intel half ever runs under Rosetta. A single-chip app never needs Rosetta.
# Needs the python.org Python (a universal build, so it can target either chip).
set -eu
cd "$(dirname "$0")"
here=$(pwd)
py=${PY:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}
version=$(sed -n 's/.*"CFBundleShortVersionString": "\([^"]*\)".*/\1/p' install.py)

[ -x .venv-build/bin/pyinstaller ] || {
  "$py" -m venv .venv-build
  .venv-build/bin/pip install --quiet "pyinstaller>=6,<7"
}

build() {
  arch=$1 zip=$2
  .venv-build/bin/pyinstaller --noconfirm --clean --log-level WARN --windowed --name headroom \
    --target-arch "$arch" --osx-bundle-identifier local.headroom --icon "$here/icon.icns" \
    --add-data "$here/ui.html:." --distpath "build/dist-$arch" --workpath "build/work-$arch" \
    --specpath build cleaner.py
  app="build/dist-$arch/headroom.app"
  # The browser tab is the window, so no Dock icon of its own; same version as install.py.
  /usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" \
    -c "Set :CFBundleShortVersionString $version" "$app/Contents/Info.plist"
  codesign --force --deep --sign - "$app"
  rm -f "dist/$zip"
  ditto -c -k --keepParent "$app" "dist/$zip"
  echo "Made: dist/$zip ($(du -h "dist/$zip" | cut -f1))"
}

mkdir -p dist
rm -rf dist/headroom.app dist/headroom.zip     # the old universal build
build arm64 headroom-apple-silicon.zip
build x86_64 headroom-intel.zip
