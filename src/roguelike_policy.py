"""User/Word-derived roguelike decisions, with no game input."""
from dataclasses import dataclass, field
from enum import StrEnum


class RogueState(StrEnum):
    UNKNOWN = 'UNKNOWN'
    TRANSIENT = 'TRANSIENT'
    MAIN_MENU = 'MAIN_MENU'
    NAV = 'NAV'
    ENTRY = 'ENTRY'
    START = 'START'
    MAP = 'MAP'
    EVENT = 'EVENT'
    CHOICE = 'CHOICE'
    OUTPOST = 'OUTPOST'
    ORDER = 'ORDER'
    ENCOUNTER = 'ENCOUNTER'
    SPECIAL_OPERATION = 'SPECIAL_OPERATION'
    BATTLE_PREP = 'BATTLE_PREP'
    BATTLE = 'BATTLE'
    BATTLE_RESULT = 'BATTLE_RESULT'
    SHOP = 'SHOP'
    ACCIDENT = 'ACCIDENT'
    SEARCH = 'SEARCH'
    BOUNTY = 'BOUNTY'
    OVERLAY = 'OVERLAY'
    SETTLEMENT_CONFIRM = 'SETTLEMENT_CONFIRM'
    RESULT_STATS = 'RESULT_STATS'
    RESULT_TEAM = 'RESULT_TEAM'
    RESULT = 'RESULT'


@dataclass(frozen=True)
class RogueAction:
    key: str
    area: tuple
    text: str = ''
    enabled: bool = True
    irreversible: bool = True


@dataclass(frozen=True)
class Candidate:
    index: int
    area: tuple
    quality: str = ''
    glowing: bool = False
    selectable: bool = True
    selected: bool = False
    fingerprint: str = ''
    identity: str = ''
    role: str = 'unknown'
    price: int | None = None


@dataclass(frozen=True)
class Observation:
    state: RogueState = RogueState.UNKNOWN
    event: str = ''
    actions: tuple = ()
    candidates: tuple = ()
    floor: int | None = None
    evidence: tuple = ()
    deployment_count: int | None = None
    deployment_area: tuple | None = None
    result_numbers: dict = field(default_factory=dict)
    detail: str = ''
    deployment_selected: bool = False
    deployment_signature: tuple = ()
    currency: int | None = None
    refresh_cost: int | None = None

    def action(self, key):
        matches = [a for a in self.actions if a.key == key and a.enabled]
        return matches[0] if len(matches) == 1 else None

    def signature(self):
        # Numeric result rewards never establish page identity.
        return (self.state, self.event, self.floor,
                tuple((a.key, a.text, a.enabled) for a in self.actions),
                tuple((c.index, c.quality, c.glowing, c.identity, c.selected, c.fingerprint)
                      for c in self.candidates), self.deployment_count, self.deployment_selected)


class DecisionUndefined(RuntimeError):
    pass


def ordered(candidates):
    return sorted(candidates, key=lambda c:(c.area[1], c.area[0], c.index))


def first_choice(candidates):
    choices=ordered(candidates)
    if not choices or not choices[0].selectable:raise DecisionUndefined('未识别到可选的第一项')
    return choices[0]


def member_reason(candidate):
    return ('gold' if candidate.quality=='gold' else 'glowing' if candidate.glowing else
            'purple' if candidate.quality=='purple' else 'vera' if candidate.identity=='vera' else None)


def choose_member(candidates):
    # Free mandatory member selection is distinct from paid shop eligibility.
    choices=[c for c in candidates if c.selectable]
    if any(c.quality not in ('gold','purple','blue') for c in choices):
        raise DecisionUndefined('队员品质颜色处于未知范围，无法决定优先级')
    eligible=[c for c in choices if member_reason(c) is not None]
    if not eligible:return first_choice(choices) if choices else None
    ranks={'gold':0,'glowing':1,'purple':2,'vera':3}
    return min(eligible,key=lambda c:(ranks[member_reason(c)],c.area[0],c.area[1],c.index))


def choose_route(actions):
    nodes=[a for a in actions if a.enabled and a.key.startswith('node_')]
    if len(nodes)==1:return nodes[0]
    if len(nodes)==2 and abs(nodes[0].area[0]-nodes[1].area[0])<150:
        return max(nodes,key=lambda a:a.area[1])
    raise DecisionUndefined('可选路线并非已定义的单节点/上下分叉，停止未定义路线选择')


def unique_action(actions):
    legal=[a for a in actions if a.enabled]
    if len(legal)!=1:raise DecisionUndefined('已确认事件的 action 区不是唯一合法选项')
    return legal[0]


def order_action(actions):
    options=[a for a in actions if a.enabled and '火种调令' in a.text]
    if len(options)!=1:raise DecisionUndefined('未可靠识别火种调令')
    return options[0]


def search_action(actions,floor=None):
    options=[a for a in actions if a.enabled and '小心陷阱' in a.text]
    if len(options)==1:return options[0]
    return unique_action(actions)


def bounty_action(actions):
    options=sorted((a for a in actions if a.enabled),key=lambda a:a.area[1])
    if len(options)<2 or '实训计划' not in options[1].text:
        raise DecisionUndefined('悬赏委托第二项不是已确认的实训计划')
    return options[1]


@dataclass
class ShopVisit:
    kind: str
    refresh_used: bool = False
    purchases: int = 0
    fallback_used: bool = False

    def reserve_refresh(self):
        if self.kind!='NORMAL_BATTLE_SHOP' or self.refresh_used:
            raise DecisionUndefined('本次商店禁止再次刷新')
        self.refresh_used=True  # Reserve before input; uncertain outcomes never reset it.

    def reserve_purchase(self,candidate):
        # Reserve the one fallback attempt at dispatch, including an uncertain
        # result or pause immediately after input. Never reserve during ranking.
        if shop_tier(candidate)=='fallback':
            if self.fallback_used:raise DecisionUndefined('本次商店低档兑换额度已使用')
            self.fallback_used=True

    def record_purchase(self,candidate):
        self.purchases+=1
        if shop_tier(candidate)=='fallback':self.fallback_used=True


@dataclass(frozen=True)
class ShopDecision:
    action: str
    reason: str
    candidate: Candidate | None = None
    reserve_after_purchase: int | None = None


def shop_tier(candidate):
    if candidate.quality=='gold':return 'gold'
    if candidate.glowing:return 'glowing'
    if candidate.identity=='vera' or (candidate.quality=='purple' and candidate.role in ('firepower','hidden_blade')):
        return 'fallback'
    return None


def choose_shop_member(candidates,currency,tiers=('gold','glowing','fallback')):
    choices=[]
    for c in candidates:
        if not c.selectable or shop_tier(c) not in tiers:continue
        if type(c.price) is not int or c.price<0:
            raise DecisionUndefined(f'商店候选{c.index}价格未知，不能判断是否可购买')
        if c.price<=currency:choices.append(c)
    ranks={'gold':0,'glowing':1,'fallback':2}
    return min(choices,key=lambda c:(ranks[shop_tier(c)],c.price,c.area[0],c.index)) if choices else None


def shop_decision(candidates,visit,refresh_available=True,*,currency=None,refresh_cost=None,eligible_before_refresh=False):
    if visit.kind=='SPECIAL_OPERATION_SHOP':return ShopDecision('back','special_operation_shop')
    if visit.kind!='NORMAL_BATTLE_SHOP':raise DecisionUndefined('商店来源未知；不允许兑换或刷新')
    if visit.fallback_used:return ShopDecision('back','fallback_already_purchased')
    if type(currency) is not int or currency<0:raise DecisionUndefined('商店青色货币余额未知')
    best=choose_shop_member(candidates,currency,('gold','glowing'))
    if best:return ShopDecision('exchange',shop_tier(best),best,currency-best.price)
    if currency==6:
        vera=choose_shop_member(tuple(c for c in candidates if c.identity=='vera'),currency,('fallback',))
        if vera:return ShopDecision('exchange','balance_6_vera',vera,currency-vera.price)
        return ShopDecision('back','balance_6_no_glowing_blue_or_vera')
    if currency<7:return ShopDecision('back','balance_below_7_no_affordable_high_priority')
    if eligible_before_refresh:
        best=choose_shop_member(candidates,currency,('fallback',))
        if best and currency-best.price>=6:
            return ShopDecision('exchange','eligible_fallback_preserving_6',best,currency-best.price)
    if not visit.refresh_used:
        if not refresh_available:return ShopDecision('back','refresh_unavailable_before_fallback')
        if type(refresh_cost) is not int or refresh_cost<0:raise DecisionUndefined('商店刷新价格未知')
        if currency<refresh_cost:return ShopDecision('back','refresh_unaffordable_before_fallback')
        return ShopDecision('refresh','no_high_priority_and_balance_ge_7')
    best=choose_shop_member(candidates,currency,('fallback',))
    if best and currency-best.price>=6:
        reason='fallback_vera_after_refresh' if best.identity=='vera' else 'fallback_purple_'+best.role+'_after_refresh'
        return ShopDecision('exchange',reason,best,currency-best.price)
    return ShopDecision('back','no_fallback_preserving_6')


@dataclass
class SettlementLedger:
    progress: int = 0
    completed_runs: int = 0

    def next_key(self):
        if not 0<=self.progress<5:raise DecisionUndefined('本轮结算已经完成')
        return ('terminate','confirm','next','next','confirm')[self.progress]

    def verify(self,action,outcome,*,count_completion=True):
        if action!=self.next_key():raise DecisionUndefined('结算顺序不符')
        expected=(RogueState.SETTLEMENT_CONFIRM,RogueState.RESULT_STATS,RogueState.RESULT_TEAM,RogueState.RESULT,RogueState.START)[self.progress]
        if outcome.state!=expected:
            raise DecisionUndefined('结算动作结果未可靠确认，不重放')
        if self.progress==4 and (not outcome.action('start') or outcome.action('terminate')):
            raise DecisionUndefined('最终确认后尚未可靠返回空闲挑战页')
        self.progress+=1
        if self.progress==5 and count_completion:self.completed_runs+=1

    def begin_next_run(self):
        if self.progress!=5:raise DecisionUndefined('前一轮最终确认尚未完成')
        self.progress=0
