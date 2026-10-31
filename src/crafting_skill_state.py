"""Confirmed ordinary-skill state for the military and unity profiles.

The legacy name-only cache is a display/compatibility projection.  Decisions
replay this per-craft ledger, whose identities include ownership and rarity.
An observed candidate is never an upgrade.  Only a complete confirmation
transaction (or an independently observed result) can add a ledger event.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from types import SimpleNamespace


LOCAL_PROFILES = frozenset(('军工', '一致'))
_SUCCESSOR_KINDS = frozenset(('hub', 'result', 'overclock', 'attribute', 'mission',
                              'core_attribute', 'core_skill', 'factor', 'rest'))
# The second purple identities were checked against the source PNG, not
# inferred from old-land names or the current module's colour alone.
LOCAL_PURPLE_CATALOG = {
    '军工': {
        '机动': 'docs/validation/season-module-live-acceptance-20261011.json#rounds[3].programRecord.history',
        '指挥': 'tests/fixtures/crafting-opportunity-chain/depth-before-confirm.png#card3:git-f8dbc8c3efeff10de5e3e70300ffc8f2e43b9fc2',
    },
    '一致': {
        '高频交易': 'tests/fixtures/crafting-profile-target-720p/observations.json#skill.options[2]',
        '裂变营销': 'tests/fixtures/crafting-opportunity-chain/depth-before-confirm.png#card4:git-f8dbc8c3efeff10de5e3e70300ffc8f2e43b9fc2',
    },
}
COMMON_BLUE_CATALOG = {
    '强击': 'tests/fixtures/crafting-opportunity-chain/depth-before-confirm.png#card0',
    '装甲': 'tests/fixtures/crafting-profile-target-720p/purple-unprefixed.png#card1',
    '屏息': 'tests/fixtures/crafting-opportunity-chain/depth-before-confirm.png#card2',
    '健体': 'tests/fixtures/crafting-first-strike-roles-720p/amplifier-and-beacon.png#card3',
    '疾射': 'tests/fixtures/astra-ten-720p/skill-purple-glyph.png#card3',
}
FIRST_STRIKE_PURPLE_CATALOG = {
    '实战精进': 'tests/fixtures/crafting-profile-target-720p/purple-unprefixed.png#card3',
    '增幅力场': 'tests/fixtures/crafting-first-strike-roles-720p/amplifier-and-beacon.png#card5',
}


class LocalSkillStateError(ValueError):
    """The available evidence cannot authorize a skill-state mutation."""


@dataclass(frozen=True, order=True)
class SkillIdentity:
    name: str
    rarity: int
    module: str
    kind: str = 'skill'

    @property
    def key(self):
        return (self.kind, self.module, self.rarity, self.name)

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class LocalSkillState:
    trusted: bool
    reason: str = ''
    craft_id: str = ''
    profile: str = ''
    levels: dict = field(default_factory=dict)
    gold_levels: dict = field(default_factory=dict)
    gold_total: int | None = None
    purple_identities: tuple = ()
    purple_levels: dict = field(default_factory=dict)
    purple_targets_known: bool = False
    phase: str = 'UNKNOWN'
    has_second_gold: bool = False
    purple_one_complete: bool = False
    purple_two_complete: bool = False
    refresh_nodes: dict = field(default_factory=dict)

    @property
    def missing(self):
        return self.reason

    @property
    def confirmed_gold_total(self):
        """All confirmed ordinary gold, even if an outside skill is unusable."""
        if not self.trusted:
            return None
        return sum(level for identity, level in self.levels.items() if identity.rarity == 3)


def _require(condition, reason):
    if not condition:
        raise LocalSkillStateError(reason)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _owner(value):
    _require(isinstance(value, str), '技能归属字段类型不可信')
    value = value.strip()
    return {'战线': '一致', '旧地': '旧地联合'}.get(value, value)


def _title(value):
    """Reuse production title aliases, then remove separators/level suffixes."""
    _require(isinstance(value, str), '技能标题类型不可信')
    value = re.sub(r'\s+', '', value).strip('◇◆◈♦♢·•・')
    value = re.sub(r'^A(?=[\u4e00-\u9fff])', '', value)
    # skill_option already applies this existing normalizer to Option.name,
    # while raw_name retains the original OCR (observed: 实占精进LV.2+1).
    # Apply the identical rule for comparison; never rewrite the raw evidence.
    from src.crafting_policy import skill_title
    value = skill_title(value)
    value = re.sub(r'(?i)L[VY][.·:]?\d+\+\d+$', '', value)
    match = re.fullmatch(r'([\u4e00-\u9fff]{2,8})[·•・:：]([\u4e00-\u9fff]{1,16})', value)
    if match:
        return _owner(match[1]), match[2]
    # Current OCR has also observed a lost separator after these known owners.
    match = re.fullmatch(r'(军工|一致|战线|先攻)([\u4e00-\u9fff]{1,16})', value)
    if match:
        return _owner(match[1]), match[2]
    _require(bool(re.fullmatch(r'[\u4e00-\u9fff]{1,16}', value)), '技能名称尚未完整识别')
    return '', value


def identity_for_option(option):
    """Require ordinary-skill identity; bare names never supply ownership."""
    _require(getattr(option, 'skill_kind', 'skill') == 'skill', '战术核心或随机结算不是普通技能升级')
    rarity = getattr(option, 'rarity', None)
    _require(getattr(option, 'rarity_known', False) is True and type(rarity) is int
             and rarity in (1, 2, 3), '技能品质尚未读清')
    named_owner, name = _title(getattr(option, 'name', ''))
    owners = [value for value in (_owner(getattr(option, 'module', '')), named_owner) if value]
    raw_name = getattr(option, 'raw_name', '')
    if raw_name:
        raw_owner, observed_name = _title(raw_name)
        _require(observed_name == name, '技能名称与原始标题冲突')
        if raw_owner:
            owners.append(raw_owner)
    # These common blue skills are visibly unprefixed in real captures.
    # This catalogue is identity evidence, not a blanket "all blue is common"
    # rule.  An explicit foreign owner always takes precedence.
    if not owners and rarity == 1 and name in COMMON_BLUE_CATALOG:
        owners.append('通用')
    if not owners and rarity == 2:
        # These exact unprefixed titles already have a sourced first-strike
        # identity in the existing policy. Keep that foreign identity so an
        # irrelevant card does not invalidate a readable local target.
        from src.crafting_policy import FIRST_STRIKE_UNPREFIXED_PURPLE
        if name in FIRST_STRIKE_UNPREFIXED_PURPLE and name in FIRST_STRIKE_PURPLE_CATALOG:
            owners.append('先攻')
    _require(bool(owners) and len(set(owners)) == 1 and owners[0] not in ('未知', 'UNKNOWN', 'unknown'),
             '技能归属缺失或相互冲突')
    return SkillIdentity(name, rarity, owners[0])


def identity_source_for_option(option):
    identity = identity_for_option(option)
    name_owner, _ = _title(option.name)
    raw_owner, _ = _title(option.raw_name) if getattr(option, 'raw_name', '') else ('', '')
    if name_owner or raw_owner:
        return 'explicit_skill_title'
    if getattr(option, 'module', ''):
        return 'explicit_module_field'
    if identity.module == '先攻' and identity.rarity == 2 and identity.name in FIRST_STRIKE_PURPLE_CATALOG:
        return FIRST_STRIKE_PURPLE_CATALOG[identity.name]
    _require(identity.rarity == 1 and identity.name in COMMON_BLUE_CATALOG,
             '无前缀技能缺少已确认通用目录证据')
    return COMMON_BLUE_CATALOG[identity.name]


def normalize_skill_candidate(option, profile):
    _require(profile in LOCAL_PROFILES, '本轮技能策略只适用于军工和一致')
    identity = identity_for_option(option)
    _require(getattr(option, 'level_known', False) is True and type(option.level) is int
             and 0 <= option.level <= 3, '普通技能当前等级尚未读清或超过上限')
    _require(type(option.gain) is int and option.gain > 0, '普通技能升级量尚未读清')
    # A full skill is safely filtered by the strategy.  An unconfirmed LV2+2
    # cannot be silently clipped, because the outcome has not been observed.
    _require(option.level == 3 or option.level + option.gain <= 3,
             '候选升级结果超过LV3，缺少实际封顶结果证据')
    return identity


def candidate_is_usable(option, profile):
    identity = identity_for_option(option)
    if getattr(option, 'visual_enabled', None) is False or getattr(option, 'skill_usable', None) is False:
        return False
    if identity.module in (profile, '通用'):
        return True
    return (getattr(option, 'skill_usable', None) is True
            and _text(getattr(option, 'usability_evidence_id', '')))


def is_usable_gold(option, profile):
    return identity_for_option(option).rarity == 3 and candidate_is_usable(option, profile)


def _identity(value):
    _require(isinstance(value, dict) and set(value) == {'name', 'rarity', 'module', 'kind'},
             '账本缺少完整技能身份')
    identity = identity_for_option(SimpleNamespace(**value, rarity_known=True, skill_kind=value.get('kind')))
    _require(asdict(identity) == value, '账本技能身份不是规范形式')
    return identity


def _digest(ledger):
    fields = {key: value for key, value in ledger.items() if key != 'integrity'}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _seal(ledger):
    ledger['integrity'] = _digest(ledger)


def _validate_baseline(baseline):
    _require(isinstance(baseline, dict) and baseline.get('source') == 'verified_new_craft',
             '缺少已验证的新件零基线，不能把未知历史当作0')
    _require(_text(baseline.get('evidence_id')) and _text(baseline.get('before_epoch'))
             and _text(baseline.get('after_epoch'))
             and baseline['before_epoch'] != baseline['after_epoch'], '新件启动缺少独立前后帧证据')
    _require(baseline.get('from_kind') == 'prepared' and baseline.get('to_kind') == 'hub'
             and baseline.get('input_status') == 'native_submitted', '新件启动事务尚未验证')


def initialize_local_skill_ledger(record, profile, craft_id, start_evidence, *,
                                  purple_identities=None, run_id=None):
    """Call only after the existing controlled experiment-start succeeds."""
    _require(profile in LOCAL_PROFILES and _text(craft_id), '新件技能账本缺少模组或件身份')
    _require(not getattr(record, 'local_skill_ledger', None), '本件技能账本已存在，禁止覆盖或清零')
    _require(getattr(record, 'complete', False) is True and not record.skills and not record.gold_picks
             and not any(row.get('kind') == 'skill' for row in record.history),
             '已有或未知技能历史不能初始化为新件0等级')
    _validate_baseline(start_evidence)
    ledger = {'version': 1, 'profile': profile, 'craft_id': craft_id, 'run_id': run_id or craft_id,
              'baseline': deepcopy(start_evidence), 'purple_targets': [], 'purple_observations': [],
              'events': [], 'refresh_nodes': {}, 'unknown_reasons': []}
    _require(_text(ledger['run_id']), '新件技能账本缺少运行身份')
    _seal(ledger)
    record.local_skill_ledger = ledger
    if purple_identities is None:
        for name, evidence_id in LOCAL_PURPLE_CATALOG[profile].items():
            register_local_purple_targets(record, profile, (SkillIdentity(name, 2, profile),),
                                          evidence_id=evidence_id)
    elif purple_identities:
        register_local_purple_targets(record, profile, purple_identities,
                                      evidence_id=start_evidence['evidence_id'])
    return build_local_skill_state(record, profile)


def _valid_ledger(record, profile):
    ledger = getattr(record, 'local_skill_ledger', None)
    _require(isinstance(ledger, dict) and bool(ledger), '技能历史UNKNOWN：缺少本件可信身份账本')
    _require(type(ledger.get('version')) is int and ledger['version'] == 1
             and ledger.get('profile') == profile and profile in LOCAL_PROFILES,
             '技能账本版本或模组归属冲突')
    _require(_text(ledger.get('craft_id')) and _text(ledger.get('run_id')), '技能账本缺少件或运行身份')
    _require(ledger.get('integrity') == _digest(ledger), '技能账本完整性校验失败，历史可能缺失或被覆盖')
    _validate_baseline(ledger.get('baseline'))
    _require(isinstance(ledger.get('unknown_reasons'), list) and not ledger['unknown_reasons'],
             '技能历史UNKNOWN：' + '、'.join(map(str, ledger.get('unknown_reasons', ()))))
    _require(getattr(record, 'complete', False) is True and not getattr(record, 'unknown_skills', ()),
             '技能历史不完整或有未确认的技能')
    for key, expected in (('events', list), ('purple_targets', list),
                          ('purple_observations', list), ('refresh_nodes', dict)):
        _require(isinstance(ledger.get(key), expected), '技能账本字段缺失：' + key)
    return ledger


def _validate_proof(proof, *, ledger, identity, level_before, gain, level_after, index, event_id, node_id):
    _require(type(index) is int and index >= 0, '技能确认缺少真实页面位置')
    _require(isinstance(proof, dict) and proof.get('source') in
             ('verified_skill_transition', 'ordinary_skill_result'), '技能提交缺少可信确认来源')
    _require(all(proof.get(key) == expected for key, expected in
                 (('craft_id', ledger['craft_id']), ('run_id', ledger['run_id']),
                  ('event_id', event_id), ('node_id', node_id))), '技能确认属于其他件、节点或事务')
    candidate, selected, confirmation, result = (proof.get(key) for key in
                                                ('candidate', 'selected', 'confirmation', 'result'))
    _require(all(isinstance(item, dict) for item in (candidate, selected, confirmation, result)),
             '技能确认事务证据链不完整')
    for observed in (candidate, selected):
        _require(_identity(observed.get('identity')) == identity
                 and type(observed.get('level')) is int and observed['level'] == level_before
                 and type(observed.get('gain')) is int and observed['gain'] == gain
                 and type(observed.get('index')) is int and observed['index'] == index,
                 '已选中技能身份、等级、升级量或位置与候选冲突')
    _require(selected.get('selected_indices') == [index], '尚未证明唯一选中目标技能')
    epochs = [observed.get('observation_epoch') for observed in (candidate, selected, result)]
    _require(all(_text(epoch) for epoch in epochs) and len(set(epochs)) == 3,
             '技能候选、选中和结果必须具有不同的新帧证据')
    _require(all(_text(observed.get('evidence_id')) for observed in (candidate, selected, result)),
             '技能确认缺少可审计证据引用')
    times = [candidate.get('observed_ns'), selected.get('observed_ns'),
             confirmation.get('sent_ns'), result.get('observed_ns')]
    _require(all(type(value) is int and value > 0 for value in times)
             and all(a < b for a, b in zip(times, times[1:])), '技能确认的观察与输入顺序不可信')
    intervention = confirmation.get('manual_intervention')
    unattended = confirmation.get('unattended_verified')
    _require(type(unattended) is bool and (intervention is not None or unattended is False)
             and (unattended is False or intervention is False), '无人干预结论缺少相应观察证据')
    _require(confirmation.get('transaction_id') == event_id
             and confirmation.get('input_status') == 'native_submitted'
             and confirmation.get('from_epoch') == selected['observation_epoch']
             and (intervention is None or type(intervention) is bool),
             '确认未完整发送或来源不明')
    _require(result.get('kind') in _SUCCESSOR_KINDS, '确认后尚未证实离开原技能节点')
    if proof['source'] == 'verified_skill_transition':
        _require(intervention is not True and level_after == level_before + gain and level_after <= 3,
                 '受控转场只能记已证实的升级量，不能推测截断或随机结算')
    else:
        _require(_identity(result.get('identity')) == identity
                 and type(result.get('level')) is int and result['level'] == level_after,
                 '独立普通技能结果身份或等级不完整')


def _replay(record, profile):
    ledger = _valid_ledger(record, profile)
    levels, usable, events, nodes = {}, {}, {}, set()
    for event in ledger['events']:
        _require(isinstance(event, dict), '技能事件格式不可信')
        identity = _identity(event.get('identity'))
        event_id, node_id = event.get('event_id'), event.get('node_id')
        _require(_text(event_id) and _text(node_id) and event_id not in events and node_id not in nodes,
                 '技能事件或节点重复，不能重复累计')
        _require(event.get('craft_id') == ledger['craft_id'], '技能事件跨件混算')
        before, after, actual, offered = (event.get(key) for key in
                                         ('level_before', 'level_after', 'actual_gain', 'offered_gain'))
        _require(all(type(value) is int for value in (before, after, actual, offered))
                 and before == levels.get(identity, 0) and 0 <= before < after <= 3
                 and actual == after - before and 0 < actual <= offered,
                 '技能等级链缺失、重复或超过LV3')
        _validate_proof(event.get('proof'), ledger=ledger, identity=identity, level_before=before,
                        gain=offered, level_after=after, index=event.get('index'),
                        event_id=event_id, node_id=node_id)
        _require(type(event.get('usable')) is bool, '技能可用性来源缺失')
        if event['usable'] and identity.module not in (profile, '通用'):
            _require(_text(event.get('usability_evidence_id')), '外地金技能缺少真实可用性证据')
        _require(identity not in usable or usable[identity] == event['usable'], '技能可用性历史发生未解释冲突')
        levels[identity], usable[identity] = after, event['usable']
        events[event_id] = event
        nodes.add(node_id)
    # Removing either representation cannot silently produce a lower-level
    # resume.  Neither gold_picks nor the legacy name map supplies evidence.
    history = [row for row in record.history if row.get('kind') == 'skill']
    _require(len(history) == len(events), '普通技能历史与确认账本数量不一致')
    seen = set()
    for row in history:
        event = events.get(row.get('event_id'))
        _require(event is not None and row['event_id'] not in seen, '普通技能历史缺失或重复')
        expected = event['identity']
        _require(all(row.get(key) == expected[key] for key in ('name', 'module', 'rarity'))
                 and row.get('craft_id') == ledger['craft_id'] and row.get('node_id') == event['node_id']
                 and row.get('level') == event['level_before'] and row.get('gain') == event['actual_gain']
                 and row.get('level_after') == event['level_after'] and row.get('level_known') is True
                 and row.get('source') == event['proof']['source'], '普通技能历史与已确认身份或等级不一致')
        seen.add(row['event_id'])
    targets = tuple(_identity(value) for value in ledger['purple_targets'])
    _require(len(targets) <= 2 and len(set(targets)) == len(targets)
             and all(identity.module == profile and identity.rarity == 2 for identity in targets),
             '两种本地紫技能目录缺失、重复或归属错误')
    for identity in targets:
        _require(any(row.get('identity') == asdict(identity) and _text(row.get('evidence_id'))
                     for row in ledger['purple_observations']), '本地紫技能目录缺少身份观察证据')
    return ledger, levels, usable, targets


def build_local_skill_state(record, profile):
    """Return UNKNOWN rather than manufacturing zero from incomplete history."""
    try:
        ledger, levels, usable, targets = _replay(record, profile)
        gold = {identity: level for identity, level in levels.items()
                if identity.rarity == 3 and usable[identity] and level > 0}
        purple = {identity: levels.get(identity, 0) for identity in targets}
        targets_known = len(targets) == 2
        one = targets_known and min(purple.values()) >= 1
        two = targets_known and min(purple.values()) >= 2
        total = sum(gold.values())
        owned_gold_count = sum(identity.rarity == 3 and level > 0 for identity, level in levels.items())
        phase = ('UNKNOWN' if not targets_known else 'BASE' if total < 3 or not one
                 else 'BALANCE' if not two else 'UPGRADE')
        return LocalSkillState(True, '' if targets_known else '两种本地紫技能身份尚未完整证实',
            ledger['craft_id'], profile, levels, gold, total, targets, purple, targets_known,
            phase, owned_gold_count >= 2, one, two, deepcopy(ledger['refresh_nodes']))
    except (LocalSkillStateError, KeyError, TypeError, ValueError, AttributeError) as error:
        ledger = getattr(record, 'local_skill_ledger', {})
        craft_id = ledger.get('craft_id', '') if isinstance(ledger, dict) else ''
        return LocalSkillState(False, str(error), craft_id=craft_id, profile=profile)


local_skill_state = build_local_skill_state


def register_local_purple_targets(record, profile, identities, *, evidence_id):
    """Accumulate explicit observed identities, stopping on a third identity."""
    ledger = _valid_ledger(record, profile)
    _require(_text(evidence_id), '本地紫技能身份缺少证据引用')
    proposed = deepcopy(ledger)
    for value in identities:
        identity = value if isinstance(value, SkillIdentity) else _identity(value)
        _require(identity.kind == 'skill' and identity.module == profile and identity.rarity == 2,
                 '目标紫技能必须是当前模组的真实普通紫技能')
        data = asdict(identity)
        if data not in proposed['purple_targets']:
            proposed['purple_targets'].append(data)
            proposed['purple_observations'].append({'identity': data, 'evidence_id': evidence_id})
    if len(proposed['purple_targets']) > 2:
        mark_local_skill_unknown(record, '发现第三种本地紫技能，原两种目标身份需要重新核验')
        raise LocalSkillStateError('发现第三种本地紫技能，不能擅自替换两种目标')
    _seal(proposed)
    record.local_skill_ledger = proposed
    return build_local_skill_state(record, profile)


def observe_local_purple_candidates(record, profile, options, *, evidence_id):
    identities = []
    for option in options:
        if getattr(option, 'rarity_known', False) is True and option.rarity == 2:
            identity = identity_for_option(option)
            if identity.module == profile:
                identities.append(identity)
    return register_local_purple_targets(record, profile, identities, evidence_id=evidence_id)


def mark_local_skill_unknown(record, reason):
    ledger = getattr(record, 'local_skill_ledger', None)
    if isinstance(ledger, dict) and ledger:
        ledger = deepcopy(ledger)
        ledger.setdefault('unknown_reasons', []).append(str(reason))
        _seal(ledger)
        record.local_skill_ledger = ledger


def _legacy_skills(levels):
    result = {}
    for identity, level in levels.items():
        key = identity.module + '·' + identity.name
        result[key] = max(result.get(key, 0), level)
    return result


def commit_confirmed_local_skill(record, option, *, profile, craft_id, node_id,
                                  event_id, confirmed_level, proof):
    """Commit once; return False for an exact replay of a committed event."""
    ledger, levels, _, _ = _replay(record, profile)
    _require(craft_id == ledger['craft_id'] and _text(event_id) and _text(node_id),
             '技能提交的件、节点或事务身份不可信')
    identity = identity_for_option(option)
    _require(getattr(option, 'level_known', False) is True and type(option.level) is int
             and type(option.gain) is int and option.gain > 0, '提交技能的当前等级或升级量尚未确认')
    _require(type(confirmed_level) is int and 0 <= option.level < confirmed_level <= 3
             and confirmed_level - option.level <= option.gain, '确认等级无有效增量或超过LV3')
    _validate_proof(proof, ledger=ledger, identity=identity, level_before=option.level,
                    gain=option.gain, level_after=confirmed_level, index=option.index,
                    event_id=event_id, node_id=node_id)
    event = {'event_id': event_id, 'node_id': node_id, 'craft_id': craft_id,
             'identity': asdict(identity), 'index': option.index, 'level_before': option.level,
             'level_after': confirmed_level, 'actual_gain': confirmed_level - option.level,
             'offered_gain': option.gain, 'usable': candidate_is_usable(option, profile),
             'identity_source': identity_source_for_option(option),
             'usability_evidence_id': getattr(option, 'usability_evidence_id', ''),
             'proof': deepcopy(proof)}
    existing = next((row for row in ledger['events'] if row['event_id'] == event_id), None)
    if existing is not None:
        _require(existing == event, '已提交事务出现不同结果，禁止覆盖历史')
        return False
    _require(not any(row['node_id'] == node_id for row in ledger['events']), '本技能节点已提交，禁止重复确认')
    _require(option.level == levels.get(identity, 0), '候选当前等级与本件已确认等级不一致')
    proposed = deepcopy(ledger)
    proposed['events'].append(event)
    _seal(proposed)
    history = {'kind': 'skill', 'name': identity.name, 'module': identity.module,
               'raw_name': getattr(option, 'raw_name', ''), 'rarity': identity.rarity,
               'level': option.level, 'gain': event['actual_gain'], 'offered_gain': option.gain,
               'level_after': confirmed_level, 'level_known': True, 'source': proof['source'],
               'unattended_verified': proof['confirmation']['unattended_verified'],
               'craft_id': craft_id, 'node_id': node_id, 'event_id': event_id,
               'group': getattr(option, 'group', ''), 'attribute': getattr(option, 'attr', ''),
               'groups': list(getattr(option, 'groups', ())), 'value': getattr(option, 'value', 0),
               'score': getattr(option, 'score', 0), 'boost': getattr(option, 'boost', False)}
    trial = SimpleNamespace(**vars(record))
    trial.local_skill_ledger = proposed
    trial.history = record.history + [history]
    state = build_local_skill_state(trial, profile)
    _require(state.trusted, '新技能事件无法重放：' + state.reason)
    record.local_skill_ledger, record.history = proposed, trial.history
    record.skills, record.gold_picks = _legacy_skills(state.levels), state.confirmed_gold_total
    return True


def normalize_local_skill_record(values):
    """Only a verified ledger may replace legacy count/name projections."""
    ledger = values.get('local_skill_ledger')
    if not isinstance(ledger, dict) or not ledger:
        return values
    record = SimpleNamespace(**values)
    state = build_local_skill_state(record, ledger.get('profile'))
    if state.trusted:
        values['skills'] = _legacy_skills(state.levels)
        values['gold_picks'] = state.confirmed_gold_total
    return values


def confirmed_local_skill_event(record, profile, event_id, *, craft_id=None, node_id=None):
    """Resume may query an existing confirmation without replaying its input."""
    ledger, _, _, _ = _replay(record, profile)
    _require(_text(event_id), '查询已确认技能缺少事务身份')
    _require(craft_id is None or craft_id == ledger['craft_id'], '已确认技能查询跨件')
    event = next((row for row in ledger['events'] if row['event_id'] == event_id), None)
    if event is not None and node_id is not None:
        _require(event['node_id'] == node_id, '已确认技能查询跨节点')
    return deepcopy(event)
