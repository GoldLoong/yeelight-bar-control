"""音乐跃动运行时：占住氛围灯，追色暂停往灯上写。"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from server.config import AppConfig, load_config
from server.music.analyze import LevelTracker
from server.music.capture import AudioInput
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
        self._snapshot: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._running = False
        self._error: str | None = None
        self._loud = 0.0
        self._beat = False
        self._last_sent: tuple[int, int, int, bool] | None = None
        self._cfg: AppConfig | None = None
        self._cfg_at = 0.0
        self._started_at = 0.0

    def status(self) -> dict[str, Any]:
        cfg = self._config()
        return {
            "running": self._running,
            "input": cfg.music.input,
            "error": self._audio.error or self._error,
            "loud": round(self._loud, 3),
            "beat": self._beat,
            "packets": int(self._audio.packets),
            "peak": round(float(self._audio.peak), 4),
        }

    def refresh(self) -> None:
        self._cfg = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                self.refresh()
                return
            self._stop.clear()
            self._error = None
            self._last_sent = None
            self._renderer.reset()
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
        while not self._stop.wait(0.05):
            now = time.monotonic()
            dt = max(0.02, min(0.2, now - last))
            last = now
            try:
                self._tick(dt)
            except Exception as exc:  # noqa: BLE001
                self._error = str(exc)

    def _tick(self, dt: float) -> None:
        cfg = self._config()
        music = cfg.music
        self._audio.ensure(music.input)
        heard = self._audio.ring.latest(0.25)
        fresh = self._audio.ring.take_new(0.09)
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
        colors = palette_from_settings(
            music.palette,
            list(cfg.ambient.colors or []),
            cfg.ambient.hex,
        )
        frame = self._renderer.step(
            levels,
            styles=list(music.styles),
            colors=colors,
            bright_max=int(music.bright),
            dt=dt,
            builtin=music.palette != "ambient",
        )
        left = _quantize_color(frame.left)
        right = _quantize_color(frame.right)
        bright = _quantize_bright(frame.bright)
        segmented = bool(frame.segmented and left != right)
        key = (bright, left, right, segmented)
        if key == self._last_sent:
            return
        self._send(cfg, bright, left, right, segmented)
        self._last_sent = key

    def _send(self, cfg: AppConfig, bright: int, left: int, right: int, segmented: bool) -> None:
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
    step = 8

    def q(channel: int) -> int:
        return max(0, min(255, int(round(channel / step) * step)))

    return (q((rgb >> 16) & 255) << 16) | (q((rgb >> 8) & 255) << 8) | q(rgb & 255)


def _quantize_bright(bright: int) -> int:
    snapped = int(round(int(bright) / 2.0) * 2)
    return max(1, min(100, snapped))
