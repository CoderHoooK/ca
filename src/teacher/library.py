"""教师端的「选择切片课程」对话框。

列出课程库（Teacher.exe 旁边的「课程库」目录）里的课程，也可以浏览到别处。
课程由 tools/slice.py 在别的电脑上切好，整个文件夹拷进课程库即可。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)
from PySide6.QtCore import Qt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.package import Manifest, PackageError, list_library, load_manifest  # noqa: E402


def describe(manifest: Manifest) -> str:
    minutes, seconds = divmod(int(manifest.duration), 60)
    parts = [
        manifest.title or "（无标题）",
        f"{minutes}:{seconds:02d}",
        f"{len(manifest.segments)} 段",
        f"{manifest.total_size / 1048576:.0f} MB",
        "有字幕" if manifest.subtitles else "无字幕",
    ]
    return "   ·   ".join(parts)


class LibraryDialog(QDialog):
    def __init__(self, parent, root: Path) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择切片课程")
        self.resize(560, 360)
        self.root = root
        self.selected: tuple[Path, Manifest] | None = None

        layout = QVBoxLayout(self)
        self._hint = QLabel()
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _item: self._accept())
        layout.addWidget(self._list, 1)

        row = QHBoxLayout()
        browse = QPushButton("浏览其他文件夹…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self._ok = QPushButton("选择")
        self._ok.setDefault(True)
        self._ok.clicked.connect(self._accept)
        row.addWidget(cancel)
        row.addWidget(self._ok)
        layout.addLayout(row)

        self._list.itemSelectionChanged.connect(
            lambda: self._ok.setEnabled(bool(self._list.selectedItems()))
        )
        self._reload()

    def _reload(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)  # 让老师看得到该往哪放
        except OSError:
            pass
        courses = list_library(self.root)
        self._list.clear()
        for folder, manifest in courses:
            item = QListWidgetItem(describe(manifest))
            item.setData(Qt.UserRole, (folder, manifest))
            item.setToolTip(str(folder))
            self._list.addItem(item)
        if courses:
            self._hint.setText(f"课程库：{self.root}")
            self._list.setCurrentRow(0)
        else:
            self._hint.setText(
                f"课程库是空的：{self.root}\n"
                "用 tools/slice.py 把电影切片后，把生成的课程文件夹整个拷到这里，"
                "再重新打开本窗口。"
            )
        self._ok.setEnabled(bool(courses))

    def _accept(self) -> None:
        items = self._list.selectedItems()
        if not items:
            return
        self.selected = items[0].data(Qt.UserRole)
        self.accept()

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择课程文件夹", str(self.root))
        if not path:
            return
        try:
            manifest = load_manifest(Path(path))
        except PackageError as exc:
            QMessageBox.warning(self, "不是有效的课程文件夹", str(exc))
            return
        self.selected = (Path(path), manifest)
        self.accept()
