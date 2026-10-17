from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from server.config import load_config, update_config
from server.music import MusicEngine
from server.sync import (
    SyncEngine,
    get_browser_status,
    get_iina_status,
    list_displays,
    media_status_to_dict,
    status_to_dict,
    update_browser_status,
    update_plugin_status,
)
from server.yeelight import YeelightClient, discover
from server.yeelight.client import QuotaExceeded

_client_lock = threading.Lock()
_client: YeelightClient | None = None
_engine: SyncEngine | None = None
_music: MusicEngine | None = None
_state_cache: dict[str, Any] | None = None
_state_cache_at: float = 0.0
_STATE_TTL_IDLE = 5.0
_STATE_TTL_SYNC = 12.0


def get_client() -> YeelightClient:
    global _client
    cfg = load_config()
    with _client_lock:
        if _client is None or _client.host != cfg.host or _client.port != cfg.tcp_port:
            if _client is not None:
                _client.close()
            _client = YeelightClient(host=cfg.host, port=cfg.tcp_port)
        return _client


def get_engine() -> SyncEngine:
    global _engine
    if _engine is None:
        _engine = SyncEngine(client_factory=get_client, get_config=load_config)
    return _engine


def get_music() -> MusicEngine:
    global _music
    if _music is None:
        engine = get_engine()
        _music = MusicEngine(
            client_factory=get_client,
            get_config=load_config,
            sync_hold=engine.set_music_hold,
            picture_active=engine.picture_active,
        )
    return _music


def _music_on() -> bool:
    return load_config().drive_mode == "music"


def _remember_state(partial: dict[str, Any], client: YeelightClient) -> dict[str, Any]:
    """写操作成功后本地合并状态，避免再发 get_prop 拖慢响应。"""
    global _state_cache, _state_cache_at
    import time

    prev = {}
    if _state_cache and isinstance(_state_cache.get("state"), dict):
        prev = dict(_state_cache["state"])
    prev.update(partial)
    if "bg_rgb" in partial and partial.get("bg_rgb") not in (None, ""):
        try:
            prev["bg_rgb_hex"] = f"#{int(partial['bg_rgb']):06x}"
        except (TypeError, ValueError):
            pass
    elif "bg_rgb_hex" in partial and partial.get("bg_rgb_hex"):
        try:
            prev["bg_rgb"] = int(str(partial["bg_rgb_hex"]).lstrip("#"), 16)
        except (TypeError, ValueError):
            pass
    payload = {
        "ok": True,
        "host": client.host,
        "connected": True,
        "state": prev,
        "cached": True,
    }
    _state_cache = payload
    _state_cache_at = time.monotonic()
    return prev


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_config()
    engine = get_engine()
    if load_config().sync.enabled:
        engine.start()
    if load_config().drive_mode == "music":
        try:
            get_music().start()
        except Exception:
            pass
    yield
    get_music().stop(restore=False)
    engine.stop(apply_exit=False)
    with _client_lock:
        if _client is not None:
            _client.close()


app = FastAPI(title="Yeelight Bar Control", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class HostBody(BaseModel):
    host: str
    tcp_port: int | None = None


class PowerBody(BaseModel):
    on: bool
    target: str = Field(pattern="^(main|bg)$")


class BrightBody(BaseModel):
    bright: int = Field(ge=1, le=100)
    target: str = Field(pattern="^(main|bg)$")


class CtBody(BaseModel):
    ct: int = Field(ge=1700, le=6500)


class RgbBody(BaseModel):
    hex: str


class AmbientFxBody(BaseModel):
    fx: str = Field(pattern="^(static|breath|cycle|flow)$")
    bright: int | None = Field(default=None, ge=1, le=100)
    speed_ms: int | None = Field(default=None, ge=400, le=12000)
    hex: str | None = None
    colors: list[str] | None = None
    direction: str | None = Field(default=None, pattern="^(lr|rl|in|out)$")


class ModePatchBody(BaseModel):
    name: str | None = None
    main_on: bool | None = None
    bg_on: bool | None = None
    bright: int | None = Field(default=None, ge=1, le=100)
    ct: int | None = Field(default=None, ge=1700, le=6500)
    bg_bright: int | None = Field(default=None, ge=1, le=100)
    bg_hex: str | None = None
    bg_fx: str | None = None
    bg_speed_ms: int | None = None
    bg_colors: list[str] | None = None
    bg_direction: str | None = None


class SyncPatch(BaseModel):
    enabled: bool | None = None
    priority: list[str] | None = None
    iina: dict[str, Any] | None = None
    browser: dict[str, Any] | None = None
    screen: dict[str, Any] | None = None
    # 兼容：顶层开关 / 手动调光写两边基础亮度
    iina_enabled: bool | None = None
    browser_enabled: bool | None = None
    screen_enabled: bool | None = None
    base_main_bright: int | None = None
    base_ambient_bright: int | None = None


class ConfigPatch(BaseModel):
    host: str | None = None
    tcp_port: int | None = None
    udp_port: int | None = None
    launch_at_login: bool | None = None
    onboarding: dict[str, Any] | None = None
    sync: SyncPatch | None = None


def _normalize_sync_patch(sync: dict[str, Any]) -> dict[str, Any]:
    from server.config import _normalize_priority

    out: dict[str, Any] = {}
    if "enabled" in sync:
        out["enabled"] = sync["enabled"]
    if "priority" in sync:
        out["priority"] = _normalize_priority(sync["priority"])
    iina = dict(sync.get("iina") or {})
    browser = dict(sync.get("browser") or {})
    screen = dict(sync.get("screen") or {})
    if "iina_enabled" in sync:
        iina["enabled"] = sync["iina_enabled"]
    if "browser_enabled" in sync:
        browser["enabled"] = sync["browser_enabled"]
    if "screen_enabled" in sync:
        screen["enabled"] = sync["screen_enabled"]
    if "base_main_bright" in sync:
        iina["base_main_bright"] = sync["base_main_bright"]
        browser["base_main_bright"] = sync["base_main_bright"]
        screen["base_main_bright"] = sync["base_main_bright"]
    if "base_ambient_bright" in sync:
        iina["base_ambient_bright"] = sync["base_ambient_bright"]
        browser["base_ambient_bright"] = sync["base_ambient_bright"]
        screen["base_ambient_bright"] = sync["base_ambient_bright"]
    if iina:
        out["iina"] = iina
    if browser:
        out["browser"] = browser
    if screen:
        out["screen"] = screen
    return out


@app.get("/api/health")
def health() -> dict[str, Any]:
    # api_rev：Electron 用来识别是否需替换旧进程（嵌套 sync.iina/browser 等）
    return {"ok": True, "service": "yeelight-bar", "api_rev": 5}


def _screen_recording_granted() -> bool:
    try:
        from Quartz import CGPreflightScreenCaptureAccess

        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return False


@app.get("/api/permissions/screen")
def api_screen_permission() -> dict[str, Any]:
    granted = _screen_recording_granted()
    return {
        "ok": True,
        "granted": granted,
        "parent": os.environ.get("YEELIGHT_SCREEN_ACCESS") or "",
    }


@app.get("/api/permissions/music")
def api_music_permissions() -> dict[str, Any]:
    """给 Electron 用的权威权限视图：以系统预检为准，并回写权限文件。"""
    from server.music.capture import live_permission_snapshot

    # live_permission_snapshot 内部已按 preflight 纠正/清理 sticky
    return {"ok": True, **live_permission_snapshot()}


@app.post("/api/permissions/screen/request")
def api_screen_permission_request() -> dict[str, Any]:
    try:
        from Quartz import CGRequestScreenCaptureAccess

        granted = bool(CGRequestScreenCaptureAccess())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "granted": False, "error": str(exc)}
    return {"ok": True, "granted": granted}


@app.get("/api/config")
def api_get_config() -> dict[str, Any]:
    return load_config().model_dump()


@app.patch("/api/config")
def api_patch_config(body: ConfigPatch) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    if isinstance(patch.get("sync"), dict):
        patch["sync"] = _normalize_sync_patch(patch["sync"])
    cfg = update_config(patch)
    get_client()
    get_engine().refresh_from_config()
    return cfg.model_dump()


@app.get("/api/discover")
def api_discover() -> dict[str, Any]:
    devices = discover(timeout=2.0)
    return {
        "devices": [
            {
                "host": d.host,
                "port": d.port,
                "model": d.model,
                "name": d.name,
                "fw_ver": d.fw_ver,
            }
            for d in devices
        ]
    }


@app.post("/api/connect")
def api_connect(body: HostBody | None = None) -> dict[str, Any]:
    if body and body.host:
        patch: dict[str, Any] = {"host": body.host}
        if body.tcp_port:
            patch["tcp_port"] = body.tcp_port
        update_config(patch)
    client = get_client()
    try:
        client.connect()
        snap = client.snapshot()
        return {"ok": True, "host": client.host, "state": snap}
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/state")
def api_state(force: bool = Query(False)) -> dict[str, Any]:
    global _state_cache, _state_cache_at
    import time

    client = get_client()
    engine = get_engine()
    ttl = _STATE_TTL_SYNC if engine.state.active or engine.state.running else _STATE_TTL_IDLE
    now = time.monotonic()
    if (
        not force
        and _state_cache is not None
        and now - _state_cache_at < ttl
    ):
        cached = dict(_state_cache)
        cached["cached"] = True
        return cached

    if client.quota_blocked():
        return {
            "ok": False,
            "host": client.host,
            "connected": False,
            "error": "TCP 配额冷却中，暂不查询状态",
            "state": (_state_cache or {}).get("state"),
            "cached": True,
        }

    try:
        snap = client.snapshot()
        # get_prop 偶发空串；保留本地记忆，避免 UI 电源被误刷成关
        if _state_cache and isinstance(_state_cache.get("state"), dict):
            prev = _state_cache["state"]
            for key, val in list(snap.items()):
                if val in (None, "") and prev.get(key) not in (None, ""):
                    snap[key] = prev[key]
        payload = {"ok": True, "host": client.host, "connected": True, "state": snap, "cached": False}
        _state_cache = payload
        _state_cache_at = now
        return payload
    except QuotaExceeded as exc:
        return {
            "ok": False,
            "host": client.host,
            "connected": False,
            "error": str(exc),
            "state": (_state_cache or {}).get("state"),
            "cached": True,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "host": client.host,
            "connected": False,
            "error": str(exc),
            "state": None,
            "cached": False,
        }


@app.post("/api/power")
def api_power(body: PowerBody) -> dict[str, Any]:
    client = get_client()
    try:
        if body.target == "main":
            client.set_main_power(body.on)
            state = _remember_state(
                {
                    "power": "on" if body.on else "off",
                    "main_power": "on" if body.on else "off",
                },
                client,
            )
        else:
            if _music_on():
                raise HTTPException(status_code=409, detail="音乐跃动中不能开关氛围灯")
            client.set_bg_power(body.on)
            state = _remember_state({"bg_power": "on" if body.on else "off"}, client)
        return {"ok": True, "state": state}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/bright")
def api_bright(body: BrightBody) -> dict[str, Any]:
    client = get_client()
    try:
        if body.target == "bg" and _music_on():
            update_config({"music": {"bright": body.bright}})
            get_music().refresh()
            state = _remember_state(
                {"bg_bright": body.bright, "bg_power": "on"},
                client,
            )
            return {
                "ok": True,
                "state": state,
                "base": body.bright,
                "applied": body.bright,
                "music": True,
            }
        # 手动调光 = 写入基础亮度；若该路正在亮度同步，则最终=基础+当前同步增量
        patch: dict[str, Any] = {"sync": {}}
        if body.target == "main":
            patch["sync"]["base_main_bright"] = body.bright
        else:
            patch["sync"]["base_ambient_bright"] = body.bright
        update_config(patch)
        engine = get_engine()
        applied = engine.apply_manual_base_bright(body.target, body.bright)
        if applied is None:
            if body.target == "main":
                power_on = True
                if _state_cache and isinstance(_state_cache.get("state"), dict):
                    p = str(
                        _state_cache["state"].get("main_power")
                        or _state_cache["state"].get("power")
                        or "on"
                    ).lower()
                    power_on = p == "on"
                if power_on:
                    try:
                        engine.set_main_bright_fast(body.bright)
                    except Exception:
                        client.set_main_bright(body.bright, "sudden", 0)
                else:
                    ct = 4000
                    if _state_cache and isinstance(_state_cache.get("state"), dict):
                        try:
                            ct = int(_state_cache["state"].get("ct") or 4000)
                        except (TypeError, ValueError):
                            ct = 4000
                    client.set_main_ct_scene(ct, body.bright)
                state = _remember_state(
                    {
                        "bright": body.bright,
                        "power": "on",
                        "main_power": "on",
                    },
                    client,
                )
            else:
                client.set_bg_bright(body.bright)
                state = _remember_state(
                    {"bg_bright": body.bright, "bg_power": "on"},
                    client,
                )
            return {"ok": True, "state": state, "base": body.bright, "applied": body.bright}
        key = "bright" if body.target == "main" else "bg_bright"
        state = _remember_state({key: applied}, client)
        return {
            "ok": True,
            "state": state,
            "base": body.bright,
            "applied": applied,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/ct")
def api_ct(body: CtBody) -> dict[str, Any]:
    client = get_client()
    engine = get_engine()
    try:
        power_on = True
        bright = 50
        if _state_cache and isinstance(_state_cache.get("state"), dict):
            st = _state_cache["state"]
            try:
                bright = int(st.get("bright") or 50)
            except (TypeError, ValueError):
                bright = 50
            p = str(st.get("main_power") or st.get("power") or "on").lower()
            power_on = p == "on"
        bright = max(1, min(100, bright))

        via = "tcp"
        # 关灯时先 TCP 打开（偶尔一次）；连续拖动走 UDP，避免配额耗尽后「不跟随」
        if not power_on:
            client.set_main_ct_scene(body.ct, bright)
            power_on = True
            via = "tcp_scene"
        else:
            try:
                engine.set_main_ct_fast(body.ct)
                via = "udp"
            except Exception:
                # UDP 失败再回退 TCP（可能较慢 / 占配额）
                client.set_main_ct(body.ct, "sudden", 0)
                via = "tcp"

        state = _remember_state(
            {
                "ct": body.ct,
                "bright": bright,
                "power": "on",
                "main_power": "on",
            },
            client,
        )
        return {"ok": True, "state": state, "via": via}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/bg/rgb")
def api_bg_rgb(body: RgbBody) -> dict[str, Any]:
    if _music_on():
        raise HTTPException(status_code=409, detail="音乐跃动中不能改氛围灯颜色")
    client = get_client()
    try:
        # 静态色用 scene 一条指令（自动开灯并停旧流）
        value = body.hex.strip().lstrip("#")
        rgb = int(value, 16)
        bright = int((load_config().ambient.bright or 40))
        client.set_bg_color_scene(rgb, bright)
        update_config({"ambient": {"fx": "static", "hex": body.hex}})
        state = _remember_state(
            {
                "bg_power": "on",
                "bg_bright": bright,
                "bg_rgb": rgb,
                "bg_rgb_hex": f"#{rgb:06x}",
            },
            client,
        )
        return {"ok": True, "state": state}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/bg/fx")
def api_bg_fx(body: AmbientFxBody) -> dict[str, Any]:
    if _music_on():
        raise HTTPException(status_code=409, detail="音乐跃动中不能改氛围灯效果")
    client = get_client()
    cfg = load_config()
    amb = cfg.ambient.model_dump()
    if body.bright is not None:
        amb["bright"] = body.bright
    if body.speed_ms is not None:
        amb["speed_ms"] = body.speed_ms
    if body.hex:
        amb["hex"] = body.hex
    if body.colors is not None:
        amb["colors"] = body.colors
    if body.direction:
        amb["direction"] = body.direction
    amb["fx"] = body.fx
    try:
        client.apply_ambient_fx(
            body.fx,
            bright=int(amb["bright"]),
            speed_ms=int(amb["speed_ms"]),
            hex_color=str(amb["hex"]),
            colors=list(amb.get("colors") or []),
            direction=str(amb.get("direction") or "out"),
        )
        update_config(
            {
                "ambient": amb,
                "sync": {"base_ambient_bright": int(amb["bright"])},
            }
        )
        hex_v = str(amb["hex"]).lstrip("#")
        try:
            rgb = int(hex_v, 16)
        except ValueError:
            rgb = None
        partial: dict[str, Any] = {
            "bg_power": "on",
            "bg_bright": int(amb["bright"]),
        }
        if rgb is not None:
            partial["bg_rgb"] = rgb
            partial["bg_rgb_hex"] = f"#{rgb:06x}"
        state = _remember_state(partial, client)
        return {"ok": True, "ambient": amb, "state": state}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/modes")
def api_modes() -> dict[str, Any]:
    cfg = load_config()
    return {"modes": {k: v.model_dump() for k, v in cfg.modes.items()}}


@app.patch("/api/modes/{mode_id}")
def api_patch_mode(mode_id: str, body: ModePatchBody) -> dict[str, Any]:
    cfg = load_config()
    if mode_id not in cfg.modes:
        raise HTTPException(status_code=404, detail="unknown mode")
    patch = body.model_dump(exclude_none=True)
    data = cfg.modes[mode_id].model_dump()
    data.update(patch)
    updated = update_config({"modes": {mode_id: data}})
    return {"ok": True, "mode": updated.modes[mode_id].model_dump()}


@app.post("/api/modes/{mode_id}/apply")
def api_apply_mode(mode_id: str) -> dict[str, Any]:
    cfg = load_config()
    if mode_id not in cfg.modes:
        raise HTTPException(status_code=404, detail="unknown mode")
    m = cfg.modes[mode_id]
    client = get_client()
    music_on = cfg.drive_mode == "music"
    try:
        # 主灯：开=scene 一条；关=power 一条。音乐跃动时不改氛围灯。
        if m.main_on:
            client.set_main_ct_scene(m.ct, m.bright)
        else:
            client.set_main_power(False)
        if not music_on:
            if m.bg_on:
                client.apply_ambient_fx(
                    m.bg_fx,
                    bright=m.bg_bright,
                    speed_ms=m.bg_speed_ms,
                    hex_color=m.bg_hex,
                    colors=m.bg_colors,
                    direction=m.bg_direction,
                )
            else:
                client.set_bg_power(False)
            update_config(
                {
                    "ambient": {
                        "fx": m.bg_fx,
                        "bright": m.bg_bright,
                        "speed_ms": m.bg_speed_ms,
                        "hex": m.bg_hex,
                        "colors": m.bg_colors,
                        "direction": m.bg_direction,
                    },
                    "sync": {
                        "base_main_bright": m.bright,
                        "base_ambient_bright": m.bg_bright,
                    },
                }
            )
        else:
            update_config({"sync": {"base_main_bright": m.bright}})
        try:
            bg_rgb = int(str(m.bg_hex).lstrip("#"), 16)
        except ValueError:
            bg_rgb = None
        partial: dict[str, Any] = {
            "power": "on" if m.main_on else "off",
            "main_power": "on" if m.main_on else "off",
            "bright": m.bright,
            "ct": m.ct,
        }
        if not music_on:
            partial.update(
                {
                    "bg_power": "on" if m.bg_on else "off",
                    "bg_bright": m.bg_bright,
                    "bg_rgb_hex": m.bg_hex,
                }
            )
            if bg_rgb is not None:
                partial["bg_rgb"] = bg_rgb
        state = _remember_state(partial, client)
        return {"ok": True, "mode": m.model_dump(), "state": state}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/modes/{mode_id}/reset")
def api_reset_mode(mode_id: str) -> dict[str, Any]:
    from server.config import default_modes

    defaults = default_modes()
    if mode_id not in defaults:
        raise HTTPException(status_code=404, detail="unknown mode")
    updated = update_config({"modes": {mode_id: defaults[mode_id].model_dump()}})
    return {"ok": True, "mode": updated.modes[mode_id].model_dump()}


class IinaHookBody(BaseModel):
    running: bool = True
    playing: bool = False
    pause: bool | None = None
    idle: bool | None = None
    path: str | None = None
    title: str | None = None
    event: str | None = None
    ts: float | None = None
    position: float | None = None
    duration: float | None = None
    speed: float | None = None
    seek: bool | None = None


class MediaHookBody(BaseModel):
    provider: str = "browser"
    running: bool = True
    playing: bool = False
    pause: bool | None = None
    event: str | None = None
    ts: float | None = None
    title: str | None = None
    url: str | None = None
    host: str | None = None
    fullscreen: bool | None = None
    position: float | None = None
    duration: float | None = None
    seek: bool | None = None


@app.get("/api/displays")
def api_displays() -> dict[str, Any]:
    return {"displays": [d.__dict__ for d in list_displays()]}


@app.post("/api/iina/hook")
def api_iina_hook(body: IinaHookBody) -> dict[str, Any]:
    st = update_plugin_status(body.model_dump())
    return {"ok": True, "status": status_to_dict(st)}


@app.get("/api/iina")
def api_iina() -> dict[str, Any]:
    cfg = load_config()
    st = get_iina_status(cfg.sync.iina.iina_ipc_socket)
    return status_to_dict(st)


@app.post("/api/media/hook")
def api_media_hook(body: MediaHookBody) -> dict[str, Any]:
    payload = body.model_dump()
    provider = (payload.get("provider") or "browser").lower()
    if provider == "iina":
        st = update_plugin_status(payload)
        return {"ok": True, "provider": "iina", "status": status_to_dict(st)}
    st = update_browser_status(payload)
    return {"ok": True, "provider": "browser", "status": media_status_to_dict(st)}


@app.get("/api/media")
def api_media() -> dict[str, Any]:
    cfg = load_config()
    browser = cfg.sync.browser
    st = get_browser_status(
        require_extension=browser.require_extension,
        match_hosts=list(browser.match_hosts),
        owners=list(browser.owners),
    )
    payload = media_status_to_dict(st)
    status_source = payload.pop("source", None)
    return {
        "source": "browser",
        "enabled": bool(browser.enabled),
        "status_source": status_source,
        **payload,
    }


@app.get("/api/sync/status")
def api_sync_status() -> dict[str, Any]:
    return get_engine().status_dict()


@app.post("/api/sync/enable")
def api_sync_enable(enabled: bool = Query(True)) -> dict[str, Any]:
    cfg = update_config({"sync": {"enabled": enabled}})
    get_engine().refresh_from_config()
    return {"ok": True, "sync": cfg.sync.model_dump(), "status": get_engine().status_dict()}


class DriveBody(BaseModel):
    mode: Literal["picture", "music"]


class MusicPatchBody(BaseModel):
    input: Literal["system", "mic"] | None = None
    styles: list[Literal["beat", "loudness", "spectrum"]] | None = None
    palette: Literal["builtin", "ambient"] | None = None
    bright: int | None = Field(default=None, ge=1, le=100)


@app.get("/api/music")
def api_music() -> dict[str, Any]:
    cfg = load_config()
    return {
        "drive_mode": cfg.drive_mode,
        "music": cfg.music.model_dump(),
        "status": get_music().status(),
    }


@app.patch("/api/music")
def api_music_patch(body: MusicPatchBody) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    if not patch:
        cfg = load_config()
    else:
        cfg = update_config({"music": patch})
    get_music().refresh()
    return {
        "ok": True,
        "drive_mode": cfg.drive_mode,
        "music": cfg.music.model_dump(),
        "status": get_music().status(),
    }


@app.post("/api/drive")
def api_drive(body: DriveBody) -> dict[str, Any]:
    cfg = update_config({"drive_mode": body.mode})
    music = get_music()
    try:
        if body.mode == "music":
            music.start()
        else:
            music.stop()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    cfg = load_config()
    return {
        "ok": True,
        "drive_mode": cfg.drive_mode,
        "music": cfg.music.model_dump(),
        "status": music.status(),
    }


def create_app() -> FastAPI:
    return app


def main() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "server.main:app",
        host=cfg.api_host,
        port=cfg.api_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()