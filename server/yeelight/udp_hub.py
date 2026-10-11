"""共享 UDP 会话：手动调色温/亮度与同步跟色共用，暂停同步时不关闭。"""

from __future__ import annotations

import threading

from server.yeelight.stream import UdpStreamSession

_lock = threading.Lock()
_session: UdpStreamSession | None = None


def get_udp(host: str, port: int = 55444) -> UdpStreamSession:
    global _session
    with _lock:
        if _session and _session.active and _session.host == host and _session.port == port:
            return _session
        if _session is not None:
            try:
                _session.stop()
            except Exception:
                pass
            _session = None
        sess = UdpStreamSession(host=host, port=port)
        sess.start()
        _session = sess
        return sess


def close_udp() -> None:
    global _session
    with _lock:
        if _session is not None:
            try:
                _session.stop()
            except Exception:
                pass
            _session = None
