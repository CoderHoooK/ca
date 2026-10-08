"""同机集成测试：1 个教师端 + 3 个学生端，全用真 mpv。

    python tests/test_integration.py              # 默认用 movie.mkv
    python tests/test_integration.py movie.mp4    # 换 mp4 跑同一套

和别的测试不同，这里**不造假**：教师端用的是真的 teacher.main.Window
（连它的 QTimer 心跳和 start_at 调度都跑起来），学生端是真的 Student，
视频、字幕、mpv、WebSocket、UDP 发现全都走真实路径。

学生端扫桌面的逻辑改指向 tests/media，所以不会往你桌面上放东西。

会弹出 4 个 mpv 窗口和 1 个教师端窗口，这是预期的。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

MEDIA = ROOT / "tests" / "media"
# 默认 mkv；给个文件名参数就能换容器，验「老师选 mp4、学生也跟得上」这条。
VIDEO = MEDIA / (sys.argv[1] if len(sys.argv) > 1 else "movie.mkv")
STUDENT_COUNT = 3
SYNC_TOLERANCE = 1.0  # 秒。超过这个值就该被心跳纠偏了

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [ok]   {name}" + (f"  {detail}" if detail else ""), flush=True)
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}  {detail}", flush=True)


def pump(app, seconds: float) -> None:
    """跑 Qt 事件循环若干秒。

    必须用它代替 time.sleep：心跳、start_at 调度、界面刷新都挂在 QTimer
    上，不转事件循环它们根本不会触发，测出来的同步就是假的。
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def pump_until(app, predicate, timeout: float, poll: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(poll)
    return False


def main() -> int:
    import student.main as student_main
    import teacher.main as teacher_main
    from student.main import Student
    from PySide6.QtWidgets import QApplication

    print("同机集成测试：1 教师 + 3 学生（真 mpv）", flush=True)

    if not VIDEO.is_file():
        print(f"缺少测试视频 {VIDEO}")
        return 1

    # 学生端扫的是桌面，测试里改成 tests/media，免得污染真实桌面
    student_main.desktop_dir = lambda: MEDIA

    # 教师端出错会弹模态框，自动化跑的时候会把测试挂死，换成一个记录器
    class FakeMessageBox:
        calls: list[tuple] = []

        @staticmethod
        def critical(parent, title, text):
            FakeMessageBox.calls.append(("critical", title, text))

        @staticmethod
        def warning(parent, title, text):
            FakeMessageBox.calls.append(("warning", title, text))

    teacher_main.QMessageBox = FakeMessageBox

    # ---------------------------------------------------------- 3 个学生端
    students = [Student() for _ in range(STUDENT_COUNT)]
    holder: dict = {}

    def run_students() -> None:
        # 事件循环必须在本线程里建。在外面建再 set_event_loop 会触发
        # 「There is no current event loop」的弃用告警。
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        holder["loop"] = loop
        try:
            loop.run_until_complete(
                asyncio.gather(*(s.run_forever() for s in students))
            )
        except (asyncio.CancelledError, RuntimeError):
            pass
        finally:
            loop.close()

    student_thread = threading.Thread(
        target=run_students, daemon=True, name="students"
    )
    student_thread.start()

    for _ in range(200):
        if "loop" in holder:
            break
        time.sleep(0.01)
    loop = holder["loop"]

    # ---------------------------------------------------------- 1 个教师端
    app = QApplication.instance() or QApplication([])
    window = teacher_main.Window()
    window.video = VIDEO          # 跳过文件选择框
    window.show()

    try:
        print("\n等 3 台学生机连上来", flush=True)
        connected = pump_until(app, lambda: window.server.client_count == 3, 30.0)
        check("3 台学生机都连上了",
              connected, f"实际 {window.server.client_count} 台")
        if not connected:
            return 1

        # ------------------------------------------------------ 同步播放
        print("\n点「同步播放」", flush=True)
        window._play()
        check("教师端 mpv 就绪", window.mpv.running)
        check("没有弹出任何错误框", not FakeMessageBox.calls, str(FakeMessageBox.calls))

        # PLAY_LEAD 之后才起播。这里等「都播起来」而不是死等固定秒数——
        # 同一台机器上 4 个 mpv 抢资源时，某台学生机可能要重试一下才播起来，
        # 这正是心跳纠偏该发光的地方，不该让测试卡在固定时刻上。
        def all_playing() -> bool:
            return all(
                s.mpv.running and s.mpv.query_paused() is False for s in students
            )

        started = pump_until(app, all_playing, 12.0)
        check("3 台学生机都播起来了", started)

        pump(app, 2.0)  # 再留点时间让心跳把位置对齐

        def positions():
            return [s.mpv.get_position() for s in students]

        teacher_pos = window.mpv.get_position()
        student_pos = positions()
        check("教师端在播放", window.mpv.query_paused() is False)
        check(
            "起播后位置一致",
            all(p is not None and abs(p - teacher_pos) < SYNC_TOLERANCE
                for p in student_pos),
            f"教师 {teacher_pos:.2f}s  学生 {[f'{p:.2f}' for p in student_pos]}",
        )

        # ------------------------------------------------------ 持续同步
        print("\n连续采样 6 秒，看有没有越走越偏", flush=True)
        worst = 0.0
        for _ in range(12):
            pump(app, 0.5)
            tp = window.mpv.get_position()
            for p in positions():
                if p is not None and tp is not None:
                    worst = max(worst, abs(p - tp))
        check("6 秒内偏差始终在阈值内", worst < SYNC_TOLERANCE,
              f"最大偏差 {worst:.3f}s")

        # ------------------------------------------------------ 暂停
        print("\n点「暂停」", flush=True)
        window._pause()
        pump(app, 1.5)
        check("3 台学生机都暂停了",
              all(s.mpv.query_paused() is True for s in students))
        check("教师端也暂停了", window.mpv.query_paused() is True)

        # ------------------------------------------------------ 继续
        print("\n点「继续」", flush=True)
        window._resume()
        pump(app, 3.0)
        teacher_pos = window.mpv.get_position()
        student_pos = positions()
        check("3 台学生机都继续播放了",
              all(s.mpv.query_paused() is False for s in students))
        check(
            "继续后位置一致",
            all(p is not None and abs(p - teacher_pos) < SYNC_TOLERANCE
                for p in student_pos),
            f"教师 {teacher_pos:.2f}s  学生 {[f'{p:.2f}' for p in student_pos]}",
        )

        # ------------------------------------------------------ 拖进度条
        print("\n拖进度条到 15 秒", flush=True)
        before_seek = window.mpv.get_position()
        window._do_seek(15.0, resume=True)
        pump(app, 3.0)
        teacher_pos = window.mpv.get_position()
        student_pos = positions()
        # 注意不能断言「位置 ≈ 15」：拖动后 video 是继续播的，等 3 秒再读
        # 早就走过 15 秒了。要验的是「跳过去了」+「大家还在同一点」。
        check(
            "拖动后确实跳到了后面（不是原地没动）",
            teacher_pos is not None and before_seek is not None
            and teacher_pos > before_seek + 1.0,
            f"{before_seek:.2f}s → {teacher_pos:.2f}s",
        )
        check(
            "拖动后教师与学生仍然一致",
            all(p is not None and abs(p - teacher_pos) < SYNC_TOLERANCE
                for p in student_pos),
            f"教师 {teacher_pos:.2f}s  学生 {[f'{p:.2f}' for p in student_pos]}",
        )
        check("拖动后仍在播放",
              window.mpv.query_paused() is False
              and all(s.mpv.query_paused() is False for s in students))

        # ------------------------------------------------------ 停止
        print("\n点「停止」", flush=True)
        window._stop()
        pump(app, 2.0)
        check("3 台学生机的 mpv 都关了",
              all(not s.mpv.running for s in students))
        check("教师端 mpv 也关了", not window.mpv.running)

        check("全程没有弹出任何错误框",
              not FakeMessageBox.calls, str(FakeMessageBox.calls))

    finally:
        window.mpv.stop()
        for s in students:
            try:
                s.mpv.stop()
            except Exception:
                pass
        # 先取消学生端的任务再关循环，否则会刷一屏
        # 「Task was destroyed but it is pending」和「Event loop is closed」
        try:
            loop.call_soon_threadsafe(
                lambda: [t.cancel() for t in asyncio.all_tasks(loop)]
            )
        except RuntimeError:
            pass
        student_thread.join(timeout=5.0)

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}，失败 {len(FAILED)}")
    for name in FAILED:
        print(f"  失败: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
