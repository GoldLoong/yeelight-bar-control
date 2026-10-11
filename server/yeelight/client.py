from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


class QuotaExceeded(RuntimeError):
    """Yeelight TCP client quota exceeded (~60 cmds/min)."""


@dataclass
class YeelightClient:
    host: str
    port: int = 55443
    timeout: float = 3.0
    # 官方约 60/min/连接。令牌桶：可持续 ~50/min，允许短突发（切模式连发几条）。
    token_capacity: float = 10.0
    token_refill_per_sec: float = 0.85
    min_cmd_gap: float = 0.05  # 同连接最小间隔，避免塞满 socket
    _sock: socket.socket | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _next_id: int = 1
    _last_cmd_at: float = field(default=0.0, repr=False)
    _quota_blocked_until: float = field(default=0.0, repr=False)
    _tokens: float = field(default=10.0, repr=False)
    _last_refill_at: float = field(default_factory=time.monotonic, repr=False)
    last_error: str | None = None

    def connect(self) -> None:
        self.close()
        sock = socket.create_connection((self.host, self.port), self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        self.last_error = None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def ensure(self) -> None:
        if not self.connected:
            self.connect()

    def quota_blocked(self) -> bool:
        return time.monotonic() < self._quota_blocked_until

    def _refill_tokens(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_refill_at)
        self._tokens = min(
            self.token_capacity,
            self._tokens + elapsed * self.token_refill_per_sec,
        )
        self._last_refill_at = now

    def _wait_for_slot(self) -> None:
        """令牌桶 + 最小间隔；空闲后首条几乎立即发出。"""
        while True:
            now = time.monotonic()
            if now < self._quota_blocked_until:
                raise QuotaExceeded(
                    f"TCP 配额冷却中，约 {int(self._quota_blocked_until - now)}s 后恢复"
                )
            self._refill_tokens()
            gap = self.min_cmd_gap - (now - self._last_cmd_at)
            if self._tokens >= 1.0 and gap <= 0:
                self._tokens -= 1.0
                return
            wait_token = 0.0
            if self._tokens < 1.0:
                wait_token = (1.0 - self._tokens) / max(self.token_refill_per_sec, 0.01)
            time.sleep(max(0.01, min(0.25, max(gap, wait_token))))

    def command(self, method: str, params: list[Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._wait_for_slot()

            self.ensure()
            assert self._sock is not None
            req_id = self._next_id
            self._next_id += 1
            payload = {"id": req_id, "method": method, "params": params or []}
            data = (json.dumps(payload, separators=(",", ":")) + "\r\n").encode("utf-8")
            try:
                self._sock.sendall(data)
                raw = self._readline()
            except OSError as exc:
                self.close()
                self.last_error = str(exc)
                raise
            finally:
                self._last_cmd_at = time.monotonic()

            try:
                resp = json.loads(raw)
            except json.JSONDecodeError as exc:
                self.last_error = f"invalid json: {raw!r}"
                raise RuntimeError(self.last_error) from exc

            if "error" in resp:
                err = resp["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                self.last_error = str(msg)
                if isinstance(err, dict) and (
                    err.get("code") == -1 or "quota" in str(msg).lower()
                ):
                    self._quota_blocked_until = time.monotonic() + 65.0
                    raise QuotaExceeded(str(msg))
                raise RuntimeError(str(msg))

            self.last_error = None
            return resp

    def _readline(self) -> str:
        assert self._sock is not None
        buf = bytearray()
        while True:
            chunk = self._sock.recv(1)
            if not chunk:
                raise ConnectionError("connection closed")
            buf.extend(chunk)
            if buf.endswith(b"\n"):
                break
            if len(buf) > 65536:
                raise RuntimeError("response too large")
        return buf.decode("utf-8", errors="replace").strip()

    def get_prop(self, props: list[str]) -> list[Any]:
        resp = self.command("get_prop", props)
        return list(resp.get("result") or [])

    def snapshot(self) -> dict[str, Any]:
        props = [
            "power",
            "main_power",
            "bright",
            "ct",
            "bg_power",
            "bg_bright",
            "bg_rgb",
            "bg_ct",
            "bg_lmode",
            "name",
            "model",
            "fw_ver",
        ]
        values = self.get_prop(props)
        data = dict(zip(props, values, strict=False))
        if data.get("bg_rgb") not in (None, ""):
            try:
                data["bg_rgb_hex"] = f"#{int(data['bg_rgb']):06x}"
            except (TypeError, ValueError):
                data["bg_rgb_hex"] = None
        return data

    def set_main_power(self, on: bool, effect: str = "sudden", duration: int = 0) -> None:
        self.command("set_power", ["on" if on else "off", effect, duration])

    def set_main_bright(self, bright: int, effect: str = "sudden", duration: int = 0) -> None:
        bright = max(1, min(100, int(bright)))
        self.command("set_bright", [bright, effect, duration])

    def set_main_ct(self, ct: int, effect: str = "sudden", duration: int = 0) -> None:
        ct = max(1700, min(6500, int(ct)))
        self.command("set_ct_abx", [ct, effect, duration])

    def set_main_ct_scene(self, ct: int, bright: int) -> None:
        """一条指令：开主灯 + 色温 + 亮度（set_scene）。"""
        ct = max(1700, min(6500, int(ct)))
        bright = max(1, min(100, int(bright)))
        self.command("set_scene", ["ct", ct, bright])

    def set_bg_power(self, on: bool, effect: str = "sudden", duration: int = 0) -> None:
        self.command("bg_set_power", ["on" if on else "off", effect, duration])

    def set_bg_bright(self, bright: int, effect: str = "sudden", duration: int = 0) -> None:
        bright = max(1, min(100, int(bright)))
        self.command("bg_set_bright", [bright, effect, duration])

    def set_bg_rgb(self, rgb: int, effect: str = "sudden", duration: int = 0) -> None:
        rgb = max(0, min(0xFFFFFF, int(rgb)))
        self.command("bg_set_rgb", [rgb, effect, duration])

    def set_bg_rgb_hex(self, hex_color: str, effect: str = "sudden", duration: int = 0) -> None:
        value = hex_color.strip().lstrip("#")
        self.set_bg_rgb(int(value, 16), effect, duration)

    def set_bg_color_scene(self, rgb: int, bright: int) -> None:
        """一条指令：开氛围灯 + RGB + 亮度。"""
        rgb = max(0, min(0xFFFFFF, int(rgb)))
        bright = max(1, min(100, int(bright)))
        try:
            self.command("bg_set_scene", ["color", rgb, bright])
        except RuntimeError:
            self.command("set_scene", ["color", rgb, bright])

    def set_bg_cf_scene(self, count: int, action: int, flow_expression: str) -> None:
        """一条指令：开氛围灯并启动色流。"""
        params = [int(count), int(action), flow_expression]
        try:
            self.command("bg_set_scene", ["cf", *params])
        except RuntimeError:
            try:
                self.command("set_scene", ["cf", *params])
            except RuntimeError:
                self.bg_start_cf(count, action, flow_expression)

    def bg_stop_cf(self) -> None:
        try:
            self.command("bg_stop_cf", [])
        except RuntimeError:
            # 部分固件仅支持 stop_cf
            self.command("stop_cf", [])

    def bg_start_cf(self, count: int, action: int, flow_expression: str) -> None:
        """
        count=0 无限循环；action: 0恢复 / 1停留 / 2关闭
        flow_expression: duration,mode,value,bright,...
        """
        try:
            self.command("bg_start_cf", [int(count), int(action), flow_expression])
        except RuntimeError:
            self.command("start_cf", [int(count), int(action), flow_expression])

    def apply_ambient_fx(
        self,
        fx: str,
        *,
        bright: int = 50,
        speed_ms: int = 3800,
        hex_color: str = "#2A5BFF",
        colors: list[str] | None = None,
        direction: str = "out",
    ) -> None:
        """应用氛围灯效果：static / breath / cycle / flow（尽量 1 条 TCP）。"""
        from server.yeelight.effects import (
            build_breath_flow,
            build_cycle_flow,
            build_flow_flow,
            hex_to_rgb_int,
            normalize_colors,
        )

        bright = max(1, min(100, int(bright)))
        speed_ms = max(400, min(12000, int(speed_ms)))
        rgb = hex_to_rgb_int(hex_color)

        if fx == "static" or not fx:
            self.set_bg_color_scene(rgb, bright)
            return

        if fx == "breath":
            expr = build_breath_flow(rgb=rgb, bright=bright, speed_ms=speed_ms)
        elif fx == "cycle":
            cols = normalize_colors(colors, hex_color)
            expr = build_cycle_flow(
                colors=cols,
                bright=bright,
                speed_ms=speed_ms,
                reverse=direction in ("rl", "in"),
            )
        else:  # flow / 燃彩
            dir_ = direction if direction in ("lr", "rl", "in", "out") else "out"
            expr = build_flow_flow(bright=bright, speed_ms=speed_ms, direction=dir_)  # type: ignore[arg-type]

        self.set_bg_cf_scene(0, 1, expr)

    def apply_exit_mode(
        self,
        mode: str,
        *,
        saved: dict[str, Any] | None = None,
    ) -> None:
        """
        IINA 退出后的氛围灯行为：
        - off_ambient：关闭氛围灯
        - restore：恢复同步开始前快照（无快照则仅开氛围灯）
        - ambient_mode（默认）：开氛围灯 + 默认色/亮度
        """
        if mode == "off_ambient":
            self.set_bg_power(False, "sudden", 0)
            return

        if mode == "restore":
            if not saved:
                self.set_bg_power(True, "sudden", 0)
                return
            bg_on = str(saved.get("bg_power") or "").lower() == "on"
            if not bg_on:
                self.set_bg_power(False, "sudden", 0)
                return
            try:
                bright = int(saved.get("bg_bright") or 40)
            except (TypeError, ValueError):
                bright = 40
            rgb = saved.get("bg_rgb")
            try:
                rgb_i = int(rgb) if rgb not in (None, "") else 0x2A3A55
            except (TypeError, ValueError):
                rgb_i = 0x2A3A55
            self.set_bg_color_scene(rgb_i, max(1, min(100, bright)))
            return

        # ambient_mode
        self.set_bg_color_scene(0x2A3A55, 40)


def rgb_tuple_to_int(r: int, g: int, b: int) -> int:
    return ((r & 255) << 16) | ((g & 255) << 8) | (b & 255)


def luminance_to_bright(r: int, g: int, b: int) -> int:
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return max(1, min(100, int(round(y / 255 * 100))))


class RateLimitedTcp:
    """按 key 节流的 TCP 调用（实例需长期复用）。"""

    def __init__(self, min_interval: float = 3.0) -> None:
        self.min_interval = min_interval
        self._last: dict[str, float] = {}

    def maybe(self, key: str, fn: Callable[[], Any], *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last.get(key, 0) < self.min_interval:
            return False
        fn()
        self._last[key] = time.monotonic()
        return True
