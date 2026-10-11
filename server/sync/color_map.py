"""颜色映射：默认同步直出，不做校色（校色会引入不同步/偏色）。"""

from __future__ import annotations


def map_rgb_for_yeelight(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    return (int(rgb[0]), int(rgb[1]), int(rgb[2]))
