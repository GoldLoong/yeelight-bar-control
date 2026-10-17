#!/usr/bin/env python3
"""
快速验证 lamp15 的：
  - TCP  set_segment_rgb（左右分段）
  - UDP  udp_chroma_sess_new（Chroma 会话）
  - 对比 UDP udp_sess_new（现有通用会话）

用法：
  python3 scripts/demo-chroma-segment.py
  python3 scripts/demo-chroma-segment.py 192.168.0.116
  YEELIGHT_HOST=192.168.0.116 python3 scripts/demo-chroma-segment.py

安全：演示结束会把氛围灯设回温和白，不关灯。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

TCP_PORT = 55443
UDP_PORT = 55444


def rgb_int(r: int, g: int, b: int) -> int:
    r = max(1, min(255, int(r)))  # 避免 0x000000 未定义行为
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    if g == 0 and b == 0 and r == 0:
        r = 1
    return (r << 16) | (g << 8) | b


def resolve_host() -> str:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
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
    raise SystemExit("未找到灯 IP。用法: python3 scripts/demo-chroma-segment.py <host>")


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
    text = data.decode("utf-8", errors="replace").strip().split("\r\n")[0]
    return json.loads(text)


def udp_rpc(
    sock: socket.socket,
    host: str,
    method: str,
    params: list,
    *,
    token: str | None = None,
    req_id: int = 1,
    wait_reply: bool = True,
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


def extract_token(resp: dict | None) -> str | None:
    if not resp:
        return None
    params = resp.get("params")
    if isinstance(params, dict) and params.get("token"):
        return str(params["token"])
    result = resp.get("result")
    if isinstance(result, dict) and result.get("token"):
        return str(result["token"])
    return None


def section(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def main() -> int:
    host = resolve_host()
    print(f"目标灯: {host}")

    # —— 1) 探测能力 ——
    section("1) TCP 探测 model / support")
    try:
        # discovery-style via get_prop; support 来自 SSDP，这里再确认关键
        props = tcp_rpc(host, "get_prop", ["power", "main_power", "bg_power", "bg_bright", "bg_rgb"])
        print("get_prop:", json.dumps(props, ensure_ascii=False))
    except Exception as exc:
        print("TCP 连接失败:", exc)
        return 1

    # SSDP 拿 support
    support = ""
    model = ""
    try:
        disc = b'M-SEARCH * HTTP/1.1\r\nMAN: "ssdp:discover"\r\nST: wifi_bulb\r\n'
        ds = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ds.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        ds.settimeout(2.0)
        for dest in ((host, 1982), ("239.255.255.250", 1982), ("255.255.255.255", 1982)):
            try:
                ds.sendto(disc, dest)
            except OSError:
                pass
        t0 = time.time()
        while time.time() - t0 < 2.5:
            try:
                data, addr = ds.recvfrom(65535)
            except socket.timeout:
                continue
            if addr[0] != host:
                continue
            txt = data.decode("utf-8", errors="replace")
            for line in txt.splitlines():
                if line.lower().startswith("model:"):
                    model = line.split(":", 1)[1].strip()
                if line.lower().startswith("support:"):
                    support = line.split(":", 1)[1].strip()
            break
        ds.close()
    except Exception as exc:
        print("SSDP 跳过:", exc)

    print(f"model: {model or '?'}")
    methods = set(support.split()) if support else set()
    for name in ("set_segment_rgb", "udp_chroma_sess_new", "udp_sess_new", "bg_set_rgb"):
        mark = "YES" if (not methods or name in methods) else "NO "
        print(f"  [{mark}] {name}")
    if methods and "set_segment_rgb" not in methods:
        print("警告: SSDP support 未声明 set_segment_rgb，仍尝试调用。")

    # 确保氛围灯开着
    try:
        tcp_rpc(host, "bg_set_power", ["on", "sudden", 0])
        tcp_rpc(host, "bg_set_bright", [40, "sudden", 0])
    except Exception as exc:
        print("开氛围灯失败:", exc)

    # —— 2) TCP set_segment_rgb ——
    section("2) TCP set_segment_rgb（左红 / 右蓝，再对调）")
    left_red = rgb_int(255, 0, 0)
    right_blue = rgb_int(0, 80, 255)
    try:
        r1 = tcp_rpc(host, "set_segment_rgb", [left_red, right_blue])
        print("左红右蓝 =>", r1)
        print("请看灯背面两侧是否明显不同色… 停 2.5s")
        time.sleep(2.5)
        r2 = tcp_rpc(host, "set_segment_rgb", [right_blue, left_red])
        print("左蓝右红 =>", r2)
        print("请再确认左右是否对调… 停 2.5s")
        time.sleep(2.5)
        tcp_ok = "error" not in r1 and "error" not in r2
    except Exception as exc:
        print("TCP set_segment_rgb 失败:", exc)
        tcp_ok = False

    # —— 3) UDP udp_chroma_sess_new ——
    section("3) UDP udp_chroma_sess_new 握手 + 推色")
    chroma_ok = False
    chroma_token = None
    chroma_segment_ok = False
    try:
        us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        us.settimeout(2.5)
        resp = udp_rpc(us, host, "udp_chroma_sess_new", [])
        print("handshake:", json.dumps(resp, ensure_ascii=False))
        chroma_token = extract_token(resp)
        if not chroma_token and resp and resp.get("method") == "udp_sess_token":
            chroma_token = extract_token(resp)
        print("token:", chroma_token)
        if not chroma_token:
            raise RuntimeError("未拿到 chroma token")

        # 尝试在 chroma 会话里推分段
        colors = [
            (rgb_int(255, 40, 0), rgb_int(0, 120, 255)),
            (rgb_int(0, 220, 80), rgb_int(220, 0, 180)),
            (rgb_int(255, 200, 0), rgb_int(80, 0, 255)),
        ]
        print("尝试 UDP set_segment_rgb × 3 …")
        for i, (L, R) in enumerate(colors, start=1):
            udp_rpc(
                us,
                host,
                "set_segment_rgb",
                [L, R],
                token=chroma_token,
                req_id=10 + i,
                wait_reply=False,
            )
            time.sleep(0.7)
        chroma_segment_ok = True
        print("已发送（无应答属正常，请肉眼看灯是否跟着变）")

        # 再试 bg_set_rgb（若分段无效，至少验证会话能推色）
        print("再试 UDP bg_set_rgb（整条同色紫）…")
        udp_rpc(
            us,
            host,
            "bg_set_rgb",
            [rgb_int(160, 0, 255), "sudden", 0],
            token=chroma_token,
            req_id=20,
            wait_reply=False,
        )
        time.sleep(1.2)

        # keepalive 形态（有的固件要）
        udp_rpc(
            us,
            host,
            "udp_sess_keep_alive",
            ["keeplive_interval", "10"],
            token=chroma_token,
            req_id=21,
            wait_reply=False,
        )
        chroma_ok = True
        us.close()
    except Exception as exc:
        print("udp_chroma_sess_new 失败:", exc)
        try:
            us.close()
        except Exception:
            pass

    # —— 4) 对比普通 udp_sess_new ——
    section("4) 对比：UDP udp_sess_new + set_segment_rgb / bg_set_rgb")
    normal_ok = False
    normal_segment_ok = False
    try:
        us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        us.settimeout(2.5)
        resp = udp_rpc(us, host, "udp_sess_new", [])
        print("handshake:", json.dumps(resp, ensure_ascii=False))
        token = extract_token(resp)
        print("token:", token)
        if not token:
            raise RuntimeError("未拿到 udp token")

        print("UDP set_segment_rgb 左青右橙…")
        udp_rpc(
            us,
            host,
            "set_segment_rgb",
            [rgb_int(0, 220, 200), rgb_int(255, 120, 0)],
            token=token,
            req_id=30,
            wait_reply=False,
        )
        time.sleep(1.5)
        normal_segment_ok = True

        print("UDP bg_set_rgb 整条绿（会清掉分段状态）…")
        udp_rpc(
            us,
            host,
            "bg_set_rgb",
            [rgb_int(0, 200, 60), "sudden", 0],
            token=token,
            req_id=31,
            wait_reply=False,
        )
        time.sleep(1.0)
        normal_ok = True
        us.close()
    except Exception as exc:
        print("udp_sess_new 失败:", exc)
        try:
            us.close()
        except Exception:
            pass

    # —— 5) 收尾 ——
    section("5) 收尾：氛围灯温和白")
    try:
        # bg_set_rgb 会重置分段；演示结束统一成整条柔和色
        tcp_rpc(host, "bg_set_rgb", [rgb_int(220, 210, 200), "smooth", 400])
        tcp_rpc(host, "bg_set_bright", [35, "smooth", 300])
        print("已恢复。")
    except Exception as exc:
        print("收尾失败:", exc)

    section("结果摘要")
    print(f"  TCP set_segment_rgb     : {'OK' if tcp_ok else 'FAIL'}")
    print(f"  UDP chroma 握手         : {'OK' if chroma_ok else 'FAIL'}")
    print(f"  UDP chroma 推分段(已发) : {'OK' if chroma_segment_ok else 'FAIL/SKIP'}")
    print(f"  UDP 普通会话握手        : {'OK' if normal_ok else 'FAIL'}")
    print(f"  UDP 普通会话推分段(已发): {'OK' if normal_segment_ok else 'FAIL/SKIP'}")
    print()
    print("请以肉眼为准：")
    print("  - 步骤 2 左右明显不同色 → 分段协议可用")
    print("  - 步骤 3/4 颜色跟着变 → 对应 UDP 会话可推色")
    print("  - 若握手 OK 但灯不变 → 会话通了但该 method 在 UDP 上无效")
    return 0 if tcp_ok or chroma_ok or normal_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
