from __future__ import annotations

import socket
from dataclasses import dataclass


SSDP_ADDR = ("239.255.255.250", 1982)
SSDP_MSG = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1982\r\n"
    'MAN: "ssdp:discover"\r\n'
    "ST: wifi_bulb\r\n"
    "\r\n"
)


@dataclass
class DiscoveredDevice:
    host: str
    port: int
    model: str | None = None
    name: str | None = None
    fw_ver: str | None = None
    support: str | None = None
    raw: dict[str, str] | None = None


def discover(timeout: float = 2.0, retries: int = 2) -> list[DiscoveredDevice]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    found: dict[str, DiscoveredDevice] = {}
    try:
        import time

        for _ in range(retries):
            sock.sendto(SSDP_MSG.encode("utf-8"), SSDP_ADDR)
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                try:
                    data, _addr = sock.recvfrom(65535)
                except socket.timeout:
                    break
                headers = _parse_ssdp(data.decode("utf-8", errors="replace"))
                loc = headers.get("location") or headers.get("Location")
                if not loc or "yeelight://" not in loc:
                    continue
                host_port = loc.split("yeelight://", 1)[1]
                host, _, port_s = host_port.partition(":")
                port = int(port_s or "55443")
                key = f"{host}:{port}"
                found[key] = DiscoveredDevice(
                    host=host,
                    port=port,
                    model=headers.get("model"),
                    name=headers.get("name"),
                    fw_ver=headers.get("fw_ver"),
                    support=headers.get("support"),
                    raw=headers,
                )
    finally:
        sock.close()
    return list(found.values())


def _parse_ssdp(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip().lower()] = value.strip()
    return out