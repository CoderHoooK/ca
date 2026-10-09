"""教师端 ↔ 学生端 的消息格式。

所有消息都是 dict，cmd 字段决定类型。局域网里只走控制指令，视频文件本身
永远不传——学生机上的视频是预先放好的。
"""

from __future__ import annotations

import json

# ---- 教师端 → 学生端 ----

PLAY = "PLAY"            # {video, position, start_at, package?}  package={id,title,http_port}：切片课程
PAUSE = "PAUSE"          # {}                        状态切换，不需要时间戳
RESUME = "RESUME"        # {position, start_at}
SEEK = "SEEK"            # {position, start_at, resume}
STOP = "STOP"            # {}                        状态切换，不需要时间戳
HEARTBEAT = "HEARTBEAT"  # {playing, position, server_time}
PING = "PING"            # {t0}
PONG = "PONG"            # {t0, t_teacher, name, id}  name/id 是教师机的主机名和实例标识，可缺省

# ---- 学生端 → 教师端 ----

VIDEO_NOT_FOUND = "VIDEO_NOT_FOUND"  # {video}
PEER_HELLO = "PEER_HELLO"            # {http_port}        我能给别的学生机提供切片的端口
HAVE = "HAVE"                        # {pkg, add:[idx..]} 我又缓存好了这几段
SOURCES = "SOURCES"                  # {req, pkg, n:[idx..]}  这几段谁有？
STATUS = "STATUS"                    # {name, play, stream, have, total, from_teacher, from_peers, buffering}
#                                      学生机的当前状态，教师端的「学生机列表」用；每秒左右报一次

# ---- 教师端 → 学生端（切片传输的 tracker 应答）----

SOURCES_REPLY = "SOURCES_REPLY"      # {req, pkg, sources:{"idx":["host:port",..]}}


def encode(msg: dict) -> str:
    return json.dumps(msg, ensure_ascii=False)


def decode(raw: str | bytes) -> dict:
    return json.loads(raw)
