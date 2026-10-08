"""路径定位：程序目录、mpv.exe、桌面。

打包成 exe 之后这些路径全都会变，所以集中在这一个文件里，
别在业务代码里散落 __file__ / sys.executable。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def app_dir() -> Path:
    """程序所在目录。

    打包后是 Student.exe / Teacher.exe 所在的目录，
    开发时是项目根目录（src/common/paths.py → 上三层）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def find_mpv() -> Path | None:
    """按优先级找 mpv.exe，找不到返回 None。

    分发时把 mpv 放在 exe 同级的 mpv\\ 目录里，学生机就不用装任何东西。
    """
    base = app_dir()
    for candidate in (
        base / "mpv" / "mpv.exe",
        base / "mpv.exe",
        Path.cwd() / "mpv" / "mpv.exe",
    ):
        if candidate.is_file():
            return candidate
    found = shutil.which("mpv")
    return Path(found) if found else None


def desktop_dir() -> Path:
    """当前用户的桌面目录。

    不能直接拼 ~/Desktop —— Windows 上桌面经常被 OneDrive 或组策略重定向，
    那时 ~/Desktop 要么不存在要么是空的，学生端会一台都找不到视频。
    注册表里的值才是准的。
    """
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        with key:
            raw, _ = winreg.QueryValueEx(key, "Desktop")
        path = Path(os.path.expandvars(raw))
        if path.is_dir():
            return path
    except (OSError, FileNotFoundError, ImportError):
        pass

    home = Path.home()
    for fallback in (home / "Desktop", home / "OneDrive" / "Desktop"):
        if fallback.is_dir():
            return fallback
    return home / "Desktop"
