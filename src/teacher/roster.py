"""教师端的「学生机列表」：每台连着的学生机一行，显示状态和缓存进度。

数据来自学生端每秒上报的 STATUS（见 student/main.py 的 _status_loop），
由 Server.students_snapshot() 给出。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QProgressBar, QTableWidget, QTableWidgetItem

HEADERS = ["学生机", "状态", "缓存进度", "来源（教师机 / 同学）"]

RED = "#c0392b"
GRAY = "#888888"


def describe(row: dict) -> tuple[str, str, int | None, str, str]:
    """一行数据 → (状态文字, 状态颜色, 进度百分比或 None, 进度条文字, 来源文字)。"""
    play = row.get("play", "")
    stream = bool(row.get("stream"))
    have, total = row.get("have", 0), row.get("total", 0)
    buffering = bool(row.get("buffering"))

    source = ""
    percent: int | None = None
    bar_text = ""
    if stream and total:
        percent = min(100, int(have * 100 / total))
        bar_text = f"{have}/{total} 段  {percent}%"
        source = f"{row.get('from_teacher', 0)} / {row.get('from_peers', 0)}"

    color = ""
    if not play:
        text, color = "连接中…", GRAY
    elif play == "not_found":
        text, color = "桌面上没有这个视频", RED
    elif play == "error":
        text, color = "播放器启动失败", RED
    elif play == "loading":
        text = "加载中（拉取第一批切片）" if stream else "加载中"
    elif play == "waiting":
        text = "已就绪，等待开始"
    elif play in ("playing", "paused"):
        text = "已暂停" if play == "paused" else "播放中"
        if buffering:
            text, color = "缓冲中（卡住了）", RED
        text += "　切片" if stream else "　本地视频"
        if not stream:
            percent, bar_text = 100, "本地视频"
    else:
        text, color = "待机", GRAY
    return text, color, percent, bar_text, source


class Roster(QTableWidget):
    def __init__(self) -> None:
        super().__init__(0, len(HEADERS))
        self.setHorizontalHeaderLabels(HEADERS)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAlternatingRowColors(True)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.setColumnWidth(2, 190)
        self.setMinimumHeight(150)
        self._ids: list[int] = []

    def update_rows(self, rows: list[dict]) -> None:
        ids = [r["id"] for r in rows]
        if ids != self._ids:  # 有学生进出或换了顺序才重建，平时只改内容，不闪
            self.setRowCount(len(rows))
            for i in range(len(rows)):
                for col in (0, 1, 3):
                    self.setItem(i, col, QTableWidgetItem())
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setAlignment(Qt.AlignCenter)
                self.setCellWidget(i, 2, bar)
            self._ids = ids

        for i, row in enumerate(rows):
            text, color, percent, bar_text, source = describe(row)
            name = row.get("name") or "（未知）"
            self.item(i, 0).setText(f"{name}　{row['host']}")
            state_item = self.item(i, 1)
            state_item.setText(text)
            state_item.setForeground(Qt.red if color == RED else (Qt.gray if color == GRAY else Qt.black))
            self.item(i, 3).setText(source)
            bar = self.cellWidget(i, 2)
            # 不能用 setVisible(False)：表格重新布局时会把单元格控件又显示出来，成了一个空进度条
            bar.setValue(percent or 0)
            bar.setFormat(bar_text if percent is not None else "—")
