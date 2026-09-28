#!/usr/bin/env bash
set -euo pipefail

# Build the one macOS entry point used by Finder and by the ChatGPT
# supervisor. The old Swift helper is intentionally not built or copied.
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
APP="$ROOT/runtime/bin/darwin/DSH.app"
LAUNCHER_BIN="$APP/Contents/MacOS/DSH"
NODE_BIN="${DAFEIYU_NODE:-$(command -v node || true)}"
if [[ -z "$NODE_BIN" && -x "/Users/zfq/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node" ]]; then
  NODE_BIN="/Users/zfq/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
fi
if [[ -z "$NODE_BIN" || ! -x "$NODE_BIN" ]]; then
  echo "node is required to read package version; set DAFEIYU_NODE=/path/to/node" >&2
  exit 2
fi
PACKAGE_VERSION="$($NODE_BIN -p "require('$ROOT/package.json').version")"
command -v clang >/dev/null 2>&1 || { echo "clang is required" >&2; exit 2; }
# Build the launcher and AppKit bridge as one universal artifact. The Python
# visual runtime remains architecture-neutral, while the native entry point
# must work on both Apple Silicon and Intel release runners.
CLANG_ARCH_FLAGS=(-arch arm64 -arch x86_64 -mmacosx-version-min=12.0)

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/runtime" \
  "$APP/Contents/Resources/assets" "$APP/Contents/Resources/DSH.iconset"
cp "$DIR/Info.plist" "$APP/Contents/Info.plist"
for command in \
  "Set :CFBundleDisplayName DSH" \
  "Set :CFBundleName DSH" \
  "Set :CFBundleExecutable DSH" \
  "Set :CFBundleIdentifier io.github.qcytsn.dsh" \
  "Set :CFBundleVersion $PACKAGE_VERSION" \
  "Set :CFBundleShortVersionString $PACKAGE_VERSION"; do
  /usr/libexec/PlistBuddy -c "$command" "$APP/Contents/Info.plist"
done

# The helper imports these modules directly from its runtime directory.
for file in __init__.py activity_director.py animation_model.py asset_paths.py helper.py layout_store.py; do
  cp "$ROOT/runtime/$file" "$APP/Contents/Resources/runtime/$file"
done
cp -R "$ROOT/assets/." "$APP/Contents/Resources/assets/"
cp "$ROOT/scripts/macos-python-helper.sh" "$APP/Contents/Resources/dsh-dafeiyu-helper"
chmod 0755 "$APP/Contents/Resources/dsh-dafeiyu-helper"
# A compiled AppKit bridge owns the actual NSWindow level transition. This is
# safer than calling objc_msgSend directly from Python and keeps topmost mode
# working across Space/full-screen changes on macOS.
clang "${CLANG_ARCH_FLAGS[@]}" -dynamiclib -framework AppKit "$DIR/WindowLevelBridge.m" \
  -o "$APP/Contents/Resources/libdsh-window-level.dylib"
# Use the checked-out virtual environment without embedding its 1 GB runtime.
printf '%s\n' "$ROOT" > "$APP/Contents/Resources/source-root.txt"

ICON_SOURCE="$ROOT/legacy/dafeiyu/idle_front/idle_front_238.png"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$ICON_SOURCE" \
    --out "$APP/Contents/Resources/DSH.iconset/icon_${size}x${size}.png" >/dev/null
done
sips -z 32 32 "$ICON_SOURCE" --out "$APP/Contents/Resources/DSH.iconset/icon_16x16@2x.png" >/dev/null
sips -z 64 64 "$ICON_SOURCE" --out "$APP/Contents/Resources/DSH.iconset/icon_32x32@2x.png" >/dev/null
sips -z 256 256 "$ICON_SOURCE" --out "$APP/Contents/Resources/DSH.iconset/icon_128x128@2x.png" >/dev/null
sips -z 512 512 "$ICON_SOURCE" --out "$APP/Contents/Resources/DSH.iconset/icon_256x256@2x.png" >/dev/null
sips -z 1024 1024 "$ICON_SOURCE" --out "$APP/Contents/Resources/DSH.iconset/icon_512x512@2x.png" >/dev/null
iconutil -c icns "$APP/Contents/Resources/DSH.iconset" -o "$APP/Contents/Resources/DSH.icns"
rm -rf "$APP/Contents/Resources/DSH.iconset"
/usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string DSH.icns" "$APP/Contents/Info.plist"

clang "${CLANG_ARCH_FLAGS[@]}" -framework AppKit "$DIR/Launcher.m" -o "$LAUNCHER_BIN"
chmod 0755 "$LAUNCHER_BIN"
codesign --force --deep --sign - --timestamp=none "$APP"
plutil -lint "$APP/Contents/Info.plist"
codesign --verify --deep --strict --verbose=2 "$APP"
echo "built single-source macOS DSH.app: $APP"
