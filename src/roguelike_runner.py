"""Bounded observed-state roguelike runner; no crafting imports or resume replay."""
import numpy as np

from src.workflow import SafetyStop, RecognitionTimeout
from src.roguelike_vision import RoguelikeScreen, RogueTemplates
from src.roguelike_timing import TIMING, ActionMode, state_class
from src.roguelike_control import Reobserve
from src.roguelike_policy import (Observation, RogueAction, RogueState as S, DecisionUndefined,
    first_choice, choose_member, member_reason, choose_route, unique_action, order_action,
    search_action, bounty_action, ShopVisit, shop_decision, SettlementLedger)


# The recorded entry flow reaches the start/intro overlay/map. Resume can
# expose these immediately; unrelated navigation and battle pages are excluded.
ENTRY_SUCCESS_STATES = frozenset((S.START, S.OVERLAY, S.MAP))
BATTLE_EXIT_STATES = frozenset((S.BATTLE_RESULT,S.OVERLAY,S.SHOP,S.CHOICE,S.MAP))


class RoguelikeRunner:
    def __init__(self, flow, resume=False):
        self.f = flow
        self.resume = resume
        self.terminal = False
        self.step_approved = False
        self.templates = RogueTemplates()
        self.target = flow.cfg['肉鸽循环次数']
        if type(self.target) is not int or not 1 <= self.target <= 100:
            raise SafetyStop('肉鸽循环次数应为1至100的整数')
        self.ledger = SettlementLedger()
        self.run_index = 1
        self.cached_frame = self.cached_screen = None
        self.active = False
        self.floor = None
        self.battle_kind = None
        self.battle_active = False
        self.battle_deploy_count = 0
        self.battle_deploy_current = 0
        self.deployment_next_at = None
        self.bounty_done = False
        self.shop = None
        self.shop_steps = 0
        self.shop_kind = None
        self.pending_action = None
        self.selected_candidate = None
        self.current = Observation()
        self.frame_revision = self.pause_revision()

    def pause_revision(self):
        return (getattr(getattr(self.f.task,'roguelike_control',None),'revision',0),
                getattr(getattr(self.f.task,'daily_focus',None),'paused_seconds',None))

    def refresh(self):
        # refresh/checkpoint itself may pause after the capture has completed.
        for _ in range(2):
            revision=self.pause_revision()
            self.f.refresh()
            if revision==self.pause_revision():
                self.frame_revision=revision
                return
        raise SafetyStop('Repeated focus pause during capture; no input sent')

    def screen(self):
        if self.cached_frame is not self.f.last_frame:
            self.cached_frame = self.f.last_frame
            self.cached_screen = RoguelikeScreen(self.f.task,self.f.last_frame,self.templates,
                special_shop=self.shop_kind=='SPECIAL_OPERATION_SHOP')
        return self.cached_screen

    def observe(self):
        self.f.checkpoint()
        if self.frame_revision!=self.pause_revision():self.refresh()
        screen=self.screen()
        observed=screen.observation
        self.f.vision.results.update(screen.matches)
        if observed.state==S.UNKNOWN:
            for state in (S.MAIN_MENU,S.NAV):
                if self.f.vision.page(state.value):
                    observed=Observation(state,evidence=('existing_'+state.value,))
                    break
        self.current=observed
        if self.battle_active:
            self.f.emit('roguelike_battle_observation',state=str(observed.state),battle_active=True,
                        battle_deploy_current=self.battle_deploy_current,battle_kind=self.battle_kind)
        return observed

    def publish(self,status='IN_PROGRESS',reason=''):
        self.f.state='ROGUELIKE_'+self.current.state.value
        data={'status':status,'run_index':self.run_index,'target_runs':self.target,
              'completed_runs':self.ledger.completed_runs,'state':self.current.state,
              'event':self.current.event,'selected_candidate':self.selected_candidate,
              'shop_refresh_used':bool(self.shop and self.shop.refresh_used),
              'shop_kind':self.shop.kind if self.shop else self.shop_kind,
              'battle_deploy_count':self.battle_deploy_count,'settlement_progress':self.ledger.progress,
              'battle_active':self.battle_active,'battle_deploy_current':self.battle_deploy_current,
              'pending_action':self.pending_action,'reason':reason}
        self.f.results['ROGUELIKE']=data
        self.f.task.info_set('肉鸽进度',f'第{self.run_index}/{self.target}轮；已完成{self.ledger.completed_runs}/{self.target}')
        self.f.task.info_set('当前状态',self.f.state)
        self.f.task.info_set('肉鸽结果',status+('：'+reason if reason else ''))
        self.f.emit('roguelike_status',roguelike_event=data['event'],
                    **{key:value for key,value in data.items() if key!='event'})

    def wait_observation(self,predicate,label,timeout=None,loading_timeout=None,resume_wait=False,started_at=None,retry_at=None,
                         loading_deadline=None):
        """One frame per poll; neither shared retries nor consecutive-frame gates."""
        deadline=self.f.clock()+(TIMING.ui_timeout if timeout is None else timeout)
        started=self.f.clock() if started_at is None else started_at
        loading=False
        revision=self.pause_revision()
        while True:
            polled_at=self.f.clock()
            self.refresh();o=self.observe()
            if revision!=self.pause_revision():
                if not resume_wait:raise Reobserve()
                # The one-shot input is already sent. Resume only this wait on
                # the fresh frame; never unwind into replaying its old click.
                revision=self.pause_revision()
                self.pending_action=None
                self.selected_candidate=None
            if predicate(o):return o
            if loading_timeout is not None:
                if o.state in (S.UNKNOWN,S.TRANSIENT) and not loading:
                    deadline=loading_deadline if loading_deadline is not None else self.f.clock()+loading_timeout
                    loading=True
                    loading_timeout=None  # Extend once, never reset a loading deadline.
            self.f.emit('roguelike_wait',reason=label+('加载' if o.state in (S.UNKNOWN,S.TRANSIENT) else ''),elapsed=round(self.f.clock()-started,1),
                        timeout=round(deadline-started,1),state=str(o.state))
            remaining=deadline-self.f.clock()
            if remaining<=0:raise RecognitionTimeout(label+': next_state='+str(o.state))
            # Capture/classification time belongs to the 150 ms observation
            # cadence; do not add another full interval after processing.
            interval=min(max(0,TIMING.poll-(self.f.clock()-polled_at)),remaining)
            if retry_at is not None and self.f.clock()<retry_at:
                interval=min(interval,retry_at-self.f.clock())
            self.f.delay(interval)

    def fail_action(self,state,action,attempt,started,reason):
        path=self.f.snapshot('roguelike_action_failed')
        if path is None and hasattr(self.f,'sequence') and hasattr(self.f,'logdir'):
            path=self.f.logdir/f'{self.f.sequence:04d}_{self.f.state}_roguelike_action_failed.png'
        self.f.emit('roguelike_action_failed',state=str(state),action=action,attempt=attempt,
                    elapsed_ms=round((self.f.clock()-started)*1000),screenshot=str(path) if path else None,
                    detection=reason,next_state=str(self.current.state))
        raise RecognitionTimeout(f'{state}: {action}: {reason} after {attempt} attempts')

    def wait_action_ready(self,state,resolve,label):
        """Titles may arrive before controls; no action is sent during this wait."""
        def ready(o):
            if o.state!=state:return False
            try:
                action=resolve(o)
                return bool(action and action.enabled)
            except DecisionUndefined:return False
        initial=self.current
        if initial.state in (state,S.UNKNOWN,S.TRANSIENT) and not ready(initial):
            waiting=self.f.clock()
            try:
                initial=self.wait_observation(
                    lambda o:o.state not in (state,S.UNKNOWN,S.TRANSIENT) or ready(o),
                    label+'按钮出现',timeout=TIMING.load_timeout)
            except RecognitionTimeout:
                self.fail_action(state,label,0,waiting,'button unavailable before input')
        return initial

    def send(self,state,resolve,next_check,label,before_input=None,timeout=None,retry=False,deadline=None,loading_timeout=None,
             mode=ActionMode.FAST_ACTION,attempt_offset=0,action_started=None,loading_deadline=None):
        """Dispatch an observed action. Safe retries belong to send_retryable_stable."""
        started=self.f.clock() if action_started is None else action_started
        if mode==ActionMode.RETRYABLE_STABLE_ACTION:retry=False
        if mode==ActionMode.ONE_SHOT_TRANSITION:
            retry=False
            if timeout is None:timeout=TIMING.post_battle_timeout
            self.refresh();self.observe()
            if self.wait_action_ready(state,resolve,label).state!=state:raise Reobserve()
        if self.current.state!=state:self.observe()
        action=None
        for attempt in range(1,TIMING.retries+2):
            reported_attempt=attempt+attempt_offset
            self.f.checkpoint()
            if self.frame_revision!=self.pause_revision():raise Reobserve()
            budget=TIMING.ui_timeout if timeout is None else timeout
            if deadline is not None:
                budget=min(budget,deadline-self.f.clock())
                if budget<=0:self.fail_action(state,label,attempt,started,'deployment time budget exhausted')
            paused=self.f.cfg['step_mode'] and not self.step_approved
            self.step_approved=False
            if paused:
                self.f.emit('step_pause',step=label)
                self.f.task.pause()
                self.f.checkpoint()
                self.step_approved=True
                raise Reobserve()
            hook=getattr(self.f.task,'before_click',None)
            if hook:hook()
            self.f.checkpoint()
            # An actual focus pause invalidates coordinates, including a pause
            # inside ensure_game(). Unchanged pages reuse the current frame.
            if self.frame_revision!=self.pause_revision():raise Reobserve()
            revision=self.pause_revision()
            o=self.current
            if o.state!=state:
                if next_check(o):return o
                self.fail_action(state,label,attempt,started,'page changed before input')
            action=resolve(o)
            if action is None or not action.enabled:
                self.fail_action(state,label,attempt,started,o.detail or 'predefined action unavailable')
            self.f.task.assert_foreground()
            self.f.checkpoint()
            validate=getattr(self.f.task,'validate_click_context',None)
            if validate:validate()
            self.f.checkpoint()
            if revision!=self.pause_revision():raise Reobserve()
            self.pending_action={'key':action.key,'state':str(state),'irreversible':action.irreversible}
            x,y,x2,y2=action.area
            screenshot=None
            if self.f.cfg['debug_mode']:
                screenshot=self.f.snapshot('roguelike_before_action')
                if screenshot is None and hasattr(self.f,'sequence'):
                    screenshot=self.f.logdir/f'{self.f.sequence:04d}_{self.f.state}_roguelike_before_action.png'
            self.f.emit('roguelike_input_intent',action=action.key,decision=label,attempt=reported_attempt,
                        action_mode=str(mode),
                        selected_candidate=self.selected_candidate,shop_refresh_used=bool(self.shop and self.shop.refresh_used),
                        area=action.area,point=(round((x+x2)/2),round((y+y2)/2)),run_index=self.run_index,
                        screenshot=str(screenshot) if screenshot else None)
            if mode==ActionMode.RETRYABLE_STABLE_ACTION:
                self.f.emit('roguelike_safe_action',action=action.key,attempt=reported_attempt,state=str(state),
                            area=action.area,point=(round((x+x2)/2),round((y+y2)/2)))
            self.f.checkpoint()
            if revision!=self.pause_revision():raise Reobserve()
            def click():
                guarded=getattr(self.f.task,'roguelike_guarded_click',None)
                if guarded:
                    return guarded(round((x+x2)/2),round((y+y2)/2),
                                   before_input=before_input if attempt==1 else None,
                                   after_sleep=0,down_time=.05)
                if before_input and attempt==1:before_input()
                dispatch=getattr(self.f.task,'roguelike_click',self.f.task.click)
                return dispatch(round((x+x2)/2),round((y+y2)/2),after_sleep=0,down_time=.05)
            control=getattr(self.f.task,'roguelike_control',None)
            if control:control.input(revision[0],click)
            else:click()
            self.f.emit('roguelike_input_dispatched',state=str(state),action=action.key,
                        attempt=reported_attempt,area=action.area,
                        point=(round((x+x2)/2),round((y+y2)/2)),
                        screenshot=str(screenshot) if screenshot else None)
            hook=getattr(self.f.task,'after_input',None)
            if hook:hook()
            self.f.delay(min(TIMING.first_check,budget))
            if revision!=self.pause_revision() and mode not in (ActionMode.ONE_SHOT_TRANSITION,ActionMode.RETRYABLE_STABLE_ACTION):raise Reobserve()
            try:
                result=self.wait_observation(next_check,label,
                    max(0,budget-TIMING.first_check),loading_timeout=loading_timeout,
                    resume_wait=mode in (ActionMode.ONE_SHOT_TRANSITION,ActionMode.RETRYABLE_STABLE_ACTION),
                    started_at=started,
                    loading_deadline=loading_deadline,
                    retry_at=started+TIMING.stable_retry_after if mode==ActionMode.RETRYABLE_STABLE_ACTION and not attempt_offset else None)
            except RecognitionTimeout:
                # Refresh once before deciding to repeat. A late result wins.
                self.refresh();self.observe()
                if next_check(self.current):result=self.current
                elif (retry and attempt<=TIMING.retries and self.current.state==state
                      and action.key!='refresh' and not action.key.startswith('exchange_')):
                    continue
                else:self.fail_action(state,action.key,reported_attempt,started,'no next state')
            self.pending_action=None
            self.f.emit('roguelike_action_timing',state=str(state),action=action.key,attempt=reported_attempt,
                        action_mode=str(mode),
                        elapsed_ms=round((self.f.clock()-started)*1000),next_state=str(result.state),
                        detection=result.event or str(result.state))
            return result

    def send_retryable_stable(self,state,resolve,next_check,label,before_input=None,timeout=None,
                             loading_timeout=None):
        """Three safe clicks at most; re-establish stability after animations.

        Page recognition owns coordinates, this helper owns permission, and
        wait_observation owns the remaining transition budget (including loads).
        """
        budget=max(TIMING.load_timeout if timeout is None else timeout,
                   TIMING.stable_retry_after*TIMING.safe_action_attempts)
        locate=resolve
        def resolve(o):
            if o.state!=state:return None
            try:
                found=locate(o)
                return found if found and found.enabled else None
            except DecisionUndefined:
                # A disappearing/ambiguous target revokes retry; it must not
                # interrupt the transition wait after input has already gone out.
                return None
        self.refresh();initial=self.observe()
        if next_check(initial) and initial.state not in (S.UNKNOWN,S.TRANSIENT):return initial
        initial=self.wait_action_ready(state,resolve,label)
        if initial.state!=state:
            # No input has been dispatched. An automatically arriving page owns
            # the next decision; do not force the stale action's next-state list.
            self.f.emit('roguelike_action_superseded',state=str(state),next_state=str(initial.state),attempt=0)
            return initial
        if next_check(initial):return initial
        button=resolve(initial)
        key=button.key if button else label
        recovery_signature=initial.signature()
        recovery_action=(button.key,button.text) if button else None
        revision=self.pause_revision()
        visual=getattr(self.screen(),'stable_action_visual',lambda:None)()
        started=attempt_started=None
        retry_allowed=True
        loaded=False
        recovery_token=recovery_visual=recovery_since=None

        def complete(o):
            return o.state not in (S.UNKNOWN,S.TRANSIENT) and next_check(o)

        def remaining():
            limit=max(budget,loading_timeout or 0) if loaded else budget
            return max(0,limit-(self.f.clock()-started))

        def sent():
            nonlocal started,attempt_started,recovery_since,recovery_token
            recovery_since=recovery_token=None
            attempt_started=self.f.clock()
            if started is None:
                started=attempt_started
                if before_input:before_input()

        def first_result(o,relocating=False):
            nonlocal retry_allowed,loaded,initial,button,visual,attempt_started
            nonlocal recovery_token,recovery_visual,recovery_since
            current_visual=getattr(self.screen(),'stable_action_visual',lambda:None)()
            changed=(visual is not None and current_visual is not None and
                     float(np.mean(np.abs(visual-current_visual)))>8)
            current_button=resolve(o) if o.state==state else None
            # Any observed motion in the full window revokes retry. Only the
            # final independent localization may correct a few pixels of box
            # estimation while the page's actual visual evidence stays stable.
            same_button=bool(button and current_button and
                (button.key,button.text,button.enabled)==
                (current_button.key,current_button.text,current_button.enabled) and
                max(abs(a-b) for a,b in zip(button.area,current_button.area))<=
                (max(6,(button.area[2]-button.area[0])*.03) if relocating else 0))
            progress=(o.state!=state or o.signature()!=initial.signature() or
                      not same_button or self.pause_revision()!=revision or changed or complete(o))
            if retry_allowed and progress:
                retry_allowed=False
                self.f.emit('roguelike_safe_action_wait',action=key,state=str(o.state),
                            elapsed=round(self.f.clock()-started,3),result='progress',retry_revoked=True,
                            state_changed=o.state!=state,signature_changed=o.signature()!=initial.signature(),
                            target_changed=not same_button,visual_changed=changed,
                            pause_changed=self.pause_revision()!=revision)
            if o.state in (S.UNKNOWN,S.TRANSIENT):loaded=True
            if complete(o):return True
            if not retry_allowed:
                if (o.state==state and o.signature()==recovery_signature and current_button and
                        (current_button.key,current_button.text)==recovery_action and
                        self.pause_revision()==revision):
                    content=getattr(self.screen(),'overlay_content',lambda:'')() if state==S.OVERLAY else ''
                    token=(o.signature(),current_button.area,content)
                    moving=(recovery_visual is not None and current_visual is not None and
                            float(np.mean(np.abs(recovery_visual-current_visual)))>8)
                    if token!=recovery_token or moving:
                        recovery_token=token;recovery_visual=current_visual;recovery_since=self.f.clock()
                    elif self.f.clock()-recovery_since>=TIMING.stable_retry_after-1e-9:
                        # The original safe target must be independently stable
                        # again. Loading itself never permits another input.
                        initial=o;button=current_button;visual=current_visual
                        retry_allowed=True;attempt_started=self.f.clock()-TIMING.stable_retry_after
                        self.f.emit('roguelike_safe_action_reconfirmed',action=key,
                                    elapsed=round(self.f.clock()-started,3),stable_for=TIMING.stable_retry_after)
                else:recovery_token=recovery_since=None
            return complete(o) or (retry_allowed and button is not None and
                self.f.clock()-attempt_started>=TIMING.stable_retry_after-1e-9)

        for attempt in range(1,TIMING.safe_action_attempts+1):
            try:
                self.send(state,resolve,first_result,label,before_input=sent,
                          mode=ActionMode.RETRYABLE_STABLE_ACTION,retry=False,
                          attempt_offset=attempt-1,action_started=started,
                          timeout=budget if started is None else remaining(),loading_timeout=loading_timeout,
                          loading_deadline=(self.f.clock() if started is None else started)+max(budget,loading_timeout or 0))
            except Reobserve:
                if attempt==1:raise
                # A pause before a prepared retry invalidates its input, but
                # must never restart the whole action on resume.
                self.pending_action=None
                self.cached_frame=self.cached_screen=None
                self.f.emit('roguelike_safe_action_wait',action=key,state=str(self.current.state),
                            elapsed=round(self.f.clock()-started,3),result='progress',retry_revoked=True)
                try:
                    self.wait_observation(complete,label+' transition',timeout=remaining(),resume_wait=True,
                        loading_timeout=loading_timeout,loading_deadline=started+max(budget,loading_timeout or 0))
                except RecognitionTimeout:
                    self.fail_action(state,key,attempt-1,started,'no next state after pause')
                return self.current
            if complete(self.current):return self.current
            self.f.emit('roguelike_safe_action_wait',action=key,state=str(state),
                        attempt=attempt,elapsed=round(self.f.clock()-attempt_started,3),
                        result='unchanged',retry_revoked=False)
            if attempt==TIMING.safe_action_attempts:
                self.fail_action(state,key,attempt,started,'unchanged after final safe click')
            while True:
                self.cached_frame=self.cached_screen=None
                self.refresh();o=self.observe()  # Independent capture, even after recovered stability.
                first_result(o,relocating=True)
                if complete(o):return o
                if retry_allowed:
                    if remaining()<=0:self.fail_action(state,key,attempt,started,'transition budget exhausted before retry')
                    fresh=resolve(o)
                    self.f.emit('roguelike_safe_action_relocate',action=key,old_area=button.area,new_area=fresh.area)
                    initial=o;button=fresh
                    visual=getattr(self.screen(),'stable_action_visual',lambda:None)()
                    break
                try:
                    self.wait_observation(first_result,label+' transition',timeout=remaining(),resume_wait=True,
                        loading_timeout=loading_timeout,loading_deadline=started+max(budget,loading_timeout or 0))
                except RecognitionTimeout:
                    self.fail_action(state,key,attempt,started,'no next state')
        return self.current

    def start_battle(self):
        def sent():self.battle_active=True
        self.send_retryable_stable(S.BATTLE_PREP,lambda o:o.action('battle_start'),
            lambda o:o.state!=S.BATTLE_PREP,'开始战斗',sent)
        if self.current.state==S.ENCOUNTER:self.battle_active=False

    def parent_action(self,o,key):
        if key=='nav_tab':
            area=self.screen().find('nav_tab')
            return RogueAction(key,area,'局域模拟',irreversible=False) if area else None
        box=self.f.vision.find(key)
        return RogueAction(key,(box.x,box.y,box.x+box.width,box.y+box.height),irreversible=False) if box else None

    def dismiss_continue_overlay(self):
        self.refresh();o=self.observe()
        if o.state!=S.OVERLAY or not o.action('dismiss'):
            raise SafetyStop('未确认允许关闭的肉鸽 overlay，拒绝空白点击')
        identity=lambda:getattr(self.screen(),'overlay_identity',lambda:'')()
        previous=identity()
        candidate='';candidate_frame=None;candidate_at=None
        def advanced(s):
            nonlocal candidate,candidate_frame,candidate_at
            if s.state!=S.OVERLAY:
                candidate='';candidate_at=None
                return s.state not in (S.START,S.UNKNOWN,S.TRANSIENT)
            current=identity()
            frame=self.f.last_frame
            if not previous or not current or current==previous or not s.action('dismiss'):
                candidate='';candidate_at=None
                return False
            if current!=candidate:
                candidate=current;candidate_frame=frame;candidate_at=self.f.clock()
                return False
            # A distinct readable overlay, observed again on a fresh frame,
            # completes this action. The dispatcher owns its next dismissal.
            if frame is not candidate_frame and self.f.clock()-candidate_at>=TIMING.poll:
                self.f.emit('roguelike_overlay_advanced',previous=previous,current=current)
                return True
            return False
        return self.send_retryable_stable(S.OVERLAY,lambda s:s.action('dismiss'),
            advanced,'空白继续/关闭',
            timeout=TIMING.animation_timeout,loading_timeout=TIMING.load_timeout)

    def select_choice(self,o,members=False):
        # Decide once from fresh pixels. A reclassified page goes back to the
        # dispatcher; SELECT and CONFIRM never carry coordinates across pages.
        state=o.state
        self.refresh();o=self.observe()
        if o.state!=state:return
        chooser=choose_member if members else first_choice
        if not o.candidates:raise SafetyStop('选项卡缺可靠证据')
        best=chooser(o.candidates)
        if best is None:
            raise SafetyStop('免费队员选择页没有可靠可选候选，未选择或确认')
        selected=best.index
        reason=(member_reason(best) or 'first_plain_blue_free_selection') if members else 'first_option'
        self.selected_candidate={'index':selected,'reason':reason}
        if members:
            self.f.emit('roguelike_member_decision',selected_candidate=selected,reason=reason,
                        candidates=[{key:getattr(c,key) for key in ('index','quality','glowing','selected','selectable')}
                                    for c in o.candidates])
        def locate(s):
            c=next((c for c in s.candidates if c.index==selected and c.selectable),None)
            return RogueAction('select_'+str(selected),c.area,irreversible=False) if c else None
        def is_selected(s):
            return s.state==o.state and any(c.index==selected and c.selected for c in s.candidates)
        if not best.selected:
            self.send_retryable_stable(o.state,locate,lambda s:is_selected(s) or s.state!=o.state,
                      ('免费三蓝选择第一项' if reason=='first_plain_blue_free_selection' else '队员 金>发亮(含蓝)>紫>薇拉') if members else '选择第一项')
        if self.current.state!=o.state:return
        self.send_retryable_stable(o.state,lambda s:s.action('confirm') if is_selected(s) else None,
                  lambda s:s.state!=o.state,'确认已选队员' if members else '确认当前选择',
                  timeout=TIMING.reward_timeout,loading_timeout=TIMING.load_timeout)

    def shop_step(self,o):
        if self.shop is None:
            if self.shop_kind is None:
                self.shop_kind='SPECIAL_OPERATION_SHOP' if self.battle_kind=='special' else 'NORMAL_BATTLE_SHOP'
            self.shop=ShopVisit(self.shop_kind)
        self.shop_steps+=1
        if self.shop_steps>24:raise SafetyStop('本次商店超过24次动作安全上限')
        if self.shop.kind=='NORMAL_BATTLE_SHOP' and not self.shop.fallback_used and o.currency is None:
            # One immediate fresh-frame read, without adding a retry timer.
            self.refresh();o=self.observe()
            if o.state!=S.SHOP:return
            if o.currency is None:
                self.f.snapshot('shop_currency_unknown')
                raise SafetyStop('普通商店：青色货币余额连续两帧未知，未购买/刷新')
        def decide(s):
            return shop_decision(s.candidates,self.shop,refresh_available=bool(s.action('refresh')),
                                 currency=s.currency,refresh_cost=s.refresh_cost,
                                 eligible_before_refresh=self.f.cfg.get('eligible_before_refresh',False))
        decision=decide(o)
        self.f.emit('roguelike_shop_decision',decision=decision.action,reason=decision.reason,
                    currency=o.currency,refresh_cost=o.refresh_cost,refresh_used=self.shop.refresh_used,
                    candidates=[{key:getattr(c,key) for key in ('index','quality','glowing','role','identity','price','selectable')} for c in o.candidates],
                    selected_candidate=decision.candidate.index if decision.candidate else None,
                    reserve_after_purchase=decision.reserve_after_purchase,
                    fallback_used=self.shop.fallback_used,
                    shop_refresh_used=self.shop.refresh_used,run_index=self.run_index)
        if decision.action=='back':
            def back(s):
                if decide(s).action!='back':raise Reobserve()
                return s.action('back')
            self.send_retryable_stable(S.SHOP,back,lambda s:s.state==S.MAP,'商店返回：'+decision.reason)
            self.shop_kind=None;self.shop=None;self.shop_steps=0
        elif decision.action=='refresh':
            before=tuple((c.fingerprint,c.quality,c.glowing,c.role,c.price,c.selectable) for c in o.candidates)
            def refreshed(s):
                return s.state==S.SHOP and s.currency is not None and (
                    s.currency!=o.currency or (s.refresh_cost is not None and s.refresh_cost!=o.refresh_cost) or
                    tuple((c.fingerprint,c.quality,c.glowing,c.role,c.price,c.selectable) for c in s.candidates)!=before)
            def refresh_action(s):
                if decide(s).action!='refresh':raise Reobserve()
                return s.action('refresh')
            self.send(S.SHOP,refresh_action,refreshed,
                      '无可购买高档且余额至少7，刷新一次',before_input=self.shop.reserve_refresh,
                      mode=ActionMode.ONE_SHOT_TRANSITION,timeout=TIMING.ui_timeout,
                      loading_timeout=TIMING.load_timeout)
        else:
            index=decision.candidate.index
            self.selected_candidate={'index':index,'reason':decision.reason}
            def exchange(s):
                current=decide(s)
                if current.action!='exchange' or current.candidate.index!=index:return None
                if current.candidate.fingerprint!=decision.candidate.fingerprint:return None
                return s.action('exchange_'+str(index))
            self.send(S.SHOP,exchange,lambda s:s.state==S.OVERLAY,'兑换 '+decision.reason,
                      before_input=lambda:self.shop.reserve_purchase(decision.candidate),
                      mode=ActionMode.ONE_SHOT_TRANSITION,timeout=TIMING.reward_timeout)
            self.shop.record_purchase(decision.candidate)

    def deployment_click(self,point,key,label,on_sent=None):
        """Short deployment input, using the unchanged control/focus lifecycle."""
        self.f.checkpoint()
        revision=self.pause_revision()
        if self.frame_revision!=revision:raise Reobserve()
        if self.f.cfg['step_mode'] and not self.step_approved:
            self.f.emit('step_pause',step='特殊部署物：选卡、同点双击')
            self.f.task.pause();self.f.checkpoint();self.step_approved=True
            raise Reobserve()
        hook=getattr(self.f.task,'before_click',None)
        if hook:hook()
        self.f.checkpoint();self.f.task.assert_foreground()
        validate=getattr(self.f.task,'validate_click_context',None)
        if validate:validate()
        self.f.checkpoint()
        if revision!=self.pause_revision():raise Reobserve()
        x,y=point
        self.pending_action={'key':key,'point':point}
        screenshot=None
        if self.f.cfg.get('debug_mode'):
            screenshot=self.f.snapshot('roguelike_deployment_before_action')
            if screenshot is None and hasattr(self.f,'sequence'):
                screenshot=self.f.logdir/f'{self.f.sequence:04d}_{self.f.state}_roguelike_deployment_before_action.png'
        self.f.emit('roguelike_input_intent',action=key,decision=label,area=(x-1,y-1,x+1,y+1),
                    point=point,battle_active=self.battle_active,screenshot=str(screenshot) if screenshot else None,
                    run_index=self.run_index,placement=self.battle_deploy_current+1)
        def click():
            dispatch=getattr(self.f.task,'roguelike_click',self.f.task.click)
            dispatch(x,y,after_sleep=0,down_time=.05)
            if on_sent:on_sent()
        control=getattr(self.f.task,'roguelike_control',None)
        if control:control.input(revision[0],click)
        else:click()
        self.f.emit('roguelike_input_dispatched',action=key,point=point,battle_active=self.battle_active,
                    battle_deploy_current=self.battle_deploy_current,screenshot=str(screenshot) if screenshot else None)
        hook=getattr(self.f.task,'after_input',None)
        if hook:hook()
        self.pending_action=None

    def deployment_interval(self):
        if self.deployment_next_at is None:return
        revision=self.pause_revision()
        # flow.clock excludes paused time. Keep the deadline across Reobserve;
        # never repeat already sent input after an interrupted gameplay interval.
        while self.f.clock()<self.deployment_next_at:
            self.f.delay(min(TIMING.battle_poll,self.deployment_next_at-self.f.clock()))
            self.refresh();self.observe()
            if revision!=self.pause_revision():raise Reobserve()
            self.wait_battle_context('部署镜头变化')
            if self.current.state in BATTLE_EXIT_STATES:break
        self.deployment_next_at=None
        self.refresh();self.observe()
        if revision!=self.pause_revision():raise Reobserve()

    def finish_battle(self,o):
        if self.battle_active and o.state in BATTLE_EXIT_STATES:
            self.battle_active=False
            self.shop_kind=('SPECIAL_OPERATION_SHOP' if self.battle_kind=='special' else
                            'NORMAL_BATTLE_SHOP' if self.battle_kind=='encounter' else None)
            self.f.emit('roguelike_battle_finished',run_index=self.run_index,source=self.battle_kind)

    def wait_battle_context(self,reason='战斗镜头/动画变化',timeout=None):
        started=self.f.clock()
        budget=TIMING.deployment_camera_timeout if timeout is None else timeout
        if self.current.state not in (S.UNKNOWN,S.TRANSIENT):
            self.finish_battle(self.current)
            return self.current
        self.f.snapshot('battle_context_before_wait')
        while True:
            o=self.current
            self.f.emit('roguelike_battle_context',state=str(o.state),battle_active=self.battle_active,
                        battle_deploy_current=self.battle_deploy_current,reason=reason,
                        elapsed=round(self.f.clock()-started,3),timeout=budget)
            if o.state==S.BATTLE or o.state in BATTLE_EXIT_STATES:
                self.finish_battle(o)
                self.f.snapshot('battle_context_recovered')
                return o
            if o.state not in (S.UNKNOWN,S.TRANSIENT):
                raise SafetyStop('Battle: unexpected state '+str(o.state))
            if self.f.clock()-started>=budget:
                self.f.snapshot('battle_context_timeout')
                raise SafetyStop('Battle: camera/animation recognition timeout')
            self.f.delay(min(TIMING.poll,budget-(self.f.clock()-started)))
            self.refresh();self.observe()

    def battle_step(self,o):
        self.current=o
        self.battle_active=True
        self.deployment_interval()
        self.wait_battle_context()
        if self.current.state!=S.BATTLE:return
        if self.battle_deploy_current>=TIMING.special_placements:
            outcome=self.wait_battle()
        else:
            self.refresh();o=self.observe()
            o=self.wait_battle_context()
            if o.state!=S.BATTLE:return
            card=self.screen().find_special_deploy_card_fast()
            if card is None:
                # A missing special card is not an ordinary-member fallback or
                # a zero-attempt failure. One fresh look, then watch the battle.
                self.refresh();o=self.observe()
                o=self.wait_battle_context()
                if o.state!=S.BATTLE:return
                card=self.screen().find_special_deploy_card_fast()
            if card is None:
                self.f.emit('roguelike_deployment',message='special card absent; waiting for battle')
                outcome=self.wait_battle()
            else:
                started=self.f.clock();revision=self.pause_revision()
                point=(round((card.area[0]+card.area[2])/2),round((card.area[1]+card.area[3])/2))
                self.f.emit('roguelike_deployment',message='special card located',source=card.text,area=card.area,card_point=point)
                self.deployment_click(point,'special_card','BattleDeployment: card clicked')
                self.f.delay(TIMING.deployment_select_wait)
                self.refresh();outcome=self.observe()
                if revision!=self.pause_revision():raise Reobserve()
                outcome=self.wait_battle_context('特殊卡选择后镜头变化')
                grounds=[]
                for attempt in range(1):
                    if outcome.state!=S.BATTLE:break
                    ground=self.screen().choose_empty_ground(exclude=grounds)
                    if ground is None:
                        self.f.emit('roguelike_deployment',message='ground pool occupied; wait without clicking units',
                                    card_point=point,ground_points=grounds,attempt=attempt)
                        break
                    tile,score=ground;grounds.append(tile)
                    self.f.emit('roguelike_deployment',message='ground selected',point=tile,score=score,attempt=attempt+1)
                    self.deployment_click(tile,'special_ground','BattleDeployment: ground click 1/2')
                    self.f.delay(TIMING.deployment_double_gap)
                    if revision!=self.pause_revision():raise Reobserve()
                    def sent():
                        if attempt==0:
                            self.battle_deploy_count+=1;self.battle_deploy_current+=1
                        self.deployment_next_at=self.f.clock()+TIMING.deploy_interval
                    self.deployment_click(tile,'special_ground','BattleDeployment: ground click 2/2',sent)
                    self.f.emit('roguelike_deployment',message='double click sent',placement=self.battle_deploy_current,
                                total=TIMING.special_placements,attempt=attempt+1,card_point=point,ground_points=grounds,
                                elapsed_ms=round((self.f.clock()-started)*1000))
                    self.f.delay(TIMING.first_check)
                    self.refresh();outcome=self.observe()
                    if revision!=self.pause_revision():raise Reobserve()
                    outcome=self.wait_battle_context('部署镜头变化')
                    if outcome.state!=S.BATTLE:break
                    # A special placement is one-shot, even without immediate feedback.
                self.step_approved=False
                if self.deployment_next_at is not None:
                    self.f.emit('roguelike_deployment',message='waiting 5.0s before next placement',seconds=TIMING.deploy_interval)
                    self.deployment_interval();outcome=self.current
                elif outcome.state==S.BATTLE:
                    outcome=self.wait_battle(check_card=False)
        self.finish_battle(outcome)
        self.publish()

    def wait_battle(self,check_card=True):
        deadline=self.f.clock()+TIMING.battle_timeout
        revision=self.pause_revision()
        while self.f.clock()<deadline:
            self.refresh();o=self.observe()
            if revision!=self.pause_revision():raise Reobserve()
            if o.state in (S.UNKNOWN,S.TRANSIENT):
                o=self.wait_battle_context(timeout=TIMING.post_battle_timeout)
            if o.state in BATTLE_EXIT_STATES:return o
            if o.state!=S.BATTLE:raise SafetyStop('Battle: unexpected state '+str(o.state))
            if (o.state==S.BATTLE and check_card and self.battle_deploy_current<TIMING.special_placements
                    and self.screen().find_special_deploy_card_fast() is not None):return o
            self.f.emit('roguelike_wait',reason='战斗结果' if o.state==S.BATTLE else '战斗结束动画',
                        elapsed=round(self.f.clock()-(deadline-TIMING.battle_timeout),1),
                        timeout=TIMING.battle_timeout,state=str(o.state))
            self.f.delay(TIMING.battle_poll)
        self.f.snapshot('battle_timeout')
        raise SafetyStop('Battle: waiting for result timed out; special placements='+str(self.battle_deploy_current))

    def settlement_step(self,o):
        # A visible settlement stage determines its next action, including a
        # fresh takeover. Completion still requires observing the final idle START.
        self.ledger.progress={S.START:0,S.SETTLEMENT_CONFIRM:1,S.RESULT_STATS:2,
                              S.RESULT_TEAM:3,S.RESULT:4}[o.state]
        key=self.ledger.next_key()
        expected=(S.SETTLEMENT_CONFIRM,S.RESULT_STATS,S.RESULT_TEAM,S.RESULT,S.START)[self.ledger.progress]
        if o.state==S.RESULT:self.f.emit('roguelike_result',run_index=self.run_index,**o.result_numbers)
        result=self.send(o.state,lambda s:s.action(key),lambda s:s.state==expected and
                         (expected!=S.START or (s.action('start') and not s.action('terminate'))),
                         '结算 '+key,mode=ActionMode.ONE_SHOT_TRANSITION,
                         timeout=TIMING.settlement_timeout if o.state==S.SETTLEMENT_CONFIRM else TIMING.ui_timeout,
                         loading_timeout=TIMING.animation_timeout if self.f.cfg.get('native_settlement_animation',False) else None)
        count_completion=getattr(self,'count_current_settlement',True)
        self.ledger.verify(key,result,count_completion=count_completion)
        if self.ledger.progress==5:
            self.active=False
            if count_completion:
                self.f.emit('roguelike_round_complete',run_index=self.run_index,completed_runs=self.ledger.completed_runs)
            else:
                self.f.emit('native_prior_result_closed',completed_runs=self.ledger.completed_runs)
                self.ledger.progress=0;self.count_current_settlement=True
        self.publish()

    def step(self,o):
        if self.terminal:raise SafetyStop('旧肉鸽 runner 已终止；请启动“继续肉鸽”')
        self.current=o
        started=self.f.clock()
        try:
            self.f.checkpoint()
            if self.frame_revision!=self.pause_revision():raise Reobserve()
            self._step(o)
        except Reobserve:
            self.pending_action=None
            self.selected_candidate=None
            self.cached_frame=self.cached_screen=None
            self.refresh();self.observe()
            self.f.emit('roguelike_resumed',state=str(self.current.state),discarded='observation/action/candidate/deployment coordinates')
        except DecisionUndefined as error:raise SafetyStop(str(error)) from error
        finally:
            self.f.emit('roguelike_state_timing',state=str(o.state),category=state_class(o.state,o.event,self.shop_kind=='SPECIAL_OPERATION_SHOP'),
                        elapsed_ms=round((self.f.clock()-started)*1000),next_state=str(self.current.state))

    def _step(self,o):
        self.publish()
        state=o.state
        self.finish_battle(o)
        if self.battle_active and state in (S.UNKNOWN,S.TRANSIENT):
            self.wait_battle_context()
            return
        if state==S.MAIN_MENU:
            self.send_retryable_stable(state,lambda s:self.parent_action(s,'main_nav'),lambda s:s.state in (S.NAV,S.ENTRY),'进入导航')
        elif state==S.NAV:
            self.send_retryable_stable(state,lambda s:self.parent_action(s,'nav_tab'),lambda s:s.state==S.ENTRY,'切换局域模拟')
        elif state==S.ENTRY:
            self.send_retryable_stable(state,lambda s:s.action('enter'),
                lambda o:o.state in ENTRY_SUCCESS_STATES,'进入副本 / 开始挑战')
        elif state==S.START:
            if o.action('terminate'):
                if self.bounty_done:self.settlement_step(o)
                else:
                    self.send_retryable_stable(state,lambda s:s.action('continue_run'),lambda s:s.state in (S.MAP,S.OVERLAY),
                              '继续已有挑战')
            else:
                if self.active:raise SafetyStop('未结算却返回开始页，完成次数不增加')
                self.send_retryable_stable(state,lambda s:s.action('start'),
                          lambda s:s.state in (S.OVERLAY,S.MAP,S.EVENT,S.CHOICE),
                          '开始新一轮挑战',before_input=lambda:setattr(self,'active',True))
        elif state==S.OVERLAY:self.dismiss_continue_overlay()
        elif state==S.TRANSIENT:
            self.wait_observation(lambda s:s.state not in (S.UNKNOWN,S.TRANSIENT),'已知动画结束后重新观察',timeout=TIMING.animation_timeout)
        elif state==S.MAP:
            self.floor=o.floor
            if self.shop_kind is not None and self.shop is None:
                self.wait_observation(lambda s:s.state in (S.OVERLAY,S.TRANSIENT,S.SHOP,S.CHOICE),'战后自动奖励/层切换/商店',timeout=TIMING.animation_timeout)
            elif o.event=='bounty_complete' or self.bounty_done:
                self.bounty_done=True
                self.send_retryable_stable(state,lambda s:s.action('back'),lambda s:s.state==S.START,'悬赏完成后返回挑战页')
            else:
                self.send_retryable_stable(state,lambda s:choose_route(s.actions),lambda s:s.state in
                          (S.EVENT,S.CHOICE,S.OUTPOST,S.ORDER,S.ENCOUNTER,S.SPECIAL_OPERATION,S.ACCIDENT,S.SEARCH,S.BOUNTY,S.OVERLAY),
                          '当前亮起节点；上下分叉选择下方')
        elif state in (S.EVENT,S.ENCOUNTER,S.SPECIAL_OPERATION,S.ACCIDENT):
            if state==S.ENCOUNTER:self.battle_kind='encounter'
            if state==S.SPECIAL_OPERATION:self.battle_kind='special'
            self.send_retryable_stable(state,lambda s:unique_action(s.actions),lambda s:s.state in
                      (S.CHOICE,S.OUTPOST,S.BATTLE_PREP,S.OVERLAY,S.MAP),'已确认事件的唯一 action')
        elif state==S.ORDER:
            self.send(state,lambda s:order_action(s.actions),lambda s:s.state in (S.EVENT,S.OUTPOST,S.OVERLAY),
                      '火种调令',mode=ActionMode.ONE_SHOT_TRANSITION,timeout=TIMING.reward_timeout)
        elif state in (S.CHOICE,S.OUTPOST):
            self.select_choice(o,state==S.OUTPOST)
        elif state==S.BATTLE_PREP:
            self.battle_deploy_current=0;self.deployment_next_at=None
            self.start_battle()
        elif state==S.BATTLE:self.battle_step(o)
        elif state==S.BATTLE_RESULT:
            self.send(state,lambda s:unique_action(s.actions),lambda s:s.state in (S.OVERLAY,S.SHOP,S.CHOICE),
                      '行动胜利确认后观察奖励',mode=ActionMode.ONE_SHOT_TRANSITION)
        elif state==S.SHOP:self.shop_step(o)
        elif state==S.SEARCH:
            reward=bool(o.action('action_0'))
            send=self.send if reward else self.send_retryable_stable
            options={'mode':ActionMode.ONE_SHOT_TRANSITION,'timeout':TIMING.reward_timeout} if reward else {}
            send(state,lambda s:search_action(s.actions),
                      lambda s:s.state==S.OVERLAY or (not reward and s.state==S.MAP),
                      '当前可见小心陷阱 / 普通唯一 action',**options)
        elif state==S.BOUNTY:
            self.send(state,lambda s:bounty_action(s.actions),lambda s:s.state==S.MAP,
                      '悬赏第二项：实训计划',mode=ActionMode.ONE_SHOT_TRANSITION,timeout=TIMING.reward_timeout)
            self.bounty_done=True
        elif state in (S.SETTLEMENT_CONFIRM,S.RESULT_STATS,S.RESULT_TEAM,S.RESULT):
            self.settlement_step(o)
        else:raise SafetyStop(o.detail or '肉鸽页面未可靠识别')

    def run(self):
        if self.terminal:raise SafetyStop('旧肉鸽 runner 已终止；请启动“继续肉鸽”')
        try:
            self.f.checkpoint()
            try:
                initial=self.wait_observation(lambda o:o.state!=S.UNKNOWN,'识别肉鸽启动页面')
            except Reobserve:
                self.refresh();initial=self.observe()
                if initial.state==S.UNKNOWN:
                    raise SafetyStop('恢复后启动页面未识别；未执行旧动作')
            if not self.resume and (initial.state not in (S.MAIN_MENU,S.NAV,S.ENTRY,S.START) or initial.action('terminate')):
                raise SafetyStop('肉鸽仅从导航/空闲挑战入口启动；不恢复中途状态')
            for _ in range(self.target-self.ledger.completed_runs):
                self.run_index=self.ledger.completed_runs+1
                if self.ledger.progress==5:self.ledger.begin_next_run()
                self.floor=None;self.bounty_done=False;self.battle_kind=None
                self.battle_deploy_count=0;self.selected_candidate=None
                self.battle_deploy_current=0;self.deployment_next_at=None
                self.battle_active=False;self.shop_kind=None;self.shop=None;self.shop_steps=0
                deadline=self.f.clock()+1200
                for _ in range(200):
                    self.f.checkpoint()
                    if self.f.clock()>deadline:raise SafetyStop('本轮超过20分钟安全上限')
                    if self.battle_active and self.current.state in (S.UNKNOWN,S.TRANSIENT):
                        self.wait_battle_context()
                    o=self.current if self.current.state!=S.UNKNOWN else self.wait_observation(lambda s:s.state!=S.UNKNOWN,'观察当前肉鸽页面')
                    self.step(o)
                    if self.ledger.progress==5:break
                else:raise SafetyStop('本轮超过200次状态转移安全上限')
            self.publish('SUCCESS')
        except Exception as error:
            control=getattr(self.f.task,'roguelike_control',None)
            reason=('肉鸽已终止：'+control.state if control and control.state in control.TERMINAL else
                    'F8 紧急停止：本次任务已取消' if self.f.stop.is_set() else str(error))
            self.publish('PARTIAL' if self.ledger.completed_runs else 'STOPPED',reason)
            if isinstance(error,SafetyStop):raise
            raise SafetyStop(reason) from error
        finally:self.terminal=True
