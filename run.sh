#!/bin/sh
# Opens headroom in the browser. The app in /Applications (see install.py) does the same.
exec python3 "${0%/*}/cleaner.py" "$@"
