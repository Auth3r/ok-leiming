"""One constraint-driven dispatch flow with current-page verification."""
import re
import cv2
import numpy as np
from ok import Box
from src.workflow import SafetyStop,RecognitionTimeout,InsufficientResource
from src.dispatch_policy import Candidate,Plan,DispatchUnreadable,choose,first_available,TACTIC
from src.dispatch_screen import DispatchScreen,ROSTER,read_dispatch_counter,catalog
from src.dispatch_filter import TacticFilter
from dataclasses import replace,asdict


def rect(values):
    return tuple(round(v*1.875) for v in values)


ROWS = ((245,365),(365,479),(479,591),(591,704))
CARDS = ((1515,265,1652,476),(1673,265,1809,476),(1830,265,1967,476))

class DispatchBlocked(InsufficientResource):
    """The prescribed team cannot satisfy this commission."""


def conditions_met(vision):
    """Every mandatory row must pass; optional reward is never a submit gate."""
    checked=0
    for top,bottom in ROWS:
        if vision.find('dispatch_green',rect((1435,top,1488,bottom))):
            checked+=1
        elif not vision.find('dispatch_empty',rect((1335,top,1405,bottom))):
            return False
    return bool(checked and vision.find('dispatch_ready'))


class Dispatch:
    def __init__(self, flow):
        self.f=flow
        self.v=flow.vision
        self.done=0
        self.skipped=[]

    def scroll_list(self,count,label):
        from src.cooperative import FocusChanged
        from src.roguelike_control import Reobserve
        f=self.f
        for attempt in range(2):
            try:
                before=getattr(f.task,'before_click',None)
                if before:before()
                f.require('BLACK_SHIELD');f.task.assert_foreground();f.checkpoint()
                f.emit('scroll',step=label,x=3000,y=1300,count=count)
                f.task.scroll(3000,1300,count)
                break
            except (FocusChanged,Reobserve):
                if attempt:raise
                f.emit('scroll_reobserve',step=label)
                f.checkpoint()
        after=getattr(f.task,'after_input',None)
        if after:after()
        f.delay(f.cfg['click_min_wait']);f.require('BLACK_SHIELD')

    def complete(self):
        return self.v.page('DISPATCH') and self.v.has('dispatch_recall','dispatch_assigned')

    def guard(self):
        if getattr(self,'requirements',None) is not None:
            screen=DispatchScreen(self.f)
            if (replace(screen.requirements,selected=0)!=self.requirements or
                    screen.mission_identity()!=self.mission or
                    screen.selected!=self.expected_selected or
                    not screen.feedback(False)['allowed']):
                raise SafetyStop('职业、战术、人数、实际战力或按钮反馈变化，拒绝派遣')
            if self.requirements.same_tactic==4 and (
                    screen.selected!=4 or len(self.picked_members)!=4 or self.filter.label()!=TACTIC):
                raise SafetyStop('四人乌斯怀亚选择回执或筛选变化，拒绝派遣')
            return
        if not conditions_met(self.v):
            raise SafetyStop('派遣条件变化：必选条件未满足，拒绝派遣')

    def power(self,roi):
        x,y,x2,y2=roi
        boxes=self.f.task.ocr(x=(x+100)/3840,y=(y2-90)/2160,to_x=x2/3840,to_y=(y2+18)/2160,
                              frame=self.f.task.frame,threshold=.9)
        values=[int(b.name.strip()) for b in boxes if re.fullmatch(r'\d{3,6}',b.name.strip())]
        return values[0] if len(values)==1 and values[0]>0 else None

    @staticmethod
    def portrait(frame,roi):
        # Exclude tactical icons, rarity border, and the power label.
        x,y,x2,y2=roi
        return frame[y+25:y+220,x+35:x2-25].copy()

    @staticmethod
    def similarity(a,b):
        if not a.size or a.shape!=b.shape or a.std()<2 or b.std()<2:return 0.0
        # OCR line baselines jitter by a few native pixels. Compare the portrait
        # within a bounded translation, retaining faces rather than card labels.
        margin=15
        template=a[margin:-margin,margin:-margin]
        return float(cv2.minMaxLoc(cv2.matchTemplate(b,template,cv2.TM_CCOEFF_NORMED))[1])

    @staticmethod
    def selected_similarity(a,b):
        # The current selection check/veil covers the middle/lower face. Its
        # upper portrait remains visible: compare that same observed identity,
        # with the existing threshold/translation tolerance, never the check.
        return Dispatch.similarity(a[:70],b[:70])

    def slots(self):
        x,y,x2,y2=rect((1175,905,1480,1035))
        return self.f.task.frame[y:y2,x:x2].copy()

    @staticmethod
    def same_slot_identity(before,after):
        if before is None or after is None or before[:2]!=after[:2]:return False
        if len(before[2])!=len(after[2]):return False
        for (filled,a),(now_filled,b) in zip(before[2],after[2]):
            if filled!=now_filled:return False
            if not filled:continue  # Both independently match the empty-slot asset.
            if a.shape!=b.shape or min(a.shape[:2])<20:return False
            # Verify each existing portrait rather than RGB brightness. The
            # animated sheen does not change occupancy or character identity.
            template=a[4:-4,4:-4]
            if cv2.minMaxLoc(cv2.matchTemplate(b,template,cv2.TM_CCOEFF_NORMED))[1]<.92:return False
        return True

    def pick_candidate(self,index,roi,require_power=True,expected_selected=None):
        from src.cooperative import FocusChanged
        from src.roguelike_control import Reobserve
        f=self.f
        power=self.power(roi) if require_power else None
        if power is None and require_power:
            f.wait(lambda:self.v.page('DISPATCH') and self.power(roi),'重新读取队员战力')
            power=self.power(roi)
        portrait=self.portrait(f.task.frame,roi)
        original_slots=self.slots()
        native=getattr(f.task,'native_frame',None)
        native_slots=DispatchScreen.selection_evidence(f) if isinstance(native,np.ndarray) and native.shape[:2]==(720,1280) else None
        before_count=expected_selected if expected_selected is not None else native_slots[0] if native_slots else None
        def count_grew():
            if before_count is None:return False
            try:return DispatchScreen.selection(f)[0]==before_count+1
            except DispatchUnreadable:return False
        def selected():
            return bool(self.v.page('DISPATCH') and self.v.find('dispatch_selected',roi))
        def same():
            similarity=self.selected_similarity if selected() else self.similarity
            current_power=self.power(roi) if require_power else None
            # A missing digit after selection is not a changed person. The
            # marker, original portrait and independently counted new slot
            # must corroborate it; a readable conflicting value still stops.
            power_ok=(not require_power or current_power==power or
                      current_power is None and selected() and before_count is not None)
            return (self.v.page('DISPATCH') and power_ok and
                    similarity(self.portrait(f.task.frame,roi),portrait)>=.9)
        def slot_delta():
            return float(np.mean(cv2.absdiff(self.slots(),original_slots)))
        def selected_receipt():
            if not selected():return False
            if not same():raise SafetyStop('已选队员头像或战力变化，选择结果不确定')
            if before_count is not None and not count_grew():
                raise SafetyStop('已选标记与实际人数增长矛盾，停止选人')
            if slot_delta()<=2:raise SafetyStop('已选标记与队员槽矛盾，停止选人')
            return True
        label=f'选择第一排第{index+1}位队员'
        if f.cfg.get('step_mode'):
            f.task.pause();f.delay(3)
        for attempt in range(2):
            sent=False;uncertain_submission=False
            for resumed in range(2):
                try:
                    f.task.before_click()
                    # Re-observe after focus acquisition; no input while a card is unstable.
                    f.wait(lambda:selected() or same(),label+' 稳定复核')
                    if selected_receipt():return True
                    f.task.move(round((roi[0]+roi[2])/2),roi[1]+130)
                    f.delay(.15);f.refresh()
                    if selected_receipt():return True
                    if not same():
                        f.wait(lambda:selected() or same(),label+' 动画后复核')
                        if selected_receipt():return True
                    # A changed slot without a selection marker is ambiguous; never toggle it.
                    if slot_delta()>2 and not self.same_slot_identity(native_slots,DispatchScreen.selection_evidence(f) if native_slots else None):
                        raise SafetyStop('队员槽已变化但选中状态不明，停止补点')
                    f.task.assert_foreground();f.checkpoint();f.task.validate_click_context()
                    f.snapshot('dispatch_before_pick')
                    f.emit('click',step=label,attempt=attempt+1,power=power)
                    def submit():
                        nonlocal sent
                        sent=True
                        return f.task.click(round((roi[0]+roi[2])/2),roi[1]+130,
                                            down_time=.15,after_sleep=0)
                    control=getattr(f.task,'task_control',None) or getattr(f.task,'roguelike_control',None)
                    revision=getattr(f.task,'observed_revision',None)
                    if control and isinstance(revision,int):control.input(revision,submit)
                    else:submit()
                    break
                except (FocusChanged,Reobserve) as error:
                    f.checkpoint();f.task.before_click();f.refresh()
                    if sent:
                        # A backend call has begun: only observe its outcome.
                        # Even unchanged slots cannot authorize a second toggle.
                        uncertain_submission=True
                        f.emit('dispatch_pick_outcome_unconfirmed',candidate=index+1,power=power)
                        break
                    if not same():raise SafetyStop('恢复后队员头像、战力或页面变化，停止选人') from error
                    if selected():
                        return selected_receipt()
                    if slot_delta()>2:raise SafetyStop('恢复后队员槽变化，停止选人') from error
                    # Availability reads this card only; it does not change the
                    # already chosen profession/tactic/extra-reward strategy.
                    screen=DispatchScreen.__new__(DispatchScreen)
                    screen.f=f;screen.frame=f.task.frame
                    available=screen.available(roi,power) if require_power else screen.selectable(roi)
                    if not available:raise SafetyStop('恢复后队员不可选或可用状态不明') from error
                    if resumed:raise SafetyStop('选人恢复期间画面连续变化，未提交输入') from error
                    f.emit('dispatch_pick_reobserve',candidate=index+1,power=power)
            f.delay(.15);f.task.after_input()
            deadline=f.clock()+min(5,f.cfg['recognition_timeout'])
            confirmed=0
            while f.clock()<deadline:
                f.delay(.25);f.refresh()
                if selected() and not same():
                    raise SafetyStop('已选队员头像或战力变化，选择结果不确定')
                confirmed=confirmed+1 if selected() and slot_delta()>2 and (before_count is None or count_grew()) else 0
                if confirmed>=2:
                    f.emit('dispatch_pick_verified',candidate=index+1,power=power,
                           selected_count=before_count+1 if before_count is not None else None)
                    return True
            if uncertain_submission or selected() or slot_delta()>2 or not same():
                raise SafetyStop('队员选择结果不确定，未重复点击')
            f.emit('dispatch_pick_retry',candidate=index+1)
        self.skipped.append('队员连续两次点选未生效')
        return False

    def roster_scroll(self,count):
        from src.cooperative import FocusChanged
        from src.roguelike_control import Reobserve
        f=self.f
        for attempt in range(2):
            try:
                before=getattr(f.task,'before_click',None)
                if before:before()
                f.require('DISPATCH');f.task.assert_foreground();f.checkpoint()
                validate=getattr(f.task,'validate_click_context',None)
                if validate:validate()
                f.task.scroll(3310,1200,count)
                break
            except (FocusChanged,Reobserve):
                if attempt:raise
                f.emit('scroll_reobserve',step='派遣队员列表')
                f.checkpoint()
        after=getattr(f.task,'after_input',None)
        if after:after()
        f.delay(f.cfg['click_min_wait']);f.require('DISPATCH')

    def visible_candidates(self,page,icons):
        f=self.f;screen=DispatchScreen(f);result=[]
        active=getattr(self,'active_filter',self.requirements.filter)
        if self.filter.label()!=active:
            raise DispatchUnreadable('选人前实际战术筛选与任务不符')
        special=self.requirements.special and self.requirements.same_tactic!=4
        areas=(self.four_card_areas(screen) if self.requirements.same_tactic==4 else
               self.card_areas() if special else screen.roster_cards())
        for index,roi in enumerate(areas):
                power=self.power(roi) if special else 0
                if power is None:continue
                portrait=self.portrait(f.task.frame,roi)
                if not portrait.size or portrait.std()<2:continue
                profession=screen.profession(roi,icons) if special and self.requirements.professions else None
                # Unknown profession never contributes to a required job count.
                identity=next((key for key,(old,oldpower) in self.portraits.items()
                    if oldpower==power and self.similarity(old,portrait)>=.92),None)
                if identity is None:
                    identity=str(len(self.portraits));self.portraits[identity]=(portrait,power)
                available=screen.available(roi,power) if special else screen.selectable(roi)
                if not special and self.v.find('dispatch_selected',roi):available=False
                result.append(Candidate(identity,profession,power,available,
                    active,page*9+index,roi,page,screen.tactics(roi) if special and self.requirements.tactic_counts else ()))
        return result

    def four_card_areas(self,screen):
        # Portrait art can erase a border. Current power-label geometry fills
        # those observed cards without using the numeric values for selection.
        areas=self.card_areas()
        for area in screen.roster_cards():
            if not any(area[0]==old[0] and abs(area[3]-old[3])<=60 for old in areas):
                areas.append(area)
        rows=[]
        for area in sorted(areas,key=lambda a:(a[3],a[0])):
            row=next((row for row in rows if abs(row[0][3]-area[3])<=60),None)
            if row is None:rows.append([area])
            else:row.append(area)
        columns=[rect(card)[0] for card in CARDS]
        ordered=[]
        for index,row in enumerate(rows):
            row.sort(key=lambda a:a[0])
            # Never silently promote a later card when an earlier column is
            # unreadable. Only the final row may end before column three.
            if ([a[0] for a in row]!=columns[:len(row)] or
                    index<len(rows)-1 and len(row)!=3):
                raise DispatchUnreadable('乌斯怀亚列表卡片有识别空缺，无法确认界面前4名')
            ordered.extend(row)
        return ordered

    def card_areas(self):
        # Anchor rows to current power labels. Actual 720P row spacing is not
        # exactly the old reference's one-third; fixed row offsets drift.
        l,t,r,b=ROSTER
        boxes=self.f.task.ocr(x=l/3840,y=t/2160,to_x=r/3840,to_y=b/2160,
                              frame=self.f.task.frame,threshold=.85)
        result=[]
        for power in sorted(boxes,key=lambda v:(round(v.y/40),v.x)):
            if not re.fullmatch(r'[xX×]?\d{3,6}',power.name.strip()):continue
            center=power.x+power.width/2
            col=min(range(3),key=lambda i:abs(center-sum(rect(CARDS[i])[::2])/2))
            left,_,right,_=rect(CARDS[col]);top=round(power.y-330);bottom=round(power.y+power.height)
            if abs(center-(left+right)/2)>140 or not t-30<=top<bottom<=b:continue
            roi=(left,top,right,bottom)
            if any(abs(top-old[1])<40 and left==old[0] for old in result):continue
            result.append(roi)
        return result

    def locate_ordinary_member(self,member):
        """Revalidate this planned card only, without re-enumerating the roster."""
        screen=DispatchScreen(self.f)
        original=self.portraits.get(member.identity)
        # Animated card art can split a closed border without moving its face.
        # The current matching portrait verifies the already observed location.
        if original is not None and self.similarity(self.portrait(self.f.task.frame,member.area),original[0])>=.92:
            if not screen.selectable(member.area):raise DispatchUnreadable('普通队员当前不可选，停止选人')
            return member
        areas=[area for area in screen.roster_cards() if area[0]==member.area[0] and
               abs(area[1]-member.area[1])<=30]
        if len(areas)!=1:raise DispatchUnreadable('普通队员当前卡片位置不唯一，停止选人')
        area=areas[0]
        if original is None or self.similarity(self.portrait(self.f.task.frame,area),original[0])<.92:
            raise DispatchUnreadable('普通队员当前头像变化，停止选人')
        if not screen.selectable(area):raise DispatchUnreadable('普通队员当前不可选，停止选人')
        return replace(member,area=area)

    def locate_special_member(self,member,page,icons):
        """Wait briefly for an animated portrait; never relax its identity score."""
        for observation in range(5):
            options=self.visible_candidates(page,icons)
            located=next((c for c in options if c.identity==member.identity and c.available and
                          c.profession==member.profession and c.power==member.power),None)
            if located:return located
            similar=[c for c in options if c.available and c.profession==member.profession
                     and c.power==member.power]
            if self.requirements.same_tactic==4 or not similar or observation==4:return None
            self.f.emit('dispatch_identity_reobserve',candidate=member.identity,page=page,
                        observation=observation+1,required_similarity=.92)
            self.f.delay(.3)
            self.f.require('DISPATCH')
        return None

    def clear_selected(self):
        f=self.f
        self.clear_restored_top=False
        requirements=getattr(self,'requirements',None)
        ordinary=requirements is not None and not requirements.special
        for _ in range(4):
            selected,_=DispatchScreen.selection(f)
            if selected==0:return
            found=None
            for page in range(1 if ordinary else 3):
                areas=(self.four_card_areas(DispatchScreen(f)) if requirements and requirements.same_tactic==4 else
                       DispatchScreen(f).roster_cards() if ordinary else self.card_areas())
                for roi in areas:
                    if self.v.find('dispatch_selected',roi):found=roi;break
                if found:break
                if not ordinary and page<2:self.roster_scroll(-4)
            if not found:raise DispatchUnreadable('已有选中队员未在三屏内定位，禁止混入旧选择')
            self.v.images['dispatch_clear_card']=f.task.frame[found[1]:found[3],found[0]:found[2]].copy()
            self.v.manifest['templates']['dispatch_clear_card']={'roi':found};self.v.begin_frame()
            f.click('DISPATCH','dispatch_clear_card',lambda:DispatchScreen.selection(f)[0]==selected-1,'清除已验证的旧选择')
            if not ordinary:
                self.roster_scroll(12);self.clear_restored_top=True
        if DispatchScreen.selection(f)[0]:raise DispatchUnreadable('无法清空已有选择')

    def accept_ready(self,screen):
        """The first decision on every observation, including an existing team."""
        if (replace(screen.requirements,selected=0)!=self.requirements or
                screen.mission_identity()!=self.mission):
            raise SafetyStop('当前委托已变化，撤销旧选人计划')
        feedback=screen.feedback(False)
        self.f.emit('dispatch_feedback',feedback=feedback)
        if self.requirements.same_tactic==4 and (
                screen.selected!=4 or len(self.picked_members)!=4 or self.filter.label()!=TACTIC):
            return False
        if (not self.requirements.special and not self.requirements.profession_types and
                not self.requirements.tactic_counts and screen.selected<min(3,self.requirements.slots)):return False
        if not feedback['allowed']:return False
        self.expected_selected=screen.selected
        members=getattr(self,'picked_members',())
        self.plan=Plan(tuple(members),bool(feedback.get('bonus')),feedback['power'])
        self.f.emit('dispatch_plan_feedback',bonus=self.plan.bonus,power=self.plan.total_power,source='current_page')
        self.sent=True  # Unknown submission results never authorize a replay.
        self.f.click('DISPATCH','dispatch_ready',self.complete,'执行已核验条件的派遣',precondition=self.guard)
        self.done+=1;self.f.task.info_set('已完成派遣',self.done)
        return True

    def special_plan(self,icons):
        if self.requirements.same_tactic==4:
            self.filter.set(TACTIC);self.active_filter=TACTIC
            self.roster_scroll(12)
            members=[];seen=set()
            for page in range(3):
                for candidate in self.visible_candidates(page,{}):
                    if not candidate.available or candidate.identity in seen:continue
                    seen.add(candidate.identity);members.append(candidate)
                    if len(members)==4:return Plan(tuple(members),False,None)
                if page<2:self.roster_scroll(-4)
            raise DispatchBlocked('BLOCKED：乌斯怀亚三屏内不足4名可选队员')
        # Other special commissions retain their existing bounded strategy.
        current=self.filter.label()
        factions=([current] if current!='全战术' else [])
        factions += [name for name in catalog()['tactics'] if name not in factions]
        for faction in factions if self.requirements.same_tactic else ['全战术']:
            self.filter.set(faction);self.active_filter=faction
            self.roster_scroll(12)
            candidates=[];seen=set()
            for page in range(3):
                for c in self.visible_candidates(page,icons):
                    if c.identity not in seen:seen.add(c.identity);candidates.append(c)
                plan=choose(self.requirements,candidates)
                if plan:return plan
                if page<2:self.roster_scroll(-4)
        return None

    def select(self):
        f=self.f;self.filter=TacticFilter(f);self.requirements=None;self.sent=False
        self.picked_members=[]
        f.dispatch_condition_glyphs={}
        try:
            observed=[]
            last_read_error=None
            def read():
                nonlocal last_read_error
                try:screen=DispatchScreen(f)
                except DispatchUnreadable as error:
                    if str(error)!=last_read_error:
                        last_read_error=str(error)
                        f.emit('dispatch_observation_error',reason=last_read_error)
                    return False
                observed.append(screen)
                return (len(observed)>=2 and observed[-1].requirements==observed[-2].requirements
                        and observed[-1].mission_identity()==observed[-2].mission_identity())
            f.wait(read,'稳定读取派遣条件和槽位')
            self.requirements=replace(observed[-1].requirements,selected=0)
            self.mission=observed[-1].mission_identity()
            f.dispatch_condition_glyphs=getattr(observed[-1],'condition_glyphs',{})
            f.emit('dispatch_requirements',requirements=asdict(self.requirements))
            # A ready team wins over empty slots, stale filters and roster OCR.
            if self.accept_ready(observed[-1]):return True
            ordinary=not self.requirements.special
            initial_selected=observed[-1].selected if ordinary else 0
            self.filter.restore_professions()
            if ordinary or not self.requirements.same_tactic:self.filter.set('全战术')
            if not ordinary:self.clear_selected()
            if ordinary:self.roster_scroll(12)
            self.active_filter='全战术'
            fixed_four=self.requirements.same_tactic==4
            icons={} if ordinary or fixed_four else DispatchScreen(f).profession_icons()
            if not fixed_four and any(p not in icons for p,_ in self.requirements.professions):
                raise DispatchUnreadable('职业条件旁图标无法可靠提取，禁止按角色名字猜职业')
            self.portraits={}
            candidates=self.visible_candidates(0,{}) if ordinary else []
            self.plan=first_available(self.requirements,candidates) if ordinary else self.special_plan(icons)
            if ordinary and self.plan:
                self.plan=replace(self.plan,members=self.plan.members[:max(0,3-initial_selected)])
            f.emit('dispatch_plan',candidates=[asdict(c) for c in candidates],plan=asdict(self.plan) if self.plan else None)
            if self.plan is None:raise DispatchUnreadable('当前页没有可选队员' if ordinary else
                '三屏内没有满足职业、同战术人数和基础战力的可用组合')
            if not ordinary:self.roster_scroll(12)
            for index,member in enumerate(self.plan.members):
                current=DispatchScreen(f)
                if self.accept_ready(current):return True
                if current.selected!=initial_selected+index:raise SafetyStop('实际选中人数与执行进度不一致')
                located=self.locate_ordinary_member(member) if ordinary else None
                if not ordinary:
                    self.roster_scroll(12)
                    for page in range(3):
                        located=self.locate_special_member(member,page,icons)
                        if located:break
                        if page<2:self.roster_scroll(-4)
                if located is None:raise DispatchUnreadable('重新定位后队员不可选或图标/战力变化')
                if self.v.find('dispatch_selected',located.area):raise SafetyStop('队员已选标记与人数矛盾，拒绝切换')
                if fixed_four:
                    picked=self.pick_candidate(index,located.area,require_power=False,expected_selected=current.selected)
                else:
                    picked=self.pick_candidate(index,located.area,require_power=False) if ordinary else self.pick_candidate(index,located.area)
                if not picked:
                    raise DispatchUnreadable('队员连续两次点选未生效')
                f.wait(lambda:DispatchScreen(f).selected==initial_selected+index+1,'核对实际选中人数')
                self.picked_members.append(located)
                if fixed_four:f.snapshot('dispatch_selected_'+str(index+1))
                if self.accept_ready(DispatchScreen(f)):return True
            # One fresh bounded reread after the priority candidates. No filler
            # card or global ordinary-team search is authorized by an empty slot.
            f.wait(lambda:DispatchScreen(f).feedback(False)['allowed'],'复核前三候选后的必选条件')
            return self.accept_ready(DispatchScreen(f))
        except (DispatchUnreadable,RecognitionTimeout,DispatchBlocked) as error:
            if self.sent:raise SafetyStop('派遣提交结果不确定，停止并禁止重复提交') from error
            if not self.v.page('DISPATCH'):raise SafetyStop('派遣页面未知，无法安全跳过') from error
            f.snapshot('dispatch_constraints_unmet');f.emit('dispatch_skipped',reason=str(error))
            self.skipped.append(str(error))
            if self.requirements is None or self.requirements.special:self.roster_scroll(12)
            self.clear_selected()  # A partial team otherwise creates an exit dialog.
            if self.requirements is not None and self.requirements.same_tactic==4:
                raise DispatchBlocked('BLOCKED：乌斯怀亚前4名未满足或未能确认游戏实际派遣条件：'+str(error)) from error
            return False
        except SafetyStop as error:
            f.snapshot('dispatch_selection_uncertain')
            f.emit('dispatch_selection_uncertain',reason=str(error))
            raise
        finally:
            if not self.sent:self.filter.cleanup()

    def run(self,max_tasks=None):
        if max_tasks is not None and (type(max_tasks) is not int or not 1<=max_tasks<=20):
            raise ValueError('派遣检查上限必须为1至20')
        f=self.f
        resumed=0
        quota=getattr(f,'dispatch_initial_quota',None)
        self.quota_budget=quota if type(quota) is int and 0<=quota<=5 else None
        def observe_budget():
            if self.quota_budget is not None:return
            readings=[]
            for observation in range(2):
                refresh=getattr(f,'refresh',None)
                if refresh:refresh()
                reading=read_dispatch_counter(f,'DISPATCH')
                f.emit('dispatch_remaining_counter',observation=observation+1,**reading)
                readings.append(reading['remaining'])
                if observation==0:f.delay(.2)
            if readings[0] is not None and readings[0]==readings[1]:
                self.quota_budget=readings[0]+self.done
                f.emit('dispatch_quota_budget',observed_remaining=readings[0],
                       submitted_at_read=self.done,total_budget=self.quota_budget)
        if self.v.page('DISPATCH'):
            observe_budget()
            resumed=1
            if not self.complete() and self.quota_budget!=0:self.select()
            f.click('DISPATCH','back',lambda:self.v.page('BLACK_SHIELD'),'返回委托列表')
        elif not self.v.page('BLACK_SHIELD'):
            f.click('MAIN_MENU','main_black',lambda:self.v.page('BLACK_SHIELD'),'进入黑盾派遣列表')
        visited=set()
        view=0
        # Dispatch reorders the list: process ONE entry, discard every old row,
        # and scan again from the current top. A finite budget also bounds OCR jitter.
        for _ in range(20):
            if self.quota_budget is not None and self.done>=self.quota_budget:
                f.emit('dispatch_quota_consumed',initial=self.quota_budget,submitted=self.done,
                       remaining=0,already_completed_details_reopened=False)
                break
            if max_tasks is not None and len(visited)+resumed>=max_tasks:break
            f.require('BLACK_SHIELD')
            spec=self.v.manifest['templates']['black_pending'];x,y,x2,y2=spec['roi']
            markers=f.task.find_feature('black_pending',template=self.v.images['black_pending'],
                box=Box(x,y,x2-x,y2-y),threshold=f.cfg['match_threshold'],frame=f.task.frame)
            centers=sorted({round(b.y+b.height/2)-round(55*1.875) for b in markers})
            processed=False
            for cy in centers:
                f.require('BLACK_SHIELD')
                marker_roi=(x,max(y,cy+60),x2,min(y2,cy+150))
                if not self.v.find('black_pending',marker_roi):continue
                labels=f.task.ocr(x=.42,y=max(0,cy-150)/2160,to_x=.66,to_y=min(2160,cy+70)/2160,
                                  frame=f.task.frame,threshold=.9)
                identity='|'.join(b.name.strip() for b in labels if b.name.strip())
                if not identity:
                    self.skipped.append('委托名称识别失败');continue
                if identity in visited:continue
                # Some pending rows have no visible arrow. The supplied screenshots
                # identify their avatar as an entry; re-match its current image before clicking.
                roi=(round(710*1.875),cy-110,round(840*1.875),cy+65)
                ax,ay,ax2,ay2=roi
                if ay<y or ay2>y2:continue
                self.v.images['dispatch_entry']=f.task.frame[ay:ay2,ax:ax2].copy()
                self.v.manifest['templates']['dispatch_entry']={'roi':roi}
                self.v.begin_frame()
                f.click('BLACK_SHIELD','dispatch_entry',lambda:self.v.page('DISPATCH'),'查看待派遣委托 '+identity,roi=roi)
                observe_budget()
                if not self.complete() and (self.quota_budget is None or self.done<self.quota_budget):self.select()
                f.click('DISPATCH','back',lambda:self.v.page('BLACK_SHIELD'),'返回委托列表')
                visited.add(identity)
                processed=True
                break  # Never use the second/third row from the pre-dispatch screenshot.
            if processed:
                if self.quota_budget is not None and self.done>=self.quota_budget:continue
                if view:
                    self.scroll_list(12,'派遣后回顶部重新扫描')
                    view=0
                continue
            if view<2:
                self.scroll_list(-4,'查看后续委托')
                view+=1
            else:break
        else:self.skipped.append('达到列表重扫描安全上限')
        f.home('BLACK_SHIELD')
        status='DONE_CHECK' if self.quota_budget==0 and not self.done else 'PARTIAL' if self.skipped else 'SUCCESS'
        return f'{status}：检查{len(visited)+resumed}条，派遣{self.done}条；'+('；'.join(self.skipped) or '已派遣条目跳过；仅检查可识别列表')
