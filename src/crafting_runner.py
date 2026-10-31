"""Observed-state crafting. Every input is revalidated; spending clicks never retry."""
import json
import re
import time
from dataclasses import asdict
from uuid import uuid4
from src.workflow import SafetyStop,RecognitionTimeout
from src.module_crafting import ModulePreparation
from src.crafting_policy import CraftRecord,PROFILES,FACTORS,skill_name
from src.crafting_vision import CraftScreen,ChoiceUnreadable,SkillIdentityChanged
from src import crafting_local_runtime

_OBSERVED_SCREEN_TYPE=CraftScreen

status={}
CHOICES={'attribute','skill','mission','factor','core_attribute','core_skill','rest'}

class SkillEvidenceInsufficient(ChoiceUnreadable):
    """No input was sent; a new complete skill decision may replace this intent."""


class ModuleCrafting:
    def __init__(self,flow):
        self.f=flow
        self.record=CraftRecord()
        self.policy=PROFILES[flow.cfg.get('制作配置','旧地联合')]()
        self.target=int(flow.cfg.get('制作数量',1))
        if not 1<=self.target<=100:raise SafetyStop('制作数量应为1至100')
        self.normal=self.quick=0
        self.run_id=str(uuid4())
        self.checkpoint_session_id=str(uuid4())
        self.hub_baseline=None
        self.rebaseline=None
        self.epoch_baseline=None
        self.completion_source='verified_run_ledger'
        self.accounting_trusted=True
        self.continuation_scope='batch'
        self.historical_completed=0
        self.session_delta_completed=0
        self.unknown_completion=0
        self.module_active=False
        self.cached_frame=None
        self.cached_screen=None
        self.verified_frame=None
        self.verified_at=None
        self.verified_revision=None
        self._fast_choice=None
        self._fast_navigation=None
        self.allow_fast_followup=True
        self.uncertain=False
        self.fast=False
        self.refresh_state=None
        self.pending_action=None
        self.hub_step=None
        self.last_decision=None
        self.completion_kind=None
        self.normal_origin_known=True
        self.etching_pending=False
        self.etch_result_seen=False
        self.refresh_spent=0
        from src.crafting_decision import AttributeConfig
        self.attribute_config=AttributeConfig(
            refresh_mode=flow.cfg.get('属性刷新方式','自动'),
            refresh_min_people=flow.cfg.get('属性刷新人数门槛',6),
            prioritize_fire_reward=flow.cfg.get('优先火力攻击奖励任务',True))
        from src.resolution import ReferenceTask
        self.page_observation=isinstance(getattr(flow,'task',None),ReferenceTask)
        from src.crafting_acceptance import CraftAcceptance
        self.acceptance_action_id=None
        self.acceptance_evidence=CraftAcceptance(self)
        timing=getattr(flow,'crafting_timing',None)
        if timing:timing.install_runner(self)
        self.execution_phase('开始观察当前制作页面')

    @property
    def acceptance_evidence(self):
        evidence=self.__dict__.get('_acceptance_evidence')
        if evidence is None:
            from src.crafting_acceptance import CraftAcceptance
            evidence=self._acceptance_evidence=CraftAcceptance(self)
        return evidence

    @acceptance_evidence.setter
    def acceptance_evidence(self,value):
        self._acceptance_evidence=value

    def screen(self,full=True):
        if self.cached_frame is not self.f.last_frame:
            native=getattr(self.f.task,'native_frame',None)
            choice=getattr(self,'_fast_choice',None)
            navigation=getattr(self,'_fast_navigation',None)
            if not full and navigation and native is not None:
                from src.crafting_fast import LocalHubScreen
                current=LocalHubScreen(self.f.task,self.f.last_frame,native,navigation['previous'])
            elif not full and choice and native is not None:
                from src.crafting_fast import LocalChoiceScreen
                current=LocalChoiceScreen(self.f.task,self.f.last_frame,choice['option'],choice['kind'],native,choice.get('previous'))
                if current.local_exit_reason:
                    self.f.emit('craft_fast_exit',reason=current.local_exit_reason,kind=choice['kind'])
            elif not full and getattr(self,'page_observation',False):
                current=CraftScreen(self.f.task,self.f.last_frame,page_only=True,native_frame=native)
            else:current=CraftScreen(self.f.task,self.f.last_frame)
            from src.resolution import ReferenceTask
            if isinstance(self.f.task,ReferenceTask):
                import hashlib
                import numpy as np
                from copy import deepcopy
                task=self.f.task;native=task.native_frame;reference=task.reference_frame
                if (native is None or reference is None or native.ndim!=3 or native.shape[2]!=3
                        or tuple(native.shape[1::-1])!=task.geometry.size
                        or reference.shape!=self.f.last_frame.shape
                        or not np.array_equal(reference,self.f.last_frame)):
                    raise SafetyStop('原生截图与当前制作观察轮次不一致，未使用原生卡位证据')
                metadata=deepcopy(task.metadata)
                game=getattr(self.f,'target_identity',None) or {}
                if isinstance(metadata,dict) and any(metadata.get(key)!=game.get(key) for key in ('hwnd','pid')):
                    raise SafetyStop('原生截图来源与当前游戏窗口不一致，未使用原生卡位证据')
                # Keep every legacy pixel producer on the exact reference resize;
                # only the occupancy fallback reads this same capture's native copy.
                current.native_frame=native.copy()
                current.capture_provenance={
                    'source':'ReferenceTask.native_frame','metadata':metadata,
                    'native_size':list(native.shape[1::-1]),'reference_size':list(reference.shape[1::-1]),
                    'native_frame_sha256':hashlib.sha256(native.tobytes()).hexdigest(),
                    'reference_frame_sha256':hashlib.sha256(reference.tobytes()).hexdigest(),
                    'observation_epoch':current.observation_epoch,
                    'recognition_frame_sha256':current.frame_hash}
            self.cached_frame=self.f.last_frame
            self.cached_screen=current
            if self.etching_pending and self.cached_screen.kind=='result' and not self.etch_result_seen:
                self.etch_result_seen=True
                self.f.emit('craft_etch_transition',phase='result_observed',page='result')
        if full:
            ensure=getattr(self.cached_screen,'ensure_full',None)
            if ensure:ensure()
        return self.cached_screen

    def publish(self,message):
        data={'目标':self.target,'正常完成':self.normal,'快速结束':self.quick,
              '分类未知完成':getattr(self,'unknown_completion',0),
              '制作配置':self.policy.name,'研发阶段':getattr(getattr(self,'cached_screen',None),'kind',None),
              '当前状态':message,'记录状态':'完整' if self.record.complete else '存在未知字段；仅在证据足够时执行','已选技能':dict(self.record.skills),
              '激增次数':dict(self.record.boosts),'研发目标':[asdict(o) for o in self.record.missions]}
        if getattr(self,'continuation_scope','batch')=='current_module_only' or self.epoch_baseline is not None:
            known=getattr(self,'historical_completed',None)
            data.update(计数范围='本次恢复后新增；仅继续当前模组' if known is None else '已关联历史；仅继续当前模组',
                        历史完成数='未知' if known is None else known,
                        基线进度=(self.epoch_baseline or {}).get('progress'),
                        本次新增完成=getattr(self,'session_delta_completed',self.normal+self.quick),
                        分类未知完成=getattr(self,'unknown_completion',0))
        if getattr(self,'attribute_status',None):data['属性决策']=self.attribute_status
        if self.policy.name=='先攻':
            data['技能模式']='完美' if self.record.perfect_mode else '狗粮'
            data['模式原因']=self.record.perfect_reason
            data['金技能分类']=self.record.first_strike_gold_picks
        if crafting_local_runtime.enabled(self):
            from src.crafting_skill_state import build_local_skill_state
            skill_state=build_local_skill_state(self.record,self.policy.name)
            waiting=(not self.module_active and self.record.complete and not self.pending_action
                     and not any((self.record.local_skill_ledger,self.record.skills,self.record.gold_picks,
                                  self.record.history,self.record.factors,self.record.boosts,self.record.missions)))
            data['本地技能阶段']='WAITING_START' if waiting else skill_state.phase
            data['已确认金等级']=skill_state.confirmed_gold_total
            data['目标可用金等级']=skill_state.gold_total
            data['本地紫等级']={identity.name:level for identity,level in skill_state.purple_levels.items()}
            data['本地技能证据']=('待启动：真实实验启动确认后建立可信账本' if waiting else
                                  '可信' if skill_state.trusted else 'UNKNOWN：'+skill_state.reason)
        status.clear();status.update(data)
        self.f.results['MODULE_CRAFT']=data
        for key,value in data.items():self.f.task.info_set(key,str(value))
        self.save_record(self.f.logdir/'crafting.json',{'status':data,'record':asdict(self.record),
                                                      'completion_ledger':self.completion_ledger(),
                                                      **self.recovery_metadata()})
        self.f.emit('crafting',**data)

    def save_record(self,path,data):
        try:
            temp=path.with_suffix('.tmp')
            temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            temp.replace(path)
            return True
        except OSError:
            self.f.task.info_set('记录状态','记录保存失败，制作继续')
            return False

    def checkpoint_action(self):
        """A durable intent is required before any irreversible confirmation."""
        data={'status':{'目标':self.target,'正常完成':self.normal,'快速结束':self.quick,
                        '分类未知完成':getattr(self,'unknown_completion',0)},
              'record':asdict(self.record),'safe':not self.uncertain,'fast':self.fast,
              'pending_action':self.pending_action,'refresh_state':self.refresh_state,
              'hub_step':self.hub_step,'completion_kind':self.completion_kind,
              'normal_origin_known':getattr(self,'normal_origin_known',False),
              'completion_ledger':self.completion_ledger(),
              'etching_pending':self.etching_pending,'etch_result_seen':self.etch_result_seen,
              'observation':self.observation_data(self.screen(full=False) if getattr(self,'_fast_choice',None) else self.screen()),**self.recovery_metadata()}
        if not self.save_record(self.f.logdir/'resume.json',data):
            raise SafetyStop('确认意图无法落盘，未发送确认')

    def recovery_metadata(self):
        return {'checkpoint_session_id':getattr(self,'checkpoint_session_id',None),
                'hub_baseline':getattr(self,'hub_baseline',None),
                'rebaseline':getattr(self,'rebaseline',None),
                'refresh_accounting':{'version':1,'reserved_total':getattr(self,'refresh_spent',None)},
                'acceptance_context':self.acceptance_evidence.context()}

    def completion_ledger(self):
        if not self.accounting_trusted:raise SafetyStop('完成账本尚未关联，禁止保存猜测计数')
        ledger={'version':1,'run_id':self.run_id,'profile':self.policy.name,'target':self.target,
                'normal':self.normal,'quick':self.quick,'source':self.completion_source,
                'module_active':self.module_active}
        if getattr(self,'continuation_scope','batch')=='current_module_only':
            ledger.update(unknown=getattr(self,'unknown_completion',0),
                          session_delta_completed=getattr(self,'session_delta_completed',0),
                          continuation_scope='current_module_only')
            if getattr(self,'historical_completed',None) is None:
                ledger.update(version=3,source='page_driven_current',baseline=self.epoch_baseline,
                              baseline_progress=(self.epoch_baseline or {}).get('progress'),historical_completed=None)
            else:ledger['historical_completed']=self.historical_completed
        elif self.epoch_baseline is not None:
            ledger.update(version=2,source='trusted_current_state',baseline=self.epoch_baseline,
                          baseline_progress=self.epoch_baseline['progress'],historical_completed=None,
                          session_delta_completed=self.normal+self.quick,continuation_scope='current_module_only')
        return ledger

    def observation_data(self,screen):
        """Stable fields exclude dialogue, score decoration and OCR ordering noise."""
        choice=getattr(self,'_fast_choice',None)
        if screen.__dict__.get('_local_only') and choice and screen.kind in ('attribute','skill'):
            option=choice['option']
            return {'kind':screen.kind,'text':screen.text,'local_only':True,
                'identity':json.dumps((screen.kind,asdict(option)),ensure_ascii=False),
                'selected_choice':asdict(option),'capture':getattr(screen,'capture_provenance',None)}
        identity=None
        if screen.kind in CHOICES and hasattr(screen,'options'):
            try:identity=self.signature(screen)
            except ChoiceUnreadable:pass  # Preserve raw failure evidence without masking the original stop.
        elif screen.kind=='hub' and hasattr(screen,'progress') and screen.progress() is not None:
            identity=('hub',screen.progress(),tuple(screen.visible_missions()))
        data={'kind':screen.kind,'text':screen.text,
              'identity':json.dumps(identity,ensure_ascii=False) if identity is not None else None}
        capture=getattr(screen,'capture_provenance',None)
        if isinstance(capture,dict):data['capture']=capture
        if screen.kind=='attribute' and identity is not None:
            data['options']=[asdict(o) for o in screen.options()]
        return data

    def wait(self,predicate,label,full=True):
        self.verified_frame=None
        self.verified_at=None
        self.verified_revision=None
        def observe():
            try:
                current=self.screen() if full else self.screen(full=False)
                if (isinstance(current,_OBSERVED_SCREEN_TYPE) and
                        getattr(current,'_full',True) is not False and
                        not getattr(current,'_lazy_pending',False) and
                        not getattr(current,'_light_conflict',False) and current.kind=='unknown' and
                        any(label in current.header for label in ('超频失败','超频未触发'))):
                    self.observe_skill_opportunities(current,phase='wait_observed')
                return predicate(current)
            except SkillIdentityChanged as error:
                if self.uncertain:
                    raise SafetyStop('技能身份已变化且旧输入已发送，结果未知，未重放') from error
                raise
            except ChoiceUnreadable as error:
                self.f.task.info_set('当前状态',label+'：正在重新读取')
                return False
        result=self.f.wait(observe,label)
        current=getattr(self,'cached_screen',None)
        complete_fallback=(isinstance(current,_OBSERVED_SCREEN_TYPE) and
            'boxes' in current.__dict__ and not current.__dict__.get('_lazy_pending')
            and not current.__dict__.get('_light_conflict') and current.kind=='hub')
        if full or complete_fallback:
            self.verified_frame=getattr(self.f,'last_frame',None)
            self.verified_at=time.monotonic()
            self.verified_revision=getattr(self.f.task,'observed_revision',None)
        return result

    def wait_fast(self,predicate,label):
        """At most three local render probes, then the existing full recovery.

        Two fresh successes remain mandatory; a partial observation never
        certifies a strategy decision or a resource-spending confirmation.
        """
        self.verified_frame=None;self.verified_at=None;self.verified_revision=None
        consecutive=0
        for attempt in range(3):
            self.f.checkpoint();self.f.refresh()
            current=self.screen(full=False)
            skill=(getattr(self,'_fast_choice',None) or {}).get('kind')=='skill'
            if skill and current.kind!='skill':
                raise SafetyStop('技能输入前页面变化，未沿用旧目标')
            if not skill and not current.__dict__.get('_local_only'):
                self.f.emit('craft_fast_exit',step=label,reason='full_observation_required')
                return self.wait(predicate,label)
            try:value=predicate(current)
            except SkillIdentityChanged:raise
            except ChoiceUnreadable:value=False
            if skill and not value and current.__dict__.get('target_evidence')=='insufficient':
                raise SkillEvidenceInsufficient('技能输入前完整身份仍不足，未发送旧目标')
            consecutive=consecutive+1 if value else 0
            if value and getattr(self,'_fast_choice',None):self._fast_choice['previous']=current
            if consecutive>=2:
                self.verified_frame=self.f.last_frame;self.verified_at=time.monotonic()
                self.verified_revision=getattr(self.f.task,'observed_revision',None)
                self.f.emit('verified',step=label,attempt=attempt+1,local_only=True)
                return value
            if attempt<2:self.f.delay(.04)
        self.f.emit('craft_fast_exit',step=label,reason='selection_not_ready_in_three_probes')
        if skill:raise SafetyStop('技能输入前连续三次未取得有效目标，未发送输入')
        return self.wait(predicate,label)

    def wait_selection(self,predicate,label,full=False):
        """Bound reversible selection observations independently of workflow waits.

        A fresh selected-card frame after parking is enough to confirm. Proof
        that nothing was selected still needs two observations before a retry.
        """
        self.verified_frame=None;self.verified_at=None;self.verified_revision=None
        clock=getattr(self.f,'clock',time.monotonic)
        deadline=clock()+3.;consecutive=0
        required=1 if label.startswith('选择 ') else 2
        identity_error=None
        for attempt in range(8):
            if attempt and clock()>=deadline:break
            self.f.checkpoint();self.f.refresh()
            current=self.screen() if full else self.screen(full=False)
            try:value=predicate(current)
            except SkillIdentityChanged as error:
                # The game's click ripple briefly covers the selected title.
                # Read fresh frames only; never reselect or confirm a mismatch.
                if not (label.startswith('选择 ') and getattr(self,'last_input_status',None)=='native_submitted'):
                    raise
                identity_error=error;value=False
                self.f.emit('craft_selected_identity_observation_retry',attempt=attempt+1,
                            reason=str(error),input_replayed=False)
            except ChoiceUnreadable:value=False
            consecutive=consecutive+1 if value else 0
            if consecutive>=required:
                self.verified_frame=self.f.last_frame;self.verified_at=time.monotonic()
                self.verified_revision=getattr(self.f.task,'observed_revision',None)
                if getattr(self,'_fast_choice',None):self._fast_choice['previous']=current
                self.f.emit('verified',step=label,attempt=attempt+1,bounded_selection=True)
                return value
            if attempt<7 and clock()<deadline:self.f.delay(.12)
        self.f.emit('craft_selection_observation_timeout',step=label,probes=attempt+1,
                    budget_seconds=3.,input_replayed=False)
        if identity_error is not None:raise identity_error
        raise RecognitionTimeout(label+'：有界观察未取得连续选中证据')

    def execution_phase(self,phase,**facts):
        info=getattr(getattr(self.f,'task',None),'info_set',None)
        if callable(info):info('执行阶段',phase)
        emit=getattr(self.f,'emit',None)
        if callable(emit):emit('craft_execution_phase',phase=phase,action_id=getattr(self,'acceptance_action_id',None),
                    transaction_id=getattr(self,'selection_transaction_id',None),
                    attempt_id=getattr(self,'input_attempt_id',None),monotonic_ns=time.perf_counter_ns(),**facts)

    def wait_transition(self,predicate,label):
        """A freshly recognized successor is enough; animations use normal retry."""
        self.verified_frame=None;self.verified_at=None;self.verified_revision=None
        self.f.checkpoint();self.f.refresh()
        current=self.screen()
        try:value=predicate(current)
        except ChoiceUnreadable:value=False
        if not value:return self.wait(predicate,label)
        self.verified_frame=self.f.last_frame;self.verified_at=time.monotonic()
        self.verified_revision=getattr(self.f.task,'observed_revision',None)
        self.f.emit('verified',step=label,attempt=1,fresh_successor=True)
        return value

    def mark_manual_intervention(self,reason):
        """Acceptance callers must mark known physical assistance explicitly."""
        self.manual_intervention=True
        self.f.emit('craft_manual_intervention',reason=reason,
                    transaction_id=getattr(self,'selection_transaction_id',None),
                    monotonic_ns=time.perf_counter_ns())

    def manual_input_evidence(self):
        task=getattr(self.f,'task',None)
        started=getattr(self,'selection_started_ns',None)
        events=getattr(task,'craft_manual_inputs',None)
        monitor=getattr(task,'craft_manual_monitor',None)
        monitor_object=getattr(task,'craft_manual_input_monitor',None)
        available=bool(getattr(monitor_object,'available',False))
        observed=[e for e in events if started is not None and e['monotonic_ns']>=started] if isinstance(events,list) else []
        manual=True if observed or getattr(self,'manual_intervention',None) is True else False if monitor=='active' and available else None
        return {'manual_intervention':manual,'manual_input_events':observed,'manual_input_monitor':monitor,
                'manual_input_monitor_available':available,
                'unattended_success':False if manual else None}

    def _current_verified(self):
        """A proof belongs to one unchanged packet, revision and short lifetime."""
        from src.resolution import ReferenceTask
        f=getattr(self,'f',None);task=getattr(f,'task',None)
        if not isinstance(task,ReferenceTask):return False
        control=getattr(task,'task_control',None) or getattr(task,'roguelike_control',None)
        revision=getattr(task,'observed_revision',None)
        at=getattr(self,'verified_at',None);native=getattr(task,'native_frame',None)
        valid=(not self.uncertain and isinstance(task,ReferenceTask)
            and getattr(native,'shape',None)==(720,1280,3) and type(at) is float
            and 0<=time.monotonic()-at<=1 and getattr(self,'verified_frame',None) is not None
            and self.verified_frame is getattr(f,'last_frame',None)
            and getattr(self,'cached_frame',None) is self.verified_frame
            and (not control or (type(revision) is int
                and getattr(self,'verified_revision',None)==revision==control.revision
                and control.state=='RUNNING')))
        if valid:
            import numpy as np
            capture=getattr(self.cached_screen,'capture_provenance',None) or {}
            valid=(np.array_equal(native,getattr(self.cached_screen,'native_frame',None))
                and capture.get('metadata')==getattr(task,'metadata',None))
        return bool(valid)

    def wait_current(self,predicate,label,full=True):
        """Reuse complete observed pixels while their packet/revision stays valid."""
        if self._current_verified():
            self.f.checkpoint()
        if self._current_verified():
            try:value=predicate(self.screen() if full else self.screen(full=False))
            except SkillIdentityChanged:raise
            except ChoiceUnreadable:value=False
            if value and self._current_verified():
                self.f.emit('craft_observation_reused',step=label,
                    observation_epoch=getattr(self.cached_screen,'observation_epoch',None),
                    verified_age_ms=(time.monotonic()-self.verified_at)*1000,
                    proof_clock_renewed=False)
                return value
        return self.wait(predicate,label) if full else self.wait(predicate,label,full=False)

    def input(self,kind,locate,after,label):
        self.acceptance_action_id=str(uuid4())
        self.input_attempt_id=str(uuid4())
        self.last_input_status='not_attempted'
        self.acceptance_evidence.event('action_started',getattr(self,'cached_screen',None),
            snapshot=False,kind=kind,label=label)
        try:return self._input_guarded(kind,locate,after,label)
        except BaseException as error:
            self.acceptance_evidence.event('action_unresolved',getattr(self,'cached_screen',None),
                kind=kind,label=label,error_type=type(error).__name__,reason=str(error))
            raise

    def _input_guarded(self,kind,locate,after,label):
        from src.cooperative import FocusChanged
        from src.roguelike_control import Reobserve
        for attempt in range(3):
            try:return self._input_once(kind,locate,after,label)
            except (FocusChanged,Reobserve) as error:
                if getattr(self,'last_input_status',None)=='partial':
                    self.uncertain=True
                    raise SafetyStop('输入部分发送后焦点或暂停状态变化，停止并保留未知结果') from error
                if isinstance(error,Reobserve):self.f.checkpoint()
                if self.uncertain:
                    # A sent input is only observed, never replayed after reacquisition.
                    self.f.task.before_click()
                    if kind in CHOICES and label.startswith('选择 '):
                        self.wait_selection(after,label+' 焦点恢复后验证')
                    else:self.wait(after,label+' 焦点恢复后验证')
                    self.uncertain=False
                    self.execution_phase('游戏选中验证' if label.startswith('选择 ') else
                        '确认结果验证' if label=='确认' else '下一节点验证',kind=kind,label=label,focus_recovered=True)
                    self.acceptance_evidence.event('action_verified',self.cached_screen,
                        snapshot=False,kind=kind,label=label,focus_recovered=True)
                    return
                if attempt==2:
                    if isinstance(error,Reobserve):
                        raise SafetyStop('制作恢复期间画面连续变化，未提交输入') from error
                    raise
                self.last_input_status='not_sent'
                self.execution_phase('明确未发送',kind=kind,label=label,reason=str(error),
                    rejected_old_coordinate=True)
                self.f.emit('craft_resume_reobserve' if isinstance(error,Reobserve) else 'craft_focus_reobserve',
                            step=label,attempt=attempt+1)
                self.f.task.before_click()

    def _input_once(self,kind,locate,after,label):
        f=self.f
        self.input_attempt_id=str(uuid4())
        self.execution_phase('输入前验证',kind=kind,label=label)
        local=bool(getattr(self,'_fast_choice',None) and kind in ('attribute','skill')
            and self._fast_choice['kind']==kind and label!='刷新' and
            (getattr(self,'refresh_state',None) or {}).get('phase') not in ('opened','confirming','uncertain'))
        navigation=bool(kind=='hub' and getattr(self,'_fast_navigation',None) and
            self._fast_navigation['label']==label and not self.uncertain and
            not getattr(self,'pending_action',None) and not getattr(self,'refresh_state',None))
        local=local or navigation
        reuse=self._current_verified()
        if reuse:
            try:
                current=self.screen(full=False) if local else self.screen()
                reuse=current.kind==kind and bool(locate(current))
            except SkillIdentityChanged:raise
            except ChoiceUnreadable:reuse=False
        decided_selection=(kind in CHOICES and label.startswith('选择 ') and
                           (getattr(self,'pending_action',None) or {}).get('phase')=='selecting')
        if not reuse and not decided_selection:
            if local:self.wait_fast(lambda s:s.kind==kind and locate(s),label+' 点击前')
            else:self.wait(lambda s:s.kind==kind and locate(s),label+' 点击前')
        if f.cfg['step_mode']:
            f.emit('step_pause',step=label,reason='existing_step_mode_pause')
            f.task.log_info('单步暂停：'+label,notify=True);f.task.pause();f.delay(3)
        f.task.before_click()
        local_skill_confirm=(kind=='skill' and label=='确认' and crafting_local_runtime.enabled(self))
        immediate_confirm=(label=='确认' and kind in CHOICES and reuse and self._current_verified()
                           and not local_skill_confirm)
        if not immediate_confirm:f.refresh()
        s=self.screen(full=False) if local else self.screen()
        if navigation and not s.__dict__.get('_local_only'):
            from src.crafting_fast import FastPathExit
            raise FastPathExit('已验证主界面像素变化，退出连续确认并重新观察')
        if s.kind!=kind:raise SafetyStop('点击前制作页面变化，停止操作')
        if kind=='prepared' and label=='实验启动':
            self.verify_start_observation(s)
        area=locate(s)
        if not area and s.__dict__.get('target_evidence')=='insufficient':
            # The same-frame full check was inconclusive; at most two new full
            # captures may recover evidence. Never retry a proven mismatch.
            for _ in range(2):
                f.checkpoint();f.delay(.08);f.refresh();s=self.screen()
                if s.kind!=kind:break
                area=locate(s)
                if area:break
                if s.__dict__.get('target_evidence')=='identity_changed':break
        if s.kind!=kind:raise SafetyStop('点击前完整复核页面变化，停止操作')
        if not area and kind=='skill' and s.__dict__.get('target_evidence')=='insufficient':
            raise SkillEvidenceInsufficient('技能输入前有限新鲜观察仍缺完整身份，未发送旧目标')
        if not area:raise SafetyStop('点击前选项或按钮变化，停止操作')
        x,y,x2,y2=area
        f.task.assert_foreground();f.checkpoint();f.task.validate_click_context()
        f.snapshot('craft_before_click')
        self.acceptance_evidence.event('input_ready',s,options=s.__dict__.get('_options'),
            kind=kind,label=label,attempt_id=self.input_attempt_id,
            transaction_id=getattr(self,'selection_transaction_id',None))
        if local_skill_confirm:crafting_local_runtime.prepare_confirmation(self,s)
        click_x,click_y=round((x+x2)*.9375),round((y+y2)*.9375)
        f.task.move(click_x,click_y)
        if not local and kind not in CHOICES:f.delay(.15)
        f.task.assert_foreground();f.checkpoint();f.task.validate_click_context()
        f.emit('click',step=label,x=click_x,y=click_y)
        if label=='确认' and kind in ('attribute','skill'):self._last_choice_confirm_area=tuple(area)
        def submit():
            # The pause guard must reject a stale point before reserving an
            # uncertain submission. Once this callback starts, only observe
            # the outcome; a backend exception can never authorize replay.
            self.uncertain=True
            dispatched_at=time.perf_counter_ns();returned=False;receipt=None
            try:
                result=f.task.click(click_x,click_y,after_sleep=0,down_time=.05 if local else .15)
                returned=True
                receipt=result.as_dict() if callable(getattr(result,'as_dict',None)) else None
                self.last_input_status=receipt.get('status','unknown') if receipt else 'unknown'
                phase={'not_sent':'明确未发送','partial':'部分发送','unknown':'发送结果未知',
                    'native_submitted':'原生提交完成'}.get(self.last_input_status,'发送结果未知')
                self.execution_phase(phase,kind=kind,label=label,receipt=receipt,
                    native_submission_is_game_acceptance=False)
                if self.last_input_status=='not_sent':
                    self.uncertain=False
                    raise RecognitionTimeout('输入明确未发送：'+label)
                if self.last_input_status=='partial':raise SafetyStop('输入部分发送，停止并保留未知结果：'+label)
                return result
            except BaseException as error:
                detail=getattr(error,'input_receipt',None)
                if detail is not None:receipt=detail.as_dict()
                if not returned:self.last_input_status=(receipt or {}).get('status','unknown')
                if self.last_input_status=='not_sent':self.uncertain=False
                if not returned:self.execution_phase('部分发送' if self.last_input_status=='partial' else
                    '明确未发送' if self.last_input_status=='not_sent' else '发送结果未知',
                    kind=kind,label=label,receipt=receipt,native_submission_is_game_acceptance=False)
                raise
            finally:
                if local_skill_confirm:crafting_local_runtime.submitted(self,dispatched_at)
                self.acceptance_evidence.event('input_submission',s,snapshot=False,kind=kind,label=label,
                    dispatch_started_ns=dispatched_at,backend_returned=returned,
                    attempt_id=getattr(self,'input_attempt_id',None),receipt=receipt,
                    send_status=getattr(self,'last_input_status','unknown'),
                    native_submission_is_game_acceptance=False)
        control=getattr(f.task,'task_control',None) or getattr(f.task,'roguelike_control',None)
        revision=getattr(f.task,'observed_revision',None)
        if control and isinstance(revision,int):control.input(revision,submit)
        else:submit()
        if not local:f.delay(.15)
        f.task.after_input()
        if kind in CHOICES and label.startswith('选择 '):
            # A fast click can still be awaiting the game's next input update.
            # Observe a genuinely new post-submit frame before parking the
            # pointer; otherwise Unity can process release at the parked point.
            # Selection is still verified AFTER parking, so hover is not proof.
            f.refresh()
            f.checkpoint();f.task.assert_foreground();f.task.validate_click_context()
            f.task.move(3800,1750)
        # Observe the outcome immediately. A selected frame or recognized
        # successor completes the action; absent evidence remains bounded.
        if kind in CHOICES and label.startswith('选择 '):self.wait_selection(after,label+' 结果',full=not local)
        elif label=='确认' and kind in CHOICES|{'refresh_confirm'}:self.wait_transition(after,label+' 结果')
        else:self.wait(after,label+' 结果')
        self.uncertain=False
        self.execution_phase('游戏选中验证' if label.startswith('选择 ') else
            '确认结果验证' if label=='确认' else '下一节点验证',kind=kind,label=label)
        self.acceptance_evidence.event('action_verified',getattr(self,'cached_screen',None),
            snapshot=getattr(self,'cached_screen',None).__dict__.get('kind') in ('overclock','unknown','result')
                if getattr(self,'cached_screen',None) is not None else False,kind=kind,label=label,
                attempt_id=getattr(self,'input_attempt_id',None),
                transaction_id=getattr(self,'selection_transaction_id',None),
                **self.manual_input_evidence())

    def button(self,kind,label,after,area=(1450,960,2015,1100)):
        self.input(kind,lambda s:s.button(label,area),after,label)

    def signature(self,s):
        if s.kind in CHOICES:
            return (s.kind,tuple((o.name,o.group,tuple(o.groups),o.attr,o.boost,o.value,o.level,o.gain,o.goal)+
                                 ((o.module,o.rarity) if s.kind in ('skill','core_skill') else ())
                                 for o in s.options()))
        return (s.kind,s.text)

    def refresh(self,kind):
        if kind=='skill' and crafting_local_runtime.enabled(self):
            crafting_local_runtime.preflight_skill_node(self)
        state=getattr(self,'refresh_state',None) or {}
        if state.get('phase') in ('confirming','uncertain'):
            raise SafetyStop('刷新确认结果不确定，禁止重放资源确认')
        if state.get('kind')==kind and state.get('phase') in ('refreshed','exhausted'):
            return False
        if state.get('phase')=='opened':
            if state.get('kind')!=kind:raise SafetyStop('已打开刷新弹窗的节点来源不一致，未继续刷新')
            return self.confirm_refresh(kind)
        s=self.screen()
        area=s.button('刷新',(1250,970,1530,1090))
        if not area or not s.enabled(area):return False
        import cv2
        cv2.imencode('.png',self.f.last_frame)[1].tofile(str(self.f.logdir/'refresh-before.png'))
        self.refresh_state={'kind':kind,'phase':'opened'}
        self.button(kind,'刷新',lambda s:s.kind=='refresh_confirm',(1250,970,1530,1090))
        return self.confirm_refresh(kind)

    def cancel_refresh(self,kind):
        """Close a proven, unconfirmed refresh dialog without buying a refresh."""
        screen=self.screen()
        locate=getattr(screen,'button',None)
        if not callable(locate) or locate('取消',(630,750,1410,850)):
            return self.button('refresh_confirm','取消',lambda s:s.kind==kind,(630,750,1410,850))
        raise SafetyStop('刷新弹窗没有可验证的取消入口；保留现场且未确认消耗。后续应在打开前检查预算。')

    def confirm_refresh(self,kind):
        if kind=='skill' and crafting_local_runtime.enabled(self):
            crafting_local_runtime.preflight_skill_node(self)
        if (getattr(self,'refresh_state',None) or {}).get('phase') in ('confirming','uncertain'):
            raise SafetyStop('刷新确认结果不确定，禁止重放资源确认')
        if (getattr(self,'refresh_state',None) or {}).get('phase') in ('refreshed','exhausted'):
            raise SafetyStop('当前节点刷新已用或已耗尽，未重复确认')
        if ((getattr(self,'refresh_state',None) or {}).get('phase')=='opened'
                and self.refresh_state.get('kind')!=kind):
            raise SafetyStop('已打开刷新弹窗的节点来源不一致，未发送确认')
        current=self.screen()
        if not (self._current_verified() and current.kind=='refresh_confirm' and current.refresh_amounts() is not None):
            self.wait(lambda s:s.kind=='refresh_confirm' and s.refresh_amounts() is not None,'重新读取刷新次数与消耗')
        amounts=self.screen().refresh_amounts()
        if amounts is None:raise ChoiceUnreadable('刷新次数或消耗尚未确认，未发送确认')
        remaining,total,balance,cost=amounts
        if (remaining,total)==(0,1):
            if kind!='skill':raise SafetyStop('当前节点没有可用刷新次数')
            # A proven zero authorizes cancellation only. Keep the original
            # reservation/history until the fresh skill page is verified.
            def cancel(current):
                if current.refresh_amounts()!=amounts:return None
                return current.button('取消',(630,750,1410,850))
            self.input('refresh_confirm',cancel,lambda now:now.kind==kind,'取消')
            self.refresh_state={'kind':kind,'phase':'exhausted','remaining_before':0,
                                'confirmed_count':0}
            self.f.emit('craft_refresh_exhausted',**self.refresh_state,resource_confirmation_sent=False)
            self.checkpoint_action()
            return False
        if (remaining,total)!=(1,1):raise ChoiceUnreadable('刷新次数状态未确认，当前次数读数不支持单次刷新')
        if cost<=0 or balance<cost:raise SafetyStop('刷新资源不足，停止制作')
        budget=getattr(self.f.task,'test_refresh_budget',None)
        if budget is not None and (type(budget) is not int or budget<0):
            raise SafetyStop('实机刷新资源上限无效，未发送确认')
        if budget is not None and (type(self.refresh_spent) is not int or self.refresh_spent<0):
            self.cancel_refresh(kind)
            raise SafetyStop('历史刷新累计消耗未知，无法证明实机预算；已取消本次刷新')
        if budget is not None and self.refresh_spent+cost>budget:
            self.cancel_refresh(kind)
            raise SafetyStop('达到本次实机测试刷新资源上限，已取消本次刷新')
        self.refresh_state={'kind':kind,'phase':'confirming','remaining_before':remaining,
                            'balance_before':balance,'cost':cost,'confirmed_count':0}
        # Reserve once before writing the intent. Unknown input outcomes retain
        # this reservation across restart, so a continuation cannot reset budget.
        if type(self.refresh_spent) is int:self.refresh_spent+=cost
        self.checkpoint_action()
        def locate(current):
            # input() captures again immediately before dispatch. A changed
            # count, balance or cost cannot reuse the earlier spending decision.
            if current.refresh_amounts()!=amounts:return None
            return current.button('确认',(630,750,1410,850))
        self.input('refresh_confirm',locate,
                   lambda s:s.kind==kind and not s.enabled((1370,1005,1505,1060)),'确认')
        self.refresh_state.update(phase='refreshed',confirmed_count=1)
        self.f.emit('craft_refresh_verified',**self.refresh_state)
        self.checkpoint_action()
        return True

    def choose(self,s,refreshed):
        if s.kind=='mission':return self.choose_mission(s)
        if s.kind=='attribute':
            from src.crafting_decision import AttributeObservation
            if isinstance(s,_OBSERVED_SCREEN_TYPE):s._defer_attribute_scores=True
            observation=s.attribute_observation() if hasattr(s,'attribute_observation') else AttributeObservation(tuple(s.options()),None,not refreshed)
            return self.choose_attribute(s,observation,refreshed)
        if s.kind=='skill':
            return self.choose_season_skill(s,refreshed)
        try:options=s.options()
        except ChoiceUnreadable:
            if s.kind not in ('core_attribute','core_skill'):raise
            options=[]
        r=self.record
        if s.kind=='skill':return self.policy.skill(options,r,refreshed)
        if s.kind=='factor':
            names=self._factor_names(s,options)
            required=set(FACTORS)-{self._factor_name(name) for name in self.record.factors}
            accepted=[o for o,name in zip(options,names) if name in (required or set(FACTORS))]
            return (accepted[0],'因子硬条件：声东击西＋好整以暇') if accepted else (None,'因子已歪，必须左上退出')
        if s.kind=='rest':return max(options,key=lambda o:o.value),'选择剩余节点最多的休整'
        if s.kind=='core_attribute':
            epochs=[getattr(s,'observation_epoch',None)]
            gold=[o for o in options if o.rarity==3]
            while not gold:
                s=self.fresh_evidence(s,epochs,{'missing_field':'gold_candidate','target_slot':None,
                    'producer_plan':('current_card_rarity',),'action_dependency':'战术核心属性必须识别金色候选'})
                if s.kind!='core_attribute':return None,'核心证据恢复时页面变化，继续当前页面'
                try:options=s.options()
                except ChoiceUnreadable:options=[]
                gold=[o for o in options if o.rarity==3]
            return max(gold,key=lambda o:o.score),'战术核心属性选择金色'
        if s.kind=='core_skill':
            epochs=[getattr(s,'observation_epoch',None)]
            while True:
                gold=[o for o in options if o.rarity==3]
                missing=None;slot=None
                if {o.index for o in options}!={0,1,2}:missing='core_skill_slots'
                elif any(not o.rarity_known for o in options):
                    missing='rarity';slot=next(o.index for o in options if not o.rarity_known)
                elif len(gold)!=1:missing='gold_candidate'
                elif gold[0].visual_enabled is None:missing='visual_enabled';slot=gold[0].index
                elif gold[0].visual_enabled is True:
                    return gold[0],'战术核心技能选择当前可选的唯一金色候选'
                else:
                    others=[o for o in options if o is not gold[0]]
                    unknown=[o for o in others if o.visual_enabled is None]
                    eligible=[o for o in others if o.visual_enabled is True]
                    if unknown:missing='visual_enabled';slot=unknown[0].index
                    elif not eligible:raise SafetyStop('当前无可选择的战术核心技能')
                    else:
                        unread=[o for o in eligible if not o.score_known]
                        if not unread:
                            return max(eligible,key=lambda o:(o.score,-o.index)),'金色不可选，选择当前可选候选中效能最高者'
                        missing='score';slot=unread[0].index
                s=self.fresh_evidence(s,epochs,{'missing_field':missing,'target_slot':slot,
                    'producer_plan':('core_skill_card_observation',),'action_dependency':'战术核心技能当前可选状态与效能'})
                if s.kind!='core_skill':return None,'核心技能证据恢复时页面变化，继续当前页面'
                try:options=s.options()
                except ChoiceUnreadable:options=[]
        raise SafetyStop('不支持的选择页面')

    def choose_season_skill(self,s,refreshed):
        epochs=[getattr(s,'observation_epoch',None)]
        while True:
            try:
                if crafting_local_runtime.enabled(self):
                    try:crafting_local_runtime.preflight_skill_node(self)
                    except SafetyStop as error:raise ChoiceUnreadable(str(error)) from error
                options=s.options();kwargs={}
                if hasattr(s,'skill_cards'):
                    cards=s.skill_cards()
                    self.acceptance_evidence.event('skill_candidates',s,options=options,cards=cards)
                    if cards.get('geometry_known') is not True or cards.get('missing'):
                        raise ChoiceUnreadable('技能占位与候选标题未读全，不能推断目标缺席或改变制作模式')
                if self.policy.name=='先攻':
                    if isinstance(s,_OBSERVED_SCREEN_TYPE):
                        self.observe_skill_opportunities(s)
                        from src.crafting_opportunities import opportunities
                        chances=opportunities(self.record,s,options)
                        self.acceptance_evidence.event('opportunities_observed',s,options=options,
                            reason='existing_production_opportunity_provider',capacity=asdict(chances))
                        if not chances.confirmed:
                            raise ChoiceUnreadable('当前技能机会缺少未消费的同源证据，未重复计算或提交')
                    else:
                        # Pure-policy adapters cannot stand in for production
                        # captured-page evidence or authorize a live tail bound.
                        chances=s.skill_opportunities(options) if hasattr(s,'skill_opportunities') else None
                    from src.crafting_policy import SkillOpportunities
                    if chances is None:
                        chances=SkillOpportunities((max(o.gain for o in options),),future_known=False)
                    evidence={key:list(value) if isinstance(value,tuple) else value
                              for key,value in asdict(chances).items()}
                    history=self.record.skill_opportunity_history
                    if not history or history[-1]['opportunities']!=evidence:
                        observed={'opportunities':evidence,'hub_step':self.hub_step,
                                  'observation_epoch':getattr(s,'observation_epoch',None)}
                        history.append(observed)
                        del history[:-64]
                        if hasattr(self.f,'emit'):self.f.emit('craft_skill_opportunities',**observed)
                    kwargs['opportunities']=chances
                    # Refresh has its own current-page evidence. Attribute
                    # refresh preferences are never inputs to this policy.
                    if refreshed:
                        available=False
                    elif isinstance(s,_OBSERVED_SCREEN_TYPE):
                        area=s.button('刷新',(1250,970,1530,1090))
                        available=s.enabled(area) if area else None
                    elif hasattr(s,'refresh_available'):
                        available=s.refresh_available
                    elif hasattr(s,'button'):
                        area=s.button('刷新',(1250,970,1530,1090))
                        available=s.enabled(area) if area and hasattr(s,'enabled') else None
                    else:available=True  # Pure policy observations never authorize live input.
                    kwargs['refresh_available']=available
                elif crafting_local_runtime.enabled(self):
                    if refreshed:available=False
                    elif hasattr(s,'refresh_available'):available=s.refresh_available
                    else:
                        area=s.button('刷新',(1250,970,1530,1090)) if hasattr(s,'button') else None
                        available=s.enabled(area) if area and hasattr(s,'enabled') else None
                    kwargs['refresh_available']=available
                from copy import deepcopy
                trial=deepcopy(self.record)
                if crafting_local_runtime.enabled(self):
                    decision=self.policy.decide_skill(options,trial,refreshed,**kwargs)
                    choice,reason=decision.legacy(options)
                    self.f.emit('craft_local_skill_decision',**asdict(decision),
                                craft_id=trial.local_skill_ledger.get('craft_id'),
                                hub_step=getattr(self,'hub_step',None))
                else:choice,reason=self.policy.skill(options,trial,refreshed,**kwargs)
                if hasattr(choice,'name') and (not choice.level_known or not choice.rarity_known):
                    raise ChoiceUnreadable('策略首选技能的等级或品质尚未读清，未提交旧候选或模式变更')
                self.record.perfect_mode,self.record.perfect_reason=trial.perfect_mode,trial.perfect_reason
                return choice,reason
            except ChoiceUnreadable as error:
                s=self.fresh_evidence(s,epochs,{'missing_field':'season_skill','target_slot':None,
                    'producer_plan':('current_skill_cards',),'action_dependency':str(error)},'craft_skill_retry')
                if s.kind!='skill':return None,'技能证据恢复时页面变化，继续当前页面'

    def publish_attribute_decision(self,observation,decision,retry=None):
        """Publish the current decision before OCR or input so the overlay stays live."""
        data={'screen_kind':'attribute','decision_action':decision.action,
              'decision_rule':decision.rule,'decision_reason':decision.reason,
              'missing':list(decision.missing),'missing_type':decision.missing_type,
              'cards_complete':observation.cards_complete,'remaining_people':observation.remaining,
              'remaining_source':observation.count_source,'refresh_available':observation.refresh_available,
              'refresh_threshold':self.attribute_config.refresh_min_people,
              'off_target_skill':self.policy.attribute_state(self.record).off_target_skill,
              'off_target_skill_evidence':self.policy.attribute_state(self.record).off_target_skill_evidence,
              'decision_assumption':decision.decision_assumption,'history_mutated':False,
              'selected_index':decision.selected_index,
              'candidates':[dict(asdict(o),identity=o.name) for o in observation.options],
              'retry':retry or {}}
        self.attribute_status=data
        status['属性决策']=data
        status['当前状态']=decision.reason
        if hasattr(self.f,'results'):self.f.results.setdefault('MODULE_CRAFT',{}).update(status)
        task=getattr(self.f,'task',None)
        if task is not None and hasattr(task,'info_set'):
            task.info_set('当前状态',decision.action.upper()+'：'+decision.reason)
            task.info_set('属性决策',json.dumps(data,ensure_ascii=False))
        self.f.emit('craft_attribute_decision',**data)

    def fresh_evidence(self,s,epochs,retry,event='craft_visual_recovery'):
        """A bounded capture operation, shared by current visual facts only."""
        retry=dict(retry,kind='temporal_retry',producer='fresh_frame_capture',
                   fresh_frame_required=True,attempt=len(epochs),max_attempts=3,
                   previous_epoch=getattr(s,'observation_epoch',None))
        reason=''
        if len(epochs)>=3:reason='观察帧3/3仍无可信证据'
        elif not hasattr(self.f,'refresh'):reason='fresh-frame capture不可用'
        else:
            self.f.refresh();now=self.screen();frame_id=getattr(now,'observation_epoch',None)
            if now is s or not frame_id or frame_id in epochs:reason='capture未产生新observation epoch，不能重复旧帧'
        if reason:
            reason=(f'缺失字段{retry["missing_field"]}；卡位{retry.get("target_slot")}；'
                    f'producer={", ".join(retry["producer_plan"])}；{reason}；{retry["action_dependency"]}')
            self.f.emit(event+'_exhausted',**retry,observed_epochs=epochs,reason=reason)
            raise SafetyStop(reason)
        epochs.append(frame_id)
        self.f.emit(event,**dict(retry,attempt=len(epochs),observation_epoch=frame_id,
                    frame_id=getattr(now,'frame_id',frame_id),frame_hash=getattr(now,'frame_hash',None)),accepted=None)
        return now

    def choose_attribute(self,s,observation,refreshed):
        from dataclasses import replace
        epoch=1;maximum=3;used=set();observed=[]
        while True:
            frame_id=getattr(s,'observation_epoch',None)
            if frame_id not in observed:observed.append(frame_id)
            if refreshed:observation=replace(observation,refresh_available=False)
            decision=self.policy.decide_attribute(observation,self.record,self.attribute_config)
            plans=decision.retry_plan;pending=None
            # Parsing may already have tried every score producer on this frame.
            for plan in plans:
                field=plan['field'];index=plan.get('slot')
                completed={row['source'] for row in getattr(s,'_score_reads',{}).get(index,[])} if field=='score' else set()
                supported=hasattr(s,'retry_attribute_field') or (field=='candidate_identity' and hasattr(s,'retry_attribute_slot'))
                for variant,producer in enumerate(plan['producers'],1):
                    key=(epoch,field,index,producer)
                    if supported and plan['scope']=='targeted' and producer not in completed and key not in used:
                        pending=(plan,variant,producer,key);break
                if pending:break
            if decision.action=='observe':
                decision=replace(decision,retry_plan=tuple(dict(plan,attempt=epoch,
                    fresh_frame_required=pending is None,action_dependency=decision.reason) for plan in plans))
            self.last_decision=decision
            if observation.options and hasattr(s,'score_evidence'):
                for evidence in s.score_evidence():self.f.emit('craft_score_evidence',**evidence)
            self.f.emit('craft_decision',profile=self.policy.name,config=asdict(self.attribute_config),
                        observation=asdict(observation),record=asdict(self.record),decision=asdict(decision),
                        off_target_skill_evidence=self.policy.attribute_state(self.record).off_target_skill_evidence,
                        decision_assumption=decision.decision_assumption,history_mutated=False)
            if decision.action!='observe' or decision.recoverable_by_observation is not True:
                self.publish_attribute_decision(observation,decision)
            if decision.action=='stop':
                raise SafetyStop(f'{decision.reason}；缺失证据：{", ".join(decision.missing)}')
            if decision.action!='observe':
                return decision.legacy(observation.options)
            if decision.recoverable_by_observation is not True:
                raise SafetyStop('策略要求了不可通过观察获得的证据：'+decision.reason)
            if not plans:
                reason=f'缺失字段{decision.missing}未绑定可读取producer，停止等待'
                self.publish_attribute_decision(observation,replace(decision,action='stop',reason=reason))
                raise SafetyStop(reason)
            plan=pending[0] if pending else plans[0]
            field=plan['field'];index=plan.get('slot')
            retry={'retry_field':field,'missing_field':field,'targeted_slot':index,'target_slot':index,
                   'producer_plan':plan['producers'],'attempt':epoch,'max_attempts':maximum,
                   'fresh_frame_required':pending is None,'action_dependency':decision.reason,
                   'observation_epoch':frame_id,'frame_id':getattr(s,'frame_id',frame_id),
                   'frame_hash':getattr(s,'frame_hash',None)}
            self.publish_attribute_decision(observation,decision,retry)
            if pending:
                _,variant,producer,key=pending;used.add(key)
                retry.update(kind='producer_variant',producer=producer,producer_variant=variant,
                             producer_count=len(plan['producers']))
                if hasattr(s,'retry_attribute_field'):
                    evidence=s.retry_attribute_field(index,field,variant)
                else:
                    success=s.retry_attribute_slot(index,variant)
                    evidence={'success':success,'accepted':success}
                self.f.emit('craft_attribute_retry',**{**evidence,**retry})
            else:
                previous=s
                try:s=self.fresh_evidence(s,observed,retry,'craft_attribute_retry')
                except SafetyStop as error:
                    self.last_decision=replace(decision,action='stop',reason=str(error))
                    self.publish_attribute_decision(observation,self.last_decision,retry)
                    raise
                epoch=len(observed)
                if s.kind!='attribute':return None,'证据恢复时页面变化，继续识别当前页面'
                if hasattr(s,'reuse_attribute_evidence'):s.reuse_attribute_evidence(previous)
            observation=s.attribute_observation()

    def choose_mission(self,s):
        from dataclasses import replace
        epochs=[getattr(s,'observation_epoch',None)];used=set()
        while True:
            try:
                options=s.options()
            except ChoiceUnreadable:options=[]
            decision=self.policy.decide_mission(options,self.record,self.attribute_config.prioritize_fire_reward,
                                                cards_complete=len(options)==3)
            pending=None
            for plan in decision.retry_plan:
                for variant,producer in enumerate(plan['producers'],1):
                    key=(len(epochs),plan['slot'],producer)
                    if plan['slot'] is not None and hasattr(s,'retry_mission_slot') and key not in used:
                        pending=(plan,variant,producer,key);break
                if pending:break
            if decision.action=='observe':
                decision=replace(decision,retry_plan=tuple(dict(plan,attempt=len(epochs),
                    fresh_frame_required=pending is None) for plan in decision.retry_plan))
            self.last_decision=decision
            self.f.emit('craft_mission_decision',options=[asdict(o) for o in options],decision=asdict(decision))
            if decision.action=='select':return decision.legacy(options)
            if decision.action!='observe' or decision.recoverable_by_observation is not True:
                raise SafetyStop(decision.reason)
            plan=pending[0] if pending else decision.retry_plan[0]
            retry={'missing_field':plan['field'],'target_slot':plan['slot'],'producer_plan':plan['producers'],
                   'action_dependency':decision.reason,'attempt':len(epochs),'max_attempts':3,
                   'observation_epoch':getattr(s,'observation_epoch',None)}
            if pending:
                _,variant,producer,key=pending;used.add(key)
                s.retry_mission_slot(plan['slot'],variant)
                self.f.emit('craft_mission_retry',**retry,kind='producer_variant',producer=producer,
                            producer_variant=variant,producer_count=len(plan['producers']))
            else:
                s=self.fresh_evidence(s,epochs,retry,'craft_mission_retry')
                if s.kind!='mission':return None,'任务证据恢复时页面变化，继续当前页面'

    def pick(self,s,exit_option=None):
        kind=s.kind
        if (self.uncertain or (self.pending_action or {}).get('phase')=='confirming' or
                (self.refresh_state or {}).get('phase') in ('confirming','uncertain')):
            raise SafetyStop('旧确认结果未知，禁止重新选择、刷新或覆盖未确认操作')
        area=s.button('刷新',(1250,970,1530,1090))
        refreshed=bool(area and not s.enabled(area)) or (self.refresh_state or {}).get('phase') in ('refreshed','exhausted')
        self.wait_current(lambda now:now.kind!='unknown' and (kind=='mission' or now.kind!=kind or bool(now.options())),'读取当前候选')
        s=self.screen()
        self.observe_skill_opportunities(s)
        if s.kind!=kind:
            self.record.lose_history()
            return None
        # A selected border is an observation, never authority to override policy.
        if exit_option is not None:
            current=self._factor_exit_selection_option(s)
            if asdict(current)!=asdict(exit_option):raise SafetyStop('退出前置因子候选变化，未选择')
            option,reason=current,'不合格因子仅满足退出前置条件，确认后立即退出'
        else:option,reason=self.choose(s,refreshed)
        s=self.screen()
        if s.kind!=kind:
            self.record.lose_history()
            return None
        if option=='refresh':
            self.f.emit('craft_refresh_decision',kind=kind,options=[asdict(o) for o in s.options()],
                        record=asdict(self.record),reason=reason)
            self.f.snapshot('craft_refresh_decision')
            did_refresh=self.refresh(kind)
            self.wait_current(lambda now:now.kind==kind and bool(now.options()),'读取刷新后候选')
            s=self.screen()
            refresh_area=s.button('刷新',(1250,970,1530,1090))
            refreshed=did_refresh or bool(refresh_area and not s.enabled(refresh_area)) or (
                self.refresh_state or {}).get('phase') in ('refreshed','exhausted')
            option,reason=self.choose(s,refreshed)
            s=self.screen()
            if s.kind!=kind:
                self.record.lose_history()
                return None
        if option is None:
            raise SafetyStop(reason+'；有限重读后策略仍没有可执行候选')
        if not hasattr(option,'name'):raise ChoiceUnreadable('当前策略决定尚未具备可执行选项')
        self.f.emit('craft_candidates',kind=kind,options=[asdict(o) for o in s.options()],chosen=option.name,
                    chosen_index=option.index,reason=reason,record=asdict(self.record),
                    decision=asdict(self.last_decision) if getattr(self,'last_decision',None) and kind=='attribute' else None)
        self.publish(reason+'：'+option.name)
        self.selection_transaction_id=str(uuid4());self.manual_intervention=None
        self.selection_started_ns=time.perf_counter_ns()
        local_skill_proof=(crafting_local_runtime.candidate(self,s,option)
                           if kind=='skill' and crafting_local_runtime.enabled(self) else None)
        self.execution_phase('决策完成',kind=kind,index=option.index,selection_verified=False)
        if hasattr(self.f,'snapshot'):self.f.snapshot('craft_decision')
        # The strategy has finished. Only this known card may use local checks;
        # a modal, unread identity or changed page returns to full observation.
        self._fast_choice=None
        if isinstance(s,_OBSERVED_SCREEN_TYPE) and kind in ('attribute','skill') and getattr(self,'page_observation',False):
            self._fast_choice={'kind':kind,'option':option,'previous':s}
            self.f.emit('craft_fast_started',kind=kind,index=option.index)
        def locate(now):
            area=self._target_for_input(now,option) if now.kind==kind else None
            if area:
                option.area=area
                return now.choice_click(area)
            return None
        if not s.selected(option.area):
            self.pending_action={'phase':'selecting','kind':kind,'option':asdict(option),
                                 'candidates':[asdict(o) for o in s.options()],
                                 'hub_step':getattr(self,'hub_step',None)}
            if local_skill_proof:self.pending_action['local_skill_proof']=local_skill_proof
            self.checkpoint_action()
            signature=self.signature(s)
            verified=lambda now:now.kind==kind and self._target_selected(now,option) and bool(now.button('确认'))
            try:self.input(kind,locate,verified,'选择 '+option.name)
            except RecognitionTimeout:
                if getattr(self,'last_input_status','unknown') not in ('native_submitted','not_sent'):
                    raise SafetyStop('选择发送结果未知，未自动补点')
                # Only a stable identical node with no selection authorizes one retry.
                self.wait_selection(lambda now:now.kind==kind and self.signature(now)==signature and
                          not any(now.selected(o.area) for o in now.options()),'核对明确未选中',full=True)
                self.uncertain=False
                self.f.emit('craft_selection_retry',index=option.index,reason='同一节点、同一候选且连续确认未选中，补点一次')
                self.input(kind,locate,verified,'选择 '+option.name+' 补点')
        self.pending_action={'phase':'selected_verified','kind':kind,'option':asdict(option),
                             'candidates':[asdict(o) for o in s.options()],
                             'hub_step':getattr(self,'hub_step',None)}
        if local_skill_proof:self.pending_action['local_skill_proof']=local_skill_proof
        return kind,option

    def select(self,s):
        for attempt in range(3):
            prior_mode=(self.record.perfect_mode,self.record.perfect_reason)
            prior_commits=len(self.record.history)
            try:return self._select_once(s)
            except (SkillIdentityChanged,SkillEvidenceInsufficient) as error:
                if self.uncertain or len(self.record.history)!=prior_commits:
                    raise SafetyStop('技能身份已变化且旧输入已发送或已提交，结果未知，未重放') from error
                if s.kind!='skill':raise
                # Only an intent whose input has not been sent is cancelled.
                # Refresh accounting and all confirmed history remain intact.
                self.record.perfect_mode,self.record.perfect_reason=prior_mode
                self.pending_action=None
                self._fast_choice=None
                self.f.emit('craft_skill_identity_redecision',attempt=attempt+1,
                            reason=str(error),input_dispatched=False,history_committed=False,
                            evidence='identity_changed' if isinstance(error,SkillIdentityChanged) else 'insufficient')
                if attempt==2:raise SafetyStop('技能身份连续变化3次，停止并保留现场') from error
                self.wait(lambda now:now.kind!='unknown','技能身份变化后重新观察')
                s=self.screen()
                if s.kind!='skill':return False

    def _select_once(self,s):
        if s.kind in CHOICES and (self.uncertain or (self.pending_action or {}).get('phase')=='confirming' or
                (self.refresh_state or {}).get('phase') in ('confirming','uncertain')):
            raise SafetyStop('旧确认结果未知，禁止选择或刷新掩盖未确认操作')
        if (s.kind=='factor' or self._bad_factor_history()) and (self.uncertain
                or (self.pending_action or {}).get('phase')=='confirming'
                or (self.refresh_state or {}).get('phase') in ('confirming','uncertain')):
            raise SafetyStop('旧确认结果未知，禁止选择或因子退出掩盖未确认操作')
        if self._bad_factor_history():
            self.fast=True
            return True
        if s.kind=='factor':
            options=s.options();names=self._factor_names(s,options)
            required=set(FACTORS)-{self._factor_name(name) for name in self.record.factors}
            if not any(name in (required or set(FACTORS)) for name in names):
                window=getattr(self.f,'target_identity',None) or {}
                if any(type(window.get(k)) is not int or window[k]<=0 for k in ('hwnd','pid')):
                    raise SafetyStop('因子退出缺少当前游戏窗口身份，未发送输入')
                self.pending_action={'phase':'factor_rejected','kind':'factor_exit','run_id':self.run_id,
                    'profile':self.policy.name,'target':self.target,'window_identity':{k:window[k] for k in ('hwnd','pid')},
                    'factor_history':list(self.record.factors),'required':sorted(required or set(FACTORS)),
                    'candidates':[asdict(o) for o in options],'canonical_names':names,
                    'signature':json.dumps(self.signature(s),ensure_ascii=False),
                    'observation_epoch':getattr(s,'observation_epoch',None),
                    'selection_required_for_exit':not crafting_local_runtime.enabled(self)}
                self.fast=True
                self.f.emit('craft_factor_rejected',**self.pending_action,record=asdict(self.record))
                self.publish('因子硬条件未满足：直接左上快速结束' if crafting_local_runtime.enabled(self) else
                             '因子硬条件未满足：仅为退出完成当前因子确认，随后立即左上退出，不刷新、不继续研发')
                self.checkpoint_action()
                return True
        picked=self.pick(s)
        if picked is None:return False
        kind,option=picked
        self.pending_action['phase']='confirming'
        self.checkpoint_action()
        self.execution_phase('确认提交',kind=kind)
        self.input(kind,lambda now:now.button('确认') if self._target_selected(now,option) else None,
                   lambda now:now.kind in ({'hub','result','overclock'}|CHOICES)-{kind}
                       or self._next_unknown_attribute(s,now),'确认')
        previous={'skills':dict(self.record.skills),'gold_picks':self.record.gold_picks,
                  'consumed_ids':list(self.record.skill_opportunity_state.get('consumed_ids',()))}
        local_proof=None
        if kind=='skill' and crafting_local_runtime.enabled(self):
            local_proof=crafting_local_runtime.commit_verified(self,option)
        else:self.record.commit(kind,option)
        if kind=='skill' and getattr(getattr(self,'policy',None),'name',None)=='先攻':
            from src.crafting_opportunities import consume
            evidence=consume(self.record,option)
            if evidence:self.f.emit('craft_skill_opportunity_consumed',**evidence)
        if kind=='skill':
            level_after=self.record.skills.get(option.name)
            if local_proof:
                from src.crafting_skill_state import identity_for_option
                level_after=crafting_local_runtime.state_for_runner(self).levels[identity_for_option(option)]
            self.acceptance_evidence.event('skill_committed',getattr(self,'cached_screen',None),
                previous=previous,chosen=asdict(option),level_after=level_after,
                level_change_source=local_proof['source'] if local_proof else 'verified_confirmation_then_existing_commit',
                gold_count_rule=('sum_of_confirmed_identity_levels; idempotent_node_transaction' if local_proof else
                                 'existing_sum_of_selected_gold_gains; not independently_game_verified'))
        self.pending_action=None
        self.refresh_state=None
        self._fast_choice=None
        self.publish('已确认：'+option.name)
        self.checkpoint_action()
        if kind in ('attribute','skill'):self._safe_same_position_followup()
        return kind=='factor' and self._factor_name(option.name) not in FACTORS

    def _next_unknown_attribute(self,prior,current):
        """A free etching confirmation may produce another attribute canvas."""
        if not crafting_local_runtime.enabled(self):return False
        from src.crafting_resume import _selection_native_capture_matches
        window=getattr(self.f,'target_identity',None) or {}
        if (any(type(window.get(k)) is not int or window[k]<=0 for k in ('hwnd','pid'))
                or getattr(prior,'observation_epoch',None)==getattr(current,'observation_epoch',None)):
            return False
        for screen in (prior,current):
            if (screen.kind!='attribute' or not hasattr(screen,'unknown_attribute_classification_evidence')
                    or not _selection_native_capture_matches(screen,window)
                    or screen.card_slots()!=tuple(range(5)) or len(screen.options())!=5):return False
        return ('请选择一个选项' in current.text_in((1600,970,2020,1080))
                and not any(current.selected(o.area) for o in current.options())
                and self.signature(prior)!=self.signature(current))

    def _safe_same_position_followup(self):
        """At most one known cost-free node entry after a verified choice.

        The new button's label/page and exact fresh hub pixels are mandatory.
        Refresh, start/finish, etching and dialogs never enter this shortcut.
        """
        from src.crafting_fast import SAFE_HUB_FOLLOWUPS,FastPathExit
        if (not getattr(self,'allow_fast_followup',True) or not getattr(self,'page_observation',False)
                or self.uncertain or self.pending_action or self.refresh_state or self._bad_factor_history()):return False
        current=self.screen()
        if current.kind!='hub':return False
        prior=getattr(self,'_last_choice_confirm_area',None)
        if not prior:return False
        labels=[label for label in SAFE_HUB_FOLLOWUPS if current.button(label)]
        if len(labels)!=1:
            self.f.emit('craft_fast_exit',reason='no_unique_safe_hub_followup');return False
        label=labels[0];area=current.button(label)
        a,b,c,d=area;x,y,x2,y2=prior
        if abs((a+c)-(x+x2))>96 or abs((b+d)-(y+y2))>96:
            self.f.emit('craft_fast_exit',reason='safe_followup_position_differs');return False
        if not self._current_verified():return False
        if crafting_local_runtime.enabled(self):
            progress=current.progress()
            if type(progress) is not int or not 0<=progress<30:return False
            self.hub_step=progress
        old=current.text
        self.observe_skill_opportunities(current,label,'entry_started')
        self._fast_navigation={'previous':current,'label':label}
        try:
            self.f.emit('craft_fast_followup',label=label,max_inputs=1,requires_exact_fresh_hub=True)
            self.button('hub',label,lambda now:now.kind!='unknown' and (now.kind!='hub' or now.text!=old))
            self.observe_skill_opportunities(self.screen(),label,'entry_verified')
            return True
        except FastPathExit as error:
            if self.uncertain:raise
            self.f.emit('craft_fast_exit',reason='safe_hub_pixels_changed',detail=str(error))
            return False
        finally:self._fast_navigation=None

    @staticmethod
    def _factor_name(name):
        # The existing factor parser canonicalizes these two names; a visible
        # generic prefix is harmless, but substrings cannot become target names.
        # Unlike a skill title, a foreign module prefix must stay conflicting.
        name=re.sub(r'\s+','',name).strip('◇◆◈♦♢·•・')
        # Actual native title OCR can merge the preceding purple quality glyph
        # into one 国 before the complete 通用 separator. No arbitrary prefix
        # or factor-name characters are removed.
        name=re.sub(r'^国(?=通用[·•・:：])','',name)
        return re.sub(r'^通用[·•・:：]+','',name)

    def _factor_names(self,s,options):
        if not options or [o.index for o in options]!=list(range(len(options))) or any(not o.name for o in options):
            raise ChoiceUnreadable('因子候选身份不完整，不能推断全部已歪')
        boxes=getattr(s,'boxes',None)
        if boxes is not None:
            anchors=[b for b in boxes if '系统研发目标' in re.sub(r'\s+','',b.name) and 240<b.y<480]
            if len(anchors)!=len(options):raise ChoiceUnreadable('因子卡片与候选不完整，不能推断全部已歪')
        names=[]
        for option in options:
            name=self._factor_name(option.name)
            if hasattr(s,'text_in'):
                x,_,x2,_=option.area
                reader=getattr(s,'factor_title_native',None)
                raw=s.text_in((x,580,x2,635))
                # options() stores integer areas after reading its original
                # floating OCR geometry. Reuse that exact same-frame field key.
                cache=getattr(s,'factor_title_evidence',{})
                keys=[key for key in cache if tuple(map(int,key))==tuple(option.area)] if isinstance(cache,dict) else []
                native_title=reader(keys[0] if len(keys)==1 else option.area) if reader else None
                if native_title:
                    coarse_name=self._factor_name(raw);native_name=self._factor_name(native_title)
                    if coarse_name!=native_name and any(target in coarse_name or target in native_name for target in FACTORS):
                        raise ChoiceUnreadable('因子整屏与独立标题对目标身份有冲突，未选择或退出')
                    raw=native_title
                if not raw or self._factor_name(raw)!=name:
                    raise ChoiceUnreadable('因子原名与当前候选矛盾，未选择或退出')
            names.append(name)
        return names

    def _target_selected(self,s,option):
        # OCR-derived mission headings can move a pixel. Bind selected pixels to
        # the freshly verified candidate, not the previous heading's rectangle.
        area=self._target_for_input(s,option)
        return bool(area) and s.selected(area)

    def _target_for_input(self,s,option):
        if s.kind=='factor':self._factor_names(s,s.options())
        return s.target(option)

    def _bad_factor_history(self):
        names=[self._factor_name(name) for name in self.record.factors]
        return any(name not in FACTORS for name in names) or (len(names)>=2 and set(names)!=set(FACTORS))

    def _factor_exit_proven(self,phase='factor_exit_verified'):
        proof=self.pending_action or {};window=getattr(self.f,'target_identity',None) or {}
        rows=proof.get('candidates');names=proof.get('canonical_names')
        required=set(FACTORS)-{self._factor_name(name) for name in self.record.factors}
        return (not self.uncertain and proof.get('phase')==phase and proof.get('kind')=='factor_exit'
            and proof.get('run_id')==self.run_id and proof.get('profile')==self.policy.name and proof.get('target')==self.target
            and proof.get('factor_history')==self.record.factors and proof.get('required')==sorted(required or set(FACTORS))
            and isinstance(rows,list) and bool(rows) and isinstance(names,list) and len(names)==len(rows)
            and [row.get('index') for row in rows]==list(range(len(rows)))
            and all(self._factor_name(row.get('name',''))==name and name and name not in (required or set(FACTORS))
                    for row,name in zip(rows,names))
            and isinstance(proof.get('signature'),str) and bool(proof['signature'])
            and all(type(window.get(k)) is int and window[k]>0 and
                    (proof.get('window_identity') or {}).get(k)==window[k] for k in ('hwnd','pid')))

    def _factor_return_button(self,now):
        area=now.button('返回',(30,25,220,110))
        if area:return area
        # The dim factor overlay can omit this label from full native OCR.
        # Reuse the existing .95 back template only on this same verified packet.
        import hashlib
        import numpy as np
        from src.resolution import ReferenceTask
        task=self.f.task;vision=getattr(self.f,'vision',None)
        native=getattr(now,'native_frame',None);capture=getattr(now,'capture_provenance',None)
        metadata=capture.get('metadata') if isinstance(capture,dict) else None
        window=getattr(self.f,'target_identity',None) or {}
        if (now.kind!='factor' or not self._factor_exit_proven('factor_rejected')
                or not isinstance(task,ReferenceTask) or getattr(vision,'task',None) is not task
                or not isinstance(native,np.ndarray) or native.shape!=(720,1280,3)
                or not isinstance(capture,dict) or capture.get('source')!='ReferenceTask.native_frame'
                or capture.get('observation_epoch')!=getattr(now,'observation_epoch',None)
                or not isinstance(metadata,dict) or metadata!=task.metadata
                or any(metadata.get(key)!=window.get(key) for key in ('hwnd','pid'))
                or not isinstance(metadata.get('session_id'),str) or not metadata['session_id']
                or type(metadata.get('sequence')) is not int or metadata['sequence']<=0
                or capture.get('native_frame_sha256')!=hashlib.sha256(native.tobytes()).hexdigest()
                or capture.get('recognition_frame_sha256')!=hashlib.sha256(now.frame.tobytes()).hexdigest()
                or not np.array_equal(native,task.native_frame)):
            return None
        box=vision.find('back')
        if box is None or not .95<=box.confidence<=1:return None
        # Vision boxes are 3840x2160; crafting input areas are 2048x1152.
        area=tuple(value/1.875 for value in (box.x,box.y,box.x+box.width,box.y+box.height))
        x,y,x2,y2=area
        if not (30<=x<x2<=220 and 25<=y<y2<=110):return None
        now.factor_return_evidence={'producer':'existing_back_template','threshold':.95,
            'confidence':float(box.confidence),'reference_area':[box.x,box.y,box.x+box.width,box.y+box.height],
            'crafting_area':list(area),'observation_epoch':now.observation_epoch}
        return area

    def _factor_exit_selection_option(self,s):
        proof=self.pending_action or {}
        if (s.kind!='factor' or not self._factor_exit_proven('factor_rejected')
                or proof.get('selection_required_for_exit') is not True
                or json.dumps(self.signature(s),ensure_ascii=False)!=proof['signature']):
            raise SafetyStop('退出前置选择缺少已证明的同一坏因子来源')
        options=s.options()
        if (not options or json.dumps([asdict(option) for option in options],ensure_ascii=False,sort_keys=True)!=
                json.dumps(proof['candidates'],ensure_ascii=False,sort_keys=True)
                or self._factor_names(s,options)!=proof['canonical_names']):
            raise SafetyStop('退出前置因子不是原完整候选，未选择')
        if len(options)>1 and any(not option.score_known for option in options):
            raise SafetyStop('退出前置候选效能不完整，未猜选项')
        return max(options,key=lambda option:(option.score,-option.index))

    def _confirm_factor_for_exit(self,s):
        option=self._factor_exit_selection_option(s)
        picked=self.pick(s,exit_option=option)
        if picked is None:raise SafetyStop('退出前置选择时因子页面变化，未确认')
        kind,option=picked
        self.pending_action['phase']='confirming'
        self.checkpoint_action()
        self.input(kind,lambda now:now.button('确认') if self._target_selected(now,option) else None,
                   lambda now:now.kind in ({'hub','result','overclock'}|CHOICES)-{kind},'确认')
        self.record.commit(kind,option)
        self.pending_action=None;self.refresh_state=None;self.fast=True
        self.publish('已确认退出前置因子：'+option.name+'；立即退出本件')
        self.checkpoint_action()

    def quick_finish(self):
        s=self.screen()
        if (self.uncertain or (self.pending_action or {}).get('phase')=='confirming'
                or (self.refresh_state or {}).get('phase') in ('confirming','uncertain')
                or not (self._bad_factor_history() or self._factor_exit_proven('factor_rejected'))):
            raise SafetyStop('因子退出来源未知或旧确认未解决，未发送返回')
        if (self._factor_exit_proven('factor_rejected') and self.pending_action.get('selection_required_for_exit') is True
                and not crafting_local_runtime.enabled(self)):
            self._confirm_factor_for_exit(s)
            s=self.screen()
        if self._factor_exit_proven('factor_rejected') and crafting_local_runtime.enabled(self):
            if not self._factor_return_button(s):
                if getattr(self.f.task,'allow_factor_exit_selection',True) is not True:
                    raise SafetyStop('当前因子页没有可验证的返回入口；未授权仅为退出免费选卡，保留未选现场。')
                self.f.emit('craft_factor_exit_selection_authorized',
                    authorization=getattr(self.f.task,'factor_exit_selection_authorization','local_factor_quick_exit_policy'),
                            reason='无可验证返回入口，仅免费确认一项后立即快速结束',
                            refresh_allowed=False,continue_research_allowed=False)
                self.pending_action['selection_required_for_exit']=True
                self.checkpoint_action()
                self._confirm_factor_for_exit(s)
                s=self.screen()
        if self._factor_exit_proven('factor_rejected'):
            def locate(now):
                if (now.kind!='factor' or json.dumps(self.signature(now),ensure_ascii=False)!=self.pending_action['signature']
                        or self._factor_names(now,now.options())!=self.pending_action['canonical_names']):return None
                return self._factor_return_button(now)
            self.input('factor',locate,lambda now:now.kind=='quick_dialog','返回')
            self.pending_action['phase']='factor_exit_verified'
            self.checkpoint_action()
        elif self._bad_factor_history() and s.kind in CHOICES:
            self.button(s.kind,'返回',lambda now:now.kind=='quick_dialog',(30,25,220,110))
        else:
            self.wait(lambda s:s.kind=='hub','返回研发进度')
            self.button('hub','返回',lambda s:s.kind=='quick_dialog',(30,25,220,110))
        self.confirm_quick()

    def confirm_quick(self):
        if (self.uncertain or (self.pending_action or {}).get('phase')=='confirming'
                or not (self._bad_factor_history() or self._factor_exit_proven())):
            raise SafetyStop('快速结束弹窗缺少明确坏因子退出来源或确认结果未知，禁止自动确认')
        self.pending_action={'phase':'confirming','kind':'quick_finish'}
        self.completion_kind='quick'
        self.normal_origin_known=False
        self.checkpoint_action()
        self.button('quick_dialog','快速智能研发',lambda s:s.kind in ('hub','result'),(1250,660,1600,770))
        self.pending_action=None
        self.checkpoint_action()

    def finish_result(self):
        self.wait(lambda s:s.kind=='result' or
                  (self.etching_pending and self.etch_result_seen and s.kind=='map'),
                  '核对模组结果')
        if self.screen().kind=='map':
            self.finish_observed_etch_map()
            return
        self.f.snapshot('module_result')
        self.input('result',lambda s:(950,1040,1090,1100),lambda s:s.kind=='map','关闭模组结果')

    def finish_observed_etch_map(self):
        if not getattr(self,'etching_pending',False):
            self.f.emit('craft_etch_transition',phase='unexpected_map',page='map',expected=False)
            raise SafetyStop('非刻蚀上下文进入 map，禁止继续制作')
        if not self.etch_result_seen:
            self.f.emit('craft_etch_transition',phase='unexpected_map',page='map',expected=False,
                        reason='module_result_not_observed')
            raise SafetyStop('进入 map，但没有观察到未知区域刻蚀后的模组结果，禁止计入完成')
        self.wait(lambda s:s.kind=='map' and self.f.vision.page('DARK_MAP'),
                  '确认刻蚀结果返回研发地图')
        self.f.emit('craft_etch_transition',phase='map_confirmed',page='map',expected=True,
                    next='module_entry')

    def record_completion(self,outcome):
        current_only=getattr(self,'continuation_scope','batch')=='current_module_only' or getattr(self,'epoch_baseline',None) is not None
        if current_only and getattr(self,'session_delta_completed',self.normal+self.quick)>=1:
            raise SafetyStop('当前基线只允许记录本个模组的完成增量，禁止重复计数')
        if outcome=='quick':self.quick+=1
        elif outcome=='normal':self.normal+=1
        elif outcome=='unknown':
            self.unknown_completion=getattr(self,'unknown_completion',0)+1
            if not current_only:
                self.historical_completed=self.normal+self.quick
                self.session_delta_completed=0
            self.continuation_scope='current_module_only'
        else:raise SafetyStop('完成类型无效，禁止保存猜测分类')
        self.session_delta_completed=getattr(self,'session_delta_completed',0)+1
        self.module_active=False
        self.pending_action=None;self.refresh_state=None;self.completion_kind=None
        self.etching_pending=False;self.etch_result_seen=False
        # Durable batch accounting must precede permission to start another module.
        self.checkpoint_action()
        self.publish('本个模组快速结束' if outcome=='quick' else '本个模组正常完成' if outcome=='normal' else '本个模组已完成；正常/快速分类未知')
        self.save_record(self.f.logdir/f'module-{self.normal+self.quick+getattr(self,"unknown_completion",0):03d}.json',
                         {**asdict(self.record),'completion_classification':outcome})

    def one(self):
        if getattr(self,'continuation_scope','batch')=='current_module_only' or self.epoch_baseline is not None:
            raise SafetyStop('历史完成数未知，本次恢复仅继续当前模组，禁止启动额外实验')
        self.refresh_state=None;self.pending_action=None;self.hub_step=None;self.completion_kind=None
        self.hub_baseline=None
        self.etching_pending=False;self.etch_result_seen=False
        self.record=CraftRecord();self.publish('准备'+self.policy.name+'模组')
        self.acceptance_evidence.start_module()
        self.normal_origin_known=True
        ModulePreparation(self.f).run()
        self.wait(lambda s:s.kind=='prepared','核对实验入口')
        self.start_prepared()
        self.advance()

    def advance(self,fast=False):
        self.fast=fast
        for _ in range(160):
            if getattr(self,'page_observation',False):
                self.wait_current(lambda s:s.kind!='unknown','识别制作节点',full=False)
                s=self.screen(full=False)
            else:
                self.wait_current(lambda s:s.kind!='unknown','识别制作节点')
                s=self.screen()
            self.observe_skill_opportunities(s)
            if self._bad_factor_history() and getattr(self,'completion_kind',None)!='quick':
                if (self.uncertain or (self.pending_action or {}).get('phase')=='confirming'
                        or (self.refresh_state or {}).get('phase') in ('confirming','uncertain') or s.kind=='refresh_confirm'):
                    raise SafetyStop('已确认歪因子但旧确认或刷新未解决，禁止继续消耗')
                if s.kind in CHOICES|{'hub'}:
                    fast=True;self.fast=True;self.quick_finish();continue
            if s.kind!='attribute':
                self.attribute_status=None;status.pop('属性决策',None)
            if s.kind=='result':
                outcome=getattr(self,'completion_kind',None)
                if outcome is None and getattr(self,'etching_pending',False):
                    outcome='normal'
                if outcome not in ('normal','quick'):
                    self.f.snapshot('module_result_unclassified')
                    outcome='unknown'
                self.finish_result()
                self.observe_skill_opportunities(self.screen(),phase='terminal_verified')
                self.record_completion(outcome)
                return
            if s.kind=='map':
                self.finish_observed_etch_map()
                self.observe_skill_opportunities(self.screen(),phase='terminal_verified')
                self.record_completion('normal')
                return
            if s.kind=='overclock':
                if getattr(self,'page_observation',False):
                    self.wait(lambda s:s.kind!='overclock' and s.kind!='unknown','等待超频动画结束',full=False)
                else:self.wait(lambda s:s.kind!='overclock' and s.kind!='unknown','等待超频动画结束')
                current=self.screen(full=False) if getattr(self,'page_observation',False) else self.screen()
                self.observe_skill_opportunities(current)
                continue
            if s.kind=='prepared':
                self.start_prepared();continue
            if s.kind=='quick_dialog':
                if not fast or not (self._bad_factor_history() or self._factor_exit_proven()):
                    raise SafetyStop('快速结束弹窗缺少已确认坏因子决策来源，禁止自动确认')
                self.confirm_quick();continue
            if s.kind=='refresh_confirm':
                if self.refresh_state:self.confirm_refresh(self.refresh_state['kind'])
                else:self.button('refresh_confirm','取消',lambda s:s.kind in CHOICES,(630,750,1410,850))
                continue
            if s.kind in CHOICES:
                if self.select(s):
                    fast=True;self.fast=True;self.quick_finish()
                continue
            if s.kind=='hub':
                self.hub_step=s.progress()
                # Optional identity evidence never relaxes normal input validation.
                self.hub_baseline=None
                if hasattr(s,'trusted_hub_state'):
                    try:self.hub_baseline=s.trusted_hub_state()
                    except SafetyStop:pass
                labels=('实验启动','战术核心','属性刻录','属性深度刻录','技能研习','技能深度研习',
                        '因子','变量','快速休整','未知区域刻蚀','完成模组','进行封装')
                known_pages=CHOICES|{'result','overclock','map','refresh_confirm','quick_dialog'}
                self.wait_current(lambda now:now.kind in known_pages or
                          now.kind=='hub' and any(now.button(v) for v in labels),'读取当前研发节点入口')
                s=self.screen()
                if s.kind in known_pages:continue
                label=next(v for v in labels if s.button(v))
                self.observe_skill_opportunities(s,label,'entry_started')
                if label in ('完成模组','进行封装'):
                    if self.completion_kind not in ('normal','quick'):
                        self.completion_kind='quick' if fast else 'normal' if getattr(self,'normal_origin_known',True) else None
                    self.checkpoint_action()
                old=s.text
                if label=='未知区域刻蚀':
                    self.etching_pending=True
                    self.f.emit('craft_etch_transition',phase='started',page='hub',hub_step=self.hub_step)
                self.button('hub',label,lambda s:s.kind!='unknown' and (s.kind!='hub' or s.text!=old))
                self.observe_skill_opportunities(self.screen(),label,'entry_verified')
                if label=='未知区域刻蚀':
                    self.f.emit('craft_etch_transition',phase='action_verified',page=self.screen().kind)
                continue
            raise SafetyStop('制作阶段出现未处理页面：'+s.kind)
        raise SafetyStop('制作节点超过本轮上限，停止并保留现场')

    def observe_skill_opportunities(self,s,label=None,phase='observed'):
        self.acceptance_evidence.event('page_observed',s,label=label,observation_phase=phase)
        if getattr(getattr(self,'policy',None),'name',None)!='先攻':return None
        from src.crafting_opportunities import observe
        try:evidence=observe(self.record,s,self.hub_step,label,phase)
        except (ValueError,TypeError,KeyError) as error:
            raise SafetyStop('技能机会来源记录无效，未猜测剩余次数：'+str(error)) from error
        if evidence:
            self.acceptance_evidence.event('opportunity_transition',s,transition=evidence,
                reason='existing_sourced_observation',label=label)
            self.f.emit('craft_skill_opportunity_source',**evidence)
            if (self.module_active and not self.uncertain and getattr(self.f,'logdir',None)
                    and (self.pending_action or {}).get('phase')!='confirming'):
                self.checkpoint_action()
        return evidence

    def start_prepared(self):
        """Continue the visible prepared module with the existing spending checks."""
        if (self.pending_action or {}).get('phase')=='confirming':
            raise SafetyStop('实验启动确认结果未知，禁止重放')
        if crafting_local_runtime.enabled(self):
            preparation=ModulePreparation(self.f)
            self.f.wait(preparation.ready,'实验启动前复核胚子身份及已选蓝图')
            preparation.verify_preparation_team()
        s=self.screen()
        self.observe_skill_opportunities(s,phase='blueprint_observed')
        epochs=[getattr(s,'observation_epoch',None)]
        while True:
            nums=re.findall(r'\d+',s.text_in((1740,20,2020,110)))
            costs=re.findall(r'\d+',s.text_in((1550,965,1750,1085)))
            if len(nums)==1 and len(costs)==1:break
            s=self.fresh_evidence(s,epochs,{'missing_field':'experiment_resource','target_slot':None,
                'producer_plan':('balance_and_cost_roi',),'action_dependency':'实验启动前必须证明余额与消耗'})
            if s.kind!='prepared':return
        balance,cost=int(nums[0]),int(costs[0])
        if cost<=0 or balance<cost:raise SafetyStop('实验启动资源不足或消耗无效')
        crafting_local_runtime.preflight_start(self,s)
        self.pending_action={'phase':'confirming','kind':'experiment_start','balance':balance,'cost':cost}
        self.normal_origin_known=True
        self.module_active=True;self.checkpoint_action()
        self.button('prepared','实验启动',lambda now:now.kind=='hub')
        self.f.module_team_ready=False
        crafting_local_runtime.start_verified(self,s,self.screen())
        self.pending_action=None;self.checkpoint_action()

    def verify_start_observation(self,screen):
        """Revalidate the reserved cost and identity on the final input frame."""
        pending=self.pending_action or {}
        balance=re.findall(r'\d+',screen.text_in((1740,20,2020,110)))
        cost=re.findall(r'\d+',screen.text_in((1550,965,1750,1085)))
        if (pending.get('kind')!='experiment_start' or pending.get('phase')!='confirming'
                or len(balance)!=1 or len(cost)!=1
                or (int(balance[0]),int(cost[0]))!=(pending.get('balance'),pending.get('cost'))):
            raise SafetyStop('实验启动前余额或成本与已保存意图不一致，未发送确认')
        if crafting_local_runtime.enabled(self):
            preparation=ModulePreparation(self.f)
            if not preparation.ready():raise SafetyStop('实验启动前模组身份或蓝图状态变化，未发送确认')

    def run(self):
        restored=getattr(self.f.task,'only',None)!='MODULE_RESUME'
        try:
            if getattr(self.f.task,'only',None)=='MODULE_RESUME':
                self.restore()
                restored=True
                if self.module_active and self.normal+self.quick<self.target:
                    self.f.task.before_module(self.f)
                    self.advance(self.fast)
                    self.f.task.after_module()
                remaining=0 if getattr(self,'continuation_scope','batch')=='current_module_only' or self.epoch_baseline is not None else self.target-self.normal-self.quick
                for _ in range(remaining):
                    self.f.task.before_module(self.f);self.one();self.f.task.after_module()
                self.publish('当前模组完成；历史批次数未知，本次恢复结束' if getattr(self,'continuation_scope','batch')=='current_module_only' or self.epoch_baseline is not None else '本轮制作完成')
            else:self.run_new()
        except Exception:
            if not restored or not self.accounting_trusted:raise
            # Retain the exact observed page and counters; ambiguous spending never resumes automatically.
            import cv2
            frame=self.f.last_frame
            if frame is not None:
                cv2.imencode('.png',frame)[1].tofile(str(self.f.logdir/'resume.png'))
                payload={'status':{'目标':self.target,'正常完成':self.normal,'快速结束':self.quick,
                                   '分类未知完成':getattr(self,'unknown_completion',0)},
                         'record':asdict(self.record),'safe':not self.uncertain,'fast':self.fast,
                         'refresh_mode':self.f.cfg.get('属性刷新方式','自动'),
                         'observation':self.observation_data(self.screen())}
                payload['refresh_state']=self.refresh_state
                payload['pending_action']=self.pending_action
                payload['hub_step']=self.hub_step
                payload['completion_kind']=self.completion_kind
                payload['normal_origin_known']=getattr(self,'normal_origin_known',False)
                payload['completion_ledger']=self.completion_ledger()
                payload['etching_pending']=self.etching_pending
                payload['etch_result_seen']=self.etch_result_seen
                payload.update(self.recovery_metadata())
                path=self.f.logdir/'resume.json'
                temp=path.with_suffix('.tmp');temp.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8');temp.replace(path)
            raise

    def restore(self):
        self.resume_trace={'resume_mode':'page_driven','historical_association':'fail',
                           'rebaseline_attempted':False,'module_identity':None,'progress':None,
                           'resource':None,'task_identity':{'profile':self.policy.name,'target':self.target,
                           'task_kind':'MODULE_RESUME'},'page_identity':None,'failed_check':None}
        from src.crafting_resume import page_driven_restore
        try:page_driven_restore(self)
        except Exception as error:
            self.resume_trace['failed_check']=str(error)
            self.f.emit('craft_resume_validation',**self.resume_trace)
            raise

    def navigate_current(self):
        if self.screen().kind in CHOICES|{'hub','result','refresh_confirm','prepared'}:return
        ModulePreparation(self.f).navigate(active=lambda:self.screen().kind in CHOICES|{'hub','result','refresh_confirm'})

    def run_new(self):
        resume=getattr(self.f.task,'test_quick_resume',None)
        if resume:
            # Explicit test-only continuation of the exact failed run, not automatic recovery.
            self.record=CraftRecord(**{**resume,'missions':[]})
            from src.crafting_policy import Option
            self.record.missions=[Option(**o) for o in resume['missions']]
            if not self._bad_factor_history():
                raise SafetyStop('续测记录不符合坏因子快速结束条件')
            self.wait(lambda s:s.kind=='quick_dialog','核对原快速结束弹窗')
            self.confirm_quick();self.advance(True)
            self.f.task.after_module();self.publish('本轮制作完成');return
        self.f.refresh()
        self.navigate_current()
        if self.screen().kind in CHOICES|{'hub','result','refresh_confirm'}:
            self.restore();self.f.task.before_module(self.f)
            if self.module_active and self.normal+self.quick<self.target:
                self.advance(self.fast);self.f.task.after_module()
        remaining=0 if getattr(self,'continuation_scope','batch')=='current_module_only' or self.epoch_baseline is not None else self.target-self.normal-self.quick
        for _ in range(remaining):
            self.f.task.before_module(self.f)
            self.one()
            self.f.task.after_module()
        self.publish('当前模组完成；历史批次数未知，本次恢复结束' if getattr(self,'continuation_scope','batch')=='current_module_only' or self.epoch_baseline is not None else '本轮制作完成')
