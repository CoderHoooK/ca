"""学生端的切片缓存与下载（P2P）。

一门课对应一个 StreamSession：它既是下载器，也是给本机 mpv 和其他同学提供
文件的「Provider」（见 common/segserver.py）。

下载策略，对应「播放这一段就先缓存这一段，缓存完继续缓存下一段」：

    · 以教师当前播放位置所在的段为起点，往后预缓存 PREFETCH_AHEAD 段
    · 本机 mpv 正在等的段是「紧急」的，永远最先下
    · 快没货了（起点的前两段还没到）就按顺序下；货够了就在窗口里**随机**挑
      一段下 —— 这样全班各自下的是不同的段，之后就能互相交换。
      如果大家都按同一个顺序下，所有人手里永远是同样的段，P2P 就没东西可换。

每一段的来源：先问 tracker（教师机）「谁有这一段」，向同学要；同学要不到
（没有、忙、超时、校验失败）才找教师机。教师机忙（503）就等一下重新问 tracker，
因为那时已经有同学拿到这一段了。

校验：每一段下完都对 manifest 里的 sha256，不对就丢掉。来自同学的数据不能信。
"""

from __future__ import annotations

import os
import random
import shutil
import threading
import time
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from common import config
from common.log import log
from common.package import MANIFEST_NAME, Manifest, PackageError, Segment

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# ↑ 必须绕开系统代理：局域网内部的请求被公司/学校代理接走就全完了

SourcesFn = Callable[[str, int], "list[tuple[str, int]]"]


def _url(host: str, port: int, pkg_id: str, rel: str) -> str:
    return f"http://{host}:{port}/p/{pkg_id}/{quote(rel)}"


def fetch_manifest(host: str, port: int, pkg_id: str, timeout: float = 20.0) -> Manifest:
    """从教师机取课程清单。教师机忙（503）或暂时连不上会重试，直到 timeout。"""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while True:
        try:
            with _opener.open(
                _url(host, port, pkg_id, MANIFEST_NAME), timeout=config.FETCH_TIMEOUT
            ) as resp:
                manifest = Manifest.from_json(resp.read())
            if manifest.id != pkg_id:
                raise PackageError("课程清单的 id 与请求的不一致")
            return manifest
        except (urllib.error.URLError, OSError, PackageError) as exc:
            last = exc
        if time.monotonic() >= deadline:
            raise RuntimeError(f"取不到课程清单：{last}")
        time.sleep(0.5)


def cleanup_cache(root: Path) -> None:
    """启动学生端时清掉上次留下的全部缓存。"""
    try:
        for child in Path(root).iterdir():
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
    except OSError:
        pass


class StreamSession:
    def __init__(
        self,
        manifest: Manifest,
        cache_dir: Path,
        teacher: tuple[str, int] | None,
        sources_fn: SourcesFn | None = None,
        have_fn: Callable[[str, int], None] | None = None,
        on_buffering: Callable[[bool], None] | None = None,
        on_progress: Callable[["StreamSession"], None] | None = None,
    ) -> None:
        self.manifest = manifest
        self.dir = Path(cache_dir) / manifest.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.teacher = teacher
        self._sources_fn = sources_fn
        self._have_fn = have_fn
        self._on_buffering = on_buffering
        self._on_progress = on_progress

        self._index = {seg.name: i for i, seg in enumerate(manifest.segments)}
        self._cond = threading.Condition()
        self._inflight: set[int] = set()
        self._urgent: set[int] = set()
        self._focus = 0
        self._waiting = 0
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []

        self.from_teacher = 0
        self.from_peers = 0
        self.bad_data = 0

        # 已经在缓存目录里的段（同一次运行里重播同一门课，不用重新下）
        self.have: set[int] = set()
        # 每一段是从哪来的：(种类, 主机)。种类 teacher / peer / cache（本机缓存里原来就有的）
        self.origin: dict[int, tuple[str, str]] = {}
        self.last_fetch: tuple[int, str, str] | None = None  # 最近下完的一段
        for i, seg in enumerate(manifest.segments):
            path = self.dir / seg.name
            if path.is_file() and path.stat().st_size == seg.size:
                self.have.add(i)
                self.origin[i] = ("cache", "")

    # ------------------------------------------------------------ Provider

    def file_path(self, rel: str) -> Path | None:
        idx = self._index.get(rel)
        path = self.dir / rel
        if idx is None:  # 字幕、字体：文件在就是下完了（下载时是先写 .part 再改名的）
            return path if rel in self.manifest.files() and path.is_file() else None
        with self._cond:
            if idx not in self.have:
                return None
        if not path.is_file():
            # 缓存被外部清掉了（比如另一个学生端实例启动时清缓存）：改回「没有」
            with self._cond:
                self.have.discard(idx)
            return None
        return path

    def wait_for(self, rel: str, timeout: float) -> Path | None:
        """本机 mpv 在等这个文件：标成紧急，阻塞到到手或超时。"""
        idx = self._index.get(rel)
        if idx is None:
            return self.file_path(rel)

        deadline = time.monotonic() + timeout
        self._set_waiting(+1)
        try:
            with self._cond:
                while not self._stop.is_set():
                    if idx in self.have:
                        break
                    self._urgent.add(idx)
                    self._cond.notify_all()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(min(remaining, 0.5))
        finally:
            self._set_waiting(-1)
        return self.file_path(rel)

    def _set_waiting(self, delta: int) -> None:
        with self._cond:
            before = self._waiting > 0
            self._waiting += delta
            after = self._waiting > 0
        if before != after and self._on_buffering:
            self._on_buffering(after)

    # ------------------------------------------------------------ 进度/焦点

    @property
    def total(self) -> int:
        return len(self.manifest.segments)

    @property
    def cached(self) -> int:
        with self._cond:
            return len(self.have)

    def snapshot(self) -> dict:
        """给界面看的快照：缓存量、当前片段、每段的来源分布。"""
        with self._cond:
            have = set(self.have)
            focus = self._focus
        origin = dict(self.origin)
        segs = self.manifest.segments
        letter = {"teacher": "t", "peer": "p", "cache": "c"}
        codes = "".join(
            letter.get(origin.get(i, ("cache", ""))[0], "c") if i in have else "."
            for i in range(len(segs))
        )
        cur = min(focus, len(segs) - 1)
        kind, host = origin.get(cur, ("", "")) if cur in have else ("", "")
        return {
            "have": len(have),
            "total": len(segs),
            "bytes": sum(segs[i].size for i in have),
            "total_bytes": sum(seg.size for seg in segs),
            "from_teacher": self.from_teacher,
            "from_peers": self.from_peers,
            "map": codes,
            "current": cur,
            "current_start": segs[cur].start,
            "current_end": segs[cur].start + segs[cur].duration,
            "current_cached": cur in have,
            "current_kind": kind,
            "current_host": host,
            "last": self.last_fetch,
        }

    def set_focus_time(self, seconds: float, urgent: bool = False) -> None:
        """教师当前播放到 seconds：预缓存窗口以这里为起点。"""
        idx = self.manifest.index_at(seconds)
        with self._cond:
            self._focus = idx
            if urgent and idx not in self.have:
                self._urgent.add(idx)
            self._cond.notify_all()

    def wait_ready(self, seconds: float, count: int = 1, timeout: float = 60.0) -> bool:
        """阻塞到 seconds 所在的段起连续 count 段都缓存好。"""
        first = self.manifest.index_at(seconds)
        wanted = range(first, min(first + count, self.total))
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._stop.is_set():
                missing = [i for i in wanted if i not in self.have]
                if not missing:
                    return True
                self._urgent.update(missing[:1])
                self._focus = first
                self._cond.notify_all()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.5))
        return False

    def subtitle_path(self) -> Path | None:
        for rel in self.manifest.subtitles:
            path = self.dir / rel
            if path.is_file():
                return path
        return None

    def fonts_dir(self) -> Path | None:
        d = self.dir / "fonts"
        return d if d.is_dir() and any(d.iterdir()) else None

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._workers:
            return
        self._stop.clear()
        for n in range(config.DOWNLOAD_WORKERS):
            t = threading.Thread(target=self._worker, name=f"dl-{n}", daemon=True)
            t.start()
            self._workers.append(t)

    def stop(self) -> None:
        """停下载。已缓存的文件保留，仍然可以给同学提供。"""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        for t in self._workers:
            t.join(timeout=2.0)
        self._workers = []

    # ------------------------------------------------------------ 下载

    def ensure_assets(self, timeout: float = 45.0) -> None:
        """字幕和字体：先问同学要（教师机上传名额有限，中文字体动辄十几 MB），
        再找教师机。取不到不致命，没字幕照样能播。

        问谁有第 focus 段，就问谁要：有切片的同学大概率也下好了字幕（不保证，
        没有就是 404，自动换下一个，最后找教师机），不用给资源单独做一套登记。
        """
        wanted = [rel for rel in self.manifest.subtitles + self.manifest.fonts
                  if not (self.dir / rel).is_file()]
        if not wanted:
            return
        deadline = time.monotonic() + timeout
        for rel in wanted:
            while not (self.dir / rel).is_file() and time.monotonic() < deadline:
                if self._stop.is_set():
                    return
                order: list[tuple[str, int]] = []
                if self._sources_fn is not None:
                    try:
                        peers = list(self._sources_fn(self.manifest.id, self._focus))
                    except Exception:
                        peers = []
                    random.shuffle(peers)
                    order += peers[: config.PEER_TRIES]
                if self.teacher is not None:
                    order.append((self.teacher[0], self.teacher[1]))
                if any(self._download_file(h, p, rel) == "ok" for h, p in order):
                    break
                time.sleep(0.4)
            if not (self.dir / rel).is_file():
                log(f"没取到 {rel}，继续（缺字幕/字体不影响播放，但画面上会没有字幕）")

    def _pick(self) -> int | None:
        """决定下一个下哪一段。调用时必须持有 self._cond。"""
        total = self.total
        self._urgent = {i for i in self._urgent if i not in self.have}

        urgent = sorted(i for i in self._urgent if i not in self._inflight)
        if urgent:
            return urgent[0]

        window = range(self._focus, min(self._focus + config.PREFETCH_AHEAD + 1, total))
        candidates = [i for i in window if i not in self.have and i not in self._inflight]
        if not candidates:
            return None
        starving = any(i not in self.have for i in (self._focus, self._focus + 1) if i < total)
        return candidates[0] if starving else random.choice(candidates)

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                n = self._pick()
                if n is None:
                    self._cond.wait(0.5)
                    continue
                self._inflight.add(n)

            ok = False
            try:
                ok = self._fetch(n)
            except Exception as exc:  # 一个线程的意外不该让整个下载停摆
                log(f"下载第 {n} 段出错：{exc!r}")
            finally:
                with self._cond:
                    self._inflight.discard(n)
                    if ok:
                        self.have.add(n)
                        self._urgent.discard(n)
                    self._cond.notify_all()

            if ok:
                if self._have_fn:
                    self._have_fn(self.manifest.id, n)
                if self._on_progress:
                    self._on_progress(self)
            else:
                # 全都失败了（教师机忙、同学没有…）：稍等再来，期间 tracker 的信息会更新
                self._stop.wait(random.uniform(0.3, 0.8))

    def _fetch(self, n: int) -> bool:
        seg = self.manifest.segments[n]
        peers: list[tuple[str, int]] = []
        if self._sources_fn is not None:
            try:
                peers = list(self._sources_fn(self.manifest.id, n))
            except Exception:
                peers = []
        random.shuffle(peers)

        order: list[tuple[str, int, bool]] = [
            (h, p, False) for h, p in peers[: config.PEER_TRIES]
        ]
        if self.teacher is not None:
            order.append((self.teacher[0], self.teacher[1], True))

        for host, port, is_teacher in order:
            if self._stop.is_set():
                return False
            if self._download_segment(host, port, seg) == "ok":
                kind = "teacher" if is_teacher else "peer"
                if is_teacher:
                    self.from_teacher += 1
                else:
                    self.from_peers += 1
                self.origin[n] = (kind, host)
                self.last_fetch = (n, kind, host)
                return True
        return False

    def _download_segment(self, host: str, port: int, seg: Segment) -> str:
        return self._download(host, port, seg.name, seg)

    def _download_file(self, host: str, port: int, rel: str) -> str:
        return self._download(host, port, rel, None)

    def _download(self, host: str, port: int, rel: str, seg: Segment | None) -> str:
        """下一个文件。返回 ok / busy / miss / bad / error。"""
        dest = self.dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + f".{threading.get_ident()}.part")
        digest = sha256()
        size = 0
        try:
            with _opener.open(_url(host, port, self.manifest.id, rel),
                              timeout=config.FETCH_TIMEOUT) as resp, open(part, "wb") as out:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if self._stop.is_set():
                        raise OSError("stopped")
            if seg is not None and (size != seg.size or digest.hexdigest() != seg.sha256):
                self.bad_data += 1
                log(f"{rel} 校验失败（来自 {host}:{port}），丢弃")
                return "bad"
            os.replace(part, dest)  # 原子改名：别人读到的要么没有，要么是完整的
            return "ok"
        except urllib.error.HTTPError as exc:
            return "busy" if exc.code == 503 else "miss"
        except (urllib.error.URLError, OSError, ValueError):
            return "error"
        finally:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
