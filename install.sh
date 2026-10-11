#!/bin/sh
# Builds "headroom.app" in ~/Applications. The work is in install.py.
exec python3 "${0%/*}/install.py"
