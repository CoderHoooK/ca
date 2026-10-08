import asyncio, json, socket, subprocess, time
from pathlib import Path
import websockets

PORT=8765

def desktop(): return Path.home()/"Desktop"

def find_teacher():
    local=socket.gethostbyname(socket.gethostname())
    parts=local.split(".")
    if len(parts)!=4: return "127.0.0.1"
    for i in range(1,255):
        ip=f"{parts[0]}.{parts[1]}.{parts[2]}.{i}"
        if ip==local: continue
        s=socket.socket(); s.settimeout(0.08)
        try:
            if s.connect_ex((ip,PORT))==0: return ip
        finally: s.close()
    return "127.0.0.1"

def video_map():
    return {p.name:p for p in desktop().glob("*.mkv")}

def subtitle_for(video):
    p=video.with_suffix(".ass")
    return p if p.exists() else None

class Student:
    def __init__(self):
        self.proc=None
    def play(self,name):
        video=video_map().get(name)
        if not video: return
        if self.proc and self.proc.poll() is None: self.proc.terminate()
        mpv=Path(__file__).resolve().parent.parent/"mpv"/"mpv.exe"
        if not mpv.exists(): mpv="mpv"
        cmd=[str(mpv),"--force-window=yes",str(video)]
        sub=subtitle_for(video)
        if sub: cmd.append("--sub-file="+str(sub))
        self.proc=subprocess.Popen(cmd)
    def stop(self):
        if self.proc and self.proc.poll() is None: self.proc.terminate()

async def main():
    s=Student()
    while True:
        try:
            host=find_teacher()
            async with websockets.connect(f"ws://{host}:{PORT}",open_timeout=3) as ws:
                async for raw in ws:
                    m=json.loads(raw); c=m.get("cmd")
                    if c=="PLAY":
                        start=float(m.get("start_at",time.time()))
                        delay=max(0,start-time.time())
                        if delay: await asyncio.sleep(delay)
                        await asyncio.to_thread(s.play,m.get("video",""))
                    elif c=="STOP":
                        await asyncio.to_thread(s.stop)
                    # PAUSE/SEEK budou v další verzi přes mpv IPC.
        except Exception:
            await asyncio.sleep(2)

asyncio.run(main())
