"""学生端界面测试：真窗口（offscreen，不需要显示器）+ 真后台线程 + 真教师端。

    python tests/test_student_ui.py

验证的是「按钮 → 学生端逻辑 → 状态 → 界面文字」这一整条线有没有接上。
mpv 换成假的。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import QApplication

from common import config, net
from student import state as st
from student.main import Student
from student.runner import Runner
from student.ui import StudentWindow
from teacher.main import Server
from test_scan import FakeMPV

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  [{'ok' if condition else 'FAIL'}]   {name}" + (f"  {detail}" if detail else ""))


def pump_until(app, predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.03)
    app.processEvents()
    return predicate()


def main() -> int:
    config.SCAN_TIMEOUT = 0.3
    config.DEEP_SCAN_TIMEOUT = 0.6
    config.REDISCOVER_INTERVAL = 0.2
    app = QApplication([])

    # ---- 教师端还没起：学生端应该显示「搜索中」，并在失败几次后自动展开设置
    student = Student()
    student.mpv = FakeMPV()
    window = StudentWindow(student)
    window.show()
    runner = Runner(student)
    runner.start()

    print("\n教师端不在")
    ok = pump_until(app, lambda: student.state.attempts >= 3)
    window.refresh()
    check("失败几次后横幅显示「正在搜索」", "正在搜索教师机" in window._banner.text(), window._banner.text())
    check("横幅里带上已尝试次数", "已尝试" in window._banner.text())
    check("失败几次后自动展开「连接设置」", window._settings.expanded)
    check("给出排查提示", "防火墙" in window._hint.text() and window._hint.isVisibleTo(window))

    # ---- 点「扫描教师机」：没有教师机时要有明确的结果提示
    window._scan_btn.click()
    check("点击后按钮立刻变成「扫描中」", window._scan_btn.text() == "扫描中…" and not window._scan_btn.isEnabled())
    pump_until(app, lambda: not student.state.scanning and "没有找到" in student.state.scan_note)
    window.refresh()
    check("扫描不到时提示「没有找到」", "没有找到" in window._scan_note.text(), window._scan_note.text())
    check("扫描结束按钮恢复", window._scan_btn.isEnabled())
    check("列表为空", window._list.count() == 0)

    # ---- 手动输入一个错的 IP
    window._ip_edit.setText("127.0.0.1:1")
    window._ip_btn.click()
    pump_until(app, lambda: student.state.pinned and student.state.attempts >= 1 and student.state.last_error)
    window.refresh()
    check("手动指定连不上时横幅变红并写出 IP", "连不上 127.0.0.1" in window._banner.text(), window._banner.text())
    check("出现「恢复自动搜索」按钮", window._auto_btn.isVisibleTo(window))

    # ---- 教师端起来了
    print("\n教师端上线")
    server = Server(lambda m, p: None)
    server.start()
    server.ready.wait(5)

    window._auto_btn.click()
    ok = pump_until(app, lambda: student.state.conn == st.CONNECTED and not student.state.pinned, 10)
    window.refresh()
    check("点「恢复自动搜索」后自动连上", ok)
    check("横幅变成已连接 + 教师机名", "已连接教师端" in window._banner.text() and net.machine_name() in window._banner.text(), window._banner.text())
    check("教师机一栏显示自动发现", "自动发现" in window._teacher_label.text(), window._teacher_label.text())
    check("延迟一栏有数值", "ms" in window._latency_label.text(), window._latency_label.text())
    check("恢复自动后「恢复自动搜索」按钮隐藏", not window._auto_btn.isVisibleTo(window))
    check("连上后自动展开的设置面板自动收起", not window._settings.expanded)

    # ---- 展开设置，扫描，列表里选中，连接
    print("\n扫描 → 选择 → 连接")
    window._settings.set_expanded(True)
    window._scan_btn.click()
    pump_until(app, lambda: window._list.count() == 1)
    window.refresh()
    check("扫描列表出现教师机", window._list.count() == 1)
    if window._list.count():
        item = window._list.item(0)
        check("只有一台时自动选中", item.isSelected())
        check("列表里标出当前连着的那台", "当前" in item.text(), item.text())
        check("「连接选中」按钮可用", window._connect_sel_btn.isEnabled())
        window._connect_sel_btn.click()
        pump_until(app, lambda: student.state.pinned)
        pump_until(app, lambda: student.state.conn == st.CONNECTED, 10)
        window.refresh()
        check("选中连接后进入手动指定模式并重新连上", student.state.pinned and student.state.conn == st.CONNECTED)
        check("教师机一栏显示手动指定", "手动指定" in window._teacher_label.text(), window._teacher_label.text())

    # ---- 切片播放：缓存进度卡片
    print("\n切片播放的进度显示")
    check("普通播放时不显示缓存一行", not window._cache_label.isVisibleTo(window))
    sstate = student.state
    sstate.video, sstate.play, sstate.stream = "课程", st.PLAYING, True
    sstate.stream_total, sstate.stream_have, sstate.from_teacher, sstate.from_peers = 20, 5, 2, 3
    student.mpv.running = True
    window.refresh()
    check("切片播放时显示缓存进度", window._cache_label.isVisibleTo(window), window._cache_label.text())
    check("显示已缓存段数和百分比", "5/20" in window._cache_label.text() and "25%" in window._cache_label.text())
    check("显示教师机/同学各提供多少", "教师机 2" in window._cache_label.text() and "同学 3" in window._cache_label.text())
    sstate.buffering = True
    window.refresh()
    check("缓冲时状态栏提示缓冲中", "缓冲" in window._play_label.text(), window._play_label.text())
    sstate.buffering = False
    sstate.video, sstate.play, sstate.stream = "", st.IDLE, False
    window.refresh()
    check("播放结束后缓存一行隐藏", not window._cache_label.isVisibleTo(window))

    # ---- 日志面板
    check("日志面板有内容", "已连接" in window._log_view.toPlainText())

    # ---- 关窗口：有托盘就藏起来，没有托盘就真退出
    print("\n关闭窗口")
    from PySide6.QtGui import QCloseEvent
    event = QCloseEvent()
    window.closeEvent(event)
    if window._tray is not None:
        check("有托盘时关窗口只是隐藏，不退出", not event.isAccepted())
    else:
        check("没有托盘时关窗口正常退出", event.isAccepted())

    window._quitting = True
    runner.stop()
    check("Runner 能干净停掉", not runner.is_alive())
    window._timer.stop()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}，失败 {len(FAILED)}")
    for name in FAILED:
        print(f"  失败: {name}")
    sys.stdout.flush()
    return 1 if FAILED else 0


if __name__ == "__main__":
    os._exit(main())
