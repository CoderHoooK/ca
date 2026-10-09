"""LanVideoSync 学生端。

设计目标只有一个：**学生不需要进行任何操作**。

启动后自动完成：
    1. UDP 广播找教师端（找不到就每 2 秒重试，永不退出）
    2. 连上后做 PING/PONG 时间同步
    3. 等指令，收到就执行

教师端重启或者网络断开，都会自动回到第 1 步，学生不用重新打开程序。

界面是**可选的**：默认弹一个状态窗口（见 ui.py），学生看得到连没连上、
在播什么；连不上时可以点「扫描教师机」自己挑，或者手动输入 IP。
加 --silent 就完全没有界面，和以前一样在后台跑。

关键设计——「未来时间点起播」：
教师端发来的每条 PLAY/RESUME/SEEK 都带一个 start_at，那是**未来**的某个时刻。
学生端拿到后先把 mpv 加载好、定位好、停住，然后睡到 start_at 才 play()。
绝不能收到消息就立刻播——那样 38 台机器会各播各的。
"""

from __future__ import annotations

import asyncio
import itertools
import os
import sys
import threading
import time
import traceback
from pathlib import Path

# 允许直接 `python src/student/main.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect

from common import config, net, protocol, timesync
from common.log import log
from common.mpvctl import MPV, MPVError
from common.paths import cache_root, desktop_dir, find_mpv
from common.segserver import SegServer
from student import state as st
from student import streaming
from student.state import StudentState
from student.streaming import StreamSession

_instance_counter = itertools.count(1)


class Student:
    def __init__(self, cache_dir: Path | None = None) -> None:
        """cache_dir 给测试用；正常运行时 None，用默认缓存目录并清掉上次的旧缓存。"""
        self.mpv = MPV("student")
        self.clock = timesync.ClockSync()
        self.current_video: Path | None = None
        self._pending_task: asyncio.Task | None = None

        self.state = StudentState(mpv_ok=find_mpv() is not None)

        # 手动指定的教师机 (host, port)。None 表示自动搜索。
        self._pinned: tuple[str, int] | None = None
        # 界面线程通过它叫醒主循环：「别等了，按新的目标重来」
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scan_lock: asyncio.Lock | None = None
        self._last_deep_scan = float("-inf")

        # ---- 切片播放 ----
        if cache_dir is None:
            root = cache_root()
            streaming.cleanup_cache(root)  # 「下次启动学生端时清理」
            cache_dir = root
        # 每个实例一个子目录：同一台机器上开多个学生端（开发测试）时不会共用缓存
        self._cache_dir = Path(cache_dir) / f"{os.getpid()}-{next(_instance_counter)}"
        self._streams: dict[str, StreamSession] = {}   # 课程 id → 会话（含已停止但仍可提供的）
        self._stream: StreamSession | None = None      # 当前正在下载/播放的
        self._seg_server: SegServer | None = None
        self._ws = None                                # 当前连着的教师端 websocket
        self._source_waiters: dict[int, dict] = {}
        self._req_ids = itertools.count(1)
        self._wait_lock = threading.Lock()
        self._waiting_requests = 0

    # ------------------------------------------------------------- 本地视频

    @staticmethod
    def find_videos() -> dict[str, Path]:
        """桌面上认得出的视频，键是小写文件名——Windows 文件名不区分大小写。

        按后缀表遍历而不是 glob("*.mkv")：一是要认 mp4 等格式，二是
        `p.suffix.lower()` 比 glob 的大小写行为更可控（.MP4 也得认，
        老师从别人那拷来的文件名什么写法都有）。

        桌面目录不存在或读不了时返回空表，而不是抛异常——学生端不能因为
        这个崩，之后教师一播放就正常报 VIDEO_NOT_FOUND。
        """
        try:
            return {
                p.name.lower(): p
                for p in desktop_dir().iterdir()
                if p.is_file() and p.suffix.lower() in config.VIDEO_EXTS
            }
        except OSError as exc:
            log(f"读不了桌面目录：{exc}")
            return {}

    @staticmethod
    def find_subtitle(video: Path) -> Path | None:
        """同名 ASS 字幕。没有也不影响播放，plan 明确要求不许因此崩。"""
        subtitle = video.with_suffix(".ass")
        return subtitle if subtitle.is_file() else None

    # --------------------------------------------------------------- 指令处理

    async def handle(self, message: dict, ws) -> None:
        cmd = message.get("cmd")

        if cmd == protocol.PLAY:
            await self._on_play(message, ws)
        elif cmd == protocol.PAUSE:
            await asyncio.to_thread(self._safe, self.mpv.pause)
            if self.state.play in (st.PLAYING, st.WAITING):
                self.state.play = st.PAUSED
        elif cmd == protocol.RESUME:
            self._schedule_seek(message, resume=True)
        elif cmd == protocol.SEEK:
            self._schedule_seek(message, resume=bool(message.get("resume", True)))
        elif cmd == protocol.SOURCES_REPLY:
            self._on_sources_reply(message)
        elif cmd == protocol.STOP:
            self._cancel_pending()
            self._stop_stream()
            self.current_video = None
            await asyncio.to_thread(self._safe, self.mpv.stop)
            self.state.play = st.IDLE
            self.state.video = ""
            self.state.teacher_position = None
        elif cmd == protocol.HEARTBEAT:
            self.state.teacher_position = _to_float(message.get("position"))
            await asyncio.to_thread(self._on_heartbeat, message)

    async def _on_play(self, message: dict, ws) -> None:
        name = message.get("video", "")
        videos = self.find_videos()
        self.state.desktop_videos = len(videos)
        video = videos.get(name.lower())

        package = message.get("package")
        if video is None and isinstance(package, dict):
            # 桌面上没有，但教师放的是切片课程：从教师机/同学那里拉切片来播
            await self._on_play_stream(message, package)
            return

        if video is None:
            # 找不到就报告，别弹错误框、别崩。教师端会显示有多少台没找到。
            log(f"桌面上找不到视频 {name!r}，回报 VIDEO_NOT_FOUND")
            self.current_video = None
            self._cancel_pending()
            self._stop_stream()
            await asyncio.to_thread(self._safe, self.mpv.stop)
            self.state.play = st.NOT_FOUND
            self.state.video = name
            self.state.has_subtitle = False
            await ws.send(protocol.encode({"cmd": protocol.VIDEO_NOT_FOUND, "video": name}))
            return

        subtitle = self.find_subtitle(video)
        log(f"播放 {video.name}（字幕：{subtitle.name if subtitle else '无'}）")
        self._stop_stream()
        self.current_video = video
        self.state.video = video.name
        self.state.has_subtitle = subtitle is not None
        self.state.stream = False
        self.state.play = st.LOADING

        self._cancel_pending()
        self._pending_task = asyncio.create_task(
            self._load_and_start(
                video, subtitle, float(message.get("position", 0.0)), float(message["start_at"])
            )
        )

    def _schedule_seek(self, message: dict, resume: bool) -> None:
        if not self.mpv.running:
            log("收到定位指令，但本地还没有在播放的视频，忽略")
            return
        self._cancel_pending()
        self._pending_task = asyncio.create_task(
            self._seek_and_start(
                float(message.get("position", 0.0)), float(message["start_at"]), resume
            )
        )

    # --------------------------------------------------------------- 同步播放

    async def _load_and_start(
        self, video: Path, subtitle: Path | None, position: float, start_at: float
    ) -> None:
        try:
            # 以暂停态加载并定位。加载 4K MKV 可能要好几秒，所以必须在
            # 睡到 start_at 之前就做掉，不能等醒了再加载。
            #
            # start() 返回时保证「已加载 + 已定位 + 已停稳」，所以这里返回
            # 之后直接睡到 start_at 就行，play() 一定会立刻生效。
            await asyncio.to_thread(self.mpv.start, video, subtitle, position)
        except MPVError as exc:
            log(f"启动 mpv 失败：{exc}")
            self.state.play = st.ERROR
            return

        self.state.play = st.WAITING
        await self._sleep_until(start_at)
        await asyncio.to_thread(self._safe, self.mpv.play)
        self.state.play = st.PLAYING

    async def _seek_and_start(self, position: float, start_at: float, resume: bool) -> None:
        try:
            await asyncio.to_thread(self._safe, self.mpv.pause)
            if self._stream is not None:
                # 切片模式：先把目标位置那一段拉到，再 seek。不然 mpv 会在新位置上
                # 干等，这段时间里画面是黑的。等不到（网络差）也照样 seek，由缓冲兜底。
                self._stream.set_focus_time(position, urgent=True)
                await asyncio.to_thread(self._stream.wait_ready, position, 1, 30.0)
            await asyncio.to_thread(self.mpv.seek, position)
        except MPVError as exc:
            # 定位失败也**不能直接退出**：那样学生端会一直停在暂停态，
            # 而心跳见「已暂停」就跳过纠偏，等于永久掉队。
            # 位置交给心跳去纠，这里先把播放状态对上。
            log(f"定位失败：{exc}，位置改由心跳纠偏")

        await self._sleep_until(start_at)
        if resume:
            await asyncio.to_thread(self._safe, self.mpv.play)
            self.state.play = st.PLAYING
        else:
            self.state.play = st.PAUSED

    async def _sleep_until(self, start_at: float) -> None:
        """睡到教师的 start_at 时刻。用同步后的时钟比，不能用本地 time.time()。"""
        delay = start_at - self.clock.teacher_now()
        if delay > 0:
            await asyncio.sleep(delay)

    # ----------------------------------------------------------------- 纠偏

    def _on_heartbeat(self, message: dict) -> None:
        """按教师端的心跳到正确位置。

        教师端说「我这边在 server_time 时刻位于 position」，换算到此刻
        应该在 position + 已经过去的时间。偏差小于阈值就不动——
        频繁 seek 会让画面一直抽搐，比轻微不同步更难看。
        """
        elapsed = self.clock.teacher_now() - float(message["server_time"])
        target = float(message["position"]) + elapsed

        # 切片模式：预缓存窗口跟着教师的位置走（暂停时也跟，位置不会动而已）
        if self._stream is not None:
            self._stream.set_focus_time(target)

        if self._pending_task is not None and not self._pending_task.done():
            return  # 正在执行 PLAY/SEEK 的等待，别和它抢 mpv
        if not self.mpv.running or self.mpv.query_paused():
            return
        if not message.get("playing"):
            return
        if self.state.buffering:
            # mpv 正在等切片，位置不动是因为没货而不是掉队。这时 seek 只会让它
            # 重新发请求、白白堆积；等货到了、画面动起来再纠偏。
            return

        current = self.mpv.get_position()
        if current is None:
            return

        drift = current - target
        if abs(drift) < config.DRIFT_THRESHOLD:
            return

        log(f"偏差 {drift:+.2f}s，纠偏到 {target:.2f}s")
        try:
            self.mpv.seek(target)
        except MPVError as exc:
            log(f"纠偏失败：{exc}")

    # ------------------------------------------------------------ 切片播放

    async def _on_play_stream(self, message: dict, package: dict) -> None:
        title = str(package.get("title") or message.get("video", ""))
        log(f"桌面上没有 {message.get('video')!r}，改为从教师机拉切片播放《{title}》")
        self.current_video = None
        self.state.video = title
        self.state.has_subtitle = False
        self.state.stream = True
        self.state.play = st.LOADING
        self.state.stream_have = self.state.from_teacher = self.state.from_peers = 0
        self.state.stream_total = 0
        self._cancel_pending()
        self._pending_task = asyncio.create_task(
            self._load_stream_and_start(
                package, float(message.get("position", 0.0)), float(message["start_at"])
            )
        )

    async def _load_stream_and_start(self, package: dict, position: float, start_at: float) -> None:
        host = self.state.teacher_host
        try:
            manifest = await asyncio.to_thread(
                streaming.fetch_manifest, host, int(package["http_port"]), str(package["id"])
            )
            session = self._open_stream(manifest, host, int(package["http_port"]))
            session.set_focus_time(position, urgent=True)
            await asyncio.to_thread(session.ensure_assets)
            self.state.has_subtitle = session.subtitle_path() is not None

            # 先等起播位置的那一段到手再叫 mpv 加载。不然 mpv 的加载会卡在
            # 等第一个切片上，超过它自己的加载超时就报错退出了。
            # mpv 加载时总要读第 0 段来探测流信息，从中途开始也一样，所以两段都等。
            ready = await asyncio.to_thread(
                session.wait_ready, 0.0, 1, config.PLAY_WAIT_TIMEOUT
            )
            if ready and position > 0:
                session.set_focus_time(position, urgent=True)
                ready = await asyncio.to_thread(
                    session.wait_ready, position, 1, config.PLAY_WAIT_TIMEOUT
                )
            if not ready:
                raise MPVError("等不到起播位置的切片（教师机和同学都没有给）")

            extra: list[str] = list(config.MPV_STREAM_ARGS)
            fonts = session.fonts_dir()
            if fonts is not None:
                extra.append("--sub-fonts-dir=" + str(fonts))
            url = self._ensure_seg_server().play_url(manifest.id)
            await asyncio.to_thread(
                self.mpv.start, url, session.subtitle_path(), position, extra_args=extra
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"切片播放启动失败：{exc}")
            self.state.play = st.ERROR
            return

        self.state.play = st.WAITING
        await self._sleep_until(start_at)
        await asyncio.to_thread(self._safe, self.mpv.play)
        self.state.play = st.PLAYING

    def _open_stream(self, manifest, host: str, port: int) -> StreamSession:
        """取得这门课的会话。同一门课已经有就复用（里面的缓存还在）。"""
        self._ensure_seg_server()
        old = self._stream
        if old is not None and old.manifest.id != manifest.id:
            old.stop()
        session = self._streams.get(manifest.id)
        if session is None:
            session = StreamSession(
                manifest, self._cache_dir, (host, port),
                sources_fn=self.request_sources,
                have_fn=self.notify_have,
                on_buffering=self._on_buffering,
                on_progress=self._on_stream_progress,
            )
            self._streams[manifest.id] = session
        else:
            session.teacher = (host, port)
        self._stream = session
        self.state.stream_total = session.total
        self._on_stream_progress(session)
        session.start()
        # 缓存里已有的段（重播同一门课）也告诉 tracker，同学才能找我要
        if session.have:
            self.notify_have(manifest.id, *sorted(session.have))
        return session

    def _stop_stream(self) -> None:
        """停止下载。已缓存的文件保留，仍然可以给同学提供。"""
        session, self._stream = self._stream, None
        if session is not None:
            session.stop()
        self.state.stream = False
        self.state.buffering = False

    def _ensure_seg_server(self) -> SegServer:
        """第一次需要时才启动切片服务：不用切片的学生机不会多开一个监听端口，
        也就不会因此触发防火墙弹窗。"""
        if self._seg_server is None:
            self._seg_server = SegServer(
                self._streams.get,
                max_uploads=config.PEER_MAX_UPLOADS,
                on_wait=self._on_wait,
            ).start()
            self._send_peer_hello()
        return self._seg_server

    def _on_wait(self, waiting: bool) -> None:
        """mpv 开始/结束等某个切片。可能同时有几个请求在等，所以要计数。"""
        with self._wait_lock:
            self._waiting_requests += 1 if waiting else -1
            self.state.buffering = self._waiting_requests > 0

    def _on_buffering(self, buffering: bool) -> None:
        pass  # 缓冲状态由 SegServer 的 on_wait 统一维护，这里留给以后扩展

    def stream_info(self) -> dict | None:
        """界面用：当前切片播放的缓存详情；不是切片播放时返回 None。"""
        session = self._stream
        return session.snapshot() if session is not None else None

    def _on_stream_progress(self, session: StreamSession) -> None:
        if session is self._stream:
            self.state.stream_have = session.cached
            self.state.from_teacher = session.from_teacher
            self.state.from_peers = session.from_peers

    # ---- 和教师端 tracker 的通信（下载线程调用，所以都要转交给事件循环）----

    def _send_threadsafe(self, message: dict) -> bool:
        ws, loop = self._ws, self._loop
        if ws is None or loop is None:
            return False
        try:
            asyncio.run_coroutine_threadsafe(ws.send(protocol.encode(message)), loop)
            return True
        except RuntimeError:
            return False

    def _send_peer_hello(self) -> None:
        if self._seg_server is not None:
            self._send_threadsafe(
                {"cmd": protocol.PEER_HELLO, "http_port": self._seg_server.port}
            )

    def notify_have(self, pkg_id: str, *indexes: int) -> None:
        """告诉教师机：我又缓存好了这几段，别的同学可以来要。"""
        self._send_threadsafe({"cmd": protocol.HAVE, "pkg": pkg_id, "add": list(indexes)})

    def request_sources(self, pkg_id: str, n: int, timeout: float = 1.5) -> list[tuple[str, int]]:
        """问 tracker：第 n 段哪些同学有？阻塞调用，在下载线程里用。"""
        req = next(self._req_ids)
        slot = {"event": threading.Event(), "sources": []}
        self._source_waiters[req] = slot
        try:
            if not self._send_threadsafe(
                {"cmd": protocol.SOURCES, "req": req, "pkg": pkg_id, "n": [n]}
            ):
                return []
            slot["event"].wait(timeout)
            return slot["sources"]
        finally:
            self._source_waiters.pop(req, None)

    def _on_sources_reply(self, message: dict) -> None:
        slot = self._source_waiters.get(message.get("req"))
        if slot is None:
            return
        found: list[tuple[str, int]] = []
        for entry in (message.get("sources") or {}).values():
            for addr in entry:
                host, _, port = str(addr).rpartition(":")
                if host and port.isdigit():
                    found.append((host, int(port)))
        slot["sources"] = found
        slot["event"].set()

    # ----------------------------------------------------------------- 杂项

    def _cancel_pending(self) -> None:
        """取消上一次的等待任务。

        老师快速连点播放时，会有多个任务同时在等各自的 start_at，
        不取消的话它们会排队去抢同一个 mpv。
        """
        if self._pending_task is not None and not self._pending_task.done():
            self._pending_task.cancel()
        self._pending_task = None

    @staticmethod
    def _safe(fn, *args) -> None:
        """mpv 命令失败不该让整个连接循环挂掉——记录一下就算了。"""
        try:
            fn(*args)
        except MPVError as exc:
            log(f"mpv 操作失败：{exc}")

    # ------------------------------------------------- 给界面线程用的控制接口
    #
    # 下面三个方法可以从任何线程调用（Qt 主线程）。它们不直接动任何状态，
    # 只是把活儿转交给学生端自己的事件循环去做，避免两个线程同时改连接状态。

    def request_scan(self) -> None:
        """立刻做一次深度扫描，结果写进 state.teachers。"""
        self._call_soon(lambda: asyncio.create_task(self._manual_scan()))

    def connect_to(self, host: str, port: int | None = None) -> None:
        """手动指定教师机（扫描列表里选的，或者自己输入的 IP）。

        会断开当前连接并改连这台；断线后也只重连这台，直到调用 use_auto()。
        """
        host = host.strip()
        if not host:
            return
        self._call_soon(lambda: self._pin(host, port or config.WS_PORT))

    def use_auto(self) -> None:
        """取消手动指定，恢复自动搜索。"""
        self._call_soon(lambda: self._pin(None, 0))

    def _call_soon(self, fn) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(fn)

    def _pin(self, host: str | None, port: int) -> None:
        # 以下都在事件循环线程里执行
        self._pinned = (host, port) if host else None
        self.state.pinned = host is not None
        self.state.attempts = 0
        self.state.last_error = ""
        log(f"改为手动连接 {host}:{port}" if host else "恢复自动搜索教师机")
        if self._wake is not None:
            self._wake.set()

    async def _manual_scan(self) -> None:
        await self._scan(deep=True, manual=True)

    async def _scan(self, deep: bool, manual: bool = False) -> list[net.Teacher]:
        """扫描一次并更新界面状态。同一时刻只跑一个扫描。"""
        assert self._scan_lock is not None
        if self._scan_lock.locked():
            return []
        async with self._scan_lock:
            self.state.scanning = True
            if manual:
                self.state.scan_note = "正在扫描…"
            log(f"开始{'深度' if deep else ''}扫描教师机")
            try:
                found = await asyncio.to_thread(net.scan, None, deep)
            except Exception as exc:
                log(f"扫描出错：{exc!r}")
                found = []
            finally:
                self.state.scanning = False

            log(
                f"扫描结束，找到 {len(found)} 台："
                + (", ".join(t.label for t in found) if found else "无")
            )
            # 手动扫描总是更新列表（包括「没找到」，让学生看到明确的结果）；
            # 自动扫描只在有收获时才更新，免得每 2 秒把列表清空闪一下。
            if manual or found:
                self.state.teachers = found
            if manual:
                self.state.scan_note = (
                    f"找到 {len(found)} 台教师机" if found
                    else "没有找到教师机。请确认教师端已打开、两台机器在同一网络，"
                         "或者手动输入教师机 IP。"
                )
            return found

    # ------------------------------------------------------------------ 主循环

    async def _find_target(self) -> tuple[str, int] | None:
        """决定这一轮连哪台。手动指定的优先；否则自动搜索。"""
        if self._pinned is not None:
            return self._pinned

        self.state.conn = st.SEARCHING
        found = await self._scan(deep=False)

        # 普通广播连着几轮都没结果，说明广播多半被挡了，自动换深度扫描。
        # 隔一段时间才重复，别让每台学生机每 2 秒都往网段里扫一遍。
        after = config.AUTO_DEEP_SCAN_AFTER
        if (
            not found
            and after
            and self.state.attempts >= after
            and asyncio.get_running_loop().time() - self._last_deep_scan
            >= config.AUTO_DEEP_SCAN_INTERVAL
        ):
            self._last_deep_scan = asyncio.get_running_loop().time()
            log("广播连续没有应答，改用深度扫描")
            found = await self._scan(deep=True)

        if not found:
            return None
        return found[0].host, found[0].port

    async def _sleep_or_wake(self, seconds: float) -> None:
        """睡一会儿，但界面一发来新指令（手动连接）就立刻醒。"""
        assert self._wake is not None
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        self._wake.clear()

    async def _session(self, host: str, port: int) -> None:
        """连一台教师机并一直处理它的指令，直到断开。"""
        state = self.state
        state.conn = st.CONNECTING
        state.teacher_host, state.teacher_port, state.teacher_name = host, port, ""
        log(f"教师端在 {host}:{port}，连接中…")

        # proxy=None 是必须的：websockets 15+ 默认读系统代理环境变量，
        # 局域网直连会被错误地当成需要走代理。
        async with connect(f"ws://{host}:{port}", open_timeout=5, proxy=None) as ws:
            self.clock = await timesync.sync(ws)
            state.conn = st.CONNECTED
            state.attempts = 0
            state.last_error = ""
            state.rtt, state.offset = self.clock.rtt, self.clock.offset
            state.teacher_name = self.clock.teacher_name
            log(
                f"已连接。RTT {self.clock.rtt * 1000:.1f}ms，"
                f"时钟偏差 {self.clock.offset:+.3f}s"
            )

            # 重连后 tracker 的记录是空的：把我的切片服务端口和已缓存的段重新报上去
            self._ws = ws
            status_task = asyncio.create_task(self._status_loop(ws))
            self._send_peer_hello()
            for pkg_id, session in self._streams.items():
                if session.have:
                    self.notify_have(pkg_id, *sorted(session.have))

            try:
                async for raw in ws:
                    try:
                        message = protocol.decode(raw)
                    except ValueError:
                        continue
                    try:
                        await self.handle(message, ws)
                    except Exception:
                        # 单条指令出错不能断掉连接
                        log("处理指令出错：\n" + traceback.format_exc())
            except asyncio.CancelledError:
                # 学生改选了别的教师机或者退出：先正常说再见（1000）。
                # 直接取消的话 websockets 会以 1011「内部错误」关闭，
                # 教师端日志里就是一串堆栈。
                await ws.close()
                raise
            finally:
                self._ws = None
                status_task.cancel()

    def _status_message(self) -> dict:
        """给教师端「学生机列表」看的当前状态。"""
        s = self.state
        play = s.play
        # 学生自己关掉了播放窗口：别还报「播放中」
        if play in (st.PLAYING, st.PAUSED, st.WAITING) and not self.mpv.running:
            play = st.IDLE
        return {
            "cmd": protocol.STATUS,
            "name": net.machine_name(),
            "play": play,
            "stream": bool(s.stream),
            "have": s.stream_have,
            "total": s.stream_total,
            "from_teacher": s.from_teacher,
            "from_peers": s.from_peers,
            "buffering": bool(s.buffering),
        }

    async def _status_loop(self, ws) -> None:
        """变了就立刻报，没变也每 3 秒报一次（教师端据此知道我还活着、状态没丢）。"""
        last: dict | None = None
        last_sent = 0.0
        try:
            while True:
                message = self._status_message()
                now = time.monotonic()
                if message != last or now - last_sent >= 3.0:
                    await ws.send(protocol.encode(message))
                    last, last_sent = message, now
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            return  # 连接断了，_session 那边会处理

    async def run_forever(self) -> None:
        """找教师端 → 连上 → 执行指令，断了就回到第一步，永不退出。"""
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._scan_lock = asyncio.Lock()

        try:
            await self._run_loop()
        finally:
            # 退出（Ctrl+C / 关窗口）时别留下孤儿 mpv 窗口
            self._cancel_pending()
            self.mpv.quit()
            for session in self._streams.values():
                session.stop()
            if self._seg_server is not None:
                self._seg_server.stop()

    async def _run_loop(self) -> None:
        assert self._wake is not None
        state = self.state

        while True:
            try:
                target = await self._find_target()
                if self._wake.is_set():  # 扫描期间学生手动选了别的，按新的来
                    self._wake.clear()
                    continue
                if target is None:
                    state.attempts += 1
                    state.last_error = "没有找到教师机"
                    log("没找到教师端，稍后重试")
                    await self._sleep_or_wake(config.REDISCOVER_INTERVAL)
                    continue

                host, port = target
                session = asyncio.create_task(self._session(host, port))
                waker = asyncio.create_task(self._wake.wait())
                try:
                    done, _ = await asyncio.wait(
                        {session, waker}, return_when=asyncio.FIRST_COMPLETED
                    )
                except asyncio.CancelledError:
                    # 整个学生端被关掉：别让连接任务变成孤儿继续跑
                    session.cancel()
                    waker.cancel()
                    raise
                waker.cancel()

                if session not in done:
                    # 学生在界面上改选了教师机：断开当前连接，立刻重来
                    session.cancel()
                    await asyncio.gather(session, return_exceptions=True)
                    self._wake.clear()
                    state.conn = st.SEARCHING
                    continue

                # 会话自己结束了：正常断开或出错
                was_connected = state.conn == st.CONNECTED
                exc = session.exception()
                if exc is not None:
                    raise exc
                log("教师端断开连接，稍后重连")
                state.conn = st.DISCONNECTED
                state.last_error = "与教师端的连接已断开"
                if not was_connected:
                    state.attempts += 1

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                was_connected = state.conn == st.CONNECTED
                state.conn = st.DISCONNECTED if was_connected else st.SEARCHING
                state.attempts += 0 if was_connected else 1
                state.last_error = f"{type(exc).__name__}: {exc}"
                log(f"连接断开（{type(exc).__name__}: {exc}），稍后重连")

            await self._sleep_or_wake(config.REDISCOVER_INTERVAL)


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def run() -> None:
    await Student().run_forever()


def main() -> None:
    args = sys.argv[1:]
    silent = "--silent" in args or "--nogui" in args
    log(f"LanVideoSync 学生端启动（{'无界面' if silent else '带界面'}）")

    if silent:
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            pass
        return

    # Qt 只在要用界面时才 import：--silent 模式（和测试）不依赖它
    from student.ui import run_gui

    run_gui(Student())


if __name__ == "__main__":
    main()
