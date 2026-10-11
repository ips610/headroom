#!/bin/sh
# Compiles the Dock launcher: launcher/build.sh <arm64|x86_64> <output file>
# Needs Xcode or the Command Line Tools. The compiler is called directly, because the xcrun
# shims in /usr/bin can stall for minutes on this kind of Mac.
set -eu
arch=$1 out=$2
dev=$(xcode-select -p)
swiftc=$(ls "$dev/Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc" "$dev/usr/bin/swiftc" 2>/dev/null | head -1)
sdk=$(ls -d "$dev/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk" "$dev/SDKs/MacOSX.sdk" 2>/dev/null | head -1)
[ -n "$swiftc" ] && [ -n "$sdk" ] || { echo "No Swift compiler found" >&2; exit 1; }
mkdir -p "$(dirname "$out")"
"$swiftc" -O -sdk "$sdk" -target "$arch-apple-macos12" -o "$out" "$(dirname "$0")/main.swift"
