"""冒烟测试：不需要 mpv 就能验证的部分。

覆盖发现机制、时间同步、纠偏数学这三块——它们是同步播放的核心，
而且全都不依赖 mpv，所以能在 mpv 就位之前先验证掉。

    python tests/smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from common import config, net, protocol, timesync
from student.main import Student

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [ok]   {name}" + (f"  {detail}" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 发现机制


async def test_discovery() -> None:
    print("\n发现机制")
    transport = await net.start_responder()
    try:
        host = await asyncio.to_thread(net.discover, 2.0)
        # 多网卡机器上返回哪个地址都算对——定向广播可能先从真实网卡应答，
        # 也可能从回环。只要拿到一个能连上的本机地址就行。
        check("学生端能通过 UDP 广播找到教师端", host is not None, f"host={host}")
    finally:
        transport.close()

    # 关掉应答服务之后，学生端应该干净地返回 None 而不是抛异常
    host = await asyncio.to_thread(net.discover, 1.0)
    check("教师端不在时返回 None（不抛异常）", host is None, f"host={host}")


# ---------------------------------------------------------------- 时间同步


async def _ping_responder(ws) -> None:
    try:
        async for raw in ws:
            message = protocol.decode(raw)
            if message.get("cmd") == protocol.PING:
                await ws.send(
                    protocol.encode(
                        {
                            "cmd": protocol.PONG,
                            "t0": message.get("t0"),
                            "t_teacher": time.time(),
                        }
                    )
                )
    except Exception:
        pass


async def test_timesync() -> None:
    print("\n时间同步")
    async with serve(_ping_responder, "127.0.0.1", 8799):
        async with connect(f"ws://127.0.0.1:8799", proxy=None) as ws:
            clock = await timesync.sync(ws)

    # 同一台机器上，偏差应该接近 0（RTT 的一半以内）
    check(
        "同机时钟偏差接近 0",
        abs(clock.offset) < 0.05,
        f"offset={clock.offset * 1000:+.2f}ms",
    )
    check("RTT 被记录下来", clock.rtt is not None and clock.rtt >= 0,
          f"rtt={clock.rtt * 1000:.2f}ms" if clock.rtt else "")

    # teacher_now 必须跟着 offset 走，不能直接返回本地时间
    clock.offset = 5.0
    drift = clock.teacher_now() - time.time()
    check("teacher_now 应用了 offset", abs(drift - 5.0) < 0.01, f"偏差 {drift:.3f}s")


# ---------------------------------------------------------------- 纠偏数学


class FakeMPV:
    """替身，只实现纠偏逻辑用到的那几个成员。"""

    def __init__(self, position: float, paused: bool = False, running: bool = True):
        self._position = position
        self._paused = paused
        self._running = running
        self.seeks: list[float] = []

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._paused

    def query_paused(self) -> bool:
        # 纠偏读的是这个：真实现会去问 mpv 要权威值，替身直接给状态。
        return self._paused

    def get_position(self) -> float:
        return self._position

    def seek(self, position: float) -> None:
        self.seeks.append(position)
        self._position = position

    def stop(self) -> None:
        self._running = False


def test_drift_correction() -> None:
    print("\n纠偏数学")

    def heartbeat(position_now: float, teacher_position: float, age: float) -> dict:
        """构造一条 age 秒之前发出的心跳。"""
        return {
            "cmd": protocol.HEARTBEAT,
            "playing": True,
            "position": teacher_position,
            "server_time": time.time() - age,
        }

    # 偏差在阈值内 → 不该动
    student = Student()
    student.mpv = FakeMPV(position=100.2)
    student._on_heartbeat(heartbeat(100.2, 100.2, 0.0))
    check("偏差 0.2s 小于阈值，不 seek", student.mpv.seeks == [], f"seeks={student.mpv.seeks}")

    # 偏差超阈值 → 应该 seek 到正确位置
    student = Student()
    student.mpv = FakeMPV(position=105.0)
    student._on_heartbeat(heartbeat(105.0, 100.0, 0.0))
    check("偏差 5s 超过阈值，seek 到 100", len(student.mpv.seeks) == 1 and abs(student.mpv.seeks[0] - 100.0) < 0.1,
          f"seeks={student.mpv.seeks}")

    # 关键：心跳带上「已经过去的时间」。教师 2 秒前在 100s，现在应该到 102s。
    # 学生停在 100s → 偏差 2s，应该 seek 到 102 而不是 100。
    student = Student()
    student.mpv = FakeMPV(position=100.0)
    student._on_heartbeat(heartbeat(100.0, 100.0, 2.0))
    corrected = student.mpv.seeks[0] if student.mpv.seeks else None
    check("心跳经过时间被算进目标位置（100s+2s→102s）",
          corrected is not None and abs(corrected - 102.0) < 0.2,
          f"seek 到 {corrected}")

    # 学生端已经被暂停 → 不该把它播起来，也不该 seek
    student = Student()
    student.mpv = FakeMPV(position=105.0, paused=True)
    student._on_heartbeat(heartbeat(105.0, 100.0, 0.0))
    check("学生端已暂停时不纠偏", student.mpv.seeks == [], f"seeks={student.mpv.seeks}")

    # 教师端自己暂停了 → 学生端也不该动
    student = Student()
    student.mpv = FakeMPV(position=105.0)
    message = heartbeat(105.0, 100.0, 0.0)
    message["playing"] = False
    student._on_heartbeat(message)
    check("教师端暂停时不纠偏", student.mpv.seeks == [], f"seeks={student.mpv.seeks}")

    # mpv 没在跑（找不到视频的学生机）→ 不该炸
    student = Student()
    student.mpv = FakeMPV(position=105.0, running=False)
    student._on_heartbeat(heartbeat(105.0, 100.0, 0.0))
    check("mpv 未运行时心跳不报错", student.mpv.seeks == [], f"seeks={student.mpv.seeks}")


# ---------------------------------------------------------------- 指令处理


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(protocol.decode(raw))


async def test_handle_video_not_found() -> None:
    print("\n指令处理")
    student = Student()
    student.mpv = FakeMPV(position=0.0)
    ws = FakeWebSocket()

    await student.handle(
        {"cmd": protocol.PLAY, "video": "根本没有这个文件.mkv", "position": 0.0,
         "start_at": time.time() + 1},
        ws,
    )
    reported = [m for m in ws.sent if m.get("cmd") == protocol.VIDEO_NOT_FOUND]
    check("找不到视频时回报 VIDEO_NOT_FOUND", len(reported) == 1, f"sent={ws.sent}")
    check("找不到视频时不崩溃", True)


def test_video_discovery() -> None:
    """学生端得认出 mp4。以前只 glob("*.mkv")，老师能放、学生扫不到，
    全班报「视频不存在」——这是最容易在教室里翻车的一条。"""
    print("\n视频发现")
    import tempfile

    import student.main as student_main

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "lower.mp4").write_bytes(b"x")
        (root / "UPPER.MP4").write_bytes(b"x")   # 大写后缀，Windows 上合法
        (root / "lecture.mkv").write_bytes(b"x")
        (root / "notes.txt").write_bytes(b"x")

        original = student_main.desktop_dir
        student_main.desktop_dir = lambda: root
        try:
            found = Student.find_videos()
        finally:
            student_main.desktop_dir = original

    names = sorted(found)
    check("小写 .mp4 能被扫到", "lower.mp4" in found, f"扫到 {names}")
    check("大写 .MP4 也认，键统一转小写", "upper.mp4" in found, f"扫到 {names}")
    check("mkv 依然能扫到", "lecture.mkv" in found, f"扫到 {names}")
    check("非视频文件不会扫进来", "notes.txt" not in found, f"扫到 {names}")


def test_subtitle_matching() -> None:
    print("\n字幕匹配")
    videos = Student.find_videos()   # 真实桌面，只验不抛异常
    check("扫真实桌面不抛异常", isinstance(videos, dict), f"找到 {len(videos)} 个")

    fake = Path("C:/tmp/movie.mkv")
    check("没有同名 ASS 时返回 None（不抛异常）", Student.find_subtitle(fake) is None)


# ---------------------------------------------------------------- 主入口


async def main() -> None:
    await test_discovery()
    await test_timesync()
    test_drift_correction()
    await test_handle_video_not_found()
    test_video_discovery()
    test_subtitle_matching()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}，失败 {len(FAILED)}")
    for name in FAILED:
        print(f"  失败: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
