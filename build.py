"""打包成 exe（onedir 目录形式）。

    python build.py            两个都打
    python build.py teacher    只打教师端
    python build.py student    只打学生端

产物：
    dist/Teacher/Teacher.exe    教师端（带界面，老师用）
    dist/Student/Student.exe    学生端（带状态窗口，加 --silent 则无界面）
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
sys.path.insert(0, str(SRC))
from common import config  # noqa: E402
MPV_SRC = ROOT / "mpv"
DIST = ROOT / "dist"
BUILD = ROOT / "build"

# 两个都不带控制台：学生不该看到黑框，出问题看 Student.log 或学生端界面里的「运行日志」。
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

    窗口里能看到：连没连上教师机、延迟、在播哪个视频、播到哪儿。
    点窗口右上角的 × 就是退出（播放窗口也一起关）。
    教师端关掉播放窗口，学生机的播放窗口也会跟着关。

    一直连不上怎么办（窗口里的「连接设置」，连续失败后会自动展开）：
      1. 点「扫描教师机」，列表里双击教师机就连
      2. 扫不到就手动输入教师机的 IP（教师端窗口顶部显示着）
      3. 想回到全自动，点「恢复自动搜索」

    不想要窗口（比如开机自启的机房机器），用 --silent 启动：
      Student.exe --silent

前提条件
    · 视频文件和同名字幕要提前拷到**每台学生机的桌面**上
      （程序只扫桌面，别的目录不看）
    · 教师机和学生机在同一个局域网里
{extra}
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
    # 学生端现在也有界面（PySide6），两边都要带 Qt。
    # websockets.sync.client 是扫描教师机时在函数里才 import 的，
    # 显式声明一下，免得哪个版本的 PyInstaller 漏掉它。
    cmd += ["--exclude-module", "tkinter", "--hidden-import", "websockets.sync.client"]

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


TEACHER_EXTRA = """
学生机桌面上没有视频时（可选）：播放切片课程
    1. 在你自己的电脑上用 tools\\slice.py 把电影切片（需要 ffmpeg）
    2. 把生成的课程文件夹整个拷到本目录下的「课程库」里
    3. 教师端点「选择切片课程」→ 选课 → 同步播放
    学生机桌面上有同名视频的照常用本地的，没有的会自动从教师机拉切片，
    学生机之间也会互相传。详见项目 README 的「切片课程」一节。
    防火墙：教师机和学生机都要允许入站（专用网络），不放行也能播，只是更慢。
"""

STUDENT_EXTRA = """
桌面上没有视频时
    教师端如果播放的是切片课程，学生端会自动从教师机和其他学生机拉切片来播，
    不用做任何操作。缓存在 %LOCALAPPDATA%\\LanVideoSync\\cache，下次启动时清空。
"""


def write_usage(out: Path, target: str) -> None:
    info = TARGETS[target]
    title = "教师端（老师在这台机器上操作）" if target == "teacher" else "学生端（学生机上双击即可）"
    text = USAGE.format(
        title=title,
        exe=f"{info['name']}.exe",
        logname=f"{info['name']}.log",
        extra=TEACHER_EXTRA if target == "teacher" else STUDENT_EXTRA,
    )
    if target == "teacher":
        library = out / config.LIBRARY_DIRNAME
        library.mkdir(exist_ok=True)
        (library / "把课程文件夹放在这里.txt").write_text(
            "tools\\slice.py 切好的课程文件夹，整个拷到这个目录里。\n"
            "教师端点「选择切片课程」就能看到。\n",
            encoding="utf-8-sig",
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
