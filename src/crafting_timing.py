"""Bounded wall timers around existing crafting calls, without extra observations."""
import json
import threading
import time
from collections import deque
from datetime import datetime
from functools import wraps
from pathlib import Path
from uuid import uuid4


def _percentile(values, percent):
    if not values:return None
    values=sorted(values);position=(len(values)-1)*percent/100
    low=int(position);high=min(low+1,len(values)-1)
    return (values[low]+(values[high]-values[low])*(position-low))/1_000_000


class CraftTiming:
    """Instance-local wrappers preserve return values, exceptions and call counts."""
    def __init__(self,flow,clock_ns=time.perf_counter_ns,sample_limit=4096):
        self.flow=flow;self.clock_ns=clock_ns;self.sample_limit=max(1,int(sample_limit))
        self.started_ns=clock_ns();self.stats={};self.local=threading.local()
        self.lock=threading.Lock();self.patches=[];self.runner_ids=set();self.capture_ids=set()
        self.finished=False;self.output_path=None;self.save_error=None
        self.actions=deque(maxlen=64)
        self.attempts=deque(maxlen=128)
        self.last_candidate_ns=None;self.last_verified_ns=None
        self.last_candidate_label=None;self.last_verified_label=None
        self.segment_id=str(uuid4());self.runner=None
        self.confirm_click=None;self.confirm_result=None
        self.handoffs=deque(maxlen=self.sample_limit)
        self.unpaired_handoffs=deque(maxlen=64)
        self.selection_click=None;self.selection_handoffs=deque(maxlen=self.sample_limit)
        self.selection_attempts=[];self.selection_manual=None
        self.selection_decision=None
        self.fast_exits=deque(maxlen=64);self.fast_exit_counts={}

    def _identity(self):
        runner=self.runner
        identity={'segment_id':self.segment_id,'run_id':getattr(runner,'run_id',None),
            'checkpoint_session_id':getattr(runner,'checkpoint_session_id',None),
            'action_id':getattr(runner,'acceptance_action_id',None),
            'transaction_id':getattr(runner,'selection_transaction_id',None),
            'page_kind':getattr(getattr(runner,'cached_screen',None),'kind',None)}
        evidence=getattr(runner,'acceptance_evidence',None)
        context=getattr(evidence,'context',None)
        if callable(context):
            try:
                values=context();identity['module_key']=values.get('module_key')
                identity['acceptance_segment_id']=values.get('segment_id')
            except Exception:identity['module_key']=None
        return identity

    def _counts(self):
        with self.lock:return {name:row['count'] for name,row in self.stats.items()}

    @staticmethod
    def _observation_counts(counts):
        return {'screenshot_calls':counts.get('capture.reference_task',0),
            'ocr_calls':counts.get('ocr.backend.full',0)+counts.get('ocr.backend.roi',0),
            'full_ocr_calls':counts.get('ocr.backend.full',0),
            'roi_ocr_calls':counts.get('ocr.backend.roi',0)}

    def _event(self,event,data):
        """Keep the audit's confirmation-result origin across intermediate waits.

        New timer instances are distinct recovery segments and never create a
        cross-process interval. Device dispatch and safety guards are unchanged.
        """
        if event=='craft_execution_phase' and data.get('phase')=='决策完成':
            self.selection_decision={'monotonic_ns':self.clock_ns(),'time':datetime.now().isoformat(),
                'transaction_id':data.get('transaction_id')}
            return
        if event=='craft_manual_intervention':
            if self.selection_click is not None:self.selection_manual=True
            return
        if event=='craft_fast_exit':
            reason=data.get('reason','unknown')
            self.fast_exit_counts[reason]=self.fast_exit_counts.get(reason,0)+1
            self.fast_exits.append({'time':datetime.now().isoformat(),'monotonic_ns':self.clock_ns(),**data})
            return
        if event not in ('click','verified','recognition_retry','step_pause',
                'craft_focus_reobserve','craft_resume_reobserve','craft_evidence_retry',
                'craft_skill_retry','craft_visual_recovery'):
            return
        point={'time':datetime.now().isoformat(),'monotonic_ns':self.clock_ns(),
            'event':event,'step':data.get('step'),**self._identity()}
        if event=='click':
            selected=self.selection_click
            if (point['step']=='确认' and selected and selected['run_id']==point['run_id']
                    and selected.get('module_key')==point.get('module_key')
                    and selected.get('page_kind')==point.get('page_kind')):
                elapsed=max(0,point['monotonic_ns']-selected['monotonic_ns'])
                counts=self._counts();delta={key:value-selected['counts'].get(key,0) for key,value in counts.items()}
                decision=self.selection_decision
                self.selection_handoffs.append({'selection_click':selected['time'],'confirm_click':point['time'],
                    'selection_to_confirmation_ms':elapsed/1_000_000,'page_kind':point['page_kind'],
                    'transaction_id':selected.get('transaction_id'),
                    'decision_to_confirmation_ms':max(0,point['monotonic_ns']-decision['monotonic_ns'])/1_000_000
                        if decision and decision['transaction_id']==selected.get('transaction_id') else None,
                    'selection_action_id':selected['action_id'],'confirmation_action_id':point['action_id'],
                    'attempts':list(self.selection_attempts),'manual_intervention':self.selection_manual,
                    # Without a physical-input monitor, absence of a report is
                    # unknown. A manually assisted path is never unattended.
                    'unattended_success':False if self.selection_manual else None,
                    **self._observation_counts(delta)})
                self._record('latency.selection_to_confirmation',elapsed,0)
            if str(point['step']).startswith('选择 '):
                retry=str(point['step']).endswith(' 补点')
                same=(selected and selected['run_id']==point['run_id'] and
                    selected.get('module_key')==point.get('module_key') and
                    selected.get('page_kind')==point.get('page_kind'))
                transaction=point.get('transaction_id')
                same_transaction=bool(same and transaction and selected.get('transaction_id')==transaction)
                if not same_transaction and (transaction or not retry or not same):
                    self.selection_click={**point,'counts':self._counts()}
                    self.selection_attempts=[];self.selection_manual=None
                self.selection_attempts.append({'action_id':point['action_id'],
                    'attempt_id':getattr(self.runner,'input_attempt_id',None),
                    'started_at':point['time'],'monotonic_ns':point['monotonic_ns'],'retry':retry})
            else:
                self.selection_click=None;self.selection_attempts=[];self.selection_manual=None
                self.selection_decision=None
            prior=self.confirm_result
            if (prior is not None and prior['run_id']==point['run_id']
                    and prior.get('module_key')==point.get('module_key')):
                elapsed=max(0,point['monotonic_ns']-prior['monotonic_ns'])
                counts=self._counts()
                delta={key:value-prior['counts'].get(key,0) for key,value in counts.items()}
                evidence=prior['intervening_evidence']
                reasons=prior['reasons']
                choices={'attribute','skill','mission','factor','core_attribute','core_skill','rest','refresh_confirm'}
                ordinary_scope=('completion_or_quick_exit_navigation' if point['step'] in
                    ('返回','快速智能研发','关闭模组结果','实验启动') else
                    'page_identity_unavailable' if prior['confirm_click']['page_kind'] is None or point['page_kind'] is None else
                    'outside_creation_node' if prior['confirm_click']['page_kind'] not in choices or
                        point['page_kind'] not in choices|{'hub'} else 'ordinary_creation_node')
                row={'segment_id':self.segment_id,'run_id':point['run_id'],
                    'module_key':prior.get('module_key'),'next_module_key':point.get('module_key'),
                    'acceptance_segment_id':prior.get('acceptance_segment_id'),
                    'confirmation_action_id':prior['action_id'],
                    'next_action_id':point['action_id'],'confirm_click':prior['confirm_click']['time'],
                    'confirm_result_verified':prior['time'],'next_click':point['time'],
                    'next_label':point['step'],'verified_to_next_input_ms':elapsed/1_000_000,
                    'confirm_click_to_next_input_ms':max(0,point['monotonic_ns']-prior['confirm_click']['monotonic_ns'])/1_000_000,
                    'class':('animation' if reasons==['explicit_overclock_animation_wait'] else
                             'exception' if reasons else 'ordinary'),
                    'reasons':reasons or ['ordinary_observation_and_final_input_guard'],
                    'confirmation_kind':prior['confirm_click']['page_kind'],'next_kind':point['page_kind'],
                    'ordinary_creation_scope':ordinary_scope,
                    'intervening_evidence':evidence,'intervening_evidence_count':prior['evidence_count'],
                    **self._observation_counts(delta)}
                self.handoffs.append(row)
                self._record('latency.confirmation_result_to_next_input',elapsed,0)
                if not reasons:self._record('latency.ordinary_confirmation_result_to_next_input',elapsed,0)
                if not reasons and ordinary_scope=='ordinary_creation_node':
                    self._record('latency.ordinary_creation_confirmation_result_to_next_input',elapsed,0)
            elif prior is not None:
                self.unpaired_handoffs.append({'confirmation_action_id':prior['action_id'],
                    'next_action_id':point['action_id'],'result_time':prior['time'],'next_click':point['time'],
                    'previous_run_id':prior['run_id'],'run_id':point['run_id'],
                    'previous_module_key':prior.get('module_key'),'module_key':point.get('module_key'),
                    'reason':'run_id_changed' if prior['run_id']!=point['run_id'] else 'module_key_changed'})
            self.confirm_result=None
            self.confirm_click=point if point['step']=='确认' else None
        elif event=='verified' and point['step']=='确认 结果' and self.confirm_click is not None:
            if self.confirm_click['run_id']==point['run_id']:
                self.confirm_result={**point,'confirm_click':self.confirm_click,
                    'counts':self._counts(),'intervening_evidence':[],'evidence_count':0,'reasons':[]}
            self.confirm_click=None
        elif self.confirm_result is not None:
            self.confirm_result['evidence_count']+=1
            if len(self.confirm_result['intervening_evidence'])<24:
                self.confirm_result['intervening_evidence'].append(point)
            reason=None
            if event=='verified' and point['step']=='等待超频动画结束':reason='explicit_overclock_animation_wait'
            elif event=='step_pause':reason='explicit_step_pause'
            elif event in ('craft_focus_reobserve','craft_resume_reobserve'):reason='focus_or_pause_reobservation'
            elif event in ('recognition_retry','craft_evidence_retry','craft_skill_retry','craft_visual_recovery'):
                reason='bounded_recognition_retry'
            if reason and reason not in self.confirm_result['reasons']:self.confirm_result['reasons'].append(reason)

    def _record(self,name,elapsed,self_ns,error=False):
        with self.lock:
            row=self.stats.setdefault(name,{'count':0,'errors':0,'inclusive_ns':0,'self_ns':0,
                'min_ns':None,'max_ns':0,'above_3000_count':0,
                'inclusive_samples':deque(maxlen=self.sample_limit),'self_samples':deque(maxlen=self.sample_limit)})
            row['count']+=1;row['errors']+=int(error)
            row['inclusive_ns']+=elapsed;row['self_ns']+=self_ns
            row['inclusive_samples'].append(elapsed);row['self_samples'].append(self_ns)
            row['min_ns']=elapsed if row['min_ns'] is None else min(row['min_ns'],elapsed)
            row['max_ns']=max(row['max_ns'],elapsed);row['above_3000_count']+=int(elapsed>3_000_000_000)

    def _call(self,name,original,args,kwargs):
        stack=getattr(self.local,'stack',None)
        if stack is None:stack=self.local.stack=[]
        started=self.clock_ns();entry=[0];stack.append(entry);failed=False
        try:return original(*args,**kwargs)
        except BaseException:
            failed=True;raise
        finally:
            elapsed=max(0,self.clock_ns()-started);stack.pop()
            if stack:stack[-1][0]+=elapsed
            self._record(name,elapsed,max(0,elapsed-entry[0]),failed)

    def _patch(self,owner,name,stage,adapt=None):
        original=getattr(owner,name,None)
        if not callable(original):return
        local=getattr(owner,'__dict__',{})
        existed=name in local;old=local.get(name)
        @wraps(original)
        def measured(*args,**kwargs):
            if adapt:return adapt(original,args,kwargs)
            label=stage(args,kwargs) if callable(stage) else stage
            return self._call(label,original,args,kwargs)
        setattr(owner,name,measured)
        self.patches.append((owner,name,existed,old,measured))

    @staticmethod
    def _ocr_stage(args,kwargs):
        frame=kwargs.get('frame');shape=getattr(frame,'shape',())
        whole=(len(shape)>=2 and shape[1]>=1280 and shape[0]>=720
               and all(kwargs.get(k,default)==default for k,default in
                       (('x',0),('y',0),('to_x',1),('to_y',1))))
        return 'ocr.backend.full' if whole else 'ocr.backend.roi'

    def install_flow(self):
        existing=getattr(self.flow,'__dict__',{}).get('crafting_timing')
        if existing is self:return self
        if existing is not None:
            raise RuntimeError('制作计时器已安装，拒绝重复包装')
        self.flow.crafting_timing=self
        task=self.flow.task
        original_task=getattr(task,'original',task)
        self._patch(self.flow,'refresh','observe.refresh')
        self._patch(self.flow,'snapshot','save.snapshot')
        def event_log(original,args,kwargs):
            self._event(args[0] if args else kwargs.get('event'),kwargs)
            return self._call('save.event_log',original,args,kwargs)
        self._patch(self.flow,'emit','save.event_log',event_log)
        # ReferenceTask keeps the original bound emit alias for coordinates.
        self._patch(task,'emit','save.event_log',event_log)
        def delay(original,args,kwargs):
            duration=args[0] if args else kwargs.get('duration',0)
            if isinstance(duration,(int,float)):
                self._record('delay.requested',max(0,round(duration*1_000_000_000)),0)
            return self._call('delay.actual',original,args,kwargs)
        self._patch(self.flow,'delay','delay.actual',delay)
        def wait(original,args,kwargs):
            predicate=args[0] if args else kwargs['predicate']
            label=args[1] if len(args)>1 else kwargs.get('label','')
            last=[None]
            def observed():
                try:return self._call('wait.predicate',predicate,(),{})
                finally:last[0]=self.clock_ns()
            changed=list(args);options=dict(kwargs)
            if changed:changed[0]=observed
            else:options['predicate']=observed
            value=self._call('wait.total',original,tuple(changed),options)
            self.last_verified_ns=last[0];self.last_verified_label=label
            if label in ('读取当前候选','读取刷新后候选'):
                self.last_candidate_ns=last[0];self.last_candidate_label=label
            return value
        self._patch(self.flow,'wait','wait.total',wait)
        def next_frame(original,args,kwargs):
            try:return self._call('capture.reference_task',original,args,kwargs)
            finally:
                capture=getattr(task,'capture',None)
                if capture is not None and id(capture) not in self.capture_ids:
                    self.capture_ids.add(id(capture))
                    self._patch(capture,'get_frame','capture.wgc')
        self._patch(task,'next_frame','capture.reference_task',next_frame)
        if original_task is not task:self._patch(task,'ocr','ocr.route')
        self._patch(original_task,'ocr',self._ocr_stage)
        self._patch(original_task,'prepare_input','input.prepare')
        self._patch(task,'before_click','input.before_click')
        self._patch(task,'assert_foreground','guard.foreground')
        self._patch(task,'validate_click_context','guard.context')
        self._patch(task,'move','input.move')
        self._patch(task,'scroll','input.scroll')
        def click(original,args,kwargs):
            at=self.clock_ns()
            if self.last_verified_ns is not None:
                gap=max(0,at-self.last_verified_ns)
                self._record('latency.verified_to_input',gap,0)
            if self.last_candidate_ns is not None:
                gap=max(0,at-self.last_candidate_ns)
                self._record('latency.candidate_to_input',gap,0)
                self.last_candidate_ns=None
            return self._call('input.dispatch',original,args,kwargs)
        self._patch(task,'click','input.dispatch',click)
        vision=getattr(self.flow,'vision',None)
        if vision is not None:
            self._patch(vision,'find','recognition.template')
            self._patch(vision,'page','recognition.page')
        return self

    def install_runner(self,runner):
        if id(runner) in self.runner_ids:return
        self.runner=runner
        self.runner_ids.add(id(runner))
        def screen_stage(args,kwargs):
            return 'screen.cached' if runner.cached_frame is runner.f.last_frame else 'screen.new'
        self._patch(runner,'screen',screen_stage)
        self._patch(runner,'choose','decision.observe_and_policy')
        def action(original,args,kwargs):
            at=self.clock_ns();error_type=None
            started_at=datetime.now().isoformat()
            with self.lock:
                before={name:(row['count'],row['inclusive_ns'],row['self_ns'])
                        for name,row in self.stats.items()}
            try:return self._call('input.action_total',original,args,kwargs)
            except BaseException as error:
                error_type=type(error).__name__;raise
            finally:
                with self.lock:
                    stages={}
                    for name,row in self.stats.items():
                        prior=before.get(name,(0,0,0))
                        if row['count']>prior[0]:
                            stages[name]={'count':row['count']-prior[0],
                                'inclusive_ms':(row['inclusive_ns']-prior[1])/1_000_000,
                                'self_ms':(row['self_ns']-prior[2])/1_000_000}
                self.actions.append({'kind':args[0] if args else kwargs.get('kind'),
                    'label':args[3] if len(args)>3 else kwargs.get('label'),
                    **self._identity(),'started_at':started_at,'finished_at':datetime.now().isoformat(),
                    'elapsed_ms':max(0,self.clock_ns()-at)/1_000_000,
                    'error_type':error_type,'stages':stages})
        self._patch(runner,'input','input.action_total',action)
        def attempt(original,args,kwargs):
            at=self.clock_ns();started=datetime.now().isoformat();error_type=None
            try:return self._call('input.attempt_total',original,args,kwargs)
            except BaseException as error:error_type=type(error).__name__;raise
            finally:
                self.attempts.append({**self._identity(),'attempt_id':getattr(runner,'input_attempt_id',None),
                    'kind':args[0] if args else kwargs.get('kind'),
                    'label':args[3] if len(args)>3 else kwargs.get('label'),
                    'started_at':started,'finished_at':datetime.now().isoformat(),
                    'elapsed_ms':max(0,self.clock_ns()-at)/1_000_000,'error_type':error_type,
                    'send_status':getattr(runner,'last_input_status','unknown'),
                    'game_result_verified':error_type is None})
        self._patch(runner,'_input_once','input.attempt_total',attempt)
        self._patch(runner,'save_record','save.record')
        self._patch(runner,'checkpoint_action','save.checkpoint_total')
        evidence=getattr(runner,'acceptance_evidence',None)
        if evidence is not None:self._patch(evidence,'event','save.acceptance_evidence')

    def summary(self):
        stages={}
        with self.lock:
            for name,row in sorted(self.stats.items()):
                stages[name]={'count':row['count'],'errors':row['errors'],
                    'inclusive_ms':row['inclusive_ns']/1_000_000,'self_ms':row['self_ns']/1_000_000,
                    'p50_ms':_percentile(row['inclusive_samples'],50),
                    'p95_ms':_percentile(row['inclusive_samples'],95),
                    'min_ms':None if row['min_ns'] is None else row['min_ns']/1_000_000,
                    'max_ms':row['max_ns']/1_000_000,'above_3000_count':row['above_3000_count'],
                    'self_p50_ms':_percentile(row['self_samples'],50),
                    'self_p95_ms':_percentile(row['self_samples'],95),
                    'sample_count':len(row['inclusive_samples']),
                    'percentiles_all_calls':row['count']<=self.sample_limit}
        handoffs=list(self.handoffs)
        return {'version':3,'source':'existing_calls_only','flow_logdir':str(self.flow.logdir),
            'acceptance_save_error':getattr(getattr(self.runner,'acceptance_evidence',None),'save_error',None),
            'segment':self._identity(),
            'selection_to_confirmation':stages.get('latency.selection_to_confirmation',{}),
            'selection_gaps_recent':list(self.selection_handoffs),
            'unresolved_selection_transaction':({**self.selection_click,
                'attempts':list(self.selection_attempts),'manual_intervention':self.selection_manual}
                if self.selection_click else None),
            'fast_exit_counts':dict(self.fast_exit_counts),'fast_exits_recent_64':list(self.fast_exits),
            'confirmation_result_to_next_input':stages.get('latency.confirmation_result_to_next_input',{}),
            'ordinary_confirmation_result_to_next_input':stages.get('latency.ordinary_confirmation_result_to_next_input',{}),
            'ordinary_creation_confirmation_result_to_next_input':stages.get('latency.ordinary_creation_confirmation_result_to_next_input',{}),
            'confirmation_gaps_recent':handoffs,
            'unpaired_handoffs_recent_64':list(self.unpaired_handoffs),
            'above_3000_actions_recent':[row for row in handoffs if row['verified_to_next_input_ms']>3000],
            'above_3000_input_actions_recent':[row for row in self.actions if row['elapsed_ms']>3000],
            'observation_calls':self._observation_counts(self._counts()),
            'handoff_boundary':'existing emit entry: click 确认 -> verified 确认 结果 -> next click; intermediate waits never renew the origin',
            'elapsed_ms':max(0,self.clock_ns()-self.started_ns)/1_000_000,
            'sample_limit_per_stage':self.sample_limit,'percentile_policy':'linear; recent bounded samples if truncated',
            'stages':stages,'actions_recent_64':list(self.actions),'attempts_recent_128':list(self.attempts),
            'limits':['inclusive stages nest and must not be summed',
                'self time excludes timed children, not uninstrumented work',
                'delay.requested is requested duration, not an additional measured wall interval',
                'latency stages are composite intervals, not additional work',
                'first WGC capture is inside capture.reference_task before its instance can be wrapped',
                'wall time includes user pauses, focus reacquisition and game animations',
                'snapshot combines image encoding and file writes',
                'handoff timestamps observe existing emit entry; device dispatch cost remains a separate stage',
                'recovery segments never pair a previous process result with a later input',
                'confirmation_gaps_recent is bounded; stage counts, maximum and above-3000 count include all calls',
                'intervening_evidence retains at most 24 events; count declares truncation'],
            'ordinary_scope':'audit-compatible ordinary excludes explicit animation/pause/retry only; ordinary_creation also requires creation page identity and excludes completion/quick-exit navigation',
            'configuration':{k:self.flow.cfg.get(k) for k in ('step_mode','click_min_wait','cooperative_interval','debug_mode')}}

    def finish(self):
        if self.finished:return self.output_path
        self.finished=True
        for owner,name,existed,old,measured in reversed(self.patches):
            if getattr(owner,name,None) is not measured:continue
            if existed:setattr(owner,name,old)
            else:delattr(owner,name)
        self.patches.clear()
        if getattr(self.flow,'crafting_timing',None) is self:delattr(self.flow,'crafting_timing')
        path=Path(self.flow.logdir)/'performance.json'
        if path.exists():path=path.with_name('performance-'+uuid4().hex[:8]+'.json')
        try:
            tmp=path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.summary(),ensure_ascii=False,indent=2),encoding='utf-8')
            tmp.replace(path);self.output_path=path
        except (OSError,TypeError,ValueError) as error:self.save_error=repr(error)
        return self.output_path
