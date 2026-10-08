"""UDP 广播发现：学生端喊一声，教师端答应一下。

为什么不扫端口：V1 是遍历 1~254 逐个 connect_ex，超时 0.08s，
最坏情况要 20 秒才找到教师端。机房场景不可接受。

为什么学生端用回复包的源地址而不是回复内容里的 IP：教师机可能有多张网卡
（有线 + 无线 + 虚拟网卡），让回复自己告诉学生该走哪条路，比教师端猜自己
的 IP 可靠。
"""

from __future__ import annotations

import asyncio
import json
import select
import socket
import time

from . import config


def local_ip() -> str | None:
    """本机在局域网里的 IP。用 UDP connect 探测，不会真的发包。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def broadcast_targets() -> list[str]:
    """广播地址候选。

    本网段定向广播排第一：多网卡的机器上它最明确，一眼看出该走哪块网卡。
    255.255.255.255 兜底（有些交换机只认它），127.0.0.1 是为了让
    「同一台机器开 1 个教师 + 3 个学生」的开发测试能跑通。
    """
    targets: list[str] = []
    ip = local_ip()
    if ip:
        parts = ip.split(".")
        if len(parts) == 4:
            targets.append(".".join(parts[:3] + ["255"]))
    targets.extend(["255.255.255.255", "127.0.0.1"])
    return targets


def discover(timeout: float = 1.5) -> str | None:
    """广播找教师端，返回教师 IP；没人应就返回 None。

    阻塞函数，学生端用 asyncio.to_thread 调它。

    实现上有两个坑值得说明：

    1. **每个广播目标必须用独立 socket。** 用同一个 socket 连发多个目标时，
       Windows 会在第一次 sendto 就把 socket 隐式绑定到那块网卡的地址。
       多网卡的机器（有线 + 无线 + VMware 虚拟网卡很常见）上，后续探测包
       会从错误的网卡发出去，教师端的应答根本回不来。

    2. **并发发出、等最先回来的那个。** 串行试的话，走错网卡的那次要白等到
       超时才轮到下一个。三个目标串行最坏 4.5 秒，机房体验很糟。
    """
    payload = config.DISCOVERY_MAGIC.encode("utf-8")
    sockets: list[socket.socket] = []

    try:
        for target in broadcast_targets():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.bind(("", 0))  # 显式绑定，别让首次 sendto 决定源地址
                sock.setblocking(False)
                sock.sendto(payload, (target, config.DISCOVERY_PORT))
            except OSError:
                # 这个广播地址发不出去（比如没有对应网卡），跳过它
                sock.close()
                continue
            sockets.append(sock)

        deadline = time.monotonic() + timeout
        while sockets:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            readable, _, _ = select.select(sockets, [], [], remaining)
            for sock in readable:
                try:
                    data, peer = sock.recvfrom(2048)
                except OSError:
                    sockets.remove(sock)
                    continue
                try:
                    reply = json.loads(data)
                except ValueError:
                    continue  # 别的程序占用同端口的杂包，忽略
                if isinstance(reply, dict) and "port" in reply:
                    return peer[0]
        return None
    finally:
        for sock in sockets:
            sock.close()


class DiscoveryResponder(asyncio.DatagramProtocol):
    """教师端这边：收到探测包就原地回一个同样的魔数。"""

    def connection_made(self, transport) -> None:
        # asyncio 不会自动挂 transport，得自己存下来才发得出回复
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if data.strip() == config.DISCOVERY_MAGIC.encode("utf-8"):
            try:
                self.transport.sendto(
                    json.dumps({"port": config.WS_PORT}).encode("utf-8"), addr
                )
            except OSError:
                pass


async def start_responder():
    """在教师端的事件循环里起 UDP 应答服务，返回 transport。"""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        DiscoveryResponder,
        local_addr=("0.0.0.0", config.DISCOVERY_PORT),
        allow_broadcast=True,
    )
    return transport
