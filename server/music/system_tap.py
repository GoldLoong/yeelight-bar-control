"""系统声音：Core Audio 进程采集，不打开屏幕流。

macOS 14.2+ 的 CATapDescription 直接混出各进程的播放。采样回调里只拷 PCM。
建不起来时由 capture._start_system 退回 ScreenCaptureKit。
"""

from __future__ import annotations

import ctypes
import os
import uuid
from typing import Callable

import numpy as np

PushFn = Callable[[np.ndarray, int], None]
NoteFn = Callable[[float], None]

_GLOB = 0x676C6F62  # 'glob'
_INPT = 0x696E7074  # 'inpt'
_SFMT = 0x73666D74  # 'sfmt'
_TFMT = 0x74666D74  # 'tfmt'
_TUID = 0x74756964  # 'tuid'
_ID2P = 0x69643270  # 'id2p'
_NOPE = 0x6E6F7065  # 'nope'  kAudioHardwareIllegalOperationError
_FLOAT = 1
_NONINTERLEAVED = 32
_UTF8 = 0x08000100


class TapUnavailable(RuntimeError):
    """接口不存在，或这条流建不起来。"""


class TapPermissionError(RuntimeError):
    """系统音频录制未授权。"""


class _AudioBuffer(ctypes.Structure):
    _fields_ = [
        ("mNumberChannels", ctypes.c_uint32),
        ("mDataByteSize", ctypes.c_uint32),
        ("mData", ctypes.c_void_p),
    ]


class _ASBD(ctypes.Structure):
    _fields_ = [
        ("mSampleRate", ctypes.c_double),
        ("mFormatID", ctypes.c_uint32),
        ("mFormatFlags", ctypes.c_uint32),
        ("mBytesPerPacket", ctypes.c_uint32),
        ("mFramesPerPacket", ctypes.c_uint32),
        ("mBytesPerFrame", ctypes.c_uint32),
        ("mChannelsPerFrame", ctypes.c_uint32),
        ("mBitsPerChannel", ctypes.c_uint32),
        ("mReserved", ctypes.c_uint32),
    ]


class _PropAddr(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


_IOProc = ctypes.CFUNCTYPE(
    ctypes.c_int32,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
)

_ca = None
_cf = None


def _status_text(code: int) -> str:
    raw = int(code) & 0xFFFFFFFF
    chars = bytes([(raw >> 24) & 0xFF, (raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF])
    if all(32 <= c < 127 for c in chars):
        return chars.decode("ascii")
    return str(int(code))


def _libs():
    global _ca, _cf
    if _ca is not None and _cf is not None:
        return _ca, _cf
    ca = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    cf = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    if not hasattr(ca, "AudioHardwareCreateProcessTap"):
        raise TapUnavailable("系统没有 Core Audio tap")
    ca.AudioHardwareCreateProcessTap.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    ca.AudioHardwareCreateProcessTap.restype = ctypes.c_int32
    ca.AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint32]
    ca.AudioHardwareDestroyProcessTap.restype = ctypes.c_int32
    ca.AudioHardwareCreateAggregateDevice.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
    ca.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
    ca.AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32
    ca.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(_PropAddr),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
    ca.AudioDeviceCreateIOProcID.argtypes = [
        ctypes.c_uint32,
        _IOProc,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ca.AudioDeviceCreateIOProcID.restype = ctypes.c_int32
    ca.AudioDeviceDestroyIOProcID.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    ca.AudioDeviceDestroyIOProcID.restype = ctypes.c_int32
    ca.AudioDeviceStart.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    ca.AudioDeviceStart.restype = ctypes.c_int32
    ca.AudioDeviceStop.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    ca.AudioDeviceStop.restype = ctypes.c_int32
    cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    _ca, _cf = ca, cf
    return ca, cf


def _check(status: int, what: str) -> None:
    if int(status) == 0:
        return
    text = _status_text(status)
    if (int(status) & 0xFFFFFFFF) == _NOPE:
        raise TapPermissionError(f"{what}被拒绝（{text}）")
    raise TapUnavailable(f"{what}失败（{text}）")


def _cfstring(ptr: int) -> str:
    _libs()
    assert _cf is not None
    length = int(_cf.CFStringGetLength(ptr))
    buf = ctypes.create_string_buffer(max(8, length * 4 + 1))
    if not _cf.CFStringGetCString(ptr, buf, len(buf), _UTF8):
        raise TapUnavailable("读到的 UID 不是文本")
    return buf.value.decode("utf-8")


def _read_string(obj_id: int, selector: int) -> str:
    ca, cf = _libs()
    ptr = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
    addr = _PropAddr(selector, _GLOB, 0)
    err = ca.AudioObjectGetPropertyData(
        ctypes.c_uint32(obj_id),
        ctypes.byref(addr),
        0,
        None,
        ctypes.byref(size),
        ctypes.byref(ptr),
    )
    _check(err, "读取采集 UID")
    if not ptr.value:
        raise TapUnavailable("采集 UID 为空")
    try:
        return _cfstring(int(ptr.value))
    finally:
        cf.CFRelease(ptr)


def _read_format(obj_id: int) -> _ASBD:
    ca, _cf = _libs()
    asbd = _ASBD()
    for selector, scope in ((_SFMT, _INPT), (_TFMT, _GLOB)):
        size = ctypes.c_uint32(ctypes.sizeof(asbd))
        addr = _PropAddr(selector, scope, 0)
        err = ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(obj_id),
            ctypes.byref(addr),
            0,
            None,
            ctypes.byref(size),
            ctypes.byref(asbd),
        )
        if int(err) == 0 and asbd.mSampleRate > 0 and asbd.mChannelsPerFrame > 0:
            return asbd
    raise TapUnavailable("读不到系统声音格式")


def _self_process_id() -> int:
    ca, _cf = _libs()
    pid = ctypes.c_int32(os.getpid())
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32))
    out = ctypes.c_uint32(0)
    addr = _PropAddr(_ID2P, _GLOB, 0)
    err = ca.AudioObjectGetPropertyData(
        1,
        ctypes.byref(addr),
        ctypes.sizeof(pid),
        ctypes.byref(pid),
        ctypes.byref(size),
        ctypes.byref(out),
    )
    if int(err) != 0:
        return 0
    return int(out.value)


def _description():
    import objc
    from Foundation import NSArray, NSNumber

    try:
        cls = objc.lookUpClass("CATapDescription")
    except Exception:
        objc.loadBundle(
            "CoreAudio",
            bundle_path="/System/Library/Frameworks/CoreAudio.framework",
            module_globals={},
        )
        cls = objc.lookUpClass("CATapDescription")
    proc = _self_process_id()
    if proc:
        excluded = NSArray.arrayWithObject_(NSNumber.numberWithUnsignedInt_(proc))
    else:
        excluded = NSArray.array()
    desc = cls.alloc().initStereoGlobalTapButExcludeProcesses_(excluded)
    if desc is None:
        raise TapUnavailable("创建不了系统声音描述")
    desc.setName_("Yeelight 系统声音")
    if hasattr(desc, "setPrivate_"):
        desc.setPrivate_(True)
    else:
        desc.setPrivateTap_(True)
    if hasattr(desc, "setMuteBehavior_"):
        desc.setMuteBehavior_(0)
    return desc


def _aggregate_dict(tap_uid: str):
    from Foundation import NSArray, NSDictionary, NSNumber, NSString

    sub = NSDictionary.dictionaryWithObjectsAndKeys_(
        NSString.stringWithString_(tap_uid),
        "uid",
        NSNumber.numberWithInt_(1),
        "drift",
        None,
    )
    return NSDictionary.dictionaryWithObjectsAndKeys_(
        NSString.stringWithString_("Yeelight System Tap"),
        "name",
        NSString.stringWithString_(str(uuid.uuid4())),
        "uid",
        NSNumber.numberWithInt_(1),
        "private",
        NSNumber.numberWithInt_(0),
        "stacked",
        NSNumber.numberWithInt_(0),
        "tapautostart",
        NSArray.arrayWithObject_(sub),
        "taps",
        None,
    )


def _channel(buf: _AudioBuffer, *, is_float: bool, bits: int) -> np.ndarray | None:
    width = 4 if bits >= 32 else 2
    if not buf.mData or int(buf.mDataByteSize) < width:
        return None
    count = int(buf.mDataByteSize) // width
    if count < 1:
        return None
    if is_float and bits >= 32:
        view = np.ctypeslib.as_array(
            ctypes.cast(buf.mData, ctypes.POINTER(ctypes.c_float)),
            shape=(count,),
        )
        return np.array(view, dtype=np.float32, copy=True)
    if bits == 16:
        view = np.ctypeslib.as_array(
            ctypes.cast(buf.mData, ctypes.POINTER(ctypes.c_int16)),
            shape=(count,),
        )
        return np.array(view, dtype=np.float32, copy=True) / np.float32(32768.0)
    return None


def pcm_from_buffer_list(data_ptr: int, asbd: _ASBD) -> np.ndarray | None:
    """把 IO 回调里的 AudioBufferList 混成单声道 float32。数据在回调返回前拷走。"""
    if not data_ptr:
        return None
    nbuf = int(ctypes.cast(data_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value)
    if nbuf < 1 or nbuf > 8:
        return None
    bufs = ctypes.cast(data_ptr + 8, ctypes.POINTER(_AudioBuffer))
    flags = int(asbd.mFormatFlags)
    is_float = bool(flags & _FLOAT)
    bits = int(asbd.mBitsPerChannel) or (32 if is_float else 16)
    noninterleaved = bool(flags & _NONINTERLEAVED) or (
        nbuf > 1 and int(bufs[0].mNumberChannels) <= 1
    )
    if noninterleaved:
        parts: list[np.ndarray] = []
        for i in range(nbuf):
            ch = _channel(bufs[i], is_float=is_float, bits=bits)
            if ch is not None and ch.size:
                parts.append(ch)
        if not parts:
            return None
        if len(parts) == 1:
            return np.ascontiguousarray(parts[0])
        n = min(int(p.size) for p in parts)
        acc = parts[0][:n].astype(np.float32, copy=True)
        for part in parts[1:]:
            acc += part[:n]
        acc /= np.float32(len(parts))
        return np.ascontiguousarray(acc)
    chans = int(bufs[0].mNumberChannels) or int(asbd.mChannelsPerFrame) or 1
    mixed = _channel(bufs[0], is_float=is_float, bits=bits)
    if mixed is None or mixed.size < chans:
        return None
    if chans == 1:
        return np.ascontiguousarray(mixed)
    usable = mixed.size - (mixed.size % chans)
    return np.ascontiguousarray(mixed[:usable].reshape(-1, chans).mean(axis=1))


def start(push: PushFn, note: NoteFn) -> Callable[[], None]:
    """开始听系统播放。返回 stop()。失败抛 TapPermissionError 或 TapUnavailable。"""
    import objc

    ca, _cf = _libs()
    desc = _description()
    tap_id = ctypes.c_uint32(0)
    _check(
        ca.AudioHardwareCreateProcessTap(objc.pyobjc_id(desc), ctypes.byref(tap_id)),
        "打开系统声音",
    )
    device_id = ctypes.c_uint32(0)
    proc_box: dict[str, object] = {}
    try:
        tap_uid = _read_string(int(tap_id.value), _TUID)
        agg = _aggregate_dict(tap_uid)
        _check(
            ca.AudioHardwareCreateAggregateDevice(
                objc.pyobjc_id(agg), ctypes.byref(device_id)
            ),
            "建立系统声音设备",
        )
        asbd = _read_format(int(device_id.value))
        rate = int(asbd.mSampleRate) or 48000

        @_IOProc
        def _on_audio(_device, _now, in_data, _in_time, _out_data, _out_time, _client):
            try:
                if not in_data:
                    return 0
                base = int(ctypes.cast(in_data, ctypes.c_void_p).value or 0)
                pcm = pcm_from_buffer_list(base, asbd)
                if pcm is None or pcm.size == 0:
                    return 0
                peak = float(np.max(np.abs(pcm)))
                note(peak)
                push(pcm, rate)
            except Exception:
                return 0
            return 0

        proc = ctypes.c_void_p()
        _check(
            ca.AudioDeviceCreateIOProcID(
                int(device_id.value), _on_audio, None, ctypes.byref(proc)
            ),
            "注册系统声音回调",
        )
        proc_box["cb"] = _on_audio
        proc_box["proc"] = proc
        proc_box["agg"] = agg
        proc_box["desc"] = desc
        _check(ca.AudioDeviceStart(int(device_id.value), proc), "开始系统声音")
    except Exception:
        proc = proc_box.get("proc")
        if proc is not None and int(device_id.value):
            try:
                ca.AudioDeviceStop(int(device_id.value), proc)
                ca.AudioDeviceDestroyIOProcID(int(device_id.value), proc)
            except Exception:
                pass
        if int(device_id.value):
            ca.AudioHardwareDestroyAggregateDevice(int(device_id.value))
        if int(tap_id.value):
            ca.AudioHardwareDestroyProcessTap(int(tap_id.value))
        raise

    def stop() -> None:
        proc = proc_box.get("proc")
        try:
            if proc is not None and int(device_id.value):
                ca.AudioDeviceStop(int(device_id.value), proc)
                ca.AudioDeviceDestroyIOProcID(int(device_id.value), proc)
        except Exception:
            pass
        try:
            if int(device_id.value):
                ca.AudioHardwareDestroyAggregateDevice(int(device_id.value))
        except Exception:
            pass
        try:
            if int(tap_id.value):
                ca.AudioHardwareDestroyProcessTap(int(tap_id.value))
        except Exception:
            pass
        proc_box.clear()

    return stop
