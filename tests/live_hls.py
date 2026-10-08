"""真 mpv 验证切片播放：教师机（限速）→ 学生机缓存 → 本机 HTTP → mpv。

    python tests/live_hls.py

需要 ffmpeg 和 mpv（在 PATH 里，或者项目的 mpv\\ 目录）。没有就跳过。
这里直接用 mpv 命令行而不是 mpvctl（后者只能在 Windows 上跑），目的是验证
「mpv 能不能通过我们的 HLS 小服务正确播放、缺切片时会不会等而不是报错」。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from common import config
from common.package import load_manifest
from common.paths import find_mpv
from common.segserver import FolderProvider, SegServer
from student.streaming import StreamSession

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  [{'ok' if condition else 'FAIL'}]   {name}" + (f"  {detail}" if detail else ""))


class SlowProvider(FolderProvider):
    """教师机：每个切片都额外延迟，模拟带宽不够。"""

    def __init__(self, folder, manifest, delay: float) -> None:
        super().__init__(folder, manifest)
        self.delay = delay
        self.served: list[str] = []

    def file_path(self, rel):
        path = super().file_path(rel)
        if path is not None and rel.endswith(".ts"):
            self.served.append(rel)
            time.sleep(self.delay)
        return path


def make_big_video(path: Path) -> None:
    """90 秒、约 28MB、每 2 秒一个关键帧。要足够大，才能看出 mpv 预读有没有被限住。"""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", "90", "-c:v", "libx264",
         "-preset", "ultrafast", "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5M",
         "-g", "60", "-c:a", "aac", "-b:a", "128k", str(path)],
        check=True,
    )


def run_mpv(mpv: str, url: str, *extra: str, timeout: float = 120.0) -> tuple[int, str, float]:
    t0 = time.monotonic()
    proc = subprocess.run(
        [mpv, "--no-config", "--vo=null", "--ao=null", "--untimed", "--msg-level=all=info", *config.MPV_STREAM_ARGS, *extra, url],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout + proc.stderr, time.monotonic() - t0


def main() -> int:
    mpv = find_mpv()
    if mpv is None or shutil.which("ffmpeg") is None:
        print("跳过：需要 mpv 和 ffmpeg")
        return 0
    mpv = str(mpv)
    import slice as slicer  # tools/slice.py

    tmp = Path(tempfile.mkdtemp(prefix="lvs_live_"))
    try:
        lib = tmp / "lib"
        big = tmp / "big.mkv"
        make_big_video(big)
        rc = slicer.main([str(big), "-o", str(lib), "--force", "--segment-seconds", "6"])
        check("切片成功", rc == 0)
        folder = lib / "big"
        manifest = load_manifest(folder)
        n = len(manifest.segments)

        # 教师机：每段延迟 0.7 秒
        provider = SlowProvider(folder, manifest, delay=0.4)
        teacher = SegServer({manifest.id: provider}.get, max_uploads=8).start()

        print("\n场景 1：从头播到尾，学生机只预缓存很少，mpv 会追上下载进度")
        config.PREFETCH_AHEAD = 1
        waits: list[bool] = []
        session = StreamSession(manifest, tmp / "cache1", ("127.0.0.1", teacher.port))
        student = SegServer({manifest.id: session}.get, max_uploads=4,
                            on_wait=lambda w: waits.append(w))
        student.start()
        session.set_focus_time(0, urgent=True)
        session.start()
        session.ensure_assets()
        code, out, took = run_mpv(mpv, student.play_url(manifest.id), "--speed=4")
        session.stop()
        check("mpv 播到结尾、正常退出", code == 0 and "End of file" in out, f"exit={code}")
        check("没有因为缺切片而报错", "Failed to open" not in out and "error" not in out.lower().replace("0 errors", ""), "")
        check("mpv 确实在等切片（on_wait 被触发）", True in waits, f"等待次数 {waits.count(True)}")
        check("全部切片都缓存到了本地", session.cached == n, f"{session.cached}/{n}")
        check("全部来自教师机（没有同学）", session.from_teacher == n and session.from_peers == 0)
        check("耗时合理（4 倍速播 90 秒 ≈ 23 秒，加上等切片，不是卡死）", took < 60, f"{took:.1f}s")
        student.stop()

        print("\n场景 2：从 60 秒开始播（中途加入），mpv 预读被限住，不会把整部片子拉下来")
        config.PREFETCH_AHEAD = 6
        provider.served.clear()
        session = StreamSession(manifest, tmp / "cache2", ("127.0.0.1", teacher.port))
        student = SegServer({manifest.id: session}.get, max_uploads=4).start()
        session.set_focus_time(60, urgent=True)
        session.start()
        code, out, took = run_mpv(mpv, student.play_url(manifest.id), "--start=60", "--length=5")
        time.sleep(1.0)  # 让预取窗口也跑完
        session.stop()
        first = manifest.index_at(60)
        got = {manifest.segment_index(r) for r in provider.served}
        check("从 60 秒起播成功", code == 0, f"exit={code}")
        # 允许：开头几段（mpv 加载时探测流信息）+ 起播位置往后的窗口
        allowed = set(range(0, 4)) | set(range(first - 1, first + config.PREFETCH_AHEAD + 5))  # HLS seek 会落在关键帧之前
        check("只下载了探测段和起播位置附近的切片，而不是整部片子",
              got <= allowed and len(got) < n - 3,
              f"共 {n} 段，下载了 {sorted(got)}，起播段 {first}")
        student.stop()

        print("\n场景 3：字幕 + 字体（从课程包里读，通过 --sub-file / --sub-fonts-dir）")
        sub_src = ROOT / "tests" / "media" / "movie.mkv"
        rc = slicer.main([str(sub_src), "-o", str(lib), "--force", "--segment-seconds", "2"])
        folder2 = lib / "movie"
        manifest2 = load_manifest(folder2)
        check("带字幕的课程切片成功", rc == 0 and bool(manifest2.subtitles), str(manifest2.subtitles))
        sess2 = StreamSession(manifest2, tmp / "cache3", None)
        # 教师机直接用文件夹（学生缓存同样的目录结构）
        shutil.copytree(folder2, sess2.dir, dirs_exist_ok=True)
        sess2.have = set(range(len(manifest2.segments)))
        srv = SegServer({manifest2.id: sess2}.get, max_uploads=4).start()
        sub = sess2.subtitle_path()

        def shot(name: str, *extra: str) -> bytes | None:
            out_dir = tmp / name
            out_dir.mkdir()
            code, _out, _ = run_mpv(mpv, srv.play_url(manifest2.id), "--start=1", "--frames=1",
                                    "--vo=image", f"--vo-image-outdir={out_dir}", *extra)
            files = sorted(out_dir.glob("*.jpg"))
            return files[0].read_bytes() if code == 0 and files else None

        plain = shot("plain", "--sid=no")
        withsub = shot("withsub", f"--sub-file={sub}")
        check("无字幕、有字幕都能出画面", plain is not None and withsub is not None)
        check("带 --sub-file 后画面上确实渲染出了字幕（两张图不同）", plain != withsub)
        srv.stop()
        teacher.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}，失败 {len(FAILED)}")
    for name in FAILED:
        print(f"  失败: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
