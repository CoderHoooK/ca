"""日志。

学生端是 --noconsole 打包的，屏幕上看不到任何输出，出问题只能靠文件。
所以每条日志都同时写 stdout（开发时）和文件（现场排查时）。
"""

from __future__ import annotations

import sys
from collections import deque
from datetime import datetime
from pathlib import Path

from .paths import app_dir

MAX_LOG_BYTES = 2 * 1024 * 1024
_checked = False

# 最近的日志行，界面（学生端的「日志」面板）从这里读，不用去解析日志文件。
# deque 的 append 是线程安全的，日志可以从任何线程写。
RECENT: deque[str] = deque(maxlen=300)


def _log_path() -> Path:
    if getattr(sys, "frozen", False):
        stem = Path(sys.executable).stem
    else:
        stem = Path(sys.argv[0]).stem or "app"
    return app_dir() / f"{stem}.log"


def _rotate_once(path: Path) -> None:
    """日志超过上限就轮转一次，免得机房跑一学期把磁盘塞满。"""
    global _checked
    if _checked:
        return
    _checked = True
    try:
        if path.is_file() and path.stat().st_size > MAX_LOG_BYTES:
            path.replace(path.with_suffix(".log.old"))
    except OSError:
        pass


def log(message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    RECENT.append(line)

    # 打包成 --noconsole 时 sys.stdout 是 None，print 会直接抛异常
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass

    path = _log_path()
    _rotate_once(path)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
