"""一个假的 mpv 进程，用 Windows 命名管道说 mpv 的 JSON IPC 协议。

真 mpv 下载下来之前，用它把 IPC 封装里最容易出低级错误的部分验证掉：
管道能不能连上、JSON 按行收发对不对、request_id 能不能对上、
property-change 事件能不能收到并更新缓存。

注意：这验证的是**我们这侧的封装逻辑**，不是 mpv 本身的协议细节。
真 mpv 到位后仍然必须跑一次真实的端到端测试。

**别从别的线程调 _emit/_write。** 假 mpv 用的是同步命名管道句柄，同一时刻
只允许一个未完成的 I/O：_pump 线程阻塞在 _read 上时，另一个线程的
WriteFile 会永远排不上队，直接死锁。（真 mpv 用的是重叠 I/O，没这个问题，
所以这只是测试替身的限制。）要推事件就让 _pump 线程自己推。
"""

from __future__ import annotations

import ctypes
import json
import threading
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255
ERROR_PIPE_CONNECTED = 535

kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE

kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.ConnectNamedPipe.restype = wintypes.BOOL

kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
kernel32.ReadFile.restype = wintypes.BOOL

kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
kernel32.WriteFile.restype = wintypes.BOOL

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class FakeMPV:
    """监听一个命名管道，按 mpv 的协议应答。

    参数 pipe_path 形如 r"\\\\.\\pipe\\lvs_test_123"。
    """

    def __init__(self, pipe_path: str):
        self.pipe_path = pipe_path
        self.position = 0.0
        self.paused = True
        self.duration = 600.0
        self.observed: dict[int, str] = {}
        self.commands: list[list] = []      # 收到的所有命令，供断言检查
        self.ready = threading.Event()      # 管道已建好，可以连了
        self.connected = threading.Event()  # 客户端已连上
        self.stopped = threading.Event()
        self._handle = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """建好管道就返回。此时客户端还连不上会阻塞，所以要先于客户端调用。"""
        self._thread.start()
        if not self.ready.wait(timeout=5.0):
            raise RuntimeError("假 mpv 没能在 5 秒内把管道建起来")

    def _run(self) -> None:
        handle = kernel32.CreateNamedPipeW(
            self.pipe_path,
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            PIPE_UNLIMITED_INSTANCES,
            65536, 65536, 0, None,
        )
        if handle == ctypes.c_void_p(-1).value:
            self.ready.set()
            return
        self._handle = handle
        self.ready.set()

        # 阻塞，直到客户端 open() 这个管道
        if not kernel32.ConnectNamedPipe(handle, None):
            if ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                kernel32.CloseHandle(handle)
                self.connected.set()
                return

        self.connected.set()
        self._pump()

    def _pump(self) -> None:
        """读一行、应答一行、循环。"""
        # 连上就先推一次属性事件，模拟 mpv 启动后的初始状态推送。
        # 客户端应该能靠它把 position/paused 缓存填上。
        self._emit("time-pos", 42.0)
        self._emit("pause", False)
        self._emit("duration", self.duration)
        self._write({"event": "file-loaded"})

        buffer = b""
        while not self.stopped.is_set():
            chunk = self._read()
            if chunk is None:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if line:
                    try:
                        self._handle_command(json.loads(line))
                    except ValueError:
                        pass
        kernel32.CloseHandle(self._handle)

    def stop(self) -> None:
        self.stopped.set()

    # -------------------------------------------------------------- 收发

    def _read(self, size: int = 4096) -> bytes | None:
        buf = ctypes.create_string_buffer(size)
        read = wintypes.DWORD(0)
        if not kernel32.ReadFile(self._handle, buf, size, ctypes.byref(read), None):
            return None
        return buf.raw[: read.value]

    def _write(self, payload: dict) -> None:
        data = (json.dumps(payload) + "\n").encode("utf-8")
        written = wintypes.DWORD(0)
        kernel32.WriteFile(self._handle, data, len(data), ctypes.byref(written), None)

    def _emit(self, name: str, value) -> None:
        """主动推一个 property-change 事件，和真 mpv 一样。"""
        self._write({
            "event": "property-change",
            "id": next((i for i, n in self.observed.items() if n == name), 0),
            "name": name,
            "data": value,
        })

    # ------------------------------------------------------------ 命令处理

    def _handle_command(self, message: dict) -> None:
        command = message.get("command")
        request_id = message.get("request_id")
        if not command:
            return
        self.commands.append(command)

        name = command[0]

        if name == "observe_property":
            self.observed[int(command[1])] = command[2]
            self._reply(request_id, None)

        elif name == "get_property":
            prop = command[1]
            if prop == "time-pos":
                self._reply(request_id, self.position)
            elif prop == "duration":
                self._reply(request_id, self.duration)
            elif prop == "pause":
                self._reply(request_id, self.paused)
            else:
                self._reply(request_id, None, error="property unavailable")

        elif name == "set_property":
            prop, value = command[1], command[2]
            if prop == "pause":
                self.paused = bool(value)
                self._reply(request_id, None)
                self._emit("pause", self.paused)
            else:
                self._reply(request_id, None)

        elif name == "seek":
            self.position = float(command[1])
            self._reply(request_id, None)
            self._emit("time-pos", self.position)

        elif name == "quit":
            self._reply(request_id, None)
            self.stopped.set()

        else:
            self._reply(request_id, None, error="unsupported command")

    def _reply(self, request_id, data, error: str | None = None) -> None:
        self._write({
            "request_id": request_id,
            "error": error or "success",
            "data": data,
        })
