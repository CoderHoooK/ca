"""打包成 exe（onedir 目录形式）。

    python build.py            两个都打
    python build.py teacher    只打教师端
    python build.py student    只打学生端

产物：
    dist/Teacher/Teacher.exe    教师端（带界面，老师用）
    dist/Student/Student.exe    学生端（无界面，学生用）
    两个目录里都放好了 mpv\\mpv.exe，拷到别人机器上不用再装任何东西。

为什么是 onedir 不是 onefile：onefile 每次启动都要把上百 MB 解压到临时目录，
学生机 38 台同时启动会明显变慢，而且杀毒软件特别爱扫这种行为。
onedir 就是一个文件夹，拷过去双击 exe 就行。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
MPV_SRC = ROOT / "mpv"
DIST = ROOT / "dist"
BUILD = ROOT / "build"

# 学生端也不带控制台：学生不该看到黑框，出问题看 Student.log。
# 想让它显示控制台方便排查，把下面这行改成 False 重打即可。
NOCONSOLE = True

TARGETS = {
    "teacher": {
        "name": "Teacher",
        "entry": SRC / "teacher" / "main.py",
    },
    "student": {
        "name": "Student",
        "entry": SRC / "student" / "main.py",
    },
}

USAGE = """LanVideoSync —— 局域网同步播放

{title}

怎么用
    双击本目录下的 {exe}

教师端
    1. 点「选择视频」选视频（同名的 .ass 字幕会自动带上）
       mkv、mp4、avi、mov、wmv、flv、webm、m4v、ts 都支持
    2. 点「▶ 同步播放」，所有学生机到同一时刻一起开始
    3. 「⏸ 暂停」「⏵ 继续」「⏹ 停止」全班跟着走
    4. 拖进度条，全班一起跳

学生端
    双击就行，什么都不用点。
    它会自己搜教师机，连上后跟着教师端播。
    教师端重启、网线掉了，它都会自己重连。

前提条件
    · 视频文件和同名字幕要提前拷到**每台学生机的桌面**上
      （程序只扫桌面，别的目录不看）
    · 教师机和学生机在同一个局域网里

出问题了看日志
    本目录下的 {logname}
"""


def check_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("没装 PyInstaller。先运行：pip install pyinstaller")


def build_one(target: str) -> Path:
    info = TARGETS[target]
    print(f"\n=== 打包 {info['name']} ===", flush=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--paths", str(SRC),
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--name", info["name"],
    ]
    if NOCONSOLE:
        cmd.append("--noconsole")
    # 学生端用不到 Qt，明确排掉，能省一百多 MB
    if target == "student":
        cmd += ["--exclude-module", "PySide6", "--exclude-module", "tkinter"]
    if target == "teacher":
        cmd += ["--exclude-module", "tkinter"]

    cmd.append(str(info["entry"]))
    subprocess.run(cmd, check=True, cwd=ROOT)

    out = DIST / info["name"]
    if not (out / f"{info['name']}.exe").is_file():
        sys.exit(f"打包结果不对，没找到 {out / (info['name'] + '.exe')}")
    return out


def copy_mpv(out: Path) -> None:
    """把 mpv 运行时拷到 exe 旁边。

    只拷 mpv.exe / mpv.com / *.dll —— mpv 的 Windows 包是绿色的，
    其余（doc、示例脚本、压缩包）都不用带。
    """
    dest = out / "mpv"
    dest.mkdir(exist_ok=True)

    needed = [MPV_SRC / "mpv.exe", MPV_SRC / "mpv.com"]
    needed += sorted(MPV_SRC.glob("*.dll"))

    if not (MPV_SRC / "mpv.exe").is_file():
        print(f"  !! {MPV_SRC / 'mpv.exe'} 不存在，跳过。")
        print("     这个包打出来没有播放器，学生机上放不了视频。")
        return

    for path in needed:
        if path.is_file():
            shutil.copy2(path, dest / path.name)
    total = sum(p.stat().st_size for p in dest.iterdir() if p.is_file())
    print(f"  mpv 已放入 {dest}（{total / 1024 / 1024:.0f} MB）")


def write_usage(out: Path, target: str) -> None:
    info = TARGETS[target]
    title = "教师端（老师在这台机器上操作）" if target == "teacher" else "学生端（学生机上双击即可）"
    text = USAGE.format(
        title=title,
        exe=f"{info['name']}.exe",
        logname=f"{info['name']}.log",
    )
    (out / "使用说明.txt").write_text(text, encoding="utf-8-sig")


def main() -> int:
    check_pyinstaller()

    wanted = sys.argv[1:] or list(TARGETS)
    for target in wanted:
        if target not in TARGETS:
            sys.exit(f"不认识的目标 {target!r}，只能是 {list(TARGETS)}")

    for target in wanted:
        out = build_one(target)
        copy_mpv(out)
        write_usage(out, target)
        print(f"  完成：{out}")

    print("\n全部完成，产物在 dist\\ 下：")
    for target in wanted:
        print(f"    dist\\{TARGETS[target]['name']}\\")
    return 0


if __name__ == "__main__":
    sys.exit(main())
