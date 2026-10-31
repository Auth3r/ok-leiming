"""Focus lease for opening attempts only; no inputs during the countdown."""
import os
import time
import win32gui
import win32process
from src.interaction import activate_window

last_external = None


class Desktop:
    own_pid=os.getpid()
    def foreground(self):return win32gui.GetForegroundWindow()
    def identity(self, hwnd):
        if not hwnd or not win32gui.IsWindow(hwnd):return None
        try:return (hwnd,win32process.GetWindowThreadProcessId(hwnd)[1])
        except Exception:return None
    def activate(self, hwnd):activate_window(hwnd)


def remember_external(game_hwnd):
    global last_external
    desktop=Desktop()
    identity=desktop.identity(desktop.foreground())
    if identity and identity[0]!=game_hwnd and identity[1]!=desktop.own_pid:
        last_external=identity


class OpeningFocus:
    status_key='抢开服状态'
    def __init__(self, task, game_hwnd, desktop=None, clock=time.monotonic):
        self.task=task;self.game_hwnd=game_hwnd;self.desktop=desktop or Desktop()
        self.clock=clock;self.paused_seconds=0;self.armed=False
        self.return_to=None;self.in_cycle=False

    def poll(self):
        # Focus changes are handled by ensure_game before input, not by pausing.
        pass

    def begin(self):
        self.poll()
        if self.in_cycle:return
        identity=self.desktop.identity(self.desktop.foreground())
        self.return_to=identity if identity and identity[0]!=self.game_hwnd and identity[1]!=self.desktop.own_pid else last_external
        self.in_cycle=True
        self.ensure_game()

    def ensure_game(self):
        self.poll()
        if self.desktop.foreground()!=self.game_hwnd:self.desktop.activate(self.game_hwnd)
        if self.desktop.foreground()!=self.game_hwnd:raise RuntimeError('未能切回游戏，尚未点击')
        self.armed=True

    def finish(self):
        self.poll()
        self.in_cycle=False
        target=self.return_to;self.return_to=None
        if not target:return
        if self.desktop.identity(target[0])!=target:
            self.task.log_warning('原窗口已关闭或已更换，跳过恢复；继续等待下一轮')
            return
        try:
            self.desktop.activate(target[0])
        except Exception as error:
            self.task.log_warning(f'无法恢复原窗口，继续等待：{error}')
