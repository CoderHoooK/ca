# LanVideoSync V1

最小局域网同步播放原型。

## 开发测试
Windows + Python 3.11+

    pip install -r requirements.txt

教师端：
    python teacher/teacher.py

学生端：
    python student/student.py

学生电脑桌面放：
    lesson01.mkv
    lesson01.ass

教师端选择 MKV，点击“同步播放”。

## mpv
将 Windows 版 mpv 的 `mpv.exe` 放到：
    mpv/mpv.exe

注意：这一版先把“自动发现 + 本地 MKV/ASS + 同步播放”跑通。
正式 V2 应改用 mpv IPC，实现真正的 PAUSE/STOP/SEEK 和更精确的预加载同步。
