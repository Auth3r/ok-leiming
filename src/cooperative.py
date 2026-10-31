"""Foreground leases for daily modules; never sends background game input."""
import time
import win32api
from src.opening_focus import OpeningFocus, Desktop
from src.workflow import Workflow, SafetyStop
from src.interaction import ForegroundUnavailable


class FocusChanged(SafetyStop):
    pass


class CursorDesktop(Desktop):
    def cursor(self):return win32api.GetCursorPos()
    def move_cursor(self,point):win32api.SetCursorPos(point)


class DailyFocus(OpeningFocus):
    status_key='窗口接管'
    def __init__(self,task,hwnd,desktop=None,clock=time.monotonic):
        super().__init__(task,hwnd,desktop or CursorDesktop(),clock)
        self.origin_cursor=None;self.expected_cursor=None;self.user_moved=False

    def observe_cursor(self):
        if self.in_cycle and self.expected_cursor is not None:
            if self.desktop.cursor()!=self.expected_cursor:self.user_moved=True

    def poll(self):
        self.observe_cursor()
        super().poll()

    def begin(self):
        if not self.in_cycle:
            self.origin_cursor=self.desktop.cursor()
            self.expected_cursor=self.origin_cursor
            self.user_moved=False
        super().begin()
        self.task.info_set(self.status_key,'正在临时操作游戏；F8取消')

    def after_input(self):
        self.expected_cursor=self.desktop.cursor()

    def ensure_game(self):
        try:super().ensure_game()
        except ForegroundUnavailable:
            self.task.log_info('自动接管受限，请在20秒内手动切到游戏；F8可取消。',notify=True)
            deadline=self.clock()-self.paused_seconds+20
            while self.desktop.foreground()!=self.game_hwnd:
                self.poll()
                if self.clock()-self.paused_seconds>=deadline:
                    raise ForegroundUnavailable('等待手动切回游戏超时，未点击')
                self.task.sleep(.1)
            self.armed=True

    def finish(self):
        if not self.in_cycle:return
        self.observe_cursor()
        target=self.return_to
        super().finish()
        # Restore only if the original window really regained focus and the pointer
        # has not moved since our last input. Physical movement during an input
        # itself cannot be distinguished reliably by polling.
        if (target and self.desktop.foreground()==target[0] and not self.user_moved
                and self.desktop.cursor()==self.expected_cursor):
            self.desktop.move_cursor(self.origin_cursor)
        self.task.info_set(self.status_key,'已归还窗口；模块间等待中')


class CooperativeWorkflow(Workflow):
    def checkpoint(self):
        super().checkpoint()
        self.task.daily_focus.poll()
        super().checkpoint()

    def click(self,*args,**kwargs):
        for attempt in range(self.cfg['max_retries']+1):
            try:return super().click(*args,**kwargs)
            except FocusChanged:
                if attempt==self.cfg['max_retries']:raise
                self.checkpoint();self.delay(.1)
