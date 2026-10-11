"""
轻量取色：Quartz 定位媒体窗口；优先 ScreenCaptureKit 抓区域，失败回退 mss。

语义（有播放窗口时优先窗口，保证换区域肉眼可感）：
- window：窗口画面中上部
- top：窗口画面顶部条带
- full：窗口画面大范围平均
无窗口时退回所选显示器的 top / 中央区域。

后端：环境变量 YEELIGHT_CAPTURE_BACKEND=auto|sck|mss（默认 auto）。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

_window_cache = None
_window_cache_at = 0.0
_window_cache_key = None
_WINDOW_CACHE_TTL = 1.0
_front_cache = None
_front_cache_at = 0.0
_front_cache_degrade = ""
_FRONT_CACHE_TTL = 0.35
_displays_cache: list[DisplayInfo] | None = None
_displays_cache_at = 0.0
_DISPLAYS_CACHE_TTL = 2.0
_last_color: tuple[int, int, int] = (36, 40, 48)
_last_meta: dict[str, object] = {"source": "none"}
_last_capture_ms: float = 0.0
_last_backend: str = "none"
_capture_lock = threading.Lock()
_mss = None
_mss_lock = threading.Lock()
_sck_fail_streak = 0
_SCK_COOLDOWN_FAILS = 3

_EXCLUDE = (
    "播放历史",
    "history",
    "preferences",
    "偏好",
    "设置",
    "字幕",
    "playlist",
    "播放列表",
    "inspector",
)

_BROWSER_EXCLUDE = _EXCLUDE + (
    "devtools",
    "开发者工具",
    "extensions",
    "扩展程序",
    "bookmarks",
    "书签",
    "settings",
    "下载",
    "downloads",
    "history",
    "历史记录",
)

DEFAULT_BROWSER_OWNERS = (
    "Google Chrome",
    "Chromium",
    "Microsoft Edge",
    "Brave Browser",
    "Safari",
    "Orion",
)

_SCREEN_EXCLUDE_OWNERS = (
    "易来挂灯",
    "Yeelight",
    "Electron",
    "Cursor",
    "Code",
    "Window Server",
    "Dock",
    "Control Center",
    "Notification Center",
    "SystemUIServer",
    "Spotlight",
    "loginwindow",
)

_prev_front: WindowRect | None = None
_capture_fail_streak = 0


@dataclass
class DisplayInfo:
    id: int
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False


@dataclass
class WindowRect:
    left: int
    top: int
    width: int
    height: int
    title: str | None = None
    window_id: int | None = None
    owner: str | None = None


def list_displays() -> list[DisplayInfo]:
    """缓存显示器列表，避免每帧新建 mss 会话。"""
    global _displays_cache, _displays_cache_at, _mss
    now = time.monotonic()
    if (
        _displays_cache is not None
        and now - _displays_cache_at < _DISPLAYS_CACHE_TTL
    ):
        return list(_displays_cache)
    try:
        import mss

        with _mss_lock:
            if _mss is None:
                _mss = mss.mss()
            monitors = list(_mss.monitors[1:])
        out = [
            DisplayInfo(
                id=i,
                left=int(m["left"]),
                top=int(m["top"]),
                width=int(m["width"]),
                height=int(m["height"]),
                is_primary=i == 1,
            )
            for i, m in enumerate(monitors, start=1)
        ]
        _displays_cache = out
        _displays_cache_at = now
        return list(out)
    except Exception:
        return list(_displays_cache or [])


def last_capture_ms() -> float:
    return float(_last_capture_ms)


def last_capture_backend() -> str:
    return str(_last_backend)


def _capture_backend_pref() -> str:
    raw = (os.environ.get("YEELIGHT_CAPTURE_BACKEND") or "auto").strip().lower()
    if raw in ("sck", "screencapturekit", "screen_capture_kit"):
        return "sck"
    if raw in ("mss", "legacy"):
        return "mss"
    return "auto"


def _pick_display(
    displays: list[DisplayInfo], display_id: int | None
) -> DisplayInfo | None:
    if not displays:
        return None
    if display_id is not None:
        for d in displays:
            if d.id == int(display_id):
                return d
    for d in displays:
        if d.is_primary:
            return d
    return displays[0]


def _window_on_display(win: WindowRect, disp: DisplayInfo) -> bool:
    cx = win.left + win.width / 2
    cy = win.top + win.height / 2
    return (
        disp.left <= cx < disp.left + disp.width
        and disp.top <= cy < disp.top + disp.height
    )


def _find_owner_windows(
    owners: Sequence[str],
    *,
    exclude: Sequence[str],
    display_id: int | None,
    min_w: int = 220,
    min_h: int = 150,
    prefer_titles: Sequence[str] | None = None,
) -> WindowRect | None:
    import Quartz  # type: ignore

    owner_set = {o for o in owners if o}
    if not owner_set:
        return None

    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    windows = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    candidates: list[WindowRect] = []
    for win in windows:
        owner = win.get("kCGWindowOwnerName") or ""
        if owner not in owner_set:
            continue
        if float(win.get("kCGWindowAlpha") or 0) <= 0.05:
            continue
        if int(win.get("kCGWindowLayer") or 0) > 25:
            continue
        title = win.get("kCGWindowName") or ""
        low = title.lower()
        if any(k in low for k in exclude):
            continue
        b = win.get("kCGWindowBounds") or {}
        w, h = int(b.get("Width") or 0), int(b.get("Height") or 0)
        if w < min_w or h < min_h:
            continue
        candidates.append(
            WindowRect(
                left=int(b.get("X") or 0),
                top=int(b.get("Y") or 0),
                width=w,
                height=h,
                title=title,
                window_id=int(win.get("kCGWindowNumber") or 0) or None,
                owner=owner,
            )
        )

    if not candidates:
        return None

    displays = list_displays()
    disp = _pick_display(displays, display_id)
    pool = candidates
    if disp is not None:
        on_disp = [c for c in candidates if _window_on_display(c, disp)]
        if on_disp:
            pool = on_disp

    hints = [str(t).strip().lower() for t in (prefer_titles or []) if str(t).strip()]

    def _score(c: WindowRect) -> tuple[int, int, int]:
        title = c.title or ""
        low = title.lower()
        media_hit = 1 if ("bilibili" in low or "哔哩" in title) else 0
        hint_hit = 0
        for h in hints:
            if h and h in low:
                hint_hit = 2
                break
        area = c.width * c.height + (40 * c.width if c.width >= c.height else 0)
        return (hint_hit, media_hit, area)

    return max(pool, key=_score)


def find_iina_window(
    *, use_cache: bool = True, display_id: int | None = None
) -> WindowRect | None:
    return find_media_window(
        "iina",
        owners=("IINA",),
        use_cache=use_cache,
        display_id=display_id,
    )


def find_media_window(
    media_source: str = "iina",
    *,
    owners: Sequence[str] | None = None,
    use_cache: bool = True,
    display_id: int | None = None,
    prefer_titles: Sequence[str] | None = None,
) -> WindowRect | None:
    global _window_cache, _window_cache_at, _window_cache_key

    source = (media_source or "iina").lower()
    if source == "iina":
        own = tuple(owners) if owners else ("IINA",)
        exclude = _EXCLUDE
        titles: tuple[str, ...] = ()
    else:
        own = tuple(owners) if owners else DEFAULT_BROWSER_OWNERS
        exclude = _BROWSER_EXCLUDE
        titles = tuple(prefer_titles or ())

    cache_key = (source, own, display_id, titles)
    now = time.monotonic()
    if (
        use_cache
        and _window_cache is not None
        and _window_cache_key == cache_key
        and now - _window_cache_at < _WINDOW_CACHE_TTL
    ):
        return _window_cache

    best = _find_owner_windows(
        own,
        exclude=exclude,
        display_id=display_id,
        prefer_titles=titles if source == "browser" else None,
    )
    _window_cache = best
    _window_cache_at = now
    _window_cache_key = cache_key
    return best


def _mean_color_bgr(
    arr: np.ndarray, *, vivid: bool = False
) -> tuple[int, int, int] | None:
    if arr.size == 0 or arr.ndim != 3:
        return None
    h, w = arr.shape[:2]
    if h < 2 or w < 2:
        return None
    # 大图先降采样，均值几乎不变，CPU 明显下降
    if h * w > 60000:
        step = 2 if h * w < 200000 else 3
        arr = arr[::step, ::step]
        h, w = arr.shape[:2]
    y0, y1 = int(h * 0.12), max(int(h * 0.88), int(h * 0.12) + 1)
    x0, x1 = int(w * 0.10), max(int(w * 0.90), int(w * 0.10) + 1)
    crop = arr[y0:y1, x0:x1, :3]
    if crop.size == 0:
        crop = arr[:, :, :3]

    pixels = crop.reshape(-1, 3).astype(np.float32)
    if pixels.shape[0] < 4:
        return None

    b = pixels[:, 0]
    g = pixels[:, 1]
    r = pixels[:, 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    sat = np.divide(mx - mn, mx, out=np.zeros_like(mx), where=mx > 1.0)

    if vivid:
        # 浏览器：轻量过滤灰底 UI，但仍以画面真实色为主，避免过艳
        thr = float(np.percentile(sat, 35))
        mask = sat >= max(thr, 0.06)
        if int(np.count_nonzero(mask)) < max(16, pixels.shape[0] // 40):
            mask = sat >= float(np.median(sat))
        if not np.any(mask):
            mask = np.ones(sat.shape, dtype=bool)
        # 线性饱和度加权（不再平方/二次抬高）
        wts = sat[mask] + 0.35
        rr = float(np.average(r[mask], weights=wts))
        gg = float(np.average(g[mask], weights=wts))
        bb = float(np.average(b[mask], weights=wts))
        return _boost_chroma(
            int(max(0, min(255, round(rr)))),
            int(max(0, min(255, round(gg)))),
            int(max(0, min(255, round(bb)))),
            amount=1.28,
            lift_peak=195,
            gray_push=1.25,
        )

    mean = pixels.mean(axis=0)
    return _boost_chroma(
        int(max(0, min(255, round(mean[2])))),
        int(max(0, min(255, round(mean[1])))),
        int(max(0, min(255, round(mean[0])))),
        amount=1.75,
        lift_peak=210,
        gray_push=1.8,
    )


def _boost_chroma(
    r: int,
    g: int,
    b: int,
    amount: float = 1.75,
    lift_peak: float = 210,
    gray_push: float = 1.8,
) -> tuple[int, int, int]:
    """适度拉高饱和度与峰值；amount/gray_push 越大越艳。"""
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    r2 = y + (r - y) * amount
    g2 = y + (g - y) * amount
    b2 = y + (b - y) * amount
    peak = max(r2, g2, b2, 1e-6)
    if peak < lift_peak:
        lift = min(1.2, lift_peak / peak)
        r2 *= lift
        g2 *= lift
        b2 *= lift
    mx = max(r2, g2, b2)
    mn = min(r2, g2, b2)
    if gray_push > 1.0 and mx > 1e-6 and (mx - mn) / mx < 0.18:
        mid = (r2 + g2 + b2) / 3.0
        r2 = mid + (r2 - mid) * gray_push
        g2 = mid + (g2 - mid) * gray_push
        b2 = mid + (b2 - mid) * gray_push
    return (
        int(max(0, min(255, round(r2)))),
        int(max(0, min(255, round(g2)))),
        int(max(0, min(255, round(b2)))),
    )


def _grab_mss(mon: dict[str, int]) -> np.ndarray:
    global _mss
    import mss

    with _mss_lock:
        if _mss is None:
            _mss = mss.mss()
        try:
            shot = _mss.grab(mon)
        except Exception:
            try:
                _mss.close()
            except Exception:
                pass
            _mss = mss.mss()
            shot = _mss.grab(mon)
        return np.asarray(shot)


def _grab(mon: dict[str, int]) -> np.ndarray:
    """优先 ScreenCaptureKit；不可用或连续失败时回退 mss。"""
    global _last_backend, _sck_fail_streak
    pref = _capture_backend_pref()

    use_sck = pref == "sck" or (
        pref == "auto" and _sck_fail_streak < _SCK_COOLDOWN_FAILS
    )
    if use_sck and pref != "mss":
        try:
            from server.sync.sck_capture import grab_region, is_available

            if pref == "sck" or is_available():
                arr = grab_region(mon)
                _sck_fail_streak = 0
                _last_backend = "sck"
                return arr
        except Exception:
            _sck_fail_streak += 1
            if pref == "sck":
                raise

    arr = _grab_mss(mon)
    _last_backend = "mss"
    # mss 成功一段时间后允许再试 SCK
    if _sck_fail_streak >= _SCK_COOLDOWN_FAILS:
        _sck_fail_streak = max(0, _sck_fail_streak - 1)
    return arr


def _is_near_fullscreen(win: WindowRect, disp: DisplayInfo | None) -> bool:
    if disp is None:
        return win.height >= 900 and win.width >= 1200
    return win.width >= disp.width * 0.92 and win.height >= disp.height * 0.88


def _sample_window(
    win: WindowRect,
    region: str,
    *,
    disp: DisplayInfo | None = None,
    fullscreen_hint: bool | None = None,
    media_source: str = "iina",
) -> dict[str, int]:
    """按区域从播放窗口内容区裁不同条带。全屏时去掉标题栏 inset。"""
    near_fs = bool(fullscreen_hint) or _is_near_fullscreen(win, disp)

    # 浏览器小窗：B 站视频在左侧，右侧是推荐；且「顶部」应对准播放器上半幅，
    # 而不是标签栏下的网页顶栏。
    if media_source == "browser" and not near_fs:
        chrome_h = 100 if win.height >= 280 else (72 if win.height >= 200 else 52)
        bottom_pad = 12
        content_top = win.top + chrome_h
        content_h = max(80, win.height - chrome_h - bottom_pad)
        left_pad = int(win.width * 0.03)
        right_pad = int(win.width * 0.30)
        usable_w = max(160, win.width - left_pad - right_pad)

        if region == "top":
            cap_w = int(max(200, usable_w * 0.88))
            cap_h = int(max(72, min(150, content_h * 0.30)))
            top = int(content_top + content_h * 0.10)
            left = int(win.left + left_pad + max(0, (usable_w - cap_w) * 0.12))
        elif region == "full":
            cap_w = int(max(220, usable_w * 0.75))
            cap_h = int(max(120, content_h * 0.45))
            top = int(content_top + content_h * 0.12)
            left = int(win.left + left_pad + max(0, (usable_w - cap_w) * 0.18))
        else:
            # window：播放器中部偏上（默认最稳）
            cap_w = int(max(200, usable_w * 0.58))
            cap_h = int(max(96, min(200, content_h * 0.38)))
            top = int(content_top + content_h * 0.20)
            left = int(win.left + left_pad + max(0, (usable_w - cap_w) * 0.22))

        return {
            "left": left,
            "top": top,
            "width": max(8, int(cap_w)),
            "height": max(8, int(cap_h)),
        }

    title = 0 if near_fs else (36 if win.height >= 160 else 14)
    bottom_pad = 0 if near_fs else 10
    content_top = win.top + title
    content_h = max(80, win.height - title - bottom_pad)
    side_pad = 0

    if region == "top":
        cap_w = int(max(200, (win.width - 2 * side_pad) * 0.94))
        cap_h = int(max(48, min(120, content_h * 0.22)))
        top = int(content_top + 2)
    elif region == "full":
        scale_w = 0.9
        scale_h = 0.62
        cap_w = int(max(220, (win.width - 2 * side_pad) * scale_w))
        cap_h = int(max(100, content_h * scale_h))
        top = int(content_top + content_h * 0.12)
    else:
        scale_w = 0.58
        cap_w = int(max(180, min(win.width - 16 - 2 * side_pad, (win.width - 2 * side_pad) * scale_w)))
        cap_h = int(max(48, min(96, content_h * 0.20)))
        top = int(content_top + content_h * 0.32)

    left = int(win.left + side_pad + (win.width - 2 * side_pad - cap_w) * 0.5)
    return {
        "left": left,
        "top": top,
        "width": max(8, int(cap_w)),
        "height": max(8, int(cap_h)),
    }


def _sample_display(disp: DisplayInfo, region: str) -> dict[str, int]:
    """显示器条带采样（前台窗找不到时的降级；非整屏主路径）。"""
    max_w = min(disp.width, 1280)
    if region == "full":
        cap_w = int(max(320, min(max_w, disp.width * 0.72)))
        cap_h = int(max(160, min(360, disp.height * 0.55)))
        left = int(disp.left + (disp.width - cap_w) / 2)
        top = int(disp.top + disp.height * 0.18)
    elif region == "top":
        cap_w = int(max(280, min(max_w, disp.width * 0.70)))
        cap_h = int(max(64, min(160, disp.height * 0.14)))
        left = int(disp.left + (disp.width - cap_w) / 2)
        top = int(disp.top + max(28, disp.height * 0.04))
    else:
        cap_w = int(max(240, min(max_w, disp.width * 0.50)))
        cap_h = int(max(80, min(200, disp.height * 0.18)))
        left = int(disp.left + (disp.width - cap_w) / 2)
        top = int(disp.top + disp.height * 0.28)
    return {
        "left": left,
        "top": top,
        "width": max(8, int(cap_w)),
        "height": max(8, int(cap_h)),
    }


def _sample_display_whole(disp: DisplayInfo) -> dict[str, int]:
    """取色目标=整屏：采所选显示器绝大部分（避开菜单栏），不是小条带。"""
    menu = max(24, int(disp.height * 0.03))
    side = max(8, int(disp.width * 0.015))
    bottom = max(8, int(disp.height * 0.02))
    return {
        "left": int(disp.left + side),
        "top": int(disp.top + menu),
        "width": max(8, int(disp.width - 2 * side)),
        "height": max(8, int(disp.height - menu - bottom)),
    }


def reset_last_color(rgb: tuple[int, int, int] | None = None) -> None:
    global _last_color, _last_meta
    _last_color = rgb or (36, 40, 48)
    _last_meta = {"source": "reset"}


def last_sample_meta() -> dict[str, object]:
    return dict(_last_meta)


def sample_region_color(
    display_id: int | None = None,
    region: str = "window",
    top_ratio: float = 0.08,
    *,
    media_source: str = "iina",
    owners: Sequence[str] | None = None,
    fullscreen_hint: bool | None = None,
    capture_target: str | None = None,
    user_rect: dict[str, Any] | None = None,
    prefer_titles: Sequence[str] | None = None,
) -> tuple[int, int, int]:
    global _last_color, _last_meta, _capture_fail_streak, _last_capture_ms
    _ = top_ratio
    region = region if region in ("window", "top", "full") else "window"
    media_source = (media_source or "iina").lower()
    t_all = time.perf_counter()

    if not _capture_lock.acquire(blocking=False):
        return _last_color
    try:
        displays = list_displays()
        disp = _pick_display(displays, display_id)

        # —— 屏幕源：整屏 / 前台窗 / 自选区域 ——
        if media_source == "screen":
            target = (capture_target or "front_window").lower()
            mon: dict[str, int] | None = None
            source = "none"
            win: WindowRect | None = None
            degrade = ""

            if target == "user_rect" and user_rect:
                try:
                    mon = {
                        "left": int(user_rect["left"]),
                        "top": int(user_rect["top"]),
                        "width": max(8, int(user_rect["width"])),
                        "height": max(8, int(user_rect["height"])),
                    }
                    source = "user_rect"
                except (KeyError, TypeError, ValueError):
                    mon = None
            elif target == "display" and disp is not None:
                # 整屏 = 显示器大范围，不套用「窗口中部/顶部」条带
                mon = _sample_display_whole(disp)
                source = "display:whole"
            else:
                win, degrade = find_front_window()
                if win is not None:
                    mon = _sample_window(
                        win,
                        region,
                        disp=disp,
                        fullscreen_hint=fullscreen_hint,
                        media_source="screen",
                    )
                    source = f"window:{region}:{degrade}"
                elif disp is not None:
                    mon = _sample_display_whole(disp)
                    source = "display:fallback"
                    degrade = "fallback_display"

            if mon is None:
                _capture_fail_streak += 1
                _last_meta = {
                    "source": "none",
                    "display_id": display_id,
                    "region": region,
                    "media_source": "screen",
                    "capture_target": target,
                    "degrade": degrade,
                    "fail_streak": _capture_fail_streak,
                }
                return _last_color

            arr = _grab(mon)
            color = _mean_color_bgr(arr, vivid=False)
            if color is None:
                _capture_fail_streak += 1
            else:
                _capture_fail_streak = 0
                _last_color = color
            _last_capture_ms = (time.perf_counter() - t_all) * 1000.0
            _last_meta = {
                "source": source,
                "display_id": display_id,
                "region": region,
                "rect": dict(mon),
                "has_window": win is not None,
                "media_source": "screen",
                "capture_target": target,
                "degrade": degrade,
                "window_owner": win.owner if win else None,
                "window_title": win.title if win else None,
                "vivid": False,
                "fail_streak": _capture_fail_streak,
                "capture_ms": round(_last_capture_ms, 1),
                "backend": _last_backend,
            }
            return _last_color if color is None else color

        win = find_media_window(
            media_source,
            owners=owners,
            use_cache=True,
            display_id=display_id,
            prefer_titles=prefer_titles if media_source == "browser" else None,
        )

        mon = None
        source = "none"
        if win is not None:
            mon = _sample_window(
                win,
                region,
                disp=disp,
                fullscreen_hint=fullscreen_hint,
                media_source=media_source,
            )
            source = f"window:{region}"
        elif disp is not None:
            mon = _sample_display(disp, "top" if region == "window" else region)
            source = f"display:{region}"

        if mon is None:
            _last_meta = {
                "source": "none",
                "display_id": display_id,
                "region": region,
                "media_source": media_source,
            }
            return _last_color

        arr = _grab(mon)
        color = _mean_color_bgr(arr, vivid=(media_source == "browser"))
        _last_capture_ms = (time.perf_counter() - t_all) * 1000.0
        _last_meta = {
            "source": source,
            "display_id": display_id,
            "region": region,
            "rect": dict(mon),
            "has_window": win is not None,
            "media_source": media_source,
            "window_owner": win.owner if win else None,
            "window_title": win.title if win else None,
            "vivid": media_source == "browser",
            "capture_ms": round(_last_capture_ms, 1),
            "backend": _last_backend,
        }
        if color is None:
            return _last_color
        _last_color = color
        return color
    except Exception as exc:  # noqa: BLE001
        _capture_fail_streak += 1
        _last_capture_ms = (time.perf_counter() - t_all) * 1000.0
        _last_meta = {
            "source": "error",
            "error": str(exc),
            "display_id": display_id,
            "region": region,
            "media_source": media_source,
            "fail_streak": _capture_fail_streak,
            "capture_ms": round(_last_capture_ms, 1),
        }
        return _last_color
    finally:
        _capture_lock.release()


def is_screen_locked() -> bool:
    """锁屏 / 会话锁。失败时返回 False（由采集失败兜底）。"""
    try:
        import Quartz  # type: ignore

        info = Quartz.CGSessionCopyCurrentDictionary()
        if not info:
            return False
        if info.get("CGSSessionScreenIsLocked"):
            return True
        # 部分系统用此键表示锁屏屏保
        if info.get("kCGSSessionOnConsoleKey") is False:
            return False
        return False
    except Exception:
        return False


def find_front_window(
    *,
    exclude_owners: Sequence[str] | None = None,
    min_w: int = 220,
    min_h: int = 150,
    min_area_ratio: float = 0.05,
    use_cache: bool = True,
) -> tuple[WindowRect | None, str]:
    """
    找前台可用窗。返回 (窗, 降级说明)。
    命中排除名单或过小 → 尝试上一前台 → 再不行返回 None（调用方退整屏）。
    """
    global _prev_front, _front_cache, _front_cache_at, _front_cache_degrade
    now = time.monotonic()
    if (
        use_cache
        and _front_cache is not None
        and now - _front_cache_at < _FRONT_CACHE_TTL
    ):
        return _front_cache, _front_cache_degrade

    import Quartz  # type: ignore

    excl = {o.lower() for o in (exclude_owners or _SCREEN_EXCLUDE_OWNERS)}
    displays = list_displays()
    screen_area = sum(max(1, d.width * d.height) for d in displays) or 1

    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    windows = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []

    def _ok(win: WindowRect) -> bool:
        owner = (win.owner or "").lower()
        if any(x in owner for x in excl):
            return False
        if win.width < min_w or win.height < min_h:
            return False
        if (win.width * win.height) / screen_area < min_area_ratio:
            return False
        return True

    front: WindowRect | None = None
    for win in windows:
        layer = int(win.get("kCGWindowLayer") or 0)
        if layer != 0:
            continue
        if float(win.get("kCGWindowAlpha") or 0) <= 0.05:
            continue
        b = win.get("kCGWindowBounds") or {}
        try:
            w = int(b.get("Width") or 0)
            h = int(b.get("Height") or 0)
            left = int(b.get("X") or 0)
            top = int(b.get("Y") or 0)
        except (TypeError, ValueError):
            continue
        if w < 80 or h < 80:
            continue
        cand = WindowRect(
            left=left,
            top=top,
            width=w,
            height=h,
            title=win.get("kCGWindowName") or None,
            window_id=int(win.get("kCGWindowNumber") or 0) or None,
            owner=win.get("kCGWindowOwnerName") or None,
        )
        # 列表大致按前到后；取第一个 layer0 大窗作为「前台候选」
        front = cand
        break

    if front and _ok(front):
        _prev_front = front
        _front_cache, _front_cache_degrade, _front_cache_at = front, "front", now
        return front, "front"

    if _prev_front and _ok(_prev_front):
        _front_cache, _front_cache_degrade, _front_cache_at = _prev_front, "previous", now
        return _prev_front, "previous"

    _front_cache, _front_cache_degrade, _front_cache_at = None, "fallback_display", now
    return None, "fallback_display"


def screen_target_valid(
    *,
    capture_target: str,
    display_id: int | None,
    user_rect: dict[str, Any] | None,
) -> bool:
    target = (capture_target or "front_window").lower()
    if target == "user_rect":
        if not user_rect:
            return False
        try:
            return int(user_rect.get("width") or 0) >= 8 and int(user_rect.get("height") or 0) >= 8
        except (TypeError, ValueError):
            return False
    if target == "display":
        return _pick_display(list_displays(), display_id) is not None
    # front_window：有任意显示器即可尝试（降级整屏）
    return bool(list_displays())


def screen_is_fullscreenish(
    *,
    capture_target: str,
    display_id: int | None,
    user_rect: dict[str, Any] | None,
) -> bool:
    target = (capture_target or "front_window").lower()
    displays = list_displays()
    disp = _pick_display(displays, display_id)
    if target == "display":
        return True
    if target == "user_rect" and user_rect and disp:
        try:
            w = int(user_rect.get("width") or 0)
            h = int(user_rect.get("height") or 0)
            return w >= disp.width * 0.85 and h >= disp.height * 0.85
        except (TypeError, ValueError):
            return False
    win, _ = find_front_window()
    if win is None or disp is None:
        return False
    return _is_near_fullscreen(win, disp)


def invalidate_window_cache() -> None:
    global _window_cache, _window_cache_at, _window_cache_key
    global _front_cache, _front_cache_at, _front_cache_degrade
    _window_cache = None
    _window_cache_at = 0.0
    _window_cache_key = None
    _front_cache = None
    _front_cache_at = 0.0
    _front_cache_degrade = ""


def capture_fail_streak() -> int:
    return int(_capture_fail_streak)
