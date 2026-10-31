"""GUI-started native runtime. Reuse RogueRunner decisions, WGC and input bounds.

This profile never enables legacy tasks or changes their 4K guard. Only native
pages with proven assets expose actions; unsupported pages suspend the run.
"""
import json
import copy
import hashlib
from collections import OrderedDict
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import cv2

from src.roguelike_control import Reobserve, RoguelikeControl
from src.roguelike_policy import Observation, RogueAction, RogueState as S, ShopVisit
from src.roguelike_preparation_720p import RoguelikePreparation720p
from src.roguelike_runner import RoguelikeRunner,BATTLE_EXIT_STATES
from src.workflow import SafetyStop,RecognitionTimeout
from src.roguelike_stage_720p import NativeStageAssets,NativeStageScreen
from src.roguelike_timing import TIMING
from src.roguelike_status import replace_status

MODE='roguelike_720p'
SUPPORTED=frozenset((S.MAIN_MENU,S.NAV,S.ENTRY,S.START))
UNKNOWN_DIAGNOSTIC_INTERVAL=3.0
UNKNOWN_RECOVERY_ATTEMPTS=2
UNKNOWN_TIMEOUT=UNKNOWN_DIAGNOSTIC_INTERVAL*(UNKNOWN_RECOVERY_ATTEMPTS+1)

class UnsupportedNativePage(SafetyStop):pass


class NativeOCRExecutor:
    """Read-only OCR services; legacy task pause/capture never owns this run."""
    paused=False
    def __init__(self,executor):self.original=executor
    def __getattr__(self,name):
        if name in ('config','ocr_lib','ocr_po_translation','text_fix','debug'):
            return getattr(self.original,name)
        raise SafetyStop('Native OCR cannot access legacy executor: '+name)


def discover_target():
    import psutil,win32gui,win32process
    from src.client_geometry import validate_client_geometry
    pids={p.info['pid'] for p in psutil.process_iter(['pid','name'])
          if (p.info['name'] or '').casefold()=='warp knights.exe'}
    windows=[]
    win32gui.EnumWindows(lambda h,_:windows.append(h) if win32gui.IsWindowVisible(h)
        and win32process.GetWindowThreadProcessId(h)[1] in pids else None,None)
    windows=[h for h in windows if win32gui.GetClientRect(h)==(0,0,1280,720)]
    if len(windows)!=1:raise SafetyStop('720P 模式需要唯一的 1280×720 游戏客户区；请还原游戏窗口')
    hwnd=windows[0]
    validate_client_geometry(hwnd,expected_size=(1280,720))
    return hwnd,win32process.GetWindowThreadProcessId(hwnd)[1]


class NativeVision:
    def __init__(self):self.results={}
    def page(self,name):return False  # No legacy page inference on native pixels.


class NativeScreen:
    """Expose only currently proven actions to the existing business runner."""
    def __init__(self,flow):
        from src.roguelike_preparation_720p import PreparationScreen
        self.frame=flow.last_frame
        self.device_exhausted=getattr(flow.runner,'native_device_exhausted',False)
        self.native=PreparationScreen(None,self.frame,flow.recognizer.templates)
        self.matches=self.native.matches
        self.result=flow.recognizer.observe(flow.packet,hwnd=flow.hwnd,pid=flow.pid)
        state=self.result['state']
        self.observation=Observation()
        if state=='ROGUE_PREPARATION_CONFIRMED' and self.result['preparation']['phase']=='idle':
            matches=self.result['preparation_matches']
            if all(matches[name]['matched'] for name in ('prep_mode','prep_change_difficulty','prep_start_label')):
                self.observation=self.native.observation
            else:
                # Page identity and mode authorization are separate. Never
                # disguise a known START page as an unknown loading frame.
                normal=matches.get('prep_normal_mode',{}).get('matched')
                change=matches['prep_change_difficulty']
                action=(RogueAction('change_difficulty',tuple(change['bbox']),'切换难度',irreversible=False),) if normal and change['matched'] else ()
                self.observation=Observation(S.START,event='mode_required' if normal else 'mode_unverified',
                    actions=action,detail='当前为普通模式，需要选择已验证的特种模式' if normal else '模式未确认；禁止开始挑战')
        elif state=='HOME_CONFIRMED':self.observation=Observation(S.MAIN_MENU)
        elif state=='NAV_CONFIRMED':self.observation=Observation(S.NAV)
        elif state=='ROGUE_ENTRY_VISIBLE':
            target=self.result['targets']['entry_button']
            if target.get('label')=='进入副本':
                self.observation=Observation(S.ENTRY,actions=(RogueAction('enter',tuple(target['bbox']),'进入副本',irreversible=False),))
        if state=='BLOCKED_BY_OVERLAY':
            self.observation=Observation(detail='登录或已知弹层遮挡；请人工处理')
        elif self.observation.state==S.UNKNOWN:
            stage=NativeStageScreen(flow.task,self.frame,flow.stage_assets,
                                   special_shop=flow.runner.shop_kind=='SPECIAL_OPERATION_SHOP')
            self.matches.update(stage.matches)
            if stage.observation.state!=S.UNKNOWN:
                self.native=stage;self.observation=stage.observation
                self.result['state']=str(self.observation.state)
                self.result['stage_matches']=stage.matches
                self.result['stage_identity']=stage.overlay_identity()
    def stable_action_visual(self):return self.native.stable_action_visual()
    def overlay_identity(self):return getattr(self.native,'overlay_identity',lambda:'')()
    def find_special_deploy_card_fast(self):
        return self.native.find_special_deploy_card_fast() if self.observation.state==S.BATTLE and not self.device_exhausted else None
    def choose_empty_ground(self,exclude=()):
        return self.native.choose_empty_ground(exclude=exclude) if self.observation.state==S.BATTLE else None


class NativeRunner(RoguelikeRunner):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.initial_page_checked=False
        self.count_current_settlement=True
        self.pending_review=False
    def battle_step(self,o):
        # A device observed before selection cannot silently become "no device"
        # after re-observation. Diagnose current frames for three seconds, then
        # preserve progress instead of waiting through the battle without it.
        if (o.state==S.BATTLE and getattr(self,'native_deployment_before',None)
                and not getattr(self,'native_device_exhausted',False)):
            started=self.f.clock()
            while self.screen().native.find_special_deploy_card_fast() is None:
                if self.f.clock()-started>=3:
                    self.f.snapshot('known_device_missing')
                    raise SafetyStop('已确认的装置连续3秒无法重新定位；停止输入并保存进度')
                self.f.delay(TIMING.first_check);self.refresh();o=self.observe()
                if o.state in BATTLE_EXIT_STATES:
                    self.finish_battle(o);return
                if o.state!=S.BATTLE:raise Reobserve()
        return super().battle_step(o)
    def _step(self,o):
        if o.state==S.START and o.event=='mode_required':
            if self.active:raise SafetyStop('已有挑战时禁止切换难度')
            self.send_retryable_stable(S.START,lambda s:s.action('change_difficulty'),
                lambda s:s.state==S.START and s.event in ('idle','mode_select','mode_selected'),
                '选择肉鸽特种模式')
            return
        if o.state==S.START and o.event in ('mode_select','mode_normal_pending'):
            if self.active:raise SafetyStop('已有挑战时禁止切换难度')
            self.send_retryable_stable(S.START,lambda s:s.action('select_special'),
                lambda s:s.state==S.START and s.event in ('mode_selected','mode_confirm'),
                '选择特种模式并验证生效标记')
            return
        if o.state==S.START and o.event=='mode_confirm':
            if self.active:raise SafetyStop('已有挑战时禁止切换难度')
            self.send_retryable_stable(S.START,lambda s:s.action('confirm_mode'),
                lambda s:s.state==S.START and (s.event=='mode_selected' or
                    (s.event=='idle' and s.action('start') is not None)),
                '确认特种模式生效')
            return
        if o.state==S.START and o.event=='mode_selected':
            if self.active:raise SafetyStop('已有挑战时禁止切换难度')
            self.send_retryable_stable(S.START,lambda s:s.action('back'),
                lambda s:s.state==S.START and s.event=='idle' and s.action('start') is not None,
                '确认特种模式并返回挑战准备页')
            return
        if o.state==S.START and o.event=='mode_unverified':
            raise UnsupportedNativePage(o.detail)
        return super()._step(o)
    def deployment_click(self,point,key,label,on_sent=None):
        if key=='special_card':
            self.refresh();self.observe()
            if self.current.state!=S.BATTLE:raise Reobserve()
            screen=self.screen().native
            remaining=screen.special_device_remaining()
            started=self.f.clock()
            deadline=started+30.
            battle_deadline=getattr(self,'native_battle_deadline',None)
            if battle_deadline is not None:deadline=min(deadline,battle_deadline)
            while remaining is None and self.f.clock()<deadline:
                # Unreadable device digits cannot authorize a click, but a
                # later confirmed victory must not remain blocked by them.
                # Recheck fresh frames only, with one bounded recovery window.
                self.f.emit('roguelike_wait',reason='装置数字未确认；只读复查或等待已确认战斗结果',
                            elapsed=round(self.f.clock()-started,1),timeout=deadline-started)
                self.f.delay(min(TIMING.first_check,deadline-self.f.clock()))
                self.refresh();self.observe()
                if self.current.state in BATTLE_EXIT_STATES:
                    self.finish_battle(self.current)
                    raise Reobserve()
                if self.current.state in (S.UNKNOWN,S.TRANSIENT):continue
                if self.current.state!=S.BATTLE:
                    raise SafetyStop('装置复查时页面异常；未发送选卡输入')
                screen=self.screen().native
                remaining=screen.special_device_remaining()
            if remaining is None or remaining<=0:
                raise SafetyStop('特殊装置数量未确认；未发送选卡输入')
            self.native_deployment_before=remaining
            card=screen.find_special_deploy_card_fast()
            if card is None:raise SafetyStop('特殊装置位置未确认；未发送选卡输入')
            self.native_deployment_area=card.area
            if screen.special_device_selected():
                self.f.emit('roguelike_deployment',message='verified device already selected; no repeat click',remaining=remaining)
                return
            point=(round((card.area[0]+card.area[2])/2),round((card.area[1]+card.area[3])/2))
        if key!='special_ground' or on_sent is None:
            return super().deployment_click(point,key,label,on_sent)
        before=getattr(self,'native_deployment_before',None)
        if before is None:raise SafetyStop('部署前数量缺失；未发送地面确认')
        super().deployment_click(point,key,label,None)
        # Preserve the existing card/double-click/5-second policy. Dispatch
        # alone cannot count a placement or authorize the next device.
        deadline=self.f.clock()+3.0
        removed_sequence=None
        while self.f.clock()<deadline:
            self.f.delay(TIMING.first_check);self.refresh();self.observe()
            if self.current.state in (S.UNKNOWN,S.TRANSIENT):
                continue  # Bounded camera settling, with no further input.
            if self.current.state!=S.BATTLE:
                raise SafetyStop('部署后页面变化；未验证成功，未增加部署计数')
            screen=self.screen().native
            after=screen.special_device_remaining()
            if (before==1 and after is None and getattr(screen,'special_device_removed',lambda area:False)
                    (getattr(self,'native_deployment_area',()))):
                sequence=self.f.packet.metadata['sequence']
                if removed_sequence is None:removed_sequence=sequence;continue
                if sequence>removed_sequence:after=0
            else:removed_sequence=None
            if after==before-1:
                on_sent()
                if after==0:self.native_device_exhausted=True
                self.f.emit('native_deployment_verified',before=before,after=after,point=point,
                            placement=self.battle_deploy_current,source=self.f.packet.metadata,
                            confirmation='last_card_removed_two_fresh_frames' if after==0 and removed_sequence is not None else 'digit_decreased')
                return
            if after is not None and after!=before:
                raise SafetyStop('部署数量变化异常；停止后续部署')
        raise SafetyStop('部署未确认：装置数量未减少；停止后续部署，不重复盲点')
    def wait_battle_context(self,reason='战斗镜头/动画变化',timeout=None):
        # Actual native victory cinematics exceed the legacy 10-second camera
        # window. Wait read-only for up to30s, capped by this battle's deadline;
        # the animation stays UNKNOWN and cannot authorize any click.
        if self.battle_active and timeout in (None,TIMING.post_battle_timeout):
            timeout=30.
            deadline=getattr(self,'native_battle_deadline',None)
            if deadline is not None:timeout=min(timeout,max(0,deadline-self.f.clock()))
        try:
            return super().wait_battle_context(reason,timeout)
        except SafetyStop as error:
            # Only this perception timeout can use validated native-asset
            # recovery. Input, capture, resource and unexpected-state failures
            # retain their explicit safety pause; the 4K runner is unchanged.
            if (str(error)=='Battle: camera/animation recognition timeout'
                    and self.battle_active and self.current.state in (S.UNKNOWN,S.TRANSIENT)):
                raise UnsupportedNativePage(str(error)) from error
            raise
    def wait_battle(self,check_card=True):
        self.native_battle_deadline=self.f.clock()+TIMING.battle_timeout
        try:return super().wait_battle(check_card)
        finally:self.native_battle_deadline=None
    def observe(self):
        result=super().observe()
        if result.state==S.BATTLE_PREP:
            self.native_device_exhausted=False
            self.native_deployment_before=None
        if result.state==S.BATTLE_RESULT:
            kind=(getattr(self.screen().native,'page',None) or {}).get('battle_kind')
            if kind in ('special','encounter'):
                if self.battle_kind is not None and self.battle_kind!=kind:
                    raise SafetyStop('战斗结果标识与已知战斗类型冲突；未确认结果')
                self.battle_kind=kind
                self.shop_kind='SPECIAL_OPERATION_SHOP' if kind=='special' else 'NORMAL_BATTLE_SHOP'
        if not self.initial_page_checked and result.state not in (S.UNKNOWN,S.TRANSIENT):
            if result.state==S.CHOICE:self.f.restore_choice_context()
            if result.state==S.SHOP:self.f.restore_shop_visit()
            if result.state==S.OUTPOST:self.f.restore_free_member_batch(result)
            if result.state==S.BATTLE_RESULT:self.f.restore_battle_result_batch()
            self.initial_page_checked=True
            if result.state in (S.SETTLEMENT_CONFIRM,S.RESULT_STATS,S.RESULT_TEAM,S.RESULT):
                self.count_current_settlement=False
                self.f.emit('native_prior_result',state=str(result.state),
                            reason='启动时已在旧结算页；先关闭，不计入本次运行，再开始挑战')
        if result.state==S.SHOP:
            native=self.screen().native
            kind=(getattr(native,'page',None) or {}).get('shop_kind')
            if kind=='SPECIAL_OPERATION_SHOP':
                if self.shop is not None and self.shop.kind!=kind:
                    raise SafetyStop('商店类型与已执行策略冲突；停止购买/刷新')
                self.shop_kind=kind
                if not self.active:
                    self.active=True;self.f.challenge_start_attempted=True
                    self.f.emit('native_existing_challenge',state=str(result.state),
                                reason='从已验证特级行动商店继续当前挑战；不重新开始')
        self.f.record_observation(result)
        if result.state==S.UNKNOWN and not self.f.unknown_saved:
            self.f.unknown_saved=True
            self.f.snapshot('first_unknown')
        return result
    def screen(self):
        if self.cached_frame is not self.f.last_frame:
            self.cached_frame=self.f.last_frame
            self.cached_screen=NativeScreen(self.f)
        return self.cached_screen
    def parent_action(self,o,key):
        target=self.screen().result.get('targets',{}).get(key)
        if target and target['matched']:
            return RogueAction(key,tuple(target['bbox']),target.get('label') or key,irreversible=False)
    def wait_native_page(self):
        # The frame between a confirmed action and the next foreground can be
        # UNKNOWN. Wait without input; never turn one loading frame into a pause.
        started=self.f.clock()
        timeout=min(TIMING.load_timeout,UNKNOWN_TIMEOUT)
        while self.current.state in (S.UNKNOWN,S.TRANSIENT):
            if self.screen().result.get('state')=='BLOCKED_BY_OVERLAY':
                raise UnsupportedNativePage(self.current.detail or '登录或安全弹层遮挡')
            elapsed=self.f.clock()-started
            if elapsed>=timeout:
                raise UnsupportedNativePage('页面切换超过 '+str(timeout)+' 秒仍未识别；未发送输入')
            self.f.emit('roguelike_wait',reason='等待页面切换；每三秒重新诊断，未识别时不发送输入',
                        elapsed=round(elapsed,1),timeout=timeout,state=str(self.current.state))
            self.f.delay(min(TIMING.poll,timeout-elapsed))
            self.refresh();self.observe()
        return self.current
    def run_native(self):
        # The original _step and action helper own normal decisions/transitions.
        # Only profile scheduling and suspension are specialized here.
        attempts=0
        deadline=self.f.clock()+1200
        while self.ledger.completed_runs<self.target:
            self.f.checkpoint()
            if self.ledger.progress==5:
                # Only the verified final settlement/idle return opens a new
                # round. Reset the same per-round fields as the original runner.
                self.ledger.begin_next_run()
                self.run_index=self.ledger.completed_runs+1
                self.floor=None;self.bounty_done=False;self.battle_kind=None
                self.battle_deploy_count=0;self.selected_candidate=None
                self.battle_deploy_current=0;self.deployment_next_at=None
                self.battle_active=False;self.shop_kind=None;self.shop=None;self.shop_steps=0
                self.native_device_exhausted=False
                self.f.challenge_start_attempted=False
                attempts=0;deadline=self.f.clock()+1200
                self.f.emit('native_next_round',run_index=self.run_index,target_runs=self.target,
                            completed_runs=self.ledger.completed_runs)
            try:
                self.refresh();o=self.observe();self.publish()
                if not self.battle_active and o.state in (S.UNKNOWN,S.TRANSIENT):
                    o=self.wait_native_page();self.publish()
                if self.pending_review:
                    self.f.reconcile_pending(o)
                    self.pending_review=False
                supported=SUPPORTED|{S(page['state']) for page in self.f.stage_assets.pages.values()}|{S.TRANSIENT}
                if o.state not in supported and not (o.state==S.UNKNOWN and self.battle_active):
                    raise UnsupportedNativePage(o.detail or '当前 720P 页面尚未支持；已停止输入，请查看最新截图')
                if o.state==S.MAP and not self.active:
                    # A GUI restart on a verified map continues that challenge.
                    # An unexpected idle return must not start another one.
                    self.active=True
                    self.f.challenge_start_attempted=True
                    self.f.emit('native_existing_challenge',state=str(o.state),
                                reason='从真实地图继续当前挑战；不重新开始，不增加完成次数')
                if attempts>=200 or self.f.clock()>deadline:
                    raise SafetyStop('本轮达到有界运行上限；未计为完成')
                attempts+=1
                self.step(o)
            except Reobserve:
                self.cached_frame=self.cached_screen=None
            except Exception as error:
                if self.f.control.state in self.f.control.TERMINAL:raise
                self.f.suspend(str(error))
                self.f.wait_for_repair(isinstance(error,(UnsupportedNativePage,RecognitionTimeout)))
                self.f.checkpoint()
                self.f.watchdog_since=self.f.clock()  # Explicit/validated resume grants one bounded observation window.
                self.f.loading_since=None;self.f.unknown_diagnostics=0
                self.pending_review=True
                try:
                    self.f.rebuild_capture()
                except Exception as error:
                    if self.f.control.state in self.f.control.TERMINAL:raise
                    self.f.suspend(str(error))
                self.cached_frame=self.cached_screen=None
        self.publish('SUCCESS')
        self.f.control.finish('SUCCESS')


class NativeFlow:
    """Minimal existing-runner port; no crafting or legacy task activation."""
    def __init__(self,capture,interaction,hwnd,pid,control,directory,sink,preflight,*,capture_factory=None,target_runs=1):
        self.capture,self.interaction,self.hwnd,self.pid=capture,interaction,hwnd,pid
        self.control,self.logdir,self.sink,self.preflight=control,Path(directory),sink,preflight
        self.logdir.mkdir(parents=True,exist_ok=True)
        self.cfg={'肉鸽循环次数':target_runs,'step_mode':False,'debug_mode':True,'eligible_before_refresh':True,
                  'native_settlement_animation':True}
        self.stop=control.cancel_event;self.clock=control.clock
        self.state='ROGUELIKE_UNKNOWN';self.results={};self.sequence=0
        self.last_frame=self.packet=None
        self.last_action='无';self.vision=NativeVision()
        self.recognizer=RoguelikePreparation720p()
        self.task=NativeTask(self)
        self.runner=NativeRunner(self)
        self.first_stage_saved=False
        self.unknown_saved=False
        self.challenge_start_attempted=False
        self.stage_assets=NativeStageAssets()
        self.capture_factory=capture_factory
        self.capture_fault=False
        self.page_since=self.clock();self.last_advance=self.page_since
        self.watchdog_since=self.page_since
        self.loading_since=self.page_since
        self.last_confirmed='未确认';self.last_observed=S.UNKNOWN
        self.wait_reason='';self.wait_elapsed=0;self.wait_limit=None
        self.dispatched=False;self.expected_next='';self.progress_written=0
        self.input_attempted=False;self.last_action_mode='';self.pending_observation=None
        self.unknown_diagnostics=0
    @staticmethod
    def resource_observation(observation):
        return (observation.signature(),observation.currency,observation.refresh_cost,
                tuple((c.index,c.price,c.selectable,c.fingerprint) for c in observation.candidates))
    def reconcile_pending(self,observation):
        pending=self.runner.pending_action
        if not pending:return
        if observation.state in (S.UNKNOWN,S.TRANSIENT):
            raise UnsupportedNativePage('上次动作结果尚未识别；未重新发送输入')
        resource=(pending['key'] in ('refresh','fire_order') or pending['key'].startswith('exchange_'))
        if (resource and self.input_attempted and self.last_action_mode=='ONE_SHOT_TRANSITION'
                and str(observation.state)==pending['state']
                and self.resource_observation(observation)==self.pending_observation):
            raise SafetyStop('上次 '+pending['key']+' 已尝试发送但结果未确认；禁止重发消耗动作')
        self.emit('native_pending_reconciled',pending=pending,state=str(observation.state),
                  input_attempted=self.input_attempted,reason='新画面已知；保留购买/刷新预留标记')
        self.runner.pending_action=None
    def wait_for_repair(self,eligible):
        # A developer can repair recorded native assets during a single GUI run.
        # No background game polling or input while paused. An admissible
        # manifest change gets one fresh read-only capture; a historical paused
        # image cannot prove the current page. User/permission pauses remain.
        manifests=(self.stage_assets.root/'manifest.json',self.recognizer.root/'manifest.json',
                   self.recognizer.navigation.root/'manifest.json')
        seen=(self.stage_assets.revision,self.recognizer.revision,self.recognizer.navigation.revision)
        while eligible and self.control.state=='PAUSED' and not self.capture_fault:
            with self.control.condition:
                if self.control.pauses=={'safety'}:
                    revision=tuple(p.stat().st_mtime_ns if p.exists() else None for p in manifests)
                    if revision!=seen:
                        seen=revision
                        try:
                            assets=NativeStageAssets(self.stage_assets.root)
                            from src.navigation_720p import Navigation720p
                            recognizer=RoguelikePreparation720p(self.recognizer.root,
                                navigation=Navigation720p(self.recognizer.navigation.root))
                            self.preflight(False)
                            packet=self.capture.get_frame()
                            old=self.packet.metadata if self.packet is not None else {}
                            if (old.get('session_id')==packet.metadata.get('session_id') and
                                    packet.metadata['sequence']<=old.get('sequence',0)):
                                raise SafetyStop('修复验证返回历史帧；保持暂停')
                            # Validate new assets against the present HWND frame,
                            # not the screenshot from before a battle finished.
                            candidate=copy.copy(self)
                            candidate.stage_assets=assets;candidate.recognizer=recognizer
                            candidate.packet=packet;candidate.last_frame=packet.image
                            screen=NativeScreen(candidate)
                            if self.control.pauses!={'safety'}:continue
                            self.packet=packet;self.last_frame=packet.image
                            self.sink({'event':'frame','frame':packet.image,'page':str(screen.observation.state),
                                       'phase':'只读修复验证','provenance':packet.metadata,'matches':screen.result,
                                       'last_action':self.last_action,'completed':self.runner.ledger.completed_runs})
                            if screen.observation.state!=S.UNKNOWN:
                                self.stage_assets=assets;self.recognizer=recognizer
                                self.control.set_paused('safety',False)
                                self.emit('native_assets_repaired',revision=revision,
                                          reason='新原生证据已通过；下一动作前重新采集 WGC')
                                break
                        except Exception as error:
                            self.emit('native_assets_rejected',reason=str(error))
                self.control.condition.wait(.25)
    def checkpoint(self):self.control.checkpoint()
    def restore_choice_context(self):
        # Preserve an observed special battle only on the exact same paused
        # choice pixels. Four cards alone never infer a shop source: the original
        # unknown-source ordinary-shop default remains unchanged.
        previous=sorted(p for p in self.logdir.parent.iterdir()
                        if p.is_dir() and p.name<self.logdir.name)
        if not previous:return
        directory=previous[-1];events_path=directory/'events.jsonl'
        if not events_path.exists():return
        events=[json.loads(line) for line in events_path.read_text(encoding='utf-8').splitlines()]
        if not any(e.get('event')=='runtime_started' and e.get('mode')==MODE for e in events):return
        if any(e.get('event')=='runtime_finished' and e.get('completed_runs',0)>0 for e in events):return
        pauses=[e for e in events if e.get('event')=='suspended' and e.get('screenshot')]
        if not pauses:return
        from src.app_paths import resolve_history_file
        paused=pauses[-1];image=resolve_history_file(paused['screenshot'],directory)
        if image.parent.resolve()!=directory.resolve():raise SafetyStop('选择恢复截图不属于上一运行目录')
        record=json.loads(image.with_suffix('.json').read_text(encoding='utf-8'))
        status=[e for e in events if e.get('event')=='roguelike_status' and e['time']<=paused['time']]
        kind=record.get('shop_kind') or (status[-1].get('shop_kind') if status else None)
        if kind!='SPECIAL_OPERATION_SHOP':return
        old=record['source'];new=self.packet.metadata
        if (old.get('hwnd'),old.get('pid'),old.get('backend'))!=(self.hwnd,self.pid,'WGC'):return
        if type(old.get('acquired_at')) not in (int,float):return
        if not 0<self.packet.acquired_at-old['acquired_at']<3600:return
        import numpy as np
        former=cv2.imdecode(np.fromfile(image,np.uint8),1)
        if former is None or former.shape!=self.last_frame.shape:return
        # Compare stable text, not the animated card background. The actual
        # archived pair differs only in dark pixels (max11), while all four
        # bright title-glyph masks match exactly. No approximate stock/currency
        # comparison or old input is used to recover this known shop context.
        regions=((559,90,721,130),(880,18,1250,65))
        if any(not np.array_equal(former[y:b,x:r],self.last_frame[y:b,x:r]) for x,y,r,b in regions):return
        titles=((355,190,630,218),(759,190,1034,218),(355,385,630,413),(759,385,1034,413))
        for x,y,r,b in titles:
            before=former[y:b,x:r].min(axis=2)>150
            current=self.last_frame[y:b,x:r].min(axis=2)>150
            if before.sum()<20 or not np.array_equal(before,current):return
        if self.runner.shop is not None:raise SafetyStop('选择恢复与已保留商店策略冲突；未发送输入')
        self.runner.shop_kind=kind
        self.emit('native_choice_context_restored',previous_run=directory.name,kind=kind,source=new,
                  reason='相同选择页与窗口，保留已观察的特级行动上下文；不恢复输入或计数')

    def restore_shop_visit(self):
        # GUI restarts must not reset a reserved refresh on the same inventory.
        # Only the latest local run, same live HWND/PID, fresh WGC and matching
        # stock/balance/price pixels can restore policy state; never old inputs.
        previous=sorted(p for p in self.logdir.parent.iterdir()
                        if p.is_dir() and p.name<self.logdir.name)
        if not previous:return
        directory=previous[-1]
        events_path=directory/'events.jsonl'
        if not events_path.exists():return
        events=[json.loads(line) for line in events_path.read_text(encoding='utf-8').splitlines()]
        if not any(e.get('event')=='runtime_started' and e.get('mode')==MODE for e in events):return
        finishes=[e for e in events if e.get('event')=='runtime_finished']
        if (finishes and finishes[-1].get('completed_runs',0)>0
                and finishes[-1].get('control') not in ('STOPPED','CANCELLED')):return
        pauses=[e for e in events if e.get('event')=='suspended' and e.get('screenshot')]
        if not pauses:return
        from src.app_paths import resolve_history_file
        paused=pauses[-1];image=resolve_history_file(paused['screenshot'],directory)
        if image.parent.resolve()!=directory.resolve():raise SafetyStop('商店恢复截图不属于上一运行目录')
        record=json.loads(image.with_suffix('.json').read_text(encoding='utf-8'))
        old=record['source'];new=self.packet.metadata
        if (old.get('hwnd'),old.get('pid'),old.get('backend'))!=(self.hwnd,self.pid,'WGC'):return
        saved=record.get('shop_visit')
        # Compatibility with the immediately preceding build: its dispatched
        # pending refresh is already reserved, even though OCR never confirmed it.
        pending=paused.get('pending_action') or {}
        dispatches=[e for e in events if e.get('event')=='roguelike_input_dispatched']
        if (saved is None and pending.get('key')=='refresh' and dispatches
                and dispatches[-1].get('action')=='refresh'):
            saved=dict(kind='NORMAL_BATTLE_SHOP',refresh_used=True,purchases=0,fallback_used=False)
        if not saved:return
        if record.get('stage') not in ('SHOP','TRANSIENT'):return
        age=new['acquired_at']-old['acquired_at']
        if not 0<age<3600:raise SafetyStop('上一商店资源动作证据过期；未重放购买或刷新')
        import numpy as np
        former=cv2.imdecode(np.fromfile(image,np.uint8),1)
        if former is None or former.shape!=self.last_frame.shape:raise SafetyStop('上一商店恢复截图无效')
        regions=((180,178,1243,390),(1018,18,1069,60),(1034,117,1235,157))
        if any(not np.array_equal(former[y:b,x:r],self.last_frame[y:b,x:r])
               for x,y,r,b in regions):raise SafetyStop('上一商店资源动作结果与当前库存不一致；未重放购买或刷新')
        if (saved.get('kind')!='NORMAL_BATTLE_SHOP' or type(saved.get('refresh_used')) is not bool
                or type(saved.get('fallback_used')) is not bool or type(saved.get('purchases')) is not int
                or not 0<=saved['purchases']<=24):raise SafetyStop('上一商店额度记录无效')
        self.restore_completed_batch(events,directory,paused,new,('SHOP',))
        self.runner.shop=ShopVisit(**saved);self.runner.shop_kind=saved['kind']
        self.emit('native_shop_visit_restored',previous_run=directory.name,shop_visit=saved,
                  source=new,reason='相同库存与余额，保留资源动作额度；不恢复或重放点击')
    def restore_free_member_batch(self,observation):
        # Carry earned rounds through a source reload at the same mandatory
        # free selection. Restore only the verified ledger, never old clicks.
        previous=sorted(p for p in self.logdir.parent.iterdir()
                        if p.is_dir() and p.name<self.logdir.name)
        if not previous:return
        directory=previous[-1];path=directory/'events.jsonl'
        if not path.exists():return
        events=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        if not any(e.get('event')=='runtime_started' and e.get('mode')==MODE for e in events):return
        finishes=[e for e in events if e.get('event')=='runtime_finished']
        if finishes and finishes[-1].get('control') not in ('STOPPED','CANCELLED'):return
        pauses=[e for e in events if e.get('event')=='suspended' and e.get('screenshot')]
        if not pauses or pauses[-1].get('state')!='OUTPOST':return
        paused=pauses[-1]
        if paused.get('pending_action'):raise SafetyStop('上次免费选人动作未确认；不恢复批次')
        from src.app_paths import resolve_history_file
        image=resolve_history_file(paused['screenshot'],directory)
        if image.parent.resolve()!=directory.resolve():raise SafetyStop('免费选人恢复截图来源无效')
        record=json.loads(image.with_suffix('.json').read_text(encoding='utf-8'))
        old=record['source'];new=self.packet.metadata
        if (old.get('hwnd'),old.get('pid'),old.get('backend'))!=(self.hwnd,self.pid,'WGC'):return
        if record.get('stage')!='OUTPOST':return
        if not 0<new['acquired_at']-old['acquired_at']<3600:
            raise SafetyStop('上一免费选人批次证据过期')
        if observation.state!=S.OUTPOST or len(observation.candidates)!=3:
            raise SafetyStop('当前免费选人页面缺少三个可靠候选')
        import numpy as np
        former=cv2.imdecode(np.fromfile(image,np.uint8),1)
        if former is None or former.shape!=self.last_frame.shape:
            raise SafetyStop('上一免费选人截图无效')
        for card in observation.candidates:
            x,y,r,b=card.area;w=r-x;h=b-y
            # Static portrait interior excludes the animated transparent card.
            a,c,d,e=x+round(w*.04),y+round(h*.10),x+round(w*.28),y+round(h*.90)
            if not np.array_equal(former[c:e,a:d],self.last_frame[c:e,a:d]):
                raise SafetyStop('免费候选画面已变化；不恢复旧批次')
        self.restore_completed_batch(events,directory,paused,new,('OUTPOST',))
        self.runner.active=True;self.challenge_start_attempted=True
        self.emit('native_existing_challenge',state='OUTPOST',
                  reason='同一免费选人页接管当前挑战；保留结算证据，不重放点击')
    def restore_completed_batch(self,events,directory,paused,source,allowed_states):
        statuses=[e for e in events if e.get('event')=='roguelike_status']
        finishes=[e for e in events if e.get('event')=='runtime_finished']
        completed=statuses[-1].get('completed_runs',0) if statuses else 0
        if finishes and finishes[-1].get('completed_runs',0)>0 and not completed:
            raise SafetyStop('暂停批次缺少最后页面计数；未开始额外挑战')
        if completed:
            # Continue the unfinished GUI batch, never repeat an earned round.
            # The exact paused inventory/source above gates this recovery; only
            # original round_complete + verified idle/ordered settlement count.
            if (statuses[-1].get('state') not in allowed_states or statuses[-1].get('pending_action')
                    or statuses[-1].get('settlement_progress')!=0
                    or statuses[-1].get('target_runs')!=self.runner.target
                    or type(completed) is not int or not 0<completed<self.runner.target):
                raise SafetyStop('暂停批次进度与当前目标不一致；未开始额外挑战')
            if finishes and finishes[-1].get('completed_runs')!=completed:
                raise SafetyStop('暂停批次结束计数不一致')
            if any(e.get('event')=='roguelike_input_dispatched' for e in events[events.index(paused)+1:]):
                raise SafetyStop('暂停后仍有游戏输入；不能恢复批次进度')
            proved=self.completed_evidence(directory,self.runner.target)
            if proved!=completed:raise SafetyStop('暂停批次缺少完整结算证据；未恢复计数')
            self.runner.ledger.completed_runs=completed
            self.runner.run_index=completed+1
            self.emit('native_run_progress_restored',previous_run=directory.name,
                      completed_runs=completed,target_runs=self.runner.target,source=source)
    def restore_battle_result_batch(self):
        # A live battle may finish while a metadata/perception error is paused.
        # Only that adjacent, fully recognized victory can carry batch progress;
        # never restart a challenge, replay deployment or count this victory.
        if self.runner.current.state!=S.BATTLE_RESULT or self.runner.battle_kind not in ('special','encounter'):
            raise SafetyStop('当前胜利页或战斗类型未确认；未接管')
        previous=sorted(p for p in self.logdir.parent.iterdir() if p.is_dir() and p.name<self.logdir.name)
        if not previous:return
        directory=previous[-1];path=directory/'events.jsonl'
        if not path.exists():return
        events=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        if not any(e.get('event')=='runtime_started' and e.get('mode')==MODE for e in events):return
        finishes=[e for e in events if e.get('event')=='runtime_finished']
        if finishes and finishes[-1].get('control') not in ('STOPPED','CANCELLED'):return
        pauses=[e for e in events if e.get('event')=='suspended' and e.get('screenshot')]
        if not pauses:return
        paused=pauses[-1]
        if paused.get('pending_action'):raise SafetyStop('上次战斗动作未确认；不恢复批次')
        from src.app_paths import resolve_history_file
        image=resolve_history_file(paused['screenshot'],directory)
        if image.parent.resolve()!=directory.resolve():raise SafetyStop('战斗恢复截图来源无效')
        record=json.loads(image.with_suffix('.json').read_text(encoding='utf-8'))
        old=record['source'];new=self.packet.metadata
        if (old.get('hwnd'),old.get('pid'),old.get('backend'))!=(self.hwnd,self.pid,'WGC'):return
        if record.get('stage') not in ('BATTLE','UNKNOWN','TRANSIENT'):return
        age=new['acquired_at']-old['acquired_at']
        if not 0<age<3600:raise SafetyStop('上一战斗批次证据过期')
        contexts=[e for e in events if e.get('event')=='roguelike_battle_observation']
        if (not contexts or not contexts[-1].get('battle_active')
                or contexts[-1].get('battle_kind') not in (None,self.runner.battle_kind)):
            raise SafetyStop('上一战斗类型证据不符；未接管')
        # The fresh, type-specific victory page proves the battle ended. Device
        # exhaustion is neither necessary for victory nor proof of a completed
        # round. Keep unverified deployment unverified, never replay it; carry
        # only earlier rounds with the existing complete settlement evidence.
        self.restore_completed_batch(events,directory,paused,new,('BATTLE','UNKNOWN','TRANSIENT'))
        self.runner.active=True;self.challenge_start_attempted=True
        self.emit('native_existing_challenge',state='BATTLE_RESULT',reason='已验证同一战斗胜利；接管批次，不重放部署或增加计数')
    def completed_evidence(self,directory,target,depth=0):
        if depth>=100:raise SafetyStop('暂停批次证据链过长')
        events=[json.loads(line) for line in (directory/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        starts=[e for e in events if e.get('event')=='runtime_started']
        if len(starts)!=1 or starts[0].get('mode')!=MODE or starts[0].get('target_runs')!=target:
            raise SafetyStop('暂停批次运行来源或目标不符')
        count=0;waiting=False
        for e in events:
            if e.get('event')=='native_run_progress_restored':
                previous=self.logdir.parent/e.get('previous_run','')
                if (count or waiting or previous.parent.resolve()!=self.logdir.parent.resolve()
                        or previous.name>=directory.name or not previous.is_dir()):
                    raise SafetyStop('暂停批次进度来源无效')
                count=self.completed_evidence(previous,target,depth+1)
                if count!=e.get('completed_runs'):raise SafetyStop('暂停批次继承计数不符')
            if e.get('event')=='roguelike_round_complete':
                if waiting or e.get('completed_runs')!=count+1 or e.get('run_index')!=count+1:
                    raise SafetyStop('暂停批次重复或跳过结算计数')
                waiting=True;count+=1
            if waiting and e.get('event')=='roguelike_status':
                if (e.get('state')!='START' or e.get('roguelike_event')!='idle'
                        or e.get('settlement_progress')!=5 or e.get('pending_action')
                        or e.get('completed_runs')!=count or e.get('target_runs')!=target):
                    raise SafetyStop('暂停批次未确认结算后返回准备页')
                waiting=False
        if waiting:raise SafetyStop('暂停批次结算证据不完整')
        return count
    def delay(self,duration):
        deadline=self.clock()+duration
        while self.clock()<deadline:
            self.checkpoint();time.sleep(min(.05,max(0,deadline-self.clock())))
    def refresh(self):
        self.checkpoint()
        if self.preflight(False) is True:
            # Relocation invalidates WGC's client crop and all prepared actions.
            self.capture_fault=True
            self.rebuild_capture()
        if self.capture_fault:raise SafetyStop('WGC 会话已失效；点击继续后重新创建，禁止旧帧回退')
        try:packet=self.capture.get_frame()
        except Exception:
            self.capture_fault=True
            raise
        self.checkpoint()
        # observe validates HWND/PID, dimensions, dtype, backend and freshness.
        result=self.recognizer.observe(packet,hwnd=self.hwnd,pid=self.pid)
        self.packet=packet;self.last_frame=packet.image
        self.vision.results={}
        self.sink({'event':'frame','page':result['state'],'phase':result.get('preparation',{}).get('phase'),
                   'frame':packet.image,'provenance':packet.metadata,'matches':result,
                   'last_action':self.last_action,'completed':self.runner.ledger.completed_runs})
    def record_observation(self,observation):
        now=self.clock()
        loading=observation.state in (S.UNKNOWN,S.TRANSIENT)
        if loading:
            if self.loading_since is None:self.loading_since=now
        else:
            self.loading_since=None
            self.unknown_diagnostics=0
        if observation.state!=self.last_observed:
            self.page_since=now
            if observation.state not in (S.UNKNOWN,S.TRANSIENT):
                self.last_advance=now;self.last_confirmed=str(observation.state)
            self.last_observed=observation.state
        self.progress()
        if loading and not self.runner.battle_active:
            elapsed=now-self.loading_since
            if elapsed>=UNKNOWN_TIMEOUT:
                raise UnsupportedNativePage('连续三次页面诊断仍未推进，无法确认状态；已停止输入并保存进度')
            if elapsed >= UNKNOWN_DIAGNOSTIC_INTERVAL*(self.unknown_diagnostics+1):
                self.unknown_diagnostics+=1
                evidence=self.snapshot('unknown_diagnosis')
                self.emit('native_unknown_diagnosis',attempt=self.unknown_diagnostics,
                          elapsed=elapsed,screenshot=str(evidence),pending_action=self.runner.pending_action,
                          reason='三秒未推进；重新加载识别器，下一帧只读复查，不重发动作')
                self.recognizer=RoguelikePreparation720p()
                self.stage_assets=NativeStageAssets()
                self.runner.cached_frame=self.runner.cached_screen=None
        limit=(TIMING.battle_timeout+TIMING.deployment_camera_timeout
               if observation.state==S.BATTLE or self.runner.battle_active
               else min(TIMING.load_timeout,UNKNOWN_TIMEOUT) if observation.state in (S.UNKNOWN,S.TRANSIENT)
               else 2*TIMING.load_timeout+TIMING.safe_action_attempts*TIMING.stable_retry_after)
        # A newly loading page gets its own bounded budget. UNKNOWN/TRANSIENT
        # flicker cannot restart it, and an active battle retains its total cap.
        baseline=max(self.last_advance,self.watchdog_since)
        if loading and not self.runner.battle_active:
            baseline=max(baseline,self.loading_since)
        stalled=now-baseline
        if stalled>limit:
            self.wait_reason=f'{observation.state}: 截图持续更新，但状态超过 {limit:g} 秒未推进'
            self.wait_elapsed=stalled;self.wait_limit=limit
            self.progress(force=True)
            raise SafetyStop(self.wait_reason)
    def progress(self,force=False):
        now=time.monotonic()
        if not force and now-self.progress_written<.5:return
        self.progress_written=now
        data={'event':'native_progress','page':str(self.last_observed),
              'last_confirmed':self.last_confirmed,'state_elapsed':max(0,self.clock()-self.page_since),
              'since_advance':max(0,self.clock()-self.last_advance),'wait_reason':self.wait_reason,
              'wait_elapsed':self.wait_elapsed,'wait_limit':self.wait_limit,
              'action':self.last_action,'dispatched':self.dispatched,'expected_next':self.expected_next,
              'pending_action':self.runner.pending_action,'completed':self.runner.ledger.completed_runs,
              'source':self.packet.metadata if self.packet else None,'control':self.control.state}
        temp=self.logdir/'progress.tmp'
        temp.write_text(json.dumps(data,ensure_ascii=False,default=str,indent=2),encoding='utf-8')
        try:
            replace_status(temp,self.logdir/'progress.json')
        except PermissionError as error:
            # A Windows reader without FILE_SHARE_DELETE can briefly hold the
            # previous complete JSON. Metadata contention is not a game error:
            # keep that file, publish the live GUI frame/status and retry on the
            # next bounded progress tick. Never replay or cancel a game action.
            self.sink({'event':'native_progress_write_deferred','reason':str(error)})
            data['status_write_error']='状态文件暂未更新；实时画面及运行继续：'+str(error)
        self.sink(data)
    def rebuild_capture(self):
        if not self.capture_fault:return
        if self.capture_factory is None:raise SafetyStop('WGC 会话无法重建；请停止并重新开始')
        self.checkpoint();self.preflight(False)
        try:self.capture.close()
        except Exception:
            self.control.finish('ERROR',source='WGC 资源释放失败；禁止创建并行会话')
            raise
        # Exactly one attempt per explicit resume; never automatic fallback/retry.
        self.capture=self.capture_factory()
        self.capture_fault=False
        self.recognizer=RoguelikePreparation720p()
        self.packet=self.last_frame=None
        self.runner.cached_frame=self.runner.cached_screen=None
        self.emit('capture_rebuilt',session_id=self.capture.session_id)
    def emit(self,event,**data):
        if event=='roguelike_input_intent':
            self.input_attempted=False;self.last_action_mode=data.get('action_mode','')
            self.pending_observation=self.resource_observation(self.runner.current)
            self.last_action=data.get('action','');self.dispatched=False
            self.wait_reason=data.get('decision','')
            expected={S.MAIN_MENU:'NAV / ENTRY',S.NAV:'ENTRY',S.ENTRY:'START',
                      S.START:'OVERLAY / MAP / EVENT / CHOICE / SETTLEMENT_CONFIRM',
                      S.OVERLAY:'新弹层 / MAP / 已知页面',S.SHOP:'库存更新 / 奖励 / MAP',
                      S.OUTPOST:'已选队员 / 奖励 / MAP',S.ORDER:'EVENT / OUTPOST / OVERLAY',
                      S.MAP:'事件 / 节点页面 / START',S.BATTLE_PREP:'BATTLE',
                      S.BATTLE:'部署数量减少 / BATTLE_RESULT',S.BATTLE_RESULT:'奖励 / SHOP / CHOICE',
                      S.SETTLEMENT_CONFIRM:'RESULT_STATS',S.RESULT_STATS:'RESULT_TEAM',
                      S.RESULT_TEAM:'RESULT',S.RESULT:'START / idle'}
            self.expected_next=expected.get(self.runner.current.state,'既有状态机的下一页')
        if event=='native_input_boundary':self.input_attempted=True
        if event=='roguelike_input_dispatched':self.last_action=data.get('action','');self.dispatched=True
        if event in ('roguelike_wait','roguelike_safe_action_wait'):
            self.wait_reason=data.get('reason',self.wait_reason)
            self.wait_elapsed=data.get('elapsed',self.wait_elapsed);self.wait_limit=data.get('timeout',self.wait_limit)
        if event=='roguelike_action_timing':
            # Confirmed purchases/refreshes may stay on the same page. They are
            # real advancement too; repeated observations alone are not.
            self.last_advance=self.clock()
            self.wait_reason='已确认推进到 '+str(data.get('next_state',''))
            self.wait_elapsed=0;self.wait_limit=None
        self.progress(force=event not in ('roguelike_wait','roguelike_safe_action_wait'))
        # Polling is not a disk log flood. Only state changes/actions/errors persist.
        if event not in ('roguelike_wait','roguelike_safe_action_wait'):
            from src.release_limits import evidence_allowed
            evidence_allowed(self.logdir)
            with (self.logdir/'events.jsonl').open('a',encoding='utf-8') as file:
                file.write(json.dumps({'time':datetime.now().isoformat(),'event':event,**data},ensure_ascii=False,default=str)+'\n')
        self.sink({'event':event,**data,'last_action':self.last_action})
    def snapshot(self,label):
        from src.release_limits import evidence_allowed
        if not evidence_allowed(self.logdir):return None
        if self.packet is None:return None
        self.sequence+=1
        path=self.logdir/f'{self.sequence:03d}-{label}.png'
        cv2.imencode('.png',self.last_frame)[1].tofile(path)
        screen=self.runner.cached_screen if self.runner.cached_frame is self.last_frame else None
        path.with_suffix('.json').write_text(json.dumps({'source':self.packet.metadata,
            'stage':str(self.runner.current.state),'last_action':self.last_action,
            'shop_kind':self.runner.shop_kind,
            'shop_visit':vars(self.runner.shop).copy() if self.runner.shop else None,
            # Error evidence must never re-enter the failing recognition/OCR.
            'matches':screen.result if screen else {'state':'UNKNOWN','reason':'frame not classified'}},
            ensure_ascii=False,default=str,indent=2),encoding='utf-8')
        return path
    def suspend(self,reason):
        # Last trusted frame is explicitly labelled with its original metadata.
        # Try one new read only; failed acquisition is never replaced by a fake fresh frame.
        capture_error=None
        try:self.refresh()
        except Exception as error:capture_error=str(error)
        path=self.snapshot('suspended')
        self.control.set_paused('safety',True)
        self.emit('suspended',reason=reason,capture_error=capture_error,screenshot=str(path) if path else None,
                  state=str(self.runner.current.state),pending_action=self.runner.pending_action)
        # Preserve one compact real first-stage fixture batch, no ongoing polling.
        if not self.first_stage_saved and capture_error is None and self.runner.screen().observation.state==S.UNKNOWN:
            self.first_stage_saved=True
            records=[]
            for index in range(1,21):
                if self.control.state in self.control.TERMINAL:break
                try:
                    packet=self.capture.get_frame()
                    result=self.recognizer.observe(packet,hwnd=self.hwnd,pid=self.pid)
                    file=self.logdir/f'first-stage-{index:02}.png'
                    cv2.imencode('.png',packet.image)[1].tofile(file)
                    records.append({'file':str(file),'metadata':packet.metadata,'recognition':result})
                    self.sink({'event':'frame','page':result['state'],'frame':packet.image,'provenance':packet.metadata})
                except Exception as error:
                    records.append({'error':str(error)});break
                time.sleep(.12)
            (self.logdir/'first-stage.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')


class NativeTask:
    def __init__(self,flow):
        self.f=flow;self.roguelike_control=flow.control;self.daily_focus=None;self.ocr_port=None
        self.focus_recovery_attempts=0
        self.ocr_cache=OrderedDict()
        self.stale_decisions=0
    def info_set(self,*args):pass
    def log_info(self,*args,**kwargs):pass
    def ocr(self,*,frame,**kwargs):
        # Reuse the initialized project OCR; never request a legacy task frame.
        from ok import og
        if self.f.control.state in self.f.control.TERMINAL:raise SafetyStop('OCR after runtime stop')
        if frame is None or not frame.size:raise SafetyStop('Native OCR requires explicit current pixels')
        if self.ocr_port is None:
            task=next(t for t in og.executor.onetime_tasks if t.only=='ROGUELIKE')
            self.ocr_port=copy.copy(task)
            self.ocr_port._executor=NativeOCRExecutor(task.executor)
            self.ocr_port.screenshot=lambda *a,**k:self.f.snapshot('ocr_error')
        # Reuse OCR only for byte-identical pixels read from the current WGC
        # frame, with identical parameters. This does not reuse an observation
        # or screenshot. Re-reading unchanged shop text otherwise makes every
        # decision older than the one-second dispatch gate on slower machines.
        key=(frame.shape,str(frame.dtype),hashlib.sha256(frame.tobytes()).digest(),
             repr(sorted(kwargs.items())))
        if key in self.ocr_cache:
            result=copy.deepcopy(self.ocr_cache[key]);self.ocr_cache.move_to_end(key)
        else:
            result=self.ocr_port.ocr(frame=frame,**kwargs)
            self.ocr_cache[key]=copy.deepcopy(result)
            if len(self.ocr_cache)>128:self.ocr_cache.popitem(last=False)
        if self.f.control.state in self.f.control.TERMINAL:raise SafetyStop('OCR after runtime stop')
        return result
    def before_click(self):
        previous=self.f.runner.current.state
        try:self.f.preflight(True)
        except SafetyStop as error:self.recover_foreground(error)
        self.f.runner.refresh();self.f.runner.observe()
        if previous==S.BATTLE and self.f.runner.current.state!=S.BATTLE:raise Reobserve()
    def assert_foreground(self):
        try:self.f.preflight(True,activate=False)
        except SafetyStop as error:self.recover_foreground(error)
    def recover_foreground(self,error):
        if str(error) not in ('游戏失去前台；未发送输入','点击位置被其他窗口遮挡；未发送输入'):raise error
        if self.focus_recovery_attempts>=3:
            raise SafetyStop('连续三次失去前台；未发送输入，请保持游戏前台') from error
        self.focus_recovery_attempts+=1
        self.f.emit('native_focus_recovery',attempt=self.focus_recovery_attempts,
                    reason='识别期间失去前台；重新激活并丢弃旧动作坐标')
        self.f.preflight(True)
        self.f.runner.refresh();self.f.runner.observe()
        raise Reobserve() from error
    def validate_click_context(self):
        age=time.time()-self.f.packet.acquired_at
        if age>1:
            self.stale_decisions+=1
            self.f.emit('native_stale_decision',age=age,state=str(self.f.runner.current.state),
                        attempt=self.stale_decisions,source=self.f.packet.metadata)
            if self.stale_decisions>=3:
                raise SafetyStop('连续三次识别耗时超过截图有效期；未发送点击，已保留进度')
            raise Reobserve()
        self.stale_decisions=0
    def validate_deployment_target(self,x,y):
        runner=self.f.runner
        key=(runner.pending_action or {}).get('key')
        if key not in ('special_card','special_ground'):return
        if runner.current.state!=S.BATTLE:raise Reobserve()
        screen=runner.screen().native
        card=screen.find_special_deploy_card_fast()
        selected=screen.special_device_selected()
        if key=='special_card':
            valid=card is not None and not selected and max(
                abs(x-round((card.area[0]+card.area[2])/2)),
                abs(y-round((card.area[1]+card.area[3])/2)))<=6
        else:
            # The previous click only requested selection. An ordinary member
            # selected by an animation/hand shift must never authorize ground input.
            valid=card is not None and selected
        if valid:
            if key=='special_ground':runner.native_selection_recoveries=0
            return
        attempts=getattr(runner,'native_selection_recoveries',0)+1
        runner.native_selection_recoveries=attempts
        self.f.snapshot('deployment_target_changed')
        self.f.emit('native_deployment_reobserve',action=key,attempt=attempts,
                    selected=selected,current_card=card.area if card else None,
                    requested=[x,y],source=self.f.packet.metadata)
        if attempts>=3:raise SafetyStop('三次未确认特殊装置选中；禁止地面输入并保留进度')
        raise Reobserve()
    def click(self,x,y,**kwargs):return self.roguelike_click(x,y,**kwargs)
    def roguelike_guarded_click(self,x,y,before_input=None,**kwargs):
        return self.roguelike_click(x,y,before_input=before_input,**kwargs)
    def roguelike_click(self,x,y,**kwargs):
        if ((self.f.runner.pending_action or {}).get('key') in ('special_card','special_ground')
                and self.f.runner.current.state!=S.BATTLE):raise Reobserve()
        from src.client_geometry import validate_client_geometry,client_input_to_screen
        try:
            self.validate_click_context()
            self.validate_deployment_target(x,y)
            self.f.preflight(True,activate=False)
            validate_client_geometry(self.f.hwnd,expected_size=(1280,720))
            screen=client_input_to_screen(self.f.hwnd,x,y)
            import win32gui
            hit=win32gui.WindowFromPoint(screen)
            if hit!=self.f.hwnd and not win32gui.IsChild(self.f.hwnd,hit):
                cover={}
                try:
                    import win32process
                    cover={'pid':win32process.GetWindowThreadProcessId(hit)[1],
                           'class':win32gui.GetClassName(hit),'title':win32gui.GetWindowText(hit),
                           'rect':list(win32gui.GetWindowRect(hit))}
                except Exception:pass  # A transient window may already be gone.
                self.f.emit('native_input_occluded',target_hwnd=self.f.hwnd,hit_hwnd=hit,
                            screen=list(screen),cover=cover,source=self.f.packet.metadata)
                # This guard runs before the backend: no press was submitted,
                # including for resource actions. Discard the decision and
                # resolve the current page again; never replay an old click.
                self.recover_foreground(SafetyStop('点击位置被其他窗口遮挡；未发送输入'))
        except Exception:
            # START's existing callback reserves the round before dispatch.
            # Roll it back only when no backend call has occurred, never after
            # an ambiguous press/transition or an already running challenge.
            pending=self.f.runner.pending_action or {}
            if (pending.get('key')=='start' and self.f.runner.current.state==S.START
                    and not self.f.challenge_start_attempted):
                self.f.runner.active=False
            raise
        self.f.emit('native_input_boundary',client=[x,y],screen=list(screen),source=self.f.packet.metadata)
        # Reserve a purchase/refresh/round only after every no-input guard.
        # Once the backend is entered, an ambiguous receipt retains the reserve.
        before_input=kwargs.get('before_input')
        if before_input:before_input()
        if (self.f.runner.pending_action or {}).get('key')=='start':self.f.challenge_start_attempted=True
        try:self.f.interaction.click(x,y,move_back=False,down_time=kwargs.get('down_time',.05))
        finally:
            receipt=getattr(self.f.interaction,'last_input_receipt',None)
            self.f.emit('native_backend_result',mouse_dispatch=getattr(self.f.interaction,'native_dispatch',None),
                        receipt=receipt.as_dict() if hasattr(receipt,'as_dict') else None)
        self.focus_recovery_attempts=0


def run_native_application(control,sink,directory,exclusive=lambda:True,target_runs=1):
    """Called exclusively after GUI Start, never by diagnostic/smoke startup."""
    from src.settings import load_settings
    from src.resolution import Resolution,selected_size
    Resolution((1280,720),selected_size(load_settings()))
    if type(target_runs) is not int or not 1<=target_runs<=100:
        raise SafetyStop('肉鸽循环次数应为1至100的整数')
    import ctypes,win32gui,win32process
    from src.capture_720p import Capture720p
    from src.client_geometry import validate_client_geometry,_dpi_awareness
    from src.diagnostics import require_input_privilege,window_identity
    from src.interaction import GameForegroundInteraction,activate_window
    from pynput import keyboard
    hwnd,pid=discover_target()
    from src.game_window_720p import ensure_visible_game
    relocations=[]
    geometry_changed=False
    flow=None
    def preflight(input_action,activate=True):
        nonlocal geometry_changed
        if not exclusive():raise SafetyStop('其他任务正在运行；720P 输入已暂停，禁止并行控制游戏')
        relocation=ensure_visible_game(hwnd,pid,allow_move=len(relocations)<3)
        if relocation:
            relocations.append(relocation);geometry_changed=True
            if flow is not None:flow.emit('native_window_relocated',**relocation)
            else:sink({'event':'native_window_relocated',**relocation})
        validate_client_geometry(hwnd,expected_size=(1280,720))
        identity=window_identity(hwnd)
        if identity.get('pid')!=pid or identity.get('exe','').casefold()!='warp knights.exe' or identity.get('minimized'):
            raise SafetyStop('游戏 HWND/PID 或窗口状态改变；已停止输入')
        if input_action:
            if geometry_changed:raise Reobserve()
            require_input_privilege(hwnd)
            if activate:activate_window(hwnd)
            if win32gui.GetForegroundWindow()!=hwnd:raise SafetyStop('游戏失去前台；未发送输入')
        else:
            changed=geometry_changed;geometry_changed=False
            return changed
    api=ctypes.windll.user32.GetDpiForWindow
    api.argtypes=[ctypes.c_void_p];api.restype=ctypes.c_uint
    preflight(False)
    sink({'event':'preflight','hwnd':hwnd,'pid':pid,'size':[1280,720],'dpi':api(hwnd),
          'dpi_awareness':_dpi_awareness(),'mode':MODE,'target_runs':target_runs,'directory':str(directory)})
    held=set()
    def pressed(key):
        if key in held:return
        held.add(key)
        if key==keyboard.Key.f7:control.toggle_pause('user')
        if key==keyboard.Key.f8:control.finish('STOPPED',source='F8')
    listener=keyboard.Listener(on_press=pressed,on_release=lambda key:held.discard(key))
    capture=Capture720p(hwnd,pid)
    flow=None
    try:
        target=SimpleNamespace(hwnd=hwnd,is_foreground=lambda:win32gui.GetForegroundWindow()==hwnd)
        class AuditedInteraction(GameForegroundInteraction):
            def _mouse_dispatch(self,x,y,action='move',key='left',amount=0):
                result=super()._mouse_dispatch(x,y,action,key,amount)
                if result and action in ('press','release'):self.native_dispatch[action]=True
                return result
            def click(self,*args,**kwargs):
                self.native_dispatch={'press':False,'release':False}
                super().click(*args,**kwargs)
                if not all(self.native_dispatch.values()):
                    raise SafetyStop('输入后端未完成按下/释放：'+str(self.native_dispatch))
        interaction=AuditedInteraction(None,target)
        flow=NativeFlow(capture,interaction,hwnd,pid,control,directory,sink,preflight,
                        capture_factory=lambda:Capture720p(hwnd,pid),target_runs=target_runs)
        listener.start()
        try:
            from src.runtime_identity import get_runtime_identity
            flow.emit('runtime_started',mode=MODE,target_runs=flow.runner.target,runtime_identity=get_runtime_identity().as_dict())
            for relocation in relocations:flow.emit('native_window_relocated',**relocation)
            flow.runner.run_native()
        finally:
            listener.stop();listener.join(timeout=1)
            flow.snapshot('runtime_final')
            flow.emit('runtime_finished',control=control.state,completed_runs=flow.runner.ledger.completed_runs,
                      pending_action=flow.runner.pending_action)
    finally:
        (flow.capture if flow is not None else capture).close()
