"""
ScreenCaptureKit 常驻 SCStream 取色（macOS）。

相对 SCScreenshotManager：流只建一次，回调里立刻拷像素，不长期持有 IOSurface。
相对 mss：通常更低延迟；默认不启用。
启用：YEELIGHT_CAPTURE_BACKEND=stream 或 config.sync.capture_backend=stream。

采样线程上不能再发 Objective-C 消息。PyObjC 在那条队列里临时注册类会空指针退出。
回调只把 CMSampleBuffer 的指针交给 CoreVideo，把 BGRA 拷进 numpy。
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from typing import Any, Callable

import numpy as np

_lock = threading.Lock()
_available: bool | None = None
_last_error: str = ""
_last_ms: float = 0.0
_session: "_StreamSession | None" = None
_fail_until = 0.0
_OutputCls: Any = None
_output_owner: "_StreamSession | None" = None
_BGRA = 0x42475241
_LOCK_RO = 1
_pyobjc_id: Any = None
_cm_get_image: Any = None
_cv_lock: Any = None
_cv_unlock: Any = None
_cv_base: Any = None
_cv_stride: Any = None
_cv_width: Any = None
_cv_height: Any = None
_cv_format: Any = None
_SCREEN_TYPE = 0
_sample_queues: list[Any] = []


def _prepare_frame_copy() -> None:
    """在开流的线程上加载 C 函数和 PyObjC，避免采样队列第一次碰到类表。"""
    global _pyobjc_id, _cm_get_image, _cv_lock, _cv_unlock
    global _cv_base, _cv_stride, _cv_width, _cv_height, _cv_format, _SCREEN_TYPE
    if _cm_get_image is not None and _pyobjc_id is not None:
        return
    import objc
    from ScreenCaptureKit import SCStreamOutputTypeScreen

    _pyobjc_id = objc.pyobjc_id
    _SCREEN_TYPE = int(SCStreamOutputTypeScreen)
    cm = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreMedia.framework/CoreMedia"
    )
    cv = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreVideo.framework/CoreVideo"
    )
    cm.CMSampleBufferGetImageBuffer.argtypes = [ctypes.c_void_p]
    cm.CMSampleBufferGetImageBuffer.restype = ctypes.c_void_p
    for fn, args, rest in (
        (cv.CVPixelBufferLockBaseAddress, [ctypes.c_void_p, ctypes.c_uint64], ctypes.c_int32),
        (cv.CVPixelBufferUnlockBaseAddress, [ctypes.c_void_p, ctypes.c_uint64], ctypes.c_int32),
        (cv.CVPixelBufferGetBaseAddress, [ctypes.c_void_p], ctypes.c_void_p),
        (cv.CVPixelBufferGetBytesPerRow, [ctypes.c_void_p], ctypes.c_size_t),
        (cv.CVPixelBufferGetWidth, [ctypes.c_void_p], ctypes.c_size_t),
        (cv.CVPixelBufferGetHeight, [ctypes.c_void_p], ctypes.c_size_t),
        (cv.CVPixelBufferGetPixelFormatType, [ctypes.c_void_p], ctypes.c_uint32),
    ):
        fn.argtypes = args
        fn.restype = rest
    _cm_get_image = cm.CMSampleBufferGetImageBuffer
    _cv_lock = cv.CVPixelBufferLockBaseAddress
    _cv_unlock = cv.CVPixelBufferUnlockBaseAddress
    _cv_base = cv.CVPixelBufferGetBaseAddress
    _cv_stride = cv.CVPixelBufferGetBytesPerRow
    _cv_width = cv.CVPixelBufferGetWidth
    _cv_height = cv.CVPixelBufferGetHeight
    _cv_format = cv.CVPixelBufferGetPixelFormatType


def _sample_queue() -> Any:
    lib = ctypes.cdll.LoadLibrary("/usr/lib/libSystem.B.dylib")
    lib.dispatch_queue_create.restype = ctypes.c_void_p
    lib.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    ptr = lib.dispatch_queue_create(b"yeelight.scstream.frames", None)
    if not ptr:
        raise RuntimeError("创建取色队列失败")
    import objc

    queue = objc.objc_object(c_void_p=ptr)
    _sample_queues.append(queue)
    return queue


def _copy_locked_bgra(pixel: int) -> np.ndarray | None:
    if not pixel or _cv_format is None:
        return None
    if int(_cv_format(pixel)) != _BGRA:
        return None
    if int(_cv_lock(pixel, _LOCK_RO)) != 0:
        return None
    try:
        base = _cv_base(pixel)
        if not base:
            return None
        width = int(_cv_width(pixel))
        height = int(_cv_height(pixel))
        stride = int(_cv_stride(pixel))
        if width < 1 or height < 1 or stride < width * 4:
            return None
        raw = ctypes.string_at(base, stride * height)
    finally:
        _cv_unlock(pixel, _LOCK_RO)
    arr = np.frombuffer(raw, dtype=np.uint8)
    if stride == width * 4:
        frame = arr.reshape(height, width, 4)
    else:
        frame = arr.reshape(height, stride)[:, : width * 4].reshape(height, width, 4)
    return np.array(frame, dtype=np.uint8, copy=True)


def _copy_sample_bgra(sample_ptr: int) -> np.ndarray | None:
    """CVPixelBuffer → 连续 BGRA。只调用已绑定的 C 函数。"""
    if not sample_ptr or _cm_get_image is None:
        return None
    pixel = _cm_get_image(sample_ptr)
    if not pixel:
        return None
    return _copy_locked_bgra(int(pixel))


def _get_output_class() -> Any:
    """NSObject 子类只注册一次，避免 'overriding existing Objective-C class'。"""
    global _OutputCls
    if _OutputCls is not None:
        return _OutputCls
    from Foundation import NSObject

    _prepare_frame_copy()

    class YeelightSCStreamOutput(NSObject):
        def stream_didOutputSampleBuffer_ofType_(  # noqa: N802
            self, _stream: Any, sample_buffer: Any, of_type: Any
        ) -> None:
            session = _output_owner
            if session is None or _pyobjc_id is None:
                return
            try:
                kind = of_type if type(of_type) is int else int(of_type)
                if kind != _SCREEN_TYPE:
                    return
                frame = _copy_sample_bgra(int(_pyobjc_id(sample_buffer) or 0))
            except Exception as exc:  # noqa: BLE001
                globals()["_last_error"] = f"frame:{exc}"
                return
            if frame is None:
                return
            with session._frame_lock:
                session._frame = frame
                session._frame_event.set()

    _OutputCls = YeelightSCStreamOutput
    return _OutputCls


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
        import CoreMedia  # noqa: F401
        from Foundation import NSObject  # noqa: F401

        _available = True
    except Exception as exc:  # noqa: BLE001
        _available = False
        globals()["_last_error"] = f"import:{exc}"
    return bool(_available)


def shutdown() -> None:
    global _session
    with _lock:
        sess = _session
        _session = None
    if sess is not None:
        sess.stop()


def grab_region(mon: dict[str, int], *, timeout: float = 1.2) -> np.ndarray:
    """截取虚拟桌面坐标矩形，返回 BGRA uint8。失败抛异常。"""
    global _last_error, _last_ms, _session, _fail_until
    if not is_available():
        raise RuntimeError(_last_error or "SCStream unavailable")
    if time.monotonic() < _fail_until:
        raise RuntimeError(_last_error or "SCStream cooling down")

    left = int(mon["left"])
    top = int(mon["top"])
    width = max(1, int(mon["width"]))
    height = max(1, int(mon["height"]))
    t0 = time.perf_counter()

    with _lock:
        if _session is None:
            _session = _StreamSession()
        sess = _session

    try:
        arr = sess.grab(left, top, width, height, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        _last_error = str(exc)
        _last_ms = (time.perf_counter() - t0) * 1000.0
        _fail_until = time.monotonic() + 2.5
        try:
            sess.stop()
        except Exception:
            pass
        with _lock:
            if _session is sess:
                _session = None
        raise

    _last_error = ""
    _last_ms = (time.perf_counter() - t0) * 1000.0
    return arr


class _StreamSession:
    """所有 SCK 调用放在自带 NSRunLoop 的线程，避免工作线程 completion 永不回调。"""

    def __init__(self) -> None:
        self._jobs: queue.Queue[tuple[Callable[[], Any], queue.Queue[Any]] | None] = (
            queue.Queue()
        )
        self._frame_lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._frame_event = threading.Event()
        self._stream: Any = None
        self._output: Any = None
        self._keep: list[Any] = []
        self._content_at = 0.0
        self._content: Any = None
        self._key: tuple[Any, ...] | None = None
        self._running = False
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main, name="scstream-runloop", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(3.0):
            raise RuntimeError("SCStream runloop thread failed to start")

    def stop(self) -> None:
        try:
            self._call(self._stop_on_loop, timeout=3.0)
        except Exception:
            pass
        self._jobs.put(None)

    def grab(
        self, left: int, top: int, width: int, height: int, *, timeout: float
    ) -> np.ndarray:
        self._call(
            lambda: self._ensure_stream_on_loop(left, top, width, height),
            timeout=max(2.0, timeout),
        )
        deadline = time.monotonic() + max(0.2, float(timeout))
        while time.monotonic() < deadline:
            with self._frame_lock:
                frame = self._frame
                if frame is not None:
                    return frame
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            self._frame_event.wait(min(0.05, remain))
            self._frame_event.clear()
        raise TimeoutError(last_error() or "SCStream frame timeout")

    def _call(self, fn: Callable[[], Any], *, timeout: float) -> Any:
        reply: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._jobs.put((fn, reply))
        try:
            kind, payload = reply.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("SCStream runloop call timeout") from exc
        if kind == "err":
            raise payload
        return payload

    def _thread_main(self) -> None:
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop

        loop = NSRunLoop.currentRunLoop()
        self._ready.set()
        while True:
            try:
                item = self._jobs.get(timeout=0.02)
            except queue.Empty:
                item = None
            if item is None and self._jobs.empty():
                # 空转泵 RunLoop，让 start/stop/frame 回调能进来
                loop.runMode_beforeDate_(
                    NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.02)
                )
                continue
            if item is None:
                # 哨兵：退出
                try:
                    self._stop_on_loop()
                except Exception:
                    pass
                break
            fn, reply = item
            try:
                result = fn()
                reply.put(("ok", result))
            except Exception as exc:  # noqa: BLE001
                reply.put(("err", exc))
            # 每笔任务后再泵一点，消化 completion
            for _ in range(5):
                loop.runMode_beforeDate_(
                    NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.01)
                )

    def _stop_on_loop(self) -> None:
        global _output_owner
        stream = self._stream
        self._stream = None
        self._output = None
        self._keep.clear()
        self._key = None
        self._running = False
        self._content = None
        self._content_at = 0.0
        if _output_owner is self:
            _output_owner = None
        if stream is not None:
            done = threading.Event()
            stream.stopCaptureWithCompletionHandler_(lambda _e: done.set())
            _wait_runloop(done, 2.0)
        with self._frame_lock:
            self._frame = None
            self._frame_event.clear()

    def _ensure_stream_on_loop(
        self, left: int, top: int, width: int, height: int
    ) -> None:
        content = self._shareable_content_on_loop()
        display, local = _pick_display_and_local(content, left, top, width, height)
        out_w, out_h = _output_size(local[2], local[3])
        key = (
            int(display.displayID()),
            int(local[0] // 8),
            int(local[1] // 8),
            int(local[2] // 8),
            int(local[3] // 8),
            out_w,
            out_h,
        )
        if self._running and self._key == key and self._stream is not None:
            return
        if self._stream is not None:
            self._stop_on_loop()
        self._start_stream_on_loop(display, local, out_w, out_h)
        self._key = key

    def _shareable_content_on_loop(self) -> Any:
        now = time.monotonic()
        if self._content is not None and now - self._content_at < 2.5:
            return self._content
        from ScreenCaptureKit import SCShareableContent

        box: dict[str, Any] = {}
        done = threading.Event()

        def _cb(content: Any, error: Any) -> None:
            box["c"] = content
            box["e"] = error
            done.set()

        SCShareableContent.getShareableContentWithCompletionHandler_(_cb)
        if not _wait_runloop(done, 3.0):
            raise TimeoutError("SCShareableContent timeout")
        if box.get("e") is not None:
            raise RuntimeError(f"SCShareableContent: {box['e']}")
        content = box.get("c")
        if content is None:
            raise RuntimeError("SCShareableContent empty")
        self._content = content
        self._content_at = now
        return content

    def _start_stream_on_loop(
        self,
        display: Any,
        local: tuple[int, int, int, int],
        out_w: int,
        out_h: int,
    ) -> None:
        global _output_owner
        from Foundation import NSArray
        from ScreenCaptureKit import (
            SCContentFilter,
            SCStream,
            SCStreamConfiguration,
            SCStreamOutputTypeScreen,
        )
        from CoreMedia import CMTimeMake
        from Quartz import CGRectMake, kCVPixelFormatType_32BGRA

        _prepare_frame_copy()
        lx, ly, lw, lh = local
        empty = NSArray.array()
        filt = SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
            display, empty, empty
        )
        conf = SCStreamConfiguration.alloc().init()
        conf.setWidth_(out_w)
        conf.setHeight_(out_h)
        conf.setQueueDepth_(2)
        conf.setShowsCursor_(False)
        conf.setCapturesAudio_(False)
        conf.setScalesToFit_(True)
        conf.setPixelFormat_(kCVPixelFormatType_32BGRA)
        conf.setMinimumFrameInterval_(CMTimeMake(1, 15))
        conf.setSourceRect_(CGRectMake(float(lx), float(ly), float(lw), float(lh)))

        _output_owner = self
        output = _get_output_class().alloc().init()
        if _pyobjc_id is not None:
            _pyobjc_id(output)
        queue = _sample_queue()
        stream = SCStream.alloc().initWithFilter_configuration_delegate_(
            filt, conf, None
        )
        result = stream.addStreamOutput_type_sampleHandlerQueue_error_(
            output, SCStreamOutputTypeScreen, queue, None
        )
        err = None
        if isinstance(result, tuple):
            ok, err = result[0], result[1] if len(result) > 1 else None
            if ok is False and err is None:
                err = "addStreamOutput failed"
        elif result is False:
            err = "addStreamOutput failed"
        if err is not None:
            raise RuntimeError(f"addStreamOutput: {err}")

        started = threading.Event()
        start_err: dict[str, Any] = {}

        def _on_start(error: Any) -> None:
            if error is not None:
                start_err["e"] = error
            started.set()

        stream.startCaptureWithCompletionHandler_(_on_start)
        if not _wait_runloop(started, 4.0):
            raise TimeoutError("SCStream start timeout")
        if start_err.get("e") is not None:
            raise RuntimeError(f"SCStream start: {start_err['e']}")

        # 强引用，防止 PyObjC 提前释放
        self._keep = [filt, conf, output, stream, display, queue]
        self._stream = stream
        self._output = output
        self._running = True


def _wait_runloop(event: threading.Event, timeout: float) -> bool:
    try:
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop
    except Exception:
        return event.wait(timeout)
    deadline = time.monotonic() + max(0.05, float(timeout))
    loop = NSRunLoop.currentRunLoop()
    while time.monotonic() < deadline:
        if event.is_set():
            return True
        loop.runMode_beforeDate_(
            NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.02)
        )
        if event.wait(0.0):
            return True
    return event.is_set()


def _output_size(src_w: int, src_h: int) -> tuple[int, int]:
    sw = max(1, int(src_w))
    sh = max(1, int(src_h))
    long_side = max(sw, sh)
    if long_side <= 240:
        return sw, sh
    scale = 240.0 / long_side
    return max(8, int(round(sw * scale))), max(8, int(round(sh * scale)))


def _pick_display_and_local(
    content: Any, left: int, top: int, width: int, height: int
) -> tuple[Any, tuple[int, int, int, int]]:
    displays = list(content.displays() or [])
    if not displays:
        raise RuntimeError("no SCDisplay")
    cx = left + width * 0.5
    cy = top + height * 0.5
    best = None
    best_score = -1.0
    for d in displays:
        f = d.frame()
        dx, dy = float(f.origin.x), float(f.origin.y)
        dw, dh = float(f.size.width), float(f.size.height)
        x0 = max(float(left), dx)
        y0 = max(float(top), dy)
        x1 = min(float(left + width), dx + dw)
        y1 = min(float(top + height), dy + dh)
        area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
        contains = dx <= cx <= dx + dw and dy <= cy <= dy + dh
        score = area + (1e12 if contains else 0.0)
        if score > best_score:
            best_score = score
            best = (d, dx, dy, dw, dh)
    assert best is not None
    d, dx, dy, dw, dh = best
    lx = int(max(0, min(dw - 1, left - dx)))
    ly = int(max(0, min(dh - 1, top - dy)))
    lw = int(max(1, min(dw - lx, width)))
    lh = int(max(1, min(dh - ly, height)))
    return d, (lx, ly, lw, lh)
