#!/usr/bin/env python3
"""手机操控时监听灯状态变化（TCP props 推送 + 轮询 get_prop）。"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

HOST = os.environ.get("YEELIGHT_HOST", "192.168.0.116")
SECONDS = float(os.environ.get("YEELIGHT_CAPTURE_SECONDS", "60"))
OUT = Path(
    os.environ.get(
        "YEELIGHT_CAPTURE_LOG",
        str(Path(__file__).resolve().parents[1] / "backups/pcap/lamp15-listen-live.log"),
    )
)
OUT.parent.mkdir(parents=True, exist_ok=True)

KEYS = [
    "bg_power",
    "bg_bright",
    "bg_rgb",
    "bg_hue",
    "bg_sat",
    "bg_lmode",
    "bg_flowing",
    "bg_flow_params",
    "flowing",
    "flow_params",
    "bg_ct",
    "music_on",
    "strip_lenth",
    "bg_proact",
    "bright",
    "ct",
    "power",
    "main_power",
]


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg: str) -> None:
    line = f"[{ts()}] {msg}"
    print(line, flush=True)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    OUT.write_text("", encoding="utf-8")
    stop = threading.Event()
    log(f"START host={HOST} seconds={SECONDS} log={OUT}")

    sock = socket.create_connection((HOST, 55443), timeout=5)
    sock.settimeout(0.5)
    log("TCP connected (waiting props push)")

    def reader() -> None:
        buf = b""
        while not stop.is_set():
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    log("TCP closed by peer")
                    break
                buf += chunk
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    if line.strip():
                        log("PUSH " + line.decode("utf-8", "replace"))
            except socket.timeout:
                continue
            except Exception as exc:  # noqa: BLE001
                log(f"reader err {exc}")
                break

    def poller() -> None:
        nid = 1
        last = None
        while not stop.is_set():
            try:
                payload = {"id": nid, "method": "get_prop", "params": KEYS}
                nid += 1
                raw = (json.dumps(payload, separators=(",", ":")) + "\r\n").encode()
                with socket.create_connection((HOST, 55443), timeout=2) as s:
                    s.settimeout(2)
                    s.sendall(raw)
                    data = b""
                    while b"\r\n" not in data:
                        c = s.recv(4096)
                        if not c:
                            break
                        data += c
                resp = json.loads(data.decode().strip().split("\r\n")[0])
                vals = resp.get("result") or []
                cur = dict(zip(KEYS, vals))
                if cur != last:
                    log("PROP " + json.dumps(cur, ensure_ascii=False))
                    last = cur
            except Exception as exc:  # noqa: BLE001
                log(f"poll err {exc}")
            stop.wait(0.35)

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    log("READY — please start phone effect now")
    try:
        time.sleep(SECONDS)
    finally:
        stop.set()
        try:
            sock.close()
        except OSError:
            pass
        log("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
