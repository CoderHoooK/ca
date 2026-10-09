"""LanVideoSync 教师端。

架构上有两点值得说明：

1. **教师机的 mpv 是播放位置的唯一真相来源。** 老师看到的进度条、广播给
   学生的心跳，都来自这一个 mpv 实例。老师点什么，学生就跟着做什么，
   不存在「教师端以为在 125 秒、学生端实际在 127 秒」这种分歧。

2. **教师机自己也走 start_at 那套流程。** 点同步播放时，教师机不是立刻播，
   而是和学生机一样等到 start_at 才 play()。这样「同步」是字面意义上的同步，
   老师屏幕上看到的画面就是学生屏幕上应该有的画面。

线程模型：Qt 主线程跑 UI 和 mpv 控制，WebSocket 服务端跑在另一个线程的
asyncio 事件循环里，两边用 run_coroutine_threadsafe 通信。
"""

from __future__ import annotations

import asyncio
import random
import sys
import threading
import time
from pathlib import Path

# 允许直接 `python src/teacher/main.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from common import config, net, protocol
from common.log import log
from common.mpvctl import MPV, MPVError
from common.package import Manifest
from common.paths import app_dir
from common.segserver import FolderProvider, SegServer
from teacher.library import LibraryDialog
from teacher.roster import Roster

SLIDER_MAX = 1000


def format_time(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return "--:--"
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _video_filter() -> str:
    """文件选择框的过滤器，按 config.VIDEO_EXTS 生成。

    末尾留一个「所有文件」兜底：万一老师手上是表里没有的冷门格式，
    也不该被过滤器挡在门外——学生端扫不到的格式到时候会自己报出来，
    总比在这儿就选不中强。
    """
    patterns = " ".join(f"*{ext}" for ext in config.VIDEO_EXTS)
    return f"视频 ({patterns});;所有文件 (*)"


class Server(threading.Thread):
    """WebSocket 服务端 + UDP 发现应答。跑在独立线程的事件循环里。"""

    def __init__(self, on_message) -> None:
        super().__init__(daemon=True, name="ws-server")
        self._clients: set = set()
        # tracker：每个学生连接 → {"host", "port"（它的切片服务端口）, "have"（课程id → 已缓存段号集合）}
        self._peers: dict = {}
        # 学生机列表：连接 → {"host", "name", 以及学生上报的 STATUS 字段}
        self._students: dict = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_message = on_message
        self.ready = threading.Event()

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:
            log(f"教师端服务异常退出：{exc!r}")
            self.ready.set()

    async def _serve(self) -> None:
        await net.start_responder()
        # ping_interval 保持默认：心跳只证明教师端还活着，断了不容易发现
        async with serve(self._on_connect, "0.0.0.0", config.WS_PORT) as server:
            self.ready.set()
            log(f"教师端就绪 ws://0.0.0.0:{config.WS_PORT}，UDP 发现端口 {config.DISCOVERY_PORT}")
            await server.serve_forever()

    # 不能叫 _handle：Python 3.13 起 threading.Thread 内部有个同名属性
    # （_ThreadHandle），会把这个方法盖掉，结果所有学生连接都报
    # "'_thread._ThreadHandle' object is not callable"。
    async def _on_connect(self, ws) -> None:
        peer = ws.remote_address[0] if ws.remote_address else "?"
        with self._lock:
            self._clients.add(ws)
            self._students[ws] = {"host": peer, "name": "", "since": time.time()}
        log(f"学生端接入 {peer}")

        try:
            async for raw in ws:
                try:
                    message = protocol.decode(raw)
                except ValueError:
                    continue

                if message.get("cmd") == protocol.PING:
                    # 立刻回，中间不要做任何别的事——回复越晚，时钟偏差算得越不准
                    await ws.send(
                        protocol.encode(
                            {
                                "cmd": protocol.PONG,
                                "t0": message.get("t0"),
                                "t_teacher": time.time(),
                                "name": net.machine_name(),
                                "id": net.INSTANCE_ID,
                            }
                        )
                    )
                    continue

                cmd = message.get("cmd")
                if cmd == protocol.STATUS:
                    self._student_status(ws, message)
                    continue
                if cmd == protocol.PEER_HELLO:
                    self._peer_hello(ws, peer, message)
                    continue
                if cmd == protocol.HAVE:
                    self._peer_have(ws, message)
                    continue
                if cmd == protocol.SOURCES:
                    await self._peer_sources(ws, message)
                    continue

                self._on_message(message, peer)
        except ConnectionClosed:
            # 学生机断网、关机、切换教师机都会走到这里，属于正常现象，
            # 别让 websockets 打一串「connection handler failed」堆栈吓到人
            pass
        finally:
            with self._lock:
                self._clients.discard(ws)
                self._peers.pop(ws, None)
                self._students.pop(ws, None)
            log(f"学生端断开 {peer}")

    # ---------------------------------------------------------------- tracker
    #
    # 教师机不转发任何视频数据，只回答「第 n 段谁有」。真正的数据是学生机之间
    # （以及学生向教师机）直接走 HTTP 拉的，见 common/segserver.py。

    def _student_status(self, ws, message: dict) -> None:
        """记下学生机上报的状态。字段都当成不可信输入，类型不对就丢掉。"""
        def num(key: str) -> int:
            value = message.get(key, 0)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

        with self._lock:
            info = self._students.get(ws)
            if info is None:
                return
            info.update(
                name=str(message.get("name", ""))[:64],
                play=str(message.get("play", ""))[:16],
                stream=bool(message.get("stream", False)),
                have=num("have"),
                total=num("total"),
                from_teacher=num("from_teacher"),
                from_peers=num("from_peers"),
                buffering=bool(message.get("buffering", False)),
                seen=time.time(),
            )

    def students_snapshot(self) -> list[dict]:
        """当前连着的学生机（给界面用的拷贝）。还没上报状态的只有 host。"""
        with self._lock:
            rows = [dict(info, id=id(ws)) for ws, info in self._students.items()]
        rows.sort(key=lambda r: (r.get("name") or r["host"], r["host"], r["since"]))
        return rows

    def _peer_hello(self, ws, peer: str, message: dict) -> None:
        try:
            port = int(message.get("http_port", 0))
        except (TypeError, ValueError):
            return
        if not 0 < port < 65536:
            return
        with self._lock:
            info = self._peers.setdefault(ws, {"have": {}})
            info["host"], info["port"] = peer, port

    def _peer_have(self, ws, message: dict) -> None:
        pkg = str(message.get("pkg", ""))
        add = message.get("add") or []
        with self._lock:
            info = self._peers.setdefault(ws, {"have": {}})
            have = info["have"].setdefault(pkg, set())
            for n in add:
                if isinstance(n, int):
                    have.add(n)

    async def _peer_sources(self, ws, message: dict) -> None:
        pkg = str(message.get("pkg", ""))
        wanted = [n for n in (message.get("n") or []) if isinstance(n, int)]
        sources: dict[str, list[str]] = {}
        with self._lock:
            for n in wanted:
                owners = [
                    f"{info['host']}:{info['port']}"
                    for other, info in self._peers.items()
                    if other is not ws and "port" in info and n in info["have"].get(pkg, ())
                ]
                random.shuffle(owners)  # 打乱，别让所有人都去找同一个同学
                sources[str(n)] = owners[:4]
        await ws.send(
            protocol.encode(
                {
                    "cmd": protocol.SOURCES_REPLY,
                    "req": message.get("req"),
                    "pkg": pkg,
                    "sources": sources,
                }
            )
        )

    def swarm_progress(self, pkg: str, total: int) -> tuple[int, float]:
        """(提供了切片服务的学生机数, 它们平均缓存了这门课的百分之几)。"""
        with self._lock:
            peers = [i for i in self._peers.values() if "port" in i]
            if not peers or total <= 0:
                return len(peers), 0.0
            done = sum(len(i["have"].get(pkg, ())) for i in peers)
        return len(peers), done / (len(peers) * total)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def broadcast(self, message: dict) -> None:
        """从 Qt 主线程调用。"""
        if self._loop is None:
            return
        payload = protocol.encode(message)
        asyncio.run_coroutine_threadsafe(self._broadcast(payload), self._loop)

    async def _broadcast(self, payload: str) -> None:
        with self._lock:
            targets = list(self._clients)
        if not targets:
            return
        await asyncio.gather(
            *(ws.send(payload) for ws in targets), return_exceptions=True
        )


class Window(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.mpv = MPV("teacher")
        self.video: Path | None = None
        # 选了切片课程时用 package（和 video 互斥）
        self.package: tuple[Path, Manifest] | None = None
        self._http: SegServer | None = None
        self._providers: dict[str, FolderProvider] = {}
        self._missing: set[str] = set()
        self._mpv_was_running = False  # 上一次 tick 时播放窗口在不在，用来发现老师叉掉了它
        self._missing_lock = threading.Lock()

        self.server = Server(self._on_student_message)
        self.server.start()

        self._build_ui()

        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(500)

        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.timeout.connect(self._heartbeat)
        self._heartbeat_timer.start(int(config.HEARTBEAT_INTERVAL * 1000))

    # -------------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        self.setWindowTitle("LanVideoSync 教师端")
        self.resize(760, 520)

        self._video_label = QLabel("未选择视频")
        self._server_label = QLabel("服务启动中…")
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, SLIDER_MAX)
        self._slider.sliderReleased.connect(self._on_seek)
        self._time_label = QLabel("--:-- / --:--")
        self._clients_label = QLabel("已连接学生机：0")
        self._status_label = QLabel("就绪")

        self._choose_button = QPushButton("选择视频")
        self._package_button = QPushButton("选择切片课程")
        self._play_button = QPushButton("▶ 同步播放")
        self._pause_button = QPushButton("⏸ 暂停")
        self._resume_button = QPushButton("⏵ 继续")
        self._stop_button = QPushButton("⏹ 停止")

        self._choose_button.clicked.connect(self._choose_video)
        self._package_button.clicked.connect(self._choose_package)
        self._play_button.clicked.connect(self._play)
        self._pause_button.clicked.connect(self._pause)
        self._resume_button.clicked.connect(self._resume)
        self._stop_button.clicked.connect(self._stop)

        layout = QVBoxLayout(self)
        layout.addWidget(self._server_label)
        layout.addWidget(self._video_label)
        layout.addWidget(self._slider)
        layout.addWidget(self._time_label)

        buttons = QHBoxLayout()
        for button in (
            self._choose_button,
            self._package_button,
            self._play_button,
            self._pause_button,
            self._resume_button,
            self._stop_button,
        ):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        # 切片起播提前量：学生机要先拉到第一批切片才能开播，人多/带宽紧就调大。改了自动记住。
        self._settings = QSettings(QSettings.IniFormat, QSettings.UserScope, "LanVideoSync", "Teacher")
        lo, hi = config.STREAM_PLAY_LEAD_RANGE
        saved = self._settings.value("stream_play_lead", config.STREAM_PLAY_LEAD, type=float)
        self._lead_spin = QSpinBox()
        self._lead_spin.setRange(lo, hi)
        self._lead_spin.setSingleStep(5)
        self._lead_spin.setSuffix(" 秒")
        self._lead_spin.setValue(int(min(max(saved, lo), hi)))
        self._lead_spin.setToolTip(
            "点「同步播放」后，等多少秒全班一起开始（只对切片课程有用）。\n"
            "学生多、教师机带宽小，就调大一点，让学生机有时间缓冲好第一批切片。"
        )
        self._lead_spin.valueChanged.connect(
            lambda v: self._settings.setValue("stream_play_lead", float(v))
        )
        lead_row = QHBoxLayout()
        lead_row.addWidget(QLabel("切片起播提前量"))
        lead_row.addWidget(self._lead_spin)
        lead_row.addWidget(QLabel("（学生多、带宽紧就调大）"))
        lead_row.addStretch(1)
        layout.addLayout(lead_row)

        # 增强轻声：教室音箱常有「无信号自动待机」，对白轻的时候会睡过去，得去调音量才醒。
        # 打开后 mpv 把轻声自动拉高。只影响教师机自己的声音，学生机不变。
        self._boost_check = QCheckBox("增强轻声（教室音箱老是自己没声音时打开）")
        self._boost_check.setChecked(self._settings.value("quiet_boost", False, type=bool))
        self._boost_check.setToolTip(
            "把电影里的轻声（安静的对白、片头片尾）自动拉高。\n"
            "音箱有「无信号自动待机」时，信号太弱会被当成没声音而休眠；\n"
            "打开这个就不容易睡过去，后排也更容易听清对白。\n"
            "代价：音量起伏变小，爆炸声不会比对白大很多。播放中也可以随时开关。\n"
            "只影响教师机自己的声音，学生机不变。"
        )
        self._boost_check.toggled.connect(self._on_boost_toggled)
        layout.addWidget(self._boost_check)

        layout.addWidget(self._clients_label)
        self._roster = Roster()
        layout.addWidget(self._roster, 1)
        layout.addWidget(self._status_label)

    # --------------------------------------------------------------- UI 刷新

    def _tick(self) -> None:
        if self.server.ready.is_set():
            self._server_label.setText(
                f"教师端：{net.local_ip() or '?'}:{config.WS_PORT}"
            )

        self._clients_label.setText(f"已连接学生机：{self.server.client_count}")
        self._roster.update_rows(self.server.students_snapshot())

        self._notice_player_closed()

        position = self.mpv.get_position()
        duration = self.mpv.get_duration()

        if position is not None and duration:
            if not self._slider.isSliderDown():
                self._slider.setValue(int(position / duration * SLIDER_MAX))
            self._time_label.setText(
                f"{format_time(position)} / {format_time(duration)}"
            )

        with self._missing_lock:
            missing = len(self._missing)

        if missing:
            self._status_label.setText(f"⚠ {missing} 台学生机没有找到视频")
        elif self.mpv.running:
            text = "已暂停" if self.mpv.query_paused() else "播放中"
            if self.package is not None:
                _, manifest = self.package
                peers, fraction = self.server.swarm_progress(
                    manifest.id, len(manifest.segments)
                )
                if peers:
                    text += f"　｜　切片已分发到 {peers} 台学生机，平均缓存 {fraction:.0%}"
            self._status_label.setText(text)
        elif self.video or self.package:
            self._status_label.setText("就绪（尚未开始播放）")

    def _notice_player_closed(self) -> None:
        """老师直接叉掉了播放窗口：等同于点「停止」，让学生机的播放窗口也关掉。

        点「停止」「换视频」时我们自己会关 mpv，那条路径在 _stop 里已经广播过 STOP，
        并把 _mpv_was_running 置回 False，所以这里不会重复广播。
        """
        running = self.mpv.running
        if self._mpv_was_running and not running:
            self.server.broadcast({"cmd": protocol.STOP})
            self._slider.setValue(0)
            self._time_label.setText("--:-- / --:--")
            log("教师机播放窗口被关闭，广播 STOP")
        self._mpv_was_running = running

    def _on_boost_toggled(self, on: bool) -> None:
        self._settings.setValue("quiet_boost", on)
        self._apply_quiet_boost()

    def _apply_quiet_boost(self) -> None:
        """把复选框的状态应用到正在播放的 mpv。没在播放就什么都不做（下次开播时会应用）。"""
        if self.mpv.running:
            self._safe(self.mpv.set_quiet_boost, self._boost_check.isChecked())

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        """加载视频时会阻塞 UI 线程，先把按钮禁掉，免得老师重复点击。"""
        for button in (
            self._choose_button,
            self._package_button,
            self._play_button,
            self._pause_button,
            self._resume_button,
            self._stop_button,
        ):
            button.setEnabled(enabled)

    # ------------------------------------------------------------------ 操作

    def _choose_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择视频", str(Path.home() / "Desktop"), _video_filter()
        )
        if not path:
            return
        self.video = Path(path)
        self.package = None
        self._video_label.setText(f"视频：{self.video.name}")
        subtitle = self.video.with_suffix(".ass")
        has_sub = subtitle.is_file()
        self._set_status(f"就绪（字幕：{'已找到' if has_sub else '无'}）")

    def _choose_package(self) -> None:
        dialog = LibraryDialog(self, app_dir() / config.LIBRARY_DIRNAME)
        if dialog.exec() != LibraryDialog.Accepted or dialog.selected is None:
            return
        folder, manifest = dialog.selected
        self.package = (folder, manifest)
        self.video = None
        self._video_label.setText(
            f"切片课程：{manifest.title}（{len(manifest.segments)} 段，"
            f"{manifest.total_size / 1048576:.0f} MB）"
        )
        self._set_status(f"就绪（字幕：{'已找到' if manifest.subtitles else '无'}）")

    def _ensure_http(self) -> SegServer:
        """第一次播放切片课程时才起 HTTP 服务。"""
        if self._http is None:
            self._http = SegServer(
                self._providers.get,
                max_uploads=config.TEACHER_MAX_UPLOADS,
                port=config.HTTP_PORT,
            ).start()
        return self._http

    def _play_package(self) -> None:
        assert self.package is not None
        folder, manifest = self.package
        http = self._ensure_http()
        self._providers[manifest.id] = FolderProvider(folder, manifest)

        subtitle = folder / manifest.subtitles[0] if manifest.subtitles else None
        extra = list(config.MPV_STREAM_ARGS)
        if manifest.fonts:
            extra.append("--sub-fonts-dir=" + str(folder / "fonts"))

        self._set_status("正在加载课程…")
        self._set_buttons_enabled(False)
        QApplication.processEvents()
        try:
            # 教师机自己也通过本机的切片服务播放，和学生走同一条路径
            self.mpv.start(http.play_url(manifest.id), subtitle, position=0.0, extra_args=extra)
            self._apply_quiet_boost()
        except MPVError as exc:
            QMessageBox.critical(self, "课程加载失败", str(exc))
            self._set_status("加载失败，详见日志")
            return
        finally:
            self._set_buttons_enabled(True)

        with self._missing_lock:
            self._missing.clear()

        # 学生机要先把第一段拉下来才能开播，提前量比本地播放长
        lead = float(self._lead_spin.value())
        start_at = self._start_at_after(lead)
        self._run_at(start_at, self.mpv.play)
        self.server.broadcast(
            {
                "cmd": protocol.PLAY,
                "video": manifest.source_name or manifest.title,
                "position": 0.0,
                "start_at": start_at,
                "package": {
                    "id": manifest.id,
                    "title": manifest.title,
                    "http_port": http.port,
                },
            }
        )
        log(f"广播 PLAY 切片课程《{manifest.title}》，{lead:g}s 后起播")

    @staticmethod
    def _start_at_after(seconds: float) -> float:
        """生成一个未来时刻。必须留出提前量——学生端要在这段时间里加载视频。"""
        return time.time() + seconds

    def _run_at(self, teacher_time: float, callback) -> None:
        """在教师时钟的 teacher_time 时刻执行。教师机自己也遵守 start_at。"""
        delay_ms = max(0, int((teacher_time - time.time()) * 1000))
        QTimer.singleShot(delay_ms, callback)

    def _play(self) -> None:
        if self.package is not None:
            self._play_package()
            return
        if self.video is None:
            QMessageBox.warning(self, "提示", "请先选择一个视频或切片课程。")
            return

        subtitle = self.video.with_suffix(".ass")
        if not subtitle.is_file():
            subtitle = None

        # start() 会阻塞到「加载完 + 定位好 + 停稳」，所以先把状态提示画出来、
        # 把按钮禁掉，免得老师以为卡死了去重复点击。
        self._set_status("正在加载视频…")
        self._set_buttons_enabled(False)
        QApplication.processEvents()
        try:
            # 暂停态加载：老师这会儿还看不到画面，等 start_at 才开
            self.mpv.start(self.video, subtitle, position=0.0)
            self._apply_quiet_boost()
        except MPVError as exc:
            QMessageBox.critical(self, "视频加载失败", str(exc))
            self._set_status("加载失败，详见日志")
            return
        finally:
            self._set_buttons_enabled(True)

        # start_at 是从「教师机已就绪」开始算的。反过来说，如果不等就绪
        # 就广播 start_at，大文件上全班会一起晚起播。
        with self._missing_lock:
            self._missing.clear()

        start_at = self._start_at_after(config.PLAY_LEAD)
        self._run_at(start_at, self.mpv.play)  # 教师机自己也在 start_at 起播
        self.server.broadcast(
            {
                "cmd": protocol.PLAY,
                "video": self.video.name,
                "position": 0.0,
                "start_at": start_at,
            }
        )
        log(f"广播 PLAY {self.video.name}，{config.PLAY_LEAD}s 后起播")

    def _pause(self) -> None:
        if not self.mpv.running:
            return
        # 暂停是纯状态切换，不加 start_at：局域网延迟只有毫秒级，
        # 让学生端立即暂停就够了，再加提前量反而多一次等待。
        if not self._safe(self.mpv.pause):
            return
        self.server.broadcast({"cmd": protocol.PAUSE})
        log("广播 PAUSE")

    def _resume(self) -> None:
        if not self.mpv.running:
            return
        position = self.mpv.get_position() or 0.0
        start_at = self._start_at_after(config.CMD_LEAD)
        self._run_at(start_at, self.mpv.play)
        self.server.broadcast(
            {"cmd": protocol.RESUME, "position": position, "start_at": start_at}
        )
        log(f"广播 RESUME @ {position:.2f}s")

    def _on_seek(self) -> None:
        if not self.mpv.running:
            return
        duration = self.mpv.get_duration() or 0.0
        position = self._slider.value() / SLIDER_MAX * duration
        was_playing = not self.mpv.paused

        self._do_seek(position, resume=was_playing)

    def _do_seek(self, position: float, resume: bool) -> None:
        if not self._safe(self.mpv.pause):
            return
        try:
            self.mpv.seek(position)
        except MPVError as exc:
            log(f"教师机定位失败：{exc}")
            return

        start_at = self._start_at_after(config.CMD_LEAD)
        if resume:
            self._run_at(start_at, self.mpv.play)

        # resume 标志很重要：老师暂停着拖进度条时，不该把学生端播起来
        self.server.broadcast(
            {
                "cmd": protocol.SEEK,
                "position": position,
                "start_at": start_at,
                "resume": resume,
            }
        )
        log(f"广播 SEEK @ {position:.2f}s（{'继续' if resume else '保持暂停'}）")

    def _stop(self) -> None:
        if not self.mpv.running:
            return
        self.server.broadcast({"cmd": protocol.STOP})
        self.mpv.stop()
        self._mpv_was_running = False
        self._slider.setValue(0)
        self._time_label.setText("--:-- / --:--")
        self._set_status("已停止")
        log("广播 STOP")

    def _safe(self, fn, *args) -> bool:
        try:
            fn(*args)
            return True
        except MPVError as exc:
            log(f"mpv 操作失败：{exc}")
            return False

    # ------------------------------------------------------------------ 心跳

    def _heartbeat(self) -> None:
        """每秒广播一次真实位置，学生端靠它自己纠偏。

        这一个机制同时解决了三件事：自动纠偏、状态一致性、以及学生端
        加载慢时错过起播时间点的补救。

        时间基准统一用 time.time()（教师机本地时钟），学生端用 PING/PONG
        算出的 offset 换算，不能各用各的本地时间。
        """
        if not self.mpv.running:
            return
        # 时间戳取「问位置」这一来一回的中点：问 mpv 要花几毫秒到几十毫秒，
        # 位置是在这段时间中间读到的，盖「发送时刻」的章会让学生端系统性地多算一点
        t_before = time.time()
        position = self.mpv.get_position()
        t_after = time.time()
        if position is None:
            return

        self.server.broadcast(
            {
                "cmd": protocol.HEARTBEAT,
                "playing": not self.mpv.query_paused(),
                "position": position,
                "server_time": (t_before + t_after) / 2.0,
            }
        )

    # ------------------------------------------------------- 学生端回报处理

    def _on_student_message(self, message: dict, peer: str) -> None:
        """注意：这是在 WebSocket 线程里被调用的，不要碰 Qt 控件。

        只往带锁的集合里写，让 QTimer 去读。
        """
        if message.get("cmd") == protocol.VIDEO_NOT_FOUND:
            with self._missing_lock:
                self._missing.add(peer)
            log(f"学生机 {peer} 没有找到视频 {message.get('video')!r}")

    def closeEvent(self, event) -> None:
        self.mpv.quit()
        if self._http is not None:
            self._http.stop()
        event.accept()


def main() -> None:
    log("LanVideoSync 教师端启动")
    app = QApplication([])
    window = Window()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
