#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

export YEELIGHT_PYTHON="$ROOT/.venv/bin/python"
export YEELIGHT_CONFIG_DIR="${HOME}/Library/Application Support/com.yeelightbar.control"
export YEELIGHT_EXAMPLE_CONFIG="$ROOT/config.example.json"
export YEELIGHT_LEGACY_CONFIG="$ROOT/config.json"
# 本机若设置了 ELECTRON_RUN_AS_NODE=1，Electron 会当 Node 跑导致打不开
unset ELECTRON_RUN_AS_NODE

# 清掉占用 8787 的旧服务，避免 Electron 复用到不含嵌套 sync 的老进程
if command -v lsof >/dev/null 2>&1; then
  old_pids="$(lsof -tiTCP:8787 -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "${old_pids}" ]]; then
    echo "停止旧 API: ${old_pids}"
    # shellcheck disable=SC2086
    kill ${old_pids} 2>/dev/null || true
    sleep 0.5
    # shellcheck disable=SC2086
    kill -9 ${old_pids} 2>/dev/null || true
  fi
fi

cd "$ROOT/app"
if [[ ! -d node_modules ]]; then
  npm install
fi
npm start
