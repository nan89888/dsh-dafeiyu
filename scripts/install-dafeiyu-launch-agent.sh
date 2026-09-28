#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODE="${DAFEIYU_NODE:-$(command -v node || true)}"
if [[ -z "$NODE" ]]; then
  NODE="/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node"
fi
if [[ ! -x "$NODE" ]]; then
  NODE="/Users/zfq/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
fi
if [[ ! -x "$NODE" ]]; then
  echo "找不到 Node.js。请设置 DAFEIYU_NODE=/path/to/node" >&2
  exit 2
fi

mkdir -p "$HOME/Library/LaunchAgents"
PLIST="$HOME/Library/LaunchAgents/io.dafeiyu.companion.plist"
LOG_DIR="$HOME/.dsh/dafeiyu"
mkdir -p "$LOG_DIR"

python3 - "$PLIST" "$ROOT" "$NODE" "$LOG_DIR" <<'PY'
import plistlib, sys
plist, root, node, log_dir = sys.argv[1:]
data = {
    'Label': 'io.dafeiyu.companion',
    'ProgramArguments': [node, root + '/scripts/dafeiyu-companion.mjs'],
    'WorkingDirectory': root,
    'RunAtLoad': True,
    'KeepAlive': True,
    'ProcessType': 'Interactive',
    'StandardOutPath': log_dir + '/launchd.stdout.log',
    'StandardErrorPath': log_dir + '/launchd.stderr.log',
}
with open(plist, 'wb') as f:
    plistlib.dump(data, f)
PY

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/io.dafeiyu.companion"
echo "已安装大肥鱼生命周期观察器：$PLIST"
echo "ChatGPT.app 启动后显示桌宠，ChatGPT.app 退出后关闭桌宠。"
