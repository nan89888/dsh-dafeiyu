#!/usr/bin/env bash
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/io.dafeiyu.companion.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "已停止并移除大肥鱼登录自启。"
