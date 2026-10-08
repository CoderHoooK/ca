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
import selectors
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import config, protocol

# 本进程（教师端）的身份。学生端扫描时靠它去重：同一台教师机会从多个地址
# 应答（局域网 IP、127.0.0.1），不去重的话列表里会出现两个「同一台」。
INSTANCE_ID = uuid.uuid4().hex[:8]


def machine_name() -> str:
    """教师机的主机名，扫描列表里用它区分是哪台教师机。"""
    try:
        return socket.gethostname()
    except OSError:
        return ""


@dataclass(frozen=True)
class Teacher:
    """扫描到的一台教师机。"""

    host: str
    port: int
    name: str = ""
    id: str = ""
    via: str = "broadcast"  # broadcast 广播 / sweep 逐个地址探测 / tcp 端口探测

    @property
    def label(self) -> str:
        return f"{self.name or '教师机'}  ({self.host})"


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


def local_ips() -> list[str]:
    """本机所有可能在局域网里的 IPv4（排除回环和 169.254 自动私有地址）。

    主出口 IP 排第一。多网卡机器（有线 + 无线 + 虚拟网卡）上要把每块网卡所在
    的网段都扫到，教师机不一定和默认路由在同一个网段。
    """
    ips: list[str] = []
    primary = local_ip()
    if primary:
        ips.append(primary)
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith(("127.", "169.254."))]


def sweep_hosts(max_subnets: int = 4) -> list[str]:
    """深度扫描要探测的地址：本机每块网卡所在 /24 网段里除自己以外的 254 个地址。

    只按 /24 扫。机房网段几乎都是 /24；真是 /16 的话扫 6 万多个地址太重，
    宁可扫不到，由学生用「手动输入 IP」兜底。
    """
    hosts: list[str] = []
    seen_subnets: set[str] = set()
    for ip in local_ips():
        prefix = ip.rsplit(".", 1)[0]
        if prefix in seen_subnets:
            continue
        seen_subnets.add(prefix)
        if len(seen_subnets) > max_subnets:
            break
        hosts.extend(f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != ip)
    return hosts


def _parse_reply(data: bytes, peer) -> Teacher | None:
    try:
        reply = json.loads(data)
    except ValueError:
        return None  # 别的程序占用同端口的杂包，忽略
    if not isinstance(reply, dict) or "port" not in reply:
        return None
    try:
        port = int(reply["port"])
    except (TypeError, ValueError):
        return None
    return Teacher(
        host=peer[0],
        port=port,
        name=str(reply.get("name", "")),
        id=str(reply.get("id", "")),
    )


def _tcp_sweep(hosts: list[str], port: int, timeout: float) -> list[str]:
    """并发探测哪些地址开着 port。返回连得上的地址。

    为什么还要有这一步：UDP 发现包可能被挡（教师机防火墙只放行了 TCP，或者
    交换机丢 UDP），但 WebSocket 的 TCP 端口是通的。这两条路互为备份。

    用 selectors 而不是 select.select：Linux 上 select 不能处理编号 >= 1024 的
    文件描述符，GUI 进程里句柄不少，254 个并发连接可能越界。
    """
    sel = selectors.DefaultSelector()
    socks: dict[socket.socket, str] = {}
    for host in hosts:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            sock.connect_ex((host, port))
            sel.register(sock, selectors.EVENT_WRITE)
        except (OSError, ValueError):
            sock.close()
            continue
        socks[sock] = host

    alive: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        pending = len(socks)
        while pending > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            events = sel.select(remaining)
            if not events:
                break
            for key, _ in events:
                sock = key.fileobj
                sel.unregister(sock)
                pending -= 1
                if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    alive.append(socks[sock])
    finally:
        sel.close()
        for sock in socks:
            sock.close()
    return alive


def probe_teacher(host: str, port: int, timeout: float = 1.0) -> Teacher | None:
    """向 host:port 发一次 WebSocket 握手 + PING，确认它真的是教师端。

    用来验证 TCP 探测的结果：8765 端口开着不代表是我们的教师端。
    副作用：教师端会短暂地看到「学生端接入/断开」，不影响计数。
    """
    from websockets.sync.client import connect

    try:
        with connect(f"ws://{host}:{port}", open_timeout=timeout, proxy=None) as ws:
            ws.send(protocol.encode({"cmd": protocol.PING, "t0": time.time()}))
            message = protocol.decode(ws.recv(timeout=timeout))
    except Exception:
        return None
    if message.get("cmd") != protocol.PONG:
        return None
    return Teacher(
        host=host,
        port=port,
        name=str(message.get("name", "")),
        id=str(message.get("id", "")),
        via="tcp",
    )


def _is_loopback(host: str) -> bool:
    return host.startswith("127.")


def _merge(found: dict[str, Teacher], teacher: Teacher) -> None:
    """按教师机身份去重。同一台机器有多个地址时，保留真实网卡地址而不是回环。"""
    key = teacher.id or teacher.host
    old = found.get(key)
    if old is None or (_is_loopback(old.host) and not _is_loopback(teacher.host)):
        found[key] = teacher


def scan(
    timeout: float | None = None,
    deep: bool = False,
    *,
    targets: list[str] | None = None,
    hosts: list[str] | None = None,
) -> list[Teacher]:
    """找出局域网里**所有**教师机（不像 discover 只要第一个）。

    普通扫描（deep=False）：只发 UDP 广播，和 discover 一样快，用来平时自动找。

    深度扫描（deep=True）：在广播之外再做两件事，应对广播被拦的网络：
        1. 给本网段每个地址单播一个 UDP 探测包（很多交换机/AP 丢广播但放单播）
        2. 并发探测每个地址的 WebSocket 端口，再用 PING 验证（UDP 被挡时的备份）

    targets / hosts 是给测试用的覆盖项（指定广播目标 / 要探测的地址）。

    阻塞函数，调用方用 asyncio.to_thread 或后台线程。
    """
    if timeout is None:
        timeout = config.DEEP_SCAN_TIMEOUT if deep else config.SCAN_TIMEOUT
    payload = config.DISCOVERY_MAGIC.encode("utf-8")
    found: dict[str, Teacher] = {}
    sockets: list[socket.socket] = []

    def open_probe_socket() -> socket.socket | None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", 0))  # 显式绑定，理由见 discover 的说明
            sock.setblocking(False)
        except OSError:
            sock.close()
            return None
        return sock

    tcp_alive: list[str] = []
    tcp_thread: threading.Thread | None = None

    try:
        # 广播：每个目标一个独立 socket（理由见 discover）
        for target in targets if targets is not None else broadcast_targets():
            sock = open_probe_socket()
            if sock is None:
                continue
            try:
                sock.sendto(payload, (target, config.DISCOVERY_PORT))
            except OSError:
                sock.close()
                continue
            sockets.append(sock)

        if deep:
            sweep = hosts if hosts is not None else sweep_hosts()

            # 单播：一个 socket 发给所有地址就行，单播按目的地址选路，
            # 不存在广播那种被隐式绑到错误网卡的问题
            sock = open_probe_socket()
            if sock is not None:
                sockets.append(sock)
                for host in sweep:
                    try:
                        sock.sendto(payload, (host, config.DISCOVERY_PORT))
                    except OSError:
                        continue  # 缓冲区满或路由不可达，跳过这个地址

            # TCP 端口探测放在另一个线程里和 UDP 收包并行，省一轮等待
            def run_tcp() -> None:
                tcp_alive.extend(_tcp_sweep(sweep, config.WS_PORT, timeout))

            tcp_thread = threading.Thread(target=run_tcp, daemon=True)
            tcp_thread.start()

        deadline = time.monotonic() + timeout
        while sockets:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select(sockets, [], [], remaining)
            for sock in readable:
                try:
                    data, peer = sock.recvfrom(2048)
                except OSError:
                    sockets.remove(sock)
                    continue
                teacher = _parse_reply(data, peer)
                if teacher is not None:
                    _merge(found, teacher)
    finally:
        for sock in sockets:
            sock.close()

    if tcp_thread is not None:
        tcp_thread.join(timeout + 1.0)
        # UDP 已经找到的地址不用再验证
        known_hosts = {t.host for t in found.values()}
        extra = [h for h in tcp_alive if h not in known_hosts]
        if extra:
            with ThreadPoolExecutor(max_workers=min(8, len(extra))) as pool:
                for teacher in pool.map(
                    lambda h: probe_teacher(h, config.WS_PORT), extra
                ):
                    if teacher is not None:
                        _merge(found, teacher)

    return list(found.values())


class DiscoveryResponder(asyncio.DatagramProtocol):
    """教师端这边：收到探测包就原地回一个同样的魔数。"""

    def connection_made(self, transport) -> None:
        # asyncio 不会自动挂 transport，得自己存下来才发得出回复
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if data.strip() == config.DISCOVERY_MAGIC.encode("utf-8"):
            try:
                reply = {
                    "app": "LanVideoSync",
                    "port": config.WS_PORT,
                    "name": machine_name(),
                    "id": INSTANCE_ID,
                }
                self.transport.sendto(json.dumps(reply).encode("utf-8"), addr)
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
