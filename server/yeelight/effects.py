"""氛围灯效果：静态 / 呼吸 / 循环 / 燃彩（基于 bg_start_cf）。"""

from __future__ import annotations

from typing import Iterable, Literal


FxKind = Literal["static", "breath", "cycle", "flow"]
FlowDir = Literal["lr", "rl", "in", "out"]


def hex_to_rgb_int(hex_color: str) -> int:
    value = hex_color.strip().lstrip("#")
    return max(0, min(0xFFFFFF, int(value, 16)))


def _clamp_bright(v: int) -> int:
    return max(1, min(100, int(v)))


def _clamp_speed(ms: int) -> int:
    return max(400, min(12000, int(ms)))


def build_breath_flow(*, rgb: int, bright: int, speed_ms: int) -> str:
    """同一颜色亮暗呼吸（近熄灭→再亮起）。speed ≈ 一整次呼吸周期。"""
    bright = _clamp_bright(bright)
    half = max(300, _clamp_speed(speed_ms) // 2)
    lo = 1
    hi = bright
    return f"{half},1,{rgb},{lo},{half},1,{rgb},{hi}"


def build_cycle_flow(
    *,
    colors: Iterable[int],
    bright: int,
    speed_ms: int,
    reverse: bool = False,
) -> str:
    """按色表依次循环。speed ≈ 整圈总时长。"""
    bright = _clamp_bright(bright)
    cols = [int(c) & 0xFFFFFF for c in colors]
    if not cols:
        cols = [0x2A5BFF, 0xAF52DE, 0x34C759, 0xFF9500]
    if reverse:
        cols = list(reversed(cols))
    step = max(300, _clamp_speed(speed_ms) // max(1, len(cols)))
    parts: list[str] = []
    for c in cols:
        parts.extend([str(step), "1", str(c), str(bright)])
    return ",".join(parts)


def build_flow_flow(
    *,
    bright: int,
    speed_ms: int,
    direction: FlowDir = "out",
) -> str:
    """
    燃彩：彩虹色流。
    direction 影响色序/节奏（灯条无真正左右扫光时用色序近似）。
    """
    bright = _clamp_bright(bright)
    # 8 段彩虹
    hues = [0, 30, 60, 120, 180, 210, 270, 330]
    if direction in ("rl", "in"):
        hues = list(reversed(hues))
    step = max(200, _clamp_speed(speed_ms) // len(hues))
    if direction == "in":
        step = max(160, int(step * 0.75))
    if direction == "out":
        step = max(160, int(step * 0.9))

    parts: list[str] = []
    for h in hues:
        # HSV → rough RGB
        import colorsys

        r, g, b = colorsys.hsv_to_rgb(h / 360.0, 1.0, 1.0)
        rgb = (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)
        parts.extend([str(step), "1", str(rgb), str(bright)])
    return ",".join(parts)


def normalize_colors(hex_list: list[str] | None, fallback_hex: str) -> list[int]:
    out: list[int] = []
    for h in hex_list or []:
        try:
            out.append(hex_to_rgb_int(h))
        except ValueError:
            continue
    if not out:
        out = [hex_to_rgb_int(fallback_hex)]
    return out
