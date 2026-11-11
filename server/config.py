from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_ID = "com.yeelightbar.control"


def _config_dir() -> Path:
    env = (os.environ.get("YEELIGHT_CONFIG_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / BUNDLE_ID


def config_path() -> Path:
    return _config_dir() / "config.json"


def example_path() -> Path:
    env = (os.environ.get("YEELIGHT_EXAMPLE_CONFIG") or "").strip()
    if env:
        p = Path(env).expanduser()
        if p.exists():
            return p
    return ROOT / "config.example.json"


# 兼容旧引用（值在 ensure 时再解析目录）
CONFIG_PATH = config_path()
EXAMPLE_PATH = example_path()


class SyncChannels(BaseModel):
    ambient_color: bool = True
    main_brightness: bool = False
    ambient_brightness: bool = False


class SourceSyncSettings(BaseModel):
    """单个媒体源的同步参数（IINA / 浏览器各自一份）。"""

    enabled: bool = True
    channels: SyncChannels = Field(default_factory=SyncChannels)
    display_id: int | None = None
    region: Literal["top", "full", "window"] = "window"
    color_smooth: float = 0.20
    main_bright_smooth: float = 0.20
    ambient_bright_smooth: float = 0.20
    base_main_bright: int = 50
    base_ambient_bright: int = 40
    main_bright_range: int = 5
    ambient_bright_range: int = 30
    segment_color: bool = False
    on_pause: Literal["keep_last"] = "keep_last"
    on_exit: Literal["ambient_mode", "restore", "off_ambient"] = "ambient_mode"
    exit_mode: str = "ambient_mode"


class IinaSyncSettings(SourceSyncSettings):
    enabled: bool = True
    iina_ipc_socket: str | None = None


class BrowserSyncSettings(SourceSyncSettings):
    """浏览器媒体源：扩展报态 + 截窗取色。"""

    enabled: bool = False
    # 浏览器默认跟播放器中部，避免「窗口顶部」采到标签栏/网页顶栏
    region: Literal["top", "full", "window"] = "window"
    # 浏览器默认更跟手、少抹灰
    color_smooth: float = 0.08
    owners: list[str] = Field(
        default_factory=lambda: [
            "Google Chrome",
            "Chromium",
            "Microsoft Edge",
            "Brave Browser",
            "Safari",
            "Orion",
        ]
    )
    match_hosts: list[str] = Field(
        default_factory=lambda: [
            "www.bilibili.com",
            "m.bilibili.com",
            "www.bilibili.tv",
        ]
    )
    require_extension: bool = True


class UserRect(BaseModel):
    display_id: int | None = None
    left: int = 0
    top: int = 0
    width: int = 200
    height: int = 120


class ScreenSyncSettings(SourceSyncSettings):
    """屏幕媒体源：整屏 / 前台窗 / 自选区域取色。"""

    enabled: bool = False
    # 屏幕更在意跟手：默认低于 IINA/浏览器
    color_smooth: float = 0.12
    capture_target: Literal["display", "front_window", "user_rect"] = "front_window"
    user_rect: UserRect | None = None
    # always / target_valid / fullscreen_only（多选 AND）
    eligibility: list[str] = Field(default_factory=lambda: ["target_valid"])


DEFAULT_SYNC_PRIORITY: list[str] = ["iina", "browser", "screen"]


def _normalize_priority(raw: Any) -> list[str]:
    allowed = ("iina", "browser", "screen")
    out: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            s = str(item).lower().strip()
            if s in allowed and s not in out:
                out.append(s)
    for s in DEFAULT_SYNC_PRIORITY:
        if s not in out:
            out.append(s)
    return out


def _copy_flat_into_source(flat: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "channels",
        "display_id",
        "region",
        "color_smooth",
        "main_bright_smooth",
        "ambient_bright_smooth",
        "base_main_bright",
        "base_ambient_bright",
        "main_bright_range",
        "ambient_bright_range",
        "segment_color",
        "on_pause",
        "on_exit",
        "exit_mode",
    )
    out: dict[str, Any] = {}
    for k in keys:
        if k in flat and flat[k] is not None:
            out[k] = flat[k]
    return out


class SyncConfig(BaseModel):
    enabled: bool = True
    priority: list[str] = Field(default_factory=lambda: list(DEFAULT_SYNC_PRIORITY))
    # 取色后端：auto/mss=稳定 mss；stream=SCStream；sck=旧截图 API（易漏内存，仅调试）
    capture_backend: Literal["auto", "mss", "stream", "sck"] = "auto"
    iina: IinaSyncSettings = Field(default_factory=IinaSyncSettings)
    browser: BrowserSyncSettings = Field(default_factory=BrowserSyncSettings)
    screen: ScreenSyncSettings = Field(default_factory=ScreenSyncSettings)

    # —— 以下为旧版扁平字段，仅迁移兼容，新写入以嵌套源为准 ——
    iina_enabled: bool | None = None
    browser_enabled: bool | None = None
    source: Literal["iina", "browser", "screen"] | None = None
    channels: SyncChannels | None = None
    display_id: int | None = None
    region: Literal["top", "full", "window"] | None = None
    fps: int | None = None
    color_smooth: float | None = None
    main_bright_smooth: float | None = None
    ambient_bright_smooth: float | None = None
    base_main_bright: int | None = None
    base_ambient_bright: int | None = None
    main_bright_range: int | None = None
    ambient_bright_range: int | None = None
    on_pause: Literal["keep_last"] | None = None
    on_exit: Literal["ambient_mode", "restore", "off_ambient"] | None = None
    exit_mode: str | None = None
    iina_ipc_socket: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_nested(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        flat_bits = _copy_flat_into_source(data)

        # 旧 browser 可能是只有 owners 的小对象
        raw_browser = data.get("browser")
        if isinstance(raw_browser, dict):
            browser = dict(flat_bits)
            browser.update(raw_browser)
        else:
            browser = dict(flat_bits)

        raw_iina = data.get("iina")
        if isinstance(raw_iina, dict):
            iina = dict(flat_bits)
            iina.update(raw_iina)
        else:
            iina = dict(flat_bits)
            if data.get("iina_ipc_socket") is not None:
                iina["iina_ipc_socket"] = data.get("iina_ipc_socket")

        raw_screen = data.get("screen")
        if isinstance(raw_screen, dict):
            screen = dict(flat_bits)
            screen.update(raw_screen)
            screen.setdefault("enabled", False)
            screen.setdefault("capture_target", "front_window")
            screen.setdefault("eligibility", ["target_valid"])
        else:
            screen = {
                "enabled": False,
                "capture_target": "front_window",
                "eligibility": ["target_valid"],
                **{k: v for k, v in flat_bits.items() if k in (
                    "channels", "display_id", "region", "color_smooth",
                    "main_bright_smooth", "ambient_bright_smooth",
                    "base_main_bright", "base_ambient_bright",
                    "main_bright_range", "ambient_bright_range",
                    "on_exit", "exit_mode",
                )},
            }

        # 开关迁移
        if "enabled" not in iina:
            if "iina_enabled" in data:
                iina["enabled"] = bool(data["iina_enabled"])
            elif data.get("source") == "browser":
                iina["enabled"] = False
            elif data.get("source") == "iina":
                iina["enabled"] = True
        if "enabled" not in browser:
            if "browser_enabled" in data:
                browser["enabled"] = bool(data["browser_enabled"])
            elif data.get("source") == "browser":
                browser["enabled"] = True
            elif data.get("source") == "iina":
                browser["enabled"] = False

        data["iina"] = iina
        data["browser"] = browser
        data["screen"] = screen
        data["priority"] = _normalize_priority(data.get("priority"))
        return data

    def params_for(self, source: str) -> SourceSyncSettings:
        if source == "browser":
            return self.browser
        if source == "screen":
            return self.screen
        return self.iina


class ModePreset(BaseModel):
    """旋钮/App 四模式：主灯(CT)+氛围灯(RGB/灯效)，对齐 YLTD003 / lamp15 官方语义。"""

    name: str
    # 一句话说明（卡片副标题 / 模式详情）
    summary: str = ""
    # 用途标签：work / game / cinema / relax
    intent: Literal["work", "game", "cinema", "relax"] = "work"
    main_on: bool = True
    bg_on: bool = True
    bright: int = 50
    ct: int = 4000
    bg_bright: int = 40
    bg_hex: str = "#2A3A55"
    bg_fx: Literal["static", "breath", "cycle", "flow"] = "static"
    bg_speed_ms: int = 3800
    bg_colors: list[str] = Field(
        default_factory=lambda: ["#2A5BFF", "#AF52DE", "#34C759", "#FF9500"]
    )
    bg_direction: Literal["lr", "rl", "in", "out"] = "out"


# 出厂默认版本；升版时把「仍等于旧出厂值」的模式自动换成新默认，用户改过的保留
MODES_REV = 3

# 彩虹流光色序（官方燃彩/流光常见 8 色）
_FLOW_RAINBOW = [
    "#FF003C",
    "#FF7A00",
    "#FFD400",
    "#3DFF6E",
    "#00E5FF",
    "#2F6BFF",
    "#8B3DFF",
    "#FF2BD6",
]


def default_modes() -> dict[str, ModePreset]:
    """复刻米家插件 / 旋钮四模式（YLTD003 / lamp15）。

    依据：
    - 说明书：出厂四种模式，旋钮双击切换；电影模式关屏幕灯只留氛围灯
    - 米家插件：Office / Gaming / Movie / Entertainment，可改名与主/氛围参数
    - 协议：主灯仅 CT（set_scene ct）；氛围灯 RGB / bg_set_scene / cf 流光
    具体数值未公开 JSON，按官方语义 + 米家常见出厂观感标定（rev3）。
    """
    return {
        "office": ModePreset(
            name="办公模式",
            summary="主灯全亮护眼 · 青柠静态氛围",
            intent="work",
            main_on=True,
            bg_on=True,
            bright=100,
            ct=4000,
            bg_bright=50,
            bg_hex="#C0FF73",
            bg_fx="static",
        ),
        "game": ModePreset(
            name="竞技模式",
            summary="主灯略冷提神 · 幻彩流光",
            intent="game",
            main_on=True,
            bg_on=True,
            bright=90,
            ct=5000,
            bg_bright=85,
            bg_hex="#3DFF6E",
            bg_fx="flow",
            bg_speed_ms=1800,
            bg_direction="lr",
            bg_colors=list(_FLOW_RAINBOW),
        ),
        "movie": ModePreset(
            name="电影模式",
            summary="关掉屏幕灯 · 低干扰偏置光",
            intent="cinema",
            main_on=False,
            bg_on=True,
            bright=35,
            ct=3000,
            bg_bright=28,
            bg_hex="#3D2A66",
            bg_fx="static",
            bg_speed_ms=4200,
        ),
        "chill": ModePreset(
            name="休闲模式",
            summary="暖光低亮 · 安静奶油氛围",
            intent="relax",
            main_on=True,
            bg_on=True,
            bright=45,
            ct=3000,
            bg_bright=40,
            bg_hex="#E8C9A0",
            bg_fx="static",
            bg_speed_ms=4000,
        ),
    }


def _mode_core(m: dict[str, Any]) -> dict[str, Any]:
    """比较出厂指纹时忽略文案类字段。"""
    keys = (
        "main_on",
        "bg_on",
        "bright",
        "ct",
        "bg_bright",
        "bg_hex",
        "bg_fx",
        "bg_speed_ms",
        "bg_direction",
    )
    out = {k: m.get(k) for k in keys}
    if "bg_hex" in out and isinstance(out["bg_hex"], str):
        out["bg_hex"] = out["bg_hex"].upper()
    return out


def _legacy_factory_modes_v1() -> dict[str, dict[str, Any]]:
    """rev=1 出厂值，用于判断用户是否改过。"""
    return {
        "office": {
            "main_on": True,
            "bg_on": True,
            "bright": 100,
            "ct": 4000,
            "bg_bright": 55,
            "bg_hex": "#C0FF73",
            "bg_fx": "static",
            "bg_speed_ms": 3800,
            "bg_direction": "out",
        },
        "game": {
            "main_on": True,
            "bg_on": True,
            "bright": 100,
            "ct": 4000,
            "bg_bright": 80,
            "bg_hex": "#34C759",
            "bg_fx": "flow",
            "bg_speed_ms": 2800,
            "bg_direction": "lr",
        },
        "movie": {
            "main_on": False,
            "bg_on": True,
            "bright": 100,
            "ct": 4000,
            "bg_bright": 55,
            "bg_hex": "#FF2D95",
            "bg_fx": "breath",
            "bg_speed_ms": 3800,
            "bg_direction": "out",
        },
        "chill": {
            "main_on": False,
            "bg_on": True,
            "bright": 100,
            "ct": 4000,
            "bg_bright": 35,
            "bg_hex": "#E8D5B5",
            "bg_fx": "static",
            "bg_speed_ms": 3800,
            "bg_direction": "out",
        },
    }


def _legacy_factory_modes_v2() -> dict[str, dict[str, Any]]:
    """rev=2 出厂值。"""
    return {
        "office": {
            "main_on": True,
            "bg_on": True,
            "bright": 100,
            "ct": 4000,
            "bg_bright": 50,
            "bg_hex": "#C0FF73",
            "bg_fx": "static",
            "bg_speed_ms": 3800,
            "bg_direction": "out",
        },
        "game": {
            "main_on": True,
            "bg_on": True,
            "bright": 90,
            "ct": 4500,
            "bg_bright": 85,
            "bg_hex": "#3DFF6E",
            "bg_fx": "flow",
            "bg_speed_ms": 1800,
            "bg_direction": "lr",
        },
        "movie": {
            "main_on": False,
            "bg_on": True,
            "bright": 35,
            "ct": 3000,
            "bg_bright": 32,
            "bg_hex": "#4B3B78",
            "bg_fx": "static",
            "bg_speed_ms": 4200,
            "bg_direction": "out",
        },
        "chill": {
            "main_on": True,
            "bg_on": True,
            "bright": 42,
            "ct": 3200,
            "bg_bright": 38,
            "bg_hex": "#E8C9A0",
            "bg_fx": "static",
            "bg_speed_ms": 4000,
            "bg_direction": "out",
        },
    }


def _legacy_fingerprint(rev: int) -> dict[str, dict[str, Any]]:
    if rev <= 1:
        return _legacy_factory_modes_v1()
    if rev == 2:
        return _legacy_factory_modes_v2()
    return {}


def migrate_modes_data(data: dict[str, Any]) -> bool:
    """若需要，把未改动的旧出厂模式升到 MODES_REV。返回是否改写了 data。"""
    rev = int(data.get("modes_rev") or 1)
    if rev >= MODES_REV:
        # 补全缺字段（summary/intent）但不覆盖用户数值
        modes = data.get("modes")
        if not isinstance(modes, dict):
            return False
        changed = False
        defaults = {k: v.model_dump() for k, v in default_modes().items()}
        for mid, preset in defaults.items():
            cur = modes.get(mid)
            if not isinstance(cur, dict):
                modes[mid] = preset
                changed = True
                continue
            for meta_key in ("summary", "intent", "name"):
                if not cur.get(meta_key) and preset.get(meta_key):
                    cur[meta_key] = preset[meta_key]
                    changed = True
        if changed:
            data["modes"] = modes
        return changed

    modes = data.get("modes")
    if not isinstance(modes, dict):
        modes = {}
    # 与「当前配置所在 rev 的出厂指纹」比较；也兼容仍停在更早出厂值的情况
    fingerprints = [_legacy_fingerprint(rev)]
    if rev > 1:
        fingerprints.append(_legacy_factory_modes_v1())
    if rev > 2:
        fingerprints.append(_legacy_factory_modes_v2())
    defaults = {k: v.model_dump() for k, v in default_modes().items()}
    for mid, preset in defaults.items():
        cur = modes.get(mid)
        is_factory = not isinstance(cur, dict) or any(
            _mode_core(cur) == _mode_core(fp.get(mid, {})) for fp in fingerprints if fp
        )
        if is_factory:
            modes[mid] = preset
        else:
            # 用户改过：只补文案
            for meta_key in ("summary", "intent"):
                if not cur.get(meta_key):
                    cur[meta_key] = preset.get(meta_key)
            if not cur.get("name"):
                cur["name"] = preset["name"]
            modes[mid] = cur
    data["modes"] = modes
    data["modes_rev"] = MODES_REV
    return True


class AmbientState(BaseModel):
    fx: Literal["static", "breath", "cycle", "flow"] = "static"
    bright: int = 40
    speed_ms: int = 3800
    hex: str = "#2A3A55"
    colors: list[str] = Field(
        default_factory=lambda: ["#2A5BFF", "#AF52DE", "#34C759", "#FF9500"]
    )
    direction: Literal["lr", "rl", "in", "out"] = "out"


class OnboardingState(BaseModel):
    complete: bool = False


class MusicSettings(BaseModel):
    """音乐跃动。亮度和追色用的氛围灯亮度分开记。"""

    input: Literal["system", "mic"] = "system"
    styles: list[Literal["beat", "loudness", "spectrum", "beat_swap", "night"]] = Field(
        default_factory=lambda: ["beat"]
    )
    palette: Literal["builtin", "ambient"] = "builtin"
    bright: int = 40
    # 彩蛋「夜色模式」：三击小点后常显入口；toast 是否已提示过
    night_mode_entry_visible: bool = False
    night_mode_toast_seen: bool = False

    @model_validator(mode="after")
    def _normalize_styles(self) -> MusicSettings:
        allowed = ("beat", "loudness", "spectrum", "beat_swap", "night")
        seen: list[str] = []
        for item in self.styles or []:
            if item in allowed and item not in seen:
                seen.append(item)
        # 夜色独占：勾选后只保留 night，避免和普通效果混跑
        if "night" in seen:
            self.styles = ["night"]  # type: ignore[assignment]
        else:
            self.styles = seen or ["beat"]  # type: ignore[assignment]
        self.bright = max(1, min(100, int(self.bright)))
        return self


class AppConfig(BaseModel):
    host: str = "192.168.0.116"
    tcp_port: int = 55443
    udp_port: int = 55444
    api_host: str = "127.0.0.1"
    api_port: int = 8787
    launch_at_login: bool = False
    onboarding: OnboardingState = Field(default_factory=OnboardingState)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    modes: dict[str, ModePreset] = Field(default_factory=default_modes)
    modes_rev: int = MODES_REV
    ambient: AmbientState = Field(default_factory=AmbientState)
    drive_mode: Literal["picture", "music"] = "picture"
    music: MusicSettings = Field(default_factory=MusicSettings)


def ensure_config() -> AppConfig:
    ensure_config_file()
    return load_config()


def load_config() -> AppConfig:
    ensure_config_file()
    path = config_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    if migrate_modes_data(data):
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return AppConfig.model_validate(data)


def _legacy_config_candidates() -> list[Path]:
    out: list[Path] = []
    env = (os.environ.get("YEELIGHT_LEGACY_CONFIG") or "").strip()
    if env:
        out.append(Path(env).expanduser())
    # 开发态：仓库里的旧 config.json（打包进 .app 则不要当用户配置）
    bundled = ".app/Contents/" in str(ROOT) or str(ROOT).endswith(".app/Contents/Resources")
    if not bundled:
        out.append(ROOT / "config.json")
    return out


def ensure_config_file() -> None:
    dest = config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    for src in _legacy_config_candidates():
        if src.exists() and src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
            try:
                data = json.loads(dest.read_text(encoding="utf-8"))
                if data.get("host") and not (data.get("onboarding") or {}).get("complete"):
                    data.setdefault("onboarding", {})["complete"] = True
                    dest.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            except (OSError, json.JSONDecodeError, TypeError):
                pass
            return
    example = example_path()
    if example.exists():
        shutil.copy2(example, dest)
        return
    dest.write_text(AppConfig().model_dump_json(indent=2) + "\n", encoding="utf-8")


def save_config(cfg: AppConfig) -> None:
    # 落盘时去掉易混淆的旧扁平字段，只保留 nested
    data = cfg.model_dump()
    sync = data.get("sync") or {}
    for k in (
        "iina_enabled",
        "browser_enabled",
        "source",
        "channels",
        "display_id",
        "region",
        "fps",
        "color_smooth",
        "main_bright_smooth",
        "ambient_bright_smooth",
        "base_main_bright",
        "base_ambient_bright",
        "main_bright_range",
        "ambient_bright_range",
        "on_pause",
        "on_exit",
        "exit_mode",
        "iina_ipc_socket",
    ):
        sync.pop(k, None)
    data["sync"] = sync
    dest = config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def update_config(patch: dict[str, Any]) -> AppConfig:
    cfg = load_config()
    data = cfg.model_dump()
    _deep_merge(data, patch)
    updated = AppConfig.model_validate(data)
    save_config(updated)
    return updated


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
