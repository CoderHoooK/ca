"""教师端「选择切片课程」+ 播放切片课程的测试。离屏 Qt，假 mpv，不需要 ffmpeg。

    python tests/test_teacher_ui.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from common import config, protocol
from teacher import main as teacher_main
from teacher.library import LibraryDialog, describe
from teacher.roster import describe as describe_row
from test_streaming import http, make_package

APP = None
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
        self.started: dict = {}

    def start(self, video, subtitle, position=0.0, load_timeout=None, extra_args=None):
        self.started = {"video": str(video), "subtitle": subtitle, "extra": list(extra_args or [])}
        self.running = True

    boost: list = []

    def set_quiet_boost(self, on): self.boost.append(bool(on))
    def play(self): self.calls.append("play"); self.paused = False
    def pause(self): self.calls.append("pause"); self.paused = True
    def seek(self, position): self.calls.append("seek")
    def stop(self): self.calls.append("stop"); self.running = False
    def quit(self): self.running = False
    def query_paused(self): return self.paused
    def get_position(self): return 3.0
    def get_duration(self): return 24.0


def main() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="lvs_tui_"))
    try:
        library = tmp / "课程库"
        folder, manifest = make_package(library, "课A")
        make_package(library, "课B")
        (library / "垃圾文件夹").mkdir()

        print("\n课程库对话框")
        dlg = LibraryDialog(None, library)
        check("列出 2 门课，跳过损坏/无关的文件夹", dlg._list.count() == 2, str(dlg._list.count()))
        check("每门课显示标题、时长、段数、字幕", "课A" in describe(manifest) and "有字幕" in describe(manifest)
              and "0:24" in describe(manifest), describe(manifest))
        dlg._list.setCurrentRow(0)
        dlg._accept()
        check("选择后返回 (文件夹, manifest)", dlg.selected is not None and dlg.selected[1].title == "课A")

        empty = LibraryDialog(None, tmp / "新的空课程库")
        check("空课程库给出提示，并告诉老师往哪放", "空" in empty._hint.text() and "新的空课程库" in empty._hint.text())
        check("空课程库时「选择」不可点", not empty._ok.isEnabled())
        check("课程库目录不存在时自动建好", (tmp / "新的空课程库").is_dir())

        print("\n播放切片课程")
        # 设置文件指到临时目录，别读写真的
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp / "settings"))
        window = teacher_main.Window()
        window.mpv = FakeMPV()
        sent: list[dict] = []
        window.server.broadcast = sent.append  # type: ignore[method-assign]
        try:
            check("不选课程时不开切片服务（不多占端口）", window._http is None)
            window.package = (folder, manifest)
            window.video = None
            t0 = time.time()
            window._play()
            check("切片服务按需启动", window._http is not None)
            port = window._http.port
            m = window.mpv.started
            check("教师机的 mpv 通过本机切片服务播放", m.get("video") == f"http://127.0.0.1:{port}/play/{manifest.id}/index.m3u8", m.get("video", ""))
            check("加载了字幕文件", m.get("subtitle") == folder / "subs" / "subtitle.ass")
            check("带上限制预读的参数", all(a in m.get("extra", []) for a in config.MPV_STREAM_ARGS))
            check("广播了一条 PLAY", len(sent) == 1 and sent[0]["cmd"] == protocol.PLAY)
            msg = sent[0]
            check("PLAY 带课程信息", msg["package"] == {"id": manifest.id, "title": "课A", "http_port": port}, str(msg.get("package")))
            check("video 字段是原始文件名（学生桌面有同名就用本地的）", msg["video"] == manifest.source_name)
            lead = msg["start_at"] - t0
            check("起播提前量默认是配置里的值", window._lead_spin.value() == int(config.STREAM_PLAY_LEAD)
                  and config.STREAM_PLAY_LEAD - 1 < lead < config.STREAM_PLAY_LEAD + 3, f"{lead:.1f}s")
            # 界面里改成 35 秒：下一次播放就按 35 秒，并且记住
            window._lead_spin.setValue(35)
            sent.clear()
            t1 = time.time()
            window._play()
            lead2 = sent[0]["start_at"] - t1
            check("在界面里改提前量后，下一次播放按新的值", 34 < lead2 < 38, f"{lead2:.1f}s")
            check("改的值被记住", QSettings(QSettings.IniFormat, QSettings.UserScope, "LanVideoSync", "Teacher")
                  .value("stream_play_lead", type=float) == 35.0)
            lo, hi = config.STREAM_PLAY_LEAD_RANGE
            window._lead_spin.setValue(10_000)
            check("提前量有上限", window._lead_spin.value() == hi)
            window._lead_spin.setValue(0)
            check("提前量有下限", window._lead_spin.value() == lo)
            window._lead_spin.setValue(int(config.STREAM_PLAY_LEAD))
            code, _ = http(f"http://127.0.0.1:{port}/p/{manifest.id}/manifest.json")
            check("学生能从教师机的服务取到 manifest", code == 200)
            code, body = http(f"http://127.0.0.1:{port}/p/{manifest.id}/seg_00000.ts")
            check("学生能取到切片", code == 200 and len(body) > 0)

            # 状态栏：学生机缓存进度
            window.server._peers = {
                "a": {"host": "1.1.1.1", "port": 1, "have": {manifest.id: set(range(6))}},
                "b": {"host": "1.1.1.2", "port": 2, "have": {manifest.id: set(range(12))}},
            }
            window._tick()
            text = window._status_label.text()
            check("状态栏显示切片分发情况", "2 台" in text and "75%" in text, text)

            # ---- 学生机列表
            rows = [
                {"id": 1, "host": "10.0.0.1", "name": "PC-01", "play": "playing", "stream": True,
                 "have": 6, "total": 12, "from_teacher": 2, "from_peers": 4, "buffering": False},
                {"id": 2, "host": "10.0.0.2", "name": "PC-02", "play": "playing", "stream": False},
                {"id": 3, "host": "10.0.0.3", "name": "PC-03", "play": "playing", "stream": True,
                 "have": 3, "total": 12, "buffering": True},
                {"id": 4, "host": "10.0.0.4", "name": "PC-04", "play": "not_found"},
                {"id": 5, "host": "10.0.0.5", "name": "", "play": ""},
                {"id": 6, "host": "10.0.0.6", "name": "PC-06", "play": "idle"},
            ]
            text, _c, pct, bar, src = describe_row(rows[0])
            check("切片播放：显示进度和来源", pct == 50 and "6/12" in bar and src == "2 / 4" and "播放中" in text, f"{text} {bar} {src}")
            text, _c, pct, bar, _s = describe_row(rows[1])
            check("本地视频：进度条显示「本地视频」", pct == 100 and bar == "本地视频" and "本地" in text, f"{text} {bar}")
            text, color, pct, _b, _s = describe_row(rows[2])
            check("缓冲中标红", "缓冲" in text and color != "", text)
            check("没找到视频标红", describe_row(rows[3])[1] != "" and "没有" in describe_row(rows[3])[0])
            check("还没上报状态的显示「连接中」", "连接中" in describe_row(rows[4])[0])
            check("待机没有进度条", describe_row(rows[5])[2] is None)

            roster = window._roster
            roster.update_rows(rows)
            check("表格每台学生机一行", roster.rowCount() == 6)
            check("学生机一栏有名字和 IP", "PC-01" in roster.item(0, 0).text() and "10.0.0.1" in roster.item(0, 0).text())
            check("进度条数值正确", roster.cellWidget(0, 2).value() == 50 and roster.cellWidget(1, 2).value() == 100)
            check("没有进度的行显示「—」而不是空进度条", roster.cellWidget(5, 2).format() == "—" and roster.cellWidget(5, 2).value() == 0)
            bar_widget = roster.cellWidget(0, 2)
            rows[0]["have"] = 9
            roster.update_rows(rows)
            check("只改内容时不重建行（进度条对象不变）", roster.cellWidget(0, 2) is bar_widget and bar_widget.value() == 75)
            roster.update_rows(rows[:2])
            check("学生离开后行数减少", roster.rowCount() == 2)

            # ---- 增强轻声
            check("「增强轻声」默认关闭", not window._boost_check.isChecked())
            window.mpv.boost = []
            window.mpv.running = True
            window._boost_check.setChecked(True)
            check("播放中勾选：立刻对 mpv 生效", window.mpv.boost == [True], str(window.mpv.boost))
            window._boost_check.setChecked(False)
            check("播放中取消：立刻关掉", window.mpv.boost == [True, False])
            window._boost_check.setChecked(True)
            check("设置被记住", QSettings(QSettings.IniFormat, QSettings.UserScope, "LanVideoSync", "Teacher")
                  .value("quiet_boost", type=bool) is True)
            window.mpv.running = False
            window.mpv.boost = []
            window._boost_check.setChecked(False)
            window._boost_check.setChecked(True)
            check("没在播放时勾选不碰 mpv", window.mpv.boost == [])
            sent.clear()
            window.package = (folder, manifest)
            window._play()
            check("开播后自动应用（切片课程）", window.mpv.boost == [True], str(window.mpv.boost))
            window.mpv.boost = []
            window.package = None
            window.video = tmp / "普通.mkv"
            window.video.write_bytes(b"x")
            window._play()
            check("开播后自动应用（本地视频）", window.mpv.boost == [True], str(window.mpv.boost))
            window._boost_check.setChecked(False)
            window.mpv.running = False
            window.package = (folder, manifest)
            window.video = None

            # 老师直接叉掉播放窗口 → 广播 STOP，学生机一起关
            sent.clear()
            window.mpv.running = True
            window._tick()                      # 记下「播放窗口在」
            window.mpv.running = False          # 老师点了播放窗口的 ×
            window._tick()
            stops = [m for m in sent if m.get("cmd") == protocol.STOP]
            check("叉掉播放窗口会广播 STOP", len(stops) == 1, str(sent))
            window._tick()
            check("只广播一次", len([m for m in sent if m.get("cmd") == protocol.STOP]) == 1)
            # 点「停止」按钮只广播一次（不会再被「窗口被关」重复广播）
            sent.clear()
            window.mpv.running = True
            window._tick()
            window._stop()
            window.mpv.running = False
            window._tick()
            check("点「停止」只广播一次 STOP", len([m for m in sent if m.get("cmd") == protocol.STOP]) == 1, str(sent))

            # 换成普通视频后，不再走切片
            sent.clear()
            window.package = None
            window.video = tmp / "普通.mkv"
            window.video.write_bytes(b"x")
            window._play()
            check("普通视频走原来的流程（PLAY 里没有 package）", sent and "package" not in sent[-1], str(sent[-1:]))
        finally:
            window.mpv.running = False
            if window._http:
                window._http.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}，失败 {len(FAILED)}")
    for name in FAILED:
        print(f"  失败: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
