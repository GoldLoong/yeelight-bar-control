from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any


PLUGIN_TTL_SEC = 2.5


@dataclass
class IinaStatus:
    running: bool
    playing: bool
    pause: bool | None = None
    path: str | None = None
    title: str | None = None
    source: str = "none"
    idle: bool | None = None
    event: str | None = None
    position: float | None = None
    duration: float | None = None
    speed: float | None = None
    seek: bool = False


_lock = threading.Lock()
_plugin_payload: dict[str, Any] | None = None
_plugin_updated_at: float = 0.0
# 仅播放器实例上报会刷新；全局心跳不能续命 playing
_player_updated_at: float = 0.0
_seek_token: int = 0
_consume_seek_token: int = 0


def update_plugin_status(payload: dict[str, Any]) -> IinaStatus:
    global _plugin_payload, _plugin_updated_at, _player_updated_at, _seek_token
    with _lock:
        event = payload.get("event")
        now = time.monotonic()
        if event in ("global-heartbeat", "global-start") and _plugin_payload:
            # 已收到退出信号时，不要被全局心跳“复活”
            if _plugin_payload.get("event") in ("will-quit", "shutdown") or (
                _plugin_payload.get("running") is False
            ):
                return _status_from_plugin(_plugin_payload, fresh=True)
            merged = dict(_plugin_payload)
            merged["running"] = True
            merged["event"] = event
            if "ts" in payload:
                merged["ts"] = payload["ts"]
            # 播放器实例若已停报，全局心跳只保 running，不得续命 playing
            if now - _player_updated_at > PLUGIN_TTL_SEC:
                merged["playing"] = False
                merged["pause"] = True
                merged["idle"] = True
            _plugin_payload = merged
            _plugin_updated_at = now
        else:
            _plugin_payload = dict(payload)
            if payload.get("seek") or event in ("seek", "file-loaded", "file-started"):
                _seek_token += 1
                _plugin_payload["seek"] = True
            _plugin_updated_at = now
            _player_updated_at = now
        return _status_from_plugin(_plugin_payload, fresh=True)


def consume_seek() -> bool:
    global _consume_seek_token
    with _lock:
        if _seek_token != _consume_seek_token:
            _consume_seek_token = _seek_token
            if _plugin_payload is not None:
                _plugin_payload["seek"] = False
            return True
        return False


def get_plugin_status(max_age: float = PLUGIN_TTL_SEC) -> IinaStatus | None:
    with _lock:
        if not _plugin_payload:
            return None
        now = time.monotonic()
        if now - _plugin_updated_at > max_age:
            return None
        st = _status_from_plugin(_plugin_payload, fresh=True)
        # 双保险：全局心跳刷新了 TTL，但播放器实例已过期 → 不当成在播
        if st.playing and now - _player_updated_at > max_age:
            return IinaStatus(
                running=st.running,
                playing=False,
                pause=True,
                idle=True,
                path=st.path,
                title=st.title,
                source=st.source,
                event=st.event or "player-stale",
                position=st.position,
                duration=st.duration,
                speed=st.speed,
                seek=False,
            )
        return st


def _status_from_plugin(payload: dict[str, Any], *, fresh: bool) -> IinaStatus:
    paused = payload.get("pause")
    idle = payload.get("idle")
    playing = bool(payload.get("playing"))
    if paused is not None or idle is not None:
        playing = (not bool(paused)) and (not bool(idle))

    def _f(key: str) -> float | None:
        try:
            v = payload.get(key)
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    return IinaStatus(
        running=bool(payload.get("running", True)) if fresh else False,
        playing=playing,
        pause=bool(paused) if paused is not None else None,
        idle=bool(idle) if idle is not None else None,
        path=payload.get("path"),
        title=payload.get("title"),
        source="plugin",
        event=payload.get("event"),
        position=_f("position"),
        duration=_f("duration"),
        speed=_f("speed"),
        seek=bool(payload.get("seek")),
    )


def is_iina_running() -> bool:
    try:
        return (
            subprocess.run(
                ["pgrep", "-x", "IINA"],
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            == 0
        )
    except OSError:
        return False


def get_iina_status(ipc_socket: str | None = None) -> IinaStatus:
    _ = ipc_socket
    proc = is_iina_running()
    plugin = get_plugin_status()
    if plugin is not None:
        # 进程已退出时，忽略尚未过期的插件缓存，避免拖慢「退出后」动作
        if not proc:
            return IinaStatus(running=False, playing=False, source="process")
        return plugin
    if not proc:
        return IinaStatus(running=False, playing=False, source="process")
    return IinaStatus(running=True, playing=False, source="process-no-plugin")


def status_to_dict(st: IinaStatus) -> dict[str, Any]:
    return asdict(st)
