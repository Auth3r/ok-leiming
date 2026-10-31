"""Pure decisions: observe only obtainable current fields; never wait for policy/history."""
from dataclasses import dataclass,replace
from itertools import product
from src.crafting_policy import CAPS


@dataclass(frozen=True)
class AttributeConfig:
    refresh_mode: str='自动'
    refresh_min_people: int=6
    # Missions whose completion REWARD is fire attack, not ordinary fire cards.
    prioritize_fire_reward: bool=True
    highest_includes_capped: bool=False
    highest_allows_tie: bool=True

    def __post_init__(self):
        if type(self.refresh_min_people) is not int or not 4<=self.refresh_min_people<=9:
            raise ValueError('属性刷新人数门槛应为4至9的整数')
        if self.refresh_mode not in ('自动','优先刷新','不刷新'):
            raise ValueError('未知属性刷新方式')


@dataclass(frozen=True)
class AttributeObservation:
    options: tuple
    remaining: int|None
    refresh_available: bool|None=True
    cards_complete: bool=True
    count_source: str='structured_input'
    slots: tuple=()


@dataclass(frozen=True)
class Decision:
    action: str
    rule: str
    reason: str
    selected_index: int|None=None
    refresh_reason: str=''
    candidates: tuple=()
    missing: tuple=()
    missing_type: str=''
    recoverable_by_observation: bool|None=None
    missing_slots: tuple=()
    retry_plan: tuple=()
    decision_assumption: str=''

    def legacy(self,options):
        selected=next((o for o in options if o.index==self.selected_index),None)
        return ('refresh' if self.action=='refresh' else selected),self.reason


def matches(option,group,attr):
    return option.applies(group) and option.attr==attr


def shares_scope(a,b):
    return bool((set((a.group,)+tuple(a.groups)) & set((b.group,)+tuple(b.groups)))-{''})


def _attribute_retry_plan(fields,slots,reason='缺失字段会改变当前选择、刷新或选中属性的记账'):
    """Producer variants share a frame; the three observation epochs do not."""
    routes={
        'score':('score_roi_native','score_roi_2x','score_digits_roi','score_roi_gray','score_digits_binary'),
        'boost_marker':('attribute_slot_native','attribute_slot_2x'),
        'attribute_scope':('attribute_slot_native','attribute_slot_2x'),
        'candidate_identity':('attribute_slot_native','attribute_slot_2x'),
        'refresh_available':('refresh_button_current_frame',),
        'remaining_people':('paired_card_edges_current_frame',),
    }
    plan=[]
    for field in fields:
        producers=routes[field]
        if field=='candidate_identity' and not slots:
            producers=('attribute_cards_current_frame',)
        targets=slots if slots and field not in ('refresh_available','remaining_people') else (None,)
        for slot in targets:
            plan.append({'field':field,'slot':slot,'scope':'targeted' if slot is not None else 'global',
                         'producers':producers,'missing_field':field,'target_slot':slot,
                         'producer_plan':producers,'fresh_frame_required':False,
                         'attempt':1,'max_attempts':3,'action_dependency':reason})
    return tuple(plan)


def _action_key(decision):
    if decision.action=='observe':return ('observe',decision.missing,decision.missing_slots)
    return decision.action,decision.selected_index


def _decide_with_score_dependencies(observation,state,config):
    decision=_decide_old_land_current(observation,state,config)
    unknown=tuple(o for o in observation.options if not o.score_known)
    if (decision.action!='observe' or decision.missing!=('score',) or len(unknown)!=1 or
            any(type(o.score) is not int or o.score<0 for o in observation.options if o.score_known)):
        return decision
    # With one unknown, comparisons only change at a known integer score.
    # 0, each equality boundary and its successor cover every nonnegative
    # interval, including arbitrarily high values. These are proof cases;
    # never write one of them into the observed Option or its score evidence.
    boundaries={0}
    for option in observation.options:
        if option.score_known:boundaries.update((option.score,option.score+1))
    proofs=[]
    for value in sorted(boundaries):
        options=tuple(replace(o,score=value,score_known=True) if o.index==unknown[0].index else o
                      for o in observation.options)
        proof=_decide_old_land_current(replace(observation,options=options),state,config)
        if proof.action not in ('select','refresh'):return decision
        if proofs and _action_key(proof)!=_action_key(proofs[0]):return decision
        proofs.append(proof)
    return replace(proofs[0],reason=proofs[0].reason+'；未知效能的所有非负比较区间均给出同一动作，未猜分数')


def _decide_with_boost_dependencies(observation,state,config,require_selected_marker=True):
    """Read a boost marker only if its possible values change this action.

    At most nine current cards have two marker states. These substitutions are
    proof cases only; the observed Options and recorded boost counts stay intact.
    """
    unknown=tuple(o for o in observation.options if not o.boost_known)
    if not unknown or not observation.cards_complete or any(
            not o.attr or not (o.group or o.groups) for o in observation.options):
        return _decide_with_score_dependencies(observation,state,config)
    decisions={}
    for values in product((False,True),repeat=len(unknown)):
        markers=dict(zip((o.index for o in unknown),values))
        options=tuple(replace(o,boost=markers[o.index],boost_known=True) if o.index in markers else o
                      for o in observation.options)
        decisions[values]=_decide_with_score_dependencies(replace(observation,options=options),state,config)
    needed=[]
    for position,option in enumerate(unknown):
        for values,decision in decisions.items():
            changed=values[:position]+(not values[position],)+values[position+1:]
            if _action_key(decision)!=_action_key(decisions[changed]):
                needed.append(option.index);break
    base=next(iter(decisions.values()))
    # A chosen marker is also needed to commit the attribute gain truthfully.
    if require_selected_marker and base.action=='select' and any(o.index==base.selected_index for o in unknown):
        needed.append(base.selected_index)
    if needed:
        slots=tuple(sorted(set(needed)))
        return replace(base,action='observe',selected_index=None,rule='boost_dependency',
                       reason='卡位'+','.join(map(str,slots))+'激增标记未知，会影响当前选择/刷新或已选属性记账',
                       missing=('boost_marker',),missing_type='current_frame',
                       recoverable_by_observation=True,missing_slots=slots,
                       retry_plan=_attribute_retry_plan(('boost_marker',),slots))
    return base


def decide_old_land(observation,state,config):
    """Current-page decisions with action-specific checks of unknown history."""
    unknown_off_target=state.off_target_skill_evidence=='unknown'
    # Unknown history cannot authorize spending a refresh. Apply the off-target
    # no-refresh policy to this decision without writing a skill to the record.
    effective_state=replace(state,skills={**state.skills,'__possible_off_target__':1}) if unknown_off_target else state
    def annotate(decision):
        if not unknown_off_target:return decision
        confirmed='本件已选目标集合外技能，降低继续投入；不刷新'
        assumed='历史技能状态未知，本次按不刷新降级策略处理'
        reason=decision.reason.replace(confirmed,assumed)
        if decision.rule=='off_target_highest' and assumed not in reason:reason+='；'+assumed
        return replace(decision,reason=reason,refresh_reason=decision.refresh_reason.replace(confirmed,assumed),
                       decision_assumption='conservative_no_refresh')
    decision=_decide_with_boost_dependencies(observation,effective_state,config,require_selected_marker=False)
    if decision.action=='observe':return annotate(decision)
    alternatives=[]
    if config.prioritize_fire_reward:
        # The task frontier is min(rank), then max(score, -index). Inserting
        # several possible missions preserves a winner whenever every individual
        # insertion preserves it: the union's best rank is one of their best
        # ranks, whose winning card is already the same. Tasks also outrank
        # off-target handling. Thus these per-mission boundary checks prove their
        # combined histories without guessing or storing any old state.
        for position,mission in enumerate(state.missions):
            matching=[o for o in observation.options if o.attr==mission.attr and shares_scope(o,mission)]
            if not matching or (mission.progress_known and mission.goal>0 and mission.current>=mission.goal):
                continue
            unknown_reward=not mission.reward_known
            unknown_progress=not mission.progress_known or mission.goal<=0
            if not unknown_reward and (not mission.reward_is('火力','攻击') or not unknown_progress):continue
            fields=tuple(field for field,unknown in (('mission_reward',unknown_reward),
                                                      ('mission_progress',unknown_progress)) if unknown)
            goals=(mission.goal,) if mission.goal>0 else tuple(sorted({1,max(o.value for o in matching)+1}))
            for goal in goals:
                currents=(mission.current,) if not unknown_progress else tuple(sorted(
                    {0,goal} | {max(0,goal-o.value-offset) for o in matching for offset in (0,1)}))
                for current in currents:
                    possible_mission=replace(mission,reward_group='火力',reward_groups=(),reward_attr='攻击',
                                             reward_known=True,progress_known=True,goal=goal,current=current)
                    missions=list(state.missions);missions[position]=possible_mission
                    possible=replace(effective_state,missions=missions)
                    alternatives.append((fields,_decide_with_boost_dependencies(
                        observation,possible,config,require_selected_marker=False)))
    missing=[];current_missing=[]
    for fields,alternate in alternatives:
        if alternate.action=='observe':
            current_missing.append(alternate)
        elif _action_key(alternate)!=_action_key(decision):missing.extend(fields)
    if missing:
        missing=tuple(dict.fromkeys(missing))
        return annotate(replace(decision,action='stop',selected_index=None,rule='historical_evidence_unavailable',
                                reason='不同的未知历史会改变当前具体动作；候选页无法恢复 '+', '.join(missing),
                                missing=missing,missing_type='historical',recoverable_by_observation=False,
                                missing_slots=(),retry_plan=()))
    if current_missing:
        # Once these current fields are obtained, the next decision either proves
        # one action under both histories or stops for that specific historical gap.
        alternate=current_missing[0]
        return annotate(replace(alternate,reason=alternate.reason+'；需先核验当前证据才能判断未知历史是否改变动作'))
    return annotate(_decide_with_boost_dependencies(observation,effective_state,config))


def _decide_old_land_current(observation,state,config):
    options=observation.options
    notes={o.index:[] for o in options}
    refresh_reason=''

    def result(action,rule,reason,chosen=None,missing=(),missing_type='',recoverable=None,
               missing_slots=(),retry_plan=()):
        return Decision(action,rule,reason,chosen.index if chosen else None,refresh_reason,
                        tuple({'index':o.index,'name':o.name,'reasons':notes[o.index]+[
                            '命中 '+rule if o is chosen else '本次刷新' if action=='refresh' else
                            '等待当前缺失字段' if action=='observe' else
                            f'优先级或同级效能低于卡片{chosen.index}（{rule}）' if chosen else reason]}
                              for o in options),tuple(missing),missing_type,recoverable,
                        tuple(missing_slots),tuple(retry_plan))

    def observe(reason,*missing,slots=(),rule='missing_evidence'):
        return result('observe',rule,reason,missing=missing,
                      missing_type='current_frame',recoverable=True,missing_slots=slots,
                      retry_plan=_attribute_retry_plan(missing,slots,reason))

    def pick(pool,rule,reason):
        if len(pool)>1 and any(not o.score_known for o in pool):
            slots=tuple(o.index for o in pool if not o.score_known)
            return observe('同优先级候选效能未读全；卡位'+','.join(map(str,slots)),
                           'score',slots=slots,rule=rule)
        chosen=max(pool,key=lambda o:(o.score,-o.index))
        return result('select',rule,reason+'；'+refresh_reason,chosen)

    def highest(pool,comparison):
        # First read the promoted candidates themselves. A known competitor can
        # already disprove promotion without reading unrelated lower rules.
        unread=tuple(o.index for o in pool if not o.score_known)
        if unread:return (),unread
        high=max(o.score for o in pool)
        if any(o.score_known and o.score>high for o in comparison):return (),()
        ties=sum(o.score_known and o.score==high for o in comparison)
        if not config.highest_allows_tie and ties>1:return (),()
        unread=tuple(o.index for o in comparison if not o.score_known)
        if unread:return (),unread
        return tuple(o for o in pool if o.score==high),()

    if not options or not observation.cards_complete:
        parsed={o.index for o in options}
        slots=tuple(row['index'] for row in observation.slots if row['index'] not in parsed or
                    'candidate_identity' in row.get('missing',()))
        return observe('可选卡片未读全；漏读不能改变人数或策略','candidate_identity',slots=slots)
    incomplete_scope=[o for o in options if not o.attr or not (o.groups or o.group)]
    if incomplete_scope:
        # A verified ordinary card cannot become a surge exception. Its unread
        # scope is irrelevant to a known fire surge or an already-safe refresh,
        # unless a possibly active fire-reward task could promote that card.
        task_may_override=config.prioritize_fire_reward and any(
            (not m.reward_known or m.reward_is('火力','攻击')) and
            (not m.progress_known or m.goal<=0 or m.current<m.goal) for m in state.missions)
        ordinary_missing=all(o.boost_known and not o.boost for o in incomplete_scope)
        if ordinary_missing and not task_may_override and all(o.boost_known for o in options):
            fire=[o for o in options if o.boost and matches(o,'火力','攻击')]
            if fire:
                refresh_reason='可靠识别火力＋攻击＋激增，不刷新；其它普通卡缺失范围不影响当前动作'
                return pick(fire,'fire_surge','激增火力攻击最高属性优先，不依赖颜色或累计次数')
            if (not any(o.boost for o in options) and not state.off_target_skill and
                    config.refresh_mode!='不刷新' and observation.refresh_available is True and
                    observation.remaining is not None and observation.remaining>=config.refresh_min_people):
                refresh_reason=f'当前完整有效卡位{observation.remaining}人≥门槛{config.refresh_min_people}，无更高业务例外'
                return result('refresh','people_threshold',refresh_reason)
        slots=tuple(o.index for o in incomplete_scope)
        return observe('属性或职业范围缺失','attribute_scope',slots=slots)

    eligible=[]
    for o in options:
        # Fire surge identity is independent of overflow and of every old cap.
        keys=[g+o.attr for g in o.groups or (o.group,) if o.boost and g+o.attr in CAPS]
        full=[]
        for key in keys:
            evidence,count=state.boost_evidence(key)
            if evidence=='exact' and count>=CAPS[key]:
                full.append(key);notes[o.index].append(f'{key}精确{count}次，达到上限{CAPS[key]}')
            elif evidence!='exact':
                notes[o.index].append(f'{key}为{evidence}（{count}），CAP不约束猜测历史')
        if not full or (o.boost and matches(o,'火力','攻击')):eligible.append(o)

    task=[]
    if config.prioritize_fire_reward:
        for m in state.missions:
            if not m.reward_is('火力','攻击'):continue
            matching=[o for o in eligible if o.attr==m.attr and shares_scope(o,m)]
            if not matching:continue
            # Unknown historical progress is compared with possible actions by
            # decide_old_land; a static candidate page cannot recover that fact.
            if not m.progress_known or m.goal<=0:continue
            if m.current<m.goal:task.extend((o,m) for o in matching)
    if task:
        # Finish now first. Otherwise prefer a matching surge, not require one.
        def rank(pair):
            option,mission=pair
            unfinished=mission.current+option.value<mission.goal
            return (unfinished,unfinished and not option.boost,
                    0 if matches(option,'重盾','防御') else
                    1 if matches(option,'工程','最大生命') else
                    2 if matches(option,'重盾','攻击') else 3)
        best=min(map(rank,task))
        pool={o.index:o for o,m in task if rank((o,m))==best}
        refresh_reason='正在进行的任务完成奖励为火力攻击，不刷新'
        return pick(list(pool.values()),'fire_reward_task','优先完成/推进火攻奖励任务所需属性')

    fire=[o for o in eligible if o.boost and matches(o,'火力','攻击')]
    if fire:
        refresh_reason='可靠识别火力＋攻击＋激增，不刷新；overflow仅作诊断'
        return pick(fire,'fire_surge','激增火力攻击最高属性优先，不依赖颜色或累计次数')

    if state.off_target_skill:
        refresh_reason='本件已选目标集合外技能，降低继续投入；不刷新'
        if not eligible:
            return result('stop','no_safe_candidate','全部候选被精确CAP排除，无法安全选择')
        return pick(eligible,'off_target_highest','从当前安全候选中选择最高效能')

    defence=[o for o in eligible if o.boost and matches(o,'重盾','防御')]
    if defence:
        refresh_reason='激增重盾防御为下一优先，不刷新'
        return pick(defence,'shield_defence','优先激增重盾防御（仅精确CAP可排除）')

    if config.refresh_mode=='不刷新':refresh_reason='用户设置不刷新'
    elif observation.refresh_available is False:refresh_reason='当前节点刷新已用或不可用'
    elif observation.refresh_available is None:return observe('当前节点刷新可用状态未知','refresh_available')
    elif observation.remaining is None:
        return observe('当前完整有效卡位计数未知；不能用已解析Option数猜人数','remaining_people')
    elif observation.remaining>=config.refresh_min_people:
        refresh_reason=f'实际剩余{observation.remaining}人≥门槛{config.refresh_min_people}，无最高业务例外'
        return result('refresh','people_threshold',refresh_reason)
    else:refresh_reason=f'实际剩余{observation.remaining}人<门槛{config.refresh_min_people}，不刷新'

    life=[o for o in eligible if o.boost and matches(o,'工程','最大生命')]
    if life:return pick(life,'engineer_life','沿用工程最大生命激增优先（仅精确CAP可排除）')
    shield=[o for o in eligible if o.boost and matches(o,'重盾','攻击')]
    if shield:
        comparison=list(options) if config.highest_includes_capped else eligible
        top,slots=highest(shield,comparison)
        if slots:
            return observe('激增重盾攻击最高效能比较；卡位'+','.join(map(str,slots))+
                           '效能未知，无法证明当前最高','score',slots=slots,rule='shield_attack_highest')
        if top:return pick(top,'shield_attack_highest','激增重盾攻击仍须为当前可选最高效能')
        for o in shield:notes[o.index].append('未满足最高效能条件，继续检查其它候选')
        eligible=[o for o in eligible if o not in shield]
    for group,attr,boost,rule in (('火力','攻击',False,'ordinary_fire'),('医疗','攻击',True,'medic_boost'),
                                 ('重盾','防御',False,'ordinary_defence')):
        pool=[o for o in eligible if o.boost==boost and matches(o,group,attr)]
        if pool:return pick(pool,rule,group+attr+('激增' if boost else '普通属性')+'沿用后续顺序')
    ordinary_shield=[o for o in eligible if not o.boost and matches(o,'重盾','攻击')]
    if ordinary_shield:
        # Ordinary shield attack may outrank ordinary medic only with current
        # highest-efficiency evidence; earlier explicit priorities still win.
        top,slots=highest(ordinary_shield,eligible)
        if slots:
            return observe('普通重盾攻击需比较安全候选最高效能；卡位'+','.join(map(str,slots))+
                           '效能未知','score',slots=slots,rule='ordinary_shield_attack_highest')
        if top:
            return pick(top,'ordinary_shield_attack_highest',
                        '普通重盾攻击为当前安全候选最高效能，优先于普通医疗攻击')
        for o in ordinary_shield:notes[o.index].append('未满足安全候选最高效能条件，沿用普通医疗及效能兜底')
    medic=[o for o in eligible if not o.boost and matches(o,'医疗','攻击')]
    if medic:return pick(medic,'ordinary_medic','医疗攻击普通属性沿用后续顺序')
    if not eligible:
        return result('stop','no_safe_candidate','全部候选达到精确上限或不满足选择条件')
    return pick(eligible,'highest_fallback','无明确优先项，选择安全候选中最高效能')


def decide_mission(options,state,prioritize_fire_reward=True,cards_complete=True):
    """Known reward text participates even without a special reward classification."""
    def observe(reason,*missing,slots=None):
        if slots is None:
            slots=tuple(o.index for o in options if o.goal<=0 or not o.attr or not (o.group or o.groups) or
                        not o.progress_known or not o.reward_known)
        producers=('mission_slot_native','mission_slot_2x') if slots else ('mission_card_headers',)
        plans=tuple({'field':field,'slot':slot,'producers':producers,'missing_field':field,
                     'target_slot':slot,'producer_plan':producers,'fresh_frame_required':not bool(slots),
                     'attempt':1,'max_attempts':3,'action_dependency':reason}
                    for field in missing for slot in slots or (None,))
        return Decision('observe','missing_evidence',reason,missing=missing,
                        missing_type='current_frame',recoverable_by_observation=True,
                        missing_slots=slots,retry_plan=plans)
    if not options or not cards_complete:return observe('任务卡未读全','mission_cards')
    missing=[]
    if any(o.goal<=0 or not o.attr or not (o.group or o.groups) for o in options):missing.append('mission_condition')
    if any(not o.progress_known for o in options):missing.append('mission_progress')
    if any(not o.reward_known for o in options):missing.append('mission_reward_text')
    if missing:return observe('当前任务OCR缺字段；只允许有限局部重读',*missing)
    pool=list(options)
    fire=[o for o in pool if o.reward_is('火力','攻击')]
    if prioritize_fire_reward and fire:pool=fire
    remaining={o.index:max(0,o.goal-o.current) for o in pool}
    # Do not invent a per-attribute increment. Estimate the action distance from
    # verified same-module gains; future card values are not guaranteed. Without
    # that evidence, order the actual remaining quantity instead of waiting.
    steps={}
    for o in pool:
        gains=[row['value'] for row in state.history if row.get('kind')=='attribute' and
               row.get('source')=='verified_confirmation' and row.get('attribute')==o.attr and
               (set((o.group,)+tuple(o.groups)) & set((row.get('group',''),)+tuple(row.get('groups',()))))-{''} and
               row.get('boost') and row.get('value',0)>0]
        increment=max(gains+[o.value]) if gains or o.value>0 else 0
        if increment:steps[o.index]=(remaining[o.index]+increment-1)//increment
        elif remaining[o.index]==0:steps[o.index]=0
    close=[o for o in pool if steps.get(o.index,2)<=1]
    if close:
        distance=min(steps[o.index] for o in close)
        pool=[o for o in close if steps[o.index]==distance]
    elif len(steps)==len(pool):
        distance=min(steps.values());pool=[o for o in pool if steps[o.index]==distance]
    else:
        distance=min(remaining.values());pool=[o for o in pool if remaining[o.index]==distance]
    if len(pool)>1:
        if any(not o.rarity_known for o in pool):
            return observe('同完成距离任务稀有度缺失','rarity',slots=tuple(o.index for o in pool if not o.rarity_known))
        rarity=max(o.rarity for o in pool);pool=[o for o in pool if o.rarity==rarity]
    if len(pool)>1:
        if any(not o.score_known for o in pool):
            return observe('同完成距离/稀有度任务效能缺失','score',slots=tuple(o.index for o in pool if not o.score_known))
        score=max(o.score for o in pool);pool=[o for o in pool if o.score==score]
    shield=[o for o in pool if o.reward_is('重盾','防御')]
    chosen=min(shield or pool,key=lambda o:o.index)
    return Decision('select','mission_priority',
                    '火攻奖励开关 → 剩余量/本件已确认增量估算的动作距离 → 稀有度（金>紫） → 效能 → 重盾防御奖励 → 页面顺序',
                    chosen.index,candidates=tuple({'index':o.index,'remaining':max(0,o.goal-o.current),
                                                  'steps':steps.get(o.index),
                                                  'reward_text':o.reward_text,
                                                  'reward_specific_priority':o.reward_specific_priority}
                                                 for o in options))
