"""切片课程包：manifest 格式、HLS 播放列表生成、课程库扫描、校验。

一门课是一个文件夹（由 tools/slice.py 生成）::

    lesson01/
      manifest.json
      seg_00000.ts  seg_00001.ts  ...       视频+音频切片（MPEG-TS，不重新编码）
      subs/subtitle.ass                      字幕（单独存，不在切片里）
      fonts/*.ttf                            字幕要用的字体（从 mkv 附件里提取）

为什么字幕单独存：ASS 字幕在切片里会被按时间硬切开，跨段的字幕行会丢；
而且字幕只有几百 KB，整个先下载下来最简单，mpv 用 --sub-file 加载，
时间轴和整部片子一致。

manifest.json 记录每段的时长、大小和 sha256。sha256 的作用：学生机的切片可能
来自别的学生机，校验通过才算数，一台机器的坏数据不会传染全班。
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "manifest.json"
FORMAT = 1


class PackageError(ValueError):
    """manifest 缺字段、格式不对，或者文件名不安全。"""


def safe_relpath(rel: str) -> str:
    """校验包内相对路径：不许绝对路径、不许 ..、不许反斜杠。

    切片服务会把 URL 里的路径当成包内文件名，这是防目录穿越的唯一一道关。
    """
    if (
        not rel
        or rel.startswith("/")
        or "\\" in rel
        or "\x00" in rel
        or any(part in ("", ".", "..") for part in rel.split("/"))
    ):
        raise PackageError(f"不安全的文件名：{rel!r}")
    return rel


@dataclass(frozen=True)
class Segment:
    name: str
    duration: float
    size: int
    sha256: str
    start: float = 0.0  # 这一段在整片里的起始时间（加载时累加算出，不存盘）


@dataclass
class Manifest:
    id: str
    title: str
    source_name: str          # 原始视频文件名，学生机桌面上有同名文件就直接用本地的
    duration: float
    segments: list[Segment]
    subtitles: list[str] = field(default_factory=list)  # 包内相对路径
    fonts: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- 查询

    def __post_init__(self) -> None:
        self._starts = [seg.start for seg in self.segments]

    def files(self) -> set[str]:
        """所有允许被服务的包内文件。"""
        names = {seg.name for seg in self.segments}
        names.update(self.subtitles)
        names.update(self.fonts)
        names.add(MANIFEST_NAME)
        return names

    def index_at(self, seconds: float) -> int:
        """第几段包含 seconds 这个时刻。越界则夹到首/尾段。"""
        if not self.segments:
            return 0
        i = bisect.bisect_right(self._starts, max(0.0, seconds)) - 1
        return min(max(i, 0), len(self.segments) - 1)

    @property
    def total_size(self) -> int:
        return sum(seg.size for seg in self.segments)

    def segment_index(self, name: str) -> int | None:
        for i, seg in enumerate(self.segments):
            if seg.name == name:
                return i
        return None

    # ------------------------------------------------------------- 序列化

    def to_dict(self) -> dict:
        return {
            "format": FORMAT,
            "id": self.id,
            "title": self.title,
            "source_name": self.source_name,
            "duration": round(self.duration, 3),
            "segments": [
                {"n": s.name, "d": round(s.duration, 6), "s": s.size, "h": s.sha256}
                for s in self.segments
            ],
            "subtitles": self.subtitles,
            "fonts": self.fonts,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1)

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        try:
            if data.get("format") != FORMAT:
                raise PackageError(f"不认识的切片包格式 {data.get('format')!r}")
            segments: list[Segment] = []
            start = 0.0
            for item in data["segments"]:
                duration = float(item["d"])
                if duration <= 0:
                    raise PackageError("切片时长必须大于 0")
                segments.append(
                    Segment(
                        name=safe_relpath(str(item["n"])),
                        duration=duration,
                        size=int(item["s"]),
                        sha256=str(item["h"]),
                        start=start,
                    )
                )
                start += duration
            if not segments:
                raise PackageError("切片包里没有任何切片")
            pkg_id = str(data["id"])
            if not pkg_id.isalnum():
                raise PackageError(f"不安全的课程 id：{pkg_id!r}")
            return cls(
                id=pkg_id,
                title=str(data.get("title", "")),
                source_name=str(data.get("source_name", "")),
                duration=float(data.get("duration", start)),
                segments=segments,
                subtitles=[safe_relpath(str(x)) for x in data.get("subtitles", [])],
                fonts=[safe_relpath(str(x)) for x in data.get("fonts", [])],
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, PackageError):
                raise
            raise PackageError(f"manifest 格式有误：{exc!r}") from exc

    @classmethod
    def from_json(cls, text: str | bytes) -> "Manifest":
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise PackageError(f"manifest 不是合法的 JSON：{exc}") from exc

    # ------------------------------------------------------------- HLS

    def m3u8(self) -> str:
        """VOD 播放列表。切片用相对路径，所以放在哪个 URL 前缀下都能用。

        用 manifest 自己生成而不是沿用 ffmpeg 写的 m3u8：学生机上只有 manifest，
        而且这样两端的播放列表保证一字不差。
        """
        target = math.ceil(max(seg.duration for seg in self.segments))
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        for seg in self.segments:
            lines.append(f"#EXTINF:{seg.duration:.6f},")
            lines.append(seg.name)
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"


def make_id(title: str, hashes: list[str]) -> str:
    """课程 id：标题 + 所有切片哈希。内容一样 id 就一样。"""
    digest = hashlib.sha1(title.encode("utf-8"))
    for h in hashes:
        digest.update(h.encode("ascii"))
    return digest.hexdigest()[:16]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(folder: Path) -> Manifest:
    path = Path(folder) / MANIFEST_NAME
    try:
        return Manifest.from_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PackageError(f"读不了 {path}：{exc}") from exc


def list_library(root: Path) -> list[tuple[Path, Manifest]]:
    """扫描课程库，返回 (文件夹, manifest)。损坏的包直接跳过，不影响别的课。"""
    found: list[tuple[Path, Manifest]] = []
    try:
        children = sorted(p for p in Path(root).iterdir() if p.is_dir())
    except OSError:
        return found
    for child in children:
        try:
            found.append((child, load_manifest(child)))
        except PackageError:
            continue
    return found


def verify_package(folder: Path, manifest: Manifest, check_hash: bool = True) -> list[str]:
    """检查课程包是否完整。返回问题列表，空表示没问题。"""
    folder = Path(folder)
    problems: list[str] = []
    for seg in manifest.segments:
        path = folder / seg.name
        if not path.is_file():
            problems.append(f"缺少切片 {seg.name}")
            continue
        if path.stat().st_size != seg.size:
            problems.append(f"{seg.name} 大小不对（{path.stat().st_size} ≠ {seg.size}）")
            continue
        if check_hash and sha256_file(path) != seg.sha256:
            problems.append(f"{seg.name} 内容校验失败")
    for rel in manifest.subtitles + manifest.fonts:
        if not (folder / rel).is_file():
            problems.append(f"缺少文件 {rel}")
    return problems
