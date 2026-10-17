from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from server.config import AppConfig, ScreenSyncSettings, _normalize_priority, load_config
from server.sync.capture import (
    capture_fail_streak,
    invalidate_window_cache,
    is_screen_locked,
    last_capture_backend,
    last_capture_ms,
    last_sample_meta,
    list_displays,
    reset_last_color,
    sample_region_color,
    screen_is_fullscreenish,
    screen_target_valid,
)
from server.sync.iina import consume_seek, get_iina_status
from server.sync.media import consume_browser_seek, get_browser_status
from server.yeelight import (
    RateLimitedTcp,
    UdpStreamSession,
    YeelightClient,
    luminance_to_bright,
    rgb_tuple_to_int,
)
from server.yeelight import udp_hub

# 性能：控制环低频；取色优先 ScreenCaptureKit（~30ms），mss 回退；目标 8–14Hz
_CTRL_HZ = 8.0
_SAMPLE_HZ_MAX = 14.0
_SAMPLE_HZ_MIN = 8.0


@dataclass
class SyncRuntimeState:
    running: bool = False
    active: bool = False
    last_rgb: tuple[int, int, int] | None = None
    last_rgb_int: int | None = None
    iina: dict[str, Any] = field(default_factory=dict)
    media: dict[str, Any] = field(default_factory=dict)
    source: str = "iina"
    error: str | None = None


class SyncEngine:
    """
    - 控制线程：播放态 / seek / UDP 会话（低频）
    - 取色线程：截屏 →（可选平滑）→ 立刻 UDP（颜色 / 亮度）
    color_smooth=0：不做相近色平滑运算，采完即发，延迟最低
    亮度同步也走 UDP（set_bright / bg_set_bright），不占 TCP 配额
    """

    def __init__(
        self,
        client_factory: Callable[[], YeelightClient],
        get_config: Callable[[], AppConfig] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._get_config = get_config or load_config
        self._stop = threading.Event()
        self._ctrl_thread: threading.Thread | None = None
        self._capture_thread: threading.Thread | None = None
        self.state = SyncRuntimeState()
        self._udp: UdpStreamSession | None = None
        self._udp_lock = threading.Lock()
        self._was_playing = False
        self._media_running = False
        self._sync_session = False  # 本轮媒体会话里是否跟过色，退出时要执行 on_exit
        self._exit_applied = False
        self._saved_bg_state: dict[str, Any] | None = None
        self._tcp_gate = RateLimitedTcp(min_interval=5.0)
        self._bg_power_on = False
        self._last_sent_rgb: int | None = None
        self._last_main_bright_sent: int | None = None
        self._last_bg_bright_sent: int | None = None
        self._last_sync_delta = 0
        self._cfg: AppConfig | None = None
        self._cfg_at = 0.0
        self._active_source = "iina"
        self._fullscreen_hint: bool | None = None
        self._browser_owners: list[str] = []
        self._browser_title_hint: str | None = None
        self._screen_capture_target = "front_window"
        self._screen_user_rect: dict[str, Any] | None = None
        self._screen_locked_latched = False
        self._screen_meta: dict[str, Any] = {}

        self._playing = False
        self._force_snap = False
        self._capture_display_id: int | None = None
        self._capture_region = "window"
        self._applied_capture_key: tuple[object, ...] | None = None
        self._color_smooth = 0.0
        self._main_bright_smooth = 0.2
        self._ambient_bright_smooth = 0.2
        self._base_main_bright = 50
        self._base_ambient_bright = 40
        self._main_bright_range = 5
        self._ambient_bright_range = 30

        self._color_lock = threading.Lock()
        self._current = [36.0, 40.0, 48.0]
        self._current_l = [36.0, 40.0, 48.0]
        self._current_r = [36.0, 40.0, 48.0]
        self._segment_color = False
        self._last_sent_l: int | None = None
        self._last_sent_r: int | None = None
        # 平滑后的亮度（float），分别给主灯 / 氛围灯
        self._cur_main_bright = 50.0
        self._cur_bg_bright = 40.0
        self._bright_lock = threading.Lock()
        self._music_hold = False

    def set_music_hold(self, hold: bool) -> None:
        """音乐跃动占住氛围灯时，追色不再往灯上写。"""
        self._music_hold = bool(hold)

    def picture_active(self) -> bool:
        return bool(self._playing)

    def start(self) -> None:
        if self._ctrl_thread and self._ctrl_thread.is_alive():
            return
        self._stop.clear()
        self.state.running = True
        self.state.error = None
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="sync-capture", daemon=True
        )
        self._ctrl_thread = threading.Thread(
            target=self._control_loop, name="sync-ctrl", daemon=True
        )
        self._capture_thread.start()
        self._ctrl_thread.start()

    def stop(self, apply_exit: bool = True) -> None:
        self._stop.set()
        self._playing = False
        for t in (self._ctrl_thread, self._capture_thread):
            if t and t.is_alive():
                t.join(timeout=2.0)
        self._ctrl_thread = None
        self._capture_thread = None
        self.state.running = False
        self.state.active = False
        self._close_udp()
        if apply_exit and self._sync_session:
            self._handle_exit()
        self._sync_session = False
        self._was_playing = False
        self._media_running = False
        self._exit_applied = True

    def refresh_from_config(self) -> None:
        self._cfg = None
        cfg = self._get_config()
        if cfg.sync.enabled:
            self.start()
        else:
            self.stop(apply_exit=True)

    def _config(self) -> AppConfig:
        now = time.monotonic()
        if self._cfg is None or now - self._cfg_at > 1.0:
            self._cfg = self._get_config()
            self._cfg_at = now
            self._apply_source_params(self._active_source or "iina")
        return self._cfg

    def _apply_source_params(self, source: str) -> None:
        cfg = self._cfg or self._get_config()
        params = cfg.sync.params_for(source)
        self._color_smooth = max(0.0, min(1.0, float(params.color_smooth or 0.0)))
        self._main_bright_smooth = max(
            0.0, min(1.0, float(params.main_bright_smooth or 0.0))
        )
        self._ambient_bright_smooth = max(
            0.0, min(1.0, float(params.ambient_bright_smooth or 0.0))
        )
        self._base_main_bright = max(1, min(100, int(params.base_main_bright or 50)))
        self._base_ambient_bright = max(
            1, min(100, int(params.base_ambient_bright or 40))
        )
        self._main_bright_range = max(0, min(50, int(params.main_bright_range or 0)))
        self._ambient_bright_range = max(
            0, min(50, int(params.ambient_bright_range or 0))
        )
        self._capture_display_id = params.display_id
        self._capture_region = params.region
        self._segment_color = bool(getattr(params, "segment_color", False))
        if source == "browser":
            browser = cfg.sync.browser
            self._browser_owners = list(browser.owners or [])
            self._screen_capture_target = "front_window"
            self._screen_user_rect = None
        elif source == "screen":
            screen = cfg.sync.screen
            self._browser_owners = []
            self._screen_capture_target = screen.capture_target
            ur = screen.user_rect
            self._screen_user_rect = ur.model_dump() if ur is not None else None
        else:
            self._browser_owners = []
            self._screen_capture_target = "front_window"
            self._screen_user_rect = None

    @staticmethod
    def _screen_eligible(screen: ScreenSyncSettings, *, locked: bool, fail_streak: int) -> tuple[bool, str]:
        if locked or fail_streak >= 12:
            return False, "locked" if locked else "capture_fail"
        flags = [str(x).lower() for x in (screen.eligibility or []) if str(x).strip()]
        if not flags:
            flags = ["target_valid"]
        # always 与其它同勾时：其它条件 AND；仅 always 则开着即可
        strict = [f for f in flags if f != "always"]
        if not strict and "always" in flags:
            return True, "always"
        checks = strict or ["target_valid"]
        ur = screen.user_rect.model_dump() if screen.user_rect else None
        for f in checks:
            if f == "target_valid":
                if not screen_target_valid(
                    capture_target=screen.capture_target,
                    display_id=screen.display_id,
                    user_rect=ur,
                ):
                    return False, "target_invalid"
            elif f == "fullscreen_only":
                if not screen_is_fullscreenish(
                    capture_target=screen.capture_target,
                    display_id=screen.display_id,
                    user_rect=ur,
                ):
                    return False, "not_fullscreen"
        return True, "ok"

    def _reset_colors(self, rgb: tuple[int, int, int] | None = None) -> None:
        if rgb is None:
            rgb = (36, 40, 48)
        with self._color_lock:
            self._current = [float(rgb[0]), float(rgb[1]), float(rgb[2])]
            self._current_l = [float(rgb[0]), float(rgb[1]), float(rgb[2])]
            self._current_r = [float(rgb[0]), float(rgb[1]), float(rgb[2])]
        self._last_sent_rgb = None
        self._last_sent_l = None
        self._last_sent_r = None

    def _control_loop(self) -> None:
        try:
            os.nice(12)
        except Exception:
            pass
        dt = 1.0 / _CTRL_HZ
        while not self._stop.is_set():
            t0 = time.monotonic()
            cfg = self._config()
            sync = cfg.sync
            if not sync.enabled:
                self._playing = False
                if self.state.active:
                    self._pause()
                time.sleep(0.4)
                continue

            self._capture_display_id = None
            self._capture_region = "window"
            iina_on = bool(sync.iina.enabled)
            browser_on = bool(sync.browser.enabled)
            screen_on = bool(sync.screen.enabled)
            browser_cfg = sync.browser
            screen_cfg = sync.screen
            self._browser_owners = list(browser_cfg.owners or [])
            priority = _normalize_priority(sync.priority)

            ist = get_iina_status(sync.iina.iina_ipc_socket)
            bst = get_browser_status(
                require_extension=bool(browser_cfg.require_extension),
                match_hosts=list(browser_cfg.match_hosts or []),
                owners=list(browser_cfg.owners or []),
            )
            locked = is_screen_locked()
            fail_n = capture_fail_streak()
            screen_ok, screen_reason = self._screen_eligible(
                screen_cfg, locked=locked, fail_streak=fail_n
            )

            self.state.iina = {
                "enabled": iina_on,
                "running": ist.running,
                "playing": ist.playing,
                "pause": ist.pause,
                "path": ist.path,
                "source": ist.source,
                "position": ist.position,
                "speed": ist.speed,
            }
            self.state.media = {
                "enabled": browser_on,
                "running": bst.running,
                "playing": bst.playing,
                "pause": bst.pause,
                "provider": bst.provider,
                "source": bst.source,
                "event": bst.event,
                "title": bst.title,
                "host": bst.host,
                "fullscreen": bst.fullscreen,
                "position": bst.position,
            }
            self._screen_meta = {
                "enabled": screen_on,
                "eligible": bool(screen_on and screen_ok),
                "reason": screen_reason,
                "capture_target": screen_cfg.capture_target,
                "locked": locked,
                "fail_streak": fail_n,
            }

            sought = False
            # seek 只影响当前活跃源，避免 IINA/浏览器 seek 打断屏幕跟色平滑
            if iina_on and consume_seek():
                if self._active_source == "iina":
                    sought = True
            if browser_on and consume_browser_seek():
                if self._active_source == "browser":
                    sought = True
            if sought:
                invalidate_window_cache()
                self._force_snap = True

            iina_playing = iina_on and ist.playing
            browser_playing = browser_on and bst.playing
            screen_playing = screen_on and screen_ok

            # 锁屏：若当前/上一会话是屏幕源，走屏幕 on_exit
            if locked and screen_on:
                if not self._screen_locked_latched:
                    self._screen_locked_latched = True
                    if self._active_source == "screen" and self._sync_session and not self._exit_applied:
                        self._playing = False
                        if self.state.active:
                            self._pause()
                        self._was_playing = False
                        self._handle_exit()
                        self._sync_session = False
                        self._exit_applied = True
                        self._media_running = False
                # 锁屏期间屏幕不可跟；其它源仍可仲裁
            else:
                self._screen_locked_latched = False

            eligible = {
                "iina": iina_playing,
                "browser": browser_playing,
                "screen": screen_playing,
            }
            source = None
            for cand in priority:
                if eligible.get(cand):
                    source = cand
                    break

            if source == "browser":
                self._fullscreen_hint = bst.fullscreen
                self._browser_title_hint = bst.title
            else:
                self._fullscreen_hint = None
                self._browser_title_hint = None

            if source and source != self._active_source:
                if self.state.active:
                    self._pause()
                self._was_playing = False
                self._active_source = source
                self.state.source = source
                self._apply_source_params(source)
                invalidate_window_cache()
                self._force_snap = True
                self._reset_colors()
            elif source:
                self._active_source = source
                self.state.source = source
                self._apply_source_params(source)
            elif self._active_source:
                self.state.source = self._active_source

            # 退出：当前会话源失去 running/eligible，且没有其它源可跟
            session_src = self._active_source
            session_running = False
            if session_src == "iina":
                session_running = bool(iina_on and ist.running)
            elif session_src == "browser":
                session_running = bool(browser_on and bst.running)
            elif session_src == "screen":
                session_running = bool(screen_on and screen_ok and not locked)

            other_playing = any(
                eligible.get(s) for s in ("iina", "browser", "screen") if s != session_src
            )

            if session_running:
                self._media_running = True
                self._exit_applied = False
            elif self._media_running and not self._exit_applied and not other_playing:
                self._playing = False
                if self._was_playing or self.state.active:
                    self._pause()
                self._was_playing = False
                if self._sync_session:
                    self._handle_exit()
                self._sync_session = False
                self._exit_applied = True
                self._media_running = False
                time.sleep(0.25)
                continue

            playing = source is not None
            if playing:
                if not self._was_playing:
                    self._on_play_start(cfg)
                self._was_playing = True
                self._playing = True
                self.state.active = True
                self.state.error = None
            else:
                self._playing = False
                if self._was_playing:
                    self._pause()
                    self._was_playing = False
                time.sleep(0.25)
                continue

            remain = dt - (time.monotonic() - t0)
            time.sleep(remain if remain > 0.01 else 0.01)

    def _capture_loop(self) -> None:
        try:
            os.nice(5)
        except Exception:
            pass
        while not self._stop.is_set():
            if self._music_hold or not self._playing:
                time.sleep(0.15)
                continue
            cfg = self._config()
            media_source = self._active_source or "iina"
            params = cfg.sync.params_for(media_source)
            need_color = params.channels.ambient_color
            need_bright = (
                params.channels.main_brightness or params.channels.ambient_brightness
            )
            if not need_color and not need_bright:
                time.sleep(0.2)
                continue

            s = self._color_smooth
            # 目标频率随平滑下降；再按实际上次截屏耗时抬高间隔，避免空转抢 CPU
            hz = _SAMPLE_HZ_MAX - s * (_SAMPLE_HZ_MAX - _SAMPLE_HZ_MIN)
            interval = 1.0 / max(4.0, hz)
            capt = last_capture_ms() / 1000.0
            if capt > 0.02:
                interval = max(interval, capt * 1.05)

            t0 = time.monotonic()
            try:
                display_id = params.display_id
                region = params.region
                owners = (
                    tuple(self._browser_owners)
                    if media_source == "browser"
                    else None
                )
                prefer_titles = None
                if media_source == "browser" and self._browser_title_hint:
                    prefer_titles = (self._browser_title_hint,)
                cap_target = (
                    self._screen_capture_target if media_source == "screen" else None
                )
                user_rect = self._screen_user_rect if media_source == "screen" else None
                segment = bool(getattr(params, "segment_color", False))
                self._segment_color = segment
                capture_key = (
                    display_id,
                    region,
                    media_source,
                    owners,
                    prefer_titles,
                    cap_target,
                    tuple(sorted((user_rect or {}).items())) if user_rect else None,
                    segment,
                )
                if capture_key != self._applied_capture_key:
                    self._applied_capture_key = capture_key
                    self._capture_display_id = display_id
                    self._capture_region = region
                    invalidate_window_cache()
                    reset_last_color()
                    self._force_snap = True
                    self._reset_colors()

                sample = sample_region_color(
                    display_id,
                    region,
                    media_source=media_source,
                    owners=owners,
                    fullscreen_hint=self._fullscreen_hint,
                    capture_target=cap_target,
                    user_rect=user_rect,
                    prefer_titles=prefer_titles,
                    split_lr=segment,
                )
                force = self._force_snap
                # 亮度跟平滑后的色，避免色已抹平、亮度仍跟着原始采样抖
                bright_rgb = sample.rgb
                if need_color:
                    if segment and sample.left is not None and sample.right is not None:
                        out_lr = self._apply_smooth_lr(
                            sample.left, sample.right, force=force
                        )
                        if out_lr is not None:
                            left, right = out_lr
                            self._push_segment(cfg, left, right, force=True)
                            avg = (
                                int(round((left[0] + right[0]) / 2)),
                                int(round((left[1] + right[1]) / 2)),
                                int(round((left[2] + right[2]) / 2)),
                            )
                            self.state.last_rgb = avg
                            self.state.last_rgb_int = rgb_tuple_to_int(*avg)
                            bright_rgb = avg
                        else:
                            with self._color_lock:
                                bright_rgb = (
                                    int(max(0, min(255, round(self._current[0])))),
                                    int(max(0, min(255, round(self._current[1])))),
                                    int(max(0, min(255, round(self._current[2])))),
                                )
                    else:
                        out = self._apply_smooth(sample.rgb, force=force)
                        if out is not None:
                            self._push(cfg, out, force=True)
                            self.state.last_rgb = out
                            self.state.last_rgb_int = rgb_tuple_to_int(*out)
                            bright_rgb = out
                        else:
                            with self._color_lock:
                                bright_rgb = (
                                    int(max(0, min(255, round(self._current[0])))),
                                    int(max(0, min(255, round(self._current[1])))),
                                    int(max(0, min(255, round(self._current[2])))),
                                )
                if need_bright:
                    self._sync_brightness(cfg, bright_rgb, force=force)
                if force:
                    self._force_snap = False
            except Exception as exc:  # noqa: BLE001
                self.state.error = str(exc)

            remain = interval - (time.monotonic() - t0)
            time.sleep(remain if remain > 0.001 else 0.001)

    def apply_manual_base_bright(self, target: str, base: int) -> int | None:
        """
        手动调光写入基础亮度。若该路正在同步且播放中，立即下发 base+当前增量。
        返回实际下发亮度；若不在同步则返回 None（由 API 直接设 base）。
        """
        base = max(1, min(100, int(base)))
        self._cfg = None
        cfg = self._config()
        src = self._active_source or "iina"
        params = cfg.sync.params_for(src)
        if target == "main":
            self._base_main_bright = base
            if not (
                cfg.sync.enabled
                and params.channels.main_brightness
                and self._playing
            ):
                return None
            applied = self._compose_bright(base, self._last_sync_delta, self._main_bright_range)
            with self._bright_lock:
                self._cur_main_bright = float(applied)
            self._send_main_bright(applied, force=True)
            return applied
        self._base_ambient_bright = base
        if not (
            cfg.sync.enabled
            and params.channels.ambient_brightness
            and self._playing
        ):
            return None
        applied = self._compose_bright(
            base, self._last_sync_delta, self._ambient_bright_range
        )
        with self._bright_lock:
            self._cur_bg_bright = float(applied)
        self._send_bg_bright(applied, force=True)
        return applied

    @staticmethod
    def _compose_bright(base: int, sync_delta: int, bright_range: int) -> int:
        """最终 = 基础 + 按波动范围缩放后的画面增量。range=0 则恒为基础。"""
        r = max(0, min(50, int(bright_range)))
        if r <= 0:
            adj = 0
        else:
            adj = int(round(float(sync_delta) * (r / 50.0)))
            adj = max(-r, min(r, adj))
        return max(1, min(100, int(base) + adj))

    @staticmethod
    def _scene_sync_delta(rgb: tuple[int, int, int]) -> int:
        """画面亮度 → 同步增量：中灰约 0，亮加暗减。"""
        scene = luminance_to_bright(*rgb)  # 1..100
        return int(scene) - 50  # 约 -49..+50

    def _smooth_bright(
        self, cur: float, target: float, smooth: float, *, force: bool
    ) -> float:
        """与颜色平滑同思路：s=0 直出；s 越大越黏。"""
        dist = abs(target - cur)
        snap = 14.0 + smooth * 26.0
        if force or smooth <= 0.001 or dist >= snap:
            return float(target)
        a = self._blend_alpha(smooth, source=self._active_source)
        return cur * (1.0 - a) + target * a

    @staticmethod
    def _blend_alpha(s: float, *, source: str | None = None) -> float:
        """
        滑条中段要能感到「黏」。
        - 旧式 1-0.72s：s=0.42 仍约 70% 新色/帧，几乎像没开平滑
        - 现用 (1-s)^2：s=0.42 → a≈0.34；屏幕源略跟手一点
        """
        s = max(0.0, min(1.0, float(s)))
        if s <= 0.001:
            return 1.0
        if (source or "") == "screen":
            # 屏幕更在意延迟：同档位更跟手
            a = (1.0 - s) ** 1.35
        else:
            a = (1.0 - s) ** 2.0
        return max(0.08, min(1.0, a))

    def _smooth_channel(
        self,
        cur: list[float],
        sample: tuple[int, int, int],
        *,
        force: bool,
        s: float,
        a: float,
    ) -> tuple[int, int, int] | None:
        dist = (
            abs(sample[0] - cur[0])
            + abs(sample[1] - cur[1])
            + abs(sample[2] - cur[2])
        )
        if not force and dist < 2.0:
            return None
        snap = 28.0 + s * 52.0
        if force or s <= 0.001 or dist >= snap:
            cur[0], cur[1], cur[2] = float(sample[0]), float(sample[1]), float(sample[2])
            return sample
        cur[0] = cur[0] * (1.0 - a) + sample[0] * a
        cur[1] = cur[1] * (1.0 - a) + sample[1] * a
        cur[2] = cur[2] * (1.0 - a) + sample[2] * a
        return (
            int(max(0, min(255, round(cur[0])))),
            int(max(0, min(255, round(cur[1])))),
            int(max(0, min(255, round(cur[2])))),
        )

    def _apply_smooth(
        self, sample: tuple[int, int, int], *, force: bool
    ) -> tuple[int, int, int] | None:
        """
        平滑只改变「本帧跟新色的比例」。
        s=0：本帧 100% 新色；s 越大越黏。
        仅在明显切镜（大距离）时直出；平滑越高，直出门槛越高。
        """
        s = self._color_smooth
        with self._color_lock:
            a = self._blend_alpha(s, source=self._active_source)
            return self._smooth_channel(
                self._current, sample, force=force, s=s, a=a
            )

    def _apply_smooth_lr(
        self,
        left: tuple[int, int, int],
        right: tuple[int, int, int],
        *,
        force: bool,
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
        s = self._color_smooth
        with self._color_lock:
            a = self._blend_alpha(s, source=self._active_source)
            out_l = self._smooth_channel(
                self._current_l, left, force=force, s=s, a=a
            )
            out_r = self._smooth_channel(
                self._current_r, right, force=force, s=s, a=a
            )
            if out_l is None and out_r is None:
                return None
            if out_l is None:
                out_l = (
                    int(max(0, min(255, round(self._current_l[0])))),
                    int(max(0, min(255, round(self._current_l[1])))),
                    int(max(0, min(255, round(self._current_l[2])))),
                )
            if out_r is None:
                out_r = (
                    int(max(0, min(255, round(self._current_r[0])))),
                    int(max(0, min(255, round(self._current_r[1])))),
                    int(max(0, min(255, round(self._current_r[2])))),
                )
            # 同步均值缓冲，供亮度通道复用
            self._current[0] = (self._current_l[0] + self._current_r[0]) / 2
            self._current[1] = (self._current_l[1] + self._current_r[1]) / 2
            self._current[2] = (self._current_l[2] + self._current_r[2]) / 2
            return out_l, out_r

    def _sync_brightness(
        self, cfg: AppConfig, rgb: tuple[int, int, int], *, force: bool
    ) -> None:
        delta = self._scene_sync_delta(rgb)
        self._last_sync_delta = delta
        params = cfg.sync.params_for(self._active_source or "iina")
        if params.channels.main_brightness:
            target = float(
                self._compose_bright(
                    self._base_main_bright, delta, self._main_bright_range
                )
            )
            with self._bright_lock:
                self._cur_main_bright = self._smooth_bright(
                    self._cur_main_bright, target, self._main_bright_smooth, force=force
                )
                out = int(max(1, min(100, round(self._cur_main_bright))))
            self._send_main_bright(out, force=force)
        if params.channels.ambient_brightness:
            target = float(
                self._compose_bright(
                    self._base_ambient_bright, delta, self._ambient_bright_range
                )
            )
            with self._bright_lock:
                self._cur_bg_bright = self._smooth_bright(
                    self._cur_bg_bright, target, self._ambient_bright_smooth, force=force
                )
                out = int(max(1, min(100, round(self._cur_bg_bright))))
            self._send_bg_bright(out, force=force)

    def _send_main_bright(self, bright: int, *, force: bool) -> None:
        if not force and self._last_main_bright_sent is not None:
            if abs(bright - self._last_main_bright_sent) < 1:
                return
        cfg = self._config()
        self._ensure_udp(cfg, turn_on_bg=False)
        with self._udp_lock:
            if self._udp is None:
                return
            try:
                self._udp.set_bright(bright)
                self._last_main_bright_sent = bright
            except Exception as exc:  # noqa: BLE001
                self.state.error = str(exc)

    def _send_bg_bright(self, bright: int, *, force: bool) -> None:
        if not force and self._last_bg_bright_sent is not None:
            if abs(bright - self._last_bg_bright_sent) < 1:
                return
        cfg = self._config()
        self._ensure_udp(cfg, turn_on_bg=False)
        with self._udp_lock:
            if self._udp is None:
                return
            try:
                self._udp.set_bg_bright(bright)
                self._last_bg_bright_sent = bright
            except Exception as exc:  # noqa: BLE001
                self.state.error = str(exc)

    def _on_play_start(self, cfg: AppConfig) -> None:
        self._reset_colors()
        with self._bright_lock:
            self._cur_main_bright = float(self._base_main_bright)
            self._cur_bg_bright = float(self._base_ambient_bright)
        self._last_main_bright_sent = None
        self._last_bg_bright_sent = None
        self._last_sync_delta = 0
        self._sync_session = True
        invalidate_window_cache()
        if self._music_hold:
            return
        # 同步开始前快照氛围灯，供「恢复同步前」使用
        if self._saved_bg_state is None:
            try:
                snap = self._client_factory().snapshot()
                self._saved_bg_state = {
                    "bg_power": snap.get("bg_power"),
                    "bg_bright": snap.get("bg_bright"),
                    "bg_rgb": snap.get("bg_rgb"),
                }
            except Exception:
                self._saved_bg_state = None
        self._ensure_udp(cfg, turn_on_bg=True)

    def _push(self, cfg: AppConfig, rgb: tuple[int, int, int], *, force: bool) -> None:
        rgb_int = rgb_tuple_to_int(*rgb)
        if not force and self._last_sent_rgb is not None:
            prev = self._last_sent_rgb
            d = (
                abs(((rgb_int >> 16) & 255) - ((prev >> 16) & 255))
                + abs(((rgb_int >> 8) & 255) - ((prev >> 8) & 255))
                + abs((rgb_int & 255) - (prev & 255))
            )
            if d < 1:
                return
        self._ensure_udp(cfg, turn_on_bg=False)
        with self._udp_lock:
            if self._udp is None:
                return
            self._udp.set_bg_rgb(rgb_int)
        self._last_sent_rgb = rgb_int
        self._last_sent_l = None
        self._last_sent_r = None

    def _push_segment(
        self,
        cfg: AppConfig,
        left: tuple[int, int, int],
        right: tuple[int, int, int],
        *,
        force: bool,
    ) -> None:
        left_i = max(1, rgb_tuple_to_int(*left))
        right_i = max(1, rgb_tuple_to_int(*right))
        if (
            not force
            and self._last_sent_l is not None
            and self._last_sent_r is not None
            and self._last_sent_l == left_i
            and self._last_sent_r == right_i
        ):
            return
        self._ensure_udp(cfg, turn_on_bg=False)
        with self._udp_lock:
            if self._udp is None:
                return
            self._udp.set_segment_rgb(left_i, right_i)
        self._last_sent_l = left_i
        self._last_sent_r = right_i
        self._last_sent_rgb = None

    def _ensure_udp(self, cfg: AppConfig, *, turn_on_bg: bool) -> None:
        with self._udp_lock:
            sess = udp_hub.get_udp(cfg.host, cfg.udp_port)
            self._udp = sess
        if turn_on_bg and not self._bg_power_on:
            try:
                self._tcp_gate.maybe(
                    "bg_power_on",
                    lambda: self._client_factory().set_bg_power(True, "sudden", 0),
                    force=True,
                )
                self._bg_power_on = True
            except Exception:
                pass

    def _pause(self) -> None:
        # 暂停只停跟色，保留 UDP 会话供手动色温/亮度继续用
        self.state.active = False

    def _handle_exit(self) -> None:
        if self._music_hold:
            return
        cfg = self._get_config()
        src = self._active_source or "iina"
        mode = cfg.sync.params_for(src).on_exit
        try:
            client = self._client_factory()
            # 配额冷却时短暂等待，避免静默跳过「关闭氛围灯」
            for _ in range(6):
                if not client.quota_blocked():
                    break
                time.sleep(0.4)
            if client.quota_blocked():
                self.state.error = "退出动作因 TCP 配额冷却未执行，稍后重试"
                return
            client.apply_exit_mode(mode, saved=self._saved_bg_state)
            self._bg_power_on = mode != "off_ambient"
            self._saved_bg_state = None
            self.state.error = None
        except Exception as exc:  # noqa: BLE001
            self.state.error = f"退出处理失败: {exc}"

    def _close_udp(self) -> None:
        with self._udp_lock:
            self._udp = None
        udp_hub.close_udp()

    def ensure_control_udp(self) -> UdpStreamSession:
        """手动调光/色温热路径：复用共享 UDP 会话（不占 TCP 配额）。"""
        cfg = self._config()
        sess = udp_hub.get_udp(cfg.host, cfg.udp_port)
        with self._udp_lock:
            self._udp = sess
        return sess

    def set_main_ct_fast(self, ct: int) -> None:
        self.ensure_control_udp().set_ct(ct)

    def set_main_bright_fast(self, bright: int) -> None:
        self.ensure_control_udp().set_bright(max(1, min(100, int(bright))))

    def status_dict(self) -> dict[str, Any]:
        displays = []
        try:
            displays = [d.__dict__ for d in list_displays()]
        except Exception:
            pass
        cfg = self._config()
        return {
            "running": self.state.running,
            "active": self.state.active,
            "source": self.state.source,
            "priority": _normalize_priority(cfg.sync.priority),
            "iina_enabled": bool(cfg.sync.iina.enabled),
            "browser_enabled": bool(cfg.sync.browser.enabled),
            "screen_enabled": bool(cfg.sync.screen.enabled),
            "last_rgb": self.state.last_rgb,
            "last_rgb_hex": (
                None if self.state.last_rgb_int is None else f"#{self.state.last_rgb_int:06x}"
            ),
            "iina": self.state.iina,
            "media": self.state.media,
            "screen": dict(self._screen_meta),
            "error": self.state.error,
            "displays": displays,
            "params": {
                "iina": cfg.sync.iina.model_dump(),
                "browser": cfg.sync.browser.model_dump(),
                "screen": cfg.sync.screen.model_dump(),
            },
            "color_smooth": self._color_smooth,
            "main_bright_smooth": self._main_bright_smooth,
            "ambient_bright_smooth": self._ambient_bright_smooth,
            "base_main_bright": self._base_main_bright,
            "base_ambient_bright": self._base_ambient_bright,
            "main_bright_range": self._main_bright_range,
            "ambient_bright_range": self._ambient_bright_range,
            "sync_bright_delta": self._last_sync_delta,
            "capture": last_sample_meta(),
            "capture_backend": last_capture_backend(),
            "capture_backend_pref": (os.environ.get("YEELIGHT_CAPTURE_BACKEND") or "auto"),
        }
