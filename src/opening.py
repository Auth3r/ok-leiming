"""Opening-day retry loop. No account input, no daily-task continuation."""
from threading import Event
from math import ceil
from src.tasks import DailyTask
from src.run_plan import build_run_plan
from src.workflow import Workflow

# Installed by the GUI on its main thread. Headless tests use logs only.
notifications = None


class OpeningFocusMoved(RuntimeError):
    """Raised before any input; caller may safely re-observe and retry."""


class OpeningWorkflow(Workflow):
    def click(self,*args,**kwargs):
        for attempt in range(self.cfg['max_retries']+1):
            try:return super().click(*args,**kwargs)
            except OpeningFocusMoved:
                if attempt==self.cfg['max_retries']:raise
                self.checkpoint()
                self.delay(.1)
    def checkpoint(self):
        super().checkpoint()
        focus=getattr(self.task,'opening_focus',None)
        if focus:focus.poll()
        super().checkpoint()

    def monitor(self, interval=30, cycle_limit=0):
        attempts=0
        while True:
            focus=getattr(self.task,'opening_focus',None)
            if focus:focus.begin()
            self.task.info_set('下次尝试（秒）',0)
            self.wait(lambda:any(self.vision.page(p) for p in ('LOGIN','MAINTENANCE','MAIN_MENU')),
                      '等待登录页、维护提示或主界面')
            if self.vision.page('MAIN_MENU'):
                if focus:focus.ensure_game()
                self.task.info_set('抢开服状态','已成功进入游戏')
                self.emit('opening_success',attempts=attempts)
                self.snapshot('opening_success')
                self.task.log_info('已成功进入游戏！抢开服已停止。',notify=True)
                if notifications is not None:notifications.success.emit()
                return 'SUCCESS'
            if self.vision.page('LOGIN'):
                attempts+=1
                self.task.info_set('已尝试次数',attempts)
                self.task.info_set('抢开服状态','正在尝试进入游戏')
                self.click('LOGIN','login_enter',lambda:self.vision.page('MAINTENANCE') or self.vision.page('MAIN_MENU'),'点击进入游戏')
                if self.vision.page('MAIN_MENU'):continue
            self.click('MAINTENANCE','maintenance_confirm',lambda:self.vision.page('LOGIN') or self.vision.page('MAIN_MENU'),'确认服务器维护中')
            if self.vision.page('MAIN_MENU'):continue
            self.emit('opening_cycle_complete',attempts=attempts)
            if focus:focus.finish()
            if cycle_limit and attempts>=cycle_limit:return 'TEST_CYCLE_COMPLETE'
            deadline=self.clock()+interval
            self.task.info_set('抢开服状态','服务器维护中，等待下一轮')
            while self.clock()<deadline:
                self.task.info_set('下次尝试（秒）',ceil(deadline-self.clock()))
                self.delay(min(1,deadline-self.clock()))


class OpeningTask(DailyTask):
    task_label='抢开服：维护结束提醒'
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.description='每轮临时切到游戏，维护确认后恢复原窗口。等待期间可切换窗口。F7暂停/继续，F8停止。'
        self.default_config={'循环间隔（秒）':30}
        self.config_description={'循环间隔（秒）':'维护提示关闭后等待多久再尝试，默认30秒，范围5～3600秒'}
        self.test_cycle_limit=0
        self.test_background_only=False
        self.opening_focus=None

    def before_click(self):
        if self.opening_focus:
            self.opening_focus.ensure_game()
            self.click_pause_revision=self.opening_focus.paused_seconds

    def validate_click_context(self):
        if self.opening_focus:
            if self.click_pause_revision!=self.opening_focus.paused_seconds:
                raise OpeningFocusMoved('暂停后重新截图识别，拒绝使用旧点击位置')
            self.assert_foreground()

    def assert_foreground(self):
        if self.opening_focus:
            self.assert_target(self.execution_settings())
            if self.opening_focus.desktop.foreground()!=self.opening_focus.game_hwnd:
                self.opening_focus.poll()
                raise OpeningFocusMoved('点击前焦点变化，重新识别后再执行')
        else:super().assert_foreground()

    def assert_target(self,settings):
        super().assert_target(settings)
        if self.test_background_only:
            import win32gui
            if win32gui.GetForegroundWindow()==self.executor.device_manager.hwnd_window.hwnd:
                raise RuntimeError('后台验证中游戏获得了前台；本轮不能算作后台通过')

    def validate_config(self,key,value):
        if type(value) is not int or not 5<=value<=3600:return '请输入5～3600之间的整数秒数'

    def task_checkpoint(self):
        if self.task_control:self.task_control.checkpoint()
        if self.resume_pending:
            self.resume_pending=False
            self.assert_target(self.execution_settings())
            if self.opening_focus and self.opening_focus.in_cycle:self.opening_focus.ensure_game()

    roguelike_checkpoint=task_checkpoint

    def run(self):
        from pynput import keyboard
        from src.roguelike_control import RoguelikeControl
        from ok.core.events import communicate
        self.task_control=self.roguelike_control=control=RoguelikeControl()
        self._paused=False;self.resume_pending=False
        self.info.pop('停止原因',None)
        self.info_set('控制状态','运行中')
        communicate.task.emit(self)
        try:plan=build_run_plan(self, task_kind='OPENING')
        except Exception:
            control.finish('ERROR',source='opening_setup')
            raise
        self.run_plan=plan
        interval=plan['循环间隔（秒）']
        stop=control.cancel_event
        from src.opening_focus import OpeningFocus
        from src.interaction import GameForegroundInteraction
        import time
        try:
            if type(self.executor.device_manager.interaction) is GameForegroundInteraction:
                self.opening_focus=OpeningFocus(self,self.executor.device_manager.hwnd_window.hwnd)
            flow=OpeningWorkflow(self,plan,stop,clock=lambda:control.clock()-(self.opening_focus.paused_seconds if self.opening_focus else 0))
        except Exception:
            control.finish('ERROR',source='opening_setup')
            self.run_plan=None
            raise
        held=set()
        def pressed(key):
            if key in held:return
            held.add(key)
            if key==keyboard.Key.f7:self.toggle_pause()
            if key==keyboard.Key.f8:
                self.terminal_stop('CANCELLED',source='F8');self.executor.pause()
        listener=keyboard.Listener(on_press=pressed,on_release=lambda key:held.discard(key))
        def global_pause(paused):control.set_paused('executor',paused)
        communicate.executor_paused.connect(global_pause)
        try:
            listener.start()
            self.assert_target(plan)
            result=flow.monitor(interval,self.test_cycle_limit)
            self.info_set('结果',result)
            control.finish('SUCCESS')
            # Never continue into queued daily tasks after successful login.
            self.executor.pause()
        except Exception as error:
            source=control.stop_source
            control.finish('STOPPED' if source in ('F8','GUI 停止','stop_button_or_executor') else 'ERROR',source='opening_error')
            self.executor.pause()
            reason={'F8':'F8 已取消抢开服','GUI 停止':'点击停止按钮，已取消抢开服',
                    'stop_button_or_executor':'当前开服任务已停止'}.get(source,str(error) or '任务已停止')
            self.info_set('停止原因',reason)
            self.info_set('抢开服状态','已停止：'+reason)
            flow.snapshot('opening_FAILED');flow.emit('opening_stopped',reason=reason)
            self.log_error(reason,notify=True)
            raise
        finally:
            communicate.executor_paused.disconnect(global_pause)
            self._paused=False
            self.opening_focus=None
            self.run_plan=None
            listener.stop();listener.join(timeout=1)
            communicate.task.emit(self)
