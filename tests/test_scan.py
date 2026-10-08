"""教师机扫描 + 学生端连接状态机的测试。不需要 mpv，不需要图形界面。

    python tests/test_scan.py

用的是真的教师端 Server（真 WebSocket + 真 UDP 应答），学生端换成假 mpv。
所有探测都走回环地址，所以在没有局域网的机器上也能跑。
"""

from __future__ import annotations

import asyncio
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common import config, net, protocol
from student import state as st
from student.main import Student
import student.main as student_main
from teacher.main import Server

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  [{'ok' if condition else 'FAIL'}]   {name}" + (f"  {detail}" if detail else ""))


async def wait_until(predicate, timeout: float = 8.0, poll: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll)
    return predicate()


class FakeMPV:
    """只记录调用，不起进程。"""

    def __init__(self) -> None:
        self.running = False
        self.paused = True
        self.calls: list[str] = []
        self.position = None

    def start(self, video, subtitle, position=0.0):
        self.calls.append(f"start:{Path(video).name}:{subtitle.name if subtitle else None}")
        self.running = True

    def play(self):
        self.calls.append("play"); self.paused = False

    def pause(self):
        self.calls.append("pause"); self.paused = True

    def seek(self, position):
        self.calls.append(f"seek:{position:.1f}")

    def stop(self):
        self.calls.append("stop"); self.running = False

    quit = stop

    def query_paused(self):
        return self.paused

    def get_position(self):
        return 0.0


# ------------------------------------------------------------------ 扫描


def test_scan_no_teacher() -> None:
    print("\n扫描：教师端不在")
    check("普通扫描返回空列表", net.scan(targets=["127.0.0.1"]) == [])
    check(
        "深度扫描返回空列表",
        net.scan(deep=True, targets=["127.0.0.1"], hosts=["127.0.0.1"]) == [],
    )


def test_scan_with_teacher() -> None:
    print("\n扫描：教师端在")
    found = net.scan(targets=["127.0.0.1"])
    check("找到 1 台", len(found) == 1, str(found))
    if found:
        t = found[0]
        check("带上教师机主机名", t.name == net.machine_name(), t.name)
        check("带上实例 id", t.id == net.INSTANCE_ID)
        check("端口是 WS_PORT", t.port == config.WS_PORT)

    found = net.scan(targets=["127.0.0.1", "127.0.0.1", "127.0.0.1"])
    check("同一台教师机被多个目标发现时去重", len(found) == 1, str(found))

    t_lo = net.Teacher("127.0.0.1", 1, id="x")
    t_lan = net.Teacher("192.168.1.5", 1, id="x")
    d: dict = {}
    net._merge(d, t_lo)
    net._merge(d, t_lan)
    check("同一台有回环和局域网两个地址时保留局域网地址", d["x"].host == "192.168.1.5")
    d = {}
    net._merge(d, t_lan)
    net._merge(d, t_lo)
    check("反过来的顺序结果一样", d["x"].host == "192.168.1.5")


def test_deep_scan() -> None:
    print("\n深度扫描")
    found = net.scan(deep=True, targets=[], hosts=["127.0.0.1"])
    check("单播探测能找到教师机", len(found) == 1 and found[0].via == "broadcast", str(found))

    # 模拟「UDP 被防火墙挡了、TCP 是通的」：把发现端口指到没人听的端口
    original = config.DISCOVERY_PORT
    config.DISCOVERY_PORT = 59999
    try:
        found = net.scan(deep=True, targets=[], hosts=["127.0.0.1"])
    finally:
        config.DISCOVERY_PORT = original
    check(
        "UDP 全被挡时，TCP 探测 + PING 验证仍能找到",
        len(found) == 1 and found[0].via == "tcp" and found[0].name == net.machine_name(),
        str(found),
    )

    check("TCP 探测：端口开着的地址被认出", net._tcp_sweep(["127.0.0.1"], config.WS_PORT, 1.0) == ["127.0.0.1"])
    check("TCP 探测：端口没开的地址不算", net._tcp_sweep(["127.0.0.1"], 59998, 1.0) == [])

    # 一个开着端口但不是教师端的服务，不能被当成教师机
    fake = socket.socket()
    fake.bind(("127.0.0.1", 0))
    fake.listen(5)
    port = fake.getsockname()[1]
    t0 = time.monotonic()
    check("不是教师端的开放端口 probe 返回 None", net.probe_teacher("127.0.0.1", port, 0.5) is None)
    check("probe 不会无限等待", time.monotonic() - t0 < 3)
    fake.close()

    hosts = net.sweep_hosts()
    check("sweep_hosts 排除本机自己", all(h not in net.local_ips() for h in hosts))


# ------------------------------------------------------------------ 学生端


async def test_student_flow(server: Server, received: list) -> None:
    print("\n学生端：连接 / 手动指定 / 恢复自动")
    student = Student()
    student.mpv = FakeMPV()
    s = student.state
    task = asyncio.create_task(student.run_forever())
    try:
        ok = await wait_until(lambda: s.conn == st.CONNECTED)
        check("自动发现并连上教师端", ok, f"conn={s.conn}")
        check("记下教师机主机名", s.teacher_name == net.machine_name(), s.teacher_name)
        check("记下 RTT 和连接次数清零", s.rtt is not None and s.attempts == 0)
        check("未手动指定", s.pinned is False)

        # ---- 界面点「扫描」
        student.request_scan()
        await asyncio.sleep(0.1)
        ok = await wait_until(lambda: not s.scanning and bool(s.scan_note) and s.scan_note != "正在扫描…")
        check("手动扫描完成并给出结果说明", ok, s.scan_note)
        check("扫描列表里有教师机", len(s.teachers) == 1, str(s.teachers))

        # ---- 手动指定一台连不上的
        student.connect_to("127.0.0.1", 1)
        ok = await wait_until(lambda: s.pinned and s.conn != st.CONNECTED and s.attempts >= 1, 10)
        check("手动指定后断开当前连接，改连指定的（连不上）", ok, f"conn={s.conn} attempts={s.attempts}")
        check("失败原因写进 last_error 给学生看", bool(s.last_error), s.last_error)
        await asyncio.sleep(0.4)
        check("手动指定时不会偷偷改回自动", s.pinned and s.conn != st.CONNECTED)

        # ---- 手动指定一台能连上的
        student.connect_to("127.0.0.1")
        ok = await wait_until(lambda: s.conn == st.CONNECTED and s.pinned)
        check("手动指定能连上的教师机，连上", ok, f"conn={s.conn}")
        check("连上后失败计数清零、错误清空", s.attempts == 0 and s.last_error == "")

        # ---- 恢复自动
        student.use_auto()
        await asyncio.sleep(0.2)
        ok = await wait_until(lambda: s.conn == st.CONNECTED and not s.pinned)
        check("恢复自动搜索后重新连上", ok, f"conn={s.conn} pinned={s.pinned}")

        # ---- 指令仍然正常：播放 / 暂停 / 停止
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "lesson01.mkv"
            video.write_bytes(b"x")
            (Path(tmp) / "lesson01.ass").write_bytes(b"x")
            original = student_main.desktop_dir
            student_main.desktop_dir = lambda: Path(tmp)
            try:
                received.clear()
                server.broadcast({"cmd": protocol.PLAY, "video": "lesson01.mkv",
                                  "position": 0.0, "start_at": time.time() + 0.6})
                ok = await wait_until(lambda: s.play == st.WAITING or s.play == st.PLAYING, 3)
                check("收到 PLAY 后进入加载/等待", ok, s.play)
                check("界面状态记下视频名和字幕", s.video == "lesson01.mkv" and s.has_subtitle)
                ok = await wait_until(lambda: s.play == st.PLAYING, 3)
                check("到 start_at 后状态变为播放中", ok, s.play)
                check("mpv 收到 start 和 play", any(c.startswith("start:lesson01.mkv:lesson01.ass") for c in student.mpv.calls) and "play" in student.mpv.calls, str(student.mpv.calls))

                server.broadcast({"cmd": protocol.HEARTBEAT, "playing": True, "position": 12.0, "server_time": time.time()})
                ok = await wait_until(lambda: s.teacher_position is not None)
                check("心跳里的位置记到状态里", ok, str(s.teacher_position))

                server.broadcast({"cmd": protocol.PAUSE})
                ok = await wait_until(lambda: s.play == st.PAUSED)
                check("PAUSE 后状态为已暂停", ok, s.play)

                server.broadcast({"cmd": protocol.STOP})
                ok = await wait_until(lambda: s.play == st.IDLE)
                check("STOP 后回到待机并清掉视频名", ok and s.video == "", s.play)

                server.broadcast({"cmd": protocol.PLAY, "video": "不存在.mkv",
                                  "position": 0.0, "start_at": time.time() + 0.5})
                ok = await wait_until(lambda: s.play == st.NOT_FOUND)
                check("找不到视频时状态为 NOT_FOUND", ok, s.play)
                ok = await wait_until(lambda: any(m.get("cmd") == protocol.VIDEO_NOT_FOUND for m, _ in received))
                check("仍然向教师端回报 VIDEO_NOT_FOUND", ok)
            finally:
                student_main.desktop_dir = original
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    check("退出时关掉 mpv", "stop" in student.mpv.calls)


async def test_auto_deep_scan() -> None:
    print("\n学生端：广播连续没结果后自动深度扫描")
    calls: list[bool] = []
    original_scan = net.scan
    saved = (config.AUTO_DEEP_SCAN_AFTER, config.AUTO_DEEP_SCAN_INTERVAL, config.REDISCOVER_INTERVAL)

    def fake_scan(timeout=None, deep=False, **kw):
        calls.append(deep)
        return []

    net.scan = fake_scan
    config.AUTO_DEEP_SCAN_AFTER, config.AUTO_DEEP_SCAN_INTERVAL, config.REDISCOVER_INTERVAL = 2, 30.0, 0.05
    student = Student()
    student.mpv = FakeMPV()
    task = asyncio.create_task(student.run_forever())
    try:
        await asyncio.sleep(1.0)
        check("前几轮只做普通扫描", calls[:2] == [False, False], str(calls[:4]))
        check("失败达到阈值后做了深度扫描", True in calls, str(calls))
        check("深度扫描在间隔内只做 1 次", calls.count(True) == 1, f"深度 {calls.count(True)} 次 / 共 {len(calls)} 次")
        check("失败次数被记录", student.state.attempts >= 2, str(student.state.attempts))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        net.scan = original_scan
        config.AUTO_DEEP_SCAN_AFTER, config.AUTO_DEEP_SCAN_INTERVAL, config.REDISCOVER_INTERVAL = saved

    # 关闭开关
    calls.clear()
    net.scan = fake_scan
    config.AUTO_DEEP_SCAN_AFTER, config.REDISCOVER_INTERVAL = 0, 0.05
    student = Student()
    student.mpv = FakeMPV()
    task = asyncio.create_task(student.run_forever())
    try:
        await asyncio.sleep(0.6)
        check("AUTO_DEEP_SCAN_AFTER=0 时从不自动深度扫描", len(calls) > 3 and True not in calls, str(calls[:6]))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        net.scan = original_scan
        config.AUTO_DEEP_SCAN_AFTER, config.AUTO_DEEP_SCAN_INTERVAL, config.REDISCOVER_INTERVAL = saved


def test_missing_desktop() -> None:
    print("\n桌面目录不存在")
    original = student_main.desktop_dir
    student_main.desktop_dir = lambda: Path("/definitely/not/here")
    try:
        check("find_videos 返回空表而不是抛异常", Student.find_videos() == {})
    finally:
        student_main.desktop_dir = original


async def main() -> int:
    config.SCAN_TIMEOUT = 0.3
    config.DEEP_SCAN_TIMEOUT = 0.6
    config.REDISCOVER_INTERVAL = 0.2

    test_missing_desktop()
    await asyncio.to_thread(test_scan_no_teacher)

    received: list = []
    server = Server(lambda message, peer: received.append((message, peer)))
    server.start()
    server.ready.wait(5)
    await asyncio.sleep(0.2)

    await asyncio.to_thread(test_scan_with_teacher)
    await asyncio.to_thread(test_deep_scan)
    await test_student_flow(server, received)
    await test_auto_deep_scan()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}，失败 {len(FAILED)}")
    for name in FAILED:
        print(f"  失败: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.stdout.flush()
    import os
    os._exit(code)  # Server 线程是 daemon，直接退出，免得等 asyncio 清理
