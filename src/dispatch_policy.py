"""Bounded, deterministic dispatch constraints; no character names or UI input."""
from dataclasses import dataclass,field
from itertools import combinations
import re

PROFESSIONS=('火力','工程','重盾','医疗','突击','狙击','隐刃')
TACTIC='乌斯怀亚'


class DispatchUnreadable(ValueError):pass


@dataclass(frozen=True)
class Requirements:
    professions: tuple
    same_tactic: int
    base_power: int
    bonus_power: int | None
    slots: int
    selected: int
    tactic_counts: tuple=()
    profession_types: int=0

    def __post_init__(self):
        if not 1<=self.slots<=4 or not 0<=self.selected<=self.slots:
            raise DispatchUnreadable('槽位/选中人数不可靠或超出已验证的1至4槽位')
        if self.base_power<=0 or (self.bonus_power is not None and self.bonus_power<self.base_power):
            raise DispatchUnreadable('基础/额外战力条件矛盾')
        if not 0<=self.same_tactic<=self.slots or any(p not in PROFESSIONS or not 1<=n<=self.slots for p,n in self.professions):
            raise DispatchUnreadable('职业/同战术人数条件不可靠')
        if any(not p or not 1<=n<=self.slots for p,n in self.tactic_counts):
            raise DispatchUnreadable('战术人数条件不可靠')
        if not 0<=self.profession_types<=self.slots:
            raise DispatchUnreadable('职业种类数不可靠')

    @property
    def special(self):return bool(self.professions or self.same_tactic)

    @property
    def filter(self):return '全战术'


@dataclass(frozen=True)
class Candidate:
    identity: str
    profession: str | None
    power: int
    available: bool
    tactic: str
    order: int
    area: tuple=field(default=(),compare=False)
    page: int=field(default=0,compare=False)
    tactics: tuple=()


@dataclass(frozen=True)
class Plan:
    members: tuple
    bonus: bool
    total_power: int | None


def first_available(requirements,candidates):
    """Ordinary missions use the first current cards; page feedback is decisive."""
    if requirements.special:
        raise DispatchUnreadable('特殊派遣不能使用普通前三选择')
    if requirements.selected:raise DispatchUnreadable('已有选择须先清空并复核')
    unique={}
    for candidate in sorted(candidates,key=lambda c:(c.order,c.identity)):
        if candidate.available and candidate.identity not in unique:
            unique[candidate.identity]=candidate
    count=min(3,requirements.slots)
    if not unique:return None
    members=tuple(unique.values())[:count]
    # No single-card OCR sum can authorize a dispatch. The actual condition
    # checks, total power and enabled button must pass after these selections.
    return Plan(members,False,None)


def parse_requirements(lines, *, slots, selected):
    """Parse only the condition pane, not card powers or a mission title."""
    jobs={};same=0;base=None;bonus=None;extra=False;diversity=0
    for value in lines:
        text=re.sub(r'\s+','',value).replace('≧','≥').replace('>=','≥')
        if not text:continue
        if '额外' in text:extra=True
        job=re.fullmatch(r'('+ '|'.join(PROFESSIONS)+r')(?:职业|队员|人员)?(?:人数|数量)?[≥:：](\d{1,2})',text)
        team=re.fullmatch(r'(?:所需)?相同战术(?:队员|人员|人数|人员数|队员数|人员数量|队员数量)?(?:人数|数量)?[≥:：](\d{1,2})',text)
        power=re.search(r'(?:基础|额外奖励|额外)?(?:总)?战力(?:要求)?[≥:：](\d{3,6})',text)
        types=re.fullmatch(r'职业种类数[≥:：](\d{1,2})',text)
        if job:
            p,n=job[1],int(job[2])
            if p in jobs:raise DispatchUnreadable('职业条件重复/矛盾')
            jobs[p]=n
        elif types:
            if diversity:raise DispatchUnreadable('职业种类数重复')
            diversity=int(types[1])
        elif team:
            if same:raise DispatchUnreadable('同战术条件重复')
            same=int(team[1])
        elif power:
            if '额外' in text or extra:
                if bonus is not None:raise DispatchUnreadable('额外战力条件重复')
                bonus=int(power[1])
            else:
                if base is not None:raise DispatchUnreadable('基础战力条件重复')
                base=int(power[1])
        elif any(x in text for x in ('≥','职业','相同战术','战力要求')):
            raise DispatchUnreadable('无法解释的派遣条件：'+text)
    if base is None:raise DispatchUnreadable('未读取基础战力')
    return Requirements(tuple(sorted(jobs.items())),same,base,bonus,slots,selected,profession_types=diversity)


def choose(requirements, candidates):
    if requirements.selected:raise DispatchUnreadable('已有选择须先清空并复核')
    # At most 24 candidates x 4 slots (<13k combinations); identical portraits
    # across scroll pages are not independent people.
    unique={}
    for c in sorted(candidates,key=lambda c:(c.order,c.identity)):
        if c.available and c.power>0 and c.identity not in unique:unique[c.identity]=c
    if len(unique)>24:raise DispatchUnreadable('候选超过有限搜索上限24人')
    pool=tuple(unique.values());best=None;best_key=None
    for size in range(1,requirements.slots+1):
        for members in combinations(pool,size):
            if any(sum(c.profession==p for c in members)<n for p,n in requirements.professions):continue
            if requirements.same_tactic and not any(
                    sum(c.tactic==t or t in c.tactics for c in members)>=requirements.same_tactic
                    for t in {t for c in members for t in (c.tactic,*c.tactics)}-{'全战术',''}):continue
            if len({c.profession for c in members if c.profession})<requirements.profession_types:continue
            if any(sum(p in c.tactics or c.tactic==p for c in members)<n for p,n in requirements.tactic_counts):continue
            total=sum(c.power for c in members)
            if total<requirements.base_power:continue
            bonus=requirements.bonus_power is None or total>=requirements.bonus_power
            # Mandatory conditions decide the team; optional rewards never add people.
            key=(len(members),tuple(c.order for c in members),tuple(c.identity for c in members))
            if best_key is None or key<best_key:best_key=key;best=Plan(members,bonus,total)
    return best
