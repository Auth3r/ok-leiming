from threading import Event
from ok import BaseTask
from src.settings import load_settings, validate
from src.config_ownership import current_version
from src import settings
from src.crafting_policy import PROFILES
from src.workflow import Workflow, SafetyStop
from src.run_plan import build_run_plan


def _control(task):
    return getattr(task,'task_control',None) or getattr(task,'roguelike_control',None)


def _input_privilege(task,action,x,y,requested):
    from src.diagnostics import require_input_privilege
    hwnd=task.executor.device_manager.hwnd_window.hwnd
    try:require_input_privilege(hwnd)
    except BaseException as error:
        from src.input_receipt import InputReceipt
        from src.interaction import _dispatch_context
        receipt=InputReceipt(action,hwnd,(x,y),requested)
        receipt.initial_context=_dispatch_context(hwnd,(x,y))
        receipt.target_pid=receipt.initial_context.get('target_pid')
        error.input_receipt=receipt.finish('not_sent','input_privilege_guard')
        raise


class DailyTask(BaseTask):
    task_label='完整日常路线'
    only=None

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.name=self.task_label
        self.description='每个模块临时切前台，完成后归还窗口。F7暂停/继续，F8停止。可关闭临时接管。'
        self.daily_focus=None
        self.task_control=None
        self.resume_pending=False
        # Legacy instances retain their original schema for stage-0 characterization.
        # Startup migration stops conflicts before OK can instantiate these tasks.
        if current_version(settings.ROOT):
            self.default_config={}
        else:
            self.default_config=load_settings()
            self.default_config.pop('window_title')
            self.default_config.pop('game_exe')
        self.config_description={
            'resolution_width':'运行分辨率：0自动识别、1280或3840；在截图方式页选择',
            'resolution_height':'对应高度：0、720或2160；必须与实际客户区一致',
            'match_threshold':'模板最低置信度，默认 0.90',
            'click_min_wait':'点击后最小等待秒数；随后循环截图识别',
            'recognition_timeout':'每轮识别的最大秒数',
            'max_retries':'额外识别重试次数，不重复点击消耗按钮',
            'dark_tide_sweep_count':'04-3 扫荡次数，每次消耗 60 体力',
            'debug_mode':'保存状态、匹配框、置信度及每次点击前截图',
            'step_mode':'每次点击前暂停；在 GUI 继续，再切回游戏',
            'cooperative_mode':'临时接管：每个模块完成后恢复原窗口',
            'cooperative_interval':'模块之间及任务开始前留给用户的秒数，默认5秒',
        }
        self.config_description.update({key:'是否执行 '+key.removeprefix('execute_') for key in self.default_config if key.startswith('execute_')})
        self.support_schedule_task=False

    def before_module(self,flow):
        if self.daily_focus and not self.daily_focus.in_cycle:
            flow.delay(flow.cfg['cooperative_interval'])
            self.daily_focus.begin()

    def after_module(self):
        if self.daily_focus:self.daily_focus.finish()

    def before_click(self):
        if self.daily_focus:
            self.daily_focus.ensure_game()
            self.click_pause_revision=self.daily_focus.paused_seconds

    def validate_click_context(self):
        if self.daily_focus:
            from src.cooperative import FocusChanged
            if self.click_pause_revision!=self.daily_focus.paused_seconds:
                raise FocusChanged('恢复后重新识别，放弃旧点击位置')
            self.assert_foreground()

    def after_input(self):
        if self.daily_focus:self.daily_focus.after_input()

    def validate_config(self,key,value):
        try:
            values=load_settings()
            values.update(self.default_config)
            values[key]=value
            validate(values)
        except (ValueError,KeyError,TypeError) as error:
            return str(error)

    def assert_foreground(self):
        from src.interaction import GameForegroundInteraction, GameBackgroundInteraction
        interaction=self.executor.device_manager.interaction
        if type(interaction) not in (GameForegroundInteraction,GameBackgroundInteraction):
            raise RuntimeError('当前输入方式不符合配置，请重启助手')
        self.assert_target(DailyTask.execution_settings(self))
        if type(interaction) is GameForegroundInteraction:
            import win32gui
            if win32gui.GetForegroundWindow()!=self.executor.device_manager.hwnd_window.hwnd:
                if self.daily_focus:
                    from src.cooperative import FocusChanged
                    raise FocusChanged('点击前切换了窗口，重新接管并识别')
                raise RuntimeError('游戏失去前台，已停止点击；请勿在任务执行时切换窗口')

    def prepare_input(self):
        from src.interaction import GameForegroundInteraction, ForegroundUnavailable
        self.assert_target(DailyTask.execution_settings(self))
        if type(self.executor.device_manager.interaction) is GameForegroundInteraction:
            try:
                self.executor.device_manager.interaction.activate_for_task()
            except ForegroundUnavailable as error:
                self.log_warning(str(error))
                self.log_info('自动切换前台受限：请在20秒内手动切到游戏（Alt+Tab）；F8可取消。',notify=True)
                self.info_set('当前状态','等待手动切到游戏（20秒）')
                import time
                import win32gui
                deadline=time.monotonic()+20
                while win32gui.GetForegroundWindow()!=self.executor.device_manager.hwnd_window.hwnd:
                    self.assert_target(DailyTask.execution_settings(self))
                    if time.monotonic()>=deadline:
                        raise ForegroundUnavailable('等待手动切到游戏超时，未执行点击。请切到游戏；若仍失败，请以管理员身份重启助手。') from error
                    self.sleep(.1)
            self.sleep(0.5)
            self.assert_foreground()

    def assert_target(self, settings):
        from src.diagnostics import window_identity, matches_target
        identity=window_identity(self.executor.device_manager.hwnd_window.hwnd)
        if not matches_target(identity, settings):
            raise SafetyStop(f'窗口不属于目标游戏，停止操作：{identity}')
        if identity.get('resolution'):
            from src.resolution import Resolution, selected_size
            try: Resolution(tuple(identity['resolution']), selected_size(settings))
            except ValueError as error: raise SafetyStop(str(error)) from error
        if identity.get('minimized'):
            raise RuntimeError('游戏已最小化，请还原窗口；可以被其他窗口遮挡，无需切到前台')
        return identity

    def execution_settings(self):
        # Direct safety checks outside a run still use the current persisted settings.
        return getattr(self, 'run_plan', None) or load_settings()

    def run(self):
        # Roguelike keeps its existing runner/event ownership, but uses these
        # same pause/stop methods and the same input guard as every daily task.
        if getattr(self,'_roguelike_run_owner',False):
            return self._run_daily()
        from src.roguelike_control import RoguelikeControl
        from ok.core.events import communicate
        from ok.task.exceptions import TaskDisabledException
        self.task_control=control=RoguelikeControl()
        self.roguelike_control=control  # Existing shared runners use this name.
        self._paused=False
        self.resume_pending=False
        getattr(self,'info',{}).pop('停止原因',None)
        for key in ('完整路线结果','路线汇总','失败与未完成','模块结果','当前模块','路线进度','下一模块'):
            getattr(self,'info',{}).pop(key,None)
        self.daily_route_status='SUCCESS'
        self.info_set('控制状态','运行中')
        communicate.task.emit(self)
        def global_pause(paused):control.set_paused('executor',paused)
        communicate.executor_paused.connect(global_pause)
        try:
            self._run_daily()
            control.finish(getattr(self,'daily_route_status','SUCCESS'))
        except Exception as error:
            self.terminal_stop('STOPPED' if isinstance(error,SafetyStop) else 'ERROR')
            if control.stop_source in ('F8','GUI 停止','stop_button_or_executor'):
                raise TaskDisabledException() from error
            self.executor.pause()  # Unknown/unsafe failure must freeze the queue.
            raise
        finally:
            communicate.executor_paused.disconnect(global_pause)
            self._paused=False
            communicate.task.emit(self)

    def _run_daily(self):
        from pynput import keyboard
        control=_control(self)
        event=control.cancel_event if control else Event()
        values=build_run_plan(self)
        self.run_plan=values
        try:
            from src.interaction import GameForegroundInteraction
            if values['cooperative_mode'] and type(self.executor.device_manager.interaction) is GameForegroundInteraction:
                import time
                from src.cooperative import DailyFocus,CooperativeWorkflow
                self.daily_focus=DailyFocus(self,self.executor.device_manager.hwnd_window.hwnd)
                workflow=CooperativeWorkflow(self,values,event,clock=lambda:time.monotonic()-self.daily_focus.paused_seconds)
            else:workflow=Workflow(self,values,event)
            if control:workflow.clock=control.clock
        except Exception:
            if control:self.terminal_stop('ERROR')
            else:self.executor.pause()
            self.run_plan=None
            raise
        held=set()
        def pressed(key):
            if control:
                if key in held:return
                held.add(key)
            if control and key==keyboard.Key.f7:
                self.toggle_pause()
            if key==keyboard.Key.f8:
                if control:
                    self.terminal_stop('CANCELLED',source='F8')
                    self.executor.pause()  # Emergency stop also freezes queued input.
                    self.log_warning('F8 已停止当前任务；模组或肉鸽请使用独立续跑入口接管',notify=True)
                    return
                event.set()
                # Disable unblocks upstream sleep even during GUI single-step pause.
                self.disable()
                self.executor.pause()
                self.log_warning('F8 已请求取消；不会再发出点击',notify=True)
        listener=keyboard.Listener(on_press=pressed,**({'on_release':lambda key:held.discard(key)} if control else {}))
        timing=None
        manual_input=None
        try:
            listener.start()
            if self.only in ('MODULE_CRAFT','MODULE_RESUME'):
                from src.crafting_manual_input import CraftManualInputMonitor
                manual_input=CraftManualInputMonitor(self,workflow.emit).start()
            # Activate the already-open game once; never launch or log in.
            self.log_info('正在切到游戏前台并检查当前模组页面。F8 可取消。' if self.only in ('MODULE_PREPARE','MODULE_CRAFT','MODULE_RESUME')
                          else '正在切到游戏前台并自动检查主界面。F8 可取消。',notify=True)
            def prepare():
                if self.daily_focus:
                    self.log_info('临时接管即将开始，请切到你平时使用的窗口。',notify=True)
                    workflow.delay(values['cooperative_interval'])
                    self.assert_target(values)
                    self.daily_focus.begin()
                else:self.prepare_input()
            if self.only in ('MODULE_CRAFT','MODULE_RESUME'):
                from src.crafting_timing import CraftTiming
                timing=CraftTiming(workflow)
                timing.install_flow()
            workflow.run(self.only,prepare=prepare)
            self.daily_route_status=getattr(workflow,'route_status','SUCCESS')
            if self.daily_focus:self.daily_focus.finish()
        finally:
            self.daily_focus=None
            self.run_plan=None
            listener.stop()
            listener.join(timeout=1)
            if manual_input:manual_input.close()
            if timing:
                path=timing.finish()
                if path:self.info_set('性能报告',str(path))
                elif timing.save_error:self.log_warning('制作性能报告保存失败：'+timing.save_error)

    def toggle_pause(self):
        control=_control(self)
        if not control:
            if self.enabled:self.unpause() if self.paused else self.pause('user')
            return
        if control.state in control.TERMINAL:return
        if control.state=='PAUSED':
            self.unpause()
        else:self.pause('user')

    def task_checkpoint(self):
        control=_control(self)
        if control:control.checkpoint()
        if self.resume_pending:
            self.resume_pending=False
            # Run on the task worker, after waking and before a new screenshot.
            # GUI resume may itself have changed the foreground window.
            self.assert_target(DailyTask.execution_settings(self))
            if self.daily_focus:self.daily_focus.ensure_game()
            else:self.prepare_input()

    roguelike_checkpoint=task_checkpoint

    def pause(self,source='step'):
        control=_control(self)
        if control:
            if control.state in control.TERMINAL:return
            control.set_paused(source,True)
            self.info_set('控制状态',{'step':'单步调试暂停','user':'用户暂停','safety':'安全暂停'}.get(source,source))
        super().pause()

    def unpause(self):
        control=_control(self)
        if control:
            if control.state in control.TERMINAL or not self.enabled:
                self._paused=False
                return
            if control.state!='PAUSED':return
            # Reset capture before waking the worker; its input revision was
            # invalidated when pausing, so old points cannot be dispatched.
            self.executor.reset_scene(check_enabled=False)
            self.resume_pending=True
            control.resume()
            self.info_set('控制状态','运行中：重新检查窗口及画面')
        super().unpause()

    def disable(self):
        control=_control(self)
        if control:control.finish('STOPPED',source='stop_button_or_executor')
        self._paused=False
        self.resume_pending=False
        super().disable()

    def enable(self):
        if not self.enabled:
            # A new queue entry starts a new lifecycle; a continue action must
            # never turn an old terminal control back into a running control.
            self.task_control=None
            self.roguelike_control=None
            self.resume_pending=False
            self._paused=False
        super().enable()

    def terminal_stop(self,status='STOPPED',source='task_error'):
        control=_control(self)
        if control:control.finish(status,source=source)
        self.info_set('控制状态',{'STOPPED':'已停止','CANCELLED':'已停止','ERROR':'错误停止'}.get(status,status))
        self.disable()

    def click(self,x,y,after_sleep=0,down_time=.02,**kwargs):
        # Shared coordinate adapter holds the input lock. Avoid upstream
        # reset_scene() waiting for global pause while holding that lock.
        _input_privilege(self,'click',x,y,2)
        result=self.executor.interaction.click(x,y,down_time=down_time,**kwargs)
        try:
            self.executor.reset_scene(check_enabled=False)
            if after_sleep:self.sleep(after_sleep)
        except BaseException as error:
            from src.input_receipt import InputReceipt
            if isinstance(result,InputReceipt):error.input_receipt=result
            raise
        return result

    def move(self,x,y):
        from src.diagnostics import require_input_privilege
        require_input_privilege(self.executor.device_manager.hwnd_window.hwnd)
        result=self.executor.interaction.move(x,y)
        self.executor.reset_scene(check_enabled=False)
        return result

    def scroll(self,x,y,count):
        _input_privilege(self,'scroll',x,y,abs(count))
        result=self.executor.interaction.scroll(x,y,count)
        try:self.executor.reset_scene(check_enabled=False)
        except BaseException as error:
            from src.input_receipt import InputReceipt
            if isinstance(result,InputReceipt):error.input_receipt=result
            raise
        return result

    def swipe(self,x1,y1,x2,y2,duration=300,**kwargs):
        from src.diagnostics import require_input_privilege
        require_input_privilege(self.executor.device_manager.hwnd_window.hwnd)
        result=self.executor.interaction.swipe(x1,y1,x2,y2,duration=duration,**kwargs)
        self.executor.reset_scene(check_enabled=False)
        return result


class InspectTask(DailyTask):
    task_label='诊断：仅检查主界面（可选，不执行日常）'
    only='INSPECT'


class RoguelikeTask(DailyTask):
    task_label='刷肉鸽'
    only='ROGUELIKE'

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        from qfluentwidgets import FluentIcon
        from queue import SimpleQueue
        self.roguelike_events=SimpleQueue()
        self.group_name='肉鸽'
        self.group_icon=FluentIcon.SYNC
        self.default_config={}
        self.description='按独立 Roguelike 设置中的肉鸽循环次数执行（1至100）。每轮最终结算确认后计数；未知页面停止，F8取消。'

    def run(self):
        from src.roguelike_control import RoguelikeControl
        from ok.core.events import communicate
        from queue import Empty
        while True:
            try:self.roguelike_events.get_nowait()
            except Empty:break
        self.roguelike_control=control=RoguelikeControl()
        self.task_control=control
        self._roguelike_run_owner=True
        getattr(self,'info',{}).pop('停止原因',None)
        self.info_set('控制状态','运行中')
        communicate.task.emit(self)
        self.log_info('roguelike_control_started state=RUNNING fresh_control=true')
        self.log_info('F7 暂停/继续 · F8 停止')
        def global_pause(paused):control.set_paused('executor',paused)
        communicate.executor_paused.connect(global_pause)
        try:
            super().run()
            control.finish('SUCCESS')
        except Exception as error:
            self.terminal_stop('STOPPED' if isinstance(error,SafetyStop) else 'ERROR')
            # Use the executor's terminal branch: it clears current_task without
            # trying to capture a frame through a globally paused executor.
            from ok.task.exceptions import TaskDisabledException
            self.info_set('肉鸽控制',control.state+'；请启动“继续肉鸽”重新接管')
            raise TaskDisabledException() from error
        finally:
            communicate.executor_paused.disconnect(global_pause)
            self._roguelike_run_owner=False
            self._paused=False

    def roguelike_event(self,event,data):
        if event.startswith('roguelike_'):
            self.roguelike_events.put((event,dict(data)))

    def roguelike_click(self,x,y,after_sleep=0,down_time=.05):
        # Called under the control's input lock. BaseTask.click's final
        # reset_scene() can block on a concurrent global pause while holding
        # that lock; invalidate its capture cache without another pause wait.
        from src.diagnostics import require_input_privilege
        require_input_privilege(self.executor.device_manager.hwnd_window.hwnd)
        self.executor.interaction.click(x,y,move_back=False,name=None,move=True,
                                        down_time=down_time,key='left')
        self.executor.reset_scene(check_enabled=False)


class RoguelikeResumeTask(RoguelikeTask):
    task_label='继续肉鸽'
    only='ROGUELIKE_RESUME'

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.description='从当前已识别的肉鸽页面直接接管；不恢复历史选择，按当前画面继续执行。暂停可继续；停止/F8后请启动本任务。'


class BlackShieldTask(DailyTask):
    task_label='单项：黑盾领取'
    only='BLACK_SHIELD'


class FundTask(DailyTask):
    task_label='单项：理财基金'
    only='FUND'


class BlackDispatchTask(DailyTask):
    task_label='单项：黑盾派遣（含额外奖励）'
    only='BLACK_DISPATCH'


class DarkTideTask(DailyTask):
    task_label='单项：暗涌 04-3'
    only='DARK_TIDE'


class GanhuoTask(DailyTask):
    task_label='单项：干火 ST-7'
    only='GANHUO'


class HuntGoldTask(DailyTask):
    task_label='单项：猎金 ER-2'
    only='HUNT_GOLD'


class DailyRewardTask(DailyTask):
    task_label='单项：每日奖励'
    only='DAILY_REWARD'


class ModulePrepareTask(DailyTask):
    task_label='准备模组'
    only='MODULE_PREPARE'

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        from qfluentwidgets import FluentIcon
        self.group_name='模组制作'
        self.group_icon=FluentIcon.EDIT
        self.description='按制作配置选择胚子及蓝图；任意队员槽有加号就停止。准备完成后停止，不启动实验。'
        self.default_config.update({'第一优先策略':'火力/狙击/隐刃优先','第二优先策略':'效能优先',
                                    '第三优先策略':'条件属性优先','优先加成':'攻击加成'})


class ModuleCraftTask(ModulePrepareTask):
    task_label='制作模组（循环）'
    only='MODULE_CRAFT'

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.description='按所选模组策略循环制作；记录技能与激增次数，分别统计正常完成和快速结束。F8取消。'
        for key in ('第一优先策略','第二优先策略','第三优先策略','优先加成'):
            self.default_config.pop(key,None)
        if not current_version(settings.ROOT):
            self.default_config.update({'制作配置':'旧地联合','制作数量':1,'属性刷新方式':'自动',
                                        '属性刷新人数门槛':6,'优先火力攻击奖励任务':True})
        self.config_description.update({'制作数量':'本轮总数量，含正常完成和快速结束（1至100）',
            '属性刷新方式':'自动受人数门槛及任务例外约束；不刷新关闭属性刷新。旧优先刷新等同自动',
            '属性刷新人数门槛':'实际剩余可选人数达到门槛才允许属性刷新（4至9，默认6）',
            '优先火力攻击奖励任务':'只提升未完成火攻奖励任务对应的三类限次激增并阻止刷新；不改变次数上限、技能或任务选取',
            '制作配置':'旧地联合、军工、一致、先攻；切换影响技能策略与模组名称核对'})

    def validate_config(self,key,value):
        if key=='制作数量' and (type(value) is not int or not 1<=value<=100):return '制作数量应为1至100的整数'
        if key=='属性刷新人数门槛' and (type(value) is not int or not 4<=value<=9):return '属性刷新人数门槛应为4至9的整数'
        if key=='优先火力攻击奖励任务' and type(value) is not bool:return '任务优先开关必须为布尔值'
        if key=='制作配置' and value not in PROFILES:return '请选择旧地联合、军工、一致或先攻'
        if key=='属性刷新方式' and value not in ('自动','优先刷新','不刷新'):return '请选择自动、优先刷新或不刷新'
        return super().validate_config(key,value)


class ModuleResumeTask(ModuleCraftTask):
    task_label='继续当前模组'
    only='MODULE_RESUME'

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.description='从当前已识别的制作页面直接接管；无法证明的历史字段保持未知。只有当前动作确实依赖缺失证据时停止；历史完成数未知时仅继续当前一件，不确定的不可逆输入不会自动重放。'
