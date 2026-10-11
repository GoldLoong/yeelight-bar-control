#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/iina-plugin"
PLUGINS_DIR="$HOME/Library/Application Support/com.colliderli.iina/plugins"
DEST="$PLUGINS_DIR/com.yeelightbar.sync.iinaplugin"
IINA_PLUGIN_BIN="/Applications/IINA.app/Contents/MacOS/iina-plugin"

mkdir -p "$PLUGINS_DIR"

# 清理旧的开发 symlink / 拷贝（避免中文路径 symlink 加载失败）
rm -f "$PLUGINS_DIR/iina-plugin.iinaplugin-dev" 2>/dev/null || true
if [[ -x "$IINA_PLUGIN_BIN" ]]; then
  "$IINA_PLUGIN_BIN" unlink "$SRC" 2>/dev/null || true
fi
rm -rf "$DEST"

# 复制到纯 ASCII 路径（正式 .iinaplugin 包）
mkdir -p "$DEST"
cp -R "$SRC/." "$DEST/"

defaults write com.colliderli.iina "PluginEnabled.com.yeelightbar.sync" -bool true

# 确保在插件顺序里
python3 - <<'PY'
import plistlib, pathlib, subprocess, tempfile, os
# defaults 已写 enabled；PluginOrder 用 defaults read/write
out = subprocess.check_output(["defaults", "read", "com.colliderli.iina", "PluginOrder"], text=True)
# 粗解析：若已包含则跳过
if "com.yeelightbar.sync" not in out:
    subprocess.run(
        ["defaults", "write", "com.colliderli.iina", "PluginOrder", "-array-add", "com.yeelightbar.sync"],
        check=False,
    )
PY

echo "已安装到: $DEST"
echo
echo "请现在执行："
echo "  1) 完全退出 IINA（Cmd+Q）"
echo "  2) 重新打开 IINA，播放任意视频"
echo "  3) 看挂灯 App 是否显示「插件」"
echo
echo "自检：重启 IINA 后应出现文件 /tmp/yeelight-iina-plugin.mark"
