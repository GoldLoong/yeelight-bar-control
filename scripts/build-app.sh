#!/usr/bin/env bash
# 打出可双击的 .app 到 app/dist
# 默认本机架构。Universal：YEELIGHT_UNIVERSAL=1 ./scripts/build-app.sh
# Python 不走 electron-builder extraResources，避免 lipo 合并 600MB+ 运行时卡死。
# 每次成功打包后会把 .app 归档到 backups/apps/（带时间戳 zip），避免被下次覆盖。
# 跳过归档：YEELIGHT_SKIP_APP_ARCHIVE=1 ./scripts/build-app.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ARCHIVE_DIR="$ROOT/backups/apps"

archive_app() {
  local app_dir="$1"
  if [[ "${YEELIGHT_SKIP_APP_ARCHIVE:-0}" == "1" ]]; then
    echo "==> 跳过历史归档（YEELIGHT_SKIP_APP_ARCHIVE=1）"
    return 0
  fi
  local stamp name zip_path
  stamp="$(date +%Y%m%d-%H%M%S)"
  name="$(basename "$app_dir" .app)"
  mkdir -p "$ARCHIVE_DIR"
  zip_path="$ARCHIVE_DIR/${name}-${stamp}.zip"
  echo "==> 归档本版 .app → $zip_path"
  # ditto 保留 macOS .app 资源叉与权限，解压后可直接用
  ditto -c -k --sequesterRsrc --keepParent "$app_dir" "$zip_path"
  ls -lh "$zip_path"
}

chmod +x "$ROOT/scripts/prepare-runtime.sh"
"$ROOT/scripts/prepare-runtime.sh"

cd "$ROOT/app"
if [[ ! -d node_modules ]]; then
  npm install
fi
if [[ ! -x node_modules/.bin/electron-builder ]]; then
  npm install --no-save electron-builder@^25.1.8
fi

unset ELECTRON_RUN_AS_NODE

HOST_ARCH="$(uname -m)"
if [[ "${YEELIGHT_UNIVERSAL:-0}" == "1" ]]; then
  echo "==> Universal Electron（Python 稍后单独拷入，不参与 lipo）"
  npx electron-builder --mac dir --universal
else
  echo "==> 本机架构 Electron（$HOST_ARCH）"
  if [[ "$HOST_ARCH" == "arm64" ]]; then
    npx electron-builder --mac dir --arm64
  else
    npx electron-builder --mac dir --x64
  fi
fi

APP_DIR="$(find "$ROOT/app/dist" -maxdepth 3 -name '*.app' | head -1)"
if [[ -z "$APP_DIR" || ! -d "$APP_DIR" ]]; then
  echo "未找到 .app 产物" >&2
  exit 1
fi

echo "==> 拷入捆绑 Python"
mkdir -p "$APP_DIR/Contents/Resources/python"
rsync -a --delete "$ROOT/packaging/payload/python/" "$APP_DIR/Contents/Resources/python/"

archive_app "$APP_DIR"

echo
echo "已生成："
echo "  $APP_DIR"
echo "历史归档目录："
echo "  $ARCHIVE_DIR"
echo
echo "可拖到「应用程序」后双击。若 Gatekeeper 拦截：右键 → 打开。"
echo "菜单栏点图标打开迷你面板；右键可开关主灯/氛围/同步。"
echo "打 Universal：YEELIGHT_UNIVERSAL=1 $0"
echo "跳过归档：YEELIGHT_SKIP_APP_ARCHIVE=1 $0"
