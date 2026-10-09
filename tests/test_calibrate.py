"""学生端「校准同步」测试：真教师端 Server + 真 Student（真 WebSocket），mpv 换成会模拟时间的假货。

    python tests/test_calibrate.py

假 mpv 能模拟「seek 之后要花 L 秒才真的动起来」——这正是自动纠偏之后还会落后的原因：
纠偏 seek 到教师此刻的位置，可 seek 完成时教师又往前播了 L 秒。
校准要做的就是测出这个 L，之后 seek 时多跳一点。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common import config, protocol
from student import state as st
from student.main import Student
from teacher.main import Server

PASSED: list[str] = []
FAILED: list[str] = []
T0 = time.time()


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  [{'ok' if condition else 'FAIL'}]   {name}" + (f"  {detail}" if detail else ""))


def teacher_pos(now: float | None = None) -> float:
    """教师端的真实位置：从 100 秒开始匀速播放。"""
    return 100.0 + ((time.time() if now is None else now) - T0)


class SimMPV:
    """seek 之后要等 latency 秒才恢复前进（期间位置停在目标上）。"""

    def __init__(self, latency: float, lag: float) -> None:
        self.latency = latency
        self.running = True
        self.paused = False
        self.calls: list[str] = []
        self._ref = teacher_pos() - lag
        self._ready_at = time.time()

    def get_position(self) -> float:
        if self.paused:
            return self._ref
        return self._ref + max(0.0, time.time() - self._ready_at)

    def seek(self, position: float) -> None:
        self.calls.append("seek")
        self._ref, self._ready_at = float(position), time.time() + self.latency

    def pause(self) -> None:
        self._ref, self.paused = self.get_position(), True
        self._ready_at = time.time()

    def play(self) -> None:
        self.paused = False
        self._ready_at = time.time()

    def query_paused(self) -> bool:
        return self.paused

    def stop(self) -> None:
        self.running = False

    quit = stop


async def wait_until(predicate, timeout: float = 15.0, poll: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll)
    return predicate()


async def drift_of(stu: Student, samples: int = 5) -> float:
    """真实偏差（正=超前）。用教师的真实位置比，不用心跳估算。取中位数。"""
    values = []
    for _ in range(samples):
        values.append(stu.mpv.get_position() - teacher_pos())
        await asyncio.sleep(0.15)
    return sorted(values)[len(values) // 2]


async def main() -> int:
    config.DRIFT_THRESHOLD = 0.5
    server = Server(lambda message, peer: None)
    server.start()
    server.ready.wait(5)
    await asyncio.sleep(0.2)

    mode = {"playing": True, "beats": True}

    async def beats() -> None:
        while True:
            if mode["beats"]:
                now = time.time()
                server.broadcast({
                    "cmd": protocol.HEARTBEAT,
                    "playing": mode["playing"],
                    "position": mode.get("fixed", teacher_pos(now)),
                    "server_time": now,
                })
            await asyncio.sleep(0.3)

    beat_task = asyncio.create_task(beats())

    stu = Student(cache_dir=Path("/tmp/lvs_calib_cache"))
    stu.mpv = SimMPV(latency=0.8, lag=2.0)
    run_task = asyncio.create_task(stu.run_forever())
    try:
        print("\n连接")
        ok = await wait_until(lambda: stu.state.conn == st.CONNECTED and stu._hb is not None)
        check("学生连上教师端并收到心跳", ok)
        stu.state.play = st.PLAYING

        print("\n不点按钮：自动纠偏会自己学会补偿 seek 延迟（以前会每秒 seek 一次、总是落后）")
        await asyncio.sleep(9.0)
        auto = await drift_of(stu)
        check("自动学到了 seek 补偿约 0.8 秒", 0.5 < stu._seek_lead < 1.1, f"{stu._seek_lead:+.2f}")
        check("自动纠偏稳定后偏差在 0.3 秒以内", abs(auto) < 0.3, f"{auto:+.2f}")
        seeks = stu.mpv.calls.count("seek")
        check("没有一直 seek（9 秒内不超过 6 次）", seeks <= 6, f"{seeks} 次")

        print("\n换一台更慢的机器（seek 延迟 1.5 秒、落后 4 秒），点「校准同步」")
        stu._seek_lead = 0.0
        stu._last_fix = None
        stu.mpv = SimMPV(latency=1.5, lag=4.0)
        stu.calibrate()
        ok = await wait_until(lambda: stu.state.calibrating, 3)
        check("按钮之后进入校准中", ok)
        ok = await wait_until(lambda: not stu.state.calibrating, 30)
        check("校准结束", ok, stu.state.calib_note)
        check("结果是「已校准」", stu.state.calib_ok is True and "已校准" in stu.state.calib_note, stu.state.calib_note)
        check("学到了 seek 补偿约 1.5 秒", 1.2 < stu._seek_lead < 1.8, f"{stu._seek_lead:+.2f}")
        after = await drift_of(stu)
        check("校准后偏差在 0.2 秒以内", abs(after) < 0.2, f"{after:+.2f}")

        print("\n校准之后自动纠偏也带补偿：再掉队一次，纠回来仍然准")
        stu.mpv._ref -= 3.0  # 突然落后 3 秒（比如卡了一下）
        await asyncio.sleep(5.0)
        again = await drift_of(stu)
        check("自动纠偏后偏差在 0.25 秒以内", abs(again) < 0.25, f"{again:+.2f}")

        print("\n时钟偏了：校准会重新对时")
        stu.clock.offset += 2.5
        stu.mpv.calls.clear()
        await asyncio.sleep(2.5)  # 错误的时钟让自动纠偏把画面带偏
        wrong = await drift_of(stu)
        check("时钟错了之后确实被带偏", abs(wrong) > 1.5, f"{wrong:+.2f}")
        stu.calibrate()
        await wait_until(lambda: stu.state.calibrating, 3)
        await wait_until(lambda: not stu.state.calibrating, 30)
        check("时钟偏差被重新测准（本机同一时钟，应接近 0）", abs(stu.clock.offset) < 0.05, f"{stu.clock.offset:+.3f}")
        fixed = await drift_of(stu)
        check("校准后又对齐了", stu.state.calib_ok is True and abs(fixed) < 0.2, f"{fixed:+.2f} {stu.state.calib_note}")

        print("\n界面状态位")
        check("校准结果可读", "秒" in stu.state.calib_note)

        print("\n老师暂停：对齐到暂停的位置，学生也停着")
        mode["playing"] = False
        mode["fixed"] = 123.4
        await asyncio.sleep(0.8)
        stu.calibrate()
        await wait_until(lambda: not stu.state.calibrating and "暂停" in stu.state.calib_note, 10)
        check("提示老师是暂停的并对齐", "暂停" in stu.state.calib_note and stu.state.calib_ok is True, stu.state.calib_note)
        check("学生停在老师暂停的位置", stu.mpv.paused and abs(stu.mpv.get_position() - 123.4) < 0.05,
              f"{stu.mpv.get_position():.2f}")
        check("学生状态是暂停", stu.state.play == st.PAUSED)
        mode.pop("fixed"); mode["playing"] = True

        print("\n不能校准的情况")
        stu.mpv.paused = False
        stu.state.play = st.IDLE
        stu.calibrate()
        await wait_until(lambda: "还没有在播放" in stu.state.calib_note, 3)
        check("没在播放：提示开始播放后再点", "还没有在播放" in stu.state.calib_note and stu.state.calib_ok is False,
              stu.state.calib_note)

        stu.state.play = st.PLAYING
        mode["beats"] = False
        await asyncio.sleep(3.3)  # 心跳断了：位置信息过期
        stu.calibrate()
        await wait_until(lambda: not stu.state.calibrating and "没有收到" in stu.state.calib_note, 8)
        check("收不到教师端位置：给出原因", "没有收到教师端的播放位置" in stu.state.calib_note, stu.state.calib_note)
        check("失败后可以再点（没卡在校准中）", not stu.state.calibrating and not stu._calibrating)

        stu.state.buffering = True
        mode["beats"] = True
        await asyncio.sleep(0.6)
        saved = config.CALIBRATE_ROUNDS
        stu.state.play = st.PLAYING
        # 缓冲 20 秒太久：把等待改短来测
        orig = stu._wait_not_buffering
        stu._wait_not_buffering = lambda timeout: orig(0.6)
        stu.calibrate()
        await wait_until(lambda: not stu.state.calibrating and "缓冲" in stu.state.calib_note, 8)
        check("一直在缓冲：说明是网络问题不是同步", "缓冲" in stu.state.calib_note and stu.state.calib_ok is False,
              stu.state.calib_note)
        stu.state.buffering = False
        stu._wait_not_buffering = orig
        config.CALIBRATE_ROUNDS = saved

        print("\n补偿量有上下限")
        stu._seek_lead = 0.0
        stu.mpv = SimMPV(latency=9.0, lag=2.0)  # 离谱的 seek 延迟：补偿不能无限涨
        stu.state.play = st.PLAYING
        stu.calibrate()
        await wait_until(lambda: stu.state.calibrating, 3)
        await wait_until(lambda: not stu.state.calibrating, 60)
        check("补偿不超过上限", stu._seek_lead <= config.SEEK_LEAD_RANGE[1] + 1e-9, f"{stu._seek_lead:+.2f}")
        check("做不到时如实说「仍落后」", stu.state.calib_ok is False and "仍" in stu.state.calib_note,
              stu.state.calib_note)
    finally:
        beat_task.cancel()
        run_task.cancel()
        try:
            await run_task
        except BaseException:
            pass

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}，失败 {len(FAILED)}")
    for name in FAILED:
        print(f"  失败: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.stdout.flush()
    os._exit(code)
