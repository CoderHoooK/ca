"""全局常量。要调参改这里就够了，不用翻别的文件。"""

# WebSocket 控制通道
WS_PORT = 8765

# UDP 广播发现
DISCOVERY_PORT = 8766
DISCOVERY_MAGIC = "LAN_VIDEO_SYNC_DISCOVER"

# 学生端扫桌面时认这些后缀，教师端的文件选择框也按它生成过滤器。
# 要加格式只改这一行（大小写不敏感，.MP4 一样认）。
# mpv 就是 ffmpeg，这些容器都能放；列太多反而容易把不想放的东西扫进来。
VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts")

# 起播提前量。学生端要在这段时间里启动 mpv、加载视频，所以给得宽一点。
PLAY_LEAD = 3.0

# 暂停态下的定位/继续。此时 mpv 已经在跑了，不需要那么久。
CMD_LEAD = 0.8

# 纠偏阈值：位置偏差超过这个秒数才 seek。低于它就别动，免得画面一直抽搐。
DRIFT_THRESHOLD = 0.5

# 教师端心跳间隔，学生端靠它自己纠偏
HEARTBEAT_INTERVAL = 1.0

# 学生端找不到教师端时的重试间隔
REDISCOVER_INTERVAL = 2.0

# 自动搜索连续失败这么多轮之后，学生端会自动做一次「深度扫描」（见 net.scan）。
# 广播被交换机/路由器挡掉的网络里，这是不用人操作就能找到教师机的办法。
# 设成 0 关闭自动深度扫描（学生仍可在界面上手动点「扫描教师机」）。
AUTO_DEEP_SCAN_AFTER = 3

# 深度扫描自动重复的最小间隔（秒）。扫一次要往本网段每个地址发一个小包，
# 不用也不该每 2 秒来一遍。
AUTO_DEEP_SCAN_INTERVAL = 30.0

# 单次扫描等待应答的时间（秒）
SCAN_TIMEOUT = 1.5
DEEP_SCAN_TIMEOUT = 2.5

# 单条 mpv 命令的超时。太小会在慢机器上误报，太大会卡住调用方。
#
# 3.0 实测偏小：同一台机器上 4 个 mpv 一起启动时，set_property pause
# 偶尔要 3 秒以上才回，学生端就会错过起播时刻（之后靠心跳纠偏救回来，
# 但白晚了一两秒）。真部署时一台机器只有一个 mpv，会比这宽松得多。
MPV_TIMEOUT = 6.0
