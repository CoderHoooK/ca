import asyncio, json, socket, threading, time
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFileDialog, QMessageBox
import websockets

HOST, PORT = "0.0.0.0", 8765
clients, lock = set(), threading.Lock()

def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except Exception: return "127.0.0.1"
    finally: s.close()

async def handler(ws):
    with lock: clients.add(ws)
    try:
        await ws.send(json.dumps({"cmd":"HELLO"}))
        async for _ in ws: pass
    finally:
        with lock: clients.discard(ws)

async def broadcast(msg):
    data = json.dumps(msg, ensure_ascii=False)
    with lock: targets = list(clients)
    if targets:
        await asyncio.gather(*(w.send(data) for w in targets), return_exceptions=True)

class Server(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True); self.loop=None
    def run(self):
        self.loop=asyncio.new_event_loop(); asyncio.set_event_loop(self.loop)
        async def start():
            server=await websockets.serve(handler, HOST, PORT)
            await server.wait_closed()
        self.loop.run_until_complete(start())
    def send(self,msg):
        if self.loop:
            asyncio.run_coroutine_threadsafe(broadcast(msg), self.loop)

class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LanVideoSync - 教师端"); self.resize(520,260)
        self.server=Server(); self.server.start(); self.video=None
        self.info=QLabel(); self.file=QLabel("未选择视频"); self.count=QLabel()
        choose=QPushButton("选择 MKV"); play=QPushButton("▶ 同步播放")
        pause=QPushButton("⏸ 同步暂停"); stop=QPushButton("⏹ 同步停止")
        choose.clicked.connect(self.choose); play.clicked.connect(self.play)
        pause.clicked.connect(lambda:self.send({"cmd":"PAUSE"}))
        stop.clicked.connect(lambda:self.send({"cmd":"STOP"}))
        l=QVBoxLayout(self); l.addWidget(self.info); l.addWidget(self.count); l.addWidget(self.file)
        r=QHBoxLayout(); [r.addWidget(x) for x in (choose,play,pause,stop)]; l.addLayout(r)
        self.timer=QTimer(); self.timer.timeout.connect(self.refresh); self.timer.start(500)
        self.refresh()
    def refresh(self):
        self.info.setText(f"教师端：{local_ip()}:{PORT}")
        with lock: n=len(clients)
        self.count.setText(f"学生端连接：{n}")
    def choose(self):
        p,_=QFileDialog.getOpenFileName(self,"选择视频",str(Path.home()/"Desktop"),"MKV (*.mkv)")
        if p:
            self.video=Path(p); self.file.setText("视频："+self.video.name)
    def send(self,msg):
        if not self.video and msg["cmd"]=="PLAY":
            QMessageBox.warning(self,"提示","请先选择 MKV。"); return
        if self.video: msg["video"]=self.video.name
        msg["server_time"]=time.time(); self.server.send(msg)
    def play(self):
        self.send({"cmd":"PLAY","position":0.0,"start_at":time.time()+1.5})

app=QApplication([])
w=Window(); w.show(); app.exec()
