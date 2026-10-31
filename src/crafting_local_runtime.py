"""Small runtime bridge for the military/unity confirmed-skill ledger.

The policy never sends input. This bridge records the existing, guarded input
transaction and binds its observations to a single module and node. It cannot
reconcile an unknown confirmation by replaying a click.
"""
from copy import deepcopy
import time

from src.crafting_skill_state import (
    LOCAL_PROFILES, LocalSkillStateError, build_local_skill_state,
    commit_confirmed_local_skill, identity_for_option,
    initialize_local_skill_ledger, normalize_skill_candidate,
)
from src.workflow import SafetyStop


def enabled(runner):
    return getattr(getattr(runner, 'policy', None), 'name', None) in LOCAL_PROFILES


def _require(value, reason):
    if not value:
        raise SafetyStop('本地技能记录UNKNOWN：' + reason)


def _capture(screen):
    epoch = getattr(screen, 'observation_epoch', None)
    capture = getattr(screen, 'capture_provenance', None) or {}
    digest = capture.get('native_frame_sha256') or getattr(screen, 'frame_hash', None)
    _require(isinstance(epoch, str) and bool(epoch) and isinstance(digest, str)
             and len(digest) == 64, '缺少当前截图轮次或图像身份')
    return {'observation_epoch': epoch, 'evidence_id': 'frame-sha256:' + digest,
            'observed_ns': time.perf_counter_ns()}


def module_key(runner):
    return runner.acceptance_evidence.context()['module_key']


def state_for_runner(runner):
    state = build_local_skill_state(runner.record, runner.policy.name)
    ledger = getattr(runner.record, 'local_skill_ledger', {}) or {}
    _require(state.trusted and state.purple_targets_known, state.reason or '技能历史不完整')
    _require(state.craft_id == module_key(runner) and ledger.get('run_id') == runner.run_id,
             '技能历史属于其他制作轮次或其他件')
    return state


def preflight_skill_node(runner):
    """Bind every skill decision, including refresh spending, to this node."""
    state = state_for_runner(runner)
    step = getattr(runner, 'hub_step', None)
    _require(type(step) is int and 0 <= step < 30, '技能节点没有已识别的研发进度')
    node = state.craft_id + ':skill:' + str(step)
    events = runner.record.local_skill_ledger['events']
    if any(row.get('node_id') == node for row in events):
        # A verified overclock grants one extra choice at the same map step.
        # Only the immediately preceding confirmed animation can authorize it.
        prior = events[-1]
        _require(prior.get('node_id', '').split(':overclock:')[0] == node
                 and prior.get('proof', {}).get('result', {}).get('kind') == 'overclock',
                 '本节点已有已确认技能，不得重复选择、刷新或确认')
        ordinal = sum(row.get('node_id', '').split(':overclock:')[0] == node for row in events)
        node += ':overclock:' + str(ordinal)
    return state, node


def preflight_start(runner, screen):
    """Check prerequisites before a start can consume experiment material."""
    if not enabled(runner):
        return
    _capture(screen)
    record = runner.record
    _require(screen.kind == 'prepared' and not record.local_skill_ledger
             and not any((record.skills, record.gold_picks, record.history,
                          record.boosts, record.missions, record.factors)),
             '实验启动不是一个无既有事实的准备页，不能覆盖现有制作记录')


def start_verified(runner, before, after):
    """Zero is authorized by a newly started experiment, never by a restart."""
    if not enabled(runner):
        return
    source, result = _capture(before), _capture(after)
    evidence = {'source': 'verified_new_craft',
                'evidence_id': source['evidence_id'] + '->' + result['evidence_id'],
                'before_epoch': source['observation_epoch'],
                'after_epoch': result['observation_epoch'],
                'from_kind': before.kind, 'to_kind': after.kind,
                'input_status': getattr(runner, 'last_input_status', None)}
    try:
        record = runner.record
        if not runner.record.complete:
            # An unlinked prepared page has no established *old* history. A
            # controlled NEW experiment is the only event authorizing zero.
            # preflight_start has already rejected any existing facts.
            from src.crafting_policy import CraftRecord
            preflight_start(runner, before)
            record = CraftRecord()
        initialize_local_skill_ledger(record, runner.policy.name,
                                     module_key(runner), evidence, run_id=runner.run_id)
        runner.record = record
    except LocalSkillStateError as error:
        raise SafetyStop(str(error)) from error


def candidate(runner, screen, option):
    """Capture the chosen candidate before a reversible selection is sent."""
    state, node = preflight_skill_node(runner)
    try:
        identity = normalize_skill_candidate(option, runner.policy.name)
    except LocalSkillStateError as error:
        raise SafetyStop(str(error)) from error
    _require(option.level == state.levels.get(identity, 0), '候选等级与已确认历史不一致')
    return {'source': 'verified_skill_transition', 'craft_id': state.craft_id,
            'run_id': runner.run_id, 'node_id': node,
            'event_id': runner.selection_transaction_id,
            'candidate': {**_capture(screen), 'identity': identity.as_dict(),
                          'level': option.level, 'gain': option.gain, 'index': option.index}}


def prepare_confirmation(runner, screen):
    """Persist the actual unique selection before an irreversible confirm."""
    pending = runner.pending_action or {}
    proof = deepcopy(pending.get('local_skill_proof'))
    _require(isinstance(proof, dict), '缺少当前技能选择事务')
    from src.crafting_policy import Option
    option = Option(**pending['option'])
    state = state_for_runner(runner)
    identity = identity_for_option(option)
    _, expected_node = preflight_skill_node(runner)
    original = proof.get('candidate') or {}
    _require(proof.get('craft_id') == state.craft_id
             and proof.get('event_id') == runner.selection_transaction_id
             and proof.get('run_id') == runner.run_id
             and proof.get('node_id') == expected_node,
             '选择事务与当前件不一致')
    _require(original.get('identity') == identity.as_dict()
             and (original.get('level'), original.get('gain'), original.get('index'))
                 == (option.level, option.gain, option.index),
             '原策略候选与确认目标不一致')
    _require(not any(row.get('event_id') == proof['event_id'] or row.get('node_id') == proof['node_id']
                     for row in runner.record.local_skill_ledger['events']),
             '本事务或技能节点已提交，不得重复确认')
    _require(option.level == state.levels.get(identity, 0)
             and option.level < option.level + option.gain <= 3,
             '确认前技能等级链或上限不成立')
    # A local ROI can prove the target's border, not uniqueness across all
    # cards. Promote this exact capture before the confirmation is sent.
    ensure_full = getattr(screen, 'ensure_full', None)
    if ensure_full:
        ensure_full()
    _require(screen.kind == 'skill', '完整选中复核已不是原技能页')
    options = screen.options()
    selected = [o.index for o in options if screen.selected(o.area)]
    _require(selected == [option.index] and screen.target(option), '目标技能未被唯一选中')
    actual = next((o for o in options if o.index == option.index), None)
    _require(actual is not None and identity_for_option(actual) == identity
             and actual.level_known is True
             and (actual.level, actual.gain) == (option.level, option.gain),
             '选中技能身份、等级或增量发生变化')
    observed = _capture(screen)
    _require(observed['observation_epoch'] != proof['candidate']['observation_epoch'],
             '确认前缺少独立选中帧')
    _require(type(original.get('observed_ns')) is int
             and 0 < original['observed_ns'] < observed['observed_ns'],
             '原候选与选中帧的观察顺序不成立')
    manual = runner.manual_input_evidence().get('manual_intervention')
    _require(manual is not True, '观察到人工介入，不能把结果归为原自动选择')
    proof['selected'] = {**observed, 'identity': identity.as_dict(),
                         'level': option.level, 'gain': option.gain,
                         'index': option.index, 'selected_indices': selected}
    proof['confirmation'] = {'transaction_id': proof['event_id'],
                             'input_status': 'not_sent',
                             'from_epoch': observed['observation_epoch'],
                             'manual_intervention': manual,
                             'unattended_verified': manual is False}
    pending['local_skill_proof'] = proof
    runner.checkpoint_action()


def submitted(runner, sent_ns):
    """Record an actual backend receipt; never call this a game result."""
    proof = (runner.pending_action or {}).get('local_skill_proof')
    if not isinstance(proof, dict) or not isinstance(proof.get('confirmation'), dict):
        return
    proof['confirmation'].update(sent_ns=sent_ns,
                                 input_status=getattr(runner, 'last_input_status', 'unknown'))


def commit_verified(runner, option):
    proof = deepcopy((runner.pending_action or {}).get('local_skill_proof'))
    _require(isinstance(proof, dict), '确认后缺少技能事务，未提交虚假等级')
    screen = runner.screen()
    if screen.kind == 'unknown':
        runner.wait(lambda now: now.kind not in ('unknown', 'skill'),
                    '核对普通技能确认后的真实节点')
        screen = runner.screen()
    proof['result'] = {**_capture(screen), 'kind': screen.kind}
    manual = runner.manual_input_evidence().get('manual_intervention')
    proof['confirmation']['manual_intervention'] = manual
    proof['confirmation']['unattended_verified'] = manual is False
    try:
        committed = commit_confirmed_local_skill(
            runner.record, option, profile=runner.policy.name,
            craft_id=module_key(runner), node_id=proof['node_id'],
            event_id=proof['event_id'], confirmed_level=option.level + option.gain, proof=proof)
    except LocalSkillStateError as error:
        raise SafetyStop(str(error)) from error
    runner.pending_action['local_skill_proof'] = proof
    runner.pending_action['local_skill_committed_event'] = proof['event_id']
    # A crash after this write can restore the already committed transaction;
    # a crash before it retains an unresolved confirmation and cannot replay it.
    runner.checkpoint_action()
    state = state_for_runner(runner)
    runner.f.emit('craft_local_skill_committed', event_id=proof['event_id'],
                  craft_id=state.craft_id, node_id=proof['node_id'],
                  gold_total=state.gold_total, phase=state.phase,
                  purple_levels={i.name: n for i, n in state.purple_levels.items()},
                  source=proof['source'], duplicate=not committed,
                  independently_observed_result_level=False,
                  unattended_verified=proof['confirmation']['unattended_verified'])
    return proof


def restore_guard(runner):
    """Unknown historical skill state cannot spend on a resumed local module."""
    if (not enabled(runner) or not runner.module_active or runner.screen().kind == 'prepared'
            or runner._bad_factor_history()):
        return
    error = None
    for attempt in range(3):
        try:
            return state_for_runner(runner)
        except SafetyStop as failure:
            error = failure
            runner.f.emit('craft_local_resume_unknown', attempt=attempt + 1,
                          max_attempts=3, reason=str(failure), history_reset=False)
            if attempt < 2:
                runner.f.checkpoint()
                runner.f.refresh()
                runner.screen()
    raise SafetyStop(str(error) + '；3次只读观察仍不能证明本件技能历史，未继续消耗')


def _stop_preserving_resume(runner, data, values, reason):
    """An unlinked checkpoint remains evidence, never a new zero baseline."""
    from src.crafting_policy import CraftRecord, Option
    try:
        runner.record = CraftRecord(**{**deepcopy(values),
            'missions': [Option(**row) for row in values.get('missions', [])]})
    except (TypeError, ValueError, KeyError, AttributeError):
        # Preserve the original bytes on disk even when their structure cannot
        # be reconstructed. accounting_trusted stays false, so no replacement
        # checkpoint can supersede them in the runner's exception handler.
        pass
    runner.accounting_trusted = False
    runner.pending_action = deepcopy(data.get('pending_action'))
    runner.refresh_state = deepcopy(data.get('refresh_state'))
    runner.resume_trace.update(blocked_action='local_skill_history_unknown',
                               history_reset=False)
    for attempt in range(3):
        runner.f.emit('craft_local_resume_unknown', attempt=attempt + 1,
                      max_attempts=3, reason=reason, history_reset=False)
        if attempt < 2:
            runner.f.checkpoint()
            runner.f.refresh()
            runner.screen()
    raise SafetyStop('本地技能记录UNKNOWN：' + reason + '；3次只读观察仍不能证明，保留历史且未继续消耗')


def guard_unresolved_resume(runner, data, values):
    """Stop before generic page-driven recovery can drop an uncertain skill."""
    if not enabled(runner):
        return
    pending = data.get('pending_action') or {}
    if (pending.get('kind') == 'skill' and pending.get('phase') == 'confirming'
            and not pending.get('local_skill_committed_event')):
        _stop_preserving_resume(runner, data, values,
            '旧技能确认结果不确定，进度前进不能证明技能身份或实际等级，禁止重放')


def recover_verified_overclock(runner, data, current, observed):
    """Recover a logged successful confirmation, with no input or source rewrite."""
    pending = data.get('pending_action') or {}
    if (not enabled(runner) or pending.get('kind') != 'skill'
            or pending.get('phase') != 'confirming' or pending.get('local_skill_committed_event')):
        return False, current, observed
    from pathlib import Path
    from dataclasses import asdict
    import hashlib
    import json
    import cv2
    import numpy as np
    from src.crafting_policy import CraftRecord, Option, normalize_record
    from src.crafting_resume import (_same_observation, completion_counts,
                                     _selection_native_capture_matches)
    try:
        source = Path(data['_source_path'])
        saved = json.loads(source.read_text(encoding='utf-8'))
        _require(all(saved.get(key) == data.get(key) for key in
                     ('record', 'pending_action', 'observation', 'completion_ledger', 'safe')),
                 '超频恢复来源检查点变化')
        events_raw = (source.parent / 'acceptance.jsonl').read_bytes()
        events = [json.loads(line) for line in events_raw.decode('utf-8').splitlines()]
        proof = deepcopy(pending['local_skill_proof'])
        option = Option(**pending['option'])
        identity = identity_for_option(option)
        completion = completion_counts(data, runner.target, runner.policy.name, True)
        record = CraftRecord(**normalize_record(deepcopy(data['record'])))
        state = build_local_skill_state(record, runner.policy.name)
        completed = completion['normal'] + completion['quick'] + completion.get('unknown', 0)
        _require(data.get('safe') is True and state.trusted and completion['module_active']
                 and state.craft_id == completion['run_id'] + ':module:' + str(completed + 1)
                 and record.local_skill_ledger['run_id'] == completion['run_id'], '超频恢复缺少同件可信历史')
        verified = [row for row in events if row.get('phase') == 'action_verified'
                    and row.get('module_key') == state.craft_id
                    and row.get('hub_step') == pending['hub_step']
                    and row.get('facts', {}).get('transaction_id') == proof['event_id']
                    and row.get('facts', {}).get('manual_intervention') is False
                    and row.get('page', {}).get('kind') == 'overclock'
                    and row.get('page', {}).get('raw_text') == '超频成功']
        _require(len(verified) == 1, '缺少唯一已确认超频动画凭证')
        result = verified[0]
        receipts = [row for row in events if row.get('phase') == 'input_submission'
                    and row.get('action_id') == result['action_id']]
        _require(len(receipts) == 1 and receipts[0]['facts']['send_status'] == 'native_submitted'
                 and receipts[0]['facts']['dispatch_started_ns'] == proof['confirmation']['sent_ns']
                 and receipts[0]['page']['capture']['native_frame_sha256']
                     == proof['selected']['evidence_id'].removeprefix('frame-sha256:'),
                 '超频原确认回执不匹配')
        picture = source.parent / result['image']['path']
        encoded = picture.read_bytes()
        native = cv2.imdecode(np.frombuffer(encoded, np.uint8), 1)
        capture = result['page']['capture']
        _require(picture.resolve().is_relative_to(source.parent.resolve())
                 and hashlib.sha256(encoded).hexdigest() == result['image']['file_sha256']
                 and native.shape == (720, 1280, 3)
                 and hashlib.sha256(native.tobytes()).hexdigest() == capture['native_frame_sha256'],
                 '超频原生截图校验失败')
        for attempt in range(2):
            _require(current.kind == 'skill' and _same_observation(data['observation'], observed)
                     and _selection_native_capture_matches(current, capture['metadata']),
                     '超频后当前技能页无法关联原件')
            matches = [o for o in current.options() if identity_for_option(o) == identity]
            _require(len(matches) == 1 and matches[0].level_known is True
                     and matches[0].level == option.level + option.gain,
                     '超频后未独立观察到原技能实际等级')
            if attempt == 0:
                epoch = current.observation_epoch
                runner.f.refresh(); current = runner.screen(); observed = runner.observation_data(current)
                _require(current.observation_epoch != epoch, '超频恢复需要两帧新观察')
        proof['result'] = {'observation_epoch': capture['observation_epoch'],
                           'evidence_id': 'frame-sha256:' + capture['native_frame_sha256'],
                           'observed_ns': result['monotonic_ns'], 'kind': 'overclock'}
        commit_confirmed_local_skill(record, option, profile=runner.policy.name,
            craft_id=state.craft_id, node_id=proof['node_id'], event_id=proof['event_id'],
            confirmed_level=option.level + option.gain, proof=proof)
        data['record'] = asdict(record)
        data['pending_action'] = None; data['refresh_state'] = None
        runner.f.emit('craft_local_overclock_recovered', event_id=proof['event_id'],
            source_checkpoint=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            events_sha256=hashlib.sha256(events_raw).hexdigest(), confirmation_replayed=False,
            observed_level=matches[0].level, source_history_modified=False)
        return True, current, observed
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, SafetyStop, cv2.error) as error:
        runner.f.emit('craft_local_overclock_recovery_rejected', reason=str(error), input_dispatched=False)
        return False, current, observed


def guard_unlinked_history(runner, data, values, current):
    """Keep known facts if the generic resume association has failed."""
    if (enabled(runner) and current.kind != 'prepared'
            and any(values.get(key) for key in ('local_skill_ledger', 'skills', 'gold_picks', 'history'))):
        _stop_preserving_resume(runner, data, values,
            '已有技能历史无法与当前件可靠关联，不能用空记录覆盖')


def resume_committed(runner, data, current, observed):
    """Release only a durable, exact committed transaction at its saved result."""
    if not enabled(runner):
        return False
    pending = data.get('pending_action') or {}
    event_id = pending.get('local_skill_committed_event')
    if pending.get('kind') != 'skill' or not isinstance(event_id, str):
        return False
    from src.crafting_policy import CraftRecord, normalize_record
    from src.crafting_resume import _same_observation, completion_counts
    values = normalize_record(deepcopy(data.get('record', {})))
    try:
        record = CraftRecord(**values)
        state = build_local_skill_state(record, runner.policy.name)
        ledger = record.local_skill_ledger
        proof = pending.get('local_skill_proof') or {}
        event = next((row for row in ledger.get('events', []) if row.get('event_id') == event_id), None)
        completion = completion_counts(data, runner.target, runner.policy.name, True)
        completed = completion['normal'] + completion['quick'] + completion.get('unknown', 0)
        module_index = completed + 1 if completion['module_active'] else max(1, completed)
        expected_craft = completion['run_id'] + ':module:' + str(module_index)
        valid = (state.trusted and event is not None and event.get('proof') == proof
                 and proof.get('event_id') == event_id
                 and proof.get('craft_id') == state.craft_id
                 and state.craft_id == expected_craft
                 and ledger.get('run_id') == completion.get('run_id')
                 and completion.get('profile') == runner.policy.name
                 and data.get('safe') is True
                 and current.kind == proof.get('result', {}).get('kind')
                 and _same_observation(data.get('observation') or {}, observed))
    except (SafetyStop, TypeError, ValueError, KeyError, AttributeError):
        valid = False
    if not valid:
        raise SafetyStop('已提交技能的恢复证据冲突，未清零或重复确认')
    data['pending_action'] = None
    data['refresh_state'] = None
    runner.f.emit('craft_local_skill_resume_committed', event_id=event_id,
                  confirmation_replayed=False, history_committed=False)
    return True
