"""把音色和起伏收成一帧氛围灯颜色和亮度。

颜色跟着曲子的冷暖走，小变化大约一秒内跟上，换段大约半秒。
节拍只在设定亮度里面轻轻亮一下，不再每拍跳一格颜色。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from server.music.analyze import Levels

# 内置色按冷暖排：暗、厚在前，亮、薄在后。不走彩虹循环。
MOOD_STOPS: tuple[int, ...] = (
    0xFF5A1F,
    0xFF7A00,
    0xFF9F0A,
    0xFF375F,
    0xE040FB,
    0x7C6CFF,
    0x40A9FF,
    0x64D2FF,
)


@dataclass
class LampFrame:
    bright: int
    left: int
    right: int
    segmented: bool


def parse_hex_color(value: str) -> int | None:
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text, 16) & 0xFFFFFF
    except ValueError:
        return None


def palette_from_settings(
    source: str,
    ambient_colors: list[str] | None,
    ambient_hex: str,
) -> list[int]:
    if source == "ambient":
        out: list[int] = []
        for item in ambient_colors or []:
            parsed = parse_hex_color(item)
            if parsed is not None and parsed not in out:
                out.append(parsed)
        if not out:
            one = parse_hex_color(ambient_hex)
            out = [one if one is not None else 0x2A3A55]
        return out
    return list(MOOD_STOPS)


class LampRenderer:
    def __init__(self) -> None:
        self.pos = 0.45
        self.env = 0.0
        self.punch = 0.0
        self.bass_s = 0.0
        self.treble_s = 0.0
        self.drive_s = 0.0
        self.fast = 0.0

    def reset(self) -> None:
        self.pos = 0.45
        self.env = 0.0
        self.punch = 0.0
        self.bass_s = 0.0
        self.treble_s = 0.0
        self.drive_s = 0.0
        self.fast = 0.0

    def step(
        self,
        levels: Levels,
        *,
        styles: list[str],
        colors: list[int],
        bright_max: int,
        dt: float,
        builtin: bool = True,
    ) -> LampFrame:
        dt = max(0.0, min(0.5, float(dt)))
        chosen = set(styles)
        beat_on = "beat" in chosen
        loud_on = "loudness" in chosen
        spec_on = "spectrum" in chosen
        if not chosen:
            beat_on = True

        target = _mood_pos(levels.centroid, levels.warmth)
        gap = abs(target - self.pos)
        if levels.retint:
            self.fast = 0.7
        if self.fast > 0:
            self.fast = max(0.0, self.fast - dt)
            tau = 0.16
        elif gap > 0.12:
            tau = 0.28
        else:
            tau = 0.55
        self.pos = _follow(self.pos, target, dt, tau)
        self.bass_s = _follow(self.bass_s, levels.bass, dt, 0.14)
        self.treble_s = _follow(self.treble_s, levels.treble, dt, 0.14)
        self.drive_s = _follow(self.drive_s, levels.drive, dt, 0.4)

        if levels.loud >= self.env:
            self.env = _follow(self.env, levels.loud, dt, 0.08)
        else:
            self.env = _follow(self.env, levels.loud, dt, 0.48)

        if beat_on and levels.beat:
            self.punch = 1.0
        else:
            release = max(0.14, min(0.42, float(levels.period) * 0.42))
            self.punch *= math.exp(-dt / release) if release > 0 else 0.0

        cap = max(1, min(100, int(bright_max)))
        cols = colors or list(MOOD_STOPS)
        drive = max(0.0, min(1.0, self.drive_s))
        left, right, segmented = self._colors(
            cols,
            builtin=builtin,
            drive=drive,
            spec_on=spec_on,
        )
        bright = self._brightness(cap, beat_on=beat_on, loud_on=loud_on, drive=drive)
        return LampFrame(bright=bright, left=left, right=right, segmented=segmented)

    def _colors(
        self,
        colors: list[int],
        *,
        builtin: bool,
        drive: float,
        spec_on: bool,
    ) -> tuple[int, int, bool]:
        if spec_on:
            left_pos = self.pos - 0.08 - 0.22 * self.bass_s
            right_pos = self.pos + 0.08 + 0.22 * self.treble_s
        else:
            left_pos = right_pos = self.pos
        left = _paint(left_pos, colors, builtin, drive)
        right = _paint(right_pos, colors, builtin, drive)
        split = spec_on and _distance(left, right) >= 28
        if not split:
            right = left
        return left, right, split

    def _brightness(self, cap: int, *, beat_on: bool, loud_on: bool, drive: float) -> int:
        if loud_on:
            shaped = self.env ** 0.75
            level = 0.38 + 0.62 * max(0.0, min(1.0, shaped))
        else:
            level = 1.0
        if beat_on:
            depth = 0.18 + 0.16 * drive
            level *= (1.0 - depth) + depth * max(0.0, min(1.0, self.punch))
        return max(1, min(cap, int(round(cap * max(0.0, min(1.0, level))))))


def _mood_pos(centroid: float, warmth: float) -> float:
    pos = float(centroid) - (float(warmth) - 0.5) * 0.45
    return max(0.0, min(1.0, pos))


def _paint(pos: float, colors: list[int], builtin: bool, drive: float) -> int:
    pos = max(0.0, min(1.0, pos))
    keep = 0.55 + 0.45 * max(0.0, min(1.0, drive))
    if builtin or len(colors) < 2:
        mood = _gradient(MOOD_STOPS, pos)
        if not builtin and colors:
            mood = _tint(colors[0], mood, 0.4)
        return _chroma(mood, keep)
    # 色表不一定按冷暖排过。先按颜色冷暖排，低音走到暖色，高音走到冷色。
    ordered = sorted(colors, key=_color_warmth)
    span = len(ordered) - 1
    return _chroma(_interp(ordered, (1.0 - pos) * span), keep)


def _color_warmth(rgb: int) -> float:
    r, g, b = _split(rgb)
    return r + 0.4 * g - b


def _follow(current: float, target: float, dt: float, tau: float) -> float:
    if tau <= 1e-4:
        return target
    gain = 1.0 - math.exp(-max(0.0, dt) / tau)
    return current + (target - current) * gain


def _split(value: int) -> tuple[int, int, int]:
    return (value >> 16) & 255, (value >> 8) & 255, value & 255


def _join(r: float, g: float, b: float) -> int:
    return (
        (max(0, min(255, int(round(r)))) << 16)
        | (max(0, min(255, int(round(g)))) << 8)
        | max(0, min(255, int(round(b))))
    )


def _tint(base: int, toward: int, amount: float) -> int:
    amount = max(0.0, min(1.0, amount))
    r1, g1, b1 = _split(base)
    r2, g2, b2 = _split(toward)
    return _join(
        r1 + (r2 - r1) * amount,
        g1 + (g2 - g1) * amount,
        b1 + (b2 - b1) * amount,
    )


def _interp(colors: list[int], pos: float) -> int:
    n = len(colors)
    if n == 1:
        return colors[0]
    x = max(0.0, min(float(n - 1), pos))
    i0 = int(x)
    if i0 >= n - 1:
        return colors[-1]
    return _tint(colors[i0], colors[i0 + 1], x - i0)


def _gradient(stops: tuple[int, ...], pos: float) -> int:
    return _interp(list(stops), max(0.0, min(1.0, pos)) * (len(stops) - 1))


def _chroma(rgb: int, keep: float) -> int:
    keep = max(0.0, min(1.0, keep))
    r, g, b = _split(rgb)
    y = 0.30 * r + 0.52 * g + 0.18 * b
    return _join(y + (r - y) * keep, y + (g - y) * keep, b + (b - y) * keep)


def _distance(a: int, b: int) -> float:
    r1, g1, b1 = _split(a)
    r2, g2, b2 = _split(b)
    return math.sqrt((r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2)
