"""教师端 ↔ 学生端 的消息格式。

所有消息都是 dict，cmd 字段决定类型。局域网里只走控制指令，视频文件本身
永远不传——学生机上的视频是预先放好的。
"""

from __future__ import annotations

import json

# ---- 教师端 → 学生端 ----

PLAY = "PLAY"            # {video, position, start_at}
PAUSE = "PAUSE"          # {}                        状态切换，不需要时间戳
RESUME = "RESUME"        # {position, start_at}
SEEK = "SEEK"            # {position, start_at, resume}
STOP = "STOP"            # {}                        状态切换，不需要时间戳
HEARTBEAT = "HEARTBEAT"  # {playing, position, server_time}
PING = "PING"            # {t0}
PONG = "PONG"            # {t0, t_teacher}

# ---- 学生端 → 教师端 ----

VIDEO_NOT_FOUND = "VIDEO_NOT_FOUND"  # {video}


def encode(msg: dict) -> str:
    return json.dumps(msg, ensure_ascii=False)


def decode(raw: str | bytes) -> dict:
    return json.loads(raw)
