#!/bin/bash
set -eu

# Single macOS launcher for the feature-complete PySide helper. There is no
# second Swift UI implementation: both DSH.app and the Codex supervisor use
# this same runtime so they cannot silently fall back to the old layout.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/../runtime/helper.py" ]; then
  ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
  RUNTIME_ROOT="$ROOT/runtime"
  ASSET_ROOT="$ROOT/assets"
elif [ -f "$SCRIPT_DIR/runtime/helper.py" ]; then
  ROOT="${DSH_DAFEIYU_ROOT:-}"
  if [ -z "$ROOT" ] && [ -f "$SCRIPT_DIR/source-root.txt" ]; then
    ROOT="$(cat "$SCRIPT_DIR/source-root.txt")"
  fi
  ROOT="${ROOT:-$SCRIPT_DIR}"
  RUNTIME_ROOT="$SCRIPT_DIR/runtime"
  ASSET_ROOT="$SCRIPT_DIR/assets"
else
  echo "DSH runtime/helper.py was not found" >&2
  exit 2
fi
PYTHON="${DSH_DAFEIYU_PYTHON:-}"
if [ -z "$PYTHON" ] && [ -x "$ROOT/.build/python-env/bin/python" ]; then
  PYTHON="$ROOT/.build/python-env/bin/python"
fi
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3)"
fi
export DSH_DAFEIYU_ASSET_ROOT="$ASSET_ROOT"
# Keep the visible macOS process/application name aligned with the bundle.
# Without an explicit argv[0], the menu bar falls back to "Python" even
# though the helper has already set QApplication's display name to DSH.
exec -a DSH "$PYTHON" "$RUNTIME_ROOT/helper.py" "$@"
