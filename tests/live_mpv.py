"""真 mpv 端到端测试。假 mpv 验证不了的协议细节，靠这个兜底。

    python tests/live_mpv.py

同一套检查跑两遍：tests/media/movie.mkv 和 tests/media/movie.mp4。
容器不同，但走的都是 mpv 的解复用/定位路径，mp4 得多验一遍——
学生端的后缀表放开之后，mp4 是和 mkv 一样的一等公民。

运行时会弹出 mpv 窗口，这是预期的——测的就是真实配置。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common.mpvctl import MPV, MPVError
from common.paths import find_mpv

MEDIA = ROOT / "tests" / "media"
SUBTITLE = MEDIA / "movie.ass"
DURATION = 20.0

# (标签, 视频文件)。字幕用 movie.ass —— movie.mp4.with_suffix(".ass")
# 正好也是它，所以两个容器共用同一个字幕，顺便验了这条推导。
FIXTURES = [("mkv", MEDIA / "movie.mkv"), ("mp4", MEDIA / "movie.mp4")]

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [ok]   {name}" + (f"  {detail}" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}  {detail}")


def wait_for(predicate, timeout: float, poll: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


def run_suite(label: str, video: Path) -> None:
    """对一个视频文件跑完整套检查。label 会加在每条检查名前面。"""
    print(f"\n=== {label}：{video.name} ===")

    mpv = MPV(f"livetest-{label}")
    try:
        print("启动 mpv（暂停态加载）")
        started = time.monotonic()
        mpv.start(video, SUBTITLE, position=5.0)
        elapsed = time.monotonic() - started
        check(f"{label}：mpv 启动并连上 IPC", mpv.running, f"耗时 {elapsed:.2f}s")
        check(f"{label}：start() 返回时处于暂停态（同步起播的前提）",
              mpv.query_paused() is True)

        # start() 的约定是「返回即就绪」：已加载、已定位、已停稳。
        # 所以这里不做任何等待，立刻读就该是对的。
        check(
            f"{label}：start() 返回时已定位到 5 秒（无需再等）",
            mpv.position is not None and abs(mpv.position - 5.0) < 0.5,
            f"position={mpv.position}",
        )
        check(
            f"{label}：订阅后收到位置推送",
            wait_for(lambda: mpv.position is not None, 5.0),
            f"position={mpv.position}",
        )

        duration = mpv.get_duration()
        check(
            f"{label}：读到时长为 20 秒",
            duration is not None and abs(duration - DURATION) < 1.0,
            f"duration={duration}",
        )

        print("播放 2 秒，看位置是否推进")
        # 起播必须立刻就生效。曾经用 --start= 定位时 mpv 会拖 1~2 秒才真正
        # 取消暂停（pause=false 回了 success 却没落地），全班起播时间就散了。
        play_at = time.monotonic()
        mpv.play()
        started = wait_for(lambda: mpv.query_paused() is False, 2.5)
        latency_ms = (time.monotonic() - play_at) * 1000
        check(f"{label}：play() 立刻生效（不再拖 1~2 秒）", started,
              f"耗时 {latency_ms:.0f}ms")

        before = mpv.get_position()
        time.sleep(2.0)
        after = mpv.get_position()
        check(
            f"{label}：播放中位置持续推进",
            before is not None and after is not None and after - before > 1.5,
            f"{before:.2f}s → {after:.2f}s",
        )

        print("暂停")
        mpv.pause()
        check(f"{label}：pause() 生效",
              wait_for(lambda: mpv.query_paused() is True, 3.0))
        paused_at = mpv.get_position()
        time.sleep(1.0)
        check(
            f"{label}：暂停后位置不再推进",
            paused_at is not None and abs(mpv.get_position() - paused_at) < 0.3,
            f"{paused_at:.2f}s → {mpv.get_position():.2f}s",
        )

        print("定位到 15 秒")
        mpv._seek_and_settle(15.0)
        check(
            f"{label}：seek 后位置落定在 15 秒",
            mpv.get_position() is not None
            and abs(mpv.get_position() - 15.0) < 0.5,
            f"position={mpv.position}",
        )
        check(f"{label}：定位后仍处于暂停态（start_at 起播的前提）",
              mpv.query_paused() is True)

        print("停止")
        mpv.stop()
        check(f"{label}：stop() 后 running 为 False", mpv.running is False)
        check(f"{label}：stop() 后句柄已释放", mpv._handle is None)

    except MPVError as exc:
        check(f"{label}：mpv 操作未报错", False, str(exc))
    finally:
        try:
            mpv.stop()
        except Exception:
            pass


def main() -> int:
    print("真 mpv 端到端测试")

    exe = find_mpv()
    print(f"\nmpv: {exe}")
    if exe is None:
        print("找不到 mpv.exe，跳过。把 mpv 放到 mpv\\mpv.exe 后重跑。")
        return 0

    check("测试字幕存在", SUBTITLE.is_file())

    available = []
    for label, video in FIXTURES:
        if video.is_file():
            check(f"{label} 测试视频存在", True, f"{video.stat().st_size} 字节")
            available.append((label, video))
        else:
            check(f"{label} 测试视频存在", False, f"缺少 {video}")

    for label, video in available:
        run_suite(label, video)

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}，失败 {len(FAILED)}")
    for name in FAILED:
        print(f"  失败: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
