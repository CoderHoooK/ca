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

    # ---- 校准同步按钮
    print("\n校准同步按钮")
    pump_until(app, lambda: student.state.conn == st.CONNECTED, 10)
    window.refresh()
    check("没在播放：按钮灰着", not window._calib_btn.isEnabled())
    sstate.video, sstate.play = "课程", st.PLAYING
    student.mpv.running = True
    sstate.calib_note, sstate.calib_ok, sstate.calibrating = "", None, False
    window.refresh()
    check("播放中：按钮可点", window._calib_btn.isEnabled() and window._calib_btn.text() == "校准同步", f"conn={sstate.conn}")
    sstate.calibrating, sstate.calib_note = True, "校准中…"
    window.refresh()
    check("校准中：按钮灰着并显示校准中", not window._calib_btn.isEnabled() and "校准中" in window._calib_btn.text())
    sstate.calibrating, sstate.calib_note, sstate.calib_ok = False, "已校准，和老师相差 0.03 秒以内", True
    window.refresh()
    check("校准完显示结果", "已校准" in window._calib_note.text() and window._calib_btn.isEnabled())
    check("成功结果是绿色", "2e9e5b" in window._calib_note.styleSheet(), window._calib_note.styleSheet())
    sstate.calib_ok, sstate.calib_note = False, "已尽力校准，仍落后约 0.50 秒"
    window.refresh()
    check("没对齐的结果是红色", "d64545" in window._calib_note.styleSheet())
    # 真的点一下：按钮 → Student.calibrate → 事件循环里跑 _calibrate（没有教师心跳，应该如实回报）
    sstate.calib_note, sstate.calib_ok = "", None
    window._calib_btn.click()
    pump_until(app, lambda: bool(sstate.calib_note), 5)
    window.refresh()
    check("点按钮真的会触发校准并把结果显示出来", bool(window._calib_note.text()), window._calib_note.text())
    sstate.video, sstate.play, sstate.calib_note, sstate.calib_ok = "", st.IDLE, "", None
    student.mpv.running = False
    window.refresh()
    check("播放结束后结果隐藏", window._calib_note.text() == "")

    # ---- 加入播放按钮：老师在放、本机没在放
    print("\n加入播放按钮")
    check("老师没在放：加入按钮不显示", not window._join_btn.isVisibleTo(window))
    student._hb = {"playing": True, "position": 5.0, "server_time": student.clock.teacher_now()}
    student.mpv.running = False
    window.refresh()
    check("老师在放、本机没在放：显示加入按钮，隐藏校准按钮",
          window._join_btn.isVisibleTo(window) and not window._calib_btn.isVisibleTo(window))
    check("提示文字说明可以加入", "加入播放" in window._hint.text(), window._hint.text())
    student._hb["server_time"] = student.clock.teacher_now() - 10  # 心跳过期
    window.refresh()
    check("老师那边的心跳断了：加入按钮消失", not window._join_btn.isVisibleTo(window))
    student._hb = None

    # ---- 切片播放：当前片段来源 + 缓存分布（用真的下载会话，教师机和「同学」都是真 HTTP 服务）
    print("\n切片播放：当前片段的来源和缓存分布")
    sys.path.insert(0, str(ROOT / "tests"))
    import shutil, tempfile, time as _time
    from test_streaming import SEG_SIZE, SEGMENTS, make_package
    from common.segserver import FolderProvider, SegServer
    from student.streaming import StreamSession
    tmpdir = Path(tempfile.mkdtemp(prefix="lvs_ui_"))
    folder, manifest = make_package(tmpdir / "pkg")
    provider = FolderProvider(folder, manifest)
    teacher_http = SegServer({manifest.id: provider}.get, max_uploads=8).start()
    peer_http = SegServer({manifest.id: provider}.get, max_uploads=8).start()
    config.PREFETCH_AHEAD = SEGMENTS
    session = StreamSession(
        manifest, tmpdir / "cache", ("127.0.0.1", teacher_http.port),
        # 偶数段有「同学」可以要，奇数段没有（只能找教师机）
        sources_fn=lambda pkg, n: [("127.0.0.1", peer_http.port)] if n % 2 == 0 else [],
    )
    for i in (0, 1):  # 本机缓存里原来就有的
        shutil.copy(folder / f"seg_{i:05d}.ts", session.dir / f"seg_{i:05d}.ts")
        session.have.add(i); session.origin[i] = ("cache", "")
    session.set_focus_time(0.0)   # 窗口只往播放位置后面取，所以先从头下完
    session.start()
    t_end = _time.monotonic() + 15
    while session.cached < SEGMENTS and _time.monotonic() < t_end:
        _time.sleep(0.05)
    session.stop()
    session.set_focus_time(6.0)   # 然后让「当前」落在第 4 段（下标 3）

    snap = session.snapshot()
    check("快照：每段一个标记", len(snap["map"]) == SEGMENTS and snap["have"] == SEGMENTS)
    check("快照：前两段是本机缓存，偶数段来自同学，奇数段来自教师机",
          snap["map"][:2] == "cc" and all(snap["map"][i] == "p" for i in range(2, SEGMENTS, 2))
          and all(snap["map"][i] == "t" for i in range(3, SEGMENTS, 2)), snap["map"])
    check("快照：缓存字节数", snap["bytes"] == SEGMENTS * SEG_SIZE, str(snap["bytes"]))
    check("快照：当前片段是第 3 下标，来自教师机", snap["current"] == 3 and snap["current_kind"] == "teacher", str(snap["current_kind"]))

    student._streams[manifest.id] = session
    student._stream = session
    sstate.video, sstate.play, sstate.stream = "课程", st.PLAYING, True
    sstate.stream_total, sstate.stream_have = SEGMENTS, SEGMENTS
    sstate.from_teacher, sstate.from_peers = session.from_teacher, session.from_peers
    student.mpv.running = True
    window.refresh()
    check("显示当前片段：第几段、时间范围、来源",
          "第 4 段" in window._cur_label.text() and "00:06–00:08" in window._cur_label.text() and "来源：教师机" in window._cur_label.text(),
          window._cur_label.text())
    check("显示最近下载的一段及来源", "最近下载" in window._cur_label.text())
    check("缓存一行带上大小", "MB" in window._cache_label.text() or "KB" in window._cache_label.text(), window._cache_label.text())
    check("缓存分布图和图例可见", window._map.isVisibleTo(window) and window._legend.isVisibleTo(window))
    check("分布图拿到每段的颜色码和当前位置", window._map._codes == snap["map"] and window._map._current == 3)

    session.set_focus_time(4.0)   # 第 3 段（下标 2），来自同学
    window.refresh()
    check("当前片段来自同学时显示同学的地址", "来源：同学 127.0.0.1" in window._cur_label.text(), window._cur_label.text())
    session.set_focus_time(0.0)   # 下标 0：本机缓存
    window.refresh()
    check("本机缓存里原来就有的显示「本机缓存」", "本机缓存" in window._cur_label.text().splitlines()[0], window._cur_label.text())

    empty = StreamSession(manifest, tmpdir / "cache_empty", None)
    student._stream = empty
    empty.set_focus_time(10.0)
    window.refresh()
    check("当前片段还没到时提示「正在获取」", "还没缓存" in window._cur_label.text(), window._cur_label.text())

    window.resize(520, 700)
    window.show(); app.processEvents(); window.refresh(); app.processEvents()
    student._stream = session; window.refresh(); app.processEvents()
    window.grab().save(str(tmpdir / "student_stream_ui.png"))  # 想看效果就把这行的路径改成自己的

    student._stream = None
    student._streams.pop(manifest.id, None)
    sstate.video, sstate.play, sstate.stream = "", st.IDLE, False
    sstate.stream_total = sstate.stream_have = sstate.from_teacher = sstate.from_peers = 0
    window.refresh()
    check("播放结束后这几行都隐藏", not window._cur_label.isVisibleTo(window) and not window._map.isVisibleTo(window))
    teacher_http.stop(); peer_http.stop()
    shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- 日志面板
    check("日志面板有内容", "已连接" in window._log_view.toPlainText())

    # ---- 关窗口：点 × 直接退出（不再藏到托盘）
    print("\n关闭窗口")
    from PySide6.QtGui import QCloseEvent
    quit_calls: list[int] = []
    original_quit = QApplication.quit
    QApplication.quit = staticmethod(lambda: quit_calls.append(1))  # type: ignore[method-assign]
    try:
        event = QCloseEvent()
        window.closeEvent(event)
    finally:
        QApplication.quit = original_quit  # type: ignore[method-assign]
    check("点 × 接受关闭，不再隐藏到托盘", event.isAccepted())
    check("点 × 会让应用退出", quit_calls == [1])

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
