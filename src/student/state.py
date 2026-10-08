"""学生端的运行状态。

逻辑层（main.py 的 Student）往这里写，界面层（ui.py）每隔几百毫秒读一次。
都是简单属性赋值，Python 里单个属性的读写是原子的，所以不用加锁；
界面只是「看」，偶尔读到新旧混合的一帧也无所谓，下一帧就对了。

放在单独文件里，是为了让无界面模式（--silent）不用 import Qt。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from common.net import Teacher

# 连接状态
SEARCHING = "searching"      # 正在找教师机
CONNECTING = "connecting"    # 找到了，正在连
CONNECTED = "connected"      # 已连接，等指令
DISCONNECTED = "disconnected"  # 连上过，断了

# 播放状态
IDLE = "idle"              # 待机
LOADING = "loading"        # 正在加载视频
WAITING = "waiting"        # 已就绪，等 start_at
PLAYING = "playing"
PAUSED = "paused"
NOT_FOUND = "not_found"    # 桌面上没有教师要放的视频
ERROR = "error"            # mpv 启动失败


@dataclass
class StudentState:
    # ---- 连接 ----
    conn: str = SEARCHING
    teacher_host: str = ""
    teacher_port: int = 0
    teacher_name: str = ""
    rtt: float | None = None
    offset: float = 0.0
    attempts: int = 0            # 连续失败次数，连上就清零
    last_error: str = ""         # 最近一次失败的原因，给学生看

    # ---- 教师选择 ----
    pinned: bool = False         # True：学生手动指定了教师机，不再自动搜索
    teachers: list[Teacher] = field(default_factory=list)  # 最近一次扫描的结果
    scanning: bool = False
    scan_note: str = ""          # 「找到 2 台」「没有找到」之类

    # ---- 播放 ----
    play: str = IDLE
    video: str = ""
    has_subtitle: bool = False
    teacher_position: float | None = None   # 教师端心跳里的位置（秒）

    # ---- 切片播放（桌面上没有视频时，从教师机/同学那里拉）----
    stream: bool = False         # 当前视频是不是靠切片播的
    stream_have: int = 0         # 已缓存的段数
    stream_total: int = 0
    from_teacher: int = 0        # 其中从教师机拉的段数
    from_peers: int = 0          # 从同学机器拉的段数
    buffering: bool = False      # mpv 正在等某一段（画面卡住）

    # ---- 本机环境 ----
    mpv_ok: bool = True          # 找得到 mpv.exe
    desktop_videos: int = 0      # 桌面上能认出的视频数量
