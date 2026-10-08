"""LanVideoSync 学生端。

设计目标只有一个：**学生不需要进行任何操作**。

启动后自动完成：
    1. UDP 广播找教师端（找不到就每 2 秒重试，永不退出）
    2. 连上后做 PING/PONG 时间同步
    3. 等指令，收到就执行

教师端重启或者网络断开，都会自动回到第 1 步，学生不用重新打开程序。

关键设计——「未来时间点起播」：
教师端发来的每条 PLAY/RESUME/SEEK 都带一个 start_at，那是**未来**的某个时刻。
学生端拿到后先把 mpv 加载好、定位好、停住，然后睡到 start_at 才 play()。
绝不能收到消息就立刻播——那样 38 台机器会各播各的。
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

# 允许直接 `python src/student/main.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect

from common import config, net, protocol, timesync
from common.log import log
from common.mpvctl import MPV, MPVError
from common.paths import desktop_dir


class Student:
    def __init__(self) -> None:
        self.mpv = MPV("student")
        self.clock = timesync.ClockSync()
        self.current_video: Path | None = None
        self._pending_task: asyncio.Task | None = None

    # ------------------------------------------------------------- 本地视频

    @staticmethod
    def find_videos() -> dict[str, Path]:
        """桌面上认得出的视频，键是小写文件名——Windows 文件名不区分大小写。

        按后缀表遍历而不是 glob("*.mkv")：一是要认 mp4 等格式，二是
        `p.suffix.lower()` 比 glob 的大小写行为更可控（.MP4 也得认，
        老师从别人那拷来的文件名什么写法都有）。
        """
        desktop = desktop_dir()
        return {
            p.name.lower(): p
            for p in desktop.iterdir()
            if p.is_file() and p.suffix.lower() in config.VIDEO_EXTS
        }

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
        elif cmd == protocol.RESUME:
            self._schedule_seek(message, resume=True)
        elif cmd == protocol.SEEK:
            self._schedule_seek(message, resume=bool(message.get("resume", True)))
        elif cmd == protocol.STOP:
            self._cancel_pending()
            self.current_video = None
            await asyncio.to_thread(self._safe, self.mpv.stop)
        elif cmd == protocol.HEARTBEAT:
            await asyncio.to_thread(self._on_heartbeat, message)

    async def _on_play(self, message: dict, ws) -> None:
        name = message.get("video", "")
        video = self.find_videos().get(name.lower())

        if video is None:
            # 找不到就报告，别弹错误框、别崩。教师端会显示有多少台没找到。
            log(f"桌面上找不到视频 {name!r}，回报 VIDEO_NOT_FOUND")
            self.current_video = None
            self._cancel_pending()
            await asyncio.to_thread(self._safe, self.mpv.stop)
            await ws.send(protocol.encode({"cmd": protocol.VIDEO_NOT_FOUND, "video": name}))
            return

        subtitle = self.find_subtitle(video)
        log(f"播放 {video.name}（字幕：{subtitle.name if subtitle else '无'}）")
        self.current_video = video

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
            return

        await self._sleep_until(start_at)
        await asyncio.to_thread(self._safe, self.mpv.play)

    async def _seek_and_start(self, position: float, start_at: float, resume: bool) -> None:
        try:
            await asyncio.to_thread(self._safe, self.mpv.pause)
            await asyncio.to_thread(self.mpv.seek, position)
        except MPVError as exc:
            # 定位失败也**不能直接退出**：那样学生端会一直停在暂停态，
            # 而心跳见「已暂停」就跳过纠偏，等于永久掉队。
            # 位置交给心跳去纠，这里先把播放状态对上。
            log(f"定位失败：{exc}，位置改由心跳纠偏")

        await self._sleep_until(start_at)
        if resume:
            await asyncio.to_thread(self._safe, self.mpv.play)

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
        if self._pending_task is not None and not self._pending_task.done():
            return  # 正在执行 PLAY/SEEK 的等待，别和它抢 mpv
        if not self.mpv.running or self.mpv.query_paused():
            return
        if not message.get("playing"):
            return

        elapsed = self.clock.teacher_now() - float(message["server_time"])
        target = float(message["position"]) + elapsed

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

    # ------------------------------------------------------------------ 主循环

    async def run_forever(self) -> None:
        """找教师端 → 连上 → 执行指令，断了就回到第一步，永不退出。"""
        while True:
            try:
                host = await asyncio.to_thread(net.discover)
                if host is None:
                    log("没找到教师端，稍后重试")
                    await asyncio.sleep(config.REDISCOVER_INTERVAL)
                    continue

                log(f"教师端在 {host}，连接中…")
                # proxy=None 是必须的：websockets 15+ 默认读系统代理环境变量，
                # 局域网直连会被错误地当成需要走代理。
                async with connect(
                    f"ws://{host}:{config.WS_PORT}", open_timeout=5, proxy=None
                ) as ws:
                    self.clock = await timesync.sync(ws)
                    log(
                        f"已连接。RTT {self.clock.rtt * 1000:.1f}ms，"
                        f"时钟偏差 {self.clock.offset:+.3f}s"
                    )

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
                raise
            except Exception as exc:
                log(f"连接断开（{type(exc).__name__}: {exc}），稍后重连")

            await asyncio.sleep(config.REDISCOVER_INTERVAL)


async def run() -> None:
    await Student().run_forever()


def main() -> None:
    log("LanVideoSync 学生端启动")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
