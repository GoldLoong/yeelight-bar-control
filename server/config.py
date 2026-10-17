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
    name: str
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


def default_modes() -> dict[str, ModePreset]:
    return {
        "office": ModePreset(
            name="办公模式",
            main_on=True,
            bg_on=True,
            bright=100,
            ct=4000,
            bg_bright=55,
            bg_hex="#C0FF73",
            bg_fx="static",
        ),
        "game": ModePreset(
            name="竞技模式",
            main_on=True,
            bg_on=True,
            bright=100,
            ct=4000,
            bg_bright=80,
            bg_hex="#34C759",
            bg_fx="flow",
            bg_speed_ms=2800,
            bg_direction="lr",
            bg_colors=[
                "#FF0000",
                "#FF7F00",
                "#FFFF00",
                "#00FF00",
                "#00FFFF",
                "#007FFF",
                "#7F00FF",
                "#FF007F",
            ],
        ),
        "movie": ModePreset(
            name="电影模式",
            main_on=False,
            bg_on=True,
            bright=100,
            ct=4000,
            bg_bright=55,
            bg_hex="#FF2D95",
            bg_fx="breath",
            bg_speed_ms=3800,
            bg_colors=["#FF2D95", "#2A5BFF", "#AF52DE", "#34C759"],
        ),
        "chill": ModePreset(
            name="休闲模式",
            main_on=False,
            bg_on=True,
            bright=100,
            ct=4000,
            bg_bright=35,
            bg_hex="#E8D5B5",
            bg_fx="static",
            bg_speed_ms=3800,
        ),
    }


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
    styles: list[Literal["beat", "loudness", "spectrum"]] = Field(
        default_factory=lambda: ["beat"]
    )
    palette: Literal["builtin", "ambient"] = "builtin"
    bright: int = 40

    @model_validator(mode="after")
    def _normalize_styles(self) -> MusicSettings:
        allowed = ("beat", "loudness", "spectrum")
        seen: list[str] = []
        for item in self.styles or []:
            if item in allowed and item not in seen:
                seen.append(item)
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
    ambient: AmbientState = Field(default_factory=AmbientState)
    drive_mode: Literal["picture", "music"] = "picture"
    music: MusicSettings = Field(default_factory=MusicSettings)


def ensure_config() -> AppConfig:
    ensure_config_file()
    return load_config()


def load_config() -> AppConfig:
    ensure_config_file()
    data = json.loads(config_path().read_text(encoding="utf-8"))
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
