"""Partial, sourced skill opportunities; this module never captures or sends input.

A visible route is not a complete schedule. Unquantified entries stay in the
record, while only observed current card gains enter SkillOpportunities.
"""
import hashlib
import json
import re

from src.crafting_policy import SkillOpportunities


_REGULAR = ('技能研习', '技能深度研习')
_NON_SKILL = {'attribute', 'mission', 'rest', 'core_attribute', 'core_skill', 'result'}


def _state(record):
    state = getattr(record, 'skill_opportunity_state', None)
    if state is None:
        state = {}
        record.skill_opportunity_state = state
    if not isinstance(state, dict):
        raise ValueError('技能机会来源记录格式错误')
    if not state:
        state.update(version=1, confirmed=[], possible=[], absent=[],
                     future_known=False, consumed_ids=[], active_id=None,
                     entry_id=None, last_kind=None, sequence=0, partial=[])
    for key in ('confirmed', 'possible', 'absent', 'consumed_ids', 'partial'):
        if not isinstance(state.get(key), list):
            raise ValueError('技能机会来源列表格式错误：' + key)
    # Only an explicitly verified terminal page closes the current module's tail.
    terminal = state.get('terminal') or {}
    token = terminal.get('source') or {}
    state['future_known'] = bool(terminal.get('phase') in ('completed', 'terminal_verified') and
                               terminal.get('kind') in ('result', 'map') and
                               not terminal.get('superseded') and
                               isinstance(token.get('epoch'), str) and token['epoch'] and
                               re.fullmatch('[0-9a-f]{64}', token.get('native_frame_sha256', '')))
    return state


def _source(screen, step):
    if (getattr(screen, '_full', True) is False or
            getattr(screen, '_lazy_pending', False) or getattr(screen, '_light_conflict', False)):
        return None
    capture = getattr(screen, 'capture_provenance', None) or {}
    epoch = getattr(screen, 'observation_epoch', None)
    image_hash = capture.get('native_frame_sha256')
    if not isinstance(epoch, str) or not epoch or not isinstance(image_hash, str):
        return None
    if not re.fullmatch('[0-9a-f]{64}', image_hash):
        return None
    metadata = capture.get('metadata') or {}
    window = {k: metadata[k] for k in ('hwnd', 'pid') if k in metadata}
    if window and (len(window) != 2 or any(type(v) is not int or v <= 0 for v in window.values())):
        return None
    return {'epoch': epoch, 'step': step if type(step) is int and 0 <= step <= 30 else None,
            'native_frame_sha256': image_hash,
            'reference_frame_sha256': capture.get('reference_frame_sha256'),
            'recognition_frame_sha256': capture.get('recognition_frame_sha256'),
            'window': window, 'capture_sequence': metadata.get('sequence'),
            'capture_session': metadata.get('session_id')}


def _row(state, key):
    return next((row for bucket in ('confirmed', 'possible', 'absent')
                 for row in state[bucket] if row.get('id') == key), None)


def _put(state, bucket, key, kind, source, **facts):
    row = _row(state, key)
    if row is None:
        row = {'id': key, 'kind': kind, 'gain': None, 'origin_source': source}
    for name in ('confirmed', 'possible', 'absent'):
        state[name][:] = [item for item in state[name] if item.get('id') != key]
    row.update(facts, source=source)
    state[bucket].append(row)
    return row


def _pending(state, source=None):
    return next((row for row in reversed(state['possible'])
                 if row.get('kind') in ('overclock', 'unknown_area') and
                 (source is None or
                  row.get('source', {}).get('step') == source['step'] and
                  row.get('source', {}).get('window') == source['window'])), None)


def observe(record, screen, hub_step, label=None, phase='observed'):
    """Record existing page evidence; unknown transitions never become absence.

    entry_started/entry_verified refer to the caller's existing guarded action.
    Repeated same-hub etching intent therefore has one ID, not two chances.
    """
    source = _source(screen, hub_step)
    if source is None:
        return None
    state = _state(record)
    if source['step'] is not None:
        state['step'] = source['step']
    kind = screen.kind
    if kind in ('result', 'map') and phase in ('completed', 'terminal_verified'):
        state['terminal'] = {'kind': kind, 'phase': phase, 'source': source}
        state['future_known'] = True
        for pending in list(state['possible']):
            _put(state, 'absent', pending['id'], pending['kind'], source,
                 reason='verified_terminal_no_future', phase=phase)
        state['active_id'] = None
        return {'phase': phase, 'category': 'terminal', 'source': source, 'future_known': True}
    state['future_known'] = False
    if state.get('terminal'):state['terminal']['superseded'] = True
    previous = state.get('last_kind')
    state['last_kind'] = kind
    text = getattr(screen, 'text', '')
    header = getattr(screen, 'header', '')
    evidence = None
    if kind == 'prepared':
        labels = [name for name in _REGULAR + ('未知区域刻蚀',) if name in text]
        partial = {'kind': 'visible_blueprint_text', 'labels': labels,
                   'indexed': False, 'complete_schedule': False, 'source': source}
        if not state['partial'] or state['partial'][-1].get('labels') != labels:
            state['partial'].append(partial)
            del state['partial'][:-8]
            evidence = partial
    elif kind == 'hub' and label in _REGULAR + ('未知区域刻蚀',):
        # Label supplied by the live hub locator; there is no inferred next index.
        if source['step'] is None:
            return None
        category = 'unknown_area' if label == '未知区域刻蚀' else 'regular'
        key = f"{category}:{source['step']}:{label}"
        state['entry_id'] = key
        evidence = _put(state, 'possible' if category == 'unknown_area' else 'confirmed',
                        key, category, source, label=label, phase=phase)
    elif kind == 'overclock':
        pending = _pending(state, source)
        if not pending or pending['kind'] != 'overclock' or previous != 'overclock':
            state['sequence'] += 1
            key = f"overclock:{state.get('entry_id')}:{state['sequence']}"
        else:
            key = pending['id']
        # Even a successful animation does not prove its successor is a skill.
        evidence = _put(state, 'possible', key, 'overclock', source,
                        phase=phase, success_visible='超频成功' in text)
    elif kind == 'unknown' and ('超频失败' in header or '超频未触发' in header):
        pending = _pending(state, source)
        if pending and pending['kind'] == 'overclock':
            evidence = _put(state, 'absent', pending['id'], pending['kind'], source,
                            reason='explicit_overclock_failure', phase=phase)
        else:
            entry = _row(state, state.get('entry_id'))
            origin = entry.get('source', {}) if entry else {}
            if (entry and entry.get('kind') in ('regular', 'unknown_area') and
                    source['step'] is not None and source['step'] == origin.get('step') and
                    source['window'] == origin.get('window') and
                    isinstance(origin.get('epoch'), str) and origin['epoch'] and
                    re.fullmatch('[0-9a-f]{64}', origin.get('native_frame_sha256', ''))):
                key = 'overclock:' + entry['id'] + ':explicit_failure'
                evidence = _put(state, 'absent', key, 'overclock', source,
                                origin_id=entry['id'], reason='explicit_overclock_failure', phase=phase)
    elif kind in _NON_SKILL:
        pending = _pending(state, source)
        if pending:
            evidence = _put(state, 'absent', pending['id'], pending['kind'], source,
                            reason='observed_non_skill_successor', observed_kind=kind, phase=phase)
    # Generic hub, unknown, modal and missing labels cannot resolve an outcome.
    if evidence:
        return {'phase': phase, 'category': evidence['kind'], 'id': evidence.get('id'),
                'source': source, 'future_known': False}
    return None


def _fingerprint(options):
    rows = [(o.index, o.name, getattr(o, 'module', ''), o.level, o.gain, o.rarity,
             o.level_known, getattr(o, 'rarity_known', True)) for o in options]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode('utf-8')).hexdigest()


def _identity(option):
    return {'index': option.index, 'name': option.name, 'module': getattr(option, 'module', ''),
            'area': list(option.area)}


def _candidate(option):
    return dict(_identity(option), level=option.level if option.level_known else None,
                gain=option.gain if option.level_known else None,
                rarity=option.rarity if getattr(option, 'rarity_known', True) else None)


def _geometry_known(options):
    indices = [o.index for o in options]
    if len(set(indices)) != len(indices):return False
    return all(type(o.index) is int and o.index >= 0 and isinstance(o.name, str) and o.name and
               len(o.area) == 4 and all(type(x) is int for x in o.area) and
               0 <= o.area[0] < o.area[2] <= 2048 and 0 <= o.area[1] < o.area[3] <= 1152
               for o in options)


def opportunities(record, screen, options):
    """Return current observed capacity; preserve unknown future in every case."""
    state = _state(record)
    state['current_source_consumed'] = False
    if screen.kind != 'skill' or getattr(screen, '_lazy_pending', False):
        return SkillOpportunities((), future_known=state['future_known'])
    state['future_known'] = False
    if state.get('terminal'):state['terminal']['superseded'] = True
    usable = [o for o in options if getattr(o, 'visual_enabled', None) is not False]
    known = [o.gain for o in usable if o.level_known and type(o.gain) is int and 0 < o.gain <= 3]
    current = max(known, default=None)
    result = SkillOpportunities((current,) if current else (), future_known=False)
    source = _source(screen, state.get('step'))
    if source is None or not usable or not _geometry_known(usable):
        return result
    pending = _pending(state, source)
    origin = pending['id'] if pending else state.get('entry_id') or 'unanchored_current_skill'
    origin_row = _row(state, origin)
    if origin_row:
        source['step'] = origin_row['source'].get('step')
    fingerprint = _fingerprint(usable)
    active = _row(state, state.get('active_id'))
    if active and active.get('origin_id') == origin and not active.get('consumed'):
        key = active['id']  # Refresh changes cards, not the number of choices.
    elif (state.get('last_consumed_origin') == origin and
          state.get('last_consumed_fingerprint') == fingerprint):
        state['current_source_consumed'] = True
        return SkillOpportunities((), future_known=False)
    else:
        state['sequence'] += 1
        key = f"{origin}:choice:{state['sequence']}"
    if pending:
        _put(state, 'confirmed', pending['id'], pending['kind'], source,
             outcome='skill', gain=None)
        state['entry_id'] = pending['id']
    _put(state, 'confirmed', key, 'skill_choice', source, origin_id=origin,
         fingerprint=fingerprint, gain=current, capacity_lower_bound=len(known) != len(usable),
         candidates=[_candidate(o) for o in usable], consumed=False)
    state['active_id'] = key
    return result


def consume(record, chosen=None):
    """Called only after the existing successful skill-confirmation commit."""
    state = _state(record)
    key = state.get('active_id')
    row = next((r for r in state['confirmed'] if r.get('id') == key), None)
    if (not key or row is None or row.get('kind') != 'skill_choice' or
            row.get('consumed') or key in state['consumed_ids'] or
            not isinstance(row.get('source', {}).get('epoch'), str) or not row['source']['epoch'] or
            not re.fullmatch('[0-9a-f]{64}', row.get('fingerprint', '')) or
            not re.fullmatch('[0-9a-f]{64}', row.get('source', {}).get('native_frame_sha256', ''))):
        return None
    if (chosen is None or not chosen.level_known or type(chosen.gain) is not int or
            not 0 < chosen.gain <= 3 or not getattr(chosen, 'rarity_known', True)):
        return None
    identity = _identity(chosen)
    candidate = next((c for c in row.get('candidates', [])
                      if all(c.get(k) == v for k, v in identity.items())), None)
    if candidate is None:return None
    if any(candidate.get(k) is not None and candidate[k] != getattr(chosen, k)
           for k in ('level', 'gain', 'rarity')):return None
    row['consumed'] = True
    row['chosen'] = identity
    row['actual_gain'] = chosen.gain
    state['consumed_ids'].append(key)
    state['last_consumed_origin'] = row['origin_id']
    state['last_consumed_fingerprint'] = row['fingerprint']
    state['active_id'] = None
    return {'id': key, 'phase': 'consumed', 'source': row['source'], 'future_known': False}
