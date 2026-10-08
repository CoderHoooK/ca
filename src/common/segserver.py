"""切片 HTTP 服务。教师端和学生端用同一份代码。

两类请求，路径前缀不同，行为也不同——这是刻意分开的：

    /play/<id>/index.m3u8        本机 mpv 用。只接受回环地址。
    /play/<id>/<文件>             切片还没缓存好就**阻塞等待**，mpv 看到的就是
                                 「网络慢」，自己会显示缓冲，不会报错退出。

    /p/<id>/manifest.json        给别的机器（同学/教师机）用。
    /p/<id>/<文件>                有就发，没有立刻 404，**绝不阻塞**；
                                 同时上传的数量有上限，超了回 503。

为什么不能共用一条路径、靠「是不是回环地址」区分：开发时同一台机器上跑好几个
学生端，同学之间也是通过 127.0.0.1 互相拉的，那样会误判成「本机 mpv」而阻塞。

503 是 P2P 能铺开的关键：开课瞬间 40 台机器都来找教师机要第 0 段，教师机只放
TEACHER_MAX_UPLOADS 个进来，其余的稍等再问——那时先拿到的同学已经有这一段了，
tracker 会把他们指给后来的人，教师机的上行就不会被打满。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import unquote, urlparse

from . import config
from .log import log
from .package import MANIFEST_NAME, Manifest

CHUNK = 256 * 1024
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


class Provider(Protocol):
    """一门课的文件来源。教师端是整个文件夹，学生端是边下边存的缓存。"""

    manifest: Manifest

    def file_path(self, rel: str) -> Path | None:
        """这个文件现在就能读吗？能就返回路径，不能返回 None。不阻塞。"""

    def wait_for(self, rel: str, timeout: float) -> Path | None:
        """阻塞到文件可读或超时。只有本机 mpv 的请求会走这条。"""


class FolderProvider:
    """教师端：课程文件夹里什么都有，不用等。"""

    def __init__(self, folder: Path, manifest: Manifest) -> None:
        self.folder = Path(folder)
        self.manifest = manifest

    def file_path(self, rel: str) -> Path | None:
        if rel not in self.manifest.files() or rel == MANIFEST_NAME:
            return None
        path = self.folder / rel
        return path if path.is_file() else None

    def wait_for(self, rel: str, timeout: float) -> Path | None:
        return self.file_path(rel)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True


class SegServer:
    def __init__(
        self,
        lookup: Callable[[str], Provider | None],
        max_uploads: int,
        port: int = 0,
        host: str = "0.0.0.0",
        on_wait: Callable[[bool], None] | None = None,
    ) -> None:
        """
        lookup       课程 id → Provider，没有这门课返回 None
        max_uploads  同时给别的机器上传的上限
        port         0 表示让系统挑空闲端口；指定的端口被占用时也会退回到 0
        on_wait      本机 mpv 开始/结束等待某个切片时调用（True/False），界面显示「缓冲中」
        """
        self._lookup = lookup
        self._slots = threading.BoundedSemaphore(max_uploads)
        self._on_wait = on_wait
        self.bytes_sent = 0
        self.uploads = 0
        self.rejected = 0
        self._stat_lock = threading.Lock()

        handler = self._make_handler()
        try:
            self._httpd = _Server((host, port), handler)
        except OSError:
            if port == 0:
                raise
            log(f"端口 {port} 被占用，改用空闲端口")
            self._httpd = _Server((host, 0), handler)
        self.port: int = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2},
            daemon=True, name="seg-http",
        )

    def start(self) -> "SegServer":
        self._thread.start()
        log(f"切片服务已启动，端口 {self.port}")
        return self

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except OSError:
            pass

    def play_url(self, pkg_id: str) -> str:
        """给本机 mpv 的播放地址。"""
        return f"http://127.0.0.1:{self.port}/play/{pkg_id}/index.m3u8"

    def _count(self, sent: int) -> None:
        with self._stat_lock:
            self.bytes_sent += sent
            self.uploads += 1

    # ------------------------------------------------------------------

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "LanVideoSync"
            protocol_version = "HTTP/1.0"  # 一个请求一条连接，实现最简单

            def log_message(self, *args) -> None:  # 别往 stderr 刷每个请求
                pass

            def _reply(self, code: int, body: bytes = b"", ctype: str = "text/plain",
                       extra: dict | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)

            def do_HEAD(self) -> None:
                self.do_GET()

            def do_GET(self) -> None:
                try:
                    self._route()
                except (ConnectionError, TimeoutError, OSError):
                    pass  # 对端（mpv 在 seek 时会掐连接）走了，不用理

            def _route(self) -> None:
                parts = [unquote(p) for p in urlparse(self.path).path.split("/")]
                # ['', 'play'|'p', id, rel...]
                if len(parts) < 4 or parts[0] != "" or parts[1] not in ("play", "p"):
                    return self._reply(404)
                kind, pkg_id, rel = parts[1], parts[2], "/".join(parts[3:])

                provider = outer._lookup(pkg_id)
                if provider is None:
                    return self._reply(404, b"unknown package")

                if kind == "play":
                    if self.client_address[0] not in LOOPBACK:
                        return self._reply(403, b"local only")
                    return self._serve_play(provider, rel)
                return self._serve_peer(provider, rel)

            # ---- 本机 mpv：缺什么等什么 ----
            def _serve_play(self, provider: Provider, rel: str) -> None:
                if rel == "index.m3u8":
                    return self._reply(
                        200, provider.manifest.m3u8().encode("utf-8"),
                        "application/vnd.apple.mpegurl",
                    )
                if rel not in provider.manifest.files():
                    return self._reply(404)

                path = provider.file_path(rel)
                if path is None:
                    # 切片还没到：挂起这个请求。mpv 会表现为缓冲中。
                    if outer._on_wait:
                        outer._on_wait(True)
                    try:
                        path = provider.wait_for(rel, config.PLAY_WAIT_TIMEOUT)
                    finally:
                        if outer._on_wait:
                            outer._on_wait(False)
                if path is None:
                    return self._reply(504, b"segment not available")
                self._send_file(path, count=False)

            # ---- 其他机器：有就给，没有就 404，超了就 503 ----
            def _serve_peer(self, provider: Provider, rel: str) -> None:
                if rel == MANIFEST_NAME:
                    return self._reply(
                        200, provider.manifest.to_json().encode("utf-8"), "application/json"
                    )
                if rel not in provider.manifest.files():
                    return self._reply(404)
                path = provider.file_path(rel)
                if path is None:
                    return self._reply(404, b"not cached")

                if not outer._slots.acquire(blocking=False):
                    outer.rejected += 1
                    return self._reply(503, b"busy", extra={"Retry-After": "1"})
                try:
                    self._send_file(path, count=True)
                finally:
                    outer._slots.release()

            def _send_file(self, path: Path, count: bool) -> None:
                try:
                    size = path.stat().st_size
                    fh = open(path, "rb")
                except OSError:
                    return self._reply(404)
                with fh:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                    if self.command == "HEAD":
                        return
                    sent = 0
                    try:
                        while True:
                            chunk = fh.read(CHUNK)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            sent += len(chunk)
                    finally:
                        if count:
                            outer._count(sent)

        return Handler
