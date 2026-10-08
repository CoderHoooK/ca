"""针对 mpv IPC 封装的测试，跑在假 mpv 上。

    python tests/test_mpv_ipc.py

真 mpv 到位后还要跑 tests/live_mpv.py 做真实端到端验证。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from common import mpvctl
from common.mpvctl import MPV
from fake_mpv import FakeMPV

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [ok]   {name}" + (f"  {detail}" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}  {detail}")


class FakeProc:
    """冒充 subprocess.Popen 的返回值。"""

    def __init__(self, *args, **kwargs):
        self.returncode = None
        self.pid = 999999

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


def main() -> int:
    print("mpv IPC 封装（跑在假 mpv 上）")

    # 拦掉真正的进程启动——我们要连的是假 mpv 那个管道
    real_popen = subprocess.Popen
    subprocess.Popen = FakeProc
    mpvctl.find_mpv = lambda: Path("C:/fake/mpv.exe")

    try:
        client = MPV("test")

        # 假 mpv 必须监听客户端将要连的那个管道名，所以先构造客户端拿到名字
        server = FakeMPV(client._pipe)
        server.start()

        try:
            client.start(Path("C:/fake/movie.mkv"), None, 0.0)

            check("管道连上了", client.running)

            # 订阅命令发出去没有
            observed = [c for c in server.commands if c[0] == "observe_property"]
            names = {c[2] for c in observed}
            check("订阅了 time-pos / pause / duration",
                  names == {"time-pos", "pause", "duration"}, f"订阅={sorted(names)}")

            # 假 mpv 连上时推了 time-pos=42.0 / pause=false，
            # 客户端应该已经通过 property-change 把缓存填上了
            deadline = time.monotonic() + 2.0
            while client.position != 42.0 and time.monotonic() < deadline:
                time.sleep(0.02)
            check("property-change 事件更新了位置缓存",
                  client.position == 42.0, f"position={client.position}")

            # start() 的约定是返回即就绪，其中一条就是「已暂停」。
            # 假 mpv 连上时推的是 pause=False，所以这里必须是 start() 末尾
            # 那次显式 pause() 把它压回了 True。
            check("start() 之后处于暂停态", client.paused is True,
                  f"paused={client.paused}")

            # 验证「缓存只靠 property-change 推送也能更新」。
            # 先手工把缓存改成 False（假装在播），再用 command() 直接发
            # set_property —— 这一步**不会**动缓存，之后缓存变回 True
            # 就只可能是假 mpv 推回来的事件起了作用。
            client._paused = False
            client.command("set_property", "pause", True)
            deadline = time.monotonic() + 2.0
            while client.paused is not True and time.monotonic() < deadline:
                time.sleep(0.02)
            check("property-change 事件能更新暂停状态",
                  client.paused is True, f"paused={client.paused}")

            # get_property 往返
            server.position = 123.5
            got = client.get_position()
            check("get_property 往返拿到正确位置",
                  got == 123.5, f"拿到 {got}")

            # 暂停 → 假 mpv 会推 property-change 回来
            client.pause()
            deadline = time.monotonic() + 2.0
            while not client.paused and time.monotonic() < deadline:
                time.sleep(0.02)
            check("pause() 生效且状态被推回", client.paused is True)

            client.play()
            deadline = time.monotonic() + 2.0
            while client.paused and time.monotonic() < deadline:
                time.sleep(0.02)
            check("play() 生效且状态被推回", client.paused is False)

            # seek
            client.seek(300.0)
            check("seek() 之后缓存立刻更新（不等 mpv 回推）",
                  client.position == 300.0, f"position={client.position}")

            # duration 走订阅缓存，不依赖 get_property 往返
            check("get_duration 拿到时长", client.get_duration() == 600.0)

            # file-loaded 事件
            check("wait_loaded 等到 file-loaded 事件", client.wait_loaded(timeout=1.0))

            # 命令超时要抛 MPVError 而不是永远卡住
            try:
                client.command("nonexistent_command", timeout=1.0)
                check("不支持的命令抛出 MPVError", False, "没抛异常")
            except mpvctl.MPVError:
                check("不支持的命令抛出 MPVError", True)

            # quit 要干净退出
            client.quit()
            check("quit() 后 running 为 False", client.running is False)
            check("quit() 后连接句柄已释放", client._handle is None)

        finally:
            server.stop()

    finally:
        subprocess.Popen = real_popen

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}，失败 {len(FAILED)}")
    for name in FAILED:
        print(f"  失败: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
