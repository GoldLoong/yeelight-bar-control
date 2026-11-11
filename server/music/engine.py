"""音乐跃动运行时：占住氛围灯，追色暂停往灯上写。"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from server.config import AppConfig, load_config
from server.music.analyze import LevelTracker
from server.music.capture import AudioInput, last_system_backend
from server.music.night import NightRenderer
from server.music.render import LampRenderer, palette_from_settings
from server.yeelight import udp_hub
from server.yeelight.client import YeelightClient


class MusicEngine:
    def __init__(
        self,
        client_factory: Callable[[], YeelightClient],
        get_config: Callable[[], AppConfig] | None = None,
        sync_hold: Callable[[bool], None] | None = None,
        picture_active: Callable[[], bool] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._get_config = get_config or load_config
        self._sync_hold = sync_hold or (lambda _on: None)
        self._picture_active = picture_active or (lambda: False)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._audio = AudioInput()
        self._tracker = LevelTracker()
        self._renderer = LampRenderer()
        self._night = NightRenderer()
        self._snapshot: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._running = False
        self._error: str | None = None
        # 音乐跃动仍运行，但用户可临时关掉氛围灯输出（开关）
        self._output_on = True
        self._loud = 0.0
        self._beat = False
        self._last_sent: tuple[int, int, int, bool] | None = None
        self._last_send_at = 0.0
        self._night_pending: tuple[AppConfig, int, int, int, bool, tuple[int, int, int, bool]] | None = None
        self._cfg: AppConfig | None = None
        self._cfg_at = 0.0
        self._started_at = 0.0
        self._last_tick_at = 0.0
        self._last_tick_ms = 0.0

    def status(self) -> dict[str, Any]:
        cfg = self._config()
        now = time.monotonic()
        audio_age = None if not self._running else self._audio.ring.age_ms()
        out: dict[str, Any] = {
            "running": self._running,
            "output_on": self._output_on,
            "input": cfg.music.input,
            "error": self._audio.error or self._error,
            "loud": 0.0 if not self._running else round(self._loud, 3),
            "beat": False if not self._running else self._beat,
            "packets": 0 if not self._running else int(self._audio.packets),
            "peak": 0.0 if not self._running else round(float(self._audio.peak), 4),
            "system_backend": last_system_backend(),
            "latency": {
                "tick_ms": 0.0 if not self._running else round(self._last_tick_ms, 1),
                "audio_age_ms": None if audio_age is None else round(audio_age, 1),
                "send_ago_ms": (
                    None
                    if (not self._running) or self._last_send_at <= 0
                    else round(max(0.0, (now - self._last_send_at) * 1000.0), 1)
                ),
                "loop_ms": 33,
            },
        }
        if "night" in list(cfg.music.styles or []):
            out["night"] = self._night.ui_status()
        return out

    def refresh(self) -> None:
        self._cfg = None

    @property
    def output_on(self) -> bool:
        return bool(self._output_on)

    def mute_writes(self) -> bool:
        """只停 UDP 写灯，不发 TCP 开关（给主灯开关刹流用，避免氛围闪灭）。"""
        with self._lock:
            was = bool(self._output_on)
            self._output_on = False
            self._last_sent = None
            self._night_pending = None
            return was

    def unmute_writes(self) -> None:
        """恢复 UDP 写灯，不发 TCP 开关。"""
        with self._lock:
            self._output_on = True
            self._last_sent = None
            self._night_pending = None

    def set_output_enabled(self, on: bool) -> None:
        """音乐跃动中临时开关氛围灯：关=停写并熄灭；开=恢复输出。"""
        want = bool(on)
        with self._lock:
            if self._output_on == want:
                return
            self._output_on = want
            self._last_sent = None
            self._night_pending = None
        client = self._client_factory()
        try:
            if want:
                client.set_bg_power(True, "sudden", 0)
            else:
                try:
                    client.bg_stop_cf()
                except Exception:
                    pass
                client.set_bg_power(False, "sudden", 0)
        except Exception as exc:  # noqa: BLE001
            self._error = f"氛围灯开关失败：{exc}"
            raise

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                self.refresh()
                return
            self._stop.clear()
            self._error = None
            self._output_on = True
            self._last_sent = None
            self._last_send_at = 0.0
            self._night_pending = None
            self._renderer.reset()
            self._night.reset()
            self._tracker = LevelTracker()
            self._started_at = time.monotonic()
            self._prepare_lamp()
            self._running = True
            self._thread = threading.Thread(target=self._loop, name="music-drive", daemon=True)
            self._thread.start()

    def stop(self, *, restore: bool = True) -> None:
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._audio.close()
        self._running = False
        self._loud = 0.0
        self._beat = False
        self._last_send_at = 0.0
        self._last_tick_ms = 0.0
        self._thread = None
        try:
            if restore and self._snapshot is not None and not self._picture_active():
                self._restore_snapshot()
        finally:
            self._sync_hold(False)
            self._snapshot = None

    def _config(self) -> AppConfig:
        now = time.monotonic()
        if self._cfg is None or now - self._cfg_at > 0.3:
            self._cfg = self._get_config()
            self._cfg_at = now
        return self._cfg

    def _prepare_lamp(self) -> None:
        self._sync_hold(True)
        client = self._client_factory()
        try:
            vals = client.get_prop(["bg_power", "bg_rgb", "bg_bright"])
            self._snapshot = {
                "bg_power": vals[0] if vals else "off",
                "bg_rgb": vals[1] if len(vals) > 1 else 0,
                "bg_bright": vals[2] if len(vals) > 2 else 40,
            }
        except Exception:
            amb = self._config().ambient
            self._snapshot = {
                "bg_power": "on",
                "bg_rgb": int(str(amb.hex).lstrip("#") or "2A3A55", 16),
                "bg_bright": amb.bright,
            }
        try:
            client.bg_stop_cf()
        except Exception:
            pass
        try:
            client.set_bg_power(True, "sudden", 0)
        except Exception as exc:  # noqa: BLE001
            self._error = f"氛围灯打不开：{exc}"

    def _restore_snapshot(self) -> None:
        snap = self._snapshot or {}
        client = self._client_factory()
        power = str(snap.get("bg_power") or "").lower()
        try:
            if power != "on":
                client.set_bg_power(False, "sudden", 0)
                return
            try:
                bright = int(snap.get("bg_bright") or 40)
            except (TypeError, ValueError):
                bright = 40
            try:
                rgb = int(snap.get("bg_rgb") or 0)
            except (TypeError, ValueError):
                rgb = 0x2A3A55
            client.set_bg_color_scene(max(0, rgb), max(1, min(100, bright)))
        except Exception as exc:  # noqa: BLE001
            self._error = f"恢复氛围灯失败：{exc}"

    def _loop(self) -> None:
        last = time.monotonic()
        # ~30Hz：比 50ms 更跟手，仍低于易来 UDP 吞包风险区
        while not self._stop.wait(0.033):
            now = time.monotonic()
            dt = max(0.016, min(0.2, now - last))
            last = now
            try:
                self._tick(dt)
            except Exception as exc:  # noqa: BLE001
                self._error = str(exc)

    def _tick(self, dt: float) -> None:
        t_tick = time.monotonic()
        cfg = self._config()
        music = cfg.music
        self._audio.ensure(music.input)
        heard = self._audio.ring.latest(0.20)
        fresh = self._audio.ring.take_new(0.055)
        if self._audio.error:
            self._error = self._audio.error
        elif self._started_at and time.monotonic() - self._started_at > 2.5 and (
            heard.size == 0
            or (self._audio.packets > 20 and self._audio.peak < 1e-4)
        ):
            if music.input == "mic" and self._audio.packets > 0 and self._audio.peak < 1e-4:
                self._error = (
                    "麦克风有数据但电平为 0。请在系统设置 → 声音 → 输入里选内建麦克风，"
                    "不要选 BlackHole 等虚拟设备，并对着麦克风说话试一下。"
                )
            elif music.input == "mic":
                self._error = "还没有收到麦克风声音"
            elif self._audio.packets > 0 and self._audio.peak < 1e-4:
                self._error = (
                    "采到的是静音。请在系统设置的「仅系统录音」里，把「Yeelight 显示器挂灯」先关掉再打开，"
                    "然后完全退出并重新打开本应用。"
                )
            else:
                self._error = "还没有收到电脑播放的声音。请先播放音乐或视频。"
        else:
            self._error = None
        levels = self._tracker.push(fresh, self._audio.ring.rate, dt)
        self._loud = levels.loud
        self._beat = levels.beat
        styles = list(music.styles)
        if "night" in styles:
            frame = self._night.step(
                levels,
                bright_max=int(music.bright),
                dt=dt,
                lamp_host=str(cfg.host or ""),
            )
        else:
            colors = palette_from_settings(
                music.palette,
                list(cfg.ambient.colors or []),
                cfg.ambient.hex,
            )
            frame = self._renderer.step(
                levels,
                styles=styles,
                colors=colors,
                bright_max=int(music.bright),
                dt=dt,
                builtin=music.palette != "ambient",
            )
        # 夜色：颜色/亮度都不量化，避免 8 档色把左右并成同色、不分段
        if "night" in styles:
            left = max(1, min(0xFFFFFF, int(frame.left)))
            right = max(1, min(0xFFFFFF, int(frame.right)))
            bright = max(1, min(100, int(frame.bright)))
        else:
            left = _quantize_color(frame.left)
            right = _quantize_color(frame.right)
            bright = _quantize_bright(frame.bright)
        segmented = bool(frame.segmented and left != right)
        key = (bright, left, right, segmented)
        if "night" in styles:
            self._send_night(cfg, bright, left, right, segmented, key)
            self._last_tick_ms = (time.monotonic() - t_tick) * 1000.0
            self._last_tick_at = time.monotonic()
            return
        if key == self._last_sent:
            self._last_tick_ms = (time.monotonic() - t_tick) * 1000.0
            self._last_tick_at = time.monotonic()
            return
        self._send(cfg, bright, left, right, segmented)
        self._last_sent = key
        self._last_send_at = time.monotonic()
        self._last_tick_ms = (time.monotonic() - t_tick) * 1000.0
        self._last_tick_at = time.monotonic()

    def _send_night(
        self,
        cfg: AppConfig,
        bright: int,
        left: int,
        right: int,
        segmented: bool,
        key: tuple[int, int, int, bool],
    ) -> None:
        """夜色下发限流：易来 UDP 过密会被吞，明暗反而发闷。

        约 18Hz；亮度跳变 ≥4 立即下发，保证呼吸峰谷到得了灯。
        """
        now = time.monotonic()
        # 先冲刷到期的挂起帧
        if self._night_pending is not None and now - self._last_send_at >= 0.055:
            p_cfg, p_b, p_l, p_r, p_seg, p_key = self._night_pending
            self._night_pending = None
            self._send(p_cfg, p_b, p_l, p_r, p_seg)
            self._last_sent = p_key
            self._last_send_at = now
        if key == self._last_sent:
            return
        prev = self._last_sent
        prev_b = prev[0] if prev else bright
        bright_jump = abs(bright - prev_b) >= 4
        # 左右切换必须立刻下发，否则节奏必然拖在 UDP 限流后面
        side_jump = bool(
            prev
            and (left != prev[1] or right != prev[2] or segmented != prev[3])
        )
        due = now - self._last_send_at >= 0.055
        if not due and not bright_jump and not side_jump:
            self._night_pending = (cfg, bright, left, right, segmented, key)
            return
        self._night_pending = None
        self._send(cfg, bright, left, right, segmented)
        self._last_sent = key
        self._last_send_at = now

    def _send(self, cfg: AppConfig, bright: int, left: int, right: int, segmented: bool) -> None:
        if not self._output_on:
            return
        sess = udp_hub.get_udp(cfg.host, cfg.udp_port)
        left_i = max(1, min(0xFFFFFF, int(left)))
        right_i = max(1, min(0xFFFFFF, int(right)))
        bright_i = max(1, min(100, int(bright)))
        if segmented and left_i != right_i:
            sess.set_segment_rgb(left_i, right_i)
        else:
            sess.set_bg_rgb(left_i)
        sess.set_bg_bright(bright_i)


def _quantize_color(rgb: int) -> int:
    step = 4

    def q(channel: int) -> int:
        return max(0, min(255, int(round(channel / step) * step)))

    return (q((rgb >> 16) & 255) << 16) | (q((rgb >> 8) & 255) << 8) | q(rgb & 255)


def _quantize_bright(bright: int) -> int:
    # 1 档亮度即可下发，少一层「亮了半拍才跳」
    return max(1, min(100, int(bright)))
