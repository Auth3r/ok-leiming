"""Pure, versioned Old Land decisions; no device operations."""
from dataclasses import dataclass, field
import re


def skill_title(text):
    """Normalize observed separator/glyph noise in unique first-strike titles.

    Explicit foreign owners, level suffixes and unrelated names are preserved.
    The original OCR boxes remain available for audit and re-observation.
    """
    text=re.sub(r'^(先攻|军工|一致|战线)([\u4e00-\u9fff]{1,16})(?=$|L[VY][.·:]?\d+\+\d+$)',
                r'\1·\2',text,flags=re.IGNORECASE)
    title=re.fullmatch(r'((?:先攻|军工|一致|战线)[·•・:：])?([\u4e00-\u9fff]{2,16})((?:L[VY][.·:]?\d+\+\d+)?)',text,re.I)
    if title:
        name=title[2]
        name={'实占':'实战精进','实站':'实战精进'}.get(name,name)
        # These unique first-strike names remain recognizable with one OCR
        # glyph error (observed 实占精进); never fuzzy-match level or rarity.
        known=('器械专精','实战精进','增幅力场')
        def one_glyph(value):
            if len(name)==len(value):return sum(a!=b for a,b in zip(name,value))<=1
            longer,shorter=(name,value) if len(name)>len(value) else (value,name)
            # A real modifier/prefix is not an OCR alias. The observed doubled
            # glyph (实占战精进) is internal to the title.
            return len(longer)==len(shorter)+1 and any(longer[:i]+longer[i+1:]==shorter for i in range(1,len(longer)-1))
        matches=[value for value in known if one_glyph(value)]
        if len(matches)==1:name=matches[0]
        text=(title[1] or '')+name+(title[3] or '')
    return text


def skill_name(text):
    text=skill_title(re.sub(r'\s+','',text).strip('◇◆◈♦♢·•・'))
    if skill_module(text):text=re.sub(r'^[\u4e00-\u9fff]{2,8}[·•・:：]+','',text)
    return text.strip('◇◆◈♦♢·•・')


def skill_module(text):
    text=skill_title(text)
    match=re.match(r'^[◇◆◈♦♢·•・]*([\u4e00-\u9fff]{2,8})[·•・:：]+',re.sub(r'\s+','',text))
    if not match:return ''
    if match[1] in ('旧地','旧地联合'):return '旧地联合'
    if match[1]=='战线':return '一致'
    profiles=[name for name in ('军工','一致','先攻') if name in match[1]]
    return profiles[0] if len(profiles)==1 else ''


def core_skill_name(text):
    """A core's title identifies it; its wrapped description does not."""
    return re.split(r'核心队员|生效条件|解锁条件',text,1)[0]


def normalize_record(values):
    """Normalize saved names and enforce the known gold-skill count lower bound."""
    values=dict(values);skills={}
    for name,level in values.get('skills',{}).items():
        # Local-profile decisions replay full identities.  Retain ownership in
        # their legacy display projection, especially for same-name outsiders.
        name=name if values.get('local_skill_ledger') else skill_name(name)
        skills[name]=max(skills.get(name,0),level)
    values['skills']=skills
    values['gold_picks']=(values.get('gold_picks',0) if values.get('local_skill_ledger') else
                         max(values.get('gold_picks',0),skills.get('连携增援',0)))
    # A legacy missing reward is unknown, never evidence of a non-fire reward.
    values['missions']=[{**m,'reward_known':m.get('reward_known',bool(m.get('reward_text') or
                                                                  (m.get('reward_group') and m.get('reward_attr'))))}
                        for m in values.get('missions',[])]
    # The legacy parser collapsed engineering/medic/commander into one label.
    # Do not invent exact counts for the discarded scope when upgrading its history.
    unknown=set(values.get('unknown_boosts',[]))
    for row in values.get('history',[]):
        if row.get('kind')=='attribute' and row.get('boost') and not row.get('groups') and row.get('group') in ('工程','医疗'):
            unknown.update(g+row.get('attribute','') for g in ('工程','医疗'))
    values['unknown_boosts']=sorted(unknown)
    if not isinstance(values.get('skill_opportunity_state',{}),dict):
        values['skill_opportunity_state']={}
    if values.get('local_skill_ledger'):
        from src.crafting_skill_state import normalize_local_skill_record
        values=normalize_local_skill_record(values)
    return values


@dataclass
class Option:
    index: int
    name: str=''
    group: str=''
    attr: str=''
    value: int=0
    score: int=0
    boost: bool=False
    rarity: int=0
    level: int=0
    gain: int=1
    goal: int=0
    current: int=0
    reward_group: str=''
    reward_attr: str=''
    area: tuple=()
    level_known: bool=True
    groups: tuple=()
    reward_groups: tuple=()
    overflow: bool|None=None
    score_known: bool=True
    progress_known: bool=True
    reward_known: bool=True
    boost_known: bool=True
    reward_text: str=''
    reward_value: int=0
    rarity_known: bool=True
    portrait_signature: str=''
    score_confidence: float|None=None
    score_evidence_id: str=''
    score_source: str=''
    score_rejected_reason: str=''
    visual_enabled: bool|None=None
    module: str=''
    raw_name: str=''
    skill_kind: str='skill'
    skill_usable: bool|None=None
    usability_evidence_id: str=''

    def applies(self,group):
        return group==self.group or group in self.groups

    def reward_is(self,group,attr):
        return self.reward_known and self.reward_attr==attr and (
            self.reward_group==group or group in self.reward_groups)

    @property
    def reward_specific_priority(self):
        if self.reward_is('火力','攻击'):return 'fire_attack_reward'
        if self.reward_is('重盾','防御'):return 'shield_defence_reward'
        return None


@dataclass
class CraftRecord:
    skills: dict=field(default_factory=dict)
    boosts: dict=field(default_factory=dict)
    missions: list=field(default_factory=list)
    factors: list=field(default_factory=list)
    history: list=field(default_factory=list)
    gold_picks: int=0
    complete: bool=True
    boosts_complete: bool|None=None
    missions_complete: bool|None=None
    unknown_skills: list=field(default_factory=list)
    source: str='verified_run'
    unknown_boosts: list=field(default_factory=list)
    known_boosts: list=field(default_factory=list)
    perfect_mode: bool=True
    perfect_reason: str=''
    skill_opportunity_history: list=field(default_factory=list)
    skill_opportunity_state: dict=field(default_factory=dict)
    local_skill_ledger: dict=field(default_factory=dict)

    @property
    def first_strike_gold_picks(self):
        """Classify confirmed gold gains without relabelling legacy unknown picks."""
        target=outside=0
        for row in self.history:
            if row.get('kind')!='skill' or row.get('rarity')!=3 or not row.get('level_known'):
                continue
            gain=row.get('gain',0)
            if first_strike_progress_key(row.get('name',''),row.get('module',''),
                                         row.get('raw_name',''),row.get('rarity'))=='器械':
                target+=gain
            else:outside+=gain
        return {'target':target,'outside':outside,
                'unclassified':max(0,self.gold_picks-target-outside)}

    def count_known(self,key):
        return key not in self.unknown_boosts and (key in self.known_boosts or bool(self.boosts_complete))

    def boost_evidence(self,key):
        """A missing key is zero only in an exact ledger; otherwise it is unknown."""
        if self.count_known(key):return 'exact',self.boosts.get(key,0)
        if key in self.boosts:return 'lower_bound',self.boosts[key]
        return 'unknown',None

    @property
    def off_target_skill(self):
        # Derive from confirmed choices, including a choice with unread level.
        names={skill_name(n) for n,level in self.skills.items() if level>0}
        names.update(skill_name(row.get('name','')) for row in self.history
                     if row.get('kind')=='skill' and row.get('name'))
        return bool(names-TARGET_SKILLS)

    @property
    def off_target_skill_evidence(self):
        """A confirmed outside skill proves true; an incomplete absence stays unknown."""
        if self.off_target_skill:return 'exact'
        return 'exact' if self.complete else 'unknown'

    def __post_init__(self):
        if self.boosts_complete is None:self.boosts_complete=self.complete
        if self.missions_complete is None:self.missions_complete=self.complete

    def lose_history(self):
        self.complete=False
        self.boosts_complete=False
        self.missions_complete=False
        self.known_boosts=[]
        self.source='historical_lower_bound'
        if self.local_skill_ledger:
            from src.crafting_skill_state import mark_local_skill_unknown
            mark_local_skill_unknown(self,'制作页面变化导致完整技能历史失去关联')

    def pending(self):
        return [m for m in self.missions if m.progress_known and m.current<m.goal]

    def commit(self,kind,o):
        if kind=='skill' and self.local_skill_ledger:
            from src.crafting_skill_state import LocalSkillStateError
            raise LocalSkillStateError('军工/一致技能必须通过完整确认事务提交，不能按点击累计')
        self.history.append({'kind':kind,'name':o.name,'group':o.group,'attribute':o.attr,
                             'groups':list(o.groups),'value':o.value,'score':o.score,'boost':o.boost,
                             'source':'verified_confirmation'})
        if not o.score_known:self.history[-1]['score_known']=False
        if kind=='skill':
            self.history[-1].update(module=o.module,raw_name=o.raw_name,rarity=o.rarity,
                                    level=o.level,gain=o.gain,level_known=o.level_known)
            if not o.level_known:
                self.complete=False
                if o.name not in self.unknown_skills:self.unknown_skills.append(o.name)
            if o.level_known:self.skills[o.name]=o.level+o.gain
            if o.rarity==3 and o.level_known:self.gold_picks+=o.gain
            self.gold_picks=max(self.gold_picks,self.skills.get('连携增援',0))
        elif kind=='attribute':
            if o.boost:
                for group in o.groups or (o.group,):
                    key=group+o.attr
                    self.boosts[key]=self.boosts.get(key,0)+1
            for m in self.missions:
                if set(m.groups or (m.group,)) & set(o.groups or (o.group,)) and m.attr==o.attr:m.current+=o.value
        elif kind in ('mission','core_attribute'):self.missions.append(o)
        elif kind=='factor':self.factors.append(o.name)


CAPS={'重盾防御':3,'工程最大生命':2,'重盾攻击':2}
LOCAL=('指挥增效','快速整备')
# Exactly the named priorities in skill(), including its existing secondary choices.
TARGET_SKILLS=frozenset(('连携增援',)+LOCAL+('电磁旋涡','强击','先制打击'))
FACTORS=('好整以暇','声东击西')


class OldLandPolicy:
    name='旧地联合'

    def capped(self,o,s):
        return o.boost and not (o.applies('火力') and o.attr=='攻击') and any(
                              s.count_known(g+o.attr) and s.boosts.get(g+o.attr,0)>=CAPS[g+o.attr]
                              for g in o.groups or (o.group,) if g+o.attr in CAPS)

    def decide_attribute(self,observation,s,config):
        from src.crafting_decision import decide_old_land
        return decide_old_land(observation,self.attribute_state(s),config)

    def attribute_state(self,s):
        return s

    def attribute(self,options,s,refreshed=False,refresh_mode='自动',remaining=None):
        from src.crafting_decision import AttributeObservation,AttributeConfig
        decision=self.decide_attribute(AttributeObservation(tuple(options),remaining,not refreshed),s,
                                       AttributeConfig(refresh_mode=refresh_mode))
        return decision.legacy(options)

    def skill(self,options,s,refreshed=False):
        def local_choice(pool,levels):
            quick=levels.get('快速整备');command=levels.get('指挥增效')
            order=('指挥增效','快速整备') if quick==1 and command==0 else ('快速整备','指挥增效')
            for name in order:
                choices=[o for o in pool if o.name==name]
                if choices:return max(choices,key=lambda o:(o.score,-o.index))
        if not s.complete:
            known={o.name:o.level for o in options if o.level_known}
            link=[o for o in options if o.name=='连携增援' and o.level_known and o.level<3]
            if link:return max(link,key=lambda o:o.score),'当前画面确认连携增援未满3级'
            local=[o for o in options if o.name in LOCAL and o.level_known and o.level<2]
            if local:return local_choice(local,known),'按可见等级：快速1 → 指挥1 → 快速补满2 → 指挥补满2'
            if not refreshed:return 'refresh','当前无可提升的主目标技能，刷新一次'
            for name in ('电磁旋涡','强击','先制打击'):
                secondary=[o for o in options if o.name==name]
                if secondary:return max(secondary,key=lambda o:(o.score,-o.index)),'刷新后选择当前可见目标技能'
            return max(options,key=lambda o:(o.score,-o.index)),'刷新后按最高分保留材料用途'
        byname=lambda name:[o for o in options if o.name==name]
        if s.skills.get('连携增援',0)<3 and byname('连携增援'):
            return max(byname('连携增援'),key=lambda o:o.score),'优先连携增援达到3级'
        local=[o for o in options if o.name in LOCAL and s.skills.get(o.name,0)<2]
        if local:return local_choice(local,{n:s.skills.get(n,0) for n in LOCAL}),'快速1 → 指挥1 → 快速补满2 → 指挥补满2'
        full=all(s.skills.get(n,0)>=2 for n in LOCAL)
        if full:
            if not any(s.skills.get(n,0) for n in ('电磁旋涡','强击')):
                for name in ('电磁旋涡','强击'):
                    if byname(name):return max(byname(name),key=lambda o:o.score),'补一个电磁旋涡或强击'
            for name in ('快速整备','指挥增效'):
                if byname(name):return max(byname(name),key=lambda o:o.score),'2+2后继续本地技能'
        if not full:
            for name in ('电磁旋涡','先制打击'):
                if byname(name):return max(byname(name),key=lambda o:o.score),'其他已知可用技能'
        if not refreshed:return 'refresh','无优先技能，刷新一次'
        if full:
            blue=[o for o in options if o.rarity==1]
            if blue:return max(blue,key=lambda o:o.score),'刷新后选择蓝技能'
        return max(options,key=lambda o:o.score),'刷新后按最高分保留材料用途'

    def decide_mission(self,options,s=None,prioritize_fire_reward=True,cards_complete=True):
        from src.crafting_decision import decide_mission
        return decide_mission(options,s or CraftRecord(),prioritize_fire_reward,cards_complete)

    def mission(self,options,s=None,prioritize_fire_reward=True):
        viable=[o for o in options if o.goal>0 and o.attr and (o.group or o.groups)]
        return self.decide_mission(viable,s,prioritize_fire_reward).legacy(viable)


def best_value(options):
    options=[o for o in options if o.visual_enabled is not False]
    if not options:
        from src.crafting_vision import ChoiceUnreadable
        raise ChoiceUnreadable('当前没有已知安全可选的技能')
    # Unknown scores cannot safely participate in a highest-value comparison.
    if len(options)>1 and any(not o.score_known for o in options):
        from src.crafting_vision import ChoiceUnreadable
        raise ChoiceUnreadable('新赛季技能效能尚未读清')
    return max(options,key=lambda o:(o.score,-o.index))


FIRST_STRIKE_TARGET_NAMES=frozenset(('器械','器械专精','实战','实战精进','增幅','增幅力场','强击'))
FIRST_STRIKE_UNPREFIXED_PURPLE=frozenset(('实战精进','增幅力场'))


def _profile_skill_target(name,module,raw_name,profile):
    """Use explicit ownership before the current profile's established targets."""
    if any(not isinstance(value,str) for value in (name,module,raw_name)):return False
    owners=[]
    if module:
        if module!=profile:return False
        owners.append(module)
    for text in (name,raw_name):
        if re.search(r'[·•・:：]',text):
            owner=skill_module(text)
            if owner!=profile:return False
            owners.append(owner)
    normalized=skill_name(name)
    if profile=='先攻':return normalized in FIRST_STRIKE_TARGET_NAMES
    # Keep the inherited secondary choices; an unfamiliar bare name is not local.
    return bool(owners) or normalized in TARGET_SKILLS


@dataclass
class _ProfileAttributeState(CraftRecord):
    """Temporary decision view; never persisted or committed to the source record."""
    _target_profile: str=''

    @property
    def off_target_skill(self):
        choices=[row for row in self.history if row.get('kind')=='skill' and row.get('name')]
        if any(not _profile_skill_target(row['name'],row.get('module',''),row.get('raw_name',''),
                                         self._target_profile) for row in choices):return True
        recorded={skill_name(row['name']) for row in choices}
        return any((re.search(r'[·•・:：]',name) or skill_name(name) not in recorded) and
                   not _profile_skill_target(name,'','',self._target_profile)
                   for name,level in self.skills.items() if level>0)

    @property
    def off_target_skill_evidence(self):
        if self.off_target_skill:return 'exact'
        return 'exact' if self.complete else 'unknown'


class LocalModulePolicy(OldLandPolicy):
    """Reuse production decisions with this profile's confirmed skill targets."""
    def attribute_state(self,s):
        from dataclasses import fields
        values={f.name:getattr(s,f.name) for f in fields(CraftRecord)}
        if self.name in ('军工','一致') and s.local_skill_ledger:
            from src.crafting_skill_state import COMMON_BLUE_CATALOG,build_local_skill_state
            state=build_local_skill_state(s,self.name)
            if state.trusted:
                # The ledger correctly resolves these unprefixed observations
                # to common identities. Attribute rules still use their
                # original observed form: only the existing TARGET_SKILLS
                # qualify, so e.g. 强击 retains its old role while 装甲 does not.
                common={event['event_id']:event['identity']['name']
                        for event in s.local_skill_ledger['events']
                        if event['identity']['module']=='通用' and event['identity']['rarity']==1
                        and event['identity']['name'] in COMMON_BLUE_CATALOG
                        and event.get('identity_source')==COMMON_BLUE_CATALOG.get(event['identity']['name'])}
                values['history']=[{**row,'module':''} if row.get('event_id') in common else row
                                   for row in s.history]
                aliases={'通用·'+name:name for name in common.values()}
                values['skills']={aliases.get(name,name):level for name,level in s.skills.items()}
            else:
                # Unknown skill history retains the existing conservative
                # attribute behavior; it cannot authorize a resource refresh.
                values['complete']=False
        return _ProfileAttributeState(**values,_target_profile=self.name)

    def decide_skill(self,options,s,refreshed=False,*,refresh_available=None):
        from src.crafting_local_policy import decide_local_skill
        from src.crafting_skill_state import build_local_skill_state
        return decide_local_skill(options,build_local_skill_state(s,self.name),self.name,
                                  refreshed=refreshed,refresh_available=refresh_available)

    def skill(self,options,s,refreshed=False,*,refresh_available=None):
        return self.decide_skill(options,s,refreshed,refresh_available=refresh_available).legacy(options)


class MilitaryPolicy(LocalModulePolicy):
    name='军工'


class UnityPolicy(LocalModulePolicy):
    name='一致'


FIRST_STRIKE_TARGETS={'器械':3,'实战':3,'增幅':2,'强击':3}


def first_strike_key(name):
    # Match only the user's short labels and the observed complete skill titles.
    return {'器械':'器械','器械专精':'器械','实战':'实战','实战精进':'实战',
            '增幅':'增幅','增幅力场':'增幅','强击':'强击'}.get(skill_name(name))


def first_strike_progress_key(name,module,raw_name,rarity):
    """Only actual target card roles can supply perfect-plan progress."""
    if not _profile_skill_target(name,module,raw_name,'先攻'):return None
    key=first_strike_key(name)
    if key=='器械' and rarity==3:return key
    if key in ('实战','增幅') and rarity==2 and (
            module=='先攻' or skill_name(name) in FIRST_STRIKE_UNPREFIXED_PURPLE):return key
    if key=='强击':return key
    return None


def can_finish_skills(deficits,gains):
    """Small four-skill feasibility check: one observed choice upgrades one skill."""
    from functools import lru_cache
    gains=tuple(sorted(gains,reverse=True))
    @lru_cache(None)
    def fits(needs,index):
        if not any(needs):return True
        if index==len(gains) or sum(needs)>sum(gains[index:]):return False
        if sum(n>0 for n in needs)>len(gains)-index:return False
        for slot,need in enumerate(needs):
            if need:
                updated=list(needs);updated[slot]=max(0,need-gains[index])
                if fits(tuple(sorted(updated,reverse=True)),index+1):return True
        return False
    return fits(tuple(sorted(deficits,reverse=True)),0)


@dataclass(frozen=True)
class SkillOpportunities:
    """Remaining choice gains; confirmed includes the current choice once.

    Possible choices only preserve feasibility, never guarantee an outcome.
    Absent stores explicit source labels, and an unread future cannot prove
    either that the target is unreachable or that this is the final choice.
    """
    confirmed: tuple
    possible: tuple=()
    absent: tuple=()
    future_known: bool=True


class FirstStrikePolicy(LocalModulePolicy):
    name='先攻'

    def skill(self,options,s,refreshed=False,opportunities=None,*,refresh_available=True):
        """Use confirmed and still possible choices as a feasibility upper bound.

        Legacy tuples contain confirmed gains, including this choice. None or
        future_known=False preserves an unread future instead of inventing zero.
        """
        def fodder(reason):
            choice=fallback_choice(require_identity=True)
            s.perfect_mode=False;s.perfect_reason=reason
            return choice,'狗粮模式：'+reason
        options=[o for o in options if o.visual_enabled is not False]
        if not options:
            from src.crafting_vision import ChoiceUnreadable
            raise ChoiceUnreadable('当前没有已知安全可选的技能')
        levels={key:0 for key in FIRST_STRIKE_TARGETS}
        known=set(levels) if s.complete else set()
        modules={row.get('name',''):row.get('module','') for row in s.history if row.get('kind')=='skill'}
        for name,level in s.skills.items():
            key=first_strike_key(name)
            module=skill_module(name) or modules.get(name,'')
            if key and module in ('',self.name):levels[key]=max(levels[key],level)
        for o in options:
            key=first_strike_progress_key(o.name,o.module,o.raw_name,o.rarity)
            if key and o.level_known and (o.rarity_known or key=='强击'):
                levels[key]=max(levels[key],o.level);known.add(key)
        deficit=lambda key:max(0,FIRST_STRIKE_TARGETS[key]-levels[key])
        progress=lambda o:first_strike_progress_key(o.name,o.module,o.raw_name,o.rarity)
        def fallback_choice(require_identity=False):
            # Confirmed LV3 remains the lower bound even if a stale page says LV0.
            # The same cap applies to sticky fodder and a new fallback decision.
            eligible=[o for o in options if not (
                progress(o)=='强击' and deficit('强击')==0)]
            choice=best_value(eligible)
            if require_identity and (not choice.name or not choice.level_known or not choice.rarity_known):
                from src.crafting_vision import ChoiceUnreadable
                raise ChoiceUnreadable('兜底技能名称、等级或品质尚未读清，未改变制作模式')
            return choice
        # Fodder retains its independent highest-value rule; it never requests
        # the perfect plan's refresh merely because a target is missing.
        if not s.perfect_mode:return fallback_choice(),'狗粮模式：'+s.perfect_reason
        unread_future=(opportunities is None or isinstance(opportunities,SkillOpportunities) and
                       not opportunities.future_known)
        complete_gold=(unread_future and any(progress(o)=='器械' and o.level_known and
                       o.rarity_known and o.score_known and o.gain>0 and deficit('器械')>0 for o in options))
        if any(_profile_skill_target(o.name,o.module,o.raw_name,self.name) and
               (not o.rarity_known or not o.level_known) and not (
                   complete_gold and o.rarity_known and progress(o) in ('实战','增幅','强击')) for o in options):
            from src.crafting_vision import ChoiceUnreadable
            raise ChoiceUnreadable('先攻技能颜色或等级尚未读清')
        gold=[o for o in options if progress(o)=='器械' and o.gain>0 and deficit('器械')>0]
        purple=[o for o in options if progress(o) in ('实战','增幅') and
                o.gain>0 and deficit(progress(o))>0]
        strike=[o for o in options if progress(o)=='强击' and o.gain>0 and deficit('强击')>0]
        if isinstance(opportunities,SkillOpportunities):
            future_known=opportunities.future_known
            gains=tuple(opportunities.confirmed)+tuple(opportunities.possible)
            last=(future_known and len(opportunities.confirmed)==1 and not opportunities.possible)
        else:
            future_known=opportunities is not None
            gains=opportunities
            last=future_known and len(gains)==1
        # The third-gold closure is the one explicit exception to the exact plan.
        local_levels=[levels[k] for k in ('实战','增幅')]
        local_near=min(local_levels)>=1 and max(local_levels)>=2
        local_balanced=min(local_levels)>=2
        balanced_purple=[o for o in purple if not local_balanced and
            first_strike_key(o.name) in ('实战','增幅') and
            min(levels[k]+(o.gain if first_strike_key(o.name)==k else 0)
                for k in ('实战','增幅'))>=2]
        completing_purple=[o for o in purple if all(
            deficit(k)<=(o.gain if first_strike_key(o.name)==k else 0) for k in levels)]
        if last and local_near and levels['强击']>=3 and s.complete and s.gold_picks==2 and not gold:
            if any(not o.rarity_known for o in options):
                from src.crafting_vision import ChoiceUnreadable
                raise ChoiceUnreadable('最后技能机会的目标外金颜色尚未读清')
        outside_gold=[o for o in options if o.rarity_known and o.rarity==3 and not progress(o)]
        closure=(last and local_near and levels['强击']>=3 and s.complete and
                 s.gold_picks==2 and not gold and outside_gold)
        if closure:
            if balanced_purple or completing_purple:
                choice=best_value(completing_purple or balanced_purple)
                reason=('最后技能机会：本地紫达到2+2并补齐精确目标' if completing_purple else
                        '最后技能机会：优先本地紫补到2+2')
            else:
                choice=best_value(outside_gold)
                reason='最后技能机会：本地紫接近完成，以目标外金补第三金'
            # An allowed closing choice can still leave the exact 3/3/2/3 plan
            # unreachable. Keep that choice; do not reselect by fodder score.
            needs={k:deficit(k) for k in levels}
            key=progress(choice)
            if key:needs[key]=max(0,needs[key]-choice.gain)
            if not can_finish_skills(tuple(needs.values()),gains[1:]):
                missing='、'.join(f'{k}{need}级' for k,need in needs.items() if need)
                s.perfect_mode=False
                s.perfect_reason='最后已确认技能机会按收官规则选择后仍缺'+missing+'，精确目标不可达'
                reason+='；'+s.perfect_reason
            return choice,reason
        if future_known and set(levels)<=known:
            if not can_finish_skills(tuple(deficit(k) for k in levels),gains):
                return fodder('剩余已确认及仍可能的技能机会不足，即使后续全部命中也无法完成')
            def viable(o):
                needs={k:deficit(k) for k in levels}
                key=first_strike_key(o.name);needs[key]=max(0,needs[key]-o.gain)
                return can_finish_skills(tuple(needs.values()),gains[1:])
            gold=[o for o in gold if viable(o)]
            purple=[o for o in purple if viable(o)]
            strike=[o for o in strike if viable(o)]
        if gold:
            completing=[o for o in gold if o.gain>=deficit(first_strike_key(o.name))]
            return best_value(completing or gold),'优先可完成或推进目标的金技能'
        priority_purple=[o for o in purple if levels[progress(o)]<2 or deficit('器械')==0]
        if priority_purple:
            if len(priority_purple)>1 and any(not o.score_known for o in priority_purple):
                from src.crafting_vision import ChoiceUnreadable
                raise ChoiceUnreadable('本地紫技能提升价值尚未读清')
            # Establish 1+1 before 2+0, then 2+2 before pushing either to 3.
            return max(priority_purple,key=lambda o:(-min(levels[first_strike_key(o.name)],2),
                       deficit(first_strike_key(o.name)),o.score,-o.index)), '本地紫补缺：1+1优于2+0，优先补到2+2'
        if not refreshed:
            if refresh_available is True:
                return 'refresh','完美模式无补缺目标金或本地紫，本节点刷新一次'
            if refresh_available is not False:
                from src.crafting_vision import ChoiceUnreadable
                raise ChoiceUnreadable('技能刷新可用状态尚未读清')
        if purple:return best_value(purple),'刷新已用或明确不可用，补本地紫最终目标'
        if strike:return best_value(strike),'刷新已用或明确不可用，补强击目标'
        return fodder(('刷新已用' if refreshed else '刷新明确不可用')+'，仍无目标金、本地紫或可补缺强击')


PROFILES={'旧地联合':OldLandPolicy,'军工':MilitaryPolicy,'一致':UnityPolicy,'先攻':FirstStrikePolicy}
PROFILE_NOTES={
    '旧地联合':('历史策略；当前游戏环境无法正常制作，本季未实机验收','未溢出火攻激增优先；任务例外遵守次数上限。',
              '盾防 → 工程生命；盾攻还需效能最高。属性、核心与恢复沿用原规则。'),
    '军工':('先3金＋本地紫1+1，再优先合法新增第二金并补紫2+2',
           '本地未满级技能：距LV3更近 → 同缺口金色 → 效能 → 页面顺序',
           '缺少当前阶段目标才评估刷新一次；可信身份与历史不足时停止。'),
    '一致':('先3金＋本地紫1+1，再优先合法新增第二金并补紫2+2',
           '本地未满级技能：距LV3更近 → 同缺口金色 → 效能 → 页面顺序',
           '缺少当前阶段目标才评估刷新一次；可信身份与历史不足时停止。'),
    '先攻':('目标：3器械 · 3实战 · 2增幅 · 3强击','目标金 > 本地紫补缺 > 本节点刷新一次 > 补缺强击',
           '1+1优先补2+2；目标外金仅最后机会补第三金；确认无法完成自动转狗粮。'),
}
