"""中途加入测试：学生重开软件 / 自己关掉播放窗口之后，能不能回到老师正在放的视频。

    python tests/test_join.py

真教师端 Server + 真 Student（真 WebSocket），mpv 换成假的。
以前的问题：教师端只在开播那一刻广播一次 PLAY，之后连上的学生机永远不知道老师在放什么，
只会收到心跳，没有 mpv 可纠偏，于是「能扫描到教师机、已连接，但就是不播」。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common import config, protocol
from student import main as student_main
from student import state as st
from student.main import Student
from teacher.main import Server

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  [{'ok' if condition else 'FAIL'}]   {name}" + (f"  {detail}" if detail else ""))


class FakeMPV:
    def __init__(self) -> None:
        self.running = False
        self.paused = True
        self.calls: list[str] = []
        self.start_position: float | None = None

    def start(self, video, subtitle, position=0.0, load_timeout=None, extra_args=None):
        self.calls.append("start")
        self.start_position = position
        self.running, self.paused = True, True

    def play(self): self.calls.append("play"); self.paused = False
    def pause(self): self.calls.append("pause"); self.paused = True
    def seek(self, position): self.calls.append("seek")
    def stop(self): self.calls.append("stop"); self.running = False
    quit = stop
    def query_paused(self): return self.paused
    def get_position(self): return 0.0


async def wait_until(predicate, timeout: float = 10.0, poll: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll)
    return predicate()


async def main() -> int:
    config.PLAY_LEAD = 0.5  # 测试里别等太久
    desktop = Path(tempfile.mkdtemp(prefix="lvs_join_"))
    (desktop / "课.mkv").write_bytes(b"x")
    student_main.desktop_dir = lambda: desktop

    server = Server(lambda message, peer: None)
    server.start()
    server.ready.wait(5)
    await asyncio.sleep(0.2)

    joins: list[str] = []
    original_join = server.join_message
    server.join_message = lambda: (joins.append("j"), original_join())[1]  # type: ignore[method-assign]

    teacher = {"position": 50.0, "playing": True, "t": time.time(), "beats": False}  # 真教师端 mpv 没开时不发心跳

    async def beats() -> None:
        while True:
            if teacher["beats"]:
                now = time.time()
                pos = teacher["position"] + ((now - teacher["t"]) if teacher["playing"] else 0.0)
                server.broadcast({
                    "cmd": protocol.HEARTBEAT, "playing": teacher["playing"],
                    "position": pos, "server_time": now,
                })
            await asyncio.sleep(0.3)

    beat_task = asyncio.create_task(beats())
    tasks: list[asyncio.Task] = []

    def new_student(tag: str) -> Student:
        stu = Student(cache_dir=Path(tempfile.mkdtemp(prefix=f"lvs_join_{tag}_")))
        stu.mpv = FakeMPV()
        tasks.append(asyncio.create_task(stu.run_forever()))
        return stu

    try:
        print("\n教师端没在播：新连上的学生什么都不会发生")
        a = new_student("a")
        await wait_until(lambda: a.state.conn == st.CONNECTED)
        await asyncio.sleep(1.0)
        check("老师没在播：学生没有起播", a.state.play == st.IDLE and a.mpv.calls == [])
        check("老师没在播：不显示「加入播放」", not a.can_join())
        check("连上时问过一次 JOIN", len(joins) >= 1)

        print("\n老师正在播放（第 50 秒），学生中途连上")
        teacher.update(position=50.0, playing=True, t=time.time(), beats=True)
        server.set_now_playing({"video": "课.mkv"})
        await asyncio.sleep(0.5)  # 心跳到位
        b = new_student("b")
        ok = await wait_until(lambda: b.state.play == st.PLAYING, 10)
        check("中途连上的学生自动开始播放", ok, b.state.play)
        check("从老师当前位置开始，而不是从头", b.mpv.start_position is not None and 50.0 < b.mpv.start_position < 60.0,
              str(b.mpv.start_position))
        check("起播（play）被调用", "play" in b.mpv.calls)
        check("播放中不显示「加入播放」", not b.can_join())

        print("\n学生自己关掉播放窗口：显示「加入播放」，点了就回来")
        b.mpv.running = False
        ok = await wait_until(lambda: b.can_join(), 5)
        check("老师在放、本机没在放：可以加入", ok)
        b.mpv.calls.clear()
        b.join()
        ok = await wait_until(lambda: b.mpv.running and b.state.play == st.PLAYING, 10)
        check("点加入后重新开始播放", ok, b.state.play)
        check("位置跟上老师（>50 秒）", b.mpv.start_position is not None and b.mpv.start_position > 50.0,
              str(b.mpv.start_position))

        print("\n本机还在播放时重连：不会重新加载（避免无谓的黑屏）")
        before = len(joins)
        # 模拟断线重连：关掉 websocket，让 Student 自己重连
        await b._ws.close()
        await wait_until(lambda: b.state.conn == st.CONNECTED and b._ws is not None, 10)
        await asyncio.sleep(1.0)
        check("重连后没有发 JOIN", len(joins) == before, f"{before} → {len(joins)}")
        check("重连后没有重新 start", b.mpv.calls.count("start") == 1, str(b.mpv.calls))

        print("\n老师暂停时加入：停在暂停的位置，不开始播")
        teacher.update(position=77.0, playing=False, t=time.time())
        await asyncio.sleep(0.6)
        c = new_student("c")
        ok = await wait_until(lambda: c.state.play == st.PAUSED, 10)
        check("加入后是暂停状态", ok, c.state.play)
        check("位置是老师暂停的位置", c.mpv.start_position is not None and abs(c.mpv.start_position - 77.0) < 0.5,
              str(c.mpv.start_position))
        check("没有调用 play", "play" not in c.mpv.calls, str(c.mpv.calls))

        print("\n老师停止 / 心跳断了：不再回 PLAY")
        server.set_now_playing(None)
        check("老师停止后 join_message 为空", original_join() is None)
        d = new_student("d")
        await wait_until(lambda: d.state.conn == st.CONNECTED)
        await asyncio.sleep(1.0)
        check("老师停止后新连上的学生不会起播", d.state.play == st.IDLE and d.mpv.calls == [])
        server.set_now_playing({"video": "课.mkv"})
        teacher["beats"] = False
        await asyncio.sleep(3.4)
        check("心跳断了超过 3 秒：不回 PLAY", original_join() is None)
        teacher["beats"] = True

        print("\n切片课程的 PLAY 带着 package")
        pkg = {"id": "abc", "title": "课", "http_port": 8767}
        server.set_now_playing({"video": "课.mkv", "package": pkg})
        teacher.update(position=10.0, playing=True, t=time.time())
        await asyncio.sleep(0.6)
        msg = original_join()
        check("回复里有 package 和 PLAY 指令", msg is not None and msg["cmd"] == protocol.PLAY and msg["package"] == pkg,
              str(msg))
        check("回复里的 start_at 在未来、位置约 10 秒",
              msg is not None and msg["start_at"] > time.time() and 10.0 < msg["position"] < 20.0 and msg["paused"] is False,
              str(msg))
    finally:
        beat_task.cancel()
        for t in tasks:
            t.cancel()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}，失败 {len(FAILED)}")
    for name in FAILED:
        print(f"  失败: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.stdout.flush()
    os._exit(code)
