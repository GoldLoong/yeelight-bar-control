"""夜色模式临时调试日志：仅指定灯 IP 才写，后续会删。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from server.config import BUNDLE_ID

# 仅此灯 IP 记录；其他人 / 其它环境零写入
DEBUG_LAMP_HOST = "192.168.0.116"

_lock = threading.Lock()
_counts: dict[str, int] = {}
_enabled_host: str | None = None
_last_flush = 0.0


def set_lamp_host(host: str | None) -> None:
    global _enabled_host
    h = (host or "").strip()
    _enabled_host = h if h == DEBUG_LAMP_HOST else None


def enabled() -> bool:
    return _enabled_host == DEBUG_LAMP_HOST


def _path() -> Path:
    return Path.home() / "Library" / "Application Support" / BUNDLE_ID / "night-pattern.log"


def log_event(kind: str, **fields: object) -> None:
    if not enabled():
        return
    with _lock:
        _counts[kind] = _counts.get(kind, 0) + 1
        row = {
            "t": round(time.time(), 3),
            "kind": kind,
            "count": _counts[kind],
            "counts": dict(_counts),
            **fields,
        }
        try:
            path = _path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            return
        global _last_flush
        now = time.monotonic()
        # 周期性写一行汇总，方便扫次数
        if now - _last_flush > 30.0:
            _last_flush = now
            try:
                with path.open("a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {"t": round(time.time(), 3), "kind": "summary", "counts": dict(_counts)},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            except OSError:
                pass
