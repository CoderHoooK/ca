"""学生端界面。

只显示状态，学生什么都不用点——连接、同步、播放全部是自动的。界面上的按钮
只在「自动找不到教师机」时才有用：

    · 扫描教师机   列出局域网里所有教师机，双击一台就连
    · 手动输入 IP  扫不到时的最后手段
    · 恢复自动搜索 手动指定之后想回到自动模式

线程模型：学生端的网络/mpv 逻辑跑在后台线程的 asyncio 循环里（main.Runner），
这里只是 Qt 主线程上的一个定时器，每 300ms 读一遍 student.state 刷新显示。
按钮调用的 student.connect_to / request_scan / use_auto 都是线程安全的。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from common.log import RECENT
from student import state as st

GREEN, AMBER, RED, GREY = "#2e9e5b", "#d9a000", "#d64545", "#8a8f98"

REFRESH_MS = 300
# 连续失败这么多次，就自动把「连接设置」面板展开，让学生看到扫描按钮
AUTO_EXPAND_AFTER = 2

PLAY_TEXT = {
    st.IDLE: "待机，等老师开始播放",
    st.LOADING: "正在加载视频…",
    st.WAITING: "已就绪，等待统一开始",
    st.PLAYING: "播放中",
    st.PAUSED: "已暂停",
    st.NOT_FOUND: "桌面上没有找到这个视频",
    st.ERROR: "播放器启动失败",
}

STYLE = f"""
QWidget {{ font-size: 13px; }}
QLabel#banner {{ font-size: 17px; font-weight: bold; color: white;
                 border-radius: 8px; padding: 12px 14px; }}
QLabel#hint {{ color: #555; }}
QLabel#warn {{ color: {RED}; font-weight: bold; }}
QLabel#key {{ color: {GREY}; }}
QFrame#card {{ background: #f6f7f9; border: 1px solid #e1e4e8; border-radius: 8px; }}
QToolButton#fold {{ border: none; font-weight: bold; color: #333; padding: 4px 0; }}
QPlainTextEdit {{ font-family: Consolas, "Courier New", monospace; font-size: 11px;
                  background: #fbfbfc; }}
QPushButton {{ padding: 5px 12px; }}
"""


def format_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


class Fold(QWidget):
    """一个可以折叠的区块：标题按钮 + 内容。"""

    def __init__(self, title: str, content: QWidget, expanded: bool = False) -> None:
        super().__init__()
        self._title = title
        self.content = content
        self.button = QToolButton()
        self.button.setObjectName("fold")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.button.clicked.connect(lambda: self.set_expanded(not self.expanded))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.button, alignment=Qt.AlignLeft)
        layout.addWidget(content)
        self.set_expanded(expanded)

    @property
    def expanded(self) -> bool:
        return not self.content.isHidden()

    def set_expanded(self, expanded: bool) -> None:
        self.content.setVisible(expanded)
        self.button.setText(("▾ " if expanded else "▸ ") + self._title)


def _card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    return frame


def _key(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("key")
    return label


class StudentWindow(QWidget):
    def __init__(self, student) -> None:
        super().__init__()
        self.student = student
        self._quitting = False
        self._auto_expanded = False
        self._tray: QSystemTrayIcon | None = None
        self._log_key: tuple[int, str] = (-1, "")
        self._ticks = 0
        self._desktop_videos = 0

        self.setWindowTitle("LanVideoSync 学生端")
        self.setStyleSheet(STYLE)
        self.setWindowIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.resize(460, 560)
        self._build()
        self._setup_tray()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    # ------------------------------------------------------------------ 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # 状态横幅
        self._banner = QLabel("正在启动…")
        self._banner.setObjectName("banner")
        self._banner.setWordWrap(True)
        root.addWidget(self._banner)

        self._hint = QLabel("")
        self._hint.setObjectName("hint")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        # 连接信息
        conn_card = _card()
        grid = QGridLayout(conn_card)
        self._teacher_label = QLabel("—")
        self._latency_label = QLabel("—")
        grid.addWidget(_key("教师机"), 0, 0)
        grid.addWidget(self._teacher_label, 0, 1)
        grid.addWidget(_key("网络延迟"), 1, 0)
        grid.addWidget(self._latency_label, 1, 1)
        grid.setColumnStretch(1, 1)
        root.addWidget(conn_card)

        # 播放信息
        play_card = _card()
        grid = QGridLayout(play_card)
        self._video_label = QLabel("—")
        self._video_label.setWordWrap(True)
        self._play_label = QLabel("—")
        self._pos_label = QLabel("—")
        grid.addWidget(_key("视频"), 0, 0)
        grid.addWidget(self._video_label, 0, 1)
        grid.addWidget(_key("状态"), 1, 0)
        grid.addWidget(self._play_label, 1, 1)
        grid.addWidget(_key("进度"), 2, 0)
        grid.addWidget(self._pos_label, 2, 1)
        # 只有靠切片播放时才显示：缓存了多少、从哪来的
        self._cache_key = _key("缓存")
        self._cache_label = QLabel("—")
        self._cache_label.setWordWrap(True)
        grid.addWidget(self._cache_key, 3, 0)
        grid.addWidget(self._cache_label, 3, 1)
        self._cache_key.setVisible(False)
        self._cache_label.setVisible(False)
        grid.setColumnStretch(1, 1)
        root.addWidget(play_card)

        self._env_label = QLabel("")
        self._env_label.setObjectName("hint")
        self._env_label.setWordWrap(True)
        root.addWidget(self._env_label)

        # 连接设置（折叠）
        self._settings = Fold("连接设置（连不上时用）", self._build_settings())
        root.addWidget(self._settings)

        # 日志（折叠）
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(300)
        self._log_view.setFixedHeight(130)
        self._log_fold = Fold("运行日志", self._log_view)
        root.addWidget(self._log_fold)

        root.addStretch(1)

    def _build_settings(self) -> QWidget:
        box = _card()
        layout = QVBoxLayout(box)

        row = QHBoxLayout()
        self._scan_btn = QPushButton("扫描教师机")
        self._scan_btn.clicked.connect(self._on_scan)
        self._scan_note = QLabel("")
        self._scan_note.setObjectName("hint")
        self._scan_note.setWordWrap(True)
        row.addWidget(self._scan_btn)
        row.addWidget(self._scan_note, 1)
        layout.addLayout(row)

        self._list = QListWidget()
        self._list.setFixedHeight(96)
        self._list.itemDoubleClicked.connect(self._on_connect_selected)
        self._list.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self._list)

        self._connect_sel_btn = QPushButton("连接选中的教师机")
        self._connect_sel_btn.clicked.connect(self._on_connect_selected)
        layout.addWidget(self._connect_sel_btn)

        row = QHBoxLayout()
        self._ip_edit = QLineEdit()
        self._ip_edit.setPlaceholderText("手动输入教师机 IP，例如 192.168.1.10")
        self._ip_edit.returnPressed.connect(self._on_connect_manual)
        self._ip_btn = QPushButton("连接")
        self._ip_btn.clicked.connect(self._on_connect_manual)
        row.addWidget(self._ip_edit, 1)
        row.addWidget(self._ip_btn)
        layout.addLayout(row)

        self._auto_btn = QPushButton("恢复自动搜索")
        self._auto_btn.clicked.connect(self.student.use_auto)
        layout.addWidget(self._auto_btn)
        return box

    def _setup_tray(self) -> None:
        """系统托盘：关窗口只是藏起来，学生机上的同步不能因为误点 × 就断掉。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self.windowIcon(), self)
        tray.setToolTip("LanVideoSync 学生端")
        menu = QMenu()
        show = QAction("显示窗口", menu)
        show.triggered.connect(self.show_window)
        quit_ = QAction("退出", menu)
        quit_.triggered.connect(self.quit_app)
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(quit_)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self.show_window()
            if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick)
            else None
        )
        tray.show()
        self._tray = tray
        self._tray_menu = menu  # 持有引用，否则会被回收

    # ------------------------------------------------------------------ 动作

    def _on_scan(self) -> None:
        self.student.request_scan()
        self.student.state.scanning = True  # 立刻反馈，不等后台线程转一圈
        self.student.state.scan_note = "正在扫描…"
        self.refresh()

    def _selected_teacher(self):
        items = self._list.selectedItems()
        return items[0].data(Qt.UserRole) if items else None

    def _on_connect_selected(self, *_args) -> None:
        teacher = self._selected_teacher()
        if teacher is not None:
            self.student.connect_to(teacher.host, teacher.port)

    def _on_connect_manual(self) -> None:
        text = self._ip_edit.text().strip()
        if not text:
            return
        host, _, port = text.partition(":")  # 允许写成 192.168.1.10:8765
        self.student.connect_to(host, int(port) if port.isdigit() else None)

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self._quitting = True
        self.close()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        if self._tray is not None and not self._quitting:
            event.ignore()
            self.hide()
            self._tray.showMessage(
                "LanVideoSync",
                "学生端仍在后台运行，右键托盘图标可退出。",
                QSystemTrayIcon.Information,
                3000,
            )
            return
        event.accept()

    # ------------------------------------------------------------------ 刷新

    def refresh(self) -> None:
        s = self.student.state
        self._refresh_banner(s)
        self._refresh_info(s)
        self._refresh_settings(s)
        self._refresh_log()

        self._ticks += 1
        if self._ticks % 10 == 1:  # 约每 3 秒看一次桌面
            self._desktop_videos = len(self.student.find_videos())
        self._refresh_env(s)

    def _set_banner(self, text: str, color: str) -> None:
        self._banner.setText(text)
        self._banner.setStyleSheet(f"background: {color};")

    def _refresh_banner(self, s) -> None:
        where = f"{s.teacher_name + ' ' if s.teacher_name else ''}({s.teacher_host})"
        hint = ""
        if s.conn == st.CONNECTED:
            self._set_banner(f"●  已连接教师端  {where}", GREEN)
            hint = "不用操作，等老师开始播放即可。"
        elif s.conn == st.CONNECTING:
            self._set_banner(f"●  正在连接 {s.teacher_host} …", AMBER)
        elif s.pinned:
            self._set_banner(
                f"●  连不上 {s.teacher_host or '指定的教师机'}，正在重试…", RED
            )
            hint = (
                f"{s.last_error}\n"
                "检查教师端是否已打开、IP 是否输对；也可以点「恢复自动搜索」。"
                if s.last_error else ""
            )
        elif s.conn == st.DISCONNECTED:
            self._set_banner("●  与教师端的连接已断开，正在重连…", RED)
            hint = "教师端重启或网络波动都会自动恢复，不用重开程序。"
        else:
            suffix = f"（已尝试 {s.attempts} 次）" if s.attempts else ""
            self._set_banner(f"●  正在搜索教师机…{suffix}", AMBER)
            if s.attempts >= AUTO_EXPAND_AFTER:
                hint = (
                    "一直找不到？请确认：① 教师端已经打开 ② 两台电脑在同一个网络 "
                    "③ 教师机防火墙允许 LanVideoSync。\n"
                    "也可以展开下面的「连接设置」，扫描或手动输入教师机 IP。"
                )
        self._hint.setText(hint)
        self._hint.setVisible(bool(hint))

    def _refresh_info(self, s) -> None:
        if s.conn == st.CONNECTED:
            name = s.teacher_name or "教师机"
            mode = "（手动指定）" if s.pinned else "（自动发现）"
            self._teacher_label.setText(f"{name}  {s.teacher_host}:{s.teacher_port}  {mode}")
            rtt = f"{s.rtt * 1000:.0f} ms" if s.rtt is not None else "—"
            self._latency_label.setText(f"{rtt}    时钟偏差 {s.offset:+.3f} s")
        else:
            self._teacher_label.setText("未连接")
            self._latency_label.setText("—")

        self._video_label.setText(
            (s.video + ("" if not s.video else ("（有字幕）" if s.has_subtitle else "（无字幕）")))
            if s.video else "—"
        )
        play = s.play
        # mpv 自己退出了（学生关掉了播放窗口）：别还显示「播放中」
        if play in (st.PLAYING, st.PAUSED, st.WAITING) and not self.student.mpv.running:
            play = st.IDLE
        text = PLAY_TEXT.get(play, play)
        if s.stream and s.buffering and play == st.PLAYING:
            text = "缓冲中，稍等…（缓冲完会自动追上老师）"
        self._play_label.setText(text)
        self._play_label.setStyleSheet(
            f"color: {RED}; font-weight: bold;" if play in (st.NOT_FOUND, st.ERROR) else ""
        )
        showing = play in (st.PLAYING, st.PAUSED)
        self._pos_label.setText(format_time(s.teacher_position) if showing else "—")

        streaming = bool(s.stream and s.stream_total)
        self._cache_key.setVisible(streaming)
        self._cache_label.setVisible(streaming)
        if streaming:
            pct = s.stream_have / s.stream_total
            self._cache_label.setText(
                f"已缓存 {s.stream_have}/{s.stream_total} 段（{pct:.0%}）　"
                f"教师机 {s.from_teacher} 段 · 同学 {s.from_peers} 段"
            )

    def _refresh_env(self, s) -> None:
        lines = []
        if not s.mpv_ok:
            lines.append("⚠ 没找到 mpv（应在程序目录的 mpv\\ 文件夹里），无法播放视频。")
        lines.append(f"桌面上识别到 {self._desktop_videos} 个视频文件")
        self._env_label.setText("\n".join(lines))
        self._env_label.setObjectName("warn" if not s.mpv_ok else "hint")
        self._env_label.style().unpolish(self._env_label)
        self._env_label.style().polish(self._env_label)

    def _refresh_settings(self, s) -> None:
        # 连不上时自动展开设置面板，连上之后如果是自动展开的就收回去
        failing = s.conn != st.CONNECTED and s.attempts >= AUTO_EXPAND_AFTER
        if failing and not self._settings.expanded:
            self._settings.set_expanded(True)
            self._auto_expanded = True
        elif s.conn == st.CONNECTED and self._auto_expanded:
            self._settings.set_expanded(False)
            self._auto_expanded = False

        self._scan_btn.setEnabled(not s.scanning)
        self._scan_btn.setText("扫描中…" if s.scanning else "扫描教师机")
        self._scan_note.setText(s.scan_note)

        # 列表内容变了才重建，否则每 300ms 重建一次会把学生的选中项冲掉
        current = [(t.host, t.port, t.name) for t in s.teachers]
        shown = [
            (self._list.item(i).data(Qt.UserRole).host,
             self._list.item(i).data(Qt.UserRole).port,
             self._list.item(i).data(Qt.UserRole).name)
            for i in range(self._list.count())
        ]
        if current != shown:
            selected = self._selected_teacher()
            self._list.clear()
            for teacher in s.teachers:
                item = QListWidgetItem(teacher.label)
                item.setData(Qt.UserRole, teacher)
                self._list.addItem(item)
                if selected is not None and (teacher.host, teacher.port) == (
                    selected.host, selected.port
                ):
                    item.setSelected(True)
            if self._list.count() == 1 and not self._list.selectedItems():
                self._list.item(0).setSelected(True)  # 只有一台就直接选中

        # 标出当前连着的那台
        for i in range(self._list.count()):
            item = self._list.item(i)
            teacher = item.data(Qt.UserRole)
            is_current = s.conn == st.CONNECTED and (teacher.host, teacher.port) == (
                s.teacher_host, s.teacher_port
            )
            item.setText(("✓ " if is_current else "") + teacher.label
                         + ("   ← 当前" if is_current else ""))

        self._auto_btn.setVisible(s.pinned)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self._connect_sel_btn.setEnabled(self._selected_teacher() is not None)

    def _refresh_log(self) -> None:
        lines = list(RECENT)
        # RECENT 满了之后长度不再变，所以连最后一行一起比，才知道有没有新内容
        key = (len(lines), lines[-1] if lines else "")
        if key == self._log_key:
            return
        self._log_key = key
        self._log_view.setPlainText("\n".join(lines))
        bar = self._log_view.verticalScrollBar()
        bar.setValue(bar.maximum())


def run_gui(student) -> None:
    """启动界面 + 后台学生端循环，阻塞到退出。"""
    from student.runner import Runner

    app = QApplication(sys.argv)
    app.setFont(QFont(app.font().family(), 10))
    window = StudentWindow(student)
    if window._tray is not None:
        app.setQuitOnLastWindowClosed(False)

    runner = Runner(student)
    runner.start()
    window.show()
    try:
        app.exec()
    finally:
        runner.stop()
