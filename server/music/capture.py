"""电脑播放（ScreenCaptureKit）和麦克风（AVAudioEngine）采样。"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable

import numpy as np

PushFn = Callable[[np.ndarray, int], None]


class SampleRing:
    def __init__(self, rate: int = 48000, seconds: float = 0.5) -> None:
        self.rate = rate
        self._cap = int(rate * seconds)
        self._chunks: list[np.ndarray] = []
        self._total = 0
        self._written = 0
        self._read_total = 0
        self._lock = threading.Lock()

    def push(self, samples: np.ndarray, rate: int) -> None:
        # 数值计算放在锁外面。锁里如果做会放开 GIL 的 numpy 写入，
        # 会和采集回调互相卡住，灯就不再更新。
        mono = _mono(samples, int(rate), self.rate)
        if mono.size == 0:
            return
        with self._lock:
            self._chunks.append(mono)
            self._total += int(mono.size)
            self._written += int(mono.size)
            while self._total > self._cap and self._chunks:
                drop = self._chunks.pop(0)
                self._total -= int(drop.size)

    def take_new(self, max_seconds: float = 0.09) -> np.ndarray:
        """取出还没分析过的采样。落后太多就丢掉旧的，避免补帧时连打一串假节拍。"""
        max_n = max(1, int(self.rate * max(0.02, float(max_seconds))))
        with self._lock:
            dropped = self._written - self._total
            if self._read_total < dropped:
                self._read_total = dropped
            available = self._written - self._read_total
            if available <= 0 or not self._chunks:
                return np.zeros(0, dtype=np.float32)
            if available > int(self.rate * 0.25):
                self._read_total = self._written - min(max_n, available)
                dropped = self._written - self._total
                available = self._written - self._read_total
            n = min(available, max_n)
            start = self._read_total - dropped
            self._read_total += n
            chunks = list(self._chunks)
        if not chunks or n <= 0:
            return np.zeros(0, dtype=np.float32)
        cat = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        start = max(0, min(int(start), cat.size))
        end = max(start, min(cat.size, start + n))
        return np.ascontiguousarray(cat[start:end], dtype=np.float32)

    def latest(self, seconds: float = 0.35) -> np.ndarray:
        need = max(1, int(self.rate * seconds))
        with self._lock:
            chunks = list(self._chunks)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        cat = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        if cat.size > need:
            cat = cat[-need:]
        return np.ascontiguousarray(cat, dtype=np.float32)


def _mono(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return arr
    if src_rate <= 0:
        src_rate = dst_rate
    if src_rate != dst_rate and arr.size > 1:
        n_out = max(1, int(round(arr.size * dst_rate / src_rate)))
        x_old = np.linspace(0.0, 1.0, arr.size, dtype=np.float32)
        x_new = np.linspace(0.0, 1.0, n_out, dtype=np.float32)
        arr = np.interp(x_new, x_old, arr).astype(np.float32)
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    if peak > 2.0:
        arr = arr / peak
    return arr


_PERM_ERROR = (
    "需要屏幕录制权限才能听电脑播放。"
    "请打开系统设置 → 隐私与安全性 → 屏幕录制，打开「Yeelight 显示器挂灯」。"
    "若开关已经开着，请先关掉，完全退出本应用后再打开，然后重新打开应用。"
)
_MIC_PERM_ERROR = (
    "没有麦克风权限。"
    "请打开系统设置 → 隐私与安全性 → 麦克风，打开「Yeelight 显示器挂灯」。"
)


# #region agent log
def _agent_log(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    try:
        import json

        line = json.dumps(
            {
                "sessionId": "d219fd",
                "hypothesisId": hypothesis_id,
                "location": location,
                "message": message,
                "data": data,
                "timestamp": int(time.time() * 1000),
            },
            ensure_ascii=False,
        )
        with open(
            "/Users/totoro/工作/易来控制/.cursor/debug-d219fd.log",
            "a",
            encoding="utf-8",
        ) as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# #endregion


def _screen_recording_granted() -> bool | None:
    """None 表示查不到。查不到时不要连着调用会弹窗的接口。"""
    try:
        from Quartz import CGPreflightScreenCaptureAccess
    except Exception:
        return None
    try:
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return None


def _live_permissions() -> dict[str, str]:
    """Electron 会持续写入最新 TCC 状态；比进程启动时的环境变量更准。"""
    out = {
        "screen": (os.environ.get("YEELIGHT_SCREEN_ACCESS") or "").strip().lower(),
        "microphone": (os.environ.get("YEELIGHT_MIC_ACCESS") or "").strip().lower(),
    }
    raw = (os.environ.get("YEELIGHT_CONFIG_DIR") or "").strip()
    if not raw:
        return out
    path = os.path.join(raw, "music-permissions.json")
    try:
        import json
        from pathlib import Path

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            screen = str(data.get("screen") or "").strip().lower()
            mic = str(data.get("microphone") or "").strip().lower()
            if screen:
                out["screen"] = screen
            if mic:
                out["microphone"] = mic
    except Exception:
        pass
    return out


def sync_permission_file(*, screen: str | None = None, microphone: str | None = None) -> None:
    """用系统预检结果回写权限文件，避免 Electron 误报 denied 覆盖真实授权。"""
    raw = (os.environ.get("YEELIGHT_CONFIG_DIR") or "").strip()
    if not raw:
        if screen is not None:
            os.environ["YEELIGHT_SCREEN_ACCESS"] = screen
        if microphone is not None:
            os.environ["YEELIGHT_MIC_ACCESS"] = microphone
        return
    import json
    from pathlib import Path

    path = Path(raw) / "music-permissions.json"
    data: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        data = {}
    if screen is not None:
        data["screen"] = screen
        data["screenValidated"] = screen == "granted"
        os.environ["YEELIGHT_SCREEN_ACCESS"] = screen
    if microphone is not None:
        data["microphone"] = microphone
        data["micValidated"] = microphone == "granted"
        os.environ["YEELIGHT_MIC_ACCESS"] = microphone
    data["updatedAt"] = int(time.time() * 1000)
    data["syncedBy"] = "python-preflight"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def live_permission_snapshot() -> dict:
    """Electron / API 共用的权限快照；effective_screen 只认 CGPreflight。"""
    live = _live_permissions()
    preflight = _screen_recording_granted()
    mic_auth = -1
    try:
        import AVFoundation as av  # type: ignore

        mic_auth = int(av.AVCaptureDevice.authorizationStatusForMediaType_("soun"))
    except Exception:
        mic_auth = -1
    screen = live.get("screen") or ""
    microphone = live.get("microphone") or ""
    # 预检为假时，文件里的 granted 不可信（常是 Electron 误写 sticky）。
    if preflight is True:
        effective_screen = "granted"
        if screen != "granted":
            sync_permission_file(screen="granted")
            screen = "granted"
    elif preflight is False:
        effective_screen = "denied"
        if screen == "granted":
            sync_permission_file(screen="denied")
            screen = "denied"
    else:
        effective_screen = screen
    # 3 = authorized
    effective_microphone = "granted" if mic_auth == 3 else microphone
    return {
        "screen": screen,
        "microphone": microphone,
        "preflight": preflight,
        "mic_auth": mic_auth,
        "effective_screen": effective_screen,
        "effective_microphone": effective_microphone,
    }


def _is_permission_failure(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in ("tcc", "denied", "拒绝", "not authorized", "permission", "屏幕录制", "麦克风")
    )


class AudioInput:
    def __init__(self) -> None:
        self.kind: str | None = None
        self.error: str | None = None
        self.ring = SampleRing()
        self.packets = 0
        self.peak = 0.0
        self._stop: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self._retry_after = 0.0
        self._gave_up = False
        # 预检失败时整个进程只真正启动一次采集，避免授权框反复弹出。
        self._system_capture_tried = False
        self._mic_tried = False
        self._last_block_log = ""

    def ensure(self, kind: str) -> None:
        kind = "mic" if kind == "mic" else "system"
        with self._lock:
            if self.kind == kind and self._stop is not None and not self.error:
                return
            now = time.monotonic()
            live = _live_permissions()
            if kind == "system":
                granted = _screen_recording_granted()
                parent = live.get("screen") or ""
                # 权威：CGPreflight。为假时绝不能因权限文件里的假 sticky 去打 SCK（会反复弹窗）。
                if granted is True:
                    if parent != "granted":
                        sync_permission_file(screen="granted")
                        parent = "granted"
                        live["screen"] = "granted"
                    allowed = True
                    self._gave_up = False
                    self._system_capture_tried = False
                elif granted is False:
                    if parent == "granted":
                        # 清掉 Electron 误写的 sticky，避免下一 tick 再冲采集。
                        sync_permission_file(screen="denied")
                        parent = "denied"
                        live["screen"] = "denied"
                    allowed = False
                else:
                    # 查不到预检时：仅当父进程明确 granted 时试一次，失败后不再试。
                    allowed = parent == "granted" and not self._system_capture_tried
                if not allowed:
                    if self._stop is not None:
                        self._close_locked()
                    self.kind = kind
                    self.error = _PERM_ERROR
                    self._gave_up = True
                    key = f"system:{parent}:{granted}:{self._gave_up}:{self._system_capture_tried}"
                    if key != self._last_block_log:
                        self._last_block_log = key
                        # #region agent log
                        _agent_log(
                            "P3",
                            "capture.py:ensure",
                            "system blocked — no SCK without preflight",
                            {
                                "preflight": granted,
                                "parent": parent,
                                "gave_up": self._gave_up,
                                "tried": self._system_capture_tried,
                            },
                        )
                        # #endregion
                    return
            else:
                mic_status = live.get("microphone") or ""
                try:
                    import AVFoundation as av  # type: ignore

                    if int(av.AVCaptureDevice.authorizationStatusForMediaType_("soun")) == 3:
                        if mic_status != "granted":
                            sync_permission_file(microphone="granted")
                        mic_status = "granted"
                        live["microphone"] = "granted"
                except Exception:
                    pass
                if mic_status == "granted":
                    self._mic_tried = False
                elif mic_status == "denied" and self._mic_tried:
                    if self._stop is not None:
                        self._close_locked()
                    self.kind = kind
                    self.error = _MIC_PERM_ERROR
                    key = f"mic:{mic_status}:{self._mic_tried}"
                    if key != self._last_block_log:
                        self._last_block_log = key
                        # #region agent log
                        _agent_log(
                            "C",
                            "capture.py:ensure",
                            "mic blocked on permission",
                            {"mic_status": mic_status, "tried": self._mic_tried},
                        )
                        # #endregion
                    return
            if self.kind == kind and self.error and now < self._retry_after:
                return
            self._close_locked()
            self.kind = kind
            try:
                self.packets = 0
                self.peak = 0.0
                # #region agent log
                _agent_log(
                    "A-C-D",
                    "capture.py:ensure",
                    "starting capture",
                    {"kind": kind, "live": live, "runId": "post-fix"},
                )
                # #endregion
                if kind == "mic":
                    def _mic_push(samples: np.ndarray, rate: int) -> None:
                        if samples is not None and getattr(samples, "size", 0):
                            peak = float(np.max(np.abs(samples)))
                            self._note_packet(peak)
                        self.ring.push(samples, rate)

                    self._stop = _start_mic(_mic_push)
                else:
                    self._stop = _start_system(self.ring.push, self._note_packet)
                self.error = None
                self._gave_up = False
                self._system_capture_tried = False
                self._mic_tried = False
                self._retry_after = 0.0
                self._last_block_log = ""
                # #region agent log
                _agent_log(
                    "A-C-D",
                    "capture.py:ensure",
                    "capture started ok",
                    {"kind": kind, "runId": "post-fix"},
                )
                # #endregion
            except Exception as exc:  # noqa: BLE001
                self._stop = None
                # #region agent log
                _agent_log(
                    "P3",
                    "capture.py:ensure",
                    "capture start failed",
                    {"kind": kind, "error": str(exc)[:300], "runId": "post-fix"},
                )
                # #endregion
                if kind == "system":
                    # 任何系统声启动失败都先停手，避免 30s 一轮反复触发 TCC。
                    self._system_capture_tried = True
                    self._gave_up = True
                    if (
                        _is_permission_failure(exc)
                        or _screen_recording_granted() is False
                    ):
                        self.error = _PERM_ERROR
                    else:
                        self.error = str(exc)
                    self._retry_after = now + 120.0
                elif kind == "mic" and _is_permission_failure(exc):
                    self._mic_tried = True
                    self.error = _MIC_PERM_ERROR
                    self._retry_after = now + 30.0
                else:
                    self.error = str(exc)
                    self._retry_after = now + 30.0

    def close(self) -> None:
        with self._lock:
            # 保留权限失败标记，避免切回音乐跃动时再次弹窗。
            self._retry_after = 0.0
            self.packets = 0
            self.peak = 0.0
            self._close_locked()

    def _note_packet(self, peak: float) -> None:
        self.packets += 1
        if peak > self.peak:
            self.peak = peak

    def _close_locked(self) -> None:
        stop = self._stop
        self._stop = None
        self.kind = None
        if stop is not None:
            try:
                stop()
            except Exception:
                pass


def _start_system(push: PushFn, note) -> Callable[[], None]:  # noqa: ANN001
    import ScreenCaptureKit as sck  # type: ignore

    content, err = _shareable_content(sck)
    if err is not None or content is None:
        raise RuntimeError(f"读不到屏幕音频：{err or '没有显示器'}")
    displays = list(content.displays() or [])
    if not displays:
        raise RuntimeError("没有可用的显示器，无法听电脑播放")
    display = displays[0]
    filt = sck.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
    cfg = sck.SCStreamConfiguration.alloc().init()
    # 2×2 再把画面压到每秒 2 帧时，开始采集会一直等不到回调，系统声音一个包都不会来。
    cfg.setWidth_(64)
    cfg.setHeight_(64)
    cfg.setShowsCursor_(False)
    cfg.setCapturesAudio_(True)
    cfg.setExcludesCurrentProcessAudio_(True)
    cfg.setSampleRate_(48000)
    cfg.setChannelCount_(2)

    sink = _sink_class().alloc().initWithPush_note_(push, note)
    stream = sck.SCStream.alloc().initWithFilter_configuration_delegate_(filt, cfg, None)
    queue = _audio_queue()
    audio_type = int(getattr(sck, "SCStreamOutputTypeAudio", 1))
    added = stream.addStreamOutput_type_sampleHandlerQueue_error_(sink, audio_type, queue, None)
    if isinstance(added, tuple) and len(added) >= 2 and added[-1] not in (None, 0, False):
        raise RuntimeError(f"系统声音输出打不开：{added[-1]}")

    started = threading.Event()
    box: dict[str, object] = {}

    def _on_start(error) -> None:  # noqa: ANN001
        box["error"] = error
        started.set()

    stream.startCaptureWithCompletionHandler_(_on_start)
    if not started.wait(4.0):
        raise RuntimeError("系统声音启动超时。请在系统设置的屏幕录制里允许本应用")
    if box.get("error") is not None:
        raise RuntimeError(f"系统声音启动失败：{box['error']}")

    holder = {"stream": stream, "sink": sink, "queue": queue}

    def stop() -> None:
        done = threading.Event()

        def _on_stop(_error) -> None:  # noqa: ANN001
            done.set()

        try:
            holder["stream"].stopCaptureWithCompletionHandler_(_on_stop)
            done.wait(2.0)
        except Exception:
            pass
        holder["stream"] = None
        holder["sink"] = None

    stop.holder = holder  # type: ignore[attr-defined]
    return stop


def _shareable_content(sck: object) -> tuple[object | None, object | None]:
    done = threading.Event()
    box: dict[str, object] = {}

    def _cb(content, error) -> None:  # noqa: ANN001
        box["content"] = content
        box["error"] = error
        done.set()

    sck.SCShareableContent.getShareableContentWithCompletionHandler_(_cb)  # type: ignore[attr-defined]
    if not done.wait(4.0):
        return None, "timeout"
    return box.get("content"), box.get("error")


_held_queues: list[object] = []


def _audio_queue(name: bytes = b"yeelight.music.audio") -> object:
    # ScreenCaptureKit 需要真正的 dispatch 队列。NSOperationQueue 会在
    # startRemoteAudioReceiveQueue 里触发 “Resurrection of an object” 并让进程退出。
    import ctypes
    import objc

    lib = ctypes.cdll.LoadLibrary("/usr/lib/libSystem.B.dylib")
    lib.dispatch_queue_create.restype = ctypes.c_void_p
    lib.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    ptr = lib.dispatch_queue_create(name, None)
    if not ptr:
        raise RuntimeError("创建系统声音队列失败")
    queue = objc.objc_object(c_void_p=ptr)
    # 队列指针由调用方和这条缓存共同持有，避免被提前释放。
    _held_queues.append(queue)
    return queue


def _copy_float_channel(channel: object, frames: int) -> np.ndarray:
    """PyObjC 11 的 floatChannelData 是 varlist，不能再 ctypes.cast。"""
    frames = int(frames)
    if frames <= 0:
        return np.zeros(0, dtype=np.float32)
    if hasattr(channel, "as_buffer"):
        raw = channel.as_buffer(frames * 4)
        return np.frombuffer(raw, dtype=np.float32, count=frames).copy()
    import ctypes

    ptr = ctypes.cast(channel, ctypes.POINTER(ctypes.c_float))
    return np.ctypeslib.as_array(ptr, shape=(frames,)).astype(np.float32, copy=True)


def _mix_down(channels: list[np.ndarray]) -> np.ndarray:
    usable = [ch for ch in channels if ch.size]
    if not usable:
        return np.zeros(0, dtype=np.float32)
    if len(usable) == 1:
        return np.ascontiguousarray(usable[0], dtype=np.float32)
    mixed = np.mean(np.stack(usable, axis=0), axis=0)
    return np.ascontiguousarray(mixed, dtype=np.float32)


def _pcm_from_sample(sample: object) -> tuple[np.ndarray, int] | None:
    """取出单声道 float32。音频 CMSampleBuffer 的 DataBuffer 是空的，必须走 AudioBufferList。"""
    parsed = _pcm_from_audio_list(sample)
    if parsed is not None:
        return parsed
    legacy = _pcm_from_block(sample)
    if legacy is None:
        return None
    return legacy, 48000


def _pcm_from_audio_list(sample: object) -> tuple[np.ndarray, int] | None:
    import AVFoundation as av  # type: ignore
    import CoreMedia  # type: ignore

    fmt_desc = CoreMedia.CMSampleBufferGetFormatDescription(sample)
    if fmt_desc is None:
        return None
    frames = int(CoreMedia.CMSampleBufferGetNumSamples(sample))
    if frames < 1:
        return None
    av_fmt = av.AVAudioFormat.alloc().initWithCMAudioFormatDescription_(fmt_desc)
    if av_fmt is None:
        return None
    # 1=Float32, 3=Int16。麦克风常用 Int16。
    try:
        common = int(av_fmt.commonFormat())
    except Exception:
        common = 0
    if common not in (1, 3):
        return None
    channels = max(1, int(av_fmt.channelCount() or 1))
    rate = int(av_fmt.sampleRate() or 48000)
    pcm = av.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(av_fmt, frames)
    if pcm is None:
        return None
    pcm.setFrameLength_(frames)
    err = CoreMedia.CMSampleBufferCopyPCMDataIntoAudioBufferList(
        sample, 0, frames, pcm.mutableAudioBufferList()
    )
    if isinstance(err, tuple):
        err = err[0]
    if err not in (0, None):
        return None
    interleaved = bool(av_fmt.isInterleaved())
    if common == 1:
        data = pcm.floatChannelData()
        if not data:
            return None
        if interleaved and channels > 1:
            raw = _copy_float_channel(data[0], frames * channels)
            mono = np.ascontiguousarray(raw.reshape(-1, channels).mean(axis=1).astype(np.float32))
        else:
            count = min(channels, len(data))
            mono = _mix_down([_copy_float_channel(data[i], frames) for i in range(count)])
    else:
        data = pcm.int16ChannelData()
        if not data:
            return None
        if interleaved and channels > 1:
            raw = _copy_int16_channel(data[0], frames * channels)
            mono = np.ascontiguousarray(
                (raw.reshape(-1, channels).mean(axis=1) / 32768.0).astype(np.float32)
            )
        else:
            count = min(channels, len(data))
            parts = [_copy_int16_channel(data[i], frames).astype(np.float32) / 32768.0 for i in range(count)]
            mono = _mix_down(parts)
    if mono.size == 0:
        return None
    return mono, rate


def _copy_int16_channel(channel: object, frames: int) -> np.ndarray:
    frames = int(frames)
    if frames <= 0:
        return np.zeros(0, dtype=np.int16)
    if hasattr(channel, "as_buffer"):
        raw = channel.as_buffer(frames * 2)
        return np.frombuffer(raw, dtype=np.int16, count=frames).copy()
    import ctypes

    ptr = ctypes.cast(channel, ctypes.POINTER(ctypes.c_int16))
    return np.ctypeslib.as_array(ptr, shape=(frames,)).astype(np.int16, copy=True)


def _pcm_from_block(sample: object) -> np.ndarray | None:
    import CoreMedia  # type: ignore

    block = CoreMedia.CMSampleBufferGetDataBuffer(sample)
    if block is None:
        return None
    length = int(CoreMedia.CMBlockBufferGetDataLength(block))
    if length < 8:
        return None
    raw = bytearray(length)
    err = CoreMedia.CMBlockBufferCopyDataBytes(block, 0, length, raw)
    if err not in (0, None):
        return None
    count = length // 4
    if count < 2:
        return None
    pcm = np.frombuffer(bytes(raw[: count * 4]), dtype=np.float32)
    if pcm.size >= 2 and pcm.size % 2 == 0:
        pcm = pcm.reshape(-1, 2).mean(axis=1)
    return np.ascontiguousarray(pcm, dtype=np.float32)


_sink_cls: type | None = None


def _sink_class() -> type:
    global _sink_cls
    if _sink_cls is not None:
        return _sink_cls
    import objc  # type: ignore
    from Foundation import NSObject  # type: ignore

    class _SckSink(NSObject):
        def initWithPush_note_(self, push, note):  # noqa: ANN001, N802
            self = objc.super(_SckSink, self).init()
            if self is None:
                return None
            self.push = push
            self.note = note
            return self

        def stream_didOutputSampleBuffer_ofType_(self, _stream, sample, typ):  # noqa: ANN001, N802
            from Foundation import NSAutoreleasePool  # type: ignore

            pool = NSAutoreleasePool.alloc().init()
            try:
                if int(typ) != 1:
                    return
                parsed = _pcm_from_sample(sample)
                if not parsed:
                    return
                pcm, rate = parsed
                if pcm.size == 0:
                    return
                peak = float(np.max(np.abs(pcm)))
                self.note(peak)
                self.push(pcm, int(rate) or 48000)
            except Exception:
                return
            finally:
                del pool

    _sink_cls = _SckSink
    return _sink_cls


def _mic_device_score(device: object) -> int:
    name = str(getattr(device, "localizedName", lambda: "")() or "").lower()
    uid = str(getattr(device, "uniqueID", lambda: "")() or "").lower()
    bad = (
        "blackhole",
        "virtual",
        "loopback",
        "soundflower",
        "oray",
        "multi-output",
        "aggregate",
        "zoomaudio",
        "microsoft teams",
    )
    if any(token in name or token in uid for token in bad):
        return -100
    if "builtin" in uid or "built-in" in name or "内建" in name or "macbook" in name:
        return 100
    if "microphone" in name or "麦克风" in name:
        return 50
    return 10


def _pick_mic_device(av: object) -> object | None:
    devices: list = []
    try:
        devices = list(av.AVCaptureDevice.devicesWithMediaType_("soun") or [])  # type: ignore[attr-defined]
    except Exception:
        devices = []
    if devices:
        ranked = sorted(devices, key=_mic_device_score, reverse=True)
        if _mic_device_score(ranked[0]) >= 0:
            return ranked[0]
    try:
        return av.AVCaptureDevice.defaultDeviceWithMediaType_("soun")  # type: ignore[attr-defined]
    except Exception:
        return None


_mic_sink_cls: type | None = None


def _mic_sink_class() -> type:
    global _mic_sink_cls
    if _mic_sink_cls is not None:
        return _mic_sink_cls
    import objc  # type: ignore
    from Foundation import NSObject  # type: ignore

    class _MicSink(NSObject):
        def initWithPush_(self, push):  # noqa: ANN001, N802
            self = objc.super(_MicSink, self).init()
            if self is None:
                return None
            self.push = push
            self._logged = 0
            return self

        def captureOutput_didOutputSampleBuffer_fromConnection_(  # noqa: N802
            self, _output, sample, _conn
        ):  # noqa: ANN001
            from Foundation import NSAutoreleasePool  # type: ignore

            pool = NSAutoreleasePool.alloc().init()
            try:
                parsed = _pcm_from_sample(sample)
                if not parsed:
                    return
                pcm, rate = parsed
                if pcm.size == 0:
                    return
                # #region agent log
                if self._logged < 3:
                    self._logged += 1
                    _agent_log(
                        "H1-H2",
                        "capture.py:mic_sink",
                        "mic sample",
                        {
                            "frames": int(pcm.size),
                            "rate": int(rate),
                            "peak": float(np.max(np.abs(pcm))),
                            "runId": "post-fix",
                        },
                    )
                # #endregion
                self.push(pcm, int(rate) or 48000)
            except Exception:
                return
            finally:
                del pool

    _mic_sink_cls = _MicSink
    return _mic_sink_cls


def _start_mic(push: PushFn) -> Callable[[], None]:
    import AVFoundation as av  # type: ignore

    # 3=authorized。优先认 Electron 主进程已申请的 App 身份权限。
    try:
        auth = int(av.AVCaptureDevice.authorizationStatusForMediaType_("soun"))
    except Exception:
        auth = -1
    live_mic = _live_permissions().get("microphone") or ""
    # #region agent log
    _agent_log(
        "C-D",
        "capture.py:_start_mic",
        "mic auth check",
        {"auth": auth, "live_mic": live_mic, "runId": "post-fix"},
    )
    # #endregion
    if auth != 3 and live_mic != "granted":
        granted = threading.Event()
        box: dict[str, bool] = {"ok": False}

        def _perm(ok) -> None:  # noqa: ANN001
            box["ok"] = bool(ok)
            granted.set()

        av.AVCaptureDevice.requestAccessForMediaType_completionHandler_("soun", _perm)
        if not granted.wait(8.0) or not box["ok"]:
            raise RuntimeError(_MIC_PERM_ERROR)
    elif auth == 3 and live_mic != "granted":
        sync_permission_file(microphone="granted")

    device = _pick_mic_device(av)
    if device is None:
        raise RuntimeError("找不到可用的麦克风设备")
    device_name = str(device.localizedName() or "")
    device_uid = str(device.uniqueID() or "")
    # #region agent log
    try:
        all_devs = [
            {
                "name": str(d.localizedName() or ""),
                "uid": str(d.uniqueID() or "")[:40],
                "score": _mic_device_score(d),
            }
            for d in list(av.AVCaptureDevice.devicesWithMediaType_("soun") or [])
        ]
    except Exception:
        all_devs = []
    _agent_log(
        "H1",
        "capture.py:_start_mic",
        "mic device picked",
        {
            "name": device_name,
            "uid": device_uid[:48],
            "score": _mic_device_score(device),
            "devices": all_devs[:8],
            "runId": "post-fix",
        },
    )
    # #endregion

    session = av.AVCaptureSession.alloc().init()
    try:
        created = av.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"麦克风输入打不开：{exc}") from exc
    inp = created[0] if isinstance(created, tuple) else created
    err = created[-1] if isinstance(created, tuple) and len(created) > 1 else None
    if err not in (None, 0, False) or inp is None:
        raise RuntimeError(f"麦克风输入打不开：{err}")
    if not session.canAddInput_(inp):
        raise RuntimeError("当前会话无法添加麦克风输入")
    session.addInput_(inp)

    output = av.AVCaptureAudioDataOutput.alloc().init()
    sink = _mic_sink_class().alloc().initWithPush_(push)
    queue = _audio_queue(b"yeelight.music.mic")
    output.setSampleBufferDelegate_queue_(sink, queue)
    if not session.canAddOutput_(output):
        raise RuntimeError("当前会话无法添加麦克风输出")
    session.addOutput_(output)
    session.startRunning()
    if not bool(session.isRunning()):
        raise RuntimeError("麦克风会话未能启动")

    holder = {"session": session, "output": output, "sink": sink, "input": inp}

    def stop() -> None:
        try:
            if holder["session"] is not None:
                holder["session"].stopRunning()
        except Exception:
            pass
        holder["session"] = None
        holder["output"] = None
        holder["sink"] = None
        holder["input"] = None

    stop.holder = holder  # type: ignore[attr-defined]
    return stop
