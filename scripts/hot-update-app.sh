#!/usr/bin/env bash
# 热更内容并重启已安装应用。
#
# 默认（保录屏权限 / TCC）：
#   - server → ~/Library/Application Support/com.yeelightbar.control/runtime/
#   - 不改 .app、不 codesign（改 Resources 会弄坏 seal；重签会换 CDHash → 录屏权限掉）
#
# 可选：
#   --ui       重打 app.asar 并写入 .app（会破坏 seal，需配合 --resign 才能启动）
#   --resign   ad-hoc 重签 .app（会换签名哈希，通常要重开屏幕录制权限）
#   --no-restart
#
# 用法：
#   ./scripts/hot-update-app.sh
#   ./scripts/hot-update-app.sh --ui --resign   # 改了 main/renderer 时，会提示可能要重开录屏
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${YEELIGHT_APP:-/Applications/Yeelight 显示器挂灯.app}"
RES="$APP/Contents/Resources"
SUPPORT="${HOME}/Library/Application Support/com.yeelightbar.control"
RUNTIME="$SUPPORT/runtime"
RESTART=1
DO_UI=0
DO_RESIGN=0
for arg in "$@"; do
  case "$arg" in
    --no-restart) RESTART=0 ;;
    --ui) DO_UI=1 ;;
    --resign) DO_RESIGN=1 ;;
  esac
done

if [[ ! -d "$APP/Contents" ]]; then
  echo "找不到已安装应用: $APP" >&2
  exit 1
fi
if [[ ! -d "$RES" ]]; then
  echo "缺少 Resources: $RES" >&2
  exit 1
fi

echo "==> 热更 server → $RUNTIME（不碰 .app 签名）"
mkdir -p "$RUNTIME"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
  "$ROOT/server/" "$RUNTIME/server/"
if [[ -f "$ROOT/config.example.json" ]]; then
  cp "$ROOT/config.example.json" "$RUNTIME/config.example.json"
fi
find "$RUNTIME" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

# 插件/扩展仍在 Resources；仅在 --ui 时同步，避免无谓改 seal
if [[ "$DO_UI" -eq 1 ]]; then
  echo "==> 热更插件 / 扩展 → $APP（--ui）"
  rsync -a --delete "$ROOT/iina-plugin/" "$RES/iina-plugin/"
  mkdir -p "$RES/browser-extension"
  rsync -a --delete "$ROOT/browser-extension/chromium/" "$RES/browser-extension/chromium/"
  if [[ -f "$ROOT/browser-extension/README.md" ]]; then
    cp "$ROOT/browser-extension/README.md" "$RES/browser-extension/README.md"
  fi
  if [[ -f "$ROOT/config.example.json" ]]; then
    cp "$ROOT/config.example.json" "$RES/config.example.json"
  fi

  echo "==> 重打 app.asar（main / preload / renderer）"
  STAGE="$(mktemp -d /tmp/yeelight-asar.XXXXXX)"
  cleanup() { rm -rf "$STAGE"; }
  trap cleanup EXIT
  cp "$ROOT/app/main.js" "$STAGE/main.js"
  cp "$ROOT/app/preload.js" "$STAGE/preload.js"
  if [[ -f "$ROOT/app/package.json" ]]; then
    python3 - <<'PY' "$ROOT/app/package.json" "$STAGE/package.json"
import json, sys
src, dst = sys.argv[1], sys.argv[2]
pkg = json.load(open(src, encoding="utf-8"))
json.dump({"name": pkg.get("name"), "version": pkg.get("version"), "main": pkg.get("main", "main.js")}, open(dst, "w", encoding="utf-8"))
PY
  else
    printf '%s\n' '{"name":"yeelight-bar-control","main":"main.js"}' > "$STAGE/package.json"
  fi
  mkdir -p "$STAGE/renderer" "$STAGE/build"
  rsync -a "$ROOT/app/renderer/" "$STAGE/renderer/"
  for f in "build/trayTemplate.png" "build/trayTemplate@2x.png"; do
    if [[ -f "$ROOT/app/$f" ]]; then
      mkdir -p "$STAGE/$(dirname "$f")"
      cp "$ROOT/app/$f" "$STAGE/$f"
    fi
  done

  ASAR_BIN=""
  if command -v npx >/dev/null 2>&1; then
    ASAR_BIN="npx --yes asar"
  elif [[ -x "$ROOT/app/node_modules/.bin/asar" ]]; then
    ASAR_BIN="$ROOT/app/node_modules/.bin/asar"
  fi
  if [[ -z "$ASAR_BIN" ]]; then
    echo "需要 asar（npx asar 或 app/node_modules/.bin/asar）" >&2
    exit 1
  fi
  $ASAR_BIN pack "$STAGE" "$RES/app.asar"
  find "$RES/server" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

  # Electron 会校验 Info.plist 里的 ElectronAsarIntegrity；哈希不对会静默秒退
  echo "==> 更新 Info.plist ElectronAsarIntegrity"
  ASAR_HASH="$(shasum -a 256 "$RES/app.asar" | awk '{print $1}')"
  PLIST="$APP/Contents/Info.plist"
  if /usr/libexec/PlistBuddy -c 'Print :ElectronAsarIntegrity:Resources/app.asar:hash' "$PLIST" >/dev/null 2>&1; then
    /usr/libexec/PlistBuddy -c "Set :ElectronAsarIntegrity:Resources/app.asar:hash $ASAR_HASH" "$PLIST"
  else
    /usr/libexec/PlistBuddy -c 'Add :ElectronAsarIntegrity dict' "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c 'Add :ElectronAsarIntegrity:Resources/app.asar dict' "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c 'Add :ElectronAsarIntegrity:Resources/app.asar:algorithm string SHA256' "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Add :ElectronAsarIntegrity:Resources/app.asar:hash string $ASAR_HASH" "$PLIST" 2>/dev/null \
      || /usr/libexec/PlistBuddy -c "Set :ElectronAsarIntegrity:Resources/app.asar:hash $ASAR_HASH" "$PLIST"
  fi
  echo "    hash=$ASAR_HASH"
  # 改了 asar/plist 必须重签才能启动；会动 TCC
  DO_RESIGN=1
fi

if [[ "$DO_RESIGN" -eq 1 ]]; then
  echo "==> ad-hoc 重签 .app（会换 CDHash，旧屏幕录制授权会失效）"
  xattr -cr "$APP" 2>/dev/null || true
  codesign --force --deep --sign - "$APP" >/dev/null
  # 旧 TCC 条目绑的是上一版 CDHash，留着只会「已勾选却取色失败」；主动清掉让系统重新要授权
  BUNDLE_ID="com.yeelightbar.control"
  echo "==> 清除失效的屏幕录制 TCC：$BUNDLE_ID"
  if tccutil reset ScreenCapture "$BUNDLE_ID" 2>/dev/null; then
    echo "    已 reset ScreenCapture（列表里旧项应消失，下次启动会重新申请）"
  else
    echo "    tccutil reset 失败（可手动：系统设置 → 屏幕录制 → 删掉本应用）" >&2
  fi
  # 清掉本地 sticky「已授权」，避免 UI/后端误以为还有效
  PERM_FILE="$SUPPORT/music-permissions.json"
  if [[ -f "$PERM_FILE" ]]; then
    python3 - <<'PY' "$PERM_FILE"
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p, encoding="utf-8"))
except Exception:
    d = {}
d.update({
    "screen": "denied",
    "screenValidated": False,
    "preflight": False,
    "source": "resign-reset",
    "screenProbeOnce": False,
    "settingsOpenedScreen": False,
    "updatedAt": __import__("time").time() * 1000,
})
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
print("    已重置", p)
PY
  fi
  # 记下新 CDHash，供应用启动时比对
  NEW_HASH="$(codesign -dv --verbose=4 "$APP" 2>&1 | awk -F= '/^CDHash=/{print $2; exit}')"
  if [[ -n "${NEW_HASH:-}" ]]; then
    mkdir -p "$SUPPORT"
    printf '%s\n' "$NEW_HASH" > "$SUPPORT/last-cdhash.txt"
    echo "    CDHash=$NEW_HASH"
  fi
  echo "⚠ 重签后需在系统设置里重新打开「屏幕录制」开关（旧项已自动清除）" >&2
fi

echo "==> 热更完成（server → runtime）"
if [[ "$RESTART" -ne 1 ]]; then
  exit 0
fi

# 进程 argv 里的中文路径会被 locale 弄乱，pgrep 全路径会漏检。
# 漏检时会在旧进程还占着单实例锁时再 open，新进程秒退，看起来像没自动打开。
app_pids() {
  {
    pgrep -f "/Applications/Yeelight" || true
    pgrep -f "com.yeelightbar.control" || true
    pgrep -f "[-]m server.main" || true
  } | sort -u
}

echo "==> 重启应用"
# Cursor / 部分终端会带上 ELECTRON_RUN_AS_NODE=1，Electron 会当 Node 秒退，open 像没打开
unset ELECTRON_RUN_AS_NODE
osascript -e 'quit app "Yeelight 显示器挂灯"' 2>/dev/null || true
sleep 0.6
pids=$(app_pids)
if [[ -n "${pids:-}" ]]; then
  kill $pids 2>/dev/null || true
  sleep 0.6
  pids=$(app_pids)
  if [[ -n "${pids:-}" ]]; then
    kill -9 $pids 2>/dev/null || true
    sleep 0.3
  fi
fi
for pid in $(lsof -nP -iTCP:8787 -sTCP:LISTEN -t 2>/dev/null || true); do
  kill "$pid" 2>/dev/null || true
done
rm -f "$SUPPORT/SingletonLock" "$SUPPORT/SingletonCookie" "$SUPPORT/SingletonSocket"

open -b com.yeelightbar.control || open "$APP"
up=0
for i in $(seq 1 20); do
  if curl -sf --max-time 1 http://127.0.0.1:8787/api/health >/dev/null 2>&1; then
    up=1
    break
  fi
  if [[ -z "$(app_pids)" ]]; then
    if [[ "$i" -eq 2 || "$i" -eq 5 || "$i" -eq 9 ]]; then
      echo "==> 进程未在，再次 open（不重签）" >&2
      open -b com.yeelightbar.control 2>/dev/null || open "$APP" 2>/dev/null || true
    fi
  fi
  sleep 0.4
done

# 拉起控制窗：second-instance + activate
open -b com.yeelightbar.control 2>/dev/null || open "$APP" 2>/dev/null || true
# 应用已在时 activate 很快；没起来时 AppleScript 会一直等，不能卡住热更
osascript -e 'tell application "Yeelight 显示器挂灯" to activate' >/dev/null 2>&1 &
act_pid=$!
for _ in 1 2 3 4 5 6; do
  if ! kill -0 "$act_pid" 2>/dev/null; then
    break
  fi
  sleep 0.4
done
kill "$act_pid" 2>/dev/null || true
wait "$act_pid" 2>/dev/null || true

if [[ "$up" -eq 1 ]]; then
  echo "已打开: $APP（health 正常；控制窗应已弹出）"
  curl -s --max-time 1 http://127.0.0.1:8787/api/health || true
  echo
  exit 0
fi
if [[ -n "$(app_pids)" ]]; then
  echo "已打开进程，API 尚未就绪；已 activate/open。请看菜单栏挂灯图标。" >&2
  exit 0
fi
echo "未能自动拉起应用。请手动打开: $APP" >&2
echo "若刚用过 --ui 未 --resign，可：./scripts/hot-update-app.sh --ui --resign（会动录屏权限）" >&2
exit 1
