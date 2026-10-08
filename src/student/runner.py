"""在后台线程里跑学生端的事件循环，让 Qt 主线程专心画界面。"""

from __future__ import annotations

import asyncio
import threading


class Runner(threading.Thread):
    """在后台线程里跑学生端的事件循环，让 Qt 主线程专心画界面。"""

    def __init__(self, student) -> None:
        super().__init__(daemon=True, name="student-loop")
        self.student = student
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        try:
            await self.student.run_forever()
        except asyncio.CancelledError:
            pass

    def stop(self, timeout: float = 5.0) -> None:
        """停掉循环并关掉 mpv。从界面线程调用。"""
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)
        self.join(timeout)
