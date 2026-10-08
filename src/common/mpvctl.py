r"""mpv IPC 封装。教师端和学生端共用同一个类。

Windows 上 mpv 的 --input-ipc-server 是一个命名管道（\\.\pipe\xxx）。
协议是每行一条 JSON：

    发送  {"command": ["set_property", "pause", true], "request_id": 7}
    响应  {"request_id": 7, "error": "success", "data": null}

播放位置和暂停状态用 observe_property 订阅，mpv 会主动推 property-change
事件，所以读位置基本是本地取缓存，不用每次都去问 mpv。


这个文件里有两处 Windows 特有的坑，改动时务必保留：

**坑一：管道名必须每个实例唯一。** 见 __init__。

**坑二：不能用阻塞读。** 同步（非 OVERLAPPED）命名管道句柄同一时刻只允许
一个未完成的 I/O 操作。如果读线程阻塞在 ReadFile 上，另一个线程的 WriteFile
就永远排不上队——两边互等，整个程序无声卡死。

    更坑的是：用 open() 拿到的 FileIO 会这样，绕开它改用 os.read/os.write
    也一样，因为 os.read 底层是 CRT 的 _read，CRT 对同一个 fd 持有内部锁，
    阻塞期间锁不放。两种情况表现完全一样，很难往这个方向想。

    解法是读端**永远不阻塞**：先用 PeekNamedPipe 问「有多少字节可读」，
    确认有数据才调 ReadFile，这样 ReadFile 瞬间返回，句柄立刻释放，
    WriteFile 就有机会插进去。见 _read_loop。

    （验证方式：同机起 1 教师 + 3 学生连跑，任一端出现「点了没反应、
      CPU 占用 0」就是这个问题复发了。）
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from . import config
from .log import log
from .paths import find_mpv

PIPE_PREFIX = "\\\\.\\pipe\\"

# observe_property 的订阅 id，随便定的，只要和 mpv 回传的 id 对得上
_OBS_TIME_POS = 1
_OBS_PAUSE = 2
_OBS_DURATION = 3

# 轮询间隔。命令都是用户点击触发的，10ms 的额外延迟完全感知不到，
# 而每次轮询只是一次 PeekNamedPipe，开销可以忽略。
_POLL_INTERVAL = 0.01

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class MPVError(RuntimeError):
    """mpv 启动失败、命令超时或命令返回 error。"""


if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE

    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL

    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL

    _kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.PeekNamedPipe.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
else:
    _kernel32 = None


class MPV:
    """一个 mpv 进程 + 它的 IPC 连接。

    典型用法::

        mpv = MPV("student")
        mpv.start(video, sub, position=125.0)   # 以暂停态加载并定位
        mpv.play()                              # 到点再真正开始
        mpv.pause(); mpv.seek(300.0); mpv.stop()
    """

    def __init__(self, role: str = "mpv"):
        self.role = role
        # 管道名必须每个实例唯一：验收要求同一台机器上同时跑 5 个学生端，
        # 名字写死的话它们会互相抢同一个管道。
        self._pipe = f"{PIPE_PREFIX}lvs_{role}_{os.getpid()}_{id(self)}"
        self._proc: subprocess.Popen | None = None
        self._handle: int | None = None
        self._running = False

        self._pending: dict[int, list] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1

        self._pos: float | None = None
        self._paused = True
        self._duration: float | None = None
        self._loaded = threading.Event()
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()

    # ------------------------------------------------------------------ 状态

    @property
    def running(self) -> bool:
        """mpv 进程还活着、IPC 还通。"""
        return self._running

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def position(self) -> float | None:
        """缓存的上次已知位置。要最新值用 get_position()。"""
        return self._pos

    def get_position(self) -> float | None:
        """问 mpv 要当前位置。超时就退回缓存值，绝不阻塞太久。

        教师端每 500ms 调一次，所以超时给得很短——卡住 UI 比数字略旧更糟。
        """
        if not self._running:
            return self._pos
        try:
            value = self.command("get_property", "time-pos", timeout=0.4)
        except MPVError:
            return self._pos
        if isinstance(value, (int, float)):
            self._pos = float(value)
        return self._pos

    def get_duration(self) -> float | None:
        """总时长。优先读订阅缓存。

        不缓存的话每次都去问 mpv，而 mpv 在文件加载完成前会把 duration 的
        回应一直压着不发，查询必然超时——教师端进度条就会一直是空白的。
        """
        if self._duration is not None:
            return self._duration
        if not self._running:
            return None
        try:
            value = self.command("get_property", "duration", timeout=0.4)
        except MPVError:
            return None
        if isinstance(value, (int, float)):
            self._duration = float(value)
        return self._duration

    def wait_loaded(self, timeout: float = 15.0) -> bool:
        """等 mpv 真正把文件读进来（file-loaded 事件）。

        start() 内部已经会等，所以正常使用时 start() 返回后这里立刻为真。
        单独留着是给需要自己控制等待时长的场景用。

        为什么要等：进程起来、管道通了都不代表文件读完，这期间 play() 是
        空操作，起播就晚了——大 MKV 上能差好几秒。不能靠 sleep 猜。
        """
        if self._loaded.is_set():
            return True
        if not self._running:
            return False
        return self._loaded.wait(timeout)

    # -------------------------------------------------------------- 生命周期

    def start(
        self,
        video: Path | str,
        sub: Path | None = None,
        position: float = 0.0,
        load_timeout: float = 30.0,
        extra_args: list[str] | None = None,
    ) -> None:
        """启动 mpv 加载视频，**返回时保证已就绪**：文件读完、定位到 position、处于暂停态。

        这个「返回即就绪」的约定是同步精度的基础。mpv 加载 4K MKV 要好几秒，
        如果加载完就自动播，每台机器的起播位置都不一样。所以这里先把它按在
        position 上停稳，再由调用方到统一的 start_at 一起 play()。

        **不要改回用 --start= 定位。** 实测（25 次里 10 次）mpv 会把 --start 的
        起始定位拖到加载之后懒执行，这期间 set_property pause false 虽然立刻
        回 success，却要等 1~2 秒才真正生效——全班起播时间就散了。
        显式 seek 并等它落定才可靠。
        """
        self.quit()

        exe = find_mpv()
        if exe is None:
            raise MPVError(
                "找不到 mpv.exe。请把 mpv 解压到程序目录下的 mpv\\ 文件夹，"
                "确保 mpv\\mpv.exe 存在。"
            )

        args = [
            str(exe),
            "--input-ipc-server=" + self._pipe,
            "--force-window=yes",
            "--keep-open=no",
            "--no-terminal",
            "--osc=no",
            "--pause=yes",
        ]
        if sub is not None and sub.is_file():
            args.append("--sub-file=" + str(sub))
        # 切片播放时传 --sub-fonts-dir 之类。放在视频地址之前。
        args.extend(extra_args or [])
        args.append(str(video))

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(args, creationflags=creationflags)

        self._pos = None
        self._paused = True
        self._duration = None
        self._loaded.clear()
        self._open_pipe(timeout=15.0)
        self._running = True

        self._reader_stop.clear()
        self._reader = threading.Thread(
            target=self._read_loop, name=f"mpv-{self.role}", daemon=True
        )
        self._reader.start()

        # 订阅位置、暂停、时长，之后 mpv 会主动推给我们
        self.command("observe_property", _OBS_TIME_POS, "time-pos")
        self.command("observe_property", _OBS_PAUSE, "pause")
        self.command("observe_property", _OBS_DURATION, "duration")

        if not self._loaded.wait(load_timeout):
            self.quit()
            raise MPVError(f"视频加载超时（{load_timeout:.0f}s）：{getattr(video, 'name', video)}")

        if position > 0:
            self._seek_and_settle(position)

        # 加载和定位期间 mpv 可能自己动过 pause，统一压回暂停态
        self.pause()

    def quit(self, timeout: float = 3.0) -> None:
        """关掉 mpv 进程和 IPC 连接。没在跑就什么都不做。"""
        proc, self._proc = self._proc, None
        reader, self._reader = self._reader, None

        self._reader_stop.set()

        if self._handle is not None:
            try:
                self._write({"command": ["quit"]})
            except OSError:
                pass

        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()

        # 读线程最多 10ms 就会看到 stop 标志退出，这里等一下纯属保险
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.0)

        self._close_pipe()
        self._running = False
        self._pos = None
        self._paused = True
        self._duration = None
        self._loaded.clear()

    # 停止播放：V1 就是直接关播放器
    stop = quit

    # ---------------------------------------------------------------- 播放控制

    def play(self) -> None:
        self.command("set_property", "pause", False)
        # 命令回了 success 就按意图更新缓存。等 mpv 的事件推回来也行，
        # 但那样「点了播放、状态栏还显示已暂停」会持续到推送到达为止。
        self._paused = False

    def pause(self) -> None:
        self.command("set_property", "pause", True)
        self._paused = True

    def query_paused(self) -> bool:
        """问 mpv 现在到底是不是暂停，并用权威结果刷新缓存。

        心跳和纠偏要读这个，不能只信 observe 推来的缓存：加载、定位期间
        mpv 会自己改 pause 并推事件，缓存有可能和真实状态不一致。而
        「以为在暂停」会让学生端整段跳过纠偏——这是最不该出错的地方。
        """
        if not self._running:
            return self._paused
        try:
            value = self.command("get_property", "pause", timeout=0.5)
        except MPVError:
            return self._paused
        if isinstance(value, bool):
            self._paused = value
        return self._paused

    def seek(self, position: float) -> None:
        """绝对定位。同时立刻更新缓存，不等 mpv 回推。

        否则紧接着的纠偏判断会拿旧位置去比，误判成还在漂。
        """
        self.command("seek", float(position), "absolute")
        self._pos = max(0.0, float(position))

    def _seek_and_settle(self, position: float, timeout: float = 10.0) -> None:
        """定位并等它真的落定，然后才认为可以起播。

        光发完 seek 就返回是不够的：seek 是异步的，mpv 还要几帧才真的
        停在新位置上。这时候 play() 的效果会延迟，起播时间就散了。
        """
        position = max(0.0, float(position))
        self.seek(position)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.get_position()
            if current is not None and abs(current - position) < 0.5:
                return
            time.sleep(0.05)
        log(f"定位到 {position:.2f}s 后没等到位置稳定，仍继续（靠心跳纠偏兜底）")

    # ------------------------------------------------------------------ IPC

    def command(self, *args, timeout: float | None = None):
        """发一条 mpv 命令，等它的响应，返回 data 字段。

        先登记 pending 再写管道——反过来的话响应可能比登记先到，就丢了。
        """
        timeout = config.MPV_TIMEOUT if timeout is None else timeout
        slot: list = [threading.Event(), None]

        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = slot

        try:
            self._write({"command": list(args), "request_id": request_id})
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            self._running = False
            raise MPVError(f"向 mpv 发送命令失败: {exc}") from exc

        if not slot[0].wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise MPVError(f"mpv 命令超时 ({timeout}s): {args}")

        with self._pending_lock:
            self._pending.pop(request_id, None)

        response = slot[1] or {}
        error = response.get("error")
        if error not in (None, "success"):
            raise MPVError(f"mpv 命令失败 ({error}): {args}")
        return response.get("data")

    def _write(self, payload: dict) -> None:
        if self._handle is None:
            raise OSError("mpv IPC 未连接")
        data = (json.dumps(payload) + "\n").encode("utf-8")
        with self._write_lock:
            offset = 0
            while offset < len(data):
                chunk = data[offset:]
                written = wintypes.DWORD(0)
                ok = _kernel32.WriteFile(
                    self._handle, chunk, len(chunk), ctypes.byref(written), None
                )
                if not ok:
                    raise OSError(ctypes.get_last_error(), "mpv IPC 写入失败")
                if written.value == 0:
                    raise OSError("mpv IPC 写入中断")
                offset += written.value

    def _open_pipe(self, timeout: float) -> None:
        """等 mpv 把管道建出来再连。mpv 启动到建管道之间有个窗口期。"""
        if _kernel32 is None:
            raise MPVError("mpv IPC 目前只实现了 Windows 版（命名管道）")

        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            # 进程提前挂了就不用再等了（比如视频文件损坏）
            if self._proc is not None and self._proc.poll() is not None:
                raise MPVError(f"mpv 启动后立即退出（返回码 {self._proc.returncode}）")
            try:
                handle = _kernel32.CreateFileW(
                    self._pipe,
                    GENERIC_READ | GENERIC_WRITE,
                    0, None, OPEN_EXISTING, 0, None,
                )
                if handle == INVALID_HANDLE_VALUE:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._handle = handle
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
        raise MPVError(f"连不上 mpv IPC 管道 {self._pipe}: {last_error}")

    def _close_pipe(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                _kernel32.CloseHandle(handle)
            except OSError:
                pass

    @staticmethod
    def _peek(handle) -> int:
        """管道里有多少字节可读；管道已断开返回 -1。"""
        available = wintypes.DWORD(0)
        ok = _kernel32.PeekNamedPipe(
            handle, None, 0, None, ctypes.byref(available), None
        )
        return available.value if ok else -1

    def _read_loop(self) -> None:
        """后台线程：轮询管道、按行拆、分发。

        **必须用 PeekNamedPipe 轮询，不能直接阻塞在 ReadFile 上。**
        原因见模块开头的「坑二」——阻塞读会把整个句柄占死，写不出去。
        """
        handle = self._handle
        buffer = b""

        try:
            while not self._reader_stop.is_set():
                available = self._peek(handle)
                if available < 0:
                    # 管道断了。这里必须记日志——读线程一退出，之后每条命令
                    # 都会超时，表现是「点了没反应」，不打日志根本查不出来。
                    log(
                        f"mpv IPC 管道断开（PeekNamedPipe 失败，"
                        f"错误码 {ctypes.get_last_error()}）"
                    )
                    break
                if available == 0:
                    time.sleep(_POLL_INTERVAL)
                    continue

                # 确认有数据再读，这一下会立刻返回，句柄马上释放
                chunk = ctypes.create_string_buffer(available)
                read = wintypes.DWORD(0)
                if not _kernel32.ReadFile(
                    handle, chunk, available, ctypes.byref(read), None
                ):
                    log(
                        f"mpv IPC 读取失败（ReadFile 失败，"
                        f"错误码 {ctypes.get_last_error()}）"
                    )
                    break
                if read.value == 0:
                    break

                buffer += chunk.raw[: read.value]
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._dispatch(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        finally:
            self._running = False

    def _dispatch(self, message: dict) -> None:
        if message.get("event"):
            event = message["event"]
            # property-change：mpv 主动推来的位置/暂停/时长
            if event == "property-change":
                name = message.get("name")
                data = message.get("data")
                if name == "time-pos":
                    self._pos = data
                elif name == "pause":
                    self._paused = bool(data)
                elif name == "duration" and isinstance(data, (int, float)):
                    self._duration = float(data)
                # 拿到位置或时长就说明文件已经读进来了。file-loaded 事件万一
                # 在读线程起来之前就发掉了（小文件有可能），靠这个兜底，
                # 否则 wait_loaded 会白等到超时。
                if name in ("time-pos", "duration") and data is not None:
                    self._loaded.set()
            elif event == "file-loaded":
                # 文件真正读进来了，这之后 play() 才会有效果
                self._loaded.set()
            elif event in ("shutdown", "end-file"):
                self._running = False
            return

        request_id = message.get("request_id")
        if request_id is None:
            return
        with self._pending_lock:
            slot = self._pending.get(request_id)
        if slot is not None:
            slot[1] = message
            slot[0].set()
