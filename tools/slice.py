"""把一部电影切成「课程包」，供教师端分发。在你自己的电脑上运行，不是教师端的一部分。

    python tools/slice.py 电影.mkv
    python tools/slice.py 电影.mkv -o D:\\课程库 --segment-seconds 10
    python tools/slice.py 电影.mkv --sub 电影.ass
    python tools/slice.py --verify 课程库\\电影

切完把整个课程文件夹拷到教师机的「课程库」目录（Teacher.exe 旁边）就行。

它做的事（全部不重新编码，几秒到几十秒就能切完一部电影）：
    1. 视频+音频按关键帧切成 MPEG-TS 小段（默认约 10 秒一段）
    2. 字幕单独提取成 ASS：优先用你指定的 --sub，其次同目录同名 .ass，
       最后才是 mkv 里内封的文字字幕（图片字幕 PGS/VobSub 无法转换，会提示）
    3. 把 mkv 里附带的字体提取出来（ASS 字幕常常靠它们才能显示正确）
    4. 给每一段算 sha256，写 manifest.json

需要 ffmpeg 和 ffprobe。查找顺序：--ffmpeg 参数 → 环境变量 FFMPEG → 项目的
ffmpeg\\ 目录 → PATH。

限制：视频编码必须是 MPEG-TS 能装的（H.264 / H.265 / MPEG-2，也就是几乎所有
常见电影和番剧）。AV1、VP9 切不了，请先转码。音频如果不能直接装进 TS（比如
FLAC），会自动转成 AAC，视频仍然不动。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common.package import (  # noqa: E402
    MANIFEST_NAME,
    PackageError,
    Segment,
    Manifest,
    load_manifest,
    make_id,
    sha256_file,
    verify_package,
)

TEXT_SUB_CODECS = {"ass", "ssa", "subrip", "srt", "mov_text", "webvtt", "text"}
BITMAP_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
FONT_EXTS = {".ttf", ".otf", ".ttc"}


class SliceError(RuntimeError):
    pass


# ----------------------------------------------------------------- ffmpeg 定位


def find_tool(name: str, explicit: str | None = None) -> str:
    """找 ffmpeg / ffprobe。explicit 可以是可执行文件，也可以是所在目录。"""
    exe = name + (".exe" if sys.platform == "win32" else "")
    candidates: list[Path] = []
    if explicit:
        p = Path(explicit)
        candidates.append(p / exe if p.is_dir() else p.with_name(exe) if p.stem == "ffmpeg" else p)
    import os

    if os.environ.get("FFMPEG"):
        candidates.append(Path(os.environ["FFMPEG"]).with_name(exe))
    candidates += [ROOT / "ffmpeg" / exe, ROOT / "ffmpeg" / "bin" / exe]
    for c in candidates:
        if c.is_file():
            return str(c)
    found = shutil.which(name)
    if found:
        return found
    raise SliceError(
        f"找不到 {name}。请安装 ffmpeg 并加入 PATH，或者用 --ffmpeg 指定路径，"
        f"或者把它放到项目的 ffmpeg\\ 目录下。"
    )


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw
    )


# ----------------------------------------------------------------- 探测


def probe(ffprobe: str, source: Path) -> dict:
    result = run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)])
    if result.returncode != 0:
        raise SliceError(f"ffprobe 读不了 {source.name}：{result.stderr.strip()[-300:]}")
    return json.loads(result.stdout)


def describe(info: dict) -> None:
    for s in info["streams"]:
        tags = s.get("tags", {})
        extra = tags.get("language", "") + (" " + tags["title"] if tags.get("title") else "")
        print(f"    #{s['index']:<2} {s['codec_type']:<10} {s.get('codec_name', '?'):<20} {extra.strip()}")


# ----------------------------------------------------------------- 切片


def slice_video(ffmpeg: str, source: Path, out: Path, seg_seconds: float, audio_track: int | None) -> list[tuple[str, float]]:
    """返回 [(切片文件名, 时长)]。"""
    playlist = out / "index.m3u8"
    audio_map = ["-map", f"0:a:{audio_track}"] if audio_track is not None else ["-map", "0:a?"]

    def attempt(audio_codec: list[str]) -> subprocess.CompletedProcess:
        for old in out.glob("seg_*.ts"):
            old.unlink()
        return run([
            ffmpeg, "-v", "error", "-y", "-i", str(source),
            "-map", "0:v:0", *audio_map, "-sn", "-dn",
            "-c:v", "copy", *audio_codec,
            "-f", "hls", "-hls_time", str(seg_seconds),
            "-hls_playlist_type", "vod", "-hls_list_size", "0",
            "-hls_segment_type", "mpegts", "-start_number", "0",
            "-hls_segment_filename", str(out / "seg_%05d.ts"),
            str(playlist),
        ])

    result = attempt(["-c:a", "copy"])
    if result.returncode != 0:
        print("  音频不能直接装进 TS，改为转成 AAC（视频不受影响）…")
        result = attempt(["-c:a", "aac", "-b:a", "192k"])
    if result.returncode != 0:
        raise SliceError(
            "切片失败。常见原因是视频编码不是 H.264/H.265/MPEG-2（比如 AV1、VP9），"
            "请先转码。\n" + result.stderr.strip()[-600:]
        )

    segments: list[tuple[str, float]] = []
    duration = None
    for line in playlist.read_text(encoding="utf-8").splitlines():
        if line.startswith("#EXTINF:"):
            duration = float(line[8:].rstrip(",").split(",")[0])
        elif line and not line.startswith("#") and duration is not None:
            segments.append((line.strip(), duration))
            duration = None
    playlist.unlink()  # 播放列表由 manifest 生成，不留 ffmpeg 的这份
    if not segments:
        raise SliceError("ffmpeg 没有产出任何切片")
    return segments


# ----------------------------------------------------------------- 字幕和字体


def make_subtitle(ffmpeg: str, source: Path, info: dict, out: Path, args) -> str | None:
    """生成 subs/subtitle.ass，返回包内相对路径；没有字幕返回 None。"""
    if args.no_sub:
        return None
    target = out / "subs" / "subtitle.ass"

    def convert(src: Path) -> None:
        target.parent.mkdir(exist_ok=True)
        res = run([ffmpeg, "-v", "error", "-y", "-i", str(src), "-c:s", "ass", str(target)])
        if res.returncode != 0:
            raise SliceError(f"字幕转换失败：{res.stderr.strip()[-300:]}")

    def copy_ass(src: Path) -> None:
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(src, target)

    # 1) 用户明确指定
    explicit = Path(args.sub) if args.sub else None
    if explicit is not None:
        if not explicit.is_file():
            raise SliceError(f"字幕文件不存在：{explicit}")
        if explicit.suffix.lower() == ".ass":
            copy_ass(explicit)
        else:
            convert(explicit)
        print(f"  字幕：使用指定的 {explicit.name}")
        return "subs/subtitle.ass"

    # 2) 同目录同名 .ass
    sibling = source.with_suffix(".ass")
    if sibling.is_file() and args.sub_track is None:
        copy_ass(sibling)
        print(f"  字幕：使用同目录的 {sibling.name}")
        return "subs/subtitle.ass"

    # 3) mkv 内封
    subs = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
    text = [(n, s) for n, s in enumerate(subs) if s.get("codec_name") in TEXT_SUB_CODECS]
    bitmap = [s for s in subs if s.get("codec_name") in BITMAP_SUB_CODECS]

    if args.sub_track is not None:
        if not 0 <= args.sub_track < len(subs):
            raise SliceError(f"没有第 {args.sub_track} 条字幕轨（共 {len(subs)} 条，从 0 数起）")
        n, chosen = args.sub_track, subs[args.sub_track]
        if chosen.get("codec_name") in BITMAP_SUB_CODECS:
            raise SliceError("这条是图片字幕（PGS/VobSub），无法转成 ASS。请换一条或自备 .ass")
    elif text:
        n, chosen = text[0]
    else:
        if bitmap:
            print("  ⚠ 字幕：只有图片字幕（PGS/VobSub），无法转换，已跳过。可以用 --sub 指定一个 .ass")
        else:
            print("  字幕：没有（视频里没有内封字幕，同目录也没有同名 .ass）")
        return None

    target.parent.mkdir(exist_ok=True)
    res = run([ffmpeg, "-v", "error", "-y", "-i", str(source), "-map", f"0:s:{n}", "-c:s", "ass", str(target)])
    if res.returncode != 0:
        raise SliceError(f"提取内封字幕失败：{res.stderr.strip()[-300:]}")
    lang = chosen.get("tags", {}).get("language", "?")
    print(f"  字幕：提取内封第 {n} 条（{chosen.get('codec_name')}，语言 {lang}）"
          + (f"；另有 {len(text) - 1} 条文字字幕可用 --sub-track 选" if len(text) > 1 else ""))
    return "subs/subtitle.ass"


def extract_fonts(ffmpeg: str, source: Path, info: dict, out: Path) -> list[str]:
    """提取 mkv 附件里的字体。返回包内相对路径。"""
    fonts: list[str] = []
    attachments = [s for s in info["streams"] if s["codec_type"] == "attachment"]
    if not attachments:
        return fonts
    (out / "fonts").mkdir(exist_ok=True)
    for s in attachments:
        name = s.get("tags", {}).get("filename") or s.get("tags", {}).get("FILENAME")
        if not name:
            continue
        name = Path(name).name  # 去掉任何路径成分
        if Path(name).suffix.lower() not in FONT_EXTS:
            continue
        dest = out / "fonts" / name
        # ffmpeg 要求必须带输出文件才肯运行；附件在打开输入时就已经写出，
        # 所以输出给 -f null 就行，真正的产物是 -dump_attachment 的那个文件。
        run([ffmpeg, "-v", "quiet", "-y", f"-dump_attachment:{s['index']}", str(dest),
             "-i", str(source), "-t", "0", "-f", "null", "-"])
        if dest.is_file() and dest.stat().st_size > 0:
            fonts.append(f"fonts/{name}")
    if fonts:
        print(f"  字体：提取了 {len(fonts)} 个（{', '.join(Path(f).name for f in fonts[:4])}"
              f"{'…' if len(fonts) > 4 else ''}）")
    return fonts


# ----------------------------------------------------------------- 主流程


def build(source: Path, out_root: Path, args) -> Path:
    ffmpeg = find_tool("ffmpeg", args.ffmpeg)
    ffprobe = find_tool("ffprobe", args.ffmpeg)

    print(f"\n源文件：{source.name}")
    info = probe(ffprobe, source)
    describe(info)
    if not any(s["codec_type"] == "video" for s in info["streams"]):
        raise SliceError("文件里没有视频流")

    title = args.title or source.stem
    folder_name = re.sub(r'[\\/:*?"<>|]', "_", title).strip() or "course"
    out = out_root / folder_name
    if out.exists():
        if not args.force:
            raise SliceError(f"{out} 已经存在。要覆盖请加 --force")
        shutil.rmtree(out)

    # 先在临时目录里做，全部成功才挪过去，失败不留半成品
    out_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".slice_", dir=out_root))
    try:
        print(f"  切片中（每段约 {args.segment_seconds:g} 秒，不重新编码）…")
        raw = slice_video(ffmpeg, source, work, args.segment_seconds, args.audio_track)

        segments, start = [], 0.0
        for name, duration in raw:
            path = work / name
            segments.append(Segment(name, duration, path.stat().st_size, sha256_file(path), start))
            start += duration

        subtitle = make_subtitle(ffmpeg, source, info, work, args)
        fonts = extract_fonts(ffmpeg, source, info, work)

        manifest = Manifest(
            id=make_id(title, [s.sha256 for s in segments]),
            title=title,
            source_name=source.name,
            duration=start,
            segments=segments,
            subtitles=[subtitle] if subtitle else [],
            fonts=fonts,
        )
        (work / MANIFEST_NAME).write_text(manifest.to_json(), encoding="utf-8")
        problems = verify_package(work, manifest, check_hash=False)
        if problems:
            raise SliceError("生成的课程包不完整：" + "; ".join(problems))
        work.rename(out)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise

    durations = [s.duration for s in segments]
    print(
        f"\n完成：{out}\n"
        f"  {len(segments)} 段，时长 {start / 60:.1f} 分钟，共 {manifest.total_size / 1048576:.1f} MB\n"
        f"  每段 {min(durations):.1f}~{max(durations):.1f} 秒"
    )
    if max(durations) > args.segment_seconds * 2.5:
        print(f"  ⚠ 有的段明显比 {args.segment_seconds:g} 秒长：源视频关键帧间隔太大，"
              "不重新编码就只能在关键帧处切。段越长，缓冲越久。")
    print(f"\n下一步：把整个文件夹 {out.name}\\ 拷到教师机的「课程库」目录。")
    return out


def cmd_verify(folder: Path) -> int:
    try:
        manifest = load_manifest(folder)
    except PackageError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"校验 {folder.name}（{len(manifest.segments)} 段，含 sha256）…")
    problems = verify_package(folder, manifest, check_hash=True)
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("✓ 完整，所有切片校验通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把电影切成课程包（不重新编码）", formatter_class=argparse.RawTextHelpFormatter
    )
    ap.add_argument("source", nargs="?", help="要切的视频文件")
    ap.add_argument("-o", "--out", default=str(ROOT / "课程库"), help="输出目录（默认 项目/课程库）")
    ap.add_argument("--segment-seconds", type=float, default=10.0, help="每段目标时长，默认 10 秒")
    ap.add_argument("--title", help="课程名，默认用文件名")
    ap.add_argument("--sub", help="指定字幕文件（.ass 最好，.srt 会转成 ass）")
    ap.add_argument("--sub-track", type=int, help="用 mkv 内封的第 N 条字幕轨（从 0 数起）")
    ap.add_argument("--no-sub", action="store_true", help="不带字幕")
    ap.add_argument("--audio-track", type=int, help="只保留第 N 条音轨（从 0 数起），默认全部保留")
    ap.add_argument("--ffmpeg", help="ffmpeg 可执行文件或所在目录")
    ap.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    ap.add_argument("--verify", metavar="课程文件夹", help="校验一个已有的课程包")
    args = ap.parse_args(argv)

    if args.verify:
        return cmd_verify(Path(args.verify))
    if not args.source:
        ap.error("请指定要切的视频文件，或用 --verify 校验课程包")
    if args.segment_seconds < 2:
        ap.error("--segment-seconds 不能小于 2")
    source = Path(args.source)
    if not source.is_file():
        print(f"找不到文件：{source}")
        return 2
    try:
        build(source, Path(args.out), args)
    except SliceError as exc:
        print(f"\n✗ {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
