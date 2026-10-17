#!/usr/bin/env bash
# 打出可双击的 .app 到 app/dist
# 默认本机架构。Universal：YEELIGHT_UNIVERSAL=1 ./scripts/build-app.sh
# Python 不走 electron-builder extraResources，避免 lipo 合并 600MB+ 运行时卡死。
# 每次打包自动把 app/package.json 的小版本 +1（0.2.0 → 0.2.1）。
# 产物写到 app/dist/v<版本>/，不覆盖上一版目录。
# 成功后再把 .app 归档到 backups/apps/（版本号 + 时间戳 zip），旧 zip 保留。
# 跳过归档：YEELIGHT_SKIP_APP_ARCHIVE=1 ./scripts/build-app.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ARCHIVE_DIR="$ROOT/backups/apps"
PKG="$ROOT/app/package.json"

bump_patch_version() {
  node -e '
    const fs = require("fs");
    const p = process.argv[1];
    const raw = fs.readFileSync(p, "utf8");
    const current = JSON.parse(raw).version || "0.0.0";
    const parts = String(current).split(".").map((n) => parseInt(n, 10) || 0);
    while (parts.length < 3) parts.push(0);
    parts[2] += 1;
    const next = parts.slice(0, 3).join(".");
    const updated = raw.replace(/"version"\s*:\s*"[^"]+"/, `"version": "${next}"`);
    if (updated === raw) {
      console.error("未能改写 package.json 的 version");
      process.exit(1);
    }
    fs.writeFileSync(p, updated);
    process.stdout.write(next);
  ' "$PKG"
}

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
  zip_path="$ARCHIVE_DIR/${name}-${VERSION}-${stamp}.zip"
  if [[ -e "$zip_path" ]]; then
    echo "归档已存在，不覆盖：$zip_path" >&2
    exit 1
  fi
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

PREV_VERSION="$(node -e 'process.stdout.write(JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")).version)' "$PKG")"
VERSION="$(bump_patch_version)"
echo "==> 版本升至 ${VERSION}（旧产物目录保留）"
OUT_REL="dist/v${VERSION}"
VERSION_KEPT=0
restore_version() {
  if [[ "$VERSION_KEPT" == "1" ]]; then
    return 0
  fi
  node -e '
    const fs = require("fs");
    const p = process.argv[1];
    const prev = process.argv[2];
    const raw = fs.readFileSync(p, "utf8");
    fs.writeFileSync(p, raw.replace(/"version"\s*:\s*"[^"]+"/, `"version": "${prev}"`));
  ' "$PKG" "$PREV_VERSION"
  echo "==> 打包未完成，版本号已退回 $PREV_VERSION" >&2
}
trap restore_version ERR

HOST_ARCH="$(uname -m)"
if [[ "${YEELIGHT_UNIVERSAL:-0}" == "1" ]]; then
  echo "==> Universal Electron（Python 稍后单独拷入，不参与 lipo）"
  npx electron-builder --mac dir --universal -c.directories.output="$OUT_REL"
else
  echo "==> 本机架构 Electron（${HOST_ARCH}）"
  if [[ "$HOST_ARCH" == "arm64" ]]; then
    npx electron-builder --mac dir --arm64 -c.directories.output="$OUT_REL"
  else
    npx electron-builder --mac dir --x64 -c.directories.output="$OUT_REL"
  fi
fi

APP_DIR="$(find "$ROOT/app/$OUT_REL" -maxdepth 3 -name '*.app' -print -quit)"
if [[ -z "$APP_DIR" || ! -d "$APP_DIR" ]]; then
  echo "未找到 .app 产物" >&2
  exit 1
fi

echo "==> 拷入捆绑 Python"
mkdir -p "$APP_DIR/Contents/Resources/python"
rsync -a --delete "$ROOT/packaging/payload/python/" "$APP_DIR/Contents/Resources/python/"

# 稳定 bundle id 的 ad-hoc 签名：比完全不签名更易被 TCC 识别。
# 注意：无 Apple 开发者证书时，每次构建 CDHash 仍会变，更新后仍可能要重新打开屏幕录制开关。
echo "==> ad-hoc 代码签名（identifier=com.yeelightbar.control）"
if codesign --force --deep --sign - --identifier "com.yeelightbar.control" "$APP_DIR" 2>/dev/null; then
  codesign -dv --verbose=2 "$APP_DIR" 2>&1 | head -8 || true
else
  echo "警告：codesign 失败，应用将保持未签名" >&2
fi

archive_app "$APP_DIR"
VERSION_KEPT=1
trap - ERR

echo
echo "已生成（v${VERSION}，未覆盖上一版）："
echo "  $APP_DIR"
echo "历史归档目录（只追加）："
echo "  $ARCHIVE_DIR"
echo
echo "可拖到「应用程序」后双击。若 Gatekeeper 拦截：右键 → 打开。"
echo "菜单栏点图标打开迷你面板；右键可开关主灯/氛围/同步。"
echo "打 Universal：YEELIGHT_UNIVERSAL=1 $0"
echo "跳过归档：YEELIGHT_SKIP_APP_ARCHIVE=1 $0"
