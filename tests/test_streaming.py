"""切片播放的测试：切片服务、下载器、教师端 tracker、多个学生之间的 P2P。

    python tests/test_streaming.py

不需要 mpv、ffmpeg、图形界面。切片包是现场伪造的（随机字节当切片），
教师端用真的 Server（真 WebSocket），学生端用真的 Student（换成假 mpv），
HTTP 切片服务也是真的，全部走回环地址。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common import config, protocol
from common.package import (
    Manifest, PackageError, load_manifest, make_id, safe_relpath, verify_package,
)
from common.segserver import FolderProvider, SegServer
from student import state as st
from student import streaming
from student.main import Student
import student.main as student_main
from student.streaming import StreamSession
from teacher.main import Server

PASSED: list[str] = []
FAILED: list[str] = []
SEGMENTS = 12
SERVER: Server = None  # type: ignore  # 全程共用一个教师端（端口只能绑一次）
SEG_SIZE = 200_000


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  [{'ok' if condition else 'FAIL'}]   {name}" + (f"  {detail}" if detail else ""))


async def wait_until(predicate, timeout: float = 15.0, poll: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll)
    return predicate()


def make_package(root: Path, title: str = "测试课") -> tuple[Path, Manifest]:
    """伪造一门课：随机字节当切片，每段 2 秒，带一个字幕。"""
    folder = root / title
    folder.mkdir(parents=True)
    rnd = random.Random(7)
    segs, hashes = [], []
    for i in range(SEGMENTS):
        data = rnd.randbytes(SEG_SIZE)
        name = f"seg_{i:05d}.ts"
        (folder / name).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        hashes.append(digest)
        segs.append({"n": name, "d": 2.0, "s": len(data), "h": digest})
    (folder / "subs").mkdir()
    (folder / "subs" / "subtitle.ass").write_text("[Script Info]\n", encoding="utf-8")
    manifest = Manifest.from_dict({
        "format": 1, "id": make_id(title, hashes), "title": title, "source_name": f"{title}.mkv",
        "duration": SEGMENTS * 2.0, "segments": segs, "subtitles": ["subs/subtitle.ass"], "fonts": [],
    })
    (folder / "manifest.json").write_text(manifest.to_json(), encoding="utf-8")
    return folder, manifest


def http(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class FakeMPV:
    def __init__(self) -> None:
        self.running = False
        self.paused = True
        self.calls: list[str] = []
        self.url = ""
        self.extra: list[str] = []
        self.sub = None

    def start(self, video, subtitle, position=0.0, load_timeout=None, extra_args=None):
        self.url, self.sub, self.extra = str(video), subtitle, list(extra_args or [])
        self.calls.append("start")
        self.running = True

    def play(self): self.calls.append("play"); self.paused = False
    def pause(self): self.calls.append("pause"); self.paused = True
    def seek(self, position): self.calls.append(f"seek:{position:.1f}")
    def stop(self): self.calls.append("stop"); self.running = False
    quit = stop
    def query_paused(self): return self.paused
    def get_position(self): return 0.0


# ------------------------------------------------------------------ 课程包

def test_package(tmp: Path) -> None:
    print("\n课程包")
    folder, manifest = make_package(tmp / "pkg")
    check("manifest 往返一致", Manifest.from_json(manifest.to_json()).to_dict() == manifest.to_dict())
    check("加载得到起始时间", load_manifest(folder).segments[3].start == 6.0)
    check("index_at 定位", manifest.index_at(0) == 0 and manifest.index_at(5.9) == 2 and manifest.index_at(999) == SEGMENTS - 1)
    check("verify 通过", verify_package(folder, manifest) == [])
    (folder / "seg_00002.ts").write_bytes(b"x" * SEG_SIZE)
    check("verify 能发现被改过的切片", any("seg_00002" in p for p in verify_package(folder, manifest)))
    for bad in ("../x", "/etc/passwd", "a\\b", "a//b", ""):
        try:
            safe_relpath(bad); ok = False
        except PackageError:
            ok = True
        check(f"拒绝不安全路径 {bad!r}", ok)
    text = manifest.m3u8()
    check("m3u8 有 ENDLIST 和每段的 EXTINF", "#EXT-X-ENDLIST" in text and text.count("#EXTINF") == SEGMENTS)
    check("m3u8 用相对路径", "http" not in text)


# ------------------------------------------------------------------ 切片服务

def test_segserver(tmp: Path) -> None:
    print("\n切片服务")
    folder, manifest = make_package(tmp / "srv")
    provider = FolderProvider(folder, manifest)
    srv = SegServer({manifest.id: provider}.get, max_uploads=2).start()
    base = f"http://127.0.0.1:{srv.port}"
    try:
        code, body = http(f"{base}/p/{manifest.id}/manifest.json")
        check("同学能取到 manifest", code == 200 and Manifest.from_json(body).id == manifest.id)
        code, body = http(f"{base}/p/{manifest.id}/seg_00003.ts")
        check("同学能取到切片，内容正确", code == 200 and hashlib.sha256(body).hexdigest() == manifest.segments[3].sha256)
        check("字幕也能取", http(f"{base}/p/{manifest.id}/subs/subtitle.ass")[0] == 200)
        check("不存在的课程 404", http(f"{base}/p/deadbeef/manifest.json")[0] == 404)
        check("不在清单里的文件 404", http(f"{base}/p/{manifest.id}/nope.ts")[0] == 404)
        check("目录穿越 404", http(f"{base}/p/{manifest.id}/..%2F..%2Fetc%2Fpasswd")[0] == 404
              and http(f"{base}/p/{manifest.id}/../../etc/passwd")[0] == 404)

        # 上传名额满了 → 503，释放后恢复
        srv._slots.acquire(); srv._slots.acquire()
        code, _ = http(f"{base}/p/{manifest.id}/seg_00001.ts")
        check("名额满了返回 503", code == 503 and srv.rejected == 1)
        check("manifest 不占名额，名额满了也能取", http(f"{base}/p/{manifest.id}/manifest.json")[0] == 200)
        srv._slots.release(); srv._slots.release()
        check("名额释放后恢复 200", http(f"{base}/p/{manifest.id}/seg_00001.ts")[0] == 200)

        code, body = http(f"{base}/play/{manifest.id}/index.m3u8")
        check("本机播放入口给出 m3u8", code == 200 and b"#EXTM3U" in body)
        code, body = http(f"{base}/play/{manifest.id}/seg_00004.ts")
        check("本机播放入口给出切片", code == 200 and len(body) == SEG_SIZE)
    finally:
        srv.stop()


def test_play_blocks(tmp: Path) -> None:
    print("\n本机播放入口：缺切片时挂起，到了再给")
    folder, manifest = make_package(tmp / "blk")
    session = StreamSession(manifest, tmp / "blk_cache", None)
    waits: list[bool] = []
    srv = SegServer({manifest.id: session}.get, max_uploads=2, on_wait=waits.append).start()
    base = f"http://127.0.0.1:{srv.port}"
    try:
        check("同学来取没有的切片：404，不挂起", http(f"{base}/p/{manifest.id}/seg_00005.ts")[0] == 404)
        result: dict = {}

        def fetch() -> None:
            t0 = time.monotonic()
            result["r"] = http(f"{base}/play/{manifest.id}/seg_00005.ts", timeout=10)
            result["t"] = time.monotonic() - t0

        th = threading.Thread(target=fetch); th.start()
        time.sleep(0.5)
        check("mpv 的请求被挂起", th.is_alive() and waits == [True])
        check("挂起的切片被标成紧急", 5 in session._urgent)
        shutil.copy(folder / "seg_00005.ts", session.dir / "seg_00005.ts")
        with session._cond:
            session.have.add(5); session._cond.notify_all()
        th.join(5)
        check("切片到了之后请求完成", not th.is_alive() and result["r"][0] == 200 and len(result["r"][1]) == SEG_SIZE,
              f"{result.get('t', 0):.2f}s")
        check("等待结束的回调", waits == [True, False])
    finally:
        srv.stop()


# ------------------------------------------------------------------ 下载器

def test_downloader(tmp: Path) -> None:
    print("\n下载器")
    folder, manifest = make_package(tmp / "dl")
    teacher = SegServer({manifest.id: FolderProvider(folder, manifest)}.get, max_uploads=8).start()
    try:
        m2 = streaming.fetch_manifest("127.0.0.1", teacher.port, manifest.id, timeout=3)
        check("fetch_manifest 取到清单", m2.id == manifest.id and len(m2.segments) == SEGMENTS)
        try:
            streaming.fetch_manifest("127.0.0.1", teacher.port, "deadbeef", timeout=1)
            ok = False
        except RuntimeError:
            ok = True
        check("取不到的课程抛错而不是死等", ok)

        config.PREFETCH_AHEAD = 3
        session = StreamSession(m2, tmp / "dl_cache", ("127.0.0.1", teacher.port))
        session.set_focus_time(8.0, urgent=True)
        session.start()
        session.ensure_assets()
        check("字幕先下好了", session.subtitle_path() is not None)
        check("起播位置的切片优先下好", session.wait_ready(8.0, 1, 5))
        asyncio_wait(lambda: session.cached >= 4, 5)
        time.sleep(0.8)  # 给它机会多下（不该多下）
        check("只缓存播放位置往后的窗口（第 4~7 段），不是整部片子",
              session.have == {4, 5, 6, 7}, f"have={sorted(session.have)}")
        session.set_focus_time(0.0)
        check("播放位置变了，窗口跟着挪", asyncio_wait(lambda: {0, 1, 2, 3} <= session.have, 5), f"have={sorted(session.have)}")
        config.PREFETCH_AHEAD = SEGMENTS
        session.set_focus_time(0.0)
        check("全部下完", asyncio_wait(lambda: session.cached == SEGMENTS, 10), f"{session.cached}/{SEGMENTS}")
        check("下载的内容全部通过校验", verify_package(session.dir, m2) == [])
        check("来源统计：全部来自教师机", session.from_teacher == SEGMENTS and session.from_peers == 0)
        check("缓存里没有残留的 .part", not list(session.dir.glob("*.part")))
        session.stop()

        # 重播同一课：缓存里已有的不用再下
        before = teacher.uploads
        again = StreamSession(m2, tmp / "dl_cache", ("127.0.0.1", teacher.port))
        check("同一缓存目录重新打开，认出已有的切片", again.cached == SEGMENTS)
        check("不会重复下载", teacher.uploads == before)

        # 缓存只有一半 / 缓存文件被改坏 → 只补缺的
        os.remove(again.dir / "seg_00002.ts")
        (again.dir / "seg_00003.ts").write_bytes(b"short")
        partial = StreamSession(m2, tmp / "dl_cache", ("127.0.0.1", teacher.port))
        check("缺的和大小不对的算作没有", partial.cached == SEGMENTS - 2)
    finally:
        teacher.stop()


def asyncio_wait(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_bad_peer(tmp: Path) -> None:
    print("\n校验：坏同学不会传染")
    folder, manifest = make_package(tmp / "bad")
    evil = tmp / "evil"
    shutil.copytree(folder, evil)
    # 第 3、7 段：大小一样、内容不同（只有 sha256 才能发现）
    for i in (3, 7):
        (evil / f"seg_{i:05d}.ts").write_bytes(b"\xff" * SEG_SIZE)
    # 第 5 段：大小不对
    (evil / "seg_00005.ts").write_bytes(b"short")
    teacher = SegServer({manifest.id: FolderProvider(folder, manifest)}.get, max_uploads=8).start()
    bad_peer = SegServer({manifest.id: FolderProvider(evil, manifest)}.get, max_uploads=8).start()
    try:
        session = StreamSession(
            manifest, tmp / "bad_cache", ("127.0.0.1", teacher.port),
            sources_fn=lambda pkg, n: [("127.0.0.1", bad_peer.port)],
        )
        session.set_focus_time(0, urgent=True)
        session.ensure_assets()
        session.start()
        ok = asyncio_wait(lambda: session.cached == SEGMENTS, 15)
        session.stop()
        check("坏数据被拒绝后改从教师机补齐", ok, f"{session.cached}/{SEGMENTS}")
        problems = verify_package(session.dir, manifest)
        check("最终缓存全部合法", problems == [], str(problems))
        check("记录了坏数据次数", session.bad_data >= 3, f"bad_data={session.bad_data}")
        check("好的段仍然从同学那里拿了", session.from_peers >= 1, f"from_peers={session.from_peers}")
        check("坏的段是教师机补的", session.from_teacher >= 3, f"from_teacher={session.from_teacher}")
    finally:
        teacher.stop(); bad_peer.stop()


def test_dead_sources(tmp: Path) -> None:
    print("\n同学地址连不上 / 教师机忙")
    folder, manifest = make_package(tmp / "dead")
    teacher = SegServer({manifest.id: FolderProvider(folder, manifest)}.get, max_uploads=1).start()
    teacher._slots.acquire()  # 教师机一开始是「满」的
    try:
        session = StreamSession(
            manifest, tmp / "dead_cache", ("127.0.0.1", teacher.port),
            sources_fn=lambda pkg, n: [("127.0.0.1", 1)],  # 没人听的端口
        )
        session.set_focus_time(0, urgent=True)
        session.start()
        time.sleep(1.5)
        check("教师机一直 503 时不崩、不写坏文件", session.cached == 0 and not list(session.dir.glob("seg_*.ts")))
        teacher._slots.release()
        check("教师机恢复后继续下完", asyncio_wait(lambda: session.cached == SEGMENTS, 15), f"{session.cached}/{SEGMENTS}")
        check("至少拒绝过几次", teacher.rejected >= 2, f"rejected={teacher.rejected}")
        session.stop()
    finally:
        teacher.stop()


# ------------------------------------------------------------------ 多个学生

async def test_swarm(tmp: Path) -> None:
    print("\n多个学生 + 真实 tracker：教师机完全不给切片，靠同学互相分发")
    folder, manifest = make_package(tmp / "swarm")
    teacher_http = SegServer({manifest.id: FolderProvider(folder, manifest)}.get, max_uploads=8).start()
    # 教师机的上传名额占满：除了 manifest/字幕外，一个切片都给不出去
    for _ in range(8):
        teacher_http._slots.acquire()

    server = SERVER
    empty_desktop = tmp / "desktop"; empty_desktop.mkdir()
    original = student_main.desktop_dir
    student_main.desktop_dir = lambda: empty_desktop
    students: list[Student] = []
    tasks = []
    try:
        for i in range(3):
            stu = Student(cache_dir=tmp / f"stu{i}")
            stu.mpv = FakeMPV()
            students.append(stu)
            tasks.append(asyncio.create_task(stu.run_forever()))
        ok = await wait_until(lambda: all(s.state.conn == st.CONNECTED for s in students))
        check("三个学生都连上了教师端", ok)

        # 学生 0 是「种子」：缓存里已经有整门课（比如上次下过）
        seed_dir = students[0]._cache_dir / manifest.id
        shutil.copytree(folder, seed_dir)

        server.broadcast({
            "cmd": protocol.PLAY, "video": manifest.source_name, "position": 0.0,
            "start_at": time.time() + 2.5,
            "package": {"id": manifest.id, "title": manifest.title, "http_port": teacher_http.port},
        })
        ok = await wait_until(lambda: all(s.state.stream for s in students), 5)
        check("没有本地视频 → 三个学生都进入切片模式", ok)

        ok = await wait_until(lambda: all(s.state.stream_have == SEGMENTS for s in students), 30)
        check("三个学生的缓存都补齐了", ok, str([s.state.stream_have for s in students]))
        seed, b, c = students
        check("种子学生没有从任何地方下载", seed.state.from_teacher == 0 and seed.state.from_peers == 0)
        check("学生 1、2 的切片全部来自同学，教师机一个没给",
              b.state.from_teacher == 0 and c.state.from_teacher == 0
              and b.state.from_peers + c.state.from_peers == 2 * SEGMENTS,
              f"b={b.state.from_peers} c={c.state.from_peers}")
        check("教师机的切片上传数为 0", teacher_http.uploads == 0, f"uploads={teacher_http.uploads}")
        check("学生之间互相也传了（不只种子在上传）",
              sum(s._seg_server.uploads for s in students[1:]) > 0)
        ok = await wait_until(lambda: all(s.state.play == st.PLAYING for s in students), 10)
        check("到 start_at 后三个学生都在播放", ok, str([s.state.play for s in students]))
        for i, s in enumerate(students):
            check(f"学生 {i} 的 mpv 指向本机切片服务，带字幕、带限制预读的参数",
                  s.mpv.url.startswith("http://127.0.0.1:") and "/play/" in s.mpv.url
                  and s.mpv.sub is not None and "--cache-secs=20" in s.mpv.extra,
                  s.mpv.url)

        for i, s in enumerate(students):
            problems = verify_package(s._cache_dir / manifest.id, manifest)
            check(f"学生 {i} 缓存内容全部通过校验", problems == [], str(problems))

        n, fraction = server.swarm_progress(manifest.id, SEGMENTS)
        check("tracker 统计到三台、平均 100%", n == 3 and abs(fraction - 1.0) < 1e-6, f"n={n} fraction={fraction}")

        # ---- 心跳不再纠偏 / seek 流程 / 停止
        server.broadcast({"cmd": protocol.SEEK, "position": 6.0, "start_at": time.time() + 0.5, "resume": True})
        await asyncio.sleep(1.2)
        check("seek 指令：每个学生都 seek 到 6 秒", all("seek:6.0" in s.mpv.calls for s in students),
              str([s.mpv.calls[-3:] for s in students]))
        server.broadcast({"cmd": protocol.STOP})
        ok = await wait_until(lambda: all(not s.state.stream for s in students), 5)
        check("STOP 后停止下载，状态复位", ok)
        check("STOP 后缓存文件仍保留（还能给同学）",
              all(len(list((s._cache_dir / manifest.id).glob("seg_*.ts"))) == SEGMENTS for s in students))

        # 学生 3 中途加入：这时只有同学能给
        late = Student(cache_dir=tmp / "late")
        late.mpv = FakeMPV()
        tasks.append(asyncio.create_task(late.run_forever()))
        await wait_until(lambda: late.state.conn == st.CONNECTED)
        server.broadcast({
            "cmd": protocol.PLAY, "video": manifest.source_name, "position": 0.0,
            "start_at": time.time() + 2.0,
            "package": {"id": manifest.id, "title": manifest.title, "http_port": teacher_http.port},
        })
        ok = await wait_until(lambda: late.state.stream_have == SEGMENTS, 30)
        check("重新播放时，新来的学生也能从同学那里补齐", ok, f"{late.state.stream_have}/{SEGMENTS}")
        check("新学生的来源全是同学", late.state.from_teacher == 0 and late.state.from_peers == SEGMENTS)
        students.append(late)
    finally:
        student_main.desktop_dir = original
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for s in students:
            s._stop_stream()
            if s._seg_server:
                s._seg_server.stop()
        teacher_http.stop()


async def test_slow_student_buffers(tmp: Path) -> None:
    print("\n掉队学生：mpv 来取没到的切片 → 缓冲状态；到了就恢复")
    config.PREFETCH_AHEAD = 1  # 只预缓存很少，这样后面的段一定还没下
    folder, manifest = make_package(tmp / "slow")
    teacher_http = SegServer({manifest.id: FolderProvider(folder, manifest)}.get, max_uploads=8).start()
    server = SERVER
    empty_desktop = tmp / "desktop2"; empty_desktop.mkdir()
    original = student_main.desktop_dir
    student_main.desktop_dir = lambda: empty_desktop
    stu = Student(cache_dir=tmp / "slowstu")
    stu.mpv = FakeMPV()
    task = asyncio.create_task(stu.run_forever())
    held = []
    try:
        await wait_until(lambda: stu.state.conn == st.CONNECTED)
        # 把教师机的名额占满，学生起播后拿不到后面的切片
        server.broadcast({
            "cmd": protocol.PLAY, "video": manifest.source_name, "position": 0.0,
            "start_at": time.time() + 1.5,
            "package": {"id": manifest.id, "title": manifest.title, "http_port": teacher_http.port},
        })
        ok = await wait_until(lambda: stu.state.stream_have >= 1, 10)
        for _ in range(8):  # 占满
            teacher_http._slots.acquire(); held.append(1)
        ok2 = await wait_until(lambda: stu.state.play == st.PLAYING, 10)
        check("先拿到起播段、到点开播", ok and ok2)
        have = stu.state.stream_have
        # 模拟 mpv 去要一个还没有的切片
        missing = next(i for i in range(SEGMENTS) if i not in stu._stream.have) if stu._stream else None
        result: dict = {}
        url = f"http://127.0.0.1:{stu._seg_server.port}/play/{manifest.id}/seg_{missing:05d}.ts"
        th = threading.Thread(target=lambda: result.update(r=http(url, timeout=20)))
        th.start()
        ok = await wait_until(lambda: stu.state.buffering, 3)
        check("mpv 在等切片 → 状态变成「缓冲中」", ok)
        # 缓冲期间心跳不该去 seek：不然会和缓冲打架，越纠越卡
        stu.mpv.calls.clear()
        server.broadcast({"cmd": protocol.HEARTBEAT, "playing": True, "position": 50.0, "server_time": time.time()})
        await asyncio.sleep(0.6)
        check("缓冲期间收到心跳：不纠偏", not any(c.startswith("seek") for c in stu.mpv.calls), str(stu.mpv.calls))
        for _ in held:
            teacher_http._slots.release()
        held.clear()
        th.join(15)
        check("名额恢复后切片到手，mpv 的请求完成", not th.is_alive() and result["r"][0] == 200)
        ok = await wait_until(lambda: not stu.state.buffering, 3)
        check("缓冲状态恢复", ok)
        check("缓存进度在增长", stu.state.stream_have > have, f"{have} → {stu.state.stream_have}")
    finally:
        student_main.desktop_dir = original
        for _ in held:
            teacher_http._slots.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        stu._stop_stream()
        if stu._seg_server:
            stu._seg_server.stop()
        teacher_http.stop()


async def test_local_video_wins(tmp: Path) -> None:
    print("\n学生桌面上有同名视频 → 仍然用本地的，不走切片")
    desktop = tmp / "desktop3"; desktop.mkdir()
    (desktop / "课.mkv").write_bytes(b"x")
    original = student_main.desktop_dir
    student_main.desktop_dir = lambda: desktop
    server = SERVER
    stu = Student(cache_dir=tmp / "localstu")
    stu.mpv = FakeMPV()
    task = asyncio.create_task(stu.run_forever())
    try:
        await wait_until(lambda: stu.state.conn == st.CONNECTED)
        server.broadcast({
            "cmd": protocol.PLAY, "video": "课.mkv", "position": 0.0, "start_at": time.time() + 0.5,
            "package": {"id": "abc123", "title": "课", "http_port": 1},
        })
        ok = await wait_until(lambda: stu.state.play == st.PLAYING, 5)
        check("用本地视频播放", ok and stu.mpv.url.endswith("课.mkv") and not stu.state.stream, stu.mpv.url)
        check("没有为此打开切片服务", stu._seg_server is None)
    finally:
        student_main.desktop_dir = original
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_cleanup(tmp: Path) -> None:
    print("\n缓存清理")
    root = tmp / "cache_root"
    (root / "123-1" / "abc").mkdir(parents=True)
    (root / "123-1" / "abc" / "seg_00000.ts").write_bytes(b"x")
    (root / "stray.txt").write_text("x")
    streaming.cleanup_cache(root)
    check("启动时清掉上次的全部缓存", root.exists() and not any(root.iterdir()))
    streaming.cleanup_cache(tmp / "does_not_exist")
    check("目录不存在也不报错", True)


async def main() -> int:
    global SERVER
    config.PREFETCH_AHEAD = SEGMENTS  # 默认窗口够大，除非某个测试专门测窗口
    SERVER = Server(lambda message, peer: None)
    SERVER.start(); SERVER.ready.wait(5)
    await asyncio.sleep(0.2)
    tmp = Path(tempfile.mkdtemp(prefix="lvs_stream_"))
    try:
        test_package(tmp)
        test_segserver(tmp)
        await asyncio.to_thread(test_play_blocks, tmp)
        await asyncio.to_thread(test_downloader, tmp)
        config.PREFETCH_AHEAD = SEGMENTS
        await asyncio.to_thread(test_bad_peer, tmp)
        await asyncio.to_thread(test_dead_sources, tmp)
        test_cleanup(tmp)
        await test_swarm(tmp)
        await test_slow_student_buffers(tmp)
        await test_local_video_wins(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)}，失败 {len(FAILED)}")
    for name in FAILED:
        print(f"  失败: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.stdout.flush()
    os._exit(code)
