"""PING/PONG 时间同步。

为什么必须做：教师端广播的 start_at 是**教师机的时刻**。如果学生机的系统
时钟比教师机慢 3 秒（机房机器没连域控、从不校时，很常见），学生端会算出
「还要等 3 秒」或者「已经迟了 3 秒」，同步直接崩掉。

和 NTP 一个思路，只看单向延迟：

    学生发 PING  t0
    教师收到，立刻回 PONG 带上教师当前时刻 t_teacher
    学生收到  t1

    往返延迟  RTT    = t1 - t0
    时钟偏差  offset = t_teacher - (t0 + t1) / 2

之后学生端算「教师此刻」= 本地 time.time() + offset。

打多个样本取 RTT 最小的那个：RTT 最小说明这一趟最没有排队和抖动，
延迟最对称，(t0+t1)/2 这个中点估计也就最准。
"""

from __future__ import annotations

import asyncio
import time

from . import protocol

SAMPLES = 5


class ClockSync:
    """学生端本地时钟与教师端时钟的换算。"""

    def __init__(self, offset: float = 0.0, rtt: float | None = None):
        self.offset = offset
        self.rtt = rtt

    def teacher_now(self) -> float:
        """教师端此刻的时刻。所有 start_at 判断都要用它，别用 time.time()。"""
        return time.time() + self.offset


async def sync(ws, samples: int = SAMPLES, timeout: float = 2.0) -> ClockSync:
    """连上教师端之后、进入正式收指令循环之前调用。

    这里直接 ws.recv() 而不用主循环，是有意的：同步阶段消息是严格一问一答的，
    串行处理最简单，也不会和主循环抢消息。
    """
    best_rtt: float | None = None
    best_offset = 0.0

    for _ in range(samples):
        t0 = time.time()
        await ws.send(protocol.encode({"cmd": protocol.PING, "t0": t0}))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            continue

        message = protocol.decode(raw)
        t1 = time.time()
        if message.get("cmd") != protocol.PONG:
            continue  # 同步期间飘进来的 HEARTBEAT 之类，跳过

        rtt = t1 - t0
        offset = float(message["t_teacher"]) - (t0 + t1) / 2.0
        if best_rtt is None or rtt < best_rtt:
            best_rtt, best_offset = rtt, offset

    if best_rtt is None:
        raise RuntimeError("时间同步失败：教师端没有回 PONG")

    return ClockSync(offset=best_offset, rtt=best_rtt)
