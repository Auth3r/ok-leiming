"""Daily/module progress uses the existing nonactivating capture-safe HUD."""
import time
from PySide6.QtCore import QTimer
from src.roguelike_hud import RoguelikeHud
from src.task_controls import control_for, control_status
from src.runtime_identity import get_runtime_identity

PAGES={'MAIN_MENU':'主界面','BLACK_SHIELD':'黑盾领取','FUND':'理财基金',
       'DISPATCH':'黑盾派遣','BLACK_DISPATCH':'黑盾派遣','DARK_TIDE':'暗涌','GANHUO':'干火',
       'HUNT_GOLD':'猎金','DAILY_REWARD':'每日奖励','MODULE_PREPARE':'模组准备',
       'MODULE_CRAFT':'模组制作','NAV':'任务导航','REWARD':'领取奖励','DONE':'本轮结束'}
STOP_REASONS={'F8':'按 F8 停止','GUI 停止':'点击停止按钮','task_error':'任务已停止',
              'stop_button_or_executor':'任务已停止','opening_error':'开服任务已停止',
              'opening_setup':'开服初始化失败'}


class TaskHudState:
    def __init__(self,task):self.task=task
    def lines(self):
        task=self.task;info=getattr(task,'info',{});control=control_for(task)
        page=str(info.get('当前步骤') or info.get('当前状态') or info.get('抢开服状态') or '正在读取当前页面')
        page=PAGES.get(page.removeprefix('State.'),page)
        source=control.stop_source if control else ''
        reason=str(info.get('停止原因') or STOP_REASONS.get(source,'任务已停止' if source else ''))
        progress=info.get('暗涌进度') if str(info.get('当前状态','')).removeprefix('State.')=='DARK_TIDE' else ''
        detail='原因：'+reason if reason else str(progress or info.get('结果') or '')
        if info.get('当前模块'):
            page=str(info.get('当前模块'))+' '+str(info.get('路线进度',''))+' · '+page
            detail=str(info.get('路线汇总') or ('下一项：'+str(info.get('下一模块',''))))
        return (task.name+' · '+control_status(task),'当前：'+page,detail,
                'F7 暂停/继续 · F8 停止','',get_runtime_identity().display_version)


class TaskHudManager:
    """One owner per run; terminal evidence stays visible briefly, then closes."""
    def __init__(self,window,clock=time.monotonic):
        self.window=window;self.clock=clock;self.task=None;self.control=None
        self.terminal_at=None;self.failed=False;self.hud=RoguelikeHud(window)
        self.timer=QTimer(window);self.timer.timeout.connect(self.poll);self.timer.start(100)
        window.destroyed.connect(self.timer.stop)

    def poll(self):
        from ok import og
        panel=getattr(getattr(self.window,'start_tab',None),'runtime_720p_panel',None)
        native=getattr(panel,'control',None)
        if native and native.state not in native.TERMINAL:
            self.hud.hide();self.task=self.control=None;return
        active=getattr(og.executor,'current_task',None)
        # Crafting owns the older SkillRecordWindow, including terminal
        # evidence after the executor has released current_task.
        if getattr(self.task,'only',None) in ('MODULE_CRAFT','MODULE_RESUME'):
            self.hud.hide();self.task=self.control=None
        if getattr(active,'only',None) in ('MODULE_CRAFT','MODULE_RESUME'):
            self.hud.hide();self.task=self.control=None;return
        if active is not None and str(getattr(active,'only','')).startswith('ROGUELIKE'):
            self.hud.hide();self.task=self.control=None;return
        control=control_for(active) if active else None
        if control is not None:
            if self.control is not control:
                self.task=active;self.control=control;self.terminal_at=None;self.failed=False
                self.hud.state=TaskHudState(active)
        elif self.control is None:
            self.hud.hide();return
        elif self.control.state not in self.control.TERMINAL:
            self.hud.hide();self.task=self.control=None;return
        if self.control.state in self.control.TERMINAL:
            if self.terminal_at is None:self.terminal_at=self.clock()
            retention=5 if self.control.state=='SUCCESS' else 30
            if self.clock()-self.terminal_at>=retention:
                self.hud.hide();self.task=self.control=None;return
        if self.failed:self.hud.hide();return
        try:
            hwnd=og.device_manager.hwnd_window.hwnd
            if not hwnd:self.hud.hide();return
            self.hud.render()
            self.hud.show_for(hwnd)
        except Exception as error:
            self.failed=True;self.hud.hide()
            self.task.log_warning('进度提示未显示：'+str(error))
