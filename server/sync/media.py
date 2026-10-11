"""浏览器等非 IINA 媒体源：扩展 hook 状态（对齐 iina.py TTL 语义）。"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse


PLUGIN_TTL_SEC = 5.0


@dataclass
class MediaStatus:
    running: bool
    playing: bool
    pause: bool | None = None
    provider: str = "none"
    source: str = "none"
    event: str | None = None
    title: str | None = None
    url: str | None = None
    host: str | None = None
    fullscreen: bool | None = None
    position: float | None = None
    duration: float | None = None
    seek: bool = False


_lock = threading.Lock()
_payload: dict[str, Any] | None = None
_updated_at: float = 0.0
_seek_token: int = 0
_consume_seek_token: int = 0


def update_browser_status(payload: dict[str, Any]) -> MediaStatus:
    global _payload, _updated_at, _seek_token
    with _lock:
        event = payload.get("event")
        # 顶层无 video 的 idle 勿覆盖近期真实播放态（iframe 播片场景）
        if (
            event == "idle"
            and not payload.get("playing")
            and _payload
            and _payload.get("playing")
            and time.monotonic() - _updated_at < 3.0
        ):
            return _status_from_payload(_payload, fresh=True)
        if event in ("heartbeat", "global-heartbeat") and _payload:
            if _payload.get("event") in ("exit", "ended", "shutdown") or (
                _payload.get("running") is False
            ):
                return _status_from_payload(_payload, fresh=True)
            merged = dict(_payload)
            merged["running"] = True
            merged["event"] = event
            for key in ("playing", "pause", "fullscreen", "position", "duration", "ts"):
                if key in payload:
                    merged[key] = payload[key]
            _payload = merged
        else:
            data = dict(payload)
            data.setdefault("provider", "browser")
            if data.get("event") in ("exit", "ended"):
                data["running"] = False
                data["playing"] = False
                data["pause"] = True
            if not data.get("host") and data.get("url"):
                try:
                    data["host"] = urlparse(str(data["url"])).hostname
                except Exception:
                    pass
            _payload = data
            if payload.get("seek") or event in ("seek", "file-loaded", "file-started"):
                _seek_token += 1
                _payload["seek"] = True
        _updated_at = time.monotonic()
        return _status_from_payload(_payload, fresh=True)


def consume_browser_seek() -> bool:
    global _consume_seek_token
    with _lock:
        if _seek_token != _consume_seek_token:
            _consume_seek_token = _seek_token
            if _payload is not None:
                _payload["seek"] = False
            return True
        return False


def get_browser_plugin_status(max_age: float = PLUGIN_TTL_SEC) -> MediaStatus | None:
    with _lock:
        if not _payload:
            return None
        if time.monotonic() - _updated_at > max_age:
            return None
        return _status_from_payload(_payload, fresh=True)


def _host_allowed(host: str | None, match_hosts: list[str] | None) -> bool:
    if not match_hosts:
        return True
    ph = (host or "").lower().strip(".")
    if not ph:
        return False
    hosts = {h.lower().strip(".") for h in match_hosts if h}
    for h in hosts:
        if ph == h or ph.endswith("." + h) or h.endswith("." + ph):
            return True
    return False


def get_browser_status(
    *,
    require_extension: bool = True,
    match_hosts: list[str] | None = None,
    owners: list[str] | None = None,
) -> MediaStatus:
    plugin = get_browser_plugin_status()
    if plugin is not None:
        # 明确退出后允许走标题兜底；暂停(running=true)仍尊重扩展
        exited = (not plugin.running) and (
            plugin.event in ("exit", "ended", "shutdown") or not plugin.playing
        )
        if not exited:
            if match_hosts and plugin.playing and not _host_allowed(plugin.host, match_hosts):
                return MediaStatus(
                    running=plugin.running,
                    playing=False,
                    pause=True,
                    provider="browser",
                    source="host-mismatch",
                    event=plugin.event,
                    title=plugin.title,
                    url=plugin.url,
                    host=plugin.host,
                    fullscreen=plugin.fullscreen,
                )
            return plugin

    # 无新鲜 hook / 已退出：视频页标题兜底（排除首页）
    title_hit = _bilibili_video_window_title(owners)
    if title_hit:
        return MediaStatus(
            running=True,
            playing=True,
            pause=False,
            provider="browser",
            source="title-fallback",
            event="title-fallback",
            title=title_hit,
            fullscreen=None,
        )

    if require_extension:
        return MediaStatus(
            running=False, playing=False, provider="browser", source="none"
        )

    return MediaStatus(
        running=True,
        playing=True,
        provider="browser",
        source="window-only",
    )


_HOME_TITLE_MARKERS = (
    "干杯~-bilibili",
    "干杯~-bilibil",
    "哔哩哔哩弹幕视频网",
    "bilibili网页版",
)


def _bilibili_video_window_title(owners: list[str] | None) -> str | None:
    """仅当窗口标题像在看视频时才兜底，排除首页。"""
    try:
        from server.sync.capture import find_media_window

        win = find_media_window(
            "browser",
            owners=owners,
            use_cache=True,
            display_id=None,
            prefer_titles=("哔哩", "bilibili"),
        )
    except Exception:
        return None
    if win is None or not win.title:
        return None
    t = win.title.strip()
    low = t.lower()
    if "bilibili" not in low and "哔哩" not in t:
        return None
    if any(m in t for m in _HOME_TITLE_MARKERS):
        return None
    # 真片标题通常更长，或带分隔符
    if len(t) < 12:
        return None
    return t


def _bilibili_window_title(owners: list[str] | None) -> str | None:
    return _bilibili_video_window_title(owners)


def _status_from_payload(payload: dict[str, Any], *, fresh: bool) -> MediaStatus:
    paused = payload.get("pause")
    playing = bool(payload.get("playing"))
    if paused is not None:
        playing = (not bool(paused)) and bool(payload.get("running", True))

    event = payload.get("event")
    if event in ("exit", "ended"):
        playing = False

    def _f(key: str) -> float | None:
        try:
            v = payload.get(key)
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    running = bool(payload.get("running", True)) if fresh else False
    if event in ("exit", "ended"):
        running = False

    return MediaStatus(
        running=running,
        playing=playing and running,
        pause=bool(paused) if paused is not None else (not playing),
        provider=str(payload.get("provider") or "browser"),
        source="extension",
        event=event,
        title=payload.get("title"),
        url=payload.get("url"),
        host=payload.get("host"),
        fullscreen=bool(payload["fullscreen"]) if "fullscreen" in payload else None,
        position=_f("position"),
        duration=_f("duration"),
        seek=bool(payload.get("seek")),
    )


def media_status_to_dict(st: MediaStatus) -> dict[str, Any]:
    return asdict(st)
