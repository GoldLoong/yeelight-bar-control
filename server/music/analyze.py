"""把一段单声道采样变成音量、音色和节拍。

颜色用的是慢特征（频谱重心、冷暖），节拍用的是短时起始能量，
并且尽量锁在你会点头的那一拍上，而不是每一声镲。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass
class Levels:
    loud: float = 0.0
    bass: float = 0.0
    mid: float = 0.0
    treble: float = 0.0
    beat: bool = False
    # 0 暗、厚，1 亮、薄。给配色用，未平滑。
    centroid: float = 0.5
    # 0 偏冷，1 偏低音。给配色用，未平滑。
    warmth: float = 0.5
    # 0 平铺，1 节奏密。决定颜色饱和和亮度起伏有多大。
    drive: float = 0.0
    onset: float = 0.0
    # 估计的点头周期（秒），亮度回落跟它走。
    period: float = 0.5
    # 静音后再出声：新的一段，颜色可以走快一点。
    retint: bool = False


class _Agc:
    def __init__(self, half_life: float = 6.0) -> None:
        self.peak = 0.02
        self.half_life = half_life

    def norm(self, value: float, dt: float) -> float:
        decay = 0.5 ** (max(0.0, dt) / self.half_life) if self.half_life > 0 else 0.0
        self.peak = max(float(value), self.peak * decay)
        if self.peak < 1e-5:
            return 0.0
        return max(0.0, min(1.0, float(value) / self.peak))


class LevelTracker:
    """按调用间隔更新。beat 只在触发的那一帧为 True。"""

    def __init__(self) -> None:
        self._win = 4096
        self._buf = np.zeros(self._win, dtype=np.float32)
        self._filled = 0
        self._hann = np.hanning(self._win).astype(np.float32)
        self._prev_log: np.ndarray | None = None
        self._loud_agc = _Agc(8.0)
        self._flux_agc = _Agc(3.0)
        self._flux_hist: deque[float] = deque(maxlen=48)
        self._env: deque[tuple[float, float]] = deque(maxlen=320)
        self._prev_flux = 0.0
        self._drive = 0.0
        self._period = 0.5
        self._period_at = -10.0
        self._last_beat = -10.0
        self._time = 0.0
        self._quiet = 0.0
        self._was_quiet = False
        self._starve = 0.0
        self._last = Levels()

    def push(self, samples: np.ndarray, rate: int, dt: float) -> Levels:
        dt = max(0.0, min(0.5, float(dt)))
        self._time += dt
        rate = int(rate) if rate and rate >= 1000 else 48000
        if samples is None or samples.size < 16:
            self._starve += dt
            if self._starve > 0.35:
                self._was_quiet = True
                self._last = Levels(period=self._period)
                return self._last
            held = self._last
            return Levels(
                loud=held.loud,
                bass=held.bass,
                mid=held.mid,
                treble=held.treble,
                centroid=held.centroid,
                warmth=held.warmth,
                drive=held.drive,
                period=self._period,
            )

        self._starve = 0.0
        mono = np.asarray(samples, dtype=np.float32).reshape(-1)
        rms = float(np.sqrt(np.mean(np.square(mono))))
        loud_n = 0.0 if rms < 1e-5 else self._loud_agc.norm(rms, dt)
        loud = 0.0 if loud_n < 0.025 else loud_n ** 0.8

        self._ingest(mono)
        bass = mid = treble = 0.0
        centroid = self._last.centroid
        warmth = self._last.warmth
        onset = 0.0
        if self._filled >= 2048:
            bass, mid, treble, centroid, warmth, onset = self._spectrum(rate, dt)
        self._drive = max(onset, self._drive * math.exp(-dt / 0.7))
        self._env.append((self._time, onset))
        self._update_period()

        retint = False
        if loud_n < 0.045:
            self._quiet += dt
            if self._quiet > 0.45:
                self._was_quiet = True
        else:
            if self._was_quiet and loud_n > 0.1:
                retint = True
                self._was_quiet = False
            self._quiet = 0.0

        beat = self._accept_beat(onset, loud_n)
        levels = Levels(
            loud=loud,
            bass=bass,
            mid=mid,
            treble=treble,
            beat=beat,
            centroid=centroid,
            warmth=warmth,
            drive=max(0.0, min(1.0, self._drive)),
            onset=onset,
            period=self._period,
            retint=retint,
        )
        self._last = levels
        return levels

    def _ingest(self, samples: np.ndarray) -> None:
        n = int(samples.size)
        if n <= 0:
            return
        if n >= self._win:
            self._buf[:] = samples[-self._win :]
            self._filled = self._win
            return
        if self._filled + n <= self._win:
            self._buf[self._filled : self._filled + n] = samples
            self._filled += n
            return
        if self._filled < self._win:
            keep = self._win - n
            self._buf[:keep] = self._buf[self._filled - keep : self._filled]
            self._buf[keep:] = samples
            self._filled = self._win
            return
        self._buf[:-n] = self._buf[n:]
        self._buf[-n:] = samples

    def _spectrum(self, rate: int, dt: float) -> tuple[float, float, float, float, float, float]:
        window = self._buf * self._hann
        mag = np.abs(np.fft.rfft(window))
        freqs = np.fft.rfftfreq(self._win, 1.0 / rate)
        logm = np.log1p(mag)
        band = (freqs >= 40.0) & (freqs <= min(12000.0, rate / 2.2))
        onset = 0.0
        if self._prev_log is not None and self._prev_log.shape == logm.shape:
            diff = logm[band] - self._prev_log[band]
            if diff.size:
                onset = float(np.mean(np.maximum(diff, 0.0)))
        self._prev_log = logm
        onset_n = 0.0 if onset < 1e-6 else self._flux_agc.norm(onset, dt)

        bass = _band_rms(mag, freqs, 40.0, 160.0)
        mid = _band_rms(mag, freqs, 200.0, 1800.0)
        treble = _band_rms(mag, freqs, 2800.0, min(10000.0, rate / 2.2))
        total = bass + mid + treble
        if total < 1e-3:
            return 0.0, 0.0, 0.0, self._last.centroid, self._last.warmth, onset_n
        # 用能量占比，不用各自的自动增益。否则低音和高音会被拉成一样响，曲风就没了。
        # 人声和旋律大多在中频。中频占多数时冷暖保持居中，只让频谱重心决定颜色。
        edges = bass + treble
        if edges < total * 0.28:
            warmth = 0.5
        else:
            warmth = bass / (edges + 1e-8)
        return bass / total, mid / total, treble / total, _centroid(mag, freqs), warmth, onset_n

    def _update_period(self) -> None:
        if self._time - self._period_at < 0.3 or len(self._env) < 24:
            return
        self._period_at = self._time
        duration = 6.0
        hop = 0.02
        n = int(duration / hop)
        now = self._time
        series = np.zeros(n, dtype=np.float32)
        for stamp, value in self._env:
            idx = int((stamp - (now - duration)) / hop)
            if 0 <= idx < n:
                series[idx] = max(series[idx], value)
        series = series - float(series.mean())
        if float(np.max(np.abs(series))) < 0.04:
            return
        best_score = 0.22
        best_lag = self._period
        lag_s = 0.30
        while lag_s <= 0.90:
            lag = int(round(lag_s / hop))
            if lag <= 0 or lag >= n // 2:
                lag_s += 0.02
                continue
            left = series[:-lag]
            right = series[lag:]
            denom = float(np.linalg.norm(left) * np.linalg.norm(right))
            if denom < 1e-5:
                lag_s += 0.02
                continue
            score = float(np.dot(left, right) / denom)
            # 同一强度时偏向大约 120 BPM，灯跟点头，不跟每颗镲。
            score -= 0.035 * abs(lag_s - 0.5)
            if score > best_score:
                best_score = score
                best_lag = lag_s
            lag_s += 0.02
        if best_score > 0.22:
            self._period = self._period * 0.72 + best_lag * 0.28

    def _accept_beat(self, onset: float, loud_n: float) -> bool:
        self._flux_hist.append(onset)
        prev = self._prev_flux
        self._prev_flux = onset
        if loud_n < 0.06 or onset < prev or len(self._flux_hist) < 8:
            return False
        hist = np.asarray(self._flux_hist, dtype=np.float32)
        med = float(np.median(hist))
        mad = float(np.median(np.abs(hist - med))) + 1e-3
        thresh = max(0.22, med + 1.15 * mad * 1.4826)
        if onset < thresh:
            return False
        if self._last_beat < 0:
            self._last_beat = self._time
            return True
        period = max(0.30, min(0.90, self._period))
        since = self._time - self._last_beat
        window = period * 0.18
        on_grid = abs(since - period) <= window or abs(since - 2 * period) <= window * 1.25
        if on_grid and since > period * 0.62:
            self._last_beat = self._time
            return True
        if since > period * 1.45 and onset > thresh * 1.15:
            self._last_beat = self._time
            return True
        return False


def _band_rms(spec: np.ndarray, freqs: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs < hi)
    if not np.any(mask):
        return 0.0
    return float(np.sqrt(np.mean(np.square(spec[mask]))))


def _centroid(spec: np.ndarray, freqs: np.ndarray) -> float:
    mask = (freqs >= 80.0) & (freqs <= 8000.0)
    if not np.any(mask):
        return 0.5
    weight = spec[mask]
    total = float(np.sum(weight))
    if total < 1e-6:
        return 0.5
    hz = float(np.sum(freqs[mask] * weight) / total)
    hz = max(80.0, min(8000.0, hz))
    pos = (math.log(hz) - math.log(180.0)) / (math.log(4200.0) - math.log(180.0))
    return max(0.0, min(1.0, pos))
