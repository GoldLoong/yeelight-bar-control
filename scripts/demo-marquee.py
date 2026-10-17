#!/usr/bin/env python3
"""
流水灯 demo — lamp15 虚拟多段 → 左右两段采样。

硬件只认 left/right（set_segment_rgb 多余参数会被忽略）。
做法：在内存里跑一条虚拟灯带（默认 8 格），每帧把左半/右半
平均成两个颜色再推出去，用时间推进「亮块」形成方向感。

方向（只有水平四向，上下做不了）：
  left   右 → 左
  right  左 → 右
  out    中间 → 两边
  in     两边 → 中间
  cycle  上面四种轮播（默认）

用法：
  python3 scripts/demo-marquee.py
  python3 scripts/demo-marquee.py 192.168.0.116
  python3 scripts/demo-marquee.py --dir out --seconds 8 --hz 14
  python3 scripts/demo-marquee.py --dir cycle --each 5

Ctrl+C 可提前结束；结束会把氛围灯收成温和白。
"""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import os
import socket
import time
from pathlib import Path

TCP_PORT = 55443
UDP_PORT = 55444

DIRS = ("left", "right", "out", "in")


def rgb_int(r: int, g: int, b: int) -> int:
    r = max(1, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    return (r << 16) | (g << 8) | b


def mix_rgb(colors: list[tuple[float, float, float]]) -> int:
    if not colors:
        return rgb_int(1, 0, 0)
    n = len(colors)
    r = sum(c[0] for c in colors) / n
    g = sum(c[1] for c in colors) / n
    b = sum(c[2] for c in colors) / n
    return rgb_int(r, g, b)


def resolve_host(cli: str | None) -> str:
    if cli:
        return cli
    env = (os.environ.get("YEELIGHT_HOST") or "").strip()
    if env:
        return env
    for p in (
        Path.home() / "Library/Application Support/com.yeelightbar.control/config.json",
        Path(__file__).resolve().parents[1] / "config.json",
    ):
        if p.exists():
            try:
                host = json.loads(p.read_text(encoding="utf-8")).get("host")
                if host:
                    return str(host)
            except Exception:
                pass
    raise SystemExit("未找到灯 IP。用法: python3 scripts/demo-marquee.py <host>")


def tcp_rpc(host: str, method: str, params: list, timeout: float = 3.0) -> dict:
    payload = {"id": 1, "method": method, "params": params}
    raw = (json.dumps(payload, separators=(",", ":")) + "\r\n").encode("utf-8")
    with socket.create_connection((host, TCP_PORT), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(raw)
        data = b""
        while b"\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode("utf-8", errors="replace").strip().split("\r\n")[0])


def udp_rpc(
    sock: socket.socket,
    host: str,
    method: str,
    params: list,
    *,
    token: str | None = None,
    req_id: int = 1,
    wait_reply: bool = False,
) -> dict | None:
    payload: dict = {"id": req_id, "method": method, "params": params}
    if token:
        payload["token"] = token
    raw = (json.dumps(payload, separators=(",", ":")) + "\r\n").encode("utf-8")
    sock.sendto(raw, (host, UDP_PORT))
    if not wait_reply:
        return None
    data, _ = sock.recvfrom(65535)
    return json.loads(data.decode("utf-8", errors="replace").strip())


def head_rgb(hue: float, level: float) -> tuple[float, float, float]:
    """level 0..1 → RGB floats。"""
    level = max(0.0, min(1.0, level))
    # 底噪略亮，避免灯珠完全灭时发「闪断」感
    v = 0.06 + 0.94 * level
    s = 0.55 + 0.45 * level
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, s, v)
    return r * 255, g * 255, b * 255


def blob_levels(n: int, centers: list[float], width: float) -> list[float]:
    """虚拟灯带每格亮度 0..1，多个中心可叠加取 max。"""
    out = [0.0] * n
    for i in range(n):
        best = 0.0
        for c in centers:
            # 环绕距离，让 left/right 方向能无带循环
            d = min(abs(i - c), abs(i - c + n), abs(i - c - n))
            # 高斯拖尾
            lv = math.exp(-0.5 * (d / max(0.35, width)) ** 2)
            if lv > best:
                best = lv
        out[i] = best
    return out


def centers_for(direction: str, n: int, phase: float) -> list[float]:
    """
    phase ∈ [0, 1)：一个完整行程。
    虚拟带索引 0 = 最左，n-1 = 最右。
    """
    span = n - 1
    mid = span / 2.0
    if direction == "right":
        return [phase * span]
    if direction == "left":
        return [(1.0 - phase) * span]
    if direction == "out":
        # 从中心向两端
        travel = phase * mid
        return [mid - travel, mid + travel]
    if direction == "in":
        # 从两端向中心
        travel = phase * mid
        return [0.0 + travel, span - travel]
    raise ValueError(direction)


def sample_halves(levels: list[float], hue: float) -> tuple[int, int]:
    n = len(levels)
    mid = n // 2
    left_cols = [head_rgb(hue, lv) for lv in levels[:mid]]
    right_cols = [head_rgb(hue, lv) for lv in levels[mid:]]
    return mix_rgb(left_cols), mix_rgb(right_cols)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="lamp15 虚拟多段流水灯 demo")
    p.add_argument("host", nargs="?", help="灯 IP")
    p.add_argument("--seconds", type=float, default=0.0, help="总时长；0=按 --each×方向数")
    p.add_argument("--each", type=float, default=5.0, help="cycle 时每个方向秒数")
    p.add_argument("--hz", type=float, default=14.0, help="推送帧率")
    p.add_argument(
        "--dir",
        choices=(*DIRS, "cycle"),
        default="cycle",
        help="left/right/out/in 或 cycle 轮播",
    )
    p.add_argument("--cells", type=int, default=8, help="虚拟灯带格数（偶数）")
    p.add_argument("--width", type=float, default=1.15, help="亮块半宽（格）")
    p.add_argument("--period", type=float, default=1.6, help="跑完一条的秒数")
    p.add_argument(
        "--session",
        choices=("udp", "chroma"),
        default="udp",
        help="UDP 会话：udp_sess_new 或 udp_chroma_sess_new",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    host = resolve_host(args.host)
    hz = max(6.0, min(24.0, float(args.hz)))
    interval = 1.0 / hz
    n = max(4, int(args.cells))
    if n % 2:
        n += 1
    width = max(0.5, float(args.width))
    period = max(0.6, float(args.period))

    sequence = list(DIRS) if args.dir == "cycle" else [args.dir]
    each = max(2.0, float(args.each))
    total = float(args.seconds)
    if total <= 0:
        total = each * len(sequence)

    labels = {
        "left": "右→左",
        "right": "左→右",
        "out": "中间→两边",
        "in": "两边→中间",
    }

    print(f"目标灯: {host}")
    print(
        f"虚拟 {n} 段 → 左右采样 | dir={args.dir} hz={hz} "
        f"period={period}s 总约 {total:.0f}s"
    )
    print("方向:", " → ".join(f"{d}({labels[d]})" for d in sequence))
    print("看背面氛围灯… Ctrl+C 可停")

    tcp_rpc(host, "bg_set_power", ["on", "sudden", 0])
    tcp_rpc(host, "bg_set_bright", [60, "sudden", 0])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.5)
    handshake = "udp_chroma_sess_new" if args.session == "chroma" else "udp_sess_new"
    resp = udp_rpc(sock, host, handshake, [], wait_reply=True)
    token = (resp or {}).get("params", {}).get("token") if resp else None
    if not token:
        print("UDP 握手失败:", resp)
        return 1
    print("token:", token)

    req_id = 1
    t0 = time.monotonic()
    t_end = t0 + total
    frames = 0
    hue_base = 0.55  # 青蓝起步，慢慢漂色

    try:
        while True:
            now = time.monotonic()
            if now >= t_end:
                break
            elapsed = now - t0
            # cycle：按 each 秒切方向
            if args.dir == "cycle":
                idx = int(elapsed // each) % len(sequence)
            else:
                idx = 0
            direction = sequence[idx]
            # 单趟相位
            phase = (elapsed / period) % 1.0
            # 色相随时间缓漂，避免单色呆板
            hue = (hue_base + elapsed * 0.04) % 1.0

            levels = blob_levels(n, centers_for(direction, n, phase), width)
            left, right = sample_halves(levels, hue)

            req_id += 1
            udp_rpc(
                sock,
                host,
                "set_segment_rgb",
                [left, right],
                token=token,
                req_id=req_id,
                wait_reply=False,
            )
            frames += 1

            if frames % int(hz * 8) == 0:
                req_id += 1
                udp_rpc(
                    sock,
                    host,
                    "udp_sess_keep_alive",
                    ["keeplive_interval", "10"],
                    token=token,
                    req_id=req_id,
                    wait_reply=False,
                )

            # 换方向时打印一声
            if frames == 1 or (
                args.dir == "cycle"
                and abs((elapsed % each)) < interval * 1.5
                and int(elapsed // each) != int((elapsed - interval) // each)
            ):
                print(f"  → {direction}（{labels[direction]}）")

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已中断")
    finally:
        sock.close()
        try:
            soft = rgb_int(220, 210, 200)
            tcp_rpc(host, "bg_set_rgb", [soft, "smooth", 400])
            tcp_rpc(host, "bg_set_bright", [35, "smooth", 300])
            print("已恢复温和白")
        except Exception as exc:
            print("收尾失败:", exc)

    print(f"共推送 {frames} 帧")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
