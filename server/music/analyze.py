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
    # 撞击感：高频裂纹瞬态 × 低频 thrump（夜色专用；音乐模式可忽略）
    impact: float = 0.0
    # 持续气声/人声感：中高能量且缺少裂纹瞬态
    sustain: float = 0.0
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
        self._hf_agc = _Agc(2.2)  # 撞击裂纹：跟得更快
        self._lf_agc = _Agc(2.2)
        self._flux_hist: deque[float] = deque(maxlen=56)
        self._env: deque[tuple[float, float]] = deque(maxlen=320)
        self._prev_flux = 0.0
        self._drive = 0.0
        self._period = 0.5
        self._period_at = -10.0
        self._last_beat = -10.0
        self._phase = 0.0  # 0..1，按 period 推进，用来卡鼓点网格
        self._time = 0.0
        self._quiet = 0.0
        self._was_quiet = False
        self._starve = 0.0
        self._mood_c = 0.5
        self._mood_w = 0.5
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
                onset=held.onset,
                impact=held.impact,
                sustain=held.sustain,
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
        impact = 0.0
        sustain = 0.0
        if self._filled >= 2048:
            bass, mid, treble, centroid, warmth, onset, impact, sustain = self._spectrum(
                rate, dt, loud=loud
            )
        # 曲风特征慢跟：避免每拍抖色，换段仍能在约 0.4s 内挪过去
        mood_tau = 0.18 if (centroid != self._mood_c and abs(centroid - self._mood_c) > 0.12) else 0.55
        a = 1.0 - math.exp(-dt / max(0.05, mood_tau))
        self._mood_c += (centroid - self._mood_c) * a
        self._mood_w += (warmth - self._mood_w) * a
        self._drive = max(onset, impact * 0.85, self._drive * math.exp(-dt / 0.55))
        self._env.append((self._time, max(onset, impact)))
        self._update_period()
        period = max(0.28, min(0.90, self._period))
        self._phase = (self._phase + dt / period) % 1.0

        retint = False
        if loud_n < 0.045:
            self._quiet += dt
            if self._quiet > 0.45:
                self._was_quiet = True
        else:
            if self._was_quiet and loud_n > 0.1:
                retint = True
                self._was_quiet = False
                self._mood_c = centroid
                self._mood_w = warmth
            self._quiet = 0.0

        beat = self._accept_beat(onset, loud_n, impact)
        levels = Levels(
            loud=loud,
            bass=bass,
            mid=mid,
            treble=treble,
            beat=beat,
            centroid=self._mood_c,
            warmth=self._mood_w,
            drive=max(0.0, min(1.0, self._drive)),
            onset=onset,
            impact=impact,
            sustain=sustain,
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

    def _spectrum(
        self, rate: int, dt: float, *, loud: float
    ) -> tuple[float, float, float, float, float, float, float, float]:
        window = self._buf * self._hann
        mag = np.abs(np.fft.rfft(window))
        freqs = np.fft.rfftfreq(self._win, 1.0 / rate)
        logm = np.log1p(mag)
        hi = min(12000.0, rate / 2.2)
        band = (freqs >= 40.0) & (freqs <= hi)
        hf_band = (freqs >= 2200.0) & (freqs <= min(11000.0, hi))
        lf_band = (freqs >= 50.0) & (freqs <= 380.0)
        mid_band = (freqs >= 280.0) & (freqs <= 2200.0)
        onset = hf_flux = lf_flux = lf_peak_flux = mid_flux = 0.0
        if self._prev_log is not None and self._prev_log.shape == logm.shape:
            diff = logm - self._prev_log
            if np.any(band):
                onset = float(np.mean(np.maximum(diff[band], 0.0)))
            if np.any(hf_band):
                # 裂纹用上分位，避免全带平均把短啪冲淡
                hf_pos = np.maximum(diff[hf_band], 0.0)
                onset_hf = float(np.mean(hf_pos))
                if hf_pos.size >= 8:
                    onset_hf = max(onset_hf, float(np.percentile(hf_pos, 75)))
                hf_flux = onset_hf
            if np.any(lf_band):
                lf_pos = np.maximum(diff[lf_band], 0.0)
                lf_flux = float(np.mean(lf_pos))
                # 短 thrump：上分位，软补插别被全带平均抹平
                if lf_pos.size >= 6:
                    lf_peak_flux = float(np.percentile(lf_pos, 80))
            if np.any(mid_band):
                mid_flux = float(np.mean(np.maximum(diff[mid_band], 0.0)))
        self._prev_log = logm
        onset_n = 0.0 if onset < 1e-6 else self._flux_agc.norm(onset, dt)
        hf_n = 0.0 if hf_flux < 1e-6 else self._hf_agc.norm(hf_flux, dt)
        lf_n = 0.0 if lf_flux < 1e-6 else self._lf_agc.norm(lf_flux, dt)
        lf_peak_n = (
            0.0 if lf_peak_flux < 1e-6 else self._lf_agc.norm(lf_peak_flux, dt)
        )
        mid_n = 0.0 if mid_flux < 1e-6 else self._flux_agc.norm(mid_flux, dt)

        bass = _band_rms(mag, freqs, 40.0, 160.0)
        mid = _band_rms(mag, freqs, 200.0, 1800.0)
        treble = _band_rms(mag, freqs, 2800.0, min(10000.0, rate / 2.2))
        total = bass + mid + treble
        if total < 1e-3:
            return 0.0, 0.0, 0.0, self._last.centroid, self._last.warmth, onset_n, 0.0, 0.0
        # 用能量占比，不用各自的自动增益。否则低音和高音会被拉成一样响，曲风就没了。
        # 冷暖始终跟 bass vs treble；中频主导时也别强行归 0.5（否则曲风色塌成一团紫）。
        edges = bass + treble
        if edges < 1e-6:
            warmth = 0.5
        else:
            warmth = bass / (edges + 1e-8)
            # 中频很重时略往中间收，但仍保留低/高偏向
            mid_r0 = mid / (total + 1e-8)
            if mid_r0 > 0.55:
                warmth = warmth * 0.65 + 0.5 * 0.35
        mid_r = mid / (total + 1e-8)
        # 撞击：真实 LF flux + HF 裂纹。禁止用「中低频占比×onset」冒充 thrump（喘息会中招）
        thump = max(lf_n, 0.85 * lf_peak_n)
        if lf_n > 0.18:
            thump = max(thump, 0.22 * (bass / total) * onset_n)
        impact = max(0.0, min(1.0, math.sqrt(max(1e-6, hf_n) * max(0.06, thump)) * 1.25))
        if hf_n > 0.55 and lf_n > 0.28:
            impact = max(impact, min(1.0, 0.55 * hf_n + 0.4 * lf_n))
        # 叫床叠啪：mid 高但仍有裂纹+thrump 时，别被 pant 逻辑压没
        if hf_n > 0.40 and max(lf_n, lf_peak_n) > 0.22:
            impact = max(
                impact,
                min(1.0, 0.46 * hf_n + 0.40 * max(lf_n, lf_peak_n) + 0.08 * onset_n),
            )
        # 喘息：只在「很像气声、几乎无 thrump/裂纹」时轻压，避免真啪被砍成漏拍
        pant_shape = mid_r > 0.55 and lf_n < 0.22 and hf_n < 0.28 and mid_n > hf_n * 1.25
        if pant_shape:
            impact *= 0.62
        if hf_n < 0.22 and lf_n < 0.12:
            impact *= 0.7
        impact = max(0.0, min(1.0, impact))
        # 持续气声 / 喘息
        airish = (mid + treble) / (total + 1e-8)
        sustain = max(
            0.0,
            min(
                1.0,
                float(loud) * (0.35 + 0.65 * airish) * (1.0 - 0.85 * min(1.0, hf_n * 1.2)),
            ),
        )
        pant = (
            float(loud)
            * (0.4 + 0.6 * mid_r)
            * (1.0 - 0.75 * min(1.0, hf_n))
            * (1.0 - 0.55 * min(1.0, lf_n))
        )
        if mid_n > 0.2:
            pant = max(pant, float(loud) * mid_r * (0.45 + 0.4 * mid_n) * (1.0 - 0.7 * hf_n))
        sustain = max(0.0, min(1.0, max(sustain, pant)))
        return (
            bass / total,
            mid / total,
            treble / total,
            _centroid(mag, freqs),
            warmth,
            onset_n,
            impact,
            sustain,
        )

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

    def _accept_beat(self, onset: float, loud_n: float, impact: float) -> bool:
        """鼓点优先：低频撞击 + 相位网格，少跟镲片乱闪。"""
        kick = max(float(onset) * 0.5, float(impact) * 1.15)
        self._flux_hist.append(kick)
        prev = self._prev_flux
        self._prev_flux = kick
        if loud_n < 0.04 or len(self._flux_hist) < 5:
            return False
        # 只要还在上升沿或撞击够硬，就允许（鼓点常短）
        rising = kick >= prev * 0.92
        if not rising and impact < 0.28:
            return False
        hist = np.asarray(self._flux_hist, dtype=np.float32)
        med = float(np.median(hist))
        mad = float(np.median(np.abs(hist - med))) + 1e-3
        thresh = max(0.12, med + 0.92 * mad * 1.4826)
        if impact > 0.42:
            thresh *= 0.82
        if kick < thresh:
            return False
        if self._last_beat < 0:
            self._last_beat = self._time
            self._phase = 0.0
            return True
        period = max(0.28, min(0.90, self._period))
        since = self._time - self._last_beat
        # 硬冷却：一拍内绝不连发（impact 会持续几帧，否则会炸成机关枪）
        if since < period * 0.45:
            return False
        # 相位：靠近拍点（0）或上拍漏了靠近 1
        phase_ok = self._phase <= 0.22 or self._phase >= 0.78
        window = period * 0.24
        on_grid = abs(since - period) <= window or abs(since - 2 * period) <= window * 1.15
        strong = kick > thresh * 1.25 or impact > 0.52
        if phase_ok or on_grid or strong:
            self._last_beat = self._time
            self._phase = 0.0
            return True
        if since > period * 1.08 and kick > thresh * 0.95:
            # 网格漂了也要咬住，别整段空拍
            self._last_beat = self._time
            self._phase = 0.0
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
