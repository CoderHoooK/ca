LanVideoSync

1. 项目目标

开发一个 Windows 局域网视频同步播放软件，使用场景是大学机房。

核心需求只有一个：

«教师机选择一个视频并点击播放后，所有已经启动学生端的电脑自动找到本地桌面上的对应 MKV 视频和 ASS 字幕，并尽可能精确地同步播放。»

视频文件不通过局域网传输。

局域网只传输：

- 播放
- 暂停
- 停止
- 跳转
- 同步时间
- 当前视频文件名

等控制信息。

---

2. 软件组成

整个项目只有两个程序：

Teacher.exe
Student.exe

Teacher.exe

运行在教师电脑。

负责：

- 启动局域网控制服务
- 自动发现/管理学生端连接
- 选择本地 MKV 视频
- 自动匹配同名 ASS 字幕
- 同步播放
- 同步暂停
- 同步停止
- 同步跳转
- 显示当前连接的学生端数量
- 显示当前视频播放状态

Student.exe

运行在学生电脑。

学生端必须做到：

«学生打开 Student.exe 后，不需要进行任何操作。»

启动后自动：

1. 扫描桌面上的 MKV 文件
2. 建立视频文件索引
3. 自动寻找同名 ASS 字幕
4. 自动寻找教师端
5. 建立 WebSocket 连接
6. 进入等待状态
7. 收到教师指令后自动执行

---

3. 学生端视频规则

假设学生电脑桌面：

Desktop/
├── lesson01.mkv
├── lesson01.ass
├── lesson02.mkv
└── lesson02.ass

教师端选择：

lesson01.mkv

教师发送：

PLAY lesson01.mkv

学生端必须寻找：

Desktop/lesson01.mkv
Desktop/lesson01.ass

如果存在：

lesson01.mkv
lesson01.ass

则使用它们播放。

如果不存在 ASS：

«允许继续播放 MKV，不应导致程序崩溃。»

如果找不到 MKV：

«不应该弹出大量错误窗口。»

学生端向教师端报告：

VIDEO_NOT_FOUND

教师端可以显示：

部分学生机没有找到视频

---

4. 视频播放器

使用：

mpv

不要自己实现视频解码。

学生端负责：

通信
↓
控制 mpv

mpv 负责：

MKV 解码
ASS 字幕
音频
视频渲染
硬件解码

---

5. mpv 控制方式

正式实现必须使用：

mpv IPC

不要通过：

重新启动 mpv

来实现暂停、跳转等功能。

启动 mpv 时开启 IPC，例如：

--input-ipc-server=...

Student.exe 与 mpv 建立 IPC 通信。

程序可以发送：

set_property
get_property
seek
observe_property

等命令。

需要实现：

播放
暂停
停止
seek
获取当前播放位置
获取播放状态

---

6. 局域网通信

使用：

WebSocket

原因：

- Python/C# 等语言都容易实现
- 双向通信
- 长连接
- 适合教师端实时控制学生端
- 控制数据量极小

不要传输视频。

例如：

{
    "type": "play",
    "video": "lesson01.mkv",
    "position": 0,
    "start_at": 1758470500.500
}

---

7. 教师端发现学生端

学生端启动以后自动寻找教师端。

不要让用户手动输入 IP。

推荐：

UDP 广播发现 + WebSocket 建立正式连接

流程：

Student.exe
    ↓
UDP 广播
"LAN_VIDEO_SYNC_DISCOVER"
    ↓
Teacher.exe
    ↓
回复教师端 IP + WebSocket Port
    ↓
Student.exe
    ↓
建立 WebSocket

这样教师机 DHCP 获取到什么 IP 都没关系。

不要把教师机 IP 写死在代码里。

---

8. 学生端连接状态

Student.exe 启动：

启动
 ↓
寻找教师端
 ↓
找到
 ↓
建立 WebSocket
 ↓
等待指令

如果教师端暂时没有启动：

继续寻找

不要退出。

如果连接断开：

自动重连

不要要求学生重新打开程序。

---

9. 核心同步机制

这是整个项目最重要的部分。

不要简单实现：

教师发送 PLAY
 ↓
学生收到
 ↓
立即播放

因为不同电脑收到指令的时间不同。

应该使用：

未来时间戳同步播放。

例如教师发送：

{
    "type": "PLAY",
    "video": "lesson01.mkv",
    "position": 0,
    "start_at": 1758470505.000
}

其中：

start_at

是未来的某一个时间点。

例如：

当前时间：10:00:00
start_at：10:00:03

学生端收到指令后：

加载视频
↓
加载 ASS
↓
准备 mpv
↓
等待 start_at
↓
开始播放

不要收到消息就立即播放。

---

10. 时间同步

为了减少不同电脑系统时间造成的误差：

学生端连接教师端时进行简单的时间同步。

可以使用：

PING
PONG

计算：

RTT

并估计：

clock_offset

例如：

教师时间
学生本地时间
网络 RTT

得到：

offset = teacher_time - student_time

学生端计算教师时间：

teacher_now = local_now + offset

之后：

start_at - teacher_now

得到准确等待时间。

---

11. 播放流程

教师点击：

同步播放

Teacher：

1. 确认视频
2. 生成未来 start_at
3. 广播 PLAY

Student：

1. 收到 PLAY
2. 找本地 MKV
3. 找同名 ASS
4. 启动/准备 mpv
5. 设置播放位置
6. 等待 start_at
7. 执行 play

---

12. 暂停

教师点击：

暂停

不要重新启动视频。

直接通过 mpv IPC：

set pause true

教师端广播：

{
    "type": "PAUSE"
}

学生端：

收到 PAUSE
 ↓
mpv IPC
 ↓
pause = true

---

13. 恢复播放

教师点击：

继续播放

教师端获取当前播放位置：

position = 125.32

然后广播：

{
    "type": "RESUME",
    "position": 125.32,
    "start_at": 1758470505.000
}

学生端：

seek 125.32
 ↓
等待 start_at
 ↓
play

---

14. 停止

教师点击：

停止

广播：

{
    "type": "STOP"
}

学生端：

mpv IPC
 ↓
quit

或者保留 mpv 窗口但停止播放。

V1 推荐直接关闭播放器。

---

15. Seek

教师拖动进度条。

例如：

02:35.2

发送：

{
    "type": "SEEK",
    "position": 155.2,
    "start_at": 1758470505.000
}

学生端：

seek 155.2
 ↓
等待 start_at
 ↓
继续播放

---

16. 自动纠偏

播放过程中，学生端定期获取：

mpv 当前 position

教师端也维护当前 position。

如果：

abs(student_position - teacher_position) < 0.1s

不处理。

如果：

0.1s ~ 0.5s

可以通过调整播放速度轻微纠偏。

如果：

> 0.5s

直接：

seek

到正确位置。

V1 可以先实现：

偏差 > 0.5 秒
→ seek

不要一开始把算法做得过度复杂。

---

17. 教师端 UI

保持极简。

不要做复杂机房管理界面。

推荐：

┌────────────────────────────────────┐
│         LanVideoSync 教师端         │
├────────────────────────────────────┤
│                                    │
│ 视频：lesson01.mkv                 │
│                                    │
│ [选择视频]                         │
│                                    │
│ ━━━━━━━━━━━━━━━●━━━━━━             │
│ 02:35 / 12:30                      │
│                                    │
│ [▶ 同步播放] [⏸ 暂停] [⏹ 停止]     │
│                                    │
│ 已连接学生机：38                   │
│                                    │
│ 状态：正常                         │
└────────────────────────────────────┘

第一版不需要：

- 聊天
- 文件传输
- 屏幕控制
- 锁屏
- 远程关机
- 用户账号
- 权限系统
- 数据库
- 云服务器

全部不要做。

---

18. 学生端 UI

学生端最好是：

无界面 / 后台运行。

如果必须显示状态，只显示一个非常简单的窗口：

LanVideoSync

状态：已连接教师端
视频：lesson01.mkv
状态：等待播放

学生不需要点击任何按钮。

最好支持：

--silent

后台模式。

---

19. Windows 开机启动

后期增加：

Student.exe

开机自动启动。

这样机房实际使用流程：

电脑开机
 ↓
Student.exe 自动启动
 ↓
自动连接教师端
 ↓
等待
 ↓
老师点击播放
 ↓
自动播放

学生完全不用操作。

---

20. 打包

最终必须生成：

Teacher.exe
Student.exe

学生电脑：

不需要安装 Python。

使用：

PyInstaller

进行打包。

Python：

源代码
 ↓
PyInstaller
 ↓
Student.exe

所有 Python runtime 和依赖一起打包。

---

21. mpv 分发

最终目录可以：

Student/
├── Student.exe
└── mpv/
    ├── mpv.exe
    └── 相关 DLL

Student.exe 自动寻找：

./mpv/mpv.exe

不要要求学生安装 mpv。

教师端同样不需要安装 mpv，因为教师端主要负责控制。

---

22. 开发阶段

不要一开始就在真实机房测试。

先在开发电脑上完成。

第一阶段：

Teacher.exe
Student.exe

同一台电脑启动：

Teacher
Student 1
Student 2
Student 3

验证：

发现
连接
播放
暂停
停止
Seek

第二阶段：

使用两台电脑：

电脑 A
Teacher

电脑 B
Student

通过 Wi-Fi/局域网测试。

第三阶段：

Teacher
+
5~10 个 Student

模拟多机。

最后才去真实机房测试。

---

23. 开发顺序

Claude Code 必须严格按照以下顺序开发。

Phase 1：项目基础

实现：

- 项目目录
- Teacher
- Student
- 配置文件
- 日志系统
- 基础 WebSocket

暂时不实现复杂同步。

完成标准：

Teacher 启动
Student 启动
Student 自动发现 Teacher
Student 自动连接
Teacher 能看到连接数量

---

Phase 2：mpv 集成

实现：

- 自动寻找 mpv
- 启动 mpv
- IPC
- 打开 MKV
- 自动加载同名 ASS
- 获取播放位置
- play
- pause
- stop
- seek

完成标准：

Student 可以独立控制本地视频。

---

Phase 3：同步播放

实现：

- 时间同步
- start_at
- 未来时间点播放
- 多 Student 同时播放

完成标准：

同一台电脑开启多个 Student 进程时，可以明显观察到同步播放。

---

Phase 4：完整教师控制

实现：

播放
暂停
继续
停止
Seek

完成标准：

教师端操作可以同步控制所有学生端。

---

Phase 5：断线重连

实现：

Student 断网
 ↓
自动重连

Teacher 重启：

Student 自动重新发现
 ↓
自动连接

---

Phase 6：打包

使用 PyInstaller。

生成：

Teacher.exe
Student.exe

学生机不需要 Python。

---

Phase 7：真实局域网测试

测试：

- Windows 防火墙
- Wi-Fi
- 网线
- 不同电脑 IP
- 多台 Student
- 4K MKV
- H.264
- H.265/HEVC
- ASS 字幕
- 断网重连
- 电脑性能差异

---

24. 代码质量要求

不要为了快速实现而把所有代码写在一个文件。

推荐：

src/
├── teacher/
│   ├── main.py
│   ├── ui.py
│   ├── server.py
│   └── controller.py
│
├── student/
│   ├── main.py
│   ├── discovery.py
│   ├── client.py
│   ├── player.py
│   └── sync.py
│
└── common/
    ├── protocol.py
    ├── time_sync.py
    └── logger.py

模块职责清晰。

---

25. 非目标

以下功能现在禁止实现：

- 视频网络传输
- 云服务器
- 登录系统
- 数据库
- 学生账号
- 文件上传
- 屏幕广播
- 远程桌面
- 远程控制鼠标键盘
- 聊天
- 作业系统
- 考试系统
- 机房资产管理

项目唯一核心：

«局域网多机同步播放本地 MKV + ASS。»

---

26. 最终验收标准

最终在一台开发电脑上：

Teacher.exe
Student.exe × 5

桌面：

movie.mkv
movie.ass

教师：

选择 movie.mkv
点击同步播放

5 个 Student：

自动找到 movie.mkv
自动找到 movie.ass
自动启动 mpv
自动等待
同时播放

教师点击：

暂停

所有学生暂停。

教师点击：

继续

所有学生继续。

教师拖动：

05:30

所有学生跳到约：

05:30

教师点击：

停止

所有学生停止。

整个过程中：

«学生不需要进行任何操作。»

---

27. 给 Claude Code 的执行要求

不要一次性生成一个巨大代码文件。

按照 Phase 1 → Phase 7 顺序开发。

每完成一个 Phase：

1. 运行测试
2. 修复问题
3. 更新 README
4. 保持已有功能不被破坏
5. 再进入下一阶段

如果发现技术方案存在问题，可以调整内部实现，但不能改变核心产品需求：

«视频文件本地存储，局域网只传控制指令，学生端无需操作，教师端控制所有学生端同