"""
ScreenCaptureKit 区域截屏（macOS 12.3+ / 优先 14+ SCScreenshotManager）。

输出与 mss.grab 对齐的 BGRA uint8 ndarray，供 capture._mean_color_bgr 复用。
失败时由上层回退 mss。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np

_lock = threading.Lock()
_available: bool | None = None
_last_error: str = ""
_last_ms: float = 0.0


def last_error() -> str:
    return _last_error


def last_grab_ms() -> float:
    return float(_last_ms)


def is_available() -> bool:
    global _available
    if _available is not None:
        return _available
    try:
        import ScreenCaptureKit  # noqa: F401
        import Quartz  # noqa: F401

        _available = True
    except Exception as exc:  # noqa: BLE001
        _available = False
        globals()["_last_error"] = f"import:{exc}"
    return bool(_available)


def _cgimage_to_bgra(img: Any) -> np.ndarray | None:
    import Quartz  # type: ignore

    w = int(Quartz.CGImageGetWidth(img))
    h = int(Quartz.CGImageGetHeight(img))
    if w < 1 or h < 1:
        return None
    bpr = int(Quartz.CGImageGetBytesPerRow(img))
    bpp = int(Quartz.CGImageGetBitsPerPixel(img))
    if bpp != 32 or bpr < w * 4:
        # 少见格式：走位图上下文转成 BGRA
        data = bytearray(h * w * 4)
        cs = Quartz.CGColorSpaceCreateDeviceRGB()
        ctx = Quartz.CGBitmapContextCreate(
            data,
            w,
            h,
            8,
            w * 4,
            cs,
            Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little,
        )
        if ctx is None:
            return None
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), img)
        return np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4).copy()

    prov = Quartz.CGImageGetDataProvider(img)
    if prov is None:
        return None
    cf = Quartz.CGDataProviderCopyData(prov)
    if cf is None:
        return None
    raw = bytes(cf)
    if bpr == w * 4:
        return np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4).copy()
    row = np.frombuffer(raw, dtype=np.uint8).reshape(h, bpr)
    return row[:, : w * 4].reshape(h, w, 4).copy()


def grab_region(mon: dict[str, int], *, timeout: float = 1.0) -> np.ndarray:
    """截取虚拟桌面坐标下的矩形，返回 BGRA。失败抛异常。"""
    global _last_error, _last_ms
    if not is_available():
        raise RuntimeError(_last_error or "ScreenCaptureKit unavailable")

    import Quartz  # type: ignore
    import ScreenCaptureKit as sck  # type: ignore

    left = int(mon["left"])
    top = int(mon["top"])
    width = max(1, int(mon["width"]))
    height = max(1, int(mon["height"]))
    rect = Quartz.CGRectMake(left, top, width, height)

    box: dict[str, Any] = {}
    done = threading.Event()

    def _cb(image, error) -> None:  # noqa: ANN001
        box["image"] = image
        box["error"] = error
        done.set()

    t0 = time.perf_counter()
    with _lock:
        # 串行化 SCK 请求，避免回调交错
        sck.SCScreenshotManager.captureImageInRect_completionHandler_(rect, _cb)
        if not done.wait(timeout):
            _last_error = "timeout"
            _last_ms = (time.perf_counter() - t0) * 1000.0
            raise TimeoutError("ScreenCaptureKit capture timeout")

    err = box.get("error")
    img = box.get("image")
    if err is not None:
        _last_error = str(err)
        _last_ms = (time.perf_counter() - t0) * 1000.0
        raise RuntimeError(_last_error)
    if img is None:
        _last_error = "empty_image"
        _last_ms = (time.perf_counter() - t0) * 1000.0
        raise RuntimeError(_last_error)

    arr = _cgimage_to_bgra(img)
    _last_ms = (time.perf_counter() - t0) * 1000.0
    if arr is None:
        _last_error = "convert_failed"
        raise RuntimeError(_last_error)
    _last_error = ""
    return arr
