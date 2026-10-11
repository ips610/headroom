#!/bin/sh
# Builds dist/headroom.zip: headroom.app with its own Python inside, for a Mac that has none.
# It uses the python.org Python (a universal build), so the app runs on Apple Silicon and Intel.
# For your own Mac, install.py is quicker and needs no build step.
set -eu
cd "$(dirname "$0")"
here=$(pwd)
py=${PY:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}

[ -x .venv-build/bin/pyinstaller ] || {
  "$py" -m venv .venv-build
  .venv-build/bin/pip install --quiet "pyinstaller>=6,<7"
}

.venv-build/bin/pyinstaller --noconfirm --clean --log-level WARN --windowed --name headroom \
  --target-arch universal2 --osx-bundle-identifier local.headroom --icon "$here/icon.icns" \
  --add-data "$here/ui.html:." --distpath dist --workpath build/work --specpath build cleaner.py

# The browser tab is the window, so no Dock icon of its own; then sign again after the change.
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" dist/headroom.app/Contents/Info.plist
codesign --force --deep --sign - dist/headroom.app

rm -f dist/headroom.zip
ditto -c -k --keepParent dist/headroom.app dist/headroom.zip
echo "Made: dist/headroom.zip ($(du -h dist/headroom.zip | cut -f1))"
