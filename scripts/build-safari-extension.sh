#!/usr/bin/env bash
# 将 Chromium 扩展脚本同步到 Safari Resources，并在有 Xcode 时生成/更新工程
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/browser-extension/chromium"
DST="$ROOT/browser-extension/safari/Resources"
XCODE_OUT="$ROOT/browser-extension/safari/YeelightBarSync"

mkdir -p "$DST/images"
cp "$SRC/background.js" "$DST/background.js"
cp "$SRC/content.js" "$DST/content.js"
cp "$SRC/images/"*.png "$DST/images/" 2>/dev/null || true

# Safari 专用 manifest 保留（background.scripts + service_worker）
if [[ ! -f "$DST/manifest.json" ]]; then
  echo "缺少 $DST/manifest.json" >&2
  exit 1
fi

echo "已同步脚本 → safari/Resources"

CONVERTER=""
if xcrun --find safari-web-extension-converter >/dev/null 2>&1; then
  CONVERTER="$(xcrun --find safari-web-extension-converter)"
elif [[ -x /Applications/Xcode.app/Contents/Developer/usr/bin/safari-web-extension-converter ]]; then
  CONVERTER=/Applications/Xcode.app/Contents/Developer/usr/bin/safari-web-extension-converter
fi

if [[ -z "$CONVERTER" ]]; then
  echo "未检测到 Xcode 的 safari-web-extension-converter。"
  echo "若已有仓库内手写工程，直接打开："
  echo "  open \"$XCODE_OUT/YeelightBarSync.xcodeproj\""
  echo "或先安装完整 Xcode（App Store），再重新运行本脚本以自动生成工程。"
  exit 0
fi

mkdir -p "$ROOT/browser-extension/safari"
# 用 Resources 作为输入；macos-only；生成到 YeelightBarSync
rm -rf "$XCODE_OUT"
"$CONVERTER" "$DST" \
  --project-location "$ROOT/browser-extension/safari" \
  --app-name "YeelightBarSync" \
  --bundle-identifier "com.yeelight.barsync.safari" \
  --macos-only \
  --copy-resources \
  --force || "$CONVERTER" "$DST" \
  --project-location "$ROOT/browser-extension/safari" \
  --app-name YeelightBarSync \
  --bundle-identifier com.yeelight.barsync.safari \
  --macos-only \
  --copy-resources

echo "Safari 工程已生成。下一步："
echo "  open \"$ROOT/browser-extension/safari/YeelightBarSync/YeelightBarSync.xcodeproj\""
echo "在 Xcode 中 Run，然后到 Safari → 设置 → 扩展 启用 Yeelight Bar Sync，并允许 127.0.0.1。"
