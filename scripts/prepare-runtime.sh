#!/usr/bin/env bash
# 准备打包进 .app 的 Python 运行时 + server / 插件。
# 默认精简复制本机 Python.framework（不依赖 GitHub）。
# 产出一份 payload/python（python.org 框架多为 universal2）。
# 可选：YEELIGHT_FETCH_STANDALONE=1 改为下载独立 CPython。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="$ROOT/packaging/cache"
PAYLOAD="$ROOT/packaging/payload"
PY_TAG="${PY_STANDALONE_TAG:-20260901}"
PY_VER="${PY_STANDALONE_VER:-3.13.15}"
HOST_ARCH="$(uname -m)"
if [[ "$HOST_ARCH" == "arm64" ]]; then
  HOST_SLOT="arm64"
else
  HOST_SLOT="x64"
fi

mkdir -p "$CACHE" "$PAYLOAD/python"

fetch_python() {
  local arch_triple="$1" dest="$2"
  local name="cpython-${PY_VER}+${PY_TAG}-${arch_triple}-install_only_stripped.tar.gz"
  local url="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/${name}"
  local tarball="$CACHE/$name"
  if [[ ! -f "$tarball" ]]; then
    echo "下载 $name"
    curl -L --fail --retry 2 --connect-timeout 15 --max-time 180 -o "$tarball" "$url"
  fi
  rm -rf "$dest"
  mkdir -p "$dest"
  tar -xzf "$tarball" -C "$dest"
  if [[ -d "$dest/python" ]]; then
    shopt -s dotglob
    mv "$dest/python"/* "$dest"/
    rmdir "$dest/python"
    shopt -u dotglob
  fi
}

vendor_host_python() {
  local dest="$1"
  local py="${ROOT}/.venv/bin/python"
  if [[ ! -x "$py" ]]; then
    py="$(command -v python3)"
  fi
  local base
  base="$("$py" -c "import sys; print(sys.base_prefix)")"
  echo "精简复制 Python: $base -> $dest"
  rm -rf "$dest"
  mkdir -p "$dest/bin" "$dest/lib/python3.13"

  local pybin=""
  if [[ -x "$base/Resources/Python.app/Contents/MacOS/Python" ]]; then
    pybin="$base/Resources/Python.app/Contents/MacOS/Python"
  elif [[ -x "$base/bin/python3" ]]; then
    pybin="$base/bin/python3"
  elif [[ -x "$base/bin/python3.13" ]]; then
    pybin="$base/bin/python3.13"
  fi
  if [[ -z "$pybin" ]]; then
    echo "找不到 python3 可执行文件" >&2
    exit 1
  fi
  cp "$pybin" "$dest/bin/python3"
  chmod +x "$dest/bin/python3"

  if [[ -f "$base/Python" ]]; then
    cp "$base/Python" "$dest/Python"
    install_name_tool -change \
      "$base/Python" \
      "@executable_path/../Python" \
      "$dest/bin/python3" 2>/dev/null || true
    codesign --force --sign - "$dest/Python" >/dev/null 2>&1 || true
    codesign --force --sign - "$dest/bin/python3" >/dev/null 2>&1 || true
  fi

  if [[ -d "$base/lib/python3.13" ]]; then
    rsync -a \
      --exclude 'test/' --exclude 'tests/' --exclude 'idle_test/' \
      --exclude 'idlelib/' --exclude 'tkinter/' --exclude 'turtledemo/' \
      --exclude 'site-packages/' --exclude '__pycache__' --exclude '*.pyc' \
      "$base/lib/python3.13/" "$dest/lib/python3.13/"
  fi
  if [[ -d "$base/lib" ]]; then
    find "$base/lib" -maxdepth 1 \( -name 'libpython*' -o -name '*.dylib' \) -exec cp {} "$dest/lib/" \; 2>/dev/null || true
  fi

  local venv_sp="${ROOT}/.venv/lib/python3.13/site-packages"
  if [[ -d "$venv_sp" ]]; then
    mkdir -p "$dest/lib/python3.13/site-packages"
    rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude 'PyObjCTest' \
      "$venv_sp/" "$dest/lib/python3.13/site-packages/"
  fi

  export PYTHONHOME="$dest"
  if "$dest/bin/python3" -c "import sys,fastapi,numpy; print('python ok', sys.version.split()[0])"; then
    echo "捆绑 Python 自检通过"
  else
    echo "警告：捆绑 Python 自检失败（运行时可能仍依赖系统 Python.framework）" >&2
  fi
  unset PYTHONHOME
}

echo "==> 本机架构 ${HOST_ARCH}，精简复制一份 Python（python.org 框架多为 universal2）"
vendor_host_python "$PAYLOAD/python"

if [[ "${YEELIGHT_FETCH_STANDALONE:-0}" == "1" ]]; then
  echo "==> YEELIGHT_FETCH_STANDALONE=1，下载独立 CPython 覆盖 payload/python"
  set +e
  if [[ "$HOST_SLOT" == "arm64" ]]; then
    fetch_python "aarch64-apple-darwin" "$PAYLOAD/python"
  else
    fetch_python "x86_64-apple-darwin" "$PAYLOAD/python"
  fi
  if [[ -x "$PAYLOAD/python/bin/python3" ]]; then
    "$PAYLOAD/python/bin/python3" -m pip install -q --upgrade pip
    "$PAYLOAD/python/bin/python3" -m pip install -q -r "$ROOT/requirements.txt"
  fi
  set -e
fi

echo "==> 复制 server / 插件 / 扩展"
rm -rf "$PAYLOAD/server" "$PAYLOAD/iina-plugin" "$PAYLOAD/browser-extension" "$PAYLOAD/config.example.json"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/server/" "$PAYLOAD/server/"
rsync -a "$ROOT/iina-plugin/" "$PAYLOAD/iina-plugin/"
mkdir -p "$PAYLOAD/browser-extension"
rsync -a "$ROOT/browser-extension/chromium/" "$PAYLOAD/browser-extension/chromium/"
if [[ -f "$ROOT/browser-extension/README.md" ]]; then
  cp "$ROOT/browser-extension/README.md" "$PAYLOAD/browser-extension/README.md"
fi
cp "$ROOT/config.example.json" "$PAYLOAD/config.example.json"

echo "payload 已就绪: $PAYLOAD"
du -sh "$PAYLOAD" "$PAYLOAD/python/"* 2>/dev/null || true
