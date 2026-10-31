"""Deterministic normal-skill decisions for 军工 and 一致 only.

The ledger supplies confirmed identities and levels.  This module never learns
skill names from colours, commits clicks, spends resources or changes a record.
``Option.level`` is the current level in the observed ``LVx+gain`` title.
"""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class LocalSkillDecision:
    action: str
    reason_code: str
    phase: str
    reason: str
    selected_index: int | None = None
    missing_goals: tuple = ()

    def legacy(self, options):
        if self.action == 'refresh':
            return 'refresh', self.reason
        selected = [o for o in options if o.index == self.selected_index]
        if self.action != 'select' or len(selected) != 1:
            _unreadable('策略选中技能与当前候选不唯一，未沿用旧选择')
        return selected[0], self.reason


def _unreadable(message):
    from src.crafting_vision import ChoiceUnreadable
    raise ChoiceUnreadable(message)


def _best_value(options):
    """Value and actual page order break ties; gain is deliberately absent."""
    if not options:
        _unreadable('当前没有已知合法且未满级的技能候选')
    if len(options) > 1 and any(not o.score_known or type(o.score) not in (int, float)
                              or not isfinite(o.score) for o in options):
        _unreadable('同优先级技能效能尚未读清，无法确定页面顺序前的排序')
    return max(options, key=lambda o: (o.score, -o.index))


def choose_local_upgrade(options):
    """Rank already-proven local candidates, independently of stage reachability.

    Keeping this ordering primitive separate permits a direct LV2-vs-LV1 test
    without fabricating a stage-three record whose two target purples are 2+2
    while one of those same purples is also claimed to be LV1.
    """
    options = [o for o in options if o.visual_enabled is not False]
    if any(not o.level_known or type(o.level) is not int or not 0 <= o.level <= 3
           or not o.rarity_known or o.rarity not in (1, 2, 3) for o in options):
        _unreadable('本地技能的当前等级或品质尚未读清')
    options = [o for o in options if o.level < 3]
    if not options:
        _unreadable('当前本地技能均不可提升，不能将满级技能排到首位')
    closest = max(o.level for o in options)
    pool = [o for o in options if o.level == closest]
    gold = [o for o in pool if o.rarity == 3]
    return _best_value(gold or pool)


def decide_local_skill(options, state, profile, refreshed=False, refresh_available=None):
    """Return one explainable action from the current confirmed piece state.

    A second gold has its special priority whenever the base goals are met and
    it is actually offered, including after purple 2+2.  Its absence does not
    invent a guaranteed offer or force another refresh before useful purples.
    """
    from src.crafting_skill_state import (LocalSkillStateError, candidate_is_usable,
        identity_for_option, is_usable_gold, normalize_skill_candidate)

    if profile not in ('军工', '一致'):
        raise ValueError('本策略仅适用于军工与一致')
    if state.profile != profile:
        _unreadable('UNKNOWN：技能历史不属于当前模组，不能跨模组复用')
    if not state.trusted:
        _unreadable('UNKNOWN：当前件技能历史不可信；' + state.reason)
    if not state.purple_targets_known:
        _unreadable('UNKNOWN：两种本地紫技能身份尚未完整确认')
    purples = tuple(state.purple_identities)
    if (len(purples) != 2 or len(set(purples)) != 2
            or any(p.module != profile or p.rarity != 2 or p.kind != 'skill' for p in purples)):
        _unreadable('UNKNOWN：本地紫目标必须是当前模组的两种不同普通紫技能')
    purple_levels = state.purple_levels
    if any(p not in purple_levels or type(purple_levels[p]) is not int
           or not 0 <= purple_levels[p] <= 3 for p in purples):
        _unreadable('UNKNOWN：两种本地紫技能当前等级证据不完整')
    gold_levels = {identity: level for identity, level in state.gold_levels.items() if level > 0}
    if (any(identity.rarity != 3 or identity.kind != 'skill' or type(level) is not int
            or not 1 <= level <= 3 for identity, level in gold_levels.items())
            or type(state.gold_total) is not int or state.gold_total != sum(gold_levels.values())):
        _unreadable('UNKNOWN：已确认金技能身份、等级与累计值不一致')
    # Goal credit needs proven usability. Ownership does not: a confirmed
    # outside gold still occupies one of the two identities this policy will
    # prioritize, even when it cannot contribute to the 3-gold goal.
    owned_gold_levels = {identity: level for identity, level in state.levels.items()
                         if identity.rarity == 3 and identity.kind == 'skill'}
    if any(type(level) is not int or not 0 <= level <= 3 for level in owned_gold_levels.values()):
        _unreadable('UNKNOWN：已拥有普通金技能的等级证据不完整')
    owned_gold = {identity for identity, level in owned_gold_levels.items() if level > 0}

    one_each = all(purple_levels[p] >= 1 for p in purples)
    two_each = all(purple_levels[p] >= 2 for p in purples)
    phase = ('BASE' if state.gold_total < 3 or not one_each
             else 'BALANCE' if not two_each else 'UPGRADE')
    missing = tuple(goal for goal, done in (
        ('gold_3', state.gold_total >= 3), ('purple_1_1', one_each),
        ('second_gold', len(owned_gold) >= 2), ('purple_2_2', two_each)) if not done)
    options = tuple(options)
    if not options:
        _unreadable('技能候选尚未识别，不能把空画面当作目标缺席')
    if any(type(o.index) is not int or o.index < 0 for o in options) or len({o.index for o in options}) != len(options):
        _unreadable('技能候选页面位置缺失或重复')

    viable = []
    identities = {}
    try:
        for option in options:
            if option.visual_enabled is False or getattr(option, 'skill_usable', None) is False:
                continue
            identity = identity_for_option(option)
            # A foreign skill without positive usability evidence is outside
            # the legal pool; its irrelevant level must not block a proved
            # local/generic target or be reinterpreted as ordinary progress.
            if not candidate_is_usable(option, profile):
                continue
            if not option.level_known or type(option.level) is not int or not 0 <= option.level <= 3:
                _unreadable('技能候选当前等级不可信')
            current = state.levels.get(identity, 0)
            if current != option.level:
                _unreadable('UNKNOWN：技能候选当前等级与本件已确认历史冲突')
            # A displayed LV3 card is not an upgrade, even if its title retains
            # a +1/+2 node label.  Exclude it before gain/cap validation.
            if option.level == 3:
                continue
            normalize_skill_candidate(option, profile)
            identities[option.index] = identity
            viable.append(option)
    except LocalSkillStateError as error:
        _unreadable('UNKNOWN：技能身份、归属或等级证据不可信；' + str(error))

    def selected(pool, code, reason, ordinary=False):
        option = choose_local_upgrade(pool) if ordinary else _best_value(pool)
        return LocalSkillDecision('select', code, phase, reason, option.index, missing)

    def no_target(target):
        if not refreshed and refresh_available is None:
            _unreadable('当前阶段目标缺席，但本节点技能刷新可用状态尚未确认')
        if not refreshed and refresh_available is True:
            return LocalSkillDecision('refresh', 'REFRESH_TARGET_MISSING', phase,
                '当前缺少' + target + '，本节点评估资源后最多刷新一次', missing_goals=missing)
        local = [o for o in viable if identities[o.index].module == profile]
        if local:
            return selected(local, 'FALLBACK_LOCAL_GOAL_PENDING',
                '刷新已用或明确不可用，按本地升级规则兜底；当前目标仍未完成：' + target, ordinary=True)
        if viable:
            return selected(viable, 'FALLBACK_VALUE_GOAL_PENDING',
                '刷新已用或明确不可用，选择合法候选中效能最高者；当前目标仍未完成：' + target)
        _unreadable('刷新已用或明确不可用，仍无合法未满级技能；当前目标尚未完成：' + target)

    eligible_gold = [o for o in viable if is_usable_gold(o, profile)]
    if state.gold_total < 3:
        # This is a priority limit on gold identities, not a fictional slot cap.
        gold = [o for o in eligible_gold if identities[o.index] in owned_gold or len(owned_gold) < 2]
        if gold:
            return selected(gold, 'GOLD_TO_THREE', '阶段一：优先增加已确认金技能等级，达到累计3金')
        missing_purple = [o for o in viable if identities[o.index] in purples
                          and purple_levels[identities[o.index]] == 0]
        if missing_purple:
            return selected(missing_purple, 'PURPLE_WHILE_GOLD_ABSENT',
                '阶段一：当前没有合格补金候选，先补缺失本地紫；3金目标仍未完成')
        return no_target('能补足3金或缺失本地紫的技能')

    if not one_each:
        missing_purple = [o for o in viable if identities[o.index] in purples
                          and purple_levels[identities[o.index]] == 0]
        if missing_purple:
            return selected(missing_purple, 'PURPLE_ONE_MISSING',
                '阶段一：优先补尚为LV0的本地紫，先完成两种紫1+1')
        return no_target('另一种尚缺的本地紫技能')

    if len(owned_gold) == 1:
        new_gold = [o for o in eligible_gold if identities[o.index] not in owned_gold and o.level == 0]
        if new_gold:
            return selected(new_gold, 'NEW_SECOND_GOLD',
                '基础已完成：合法新增第二种金技能优先于紫升级与普通等级排序')

    if not two_each:
        lowest = min(purple_levels[p] for p in purples)
        purple = [o for o in viable if identities[o.index] in purples
                  and purple_levels[identities[o.index]] == lowest]
        if purple:
            return selected(purple, 'PURPLE_BALANCE_TWO',
                '阶段二：优先补当前等级最低的本地紫，逐步达到两种紫2+2')
        return no_target('能补齐紫2+2的较低等级本地紫技能')

    local = [o for o in viable if identities[o.index].module == profile]
    if local:
        return selected(local, 'LOCAL_UPGRADE_ORDER',
            '普通升级：本地技能优先，当前等级距LV3更近 → 同缺口金色 → 效能 → 页面顺序', ordinary=True)
    return no_target('可提升的本地技能')
