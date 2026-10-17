from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UdpStreamSession:
    host: str
    port: int = 55444
    timeout: float = 2.0
    _sock: socket.socket | None = field(default=None, repr=False)
    _token: str | None = field(default=None, repr=False)
    _next_id: int = 1
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _keepalive_stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _keepalive_thread: threading.Thread | None = field(default=None, repr=False)
    last_error: str | None = None

    @property
    def active(self) -> bool:
        return self._sock is not None and self._token is not None

    def start(self) -> str:
        self.stop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        self._sock = sock
        resp = self._rpc("udp_sess_new", [])
        params = resp.get("params") or {}
        token = params.get("token")
        if not token and isinstance(resp.get("result"), dict):
            token = resp["result"].get("token")
        if not token:
            # some firmwares reply as method udp_sess_token
            if resp.get("method") == "udp_sess_token":
                token = (resp.get("params") or {}).get("token")
        if not token:
            raise RuntimeError(f"no udp token in response: {resp}")
        self._token = token
        self._keepalive_stop.clear()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, name="yeelight-udp-ka", daemon=True
        )
        self._keepalive_thread.start()
        self.last_error = None
        return token

    def stop(self) -> None:
        self._keepalive_stop.set()
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            self._keepalive_thread.join(timeout=1.0)
        self._keepalive_thread = None
        self._token = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def set_bg_rgb(self, rgb: int) -> None:
        if not self.active:
            raise RuntimeError("udp session not active")
        rgb = max(0, min(0xFFFFFF, int(rgb)))
        self._send(
            {
                "id": self._alloc_id(),
                "method": "bg_set_rgb",
                "params": [rgb, "sudden", 0],
                "token": self._token,
            }
        )

    def set_segment_rgb(self, left_rgb: int, right_rgb: int) -> None:
        """氛围灯左右两段分色（会覆盖整条 bg_set_rgb 状态）。"""
        if not self.active:
            raise RuntimeError("udp session not active")
        left = max(1, min(0xFFFFFF, int(left_rgb)))
        right = max(1, min(0xFFFFFF, int(right_rgb)))
        self._send(
            {
                "id": self._alloc_id(),
                "method": "set_segment_rgb",
                "params": [left, right],
                "token": self._token,
            }
        )

    def set_bright(self, bright: int) -> None:
        """主灯亮度（UDP，不占 TCP 配额）。"""
        if not self.active:
            raise RuntimeError("udp session not active")
        bright = max(1, min(100, int(bright)))
        self._send(
            {
                "id": self._alloc_id(),
                "method": "set_bright",
                "params": [bright, "sudden", 0],
                "token": self._token,
            }
        )

    def set_bg_bright(self, bright: int) -> None:
        """氛围灯亮度（UDP）。"""
        if not self.active:
            raise RuntimeError("udp session not active")
        bright = max(1, min(100, int(bright)))
        self._send(
            {
                "id": self._alloc_id(),
                "method": "bg_set_bright",
                "params": [bright, "sudden", 0],
                "token": self._token,
            }
        )

    def set_ct(self, ct: int) -> None:
        """主灯色温（UDP，拖动热路径不占 TCP 配额）。"""
        if not self.active:
            raise RuntimeError("udp session not active")
        ct = max(1700, min(6500, int(ct)))
        self._send(
            {
                "id": self._alloc_id(),
                "method": "set_ct_abx",
                "params": [ct, "sudden", 0],
                "token": self._token,
            }
        )

    def _keepalive_loop(self) -> None:
        while not self._keepalive_stop.wait(10.0):
            try:
                if not self.active:
                    break
                self._send(
                    {
                        "id": self._alloc_id(),
                        "method": "udp_sess_keep_alive",
                        "params": ["keeplive_interval", "10"],
                        "token": self._token,
                    }
                )
            except OSError as exc:
                self.last_error = str(exc)
                break

    def _rpc(self, method: str, params: list[Any]) -> dict[str, Any]:
        assert self._sock is not None
        req_id = self._alloc_id()
        self._send({"id": req_id, "method": method, "params": params})
        # handshake expects a reply
        data, _ = self._sock.recvfrom(65535)
        return json.loads(data.decode("utf-8", errors="replace").strip())

    def _send(self, payload: dict[str, Any]) -> None:
        assert self._sock is not None
        raw = (json.dumps(payload, separators=(",", ":")) + "\r\n").encode("utf-8")
        self._sock.sendto(raw, (self.host, self.port))

    def _alloc_id(self) -> int:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            return req_id


def smooth_color(
    prev: tuple[int, int, int] | None,
    nxt: tuple[int, int, int],
    alpha: float = 0.35,
) -> tuple[int, int, int]:
    if prev is None:
        return nxt
    return (
        int(prev[0] + (nxt[0] - prev[0]) * alpha),
        int(prev[1] + (nxt[1] - prev[1]) * alpha),
        int(prev[2] + (nxt[2] - prev[2]) * alpha),
    )


def adaptive_smooth_color(
    prev: tuple[int, int, int] | None,
    nxt: tuple[int, int, int],
) -> tuple[int, int, int]:
    """更跟手：大变化几乎直出，小抖动才平滑。"""
    if prev is None:
        return nxt
    dist = abs(prev[0] - nxt[0]) + abs(prev[1] - nxt[1]) + abs(prev[2] - nxt[2])
    if dist >= 50:
        alpha = 0.9
    elif dist >= 22:
        alpha = 0.7
    elif dist >= 10:
        alpha = 0.5
    else:
        alpha = 0.32
    return smooth_color(prev, nxt, alpha=alpha)