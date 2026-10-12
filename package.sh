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
version=$(sed -n 's/^VERSION = "\([^"]*\)"/\1/p' cleaner.py)
sign_id=${SIGN_ID:-IPS Local Apps}

# A release is what's in git: nothing uncommitted ends up in a zip.
[ -z "$(git status --porcelain)" ] || {
  echo "There are uncommitted changes. Commit or stash them, then build." >&2
  exit 1
}
# Same certificate every release, so friends' Macs keep headroom's permissions across updates.
# SIGN_ID picks another one (a Developer ID later). If it isn't in the keychain the build stops
# here, not after quietly signing ad-hoc; ALLOW_ADHOC=1 builds with a throwaway signature anyway.
security find-identity -p codesigning | grep -qF -e "\"$sign_id\"" -e ") $sign_id " || {
  [ "${ALLOW_ADHOC:-}" = 1 ] || {
    echo "No \"$sign_id\" certificate in the keychain. Set SIGN_ID, or ALLOW_ADHOC=1 to sign ad-hoc." >&2
    exit 1
  }
  sign_id=-
}

# A new build environment every time, from requirements-build.txt: exact versions, and pip
# refuses any download that isn't the file hashed there.
rm -rf .venv-build
"$py" -m venv .venv-build
.venv-build/bin/pip install --quiet --require-hashes --only-binary :all: -r requirements-build.txt

build() {
  arch=$1 zip=$2
  .venv-build/bin/pyinstaller --noconfirm --clean --log-level WARN --windowed --name headroom \
    --target-arch "$arch" --osx-bundle-identifier local.headroom --icon "$here/icon.icns" \
    --add-data "$here/ui.html:." --add-data "$here/favicon-32.png:." --add-data "$here/apple-touch-icon.png:." \
    --distpath "build/dist-$arch" --workpath "build/work-$arch" \
    --specpath build cleaner.py
  app="build/dist-$arch/headroom.app"
  # The Dock launcher becomes the app's main program and starts the frozen server beside it,
  # which gives headroom a Dock icon and a Quit. Same version as install.py. The same script
  # builds headroom-size beside it, for this chip: the folder measurer the scans use. And
  # headroom-cloud, which removes the local copies of iCloud Drive files.
  launcher/build.sh "$arch" "$app/Contents/MacOS/headroom-app"
  for helper in headroom-size headroom-cloud; do
    [ -x "$app/Contents/MacOS/$helper" ] || { echo "$helper was not built for $arch" >&2; exit 1; }
  done
  /usr/libexec/PlistBuddy -c "Set :CFBundleExecutable headroom-app" \
    -c "Set :CFBundleShortVersionString $version" "$app/Contents/Info.plist"
  codesign --force --deep --sign "$sign_id" "$app"     # checked at the top; a failure here stops the build
  rm -f "dist/$zip"
  ditto -c -k --keepParent "$app" "dist/$zip"
  echo "Made: dist/$zip ($(du -h "dist/$zip" | cut -f1))"
}

mkdir -p dist
rm -rf dist/headroom.app dist/headroom.zip     # the old universal build
build arm64 headroom-apple-silicon.zip
build x86_64 headroom-intel.zip
# Goes on the Releases page beside the zips, so a download can be checked against it.
(cd dist && shasum -a 256 headroom-apple-silicon.zip headroom-intel.zip > SHA256SUMS)
echo "Made: dist/SHA256SUMS"
