"""夜色模式：左右跟节奏；亮度跟舒服；艳丽跟进入状态。

约定：
- 左右切换=抽插周期（越快切得越快）
- 亮度=舒服程度（呻吟/反馈/进入感）
- 色彩艳丽度=兴奋/高潮进度（越进入越艳）
- 高潮尖叫=短闪带粉的高亮白；花样只改节奏预测
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from server.music.analyze import Levels
from server.music import night_log

NIGHT_STOPS: tuple[int, ...] = (
    0x1A080E,
    0x3A101C,
    0x5C1828,
    0x7A2438,
    0x9A3450,
    0xB84868,
    0x8A3A58,
    0x6A2848,
)

STRETCH_RATIO = 1.06
FINALE_FULL_S = 8.0
FINALE_MAX_S = 18.0
FINALE_MOAN_QUIET_S = 4.0
# 她高潮后他再补插这么多下，灯仍停在结算
FINALE_TAIL_STROKES = 8
FINALE_BLOOM_S = 0.9
FINALE_REENTRY_S = 15.0
# 结算收束淡出：比旧 1.4s 更长，余韵慢慢暗
FINALE_FADE_S = 2.6
FINALE_PURPLE: tuple[int, ...] = (
    0x140820,
    0x2A1450,
    0x4A28A0,
    0x7040D0,
    0xA070F0,
    0xD0B8FF,
)
# 尖峰短闪：带粉的高亮白；结算闪改用紫，和红粉节奏分开
PEAK_WHITE = 0xFFF8FA
PEAK_PINK = 0xFFC0D4
PEAK_VIOLET = 0xC090FF
PEAK_LILAC = 0xF4EEFF
WARM_LIFT = 0xC45A72

# 花样最短驻留：快拍时「按撞击数」会在 ~3s 内触发，匀拍/浅深对打，故以时间为主
PATTERN_MIN_DWELL_S = 16.0
PATTERN_MIN_HITS = 22
SHALLOW_N_MIN_S = 25.0
SHALLOW_N_MIN_HITS = 16

PERIOD_HIST_S = 10.0
PERIOD_REFRESH_S = 0.75
PERIOD_CHANGE_MIN = 0.08

# 左右切换超前 = 半拍(对齐 hit) + 链路补偿(UDP/灯)；随周期变
NIGHT_PIPE_LEAD_S = 0.045  # UDP限流+灯端，约 45ms

# 卸力确认窗（尖峰至少展示 PEAK_MIN_SHOW_S，避免一进尖峰就因「已变稀」立刻紫结算）
RELEASE_MIN_S = 0.9
RELEASE_MAX_S = 2.8
PEAK_MIN_SHOW_S = 1.7

Pattern = str

@dataclass
class NightFrame:
    bright: int
    left: int
    right: int
    segmented: bool
    phase: str

def _follow(current: float, target: float, dt: float, tau: float) -> float:
    if tau <= 1e-4:
        return target
    gain = 1.0 - math.exp(-max(0.0, dt) / tau)
    return current + (target - current) * gain

def _split(value: int) -> tuple[int, int, int]:
    return (value >> 16) & 255, (value >> 8) & 255, value & 255

def _join(r: float, g: float, b: float) -> int:
    return (
        (max(0, min(255, int(round(r)))) << 16)
        | (max(0, min(255, int(round(g)))) << 8)
        | max(0, min(255, int(round(b))))
    )

def _tint(base: int, toward: int, amount: float) -> int:
    amount = max(0.0, min(1.0, amount))
    r1, g1, b1 = _split(base)
    r2, g2, b2 = _split(toward)
    return _join(
        r1 + (r2 - r1) * amount,
        g1 + (g2 - g1) * amount,
        b1 + (b2 - b1) * amount,
    )

def _interp(colors: list[int], pos: float) -> int:
    n = len(colors)
    if n <= 1:
        return colors[0] if colors else 0x5C1228
    x = max(0.0, min(float(n - 1), pos))
    i0 = int(x)
    if i0 >= n - 1:
        return colors[-1]
    return _tint(colors[i0], colors[i0 + 1], x - i0)

def _chroma(rgb: int, keep: float) -> int:
    keep = max(0.0, min(1.0, keep))
    r, g, b = _split(rgb)
    y = 0.30 * r + 0.52 * g + 0.18 * b
    return _join(y + (r - y) * keep, y + (g - y) * keep, y + (b - y) * keep)

def _shift(base: int, toward: int, amount: float) -> tuple[float, float, float]:
    amount = max(0.0, amount)
    r1, g1, b1 = _split(base)
    r2, g2, b2 = _split(toward)
    return (
        (r2 - r1) * amount,
        (g2 - g1) * amount,
        (b2 - b1) * amount,
    )

class NightRenderer:
    def __init__(self) -> None:
        self.arousal = 0.0
        self.climax_progress = 0.0  # 0..1 表现累加，UI 高潮条
        self.phase = "idle"
        self.phase_t = 0.0
        self.climax_cool = 0.0
        self.guide = 0.0
        self.flash = 0.0
        self.white_flash = 0.0  # 仅尖峰
        self.env = 0.0
        self.breath_lvl = 0.0  # 0..1 舒服→亮度（沿用字段名，语义已变）
        self.force_pink = 0.0
        self.breath = 0.0
        self.thrust = 0.0
        self.period = 0.55
        self.predict_force = 0.45  # 预测下一一下 0..1
        self.pattern: Pattern = "even"
        self._time = 0.0
        self._last_hit = -10.0
        self._prev_thrust = 0.0
        self._period_at = -10.0
        self._phase = 0.0
        self._hits: deque[float] = deque(maxlen=48)
        self._forces: deque[float] = deque(maxlen=24)
        self._onset_hist: deque[tuple[float, float]] = deque(maxlen=400)
        self._air_hist: deque[tuple[float, float]] = deque(maxlen=120)
        self._flip = False
        self._stroke_i = 0
        self._shallow_n = 7
        self._deep_boost = 1.35
        self._surge_i = 0
        self._surge_len = 6
        self._double_tail = 0.0
        self._stability = 0.0
        self._finale_t = 0.0
        self._finale_kind = ""
        self._last_finale = -100.0
        self._finale_moan = 0.0
        self._finale_moan_pulse = 0.0
        self._finale_moan_prev = 0.0
        self._last_finale_moan_at = -100.0
        self._finale_slap_pulse = 0.0
        self._last_finale_slap_at = -100.0
        self._finale_side_flip = False
        self._finale_side_mix = 0.5  # 结算补插极轻左右：→1 左略亮 / →0 右略亮
        self._finale_slap_gaps: deque[float] = deque(maxlen=6)
        self._orgasm_tail: deque[float] = deque(maxlen=24)
        self._lamp_host = ""
        self._moan_boost = 0.0
        self._awaiting_moan = False
        self._period_scale = 1.0
        self._matched = False
        self._pattern_since = 0.0
        self._pattern_hits0 = 0
        self._shallow_n_at = 0.0  # 开局也要跑满 SHALLOW_N_MIN_S，禁止 -100 立刻改 N
        self._shallow_hits0 = 0
        self._pending_pattern: Pattern | None = None
        self._predict_deep_log_at = -100.0
        self._deep_tint = 0.0  # 浅深「深」一下的颜色提示（不抬亮度）
        self._period_smooth = 0.55  # 显示用平滑周期
        self._bright_smooth = 0.2  # 下发亮度平滑
        self._slap_heat = 0.0  # 碰撞短热（次要色点缀）
        self._feedback = 0.0  # 舒服反馈
        self._side_mix = 0.5  # 左右权重 →1 左亮 / →0 右亮
        self._side_target = 0.92  # 当前要停的一侧（一下一切）
        self._side_hold = 0.0  # 深入停顿剩余秒
        self._hold_side = 0.92  # 停顿时锁住的一侧
        self._hit_clarity = 0.35  # 撞击是否清楚：高=快切，低=慢跟
        self._pre_switched = False  # 本拍是否已切过边
        self._last_flip_at = -10.0
        self._gaps: deque[float] = deque(maxlen=8)
        self._subdiv = 1  # 1=一拍一切；2=双拍格子
        self._impact_floor = 1.0  # 上次撞击之后的最低尖峰，用来区分余音和新的一下
        self._last_impact_at = -10.0
        self._slap_clear = 0.0  # 明确啪声门控：高时叫床不抢节奏
        self._vocal = 0.0  # 叫床/气声
        self._impact_peak = 0.0  # 撞击尖峰保持，防帧间漏检
        # min_gap 卡住的强峰：间隙一过立刻补拍，避免能量掉光漏拍
        self._pending_hit_t = -100.0
        self._pending_thrust = 0.0
        self._pending_impact = 0.0
        # 尖峰候选 / 卸力
        self._peak_at = -100.0
        self._peak_air = 0.0
        self._peak_loud = 0.0
        self._hits_before_peak = 0.0
        self._scream_hold = 0.0

    def reset(self) -> None:
        host = self._lamp_host
        self.__init__()
        self._lamp_host = host
        night_log.set_lamp_host(host)

    def ui_status(self) -> dict[str, object]:
        label = self._pattern_label()
        if self.phase == "peak_candidate":
            label = "尖峰"
        force = int(max(0, min(9, round(self.predict_force * 9))))
        return {
            "pattern": label,
            "force": force,
            "climax": self._climax_score_09(),
            "phase": self.phase,
            "period": round(self.period, 3),
            "stability": round(self._stability, 3),
        }

    def _pattern_label(self) -> str:
        if self.phase == "finale":
            return "结算"
        if self.phase == "peak_candidate":
            return "尖峰"
        if not self._matched and self.phase in ("idle", "afterglow"):
            return "-"
        if self._stability < 0.18 and len(self._hits) < 3:
            return "-"
        if self.pattern == "shallow_deep":
            return f"{int(self._shallow_n)}浅1深"
        return {
            "even": "匀拍",
            "stretch": "渐疏",
            "surge": "浪涌",
            "double": "双拍",
        }.get(self.pattern, "-")

    def _climax_score_09(self) -> int:
        # 表现一点点累加；尖峰/结算只托底，不突然跳满
        score = self.climax_progress
        if self.phase == "peak_candidate":
            score = max(score, 0.72 + 0.15 * self.white_flash)
        elif self.phase == "finale":
            score = max(score, 0.85)
        return int(max(0, min(9, round(score * 9))))

    def step(
        self,
        levels: Levels,
        *,
        bright_max: int,
        dt: float,
        lamp_host: str | None = None,
    ) -> NightFrame:
        dt = max(0.0, min(0.5, float(dt)))
        if lamp_host is not None and lamp_host != self._lamp_host:
            self._lamp_host = lamp_host
            night_log.set_lamp_host(lamp_host)

        self._time += dt
        if self.climax_cool > 0:
            self.climax_cool = max(0.0, self.climax_cool - dt)

        bass = float(levels.bass)
        mid = float(levels.mid)
        treble = float(levels.treble)
        loud = float(levels.loud)
        onset = float(levels.onset)
        impact = float(getattr(levels, "impact", 0.0) or 0.0)
        sustain = float(getattr(levels, "sustain", 0.0) or 0.0)
        # 尖峰保持 ~120ms，避免啪落在帧缝被漏掉
        if impact >= self._impact_peak:
            self._impact_peak = impact
        else:
            self._impact_peak = _follow(self._impact_peak, impact, dt, 0.12)
        impact_use = max(impact, self._impact_peak)
        if impact_use >= 0.18:
            self._last_impact_at = self._time
        if self._time - self._last_hit < 0.06:
            self._impact_floor = impact_use
        else:
            self._impact_floor = min(self._impact_floor, impact_use)
        # 占比只作色调参考；撞击强度看 impact（HF裂纹×LF thrump）
        body = max(0.0, min(1.0, 0.50 * bass + 0.45 * mid + 0.05 * treble))
        air = max(0.0, min(1.0, 0.50 * mid + 0.50 * treble))
        # 啪只认撞击。onset/中频是旧节拍特征，喘息会顶满，不能单独成拍
        slap = max(
            0.0,
            min(1.0, impact_use * (0.82 + 0.18 * min(1.0, onset))),
        )
        # 叫床/喘息：sustain；有明显 impact 时别把 vocal 抬太高
        vocal_raw = max(
            0.0,
            min(
                1.0,
                max(
                    sustain * (1.0 - 0.55 * impact_use),
                    loud * (0.2 + 0.5 * air + 0.15 * treble) * (1.0 - 0.85 * impact_use),
                ),
            ),
        )
        pant_raw = 0.0
        if mid > 0.5 and impact_use < 0.28 and sustain > 0.35:
            pant_raw = max(0.0, min(1.0, sustain * (0.5 + 0.4 * mid) * (1.0 - impact_use)))
        vocal_raw = max(vocal_raw, pant_raw)
        self._vocal = _follow(
            self._vocal, vocal_raw, dt, 0.2 if vocal_raw > self._vocal else 0.55
        )
        # 清晰度只跟撞击。旧逻辑用叫声去乘 slap_clear，喘息一响真啪也会变糊
        slap_clear = max(0.0, min(1.0, slap * (0.45 + 0.55 * impact_use)))
        if impact_use < 0.2 and self._vocal > 0.4:
            slap_clear *= 0.3
        elif sustain > impact_use * 1.5 and impact_use < 0.22:
            slap_clear *= 0.4
        self._slap_clear = _follow(
            self._slap_clear,
            slap_clear,
            dt,
            0.06 if slap_clear > self._slap_clear else 0.16,
        )
        # 节奏能量=撞击。中频 squish / vocal_bleed 是旧「摩擦也算抽」会把喘息写进左右
        thrust_raw = slap
        if self._vocal > 0.5 and impact_use < 0.22:
            thrust_raw *= 0.25
        breath_raw = max(
            0.0,
            min(1.0, vocal_raw * (1.0 - 0.9 * self._slap_clear)),
        )
        self.thrust = _follow(
            self.thrust, thrust_raw, dt, 0.05 if thrust_raw > self.thrust else 0.26
        )
        self.breath = _follow(
            self.breath, breath_raw, dt, 0.35 if breath_raw > self.breath else 0.9
        )
        if loud >= self.env:
            self.env = _follow(self.env, loud, dt, 0.1)
        else:
            self.env = _follow(self.env, loud, dt, 0.5)

        self._air_hist.append((self._time, air * loud))
        self._refresh_period()
        self._update_stability(dt)

        # 结算/余韵：叫床不算还在抽。高潮后补插几下留在结算，连着打过才拉回
        if self.phase in ("finale", "peak_candidate"):
            self._note_orgasm_tail(impact_use)
        dense_hits = self._hit_density_window(1.2) >= max(3.0, 1.4 / max(0.16, self._switch_period()))
        clear_stroke = impact_use >= 0.42 and dense_hits
        if self.phase == "afterglow" and clear_stroke:
            self.phase = "peak_approach" if self.climax_progress > 0.35 else "build"
            self.phase_t = 0.0
            self._matched = True

        if self.phase == "finale" and self._finale_kind != "bloom":
            self._update_finale_moan(dt, air=air, loud=loud, breath_raw=breath_raw)
        # 余声还在就留在结算。只有叫声已经静了、又密着抽，才拉回节奏
        if (
            self.phase == "finale"
            and self._finale_t > 1.2
            and self._orgasm_resumed()
            and not self._finale_moan_holding()
        ):
            self.phase = "peak_approach"
            self.phase_t = 0.0
            self._finale_kind = ""
            self.white_flash = 0.0
            self._finale_slap_pulse = 0.0
            self._finale_side_mix = 0.5
            self.climax_cool = 0.8
            night_log.log_event("finale_abort", reason="activity_resume")

        if self.phase == "finale":
            frame = self._step_finale(
                levels,
                bright_max=bright_max,
                dt=dt,
                air=air,
                loud=loud,
                onset=onset,
                breath_raw=breath_raw,
                impact=impact_use,
                slap_clear=self._slap_clear,
            )
            self._prev_thrust = thrust_raw
            return frame

        if self.phase == "peak_candidate":
            frame = self._step_peak_candidate(
                bright_max=bright_max, dt=dt, air=air, loud=loud, thrust_raw=thrust_raw
            )
            self._prev_thrust = thrust_raw
            return frame

        impact_now = max(impact, self._impact_peak)
        hit, hit_reason = self._detect_hit(
            levels, thrust_raw, impact=impact_now
        )
        force = (
            self._hit_force(
                thrust_raw, loud, body, impact=impact_now
            )
            if hit
            else 0.0
        )
        ghost = False
        if not hit:
            ghost = self._maybe_ghost_stroke(
                thrust_raw, loud, impact_now
            )
            # min_gap 卡住的强峰：先挂起，间隙一过再补
            if hit_reason == "min_gap" and impact_now >= 0.36:
                self._pending_hit_t = self._time
                self._pending_thrust = max(self._pending_thrust, thrust_raw)
                self._pending_impact = max(self._pending_impact, impact_now)

        # 判定后再写入历史，避免本帧污染 median
        # 周期自相关只收撞击；弱帧写 0，避免喘息 onset 把周期拽走
        self._onset_hist.append(
            (self._time, thrust_raw if impact_now >= 0.22 else 0.0)
        )

        if not hit:
            pending = self._flush_pending_hit(thrust_raw, impact_now)
            if pending is not None:
                hit = True
                impact_now = pending
                force = self._hit_force(
                    max(thrust_raw, pending), loud, body, impact=pending
                )
        if hit:
            self._on_real_hit(
                force, loud=loud, onset=onset, impact=impact_now
            )
            self._impact_peak *= 0.12  # 同一下余音不再立刻再触发
        elif not ghost:
            self._apply_delayed_moan(breath_raw, thrust_raw, dt)

        if self._double_tail > 0:
            self._double_tail = max(0.0, self._double_tail - dt)

        self._advance_phase_clock(dt, hit)
        self._recommend_pattern()
        self._refresh_predict_force(hit_force=force if hit else None)
        self._drive_comfort(dt)
        self._drive_sides(dt)
        # 衰减：啪热短、反馈稍长
        self._slap_heat = _follow(self._slap_heat, 0.0, dt, 0.22)
        self._feedback = _follow(self._feedback, 0.0, dt, 0.7)
        self._deep_tint = _follow(self._deep_tint, 0.0, dt, 0.35)
        self.force_pink = _follow(self.force_pink, self._feedback, dt, 0.25)
        self._period_smooth = _follow(self._period_smooth, self._switch_period(), dt, 1.2)

        hit_rate = self._hit_density()
        tempo = max(0.0, min(1.0, 1.0 - (self.period - 0.22) / 1.0))
        raw = (
            0.38 * self.thrust
            + 0.26 * hit_rate
            + 0.16 * tempo * max(self.thrust, hit_rate)
            + 0.10 * self.breath
            + 0.10 * self._moan_boost
        )
        raw = max(0.0, min(1.0, raw))
        # 兴奋度慢跟；高潮条用累加（见 _update_climax_progress）
        if raw >= self.arousal:
            self.arousal = _follow(self.arousal, raw, dt, 2.4)
        else:
            self.arousal = _follow(self.arousal, raw, dt, 3.8)
        self._update_climax_progress(
            hit=hit,
            force=force,
            hit_rate=hit_rate,
            dt=dt,
            thrust_raw=thrust_raw,
            breath_raw=breath_raw,
            loud=loud,
        )

        if self._try_enter_peak_candidate(air=air, loud=loud, onset=onset, hit_rate=hit_rate):
            self._prev_thrust = thrust_raw
            return self._paint(bright_max)

        self._advance_phase(dt)
        if self.phase == "peak_approach":
            self.guide = _follow(self.guide, 0.5, dt, 1.4)
        elif self.phase == "build":
            self.guide = _follow(self.guide, 0.12, dt, 1.0)
        else:
            self.guide = _follow(self.guide, 0.0, dt, 0.55)

        self.flash *= math.exp(-dt / 0.1)
        # 尖叫粉白闪：短促
        self.white_flash *= math.exp(-dt / 0.06)
        self._prev_thrust = thrust_raw
        return self._paint(bright_max)

    # ——— hits / period / comfort / sides ———

    def _detect_hit(
        self, levels: Levels, thrust_raw: float, *, impact: float
    ) -> tuple[bool, str]:
        sprint = self.climax_progress >= 0.45 or self.phase == "peak_approach"
        since = self._time - self._last_hit
        step = self._switch_period()
        # 最近已经是快间隔：按格子放行，别用慢周期的半拍把快抽卡掉
        recent_gaps = list(self._gaps)[-3:]
        fast_recent = (
            len(recent_gaps) >= 2
            and sorted(recent_gaps)[len(recent_gaps) // 2] <= 0.26
        )
        dipped = self._impact_floor < 0.24 and self._impact_floor < impact * 0.7
        if self._subdiv >= 2 or dipped or fast_recent:
            min_gap = max(0.12, step * 0.28)
        else:
            min_gap = max(0.15, min(0.36, step * (0.42 if sprint else 0.48)))
        if since < min_gap:
            return False, "min_gap"
        sustain = float(getattr(levels, "sustain", 0.0) or 0.0)
        # 喘息的 onset 很高但不能算一拍；没有撞击就不是左右切换
        if impact < 0.2 and self._vocal > 0.4:
            return False, "vocal_block"
        if impact < 0.22 and sustain > 0.45 and self._slap_clear < 0.25:
            return False, "pant_block"
        if impact < 0.2 and self._slap_clear < 0.28:
            return False, "no_impact"
        rising = (
            thrust_raw >= self._prev_thrust * 0.78
            or impact >= 0.32
            or impact >= self._impact_peak * 0.88
        )
        if not rising:
            return False, "not_rising"
        floor = 0.12 if sprint else 0.15
        if impact < 0.3:
            floor += 0.03
        if thrust_raw < floor and impact < 0.32:
            return False, "thrust_low"
        if impact >= 0.38 and since >= min_gap:
            return True, "impact"
        if impact >= 0.3 and self._slap_clear >= 0.35 and since >= min_gap:
            return True, "slap_clear"
        recent = [v for t, v in self._onset_hist if self._time - t < 1.6 and v > 0.05]
        if len(recent) < 6:
            need = 0.18 if sprint else 0.22
            ok = thrust_raw > need and impact > 0.24
            return ok, "cold_ok" if ok else "cold_low"
        med = sorted(recent)[len(recent) // 2]
        need = max(0.14 if sprint else 0.16, med * (1.05 if sprint else 1.12))
        if self._vocal > 0.55 and impact < 0.32:
            need *= 1.15
        ok = thrust_raw > need and impact > 0.24
        return ok, ("med_ok" if ok else "med_low") + f":need={need:.2f}"

    def _maybe_ghost_stroke(self, thrust_raw: float, loud: float, impact: float) -> bool:
        """漏拍改由相位格子维持，这里不再额外切边。"""
        return False

    def _flush_pending_hit(self, thrust_raw: float, impact: float) -> float | None:
        """min_gap 卡住的强峰：间隙一过立刻补拍；返回撞击峰值或 None。"""
        if self._pending_hit_t <= 0:
            return None
        if self._time - self._pending_hit_t > 0.9:
            self._pending_hit_t = -100.0
            self._pending_thrust = 0.0
            self._pending_impact = 0.0
            return None
        step = self._switch_period()
        min_gap = max(0.12, step * 0.28)
        if self._time - self._last_hit < min_gap:
            self._pending_impact = max(self._pending_impact, impact)
            self._pending_thrust = max(self._pending_thrust, thrust_raw)
            return None
        peak = max(self._pending_impact, impact)
        self._pending_hit_t = -100.0
        self._pending_thrust = 0.0
        self._pending_impact = 0.0
        return peak if peak >= 0.32 else None

    def _hit_force(
        self, thrust_raw: float, loud: float, body: float, *, impact: float
    ) -> float:
        raw = max(
            0.0,
            min(1.0, 0.55 * thrust_raw + 0.35 * impact + 0.1 * loud * max(0.2, impact)),
        )
        if len(self._forces) >= 4:
            ordered = sorted(self._forces)
            p30 = ordered[max(0, int(len(ordered) * 0.3) - 1)]
            p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
            span = max(0.08, p90 - p30)
            raw = max(0.0, min(1.0, (raw - p30) / span))
        return raw

    def _phase_lead_s(self) -> float:
        """显示超前：半拍对齐撞击 + 链路补偿。快拍时不能超过半格。"""
        period = max(0.14, min(1.2, self._period_smooth or self._switch_period()))
        return max(0.06, min(0.28, 0.45 * period + NIGHT_PIPE_LEAD_S))

    def _vis_phase(self) -> float:
        """显示相位=检测时钟+超前；半拍切换沿应对准撞击。"""
        period = max(0.14, min(1.2, self._period_smooth or self._switch_period()))
        return (self._phase + self._phase_lead_s() / period) % 1.0

    def _acoust_lag_s(self) -> float:
        """声能峰→当前：排除本帧，避免与 hit 同戳导致假 0。"""
        best_t = None
        best_v = -1.0
        now = self._time
        for t, v in self._onset_hist:
            age = now - t
            if age > 0.28 or age < 0.02:
                continue
            if v >= best_v:
                best_v = v
                best_t = t
        if best_t is None or best_v < 0:
            return 0.0
        return max(0.0, now - best_t)

    def _is_deep_stroke(self, force: float, loud: float) -> bool:
        if self.pattern == "shallow_deep":
            n = max(2, int(self._shallow_n))
            if self._stroke_i % n == 0:
                return True
        if len(self._forces) >= 4:
            ordered = sorted(self._forces)
            med = ordered[len(ordered) // 2]
            if force > max(0.55, med * 1.35) and loud > 0.28:
                return True
        return force > 0.72 and loud > 0.4

    def _switch_period(self) -> float:
        bar = max(0.18, min(1.2, self.period))
        if self._subdiv >= 2:
            return max(0.14, bar * 0.5)
        return bar

    def _learn_period(self, gap: float) -> None:
        """连续清晰的新间隔立刻改拍。单次噪声、散乱间隔不改。"""
        if gap < 0.12 or gap > 1.5:
            return
        bar = max(0.18, min(1.2, self.period))
        self._gaps.append(gap)
        recent = list(self._gaps)[-6:]
        last2 = recent[-2:]
        last3 = recent[-3:]
        clear2 = len(last2) == 2 and max(last2) - min(last2) <= 0.22 * max(
            0.14, sum(last2) * 0.5
        )
        clear3 = len(last3) == 3 and max(last3) - min(last3) <= 0.24 * max(
            0.14, sorted(last3)[1]
        )
        if not clear2 and not clear3:
            return
        cluster = last2 if clear2 else last3
        med = sorted(cluster)[len(cluster) // 2]
        step = self._switch_period()
        ratio = med / max(0.14, step)
        # 清晰连续：直接落到新间隔。半拍/整拍只用来记 subdiv
        if 0.32 <= ratio <= 0.68:
            self._subdiv = 2
            target_step = med
        elif 1.55 <= ratio <= 2.5 and self._subdiv >= 2:
            self._subdiv = 1
            target_step = med
        elif 0.55 <= ratio <= 1.55:
            target_step = med
            if med < 0.55 * bar:
                self._subdiv = 2
            elif med > 0.9 * bar:
                self._subdiv = 1
        elif clear3 and 0.35 <= ratio <= 2.4:
            target_step = med
            if med < 0.55 * bar:
                self._subdiv = 2
            elif med > 0.85 * bar:
                self._subdiv = 1
        else:
            return
        if self._subdiv >= 2:
            self.period = max(0.18, min(1.15, target_step * 2.0))
        else:
            self.period = max(0.18, min(1.15, target_step))
        self._period_smooth = self._switch_period()
        # 软对齐，不硬清零相位（清零易卡边/叠切）
        if abs(target_step - step) / max(0.14, step) > 0.08:
            if self._phase > 0.85 or self._phase < 0.15:
                self._phase *= 0.5

    def _flip_side(self, *, hard: bool) -> None:
        self._flip = not self._flip
        self._side_target = 0.92 if self._flip else 0.08
        if hard:
            self._side_mix = self._side_mix * 0.2 + self._side_target * 0.8
        else:
            self._side_mix = self._side_mix * 0.35 + self._side_target * 0.65
        self._last_flip_at = self._time
        self._pre_switched = True

    def _coast_ok(self) -> bool:
        """漏检时按格子继续切。撞击能量没了就停，不看叫声响不响。"""
        if len(self._hits) < 4 or self._side_hold > 0:
            return False
        if self.phase in ("finale", "idle"):
            return False
        if self._time - self._last_hit > 12.0:
            return False
        window = max(1.8, 4.0 * self._switch_period())
        return self._time - self._last_impact_at <= window

    def _on_real_hit(
        self, force: float, *, loud: float = 0.0, onset: float = 0.0, impact: float = 0.0
    ) -> None:
        """真实撞击切一次边。贴着格子才轻轻校正相位，单次偏拍不改周期。"""
        prev_hit = self._last_hit
        self._hits.append(self._time)
        self._forces.append(force)
        self._last_hit = self._time
        self._impact_floor = impact
        if prev_hit > 0:
            self._learn_period(self._time - prev_hit)
        step = self._switch_period()
        # 快拍每一记都是新的一拍。不能把相位按死在 0 附近，否则「本拍已切」永远不放开，灯就停在一边
        fast = step <= 0.28
        on_grid = fast or self._phase >= 0.58 or self._phase <= 0.2
        if on_grid and not fast:
            self._phase *= 0.2
        elif fast:
            self._phase = 0.0
        self._stroke_i += 1
        # 清晰度只看撞击。旧逻辑用 vocal 去乘，喘息叠在啪上会把左右切慢、看起来被带跑
        clarity = max(
            0.0,
            min(
                1.0,
                0.45 * force
                + 0.45 * max(impact, self._slap_clear)
                + 0.10 * self._stability,
            ),
        )
        self._hit_clarity = max(self._hit_clarity * 0.35, clarity)
        slap = max(force, impact, 0.45 * loud * max(0.25, impact) + 0.45 * force)
        self._slap_heat = max(self._slap_heat, min(1.0, 0.25 + 0.75 * slap))
        deep = self._is_deep_stroke(force, loud)
        # 同一拍里提前切过就不再翻。快拍以间隔为准，不看可能被卡死的 pre_switched
        since_flip = self._time - self._last_flip_at
        same_beat = (not fast) and self._pre_switched and since_flip < step * 0.55
        if not same_beat and since_flip >= max(0.07, step * 0.28):
            self._flip_side(hard=fast or clarity >= 0.62)
        if fast:
            self._side_hold = 0.0
        elif deep and clarity >= 0.48:
            self._deep_tint = max(self._deep_tint, 0.75)
            self._slap_heat = max(self._slap_heat, min(1.0, 0.55 + 0.45 * slap))
            # 清楚的深撞：只停真实这一下，不超过当前格子的一小段
            self._hold_side = self._side_target
            self._side_mix = self._side_target
            self._side_hold = min(0.22, max(0.08, step * 0.28))
            if self.breath_lvl < 0.55:
                self.breath_lvl = max(self.breath_lvl, 0.5 + 0.15 * slap)
        elif clarity >= 0.62:
            # 清楚撞击：快切到位
            self._side_mix = self._side_mix * 0.2 + self._side_target * 0.8
        self._awaiting_moan = True
        self._matched = True
        if self.phase == "idle":
            self.phase = "build"
            self.phase_t = 0.0
        self._period_scale = _follow(self._period_scale, 1.0, 0.05, 0.8)
        if self.pattern == "double":
            self._double_tail = min(0.14, self.period * 0.22)
        if self.pattern == "surge":
            self._surge_i = (self._surge_i + 1) % max(3, self._surge_len)
        self._refresh_predict_force(hit_force=force)

    def _next_is_deep(self) -> bool:
        if self.pattern == "shallow_deep":
            return (self._stroke_i + 1) % max(2, int(self._shallow_n)) == 0
        if len(self._forces) >= 3:
            last = self._forces[-1]
            avg = sum(self._forces) / len(self._forces)
            return self.predict_force > max(0.7, avg * 1.25) or last > avg * 1.3
        return self.predict_force > 0.78

    def _update_climax_progress(
        self,
        *,
        hit: bool,
        force: float,
        hit_rate: float,
        dt: float,
        thrust_raw: float = 0.0,
        breath_raw: float = 0.0,
        loud: float = 0.0,
    ) -> None:
        """完整过程日志：10 分钟最高 climax 仅 0.46、从未 ≥0.55 → 进不了尖峰。

        前戏摩擦持续小涨，抽插命中中涨，空窗长衰减要很慢（短停不吐进度）。
        """
        if self.phase == "finale":
            return
        if self.phase == "peak_candidate":
            self.climax_progress = max(self.climax_progress, 0.72)
            return

        since = self._time - self._last_hit
        gain = 0.0
        if hit:
            # 单下约 0.02；稳定只作轻加成，不再把前戏乘没
            step = 0.018 * (0.4 + 0.6 * max(0.2, force))
            step *= 0.85 + 0.15 * self._stability
            if self._moan_boost > 0.2:
                step += 0.008 * self._moan_boost
            if hit_rate > 0.5:
                step += 0.005
            # 后段已有进度时同样一下涨更多（进入/冲刺）
            step *= 1.0 + 0.35 * self.climax_progress
            gain += step
        else:
            # 前戏：持续摩擦/水声，无清晰撞击也慢慢攒
            friction = max(0.0, min(1.0, 0.55 * thrust_raw + 0.25 * loud + 0.2 * breath_raw))
            if friction > 0.12 and self.phase in ("build", "peak_approach", "idle"):
                gain += dt * 0.012 * (friction - 0.08)

        if gain > 0:
            # 日志：141s 已到 0.9，但真实做到 ~490s → 后半段要涨得更慢
            if self.climax_progress > 0.55:
                gain *= 0.45
            if self.climax_progress > 0.75:
                gain *= 0.5
            self.climax_progress = min(1.0, self.climax_progress + gain)
            return

        # 衰减：短停几乎不掉；真静音（日志 490s 后）才退
        if since < 5.0:
            decay = 0.0015 * dt
        elif since < 12.0:
            decay = 0.006 * dt
        elif since < 30.0:
            decay = 0.015 * dt
        else:
            decay = 0.045 * dt
        self.climax_progress = max(0.0, self.climax_progress - decay)

    def _refresh_predict_force(self, hit_force: float | None) -> None:
        """花样只改预测力度；UI 力度=预测下一一下。"""
        avg = sum(self._forces) / len(self._forces) if self._forces else 0.4
        last = self._forces[-1] if self._forces else 0.4
        moan_w = 0.15 * (1.0 - 0.85 * max(0.0, min(1.0, self._slap_clear)))
        base = 0.4 * last + 0.35 * avg + moan_w * self._moan_boost + 0.1 * self.arousal
        if hit_force is not None:
            base = 0.5 * base + 0.5 * hit_force

        next_i = self._stroke_i + 1
        if self.pattern == "shallow_deep":
            if next_i % max(2, self._shallow_n) == 0:
                base = max(0.88, min(1.0, 0.85 * self._deep_boost / 1.15))
                # 每帧都会走到「下一记是深」，日志节流，避免刷屏
                if self._time - self._predict_deep_log_at >= 5.0:
                    self._predict_deep_log_at = self._time
                    night_log.log_event(
                        "predict_deep", n=self._shallow_n, force=round(base, 3)
                    )
            else:
                base = min(base, 0.55) * 0.75  # 浅一下明显轻，衬托深
        elif self.pattern == "surge":
            pos = (self._surge_i + 1) % max(3, self._surge_len)
            base = 0.55 + 0.4 * (pos / max(1, self._surge_len - 1))
        elif self.pattern == "double":
            base = max(base, 0.5)
        elif self.pattern == "stretch":
            base *= 0.92

        self.predict_force = max(0.12, min(1.0, base))
        self._deep_boost = 1.25 + 0.25 * avg

    def _apply_delayed_moan(self, breath_raw: float, thrust_raw: float, dt: float) -> None:
        """碰撞后尖叫/深吟 → 只进舒服/色彩；有明确连啪时不抢节奏。"""
        self._moan_boost = _follow(self._moan_boost, 0.0, dt, 0.85)
        if not self._awaiting_moan or self._last_hit < 0:
            return
        since = self._time - self._last_hit
        if since < 0.05:
            return
        if since > 0.55:
            self._awaiting_moan = False
            return
        # 明确啪还在打：叫床只作轻微反馈，不抬 moan_boost 干扰左右/力度
        if self._slap_clear > 0.45 or (thrust_raw > 0.55 and self._vocal < self._slap_clear):
            if breath_raw > 0.35:
                self._feedback = max(
                    self._feedback, min(0.45, 0.15 + 0.25 * breath_raw)
                )
            return
        if breath_raw < 0.18 and self.breath < 0.2:
            return
        if thrust_raw > 0.72 and since < 0.1:
            return
        self._awaiting_moan = False
        strength = max(breath_raw, self.breath) * (1.0 - 0.5 * self._slap_clear)
        self._moan_boost = max(self._moan_boost, min(1.0, 0.35 + 0.65 * strength))
        self._feedback = max(self._feedback, min(1.0, 0.3 + 0.7 * strength))
        night_log.log_event("moan_lag", breath=round(strength, 3), feedback=round(self._feedback, 3))

    def _drive_comfort(self, dt: float) -> None:
        """亮度只跟舒服程度，不跟拍子闪。"""
        if self.phase in ("finale",):
            return
        # 尖叫闪期间亮度交给 paint 的 white_flash
        if self.phase == "peak_candidate" and self.white_flash > 0.35:
            self.breath_lvl = _follow(self.breath_lvl, 0.95, dt, 0.08)
            return
        fb = max(0.0, min(1.0, self._feedback))
        moan = max(0.0, min(1.0, self._moan_boost))
        wet = max(0.0, min(1.0, self.breath))
        state = max(0.0, min(1.0, 0.55 * self.climax_progress + 0.45 * self.arousal))
        # 有明确啪时亮度少吃叫床，避免叫声把灯拽亮打乱层次
        slap_g = max(0.0, min(1.0, self._slap_clear))
        comfort = (
            0.38 * fb * (1.0 - 0.55 * slap_g)
            + 0.28 * moan * (1.0 - 0.7 * slap_g)
            + 0.18 * wet * (1.0 - 0.5 * slap_g)
            + 0.16 * state
            + 0.12 * slap_g * max(0.2, self.thrust)
        )
        comfort = max(0.0, min(1.0, comfort))
        floor = 0.14 + 0.06 * min(1.0, self.env)
        peak = 0.55 + 0.4 * max(comfort, state * 0.7)
        target = floor + (peak - floor) * max(comfort, state * 0.45)
        keep_alive = (
            self._matched
            or self.phase in ("build", "peak_approach", "afterglow", "peak_candidate")
            or (self._time - self._last_hit) < 4.0
            or self.env > 0.06
            or wet > 0.08
            or fb > 0.05
        )
        if not keep_alive:
            self.breath_lvl = _follow(self.breath_lvl, floor, dt, 0.7)
            return
        tau = 0.22 if target >= self.breath_lvl else 0.45
        self.breath_lvl = _follow(self.breath_lvl, target, dt, tau)

    def _drive_sides(self, dt: float) -> None:
        """左一下右一下：只在撞击切换；清楚快切，糊了慢跟。"""
        if self.phase in ("finale",):
            return
        # 清晰度慢慢回落，糊声段不会一直快切
        self._hit_clarity = _follow(self._hit_clarity, 0.28, dt, 1.8)
        if self._side_hold > 0:
            self._side_hold = max(0.0, self._side_hold - dt)
            self._side_mix = _follow(self._side_mix, self._hold_side, dt, 0.035)
            return
        active = (
            self._matched
            or self.phase in ("build", "peak_approach", "peak_candidate")
            or (self._time - self._last_hit) < 3.5
            or self.thrust > 0.1
        )
        if not active:
            self._side_mix = _follow(self._side_mix, 0.5, dt, 0.55)
            return

        period = max(0.14, min(1.2, self._switch_period()))
        clarity = max(0.0, min(1.0, self._hit_clarity))
        fast = period <= 0.28
        # 快拍必须在一拍内切到位；慢拍才允许糊着跟
        tau = 0.012 if fast else (0.28 - 0.25 * clarity)

        # 快拍不提前切，切边对准真实撞击。慢拍才预切
        if (
            not fast
            and not self._pre_switched
            and clarity >= 0.55
            and self._slap_clear >= 0.38
            and self._vocal < self._slap_clear - 0.05
            and self._side_hold <= 0
            and (self._time - self._last_hit) > period * 0.45
        ):
            lead = self._phase_lead_s()
            if self._phase >= max(0.72, 1.0 - lead / period):
                self._flip = not self._flip
                self._side_target = 0.92 if self._flip else 0.08
                self._pre_switched = True
                if clarity >= 0.7 or self._next_is_deep():
                    self._side_mix = self._side_target
                    tau = 0.03

        self._side_mix = _follow(self._side_mix, self._side_target, dt, tau)

    def _refresh_period(self) -> None:
        # 已经有撞击间隔之后，周期只由连续间隔学习，不再用声能自相关改拍
        if len(self._gaps) >= 3:
            return
        if len(self._onset_hist) < 40:
            return
        if self._time - self._period_at < PERIOD_REFRESH_S:
            return
        self._period_at = self._time
        duration = PERIOD_HIST_S
        hop = 0.03
        n = int(duration / hop)
        now = self._time
        series = [0.0] * n
        for stamp, value in self._onset_hist:
            idx = int((stamp - (now - duration)) / hop)
            if 0 <= idx < n:
                series[idx] = max(series[idx], value)
        mean = sum(series) / n
        centered = [v - mean for v in series]
        if max(abs(v) for v in centered) < 0.035:
            return
        best_score = 0.22
        best_lag = self.period
        lag_s = 0.25
        while lag_s <= 1.35 + 1e-9:
            lag = int(round(lag_s / hop))
            if 0 < lag < n // 2:
                left = centered[:-lag]
                right = centered[lag:]
                dot = sum(a * b for a, b in zip(left, right))
                n1 = math.sqrt(sum(a * a for a in left))
                n2 = math.sqrt(sum(b * b for b in right))
                if n1 > 1e-5 and n2 > 1e-5:
                    score = dot / (n1 * n2) - 0.03 * abs(lag_s - 0.55)
                    if score > best_score:
                        best_score = score
                        best_lag = lag_s
            lag_s += 0.03
        if best_score <= 0.22:
            return
        rel = abs(best_lag - self.period) / max(0.22, self.period)
        if rel < PERIOD_CHANGE_MIN:
            return
        self.period = self.period * 0.9 + best_lag * 0.1

    def _update_stability(self, dt: float = 0.05) -> None:
        # 用真实帧间隔；tau 拉长，避免单次不齐拍把稳定度打到 0/1 来回甩
        step = max(0.01, min(0.12, float(dt)))
        tau = 2.8
        if len(self._hits) < 6:
            self._stability = _follow(self._stability, 0.0, step, tau)
            return
        recent_hits = [t for t in self._hits if self._time - t < 10.0]
        if len(recent_hits) < 6:
            self._stability = _follow(self._stability, 0.15, step, tau)
            return
        intervals = [
            recent_hits[i] - recent_hits[i - 1] for i in range(1, len(recent_hits))
        ][-12:]
        if len(intervals) < 5:
            self._stability = _follow(self._stability, 0.2, step, tau)
            return
        mean = sum(intervals) / len(intervals)
        var = sum((x - mean) ** 2 for x in intervals) / len(intervals)
        cv = math.sqrt(var) / max(0.08, mean)
        target = max(0.0, min(1.0, 1.0 - cv * 1.5))
        self._stability = _follow(self._stability, target, step, tau)

    def _advance_phase_clock(self, dt: float, hit: bool) -> None:
        # 命中若已在峰区锁相，仍允许本帧微进；半程命中绝不清零
        # 左右时钟只跟撞击间隔。花样（渐疏等）只改预测力度，不再把相位周期拉长
        period = self._switch_period()
        # 有活动时相位必须转；idle 静音才停
        if self.phase == "idle" and not self._matched and self.env < 0.05:
            return
        if hit and self._phase < 0.05:
            return
        # 深入停顿：相位慢转，感到停一下；不在停顿前预减速
        rate = 0.35 if self._side_hold > 0 else 1.0
        self._phase += (dt / period) * rate
        if self._phase < 1.0:
            return
        already = self._pre_switched
        self._phase -= 1.0
        if self._phase > 1.0:
            self._phase = 0.0
        self._pre_switched = False
        # 快拍只跟真实撞击切边。格子再补一刀会和下一记叠成两次，看起来没动
        if period <= 0.28:
            return
        if not already and not hit and self._coast_ok():
            self._flip_side(hard=False)

    def _hit_density(self) -> float:
        if not self._hits:
            return 0.0
        window = 2.2
        recent = [t for t in self._hits if self._time - t <= window]
        if not recent:
            return 0.0
        expect = window / max(0.22, self.period)
        return max(0.0, min(1.0, len(recent) / max(1.2, expect)))

    def _hit_density_window(self, window: float) -> float:
        recent = [t for t in self._hits if self._time - t <= window]
        return float(len(recent))

    def _note_orgasm_tail(self, impact: float) -> None:
        """高潮后的补插计数。不切左右，只用来判断是几下还是又抽起来了。"""
        # 余声呻吟的起伏不要算成又开始抽
        if self._vocal > 0.28 and impact < 0.55:
            return
        step = max(0.14, self._switch_period())
        min_gap = max(0.12, step * 0.28)
        dipped = self._impact_floor < 0.24 and self._impact_floor < impact * 0.7
        if impact < 0.45 or not dipped:
            return
        if self._orgasm_tail and self._time - self._orgasm_tail[-1] < min_gap:
            return
        self._orgasm_tail.append(self._time)

    def _orgasm_resumed(self) -> bool:
        """补插超过几下，并且这一小段还在密着打，才算重新开抽。"""
        if len(self._orgasm_tail) < FINALE_TAIL_STROKES + 3:
            return False
        recent = [t for t in self._orgasm_tail if self._time - t <= 1.4]
        return len(recent) >= 4

    # ——— pattern（只影响预测力度/周期，亮度统一呼吸）———

    def _recommend_pattern(self) -> None:
        if self.phase in ("climax", "finale", "afterglow", "idle", "peak_candidate"):
            return
        if self._stability > 0.55 and self.period >= 0.55:
            want_n = 9
        elif self._stability > 0.4 and self.period >= 0.42:
            want_n = 7
        elif self.period < 0.35 or self._stability < 0.28:
            want_n = 5
        else:
            want_n = 7
        n_ready = (
            self._time - self._shallow_n_at >= SHALLOW_N_MIN_S
            and (self._stroke_i - self._shallow_hits0) >= SHALLOW_N_MIN_HITS
            and self._stability >= 0.28
        )
        if want_n != self._shallow_n and n_ready and abs(want_n - self._shallow_n) >= 2:
            self._shallow_n = want_n
            self._shallow_n_at = self._time
            self._shallow_hits0 = self._stroke_i
            night_log.log_event("shallow_n", n=want_n, stability=round(self._stability, 3))

        st = self._stability
        ar = self.arousal
        cur = self.pattern
        # 进入/离开阈值拉开，避免稳定度在 0.35↔0.55 抖时匀拍/浅深对打
        if cur == "shallow_deep":
            if st < 0.26 or ar < 0.26:
                choice: Pattern = "even"
            elif st > 0.64 and ar < 0.46 and self.phase == "build":
                choice = "stretch"
            else:
                choice = "shallow_deep"
        elif cur == "stretch":
            if st < 0.36 or ar > 0.66 or self.phase == "peak_approach":
                choice = "shallow_deep" if st > 0.5 else "even"
            else:
                choice = "stretch"
        elif cur == "surge":
            if self.phase != "peak_approach" or ar < 0.62 or st < 0.42:
                choice = "shallow_deep" if st > 0.52 else "even"
            else:
                choice = "surge"
        elif cur == "double":
            if st < 0.48 or self.period >= 0.38 or ar > 0.68:
                choice = "even"
            else:
                choice = "double"
        else:
            # even：进入浅深要更高门槛；临近高潮更倾向稳住浅深而不是 surge 乱切
            if self.phase == "peak_approach" and st > 0.68 and ar > 0.85:
                choice = "surge"
            elif st > 0.62 and 0.44 <= ar < 0.8:
                choice = "shallow_deep"
            elif st > 0.64 and self.phase == "build" and ar < 0.44:
                choice = "stretch"
            elif st > 0.62 and self.period < 0.3 and ar < 0.5:
                choice = "double"
            else:
                choice = "even"

        if choice == cur:
            self._pending_pattern = None
            return
        elapsed = self._time - self._pattern_since
        # 只用时间驻留；撞击数曾把快拍花样切到 <1s
        min_dwell = PATTERN_MIN_DWELL_S
        if self.period < 0.4 or self.phase == "peak_approach":
            min_dwell = max(min_dwell, 18.0)
        if elapsed < min_dwell:
            self._pending_pattern = choice
            return
        self._pending_pattern = None
        self.pattern = choice
        self._pattern_since = self._time
        self._pattern_hits0 = self._stroke_i
        night_log.log_event(
            "pattern",
            pattern=choice,
            prev=cur,
            phase=self.phase,
            stability=round(st, 3),
            period=round(self.period, 3),
            n=self._shallow_n,
        )

    # ——— 尖峰候选 → 卸力 → 结算 ———

    def _air_percentile_high(self) -> float:
        vals = [v for t, v in self._air_hist if self._time - t < 3.0]
        if len(vals) < 8:
            return 0.28  # 历史不足时用较低基线，靠绝对尖叫门槛
        ordered = sorted(vals)
        return ordered[int(len(ordered) * 0.85)]

    def _try_enter_peak_candidate(
        self, *, air: float, loud: float, onset: float, hit_rate: float
    ) -> bool:
        if self.climax_cool > 0 or self.phase in ("finale", "peak_candidate"):
            return False
        if self.phase not in ("build", "peak_approach"):
            return False
        # 日志：106s climax=0.43 就进 afterglow，整场 afterglow↔冲刺互撕
        if self.climax_progress < 0.72:
            return False
        scream = air * loud
        hi = self._air_percentile_high()
        spike = (scream > max(0.40, hi * 1.12) or scream > 0.52) and loud > 0.45 and air > 0.32
        step = self._switch_period()
        since_hit = self._time - self._last_hit
        expect = max(1.5, 1.0 / step)
        hits_1 = self._hit_density_window(1.0)
        # 相对当前拍子变稀，或已经空了一拍以上：卸力/射
        slowed = since_hit > step * 1.65 or hits_1 <= expect * 0.45
        release = (
            self.climax_progress >= 0.8
            and slowed
            and (spike or air > 0.36 or self._vocal > 0.42)
        )
        if not ((spike and self.arousal > 0.4) or release):
            return False
        # 仍按满格子在打 → 还在冲刺，不结算
        if not slowed and hits_1 >= expect * 0.7:
            return False
        # 再高潮：15s 内不再走短 bloom（日志里 bloom>>尖峰）；一律重新进尖峰确认
        if self._last_finale > 0 and (self._time - self._last_finale) < FINALE_REENTRY_S:
            if (
                not spike
                or scream < 0.52
                or since_hit < 2.8
                or self.climax_progress < 0.82
                or self.arousal < 0.55
            ):
                return False
            # 落入下方 peak_candidate，不再直接 purple bloom

        self.phase = "peak_candidate"
        self.phase_t = 0.0
        self._orgasm_tail.clear()
        self._peak_at = self._time
        self._peak_air = scream
        self._peak_loud = loud
        self._hits_before_peak = self._hit_density_window(1.0)
        self._scream_hold = 0.08
        self.white_flash = 1.0
        self.breath_lvl = max(self.breath_lvl, 0.9)
        night_log.log_event(
            "peak_candidate",
            scream=round(scream, 3),
            arousal=round(self.arousal, 3),
            climax=round(self.climax_progress, 3),
        )
        return True

    def _step_peak_candidate(
        self,
        *,
        bright_max: int,
        dt: float,
        air: float,
        loud: float,
        thrust_raw: float,
    ) -> NightFrame:
        self.phase_t += dt
        scream = air * loud
        # 尖叫只补一记短闪，不拖成长白
        if scream > self._peak_air * 0.55 and loud > 0.42:
            self._scream_hold = min(1.8, self._scream_hold + dt)
            self._peak_air = max(self._peak_air, scream)
            if self.white_flash < 0.2:
                self.white_flash = max(self.white_flash, 0.85)
        else:
            self.white_flash *= math.exp(-dt / 0.07)

        since = self._time - self._peak_at
        hits_now = self._hit_density_window(1.0)
        # 她已经到了：他再补插几下仍进结算。连着打过这几下才取消
        if since > 1.0 and self._orgasm_resumed() and thrust_raw > 0.35:
            self.phase = "peak_approach"
            self.phase_t = 0.0
            self.white_flash = 0.0
            night_log.log_event("peak_cancel", reason="reaccelerate")
            return self._paint(bright_max)

        # 卸力确认。进尖峰时往往已经 sparse——必须先展示够 PEAK_MIN_SHOW_S
        # 空 tail / 刚清过的 tail 绝不能当 ok（旧 bug：≤8 在 0 时恒真 → 0.9s 必进紫）
        weakened = scream < self._peak_air * 0.5 and loud < self._peak_loud * 0.65
        sparse = hits_now <= max(0.5, self._hits_before_peak * 0.45)
        tail_n = len(self._orgasm_tail)
        tail_ok = (
            since >= PEAK_MIN_SHOW_S
            and 3 <= tail_n <= FINALE_TAIL_STROKES
            and sparse
            and weakened
            and not self._orgasm_resumed()
        )
        release_ok = weakened and sparse and since >= PEAK_MIN_SHOW_S
        if (
            since >= RELEASE_MIN_S
            and self.climax_progress >= 0.75
            and not self._orgasm_resumed()
            and (release_ok or tail_ok)
        ):
            self._enter_finale("full")
            return self._paint_finale(bright_max, bloom_only=False)

        if since > RELEASE_MAX_S:
            if self.climax_progress >= 0.75 and not self._orgasm_resumed():
                self._enter_finale("full")
                return self._paint_finale(bright_max, bloom_only=False)
            # 还在密着抽，不算她高潮后的补插
            self.phase = "peak_approach" if self.arousal > 0.5 else "build"
            self.phase_t = 0.0
            self.white_flash = 0.0
            self.climax_cool = 1.2
            night_log.log_event("peak_timeout")
            return self._paint(bright_max)

        return self._paint(bright_max)

    def _enter_finale(self, kind: str) -> None:
        self.phase = "finale"
        self.phase_t = 0.0
        self._finale_t = 0.0
        self._finale_kind = kind
        self._last_finale = self._time
        self._finale_moan = 0.0
        self._finale_moan_pulse = 0.0
        self._finale_moan_prev = 0.0
        self._last_finale_moan_at = -100.0
        self._finale_slap_pulse = 0.0
        self._last_finale_slap_at = -100.0
        self._finale_side_flip = False
        self._finale_side_mix = 0.5
        self._finale_slap_gaps.clear()
        self.white_flash = 0.0
        self.climax_cool = 3.0
        night_log.log_event(
            "finale_full" if kind == "full" else "finale_bloom",
            arousal=round(self.arousal, 3),
            period=round(self.period, 3),
        )

    def _update_finale_moan(
        self, dt: float, *, air: float, loud: float, breath_raw: float
    ) -> None:
        """结算余温：慢包络 + 每声短 pulse，驱动明显紫色明暗。"""
        # 直接听叫声。撞击清晰度会在补插后还挂着，不能拿来把余声乘掉
        moan_raw = max(0.0, min(1.0, max(self._vocal, breath_raw)))
        if (
            moan_raw < 0.14
            and self._slap_clear < 0.40
            and loud > 0.26
            and air > 0.28
        ):
            moan_raw = max(moan_raw, min(1.0, loud * (0.42 + 0.58 * air)))
        # 起 ~100ms，落 ~1.1s：余韵包络，别跟成节拍器
        self._finale_moan = _follow(
            self._finale_moan,
            moan_raw,
            dt,
            0.10 if moan_raw > self._finale_moan else 1.10,
        )
        if moan_raw >= 0.11:
            self._last_finale_moan_at = self._time
        # 每一声抬起：短 pulse 叠在慢包络上，肉眼能看见「吸亮」
        rising = moan_raw > self._finale_moan_prev + 0.04 and moan_raw >= 0.14
        if rising:
            self._finale_moan_pulse = max(
                self._finale_moan_pulse, 0.62 + 0.38 * moan_raw
            )
        self._finale_moan_pulse *= math.exp(-dt / 0.38)
        self._finale_moan_prev = moan_raw

    def _update_finale_slap(
        self,
        dt: float,
        *,
        impact: float,
        onset: float,
        slap_clear: float,
    ) -> None:
        """结算补插：极轻但可见的左右偏亮；不抬整灯亮度。"""
        # ~0.3s 偏亮，再 ~0.4s 回对称
        self._finale_slap_pulse *= math.exp(-dt / 0.32)
        if self._finale_slap_pulse < 0.05:
            self._finale_side_mix = _follow(self._finale_side_mix, 0.5, dt, 0.38)
        else:
            # 偏置目标拉远一点，到位要快，才「嘀一下」
            want = 0.86 if self._finale_side_flip else 0.14
            self._finale_side_mix = _follow(
                self._finale_side_mix,
                want,
                dt,
                0.05 if abs(self._finale_side_mix - want) > 0.10 else 0.12,
            )

        # 叫声叠着时抬门槛；真有裂纹/撞感仍放行
        vocal_gate = self._vocal > 0.38 and impact < 0.48 and slap_clear < 0.40
        if vocal_gate and onset < 0.55:
            return
        # 用最近补插间隔估 min_gap，快补插别被慢周期卡死
        if len(self._finale_slap_gaps) >= 2:
            gaps = sorted(self._finale_slap_gaps)
            med = gaps[len(gaps) // 2]
            min_gap = max(0.10, min(0.28, med * 0.55))
        else:
            step = max(0.14, self._switch_period())
            min_gap = max(0.11, min(0.30, step * 0.30))
        since = (
            self._time - self._last_finale_slap_at
            if self._last_finale_slap_at > 0
            else 99.0
        )
        if since < min_gap:
            return
        # 谷→峰：允许比主节奏稍浅的谷，高潮后 thrump 常偏软
        rise = impact - self._impact_floor
        dipped = self._impact_floor < impact * 0.78 and (
            self._impact_floor < 0.34 or rise >= 0.10
        )
        if not dipped:
            return
        # 综合判：撞击为主，清晰度/onset 辅助弱 thrump
        score = (
            0.62 * impact
            + 0.22 * max(0.0, min(1.0, slap_clear))
            + 0.16 * max(0.0, min(1.0, onset))
        )
        need = 0.30
        if self._vocal > 0.45:
            need = 0.36
        if score < need and impact < 0.34:
            return
        if impact < 0.28 and slap_clear < 0.32:
            return

        if self._last_finale_slap_at > 0:
            gap = self._time - self._last_finale_slap_at
            if 0.08 <= gap <= 1.6:
                self._finale_slap_gaps.append(gap)
        self._last_finale_slap_at = self._time
        self._finale_slap_pulse = max(
            self._finale_slap_pulse, 0.55 + 0.45 * min(1.0, max(impact, score))
        )
        self._finale_side_flip = not self._finale_side_flip
        # 立刻偏到目标侧，再慢慢回，避免跟丢「嘀一下」
        self._finale_side_mix = 0.86 if self._finale_side_flip else 0.14
        # 吃掉这一峰，下一记必须再落谷，减少连触发
        self._impact_floor = min(self._impact_floor, max(0.08, impact * 0.22))

    def _finale_moan_holding(self) -> bool:
        """余声还在，或两声之间的短停，结算先别收。"""
        if self._finale_moan >= 0.10:
            return True
        return (
            self._last_finale_moan_at > 0
            and (self._time - self._last_finale_moan_at) < FINALE_MOAN_QUIET_S
        )

    def _finale_end_est(self) -> float:
        """预计收束：没余声用底长；有余声则收到最后一声后再静一小段。"""
        if self._last_finale_moan_at <= 0:
            return FINALE_FULL_S
        rel = self._last_finale_moan_at - (self._time - self._finale_t)
        return min(FINALE_MAX_S, max(FINALE_FULL_S, rel + FINALE_MOAN_QUIET_S))

    def _step_finale(
        self,
        levels: Levels,
        *,
        bright_max: int,
        dt: float,
        air: float,
        loud: float,
        onset: float,
        breath_raw: float = 0.0,
        impact: float = 0.0,
        slap_clear: float = 0.0,
    ) -> NightFrame:
        self._finale_t += dt
        self.phase_t += dt
        if self._finale_kind == "bloom":
            self.white_flash = max(self.white_flash, math.exp(-self._finale_t / 0.18))
            if self._finale_t >= FINALE_BLOOM_S:
                self.phase = "afterglow"
                self.phase_t = 0.0
                self._finale_kind = ""
                self.white_flash = 0.0
            return self._paint_finale(bright_max, bloom_only=True)

        self._update_finale_slap(
            dt, impact=impact, onset=onset, slap_clear=slap_clear
        )

        # 再闪只认「偏尖的余声」，别把补插 onset 当尖叫
        if (
            onset > 0.72
            and loud > 0.55
            and self._finale_t > 0.8
            and impact < 0.42
            and self._vocal >= 0.28
        ):
            self.flash = 1.0
            self._finale_moan_pulse = max(self._finale_moan_pulse, 0.85)
            self._last_finale_moan_at = self._time
            night_log.log_event("finale_reflash")

        self.flash *= math.exp(-dt / 0.14)

        # 底长够了才允许收；余声还在就续，最多 FINALE_MAX_S
        if self._finale_t >= FINALE_MAX_S or (
            self._finale_t >= FINALE_FULL_S and not self._finale_moan_holding()
        ):
            night_log.log_event(
                "finale_end",
                dur=round(self._finale_t, 2),
                moan=round(self._finale_moan, 3),
            )
            self.phase = "afterglow"
            self.phase_t = 0.0
            self._finale_kind = ""
            self._stroke_i = 0
            self._surge_i = 0
            self._finale_moan = 0.0
            self._finale_moan_pulse = 0.0
            self._finale_slap_pulse = 0.0
            self._finale_side_mix = 0.5
        return self._paint_finale(bright_max, bloom_only=False)

    def _advance_phase(self, dt: float) -> None:
        self.phase_t += dt
        if self.phase == "afterglow":
            # 有活动绝不赖在余韵（日志 249 帧 afterglow 且 loud>0.1）
            if (
                self._time - self._last_hit < self._switch_period() * 1.2
                and self._hit_density_window(1.2) >= 3
                and self.thrust > 0.4
            ):
                self.phase = "peak_approach" if self.climax_progress > 0.35 else "build"
                self.phase_t = 0.0
                self._matched = True
            elif self.arousal < 0.12 and self.phase_t > 3.5:
                self.phase = "idle"
                self.phase_t = 0.0
            return
        if self.arousal > 0.45 or self.climax_progress > 0.38:
            if self.phase != "peak_approach":
                self.phase = "peak_approach"
                self.phase_t = 0.0
        elif self.arousal > 0.14 or self.climax_progress > 0.06 or self._matched:
            if self.phase not in ("build", "peak_approach"):
                self.phase = "build"
                self.phase_t = 0.0
            elif self.phase == "peak_approach" and self.arousal < 0.38 and self.climax_progress < 0.3:
                self.phase = "build"
                self.phase_t = 0.0
        else:
            if self.phase != "idle" and (self._time - self._last_hit) > 4.0:
                self.phase = "idle"
                self.phase_t = 0.0
                self._matched = False

    # ——— paint ———

    def _paint_finale(self, bright_max: int, *, bloom_only: bool) -> NightFrame:
        """结算：明显呻吟包络（静低/峰高）+ 极轻左右偏亮；啪不抬整灯。"""
        cap = max(1, min(100, int(bright_max)))
        t = self._finale_t
        moan = max(0.0, min(1.0, self._finale_moan))
        pulse = max(0.0, min(1.0, self._finale_moan_pulse))
        slap_p = max(0.0, min(1.0, self._finale_slap_pulse))
        # 包络：慢 moan + 短 pulse；静音时才露很弱的底呼吸
        env = max(0.0, min(1.0, 0.72 * moan + 0.55 * pulse))
        quiet = max(0.0, 1.0 - env * 1.35)
        base = 0.5 + 0.5 * math.sin(t * math.pi * 0.55)
        # 两侧同相跟呻吟（相位差极小，不当左右喘）
        inhale = max(0.0, min(1.0, env * 0.92 + quiet * 0.14 * base))
        inhale_r = max(
            0.0,
            min(1.0, env * 0.90 + quiet * 0.14 * (0.5 + 0.5 * math.sin(t * math.pi * 0.55 - 0.18))),
        )
        if bloom_only:
            k = math.exp(-t / 0.28)
            level = (0.55 + 0.35 * inhale) * k + 0.08
            col = _tint(PEAK_VIOLET, PEAK_LILAC, 0.35 + 0.55 * inhale)
            left = _chroma(col, 0.88)
            right = _chroma(_tint(col, 0x7040D0, 0.2), 0.82)
            left = _tint(0x140820, left, 0.55 + 0.45 * inhale)
            right = _tint(0x140820, right, 0.55 + 0.45 * inhale_r)
            bright = max(1, min(cap, int(round(cap * min(1.0, level)))))
            return NightFrame(bright, left, right, True, "finale")

        violet = 0x7040D0
        end_est = self._finale_end_est()
        fade_start = max(0.7, end_est - FINALE_FADE_S)
        # 静 ~0.18 / 缓 ~0.48 / 峰 ~0.78–0.85，静↔峰约 3×
        body = 0.18 + 0.42 * moan + 0.28 * pulse
        if t < 0.7:
            burst = 1.0 - t / 0.7
            level = min(1.0, 0.42 + 0.28 * body + 0.22 * burst)
            col = _tint(violet, PEAK_LILAC, 0.20 + 0.55 * inhale)
        elif t < fade_start:
            level = min(1.0, body + 0.08 * quiet * base)
            col = _interp(
                list(FINALE_PURPLE),
                0.12 + 0.55 * inhale + 0.28 * moan + 0.12 * pulse,
            )
        else:
            u = (t - fade_start) / max(0.2, end_est - fade_start)
            u = max(0.0, min(1.0, u))
            level = max(0.06, body * (1.0 - 0.78 * u))
            col = _tint(0x140820, violet, 0.50 * (1.0 - u) * (0.35 + 0.65 * inhale))
        # 补插：亮侧略浅紫/淡紫，暗侧略深紫——同族色差，比纯明暗更容易分左右
        sm = max(0.0, min(1.0, self._finale_side_mix))
        side_w = 0.28 * slap_p
        hue_w = 0.48 * slap_p  # 色差权重；无补插时左右仍同色
        lit_col = _chroma(_tint(col, PEAK_LILAC, 0.62 * hue_w), 0.92)
        dim_col = _chroma(_tint(col, 0x140828, 0.72 * hue_w), 0.74)
        left_col = _tint(dim_col, lit_col, sm)
        right_col = _tint(dim_col, lit_col, 1.0 - sm)
        left_k = max(0.0, min(1.0, 0.28 + 0.72 * inhale + side_w * (2.0 * sm - 1.0)))
        right_k = max(
            0.0, min(1.0, 0.28 + 0.72 * inhale_r + side_w * (1.0 - 2.0 * sm))
        )
        left = _tint(0x0A0614, left_col, left_k)
        right = _tint(0x0A0614, right_col, right_k)
        bright = max(1, min(cap, int(round(cap * min(1.0, level)))))
        return NightFrame(bright, left, right, True, "finale")

    def _paint(self, bright_max: int) -> NightFrame:
        """亮的一头=舒服与高潮叠色；暗的一头回到近黑的块。左右只换哪边亮。尖叫仍是整灯粉白短闪。"""
        cap = max(1, min(100, int(bright_max)))
        wine = 0x3A101C
        warm = 0xE8A0B4
        vivid = 0xFF5088

        # 高潮尖叫：整灯短闪带粉高亮白，闪完回到舒服/高潮色
        if self.white_flash > 0.05:
            w = min(1.0, self.white_flash)
            level = 0.82 + 0.18 * w
            if self.phase == "peak_candidate":
                col = _tint(PEAK_VIOLET, PEAK_LILAC, 0.35 + 0.55 * w)
            else:
                col = _tint(PEAK_PINK, PEAK_WHITE, 0.45 + 0.5 * w)
            rgb = _chroma(col, 0.9)
            bright = max(1, min(cap, int(round(cap * level))))
            return NightFrame(bright, rgb, rgb, False, self.phase)

        # —— 亮度：舒服程度 ——
        comfort_lvl = max(0.0, min(1.0, self.breath_lvl))
        level = 0.14 + 0.82 * comfort_lvl
        self._bright_smooth += (level - self._bright_smooth) * 0.42
        self._bright_smooth = max(0.12, min(1.0, self._bright_smooth))
        bright = max(1, min(cap, int(round(cap * self._bright_smooth))))

        # —— 颜色：舒服、高潮各自按程度加，不看左右、不看这一下撞击 ——
        comfort = max(
            0.0,
            min(
                1.0,
                0.42 * self._feedback + 0.38 * self._moan_boost + 0.20 * self.breath,
            ),
        )
        climax = max(0.0, min(1.0, self.climax_progress))
        cr, cg, cb = _shift(wine, warm, comfort)
        kr, kg, kb = _shift(wine, vivid, climax)
        wr, wg, wb = _split(wine)
        base = _join(wr + cr + kr, wg + cg + kg, wb + cb + kb)
        keep = max(0.0, min(1.0, 0.30 + 0.34 * comfort + 0.42 * climax))
        col = _chroma(base, keep)

        sm = max(0.0, min(1.0, self._side_mix))
        active = (
            self._matched
            or self.phase in ("build", "peak_approach", "peak_candidate", "afterglow")
            or (self._time - self._last_hit) < 3.5
            or abs(sm - 0.5) > 0.08
        )
        if not active:
            return NightFrame(bright, col, col, False, self.phase)
        if self._switch_period() <= 0.28 or self._hit_clarity >= 0.5:
            sm = 1.0 if sm >= 0.5 else 0.0
        else:
            if sm > 0.55:
                sm = 0.55 + (sm - 0.55) * 1.6
            elif sm < 0.45:
                sm = 0.45 - (0.45 - sm) * 1.6
            sm = max(0.0, min(1.0, sm))
        # 亮侧用叠好的颜色；暗侧拉回暗底，左右才有原来的块感
        lit = _chroma(col, min(0.96, keep + 0.16))
        dim = _chroma(_tint(wine, col, 0.22), max(0.16, keep * 0.35))
        left = _tint(dim, lit, sm)
        right = _tint(dim, lit, 1.0 - sm)
        return NightFrame(bright, left, right, True, self.phase)
