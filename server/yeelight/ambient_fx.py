"""氛围灯动态效果控制器。

- static / breath / cycle：TCP `bg_set_scene` / `bg_start_cf`（整条）
- flow（燃彩）：FastLED 式 fade+彗星(HSV 相近色拖尾)+blur → 左右采样
  （交接文档 11.4；相近色过渡那版，保留平滑四向运动）
"""

from __future__ import annotations

import colorsys
import math
import threading
import time
from typing import Any, Callable

from server.yeelight import udp_hub
from server.yeelight.effects import (
    build_breath_flow,
    build_cycle_flow,
    build_flow_flow,
    hex_to_rgb_int,
    normalize_colors,
)
from server.yeelight.stream import smooth_color_hsv

# UI dir → demo-marquee 方向（交接文档 11.4）
_DIR_MAP = {
    "lr": "right",  # 左→右
    "rl": "left",  # 右→左
    "out": "out",
    "in": "in",
}

# 虚拟灯带格数（偶数）；越高流动越细
_CELLS = 32


def _rgb_int(r: float, g: float, b: float) -> int:
    ri = max(1, min(255, int(round(r))))
    gi = max(0, min(255, int(round(g))))
    bi = max(0, min(255, int(round(b))))
    return (ri << 16) | (gi << 8) | bi


def _hsv(h: float, s: float, v: float) -> tuple[float, float, float]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return r * 255.0, g * 255.0, b * 255.0


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _smoothstep(t: float) -> float:
    t = _clamp01(t)
    return t * t * (3.0 - 2.0 * t)


def _pingpong(t: float) -> float:
    """连续 0→1→0，避免 out/in 在周期末跳回。"""
    x = t % 2.0
    return x if x <= 1.0 else 2.0 - x


def _fade_to_black(buf: list[list[float]], amount: float) -> None:
    keep = max(0.0, min(1.0, 1.0 - amount))
    for c in buf:
        c[0] *= keep
        c[1] *= keep
        c[2] *= keep
        # 极暗底噪，避免两段采样时闪黑
        if c[0] + c[1] + c[2] < 6.0:
            c[0] = max(c[0], 2.0)
            c[1] = max(c[1], 1.5)
            c[2] = max(c[2], 1.5)


def _blur1d(buf: list[list[float]], amount: float, *, circular: bool) -> None:
    """邻格渗色；circular 时环绕，左右跑马过缝更顺。"""
    n = len(buf)
    if n < 3:
        return
    a = _clamp01(amount)
    src = [c[:] for c in buf]
    for i in range(n):
        if circular:
            l = src[(i - 1) % n]
            r = src[(i + 1) % n]
        else:
            l = src[i - 1 if i > 0 else 0]
            r = src[i + 1 if i < n - 1 else n - 1]
        m = src[i]
        for k in range(3):
            blended = 0.25 * l[k] + 0.5 * m[k] + 0.25 * r[k]
            buf[i][k] = m[k] * (1.0 - a) + blended * a


def _add_at(buf: list[list[float]], pos: float, rgb: tuple[float, float, float], scale: float) -> None:
    """亚像素写入（线性插值到相邻两格）。"""
    n = len(buf)
    if n <= 0 or scale <= 0:
        return
    # 允许轻微越界后环绕或夹紧由调用方决定；此处环绕
    p = pos % n
    i0 = int(math.floor(p)) % n
    i1 = (i0 + 1) % n
    t = p - math.floor(p)
    for idx, w in ((i0, (1.0 - t) * scale), (i1, t * scale)):
        if w <= 0:
            continue
        buf[idx][0] = min(255.0, buf[idx][0] + rgb[0] * w)
        buf[idx][1] = min(255.0, buf[idx][1] + rgb[1] * w)
        buf[idx][2] = min(255.0, buf[idx][2] + rgb[2] * w)


def _paint_comet(
    buf: list[list[float]],
    pos: float,
    hue: float,
    vel_sign: float,
) -> None:
    """
    FastLED 式彗星：头亮 + 身后渐暗；拖尾色相略落后（相邻色过渡）。
    vel_sign: +1 向右，-1 向左。
    """
    sign = 1.0 if vel_sign >= 0 else -1.0
    # (身后偏移, 亮度, 色相落后, 饱和)
    spine = (
        (0.0, 1.0, 0.0, 0.95),
        (-0.7 * sign, 0.78, 0.018, 0.92),
        (-1.5 * sign, 0.55, 0.036, 0.88),
        (-2.5 * sign, 0.32, 0.055, 0.82),
        (-3.7 * sign, 0.16, 0.075, 0.75),
        (-5.0 * sign, 0.07, 0.095, 0.68),
    )
    for dist, scale, hue_back, sat in spine:
        _add_at(buf, pos + dist, _hsv(hue - hue_back, sat, 1.0), scale)


def _motion_state(direction: str, n: int, laps: float) -> list[tuple[float, float, float]]:
    """返回 [(pos, vel_sign, hue_shift), ...]。out/in 用 ping-pong 避免瞬移。"""
    mid = (n - 1) / 2.0
    span = float(n)
    if direction == "right":
        phase = laps % 1.0
        return [(phase * span, 1.0, 0.0)]
    if direction == "left":
        phase = laps % 1.0
        return [((1.0 - phase) * span, -1.0, 0.0)]
    if direction == "out":
        u = _smoothstep(_pingpong(laps))
        travel = u * mid
        expanding = (laps % 2.0) < 1.0
        s_left = -1.0 if expanding else 1.0
        s_right = 1.0 if expanding else -1.0
        return [
            (mid - travel, s_left, -0.03),
            (mid + travel, s_right, 0.03),
        ]
    if direction == "in":
        u = _smoothstep(_pingpong(laps))
        travel = u * mid
        contracting = (laps % 2.0) < 1.0
        s_left = 1.0 if contracting else -1.0
        s_right = -1.0 if contracting else 1.0
        return [
            (0.0 + travel, s_left, -0.03),
            ((n - 1) - travel, s_right, 0.03),
        ]
    return [(0.0, 1.0, 0.0)]


def _sample_halves_buf(
    buf: list[list[float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    n = len(buf)
    mid = n // 2

    def avg(part: list[list[float]]) -> tuple[float, float, float]:
        if not part:
            return (10.0, 6.0, 4.0)
        acc = [0.0, 0.0, 0.0]
        wsum = 0.0
        for c in part:
            lum = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
            w = 0.45 + (lum / 255.0) ** 1.1
            acc[0] += c[0] * w
            acc[1] += c[1] * w
            acc[2] += c[2] * w
            wsum += w
        inv = 1.0 / max(1e-6, wsum)
        return (max(5.0, acc[0] * inv), max(3.0, acc[1] * inv), max(2.0, acc[2] * inv))

    return avg(buf[:mid]), avg(buf[mid:])


class AmbientFxController:
    """全局单例：管理 CF 与分段燃彩线程。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hold_cb: Callable[[bool], None] | None = None
        self._active_fx: str = "static"
        self._writes_paused = False

    def set_hold_callback(self, cb: Callable[[bool], None] | None) -> None:
        self._hold_cb = cb

    @property
    def active_fx(self) -> str:
        return self._active_fx

    def pause_writes(self) -> None:
        """开关 TCP 期间暂停燃彩 UDP，不销毁效果线程。"""
        self._writes_paused = True

    def resume_writes(self) -> None:
        self._writes_paused = False

    def stop(self) -> None:
        with self._lock:
            self._stop_runner_locked()
            self._active_fx = "static"
            self._set_hold(False)

    def apply(
        self,
        client: Any,
        fx: str,
        *,
        bright: int = 50,
        speed_ms: int = 3800,
        hex_color: str = "#2A5BFF",
        colors: list[str] | None = None,
        direction: str = "out",
        host: str | None = None,
        udp_port: int = 55444,
    ) -> None:
        bright = max(1, min(100, int(bright)))
        speed_ms = max(400, min(12000, int(speed_ms)))
        rgb = hex_to_rgb_int(hex_color)
        fx = (fx or "static").strip().lower()

        with self._lock:
            self._stop_runner_locked()

            if fx == "static" or not fx:
                try:
                    client.bg_stop_cf()
                except Exception:
                    pass
                client.set_bg_color_scene(rgb, bright)
                self._active_fx = "static"
                self._set_hold(False)
                return

            # 动态效果期间禁止跟色盖掉氛围灯
            self._set_hold(True)
            self._active_fx = fx

            # 换速率/效果时先停旧 CF，否则固件可能沿用上一段时序
            try:
                client.bg_stop_cf()
            except Exception:
                pass

            if fx == "breath":
                expr = build_breath_flow(rgb=rgb, bright=bright, speed_ms=speed_ms)
                client.set_bg_cf_scene(0, 1, expr)
                return

            if fx == "cycle":
                cols = normalize_colors(colors, hex_color)
                expr = build_cycle_flow(
                    colors=cols,
                    bright=bright,
                    speed_ms=speed_ms,
                    reverse=direction in ("rl", "in"),
                )
                client.set_bg_cf_scene(0, 1, expr)
                return

            # flow / 燃彩：双色分段流水（UDP set_segment_rgb）
            try:
                client.set_bg_power(True, "sudden", 0)
            except Exception:
                pass
            try:
                client.set_bg_bright(bright, "sudden", 0)
            except Exception:
                pass

            lamp_host = host or getattr(client, "host", None)
            if not lamp_host:
                # 无 host 时回退整条 CF（至少有颜色变化）
                dir_ = direction if direction in ("lr", "rl", "in", "out") else "out"
                expr = build_flow_flow(bright=bright, speed_ms=speed_ms, direction=dir_)
                client.set_bg_cf_scene(0, 1, expr)
                return

            self._stop.clear()
            # speed_ms = 一整趟扫光周期；下限要够快，上限要够慢，拖动才有感
            period = max(0.35, min(8.0, speed_ms / 1000.0))
            dir_key = _DIR_MAP.get(direction, "out")
            self._thread = threading.Thread(
                target=self._marquee_loop,
                name="ambient-fx-marquee",
                args=(lamp_host, int(udp_port), dir_key, period, bright),
                daemon=True,
            )
            self._thread.start()

    def _stop_runner_locked(self) -> None:
        self._stop.set()
        t = self._thread
        self._thread = None
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.2)

    def _set_hold(self, hold: bool) -> None:
        cb = self._hold_cb
        if cb is not None:
            try:
                cb(hold)
            except Exception:
                pass

    def _marquee_loop(
        self,
        host: str,
        udp_port: int,
        direction: str,
        period: float,
        bright: int,
    ) -> None:
        """
        回退到「相近色过渡」那版：fade → HSV 彗星（拖尾色相微退）→ blur → 采样。
        方向仍用后来的 ping-pong / 定向拖尾，不再用 8 色色板底光。
        """
        hz = 24.0
        interval = 1.0 / hz
        n = _CELLS
        buf = [[0.0, 0.0, 0.0] for _ in range(n)]
        t0 = time.monotonic()
        prev_l: tuple[int, int, int] | None = None
        prev_r: tuple[int, int, int] | None = None
        circular = direction in ("left", "right")
        # 一圈只走约 1/3 色环，帧与帧之间都是相近色
        hue_per_lap = 0.28
        try:
            sess = udp_hub.get_udp(host, udp_port)
        except Exception:
            return

        _ = bright
        while not self._stop.wait(0):
            if self._writes_paused:
                time.sleep(interval)
                continue
            elapsed = time.monotonic() - t0
            period_s = max(0.45, period)
            laps = elapsed / period_s
            hue = laps * hue_per_lap

            _fade_to_black(buf, 0.14)
            for pos, vel, hshift in _motion_state(direction, n, laps):
                _paint_comet(buf, pos, hue + hshift, vel)
            _blur1d(buf, 0.78, circular=circular)
            _blur1d(buf, 0.55, circular=circular)

            raw_l, raw_r = _sample_halves_buf(buf)
            left_i = (
                int(round(raw_l[0])),
                int(round(raw_l[1])),
                int(round(raw_l[2])),
            )
            right_i = (
                int(round(raw_r[0])),
                int(round(raw_r[1])),
                int(round(raw_r[2])),
            )
            smooth_l = smooth_color_hsv(prev_l, left_i, alpha=0.28)
            smooth_r = smooth_color_hsv(prev_r, right_i, alpha=0.28)
            prev_l, prev_r = smooth_l, smooth_r
            try:
                sess.set_segment_rgb(_rgb_int(*smooth_l), _rgb_int(*smooth_r))
            except Exception:
                break
            if self._stop.wait(interval):
                break


_controller: AmbientFxController | None = None


def get_ambient_fx() -> AmbientFxController:
    global _controller
    if _controller is None:
        _controller = AmbientFxController()
    return _controller
