"""Bounded new-season acceptance on the already-open game; inspect sends no input.

Run through the project's existing ASCII-path launcher/alias when required by OK.
All invocation output is isolated from historical screenshots and configuration.
"""
import argparse
import json
import os
import sys
import hashlib
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ('先攻', '军工', '一致')
LIMITS = {'先攻': 8, '军工': 1, '一致': 1}
SESSION_ROOT = ROOT / 'logs' / 'season-live'


class AcceptanceStop(RuntimeError):
    pass


@contextmanager
def exclusive_file(path):
    """A real cross-process lock: concurrent invocations cannot share a game lease."""
    import msvcrt
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise AcceptanceStop('已有实机操作持有锁，未发送游戏输入。') from error
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    """A failed durable write must stop before any potentially spending input."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class AttemptLedger:
    """Every reserved start consumes one slot, including pending/unknown outcomes."""
    STATES = {'pending', 'unknown', 'started', 'completed', 'failed'}

    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if not self.path.exists():
            return {'version': 1, 'maximum': 10, 'profile_limits': LIMITS, 'entries': []}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data['version'] != 1 or data['maximum'] != 10 or data['profile_limits'] != LIMITS:
                raise ValueError('budget changed')
            entries = data['entries']
            if not isinstance(entries, list) or len(entries) > 10:
                raise ValueError('invalid attempt count')
            ids = set()
            for entry in entries:
                if entry['profile'] not in PROFILES or entry['state'] not in self.STATES or entry['id'] in ids:
                    raise ValueError('invalid attempt entry')
                if not entry.get('logdir'):
                    raise ValueError('attempt has no evidence directory')
                ids.add(entry['id'])
            for profile in PROFILES:
                if sum(e['profile'] == profile for e in entries) > LIMITS[profile]:
                    raise ValueError('profile limit exceeded')
            return data
        except (KeyError, TypeError, ValueError) as error:
            raise AcceptanceStop('制作预算账本损坏或被改变，拒绝猜测剩余次数。') from error

    def allowed_profile(self, requested=None):
        entries = self.read()['entries']
        if any(e['state'] in ('pending', 'unknown') for e in entries):
            raise AcceptanceStop('此前实验启动结果尚未确认；pending/unknown 已占预算，禁止重放或启动下一件。')
        if len(entries) >= 10:
            raise AcceptanceStop('本次10次实际制作尝试预算已用尽。')
        candidates = (requested,) if requested else PROFILES
        for profile in candidates:
            if profile not in PROFILES:
                raise AcceptanceStop('禁止本次未授权的模组类型。')
            if sum(e['profile'] == profile for e in entries) < LIMITS[profile]:
                return profile
        raise AcceptanceStop('该模组本次制作预算已用尽。')

    def reserve(self, profile, logdir, evidence):
        with exclusive_file(self.path.with_suffix('.lock')):
            self.allowed_profile(profile)
            data = self.read()
            entry = {'id': uuid4().hex, 'profile': profile, 'state': 'pending',
                     'reserved_at': utc_now(), 'logdir': str(Path(logdir).resolve()),
                     'start_evidence': evidence, 'input_result': 'not_yet_observed'}
            data['entries'].append(entry)
            atomic_json(self.path, data)
            return entry['id']

    def mark(self, entry_id, state, **evidence):
        transitions = {'pending': {'started', 'unknown'}, 'started': {'completed', 'failed'},
                       'unknown': set(), 'completed': set(), 'failed': {'completed'}}
        with exclusive_file(self.path.with_suffix('.lock')):
            data = self.read()
            found = [entry for entry in data['entries'] if entry['id'] == entry_id]
            if len(found) != 1 or state not in transitions[found[0]['state']]:
                raise AcceptanceStop('实验账本状态不可安全推进。')
            found[0].update(state=state, updated_at=utc_now(), **evidence)
            atomic_json(self.path, data)

    def resume_entry(self, entry_id, requested_profile=None):
        found = [entry for entry in self.read()['entries'] if entry['id'] == entry_id]
        if len(found) != 1 or found[0]['state'] not in ('started', 'failed'):
            raise AcceptanceStop('只能恢复已确认启动且尚未完成的本次尝试；pending/unknown 禁止恢复或重放。')
        entry = found[0]
        if requested_profile and entry['profile'] != requested_profile:
            raise AcceptanceStop('恢复模组配置与原启动账本不符。')
        return deepcopy(entry)

    def attach_recovery(self, entry_id, logdir, source, source_hash):
        with exclusive_file(self.path.with_suffix('.lock')):
            self.resume_entry(entry_id)
            data = self.read()
            entry = next(item for item in data['entries'] if item['id'] == entry_id)
            entry.setdefault('recoveries', []).append({'logdir': str(Path(logdir).resolve()),
                'source_checkpoint': str(Path(source).resolve()), 'source_sha256': source_hash,
                'started_at': utc_now(), 'new_start_budget': 0})
            atomic_json(self.path, data)


def external_paid_history(path, prior_path):
    """Audit a user-ended GUI piece; this evidence never authorizes a resume."""
    path, prior_path = Path(path).resolve(), Path(prior_path).resolve()
    try:
        content = path.read_bytes()
        receipt = json.loads(content)
        if (type(receipt.get('version')) is not int or receipt['version'] != 1
                or receipt.get('kind') != 'external_paid_gui_history'
                or receipt.get('profile') != '先攻' or receipt.get('result') != 'unknown'
                or receipt.get('user_disposition') != 'finished_or_abandoned'
                or receipt.get('subsequent_refresh_submitted') is not False):
            raise ValueError('external history disposition')
        def verified(reference):
            source = Path(reference['path']).resolve()
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != reference['sha256']:
                raise ValueError('external evidence changed')
            return source, raw
        original, original_bytes = verified(receipt['prior_ledger'])
        prior = AttemptLedger(prior_path).read()
        if (original != prior_path or len(prior['entries']) != 10
                or receipt['prior_ledger'].get('paid_starts') != 10
                or receipt['prior_ledger'].get('attempt_ids') != [e['id'] for e in prior['entries']]
                or any(e['state'] in ('pending', 'unknown') for e in prior['entries'])):
            raise ValueError('original ten attempts not accounted')
        source, source_bytes = verified(receipt['source'])
        event_path, event_bytes = verified(receipt['events'])
        picture, _ = verified(receipt['paid_start']['screenshot'])
        if (not source.is_relative_to((ROOT / 'logs').resolve())
                or event_path.parent != source.parent or picture.parent != source.parent):
            raise ValueError('external evidence directory')
        saved = json.loads(source_bytes)
        completion = saved['completion_ledger']
        if (completion['run_id'] != receipt['source_run_id'] or completion['profile'] != '先攻'
                or completion['target'] != receipt['source_target']
                or saved.get('safe') is not True or saved.get('pending_action') is not None
                or saved.get('refresh_state') != {'kind': 'attribute', 'phase': 'opened'}):
            raise ValueError('external source identity')
        events = [json.loads(line) for line in event_bytes.decode('utf-8').splitlines() if line.strip()]
        paid = receipt['paid_start']
        paid_clicks = [i for i, e in enumerate(events) if e.get('event') == 'click'
                       and e.get('step') == '实验启动' and e.get('time') == paid.get('click_at')]
        paid_verified = [i for i, e in enumerate(events) if e.get('event') == 'verified'
                         and e.get('step') == '实验启动 结果' and e.get('time') == paid.get('verified_at')]
        if (paid.get('confirmed') is not True or type(paid.get('cost')) is not int or paid['cost'] <= 0
                or type(paid.get('balance_before')) is not int or paid['balance_before'] < paid['cost']
                or len(paid_clicks) != 1 or len(paid_verified) != 1 or paid_verified[0] <= paid_clicks[0]):
            raise ValueError('external paid start unconfirmed')
        refreshes = [{key: e[key] for key in ('time', 'kind', 'balance_before', 'cost', 'confirmed_count')}
                     for e in events if e.get('event') == 'craft_refresh_verified']
        if (refreshes != receipt['confirmed_refreshes']
                or any(type(e['cost']) is not int or e['cost'] <= 0 or e['confirmed_count'] != 1 for e in refreshes)):
            raise ValueError('external refresh history changed')
        spent = sum(e['cost'] for e in refreshes)
        if (saved.get('refresh_accounting') != {'version': 1, 'reserved_total': spent}
                or type(receipt.get('confirmed_cost')) is not int
                or receipt['confirmed_cost'] != paid['cost'] + spent
                or paid['balance_before'] - receipt['confirmed_cost'] != receipt['last_displayed_balance']):
            raise ValueError('external paid accounting')
        last_open = max(i for i, e in enumerate(events) if e.get('event') == 'click' and e.get('step') == '刷新')
        if (not any(e.get('event') == 'verified' and e.get('step') == '刷新 结果' for e in events[last_open + 1:])
                or any(e.get('event') == 'click' for e in events[last_open + 1:])):
            raise ValueError('external last refresh may have been submitted')
        return {'external_history': {'path': str(path), 'sha256': hashlib.sha256(content).hexdigest()},
                'prior_ledger': {'path': str(prior_path), 'sha256': hashlib.sha256(original_bytes).hexdigest()},
                'prior_attempt_ids': [e['id'] for e in prior['entries']],
                'prior_paid_starts': 10, 'external_paid_starts': 1,
                'external_confirmed_cost': receipt['confirmed_cost']}
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        raise AcceptanceStop('追加单件授权缺少完整外部付费历史，未重置旧预算或猜测源结果。') from error


class AdditionalAttemptLedger(AttemptLedger):
    """One explicitly authorized first-strike start, with all eleven prior starts pinned."""
    def __init__(self, path, prior_path, history_path):
        super().__init__(path)
        self.prior_path, self.history_path = Path(prior_path), Path(history_path)
        if self.path.resolve() in (self.prior_path.resolve(), self.history_path.resolve()):
            raise AcceptanceStop('追加预算必须使用独立固定账本，不能覆盖旧账本或审计源。')

    def read(self):
        context = external_paid_history(self.history_path, self.prior_path)
        manifest = {'version': 1, 'maximum': 1, 'profile_limits': {'先攻': 1},
                    'authorization': 'additional_one_first_strike', 'prior_history': context}
        if not self.path.exists():
            return dict(manifest, entries=[])
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if (type(data.get('version')) is not int or type(data.get('maximum')) is not int
                    or any(data.get(key) != value for key, value in manifest.items())):
                raise ValueError('additional authorization changed')
            entries = data['entries']
            if (not isinstance(entries, list) or len(entries) > 1
                    or any(e['profile'] != '先攻' or e['state'] not in self.STATES
                           or not e.get('logdir') or not isinstance(e.get('id'), str) or not e['id'] for e in entries)):
                raise ValueError('additional attempt invalid')
            return data
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise AcceptanceStop('追加单件预算已改变或缺少证据，禁止追加第二次启动。') from error

    def allowed_profile(self, requested=None):
        if requested not in (None, '先攻'):
            raise AcceptanceStop('本次追加授权仅允许一件先攻。')
        entries = self.read()['entries']
        if any(e['state'] in ('pending', 'unknown') for e in entries):
            raise AcceptanceStop('追加实验启动结果未知，已占唯一预算，禁止重放或新启动。')
        if entries:
            raise AcceptanceStop('本次追加一件实际启动预算已用尽，不能重复使用授权参数。')
        return '先攻'

    def audit(self):
        try:
            data = self.read()
        except AcceptanceStop as error:
            # Reporting must not skip listener/capture cleanup if immutable
            # evidence is missing. This grants no new remaining budget.
            return {'failed_check': str(error), 'additional_remaining_starts': None}
        return dict(data['prior_history'], additional_reserved_starts=len(data['entries']),
                    additional_remaining_starts=1 - len(data['entries']),
                    additional_confirmed_starts=sum(e['state'] in ('started', 'failed', 'completed') for e in data['entries']),
                    total_reserved_or_confirmed_starts=11 + len(data['entries']))


class FollowupAttemptLedger(AdditionalAttemptLedger):
    """Two further starts after the immutable ten, external GUI and completed one."""
    def __init__(self, path, prior_path, history_path, additional_path):
        super().__init__(path, prior_path, history_path)
        self.additional_path = Path(additional_path)
        if self.path.resolve() == self.additional_path.resolve():
            raise AcceptanceStop('后续两件必须使用独立账本，不能覆盖已完成单件预算。')
        self.pinned_history = self._history()

    def _history(self):
        prior = AdditionalAttemptLedger(self.additional_path, self.prior_path, self.history_path).read()
        entries = prior['entries']
        if (len(entries) != 1 or entries[0]['state'] != 'completed'
                or entries[0].get('classification') != 'quick'):
            raise AcceptanceStop('后续两件要求此前追加单件已确认快速完成；未知启动不能获得新额度。')
        return dict(prior['prior_history'], previous_additional_paid_starts=1,
                    previous_additional_attempt_ids=[entries[0]['id']],
                    previous_additional_ledger={'path': str(self.additional_path.resolve()),
                        'sha256': hashlib.sha256(self.additional_path.read_bytes()).hexdigest()})

    def read(self):
        context = self._history()
        if context != self.pinned_history:
            raise AcceptanceStop('后续两件的历史源已改变，未重置或猜测额度。')
        manifest = {'version': 1, 'maximum': 2, 'profile_limits': {'先攻': 2},
                    'authorization': 'followup_two_first_strike', 'prior_history': context}
        if not self.path.exists():return dict(manifest, entries=[])
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if (type(data.get('version')) is not int or type(data.get('maximum')) is not int
                    or any(data.get(key) != value for key, value in manifest.items())):
                raise ValueError('followup authorization changed')
            entries = data['entries']
            if (not isinstance(entries, list) or len(entries) > 2
                    or any(e['profile'] != '先攻' or e['state'] not in self.STATES
                           or not e.get('logdir') or not isinstance(e.get('id'), str) or not e['id'] for e in entries)
                    or len({e['id'] for e in entries}) != len(entries)):
                raise ValueError('followup attempt invalid')
            return data
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise AcceptanceStop('后续两件预算损坏或被改变，禁止重置额度。') from error

    def allowed_profile(self, requested=None):
        if requested not in (None, '先攻'):raise AcceptanceStop('后续两件授权仅允许先攻。')
        entries = self.read()['entries']
        if any(e['state'] in ('pending', 'unknown') for e in entries):
            raise AcceptanceStop('后续启动结果未知，已占额度，禁止重放或启动下一件。')
        if len(entries) >= 2:raise AcceptanceStop('后续两件实际启动额度已用尽，重复参数不能重置。')
        return '先攻'

    def audit(self):
        try:data = self.read()
        except AcceptanceStop as error:
            return {'failed_check': str(error), 'followup_remaining_starts': None}
        return dict(data['prior_history'], followup_reserved_starts=len(data['entries']),
                    followup_remaining_starts=2 - len(data['entries']),
                    followup_confirmed_starts=sum(e['state'] in ('started', 'failed', 'completed') for e in data['entries']),
                    total_reserved_or_confirmed_starts=12 + len(data['entries']))


class AcceptanceAttemptLedger(FollowupAttemptLedger):
    """One separately authorized acceptance start after all fourteen pinned starts."""
    def __init__(self, path, prior_path, history_path, additional_path, followup_path, final_history_path):
        self.followup_path, self.final_history_path = Path(followup_path), Path(final_history_path)
        if Path(path).resolve() in (self.followup_path.resolve(), self.final_history_path.resolve()):
            raise AcceptanceStop('验收单件必须使用独立固定账本，不能覆盖此前预算或审计。')
        super().__init__(path, prior_path, history_path, additional_path)

    def _history(self):
        previous = FollowupAttemptLedger(self.followup_path, self.prior_path,
                                        self.history_path, self.additional_path).read()
        entries = previous['entries']
        if len(entries) != 2 or any(e['state'] != 'completed' or e.get('classification') != 'quick' for e in entries):
            raise AcceptanceStop('验收单件要求此前两件已确认完成，未确认启动不能获得新额度。')
        try:
            raw = self.final_history_path.read_bytes()
            receipt = json.loads(raw)
            budget = receipt['budget']
            ledger_hash = hashlib.sha256(self.followup_path.read_bytes()).hexdigest()
            fields = ('id', 'profile', 'state', 'classification', 'logdir')
            expected_entries = [{k: e[k] for k in fields} for e in entries]
            reported_entries = [{k: e[k] for k in fields} for e in budget['entries']]
            counts = {'maximum_new_paid_starts': 2, 'used_new_paid_starts': 2,
                      'remaining_new_paid_starts': 0, 'new_paid_starts_during_recovery': 0,
                      'total_reserved_or_confirmed_paid_starts_including_prior_receipt': 14}
            if (type(receipt.get('version')) is not int or receipt['version'] != 1
                    or receipt.get('kind') != 'p0_followup_two_live_performance_validation'
                    or budget.get('authorization') != 'followup_two_first_strike'
                    or any(type(budget.get(k)) is not int or budget[k] != v for k, v in counts.items())
                    or budget.get('original_ten_attempts_not_reset') is not True
                    or Path(budget['ledger']['path']).resolve() != self.followup_path.resolve()
                    or budget['ledger'].get('sha256') != ledger_hash or reported_entries != expected_entries):
                raise ValueError('fourteen-start receipt differs')
            return dict(previous['prior_history'], previous_followup_paid_starts=2,
                        previous_followup_attempt_ids=[e['id'] for e in entries],
                        previous_followup_ledger={'path': str(self.followup_path.resolve()), 'sha256': ledger_hash},
                        fourteen_start_receipt={'path': str(self.final_history_path.resolve()),
                            'sha256': hashlib.sha256(raw).hexdigest()})
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise AcceptanceStop('此前14次启动的固定审计或哈希不符，禁止新验收启动。') from error

    def read(self):
        context = self._history()
        if context != self.pinned_history:
            raise AcceptanceStop('验收单件的历史源已改变，未重置额度。')
        manifest = {'version': 1, 'maximum': 1, 'profile_limits': {'先攻': 1},
                    'authorization': 'acceptance_one_first_strike', 'prior_history': context}
        if not self.path.exists():return dict(manifest, entries=[])
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            entries = data['entries']
            if (type(data.get('version')) is not int or type(data.get('maximum')) is not int
                    or any(data.get(key) != value for key, value in manifest.items())
                    or not isinstance(entries, list) or len(entries) > 1
                    or any(e['profile'] != '先攻' or e['state'] not in self.STATES
                           or not e.get('logdir') or not isinstance(e.get('id'), str) or not e['id'] for e in entries)):
                raise ValueError('acceptance budget changed')
            return data
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise AcceptanceStop('验收单件预算损坏或被改变，禁止重置额度。') from error

    def allowed_profile(self, requested=None):
        return AdditionalAttemptLedger.allowed_profile(self, requested)

    def audit(self):
        try:data = self.read()
        except AcceptanceStop as error:
            return {'failed_check': str(error), 'acceptance_remaining_starts': None}
        return dict(data['prior_history'], acceptance_reserved_starts=len(data['entries']),
                    acceptance_remaining_starts=1 - len(data['entries']),
                    acceptance_confirmed_starts=sum(e['state'] in ('started', 'failed', 'completed') for e in data['entries']),
                    total_reserved_or_confirmed_starts=14 + len(data['entries']))


def invocation_ledger(args):
    prior_path = SESSION_ROOT / 'attempts.json'
    followup = getattr(args, 'followup_starts', None)
    acceptance = getattr(args, 'acceptance_starts', None)
    if acceptance is not None:
        if (type(acceptance) is not int or acceptance != 1 or args.additional_starts is not None
                or followup is not None or args.phase != 'run' or args.profile != '先攻' or not args.external_history):
            raise AcceptanceStop('验收入口须明确单件先攻授权及完整历史，不能混用旧额度或准备入口。')
        ledger = AcceptanceAttemptLedger(SESSION_ROOT / 'acceptance-attempts.json', prior_path,
            args.external_history, SESSION_ROOT / 'additional-attempts.json', SESSION_ROOT / 'followup-attempts.json',
            ROOT / 'docs/validation/p0-followup-two-20261012.json')
        ledger.read()
        return ledger
    if followup is not None:
        if (type(followup) is not int or followup != 2 or args.additional_starts is not None
                or args.phase != 'run' or args.profile != '先攻' or not args.external_history):
            raise AcceptanceStop('后续入口必须显式授权两件先攻并附完整外部付费历史。')
        ledger = FollowupAttemptLedger(SESSION_ROOT / 'followup-attempts.json', prior_path,
                                     args.external_history, SESSION_ROOT / 'additional-attempts.json')
        ledger.read()
        return ledger
    if args.additional_starts is None:
        if args.external_history:
            raise AcceptanceStop('外部历史只能与显式追加一件授权一起使用。')
        return AttemptLedger(prior_path)
    if (type(args.additional_starts) is not int or args.additional_starts != 1
            or args.phase != 'run' or args.profile != '先攻' or not args.external_history):
        raise AcceptanceStop('追加入口必须显式授权一件先攻并附完整外部付费历史。')
    ledger = AdditionalAttemptLedger(SESSION_ROOT / 'additional-attempts.json', prior_path, args.external_history)
    ledger.read()
    return ledger


def navigation_fingerprint():
    digest = hashlib.sha256()
    names = ['src/module_crafting.py', 'src/vision.py', 'templates/manifest.json',
             'templates/daily_native/manifest.json', 'templates/navigation_720p/manifest.json']
    season = ROOT / 'templates/module_season'
    if season.is_dir():
        names.extend(path.relative_to(ROOT).as_posix() for path in sorted(season.rglob('*')) if path.is_file())
    for name in names:
        path = ROOT / name
        digest.update(name.encode('utf-8'))
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def require_p0(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if (data.get('version') != 1 or data.get('status') != 'passed'
                or data.get('navigation_fingerprint') != navigation_fingerprint()):
            raise ValueError('P0 not current or passed')
        for profile in PROFILES:
            observations = data['profiles'][profile]
            if len(observations) < 2 or not all(o.get('identity_verified') is True for o in observations):
                raise ValueError('missing repeated identity evidence')
            for observation in observations:
                path = Path(observation['evidence']).resolve()
                if not path.is_relative_to(SESSION_ROOT.resolve()) or not path.is_file():
                    raise ValueError('evidence missing')
                saved = json.loads(path.read_text(encoding='utf-8'))
                if saved.get('profile') != profile or saved.get('identity_verified') is not True:
                    raise ValueError('evidence identity mismatch')
                for key in ('profile', 'identity_verified', 'scroll_count', 'reached_bottom',
                            'list_changed', 'bottom_verified', 'boundary_evidence',
                            'returned_to_research', 'manual_input', 'repetition',
                            'navigation_fingerprint'):
                    if saved.get(key) != observation.get(key):
                        raise ValueError('saved navigation proof differs from summary: ' + key)
        if not any(type(o.get('scroll_count')) is int and o['scroll_count'] > 0
                   and o.get('reached_bottom') is True and o.get('list_changed') is True
                   and o.get('bottom_verified') is not False
                   for o in data['profiles']['先攻']):
            raise ValueError('missing actual bottom navigation')
        return data
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise AcceptanceStop('P0三种模组重复导航、身份及先攻滚动到底部尚未全部通过，禁止实验启动。') from error


class StartGuard:
    """Wrap this runner only; all original resource, foreground and pause guards run."""
    def __init__(self, runner, ledger, profile, p0_path):
        self.runner, self.ledger, self.profile, self.p0_path = runner, ledger, profile, p0_path
        self.entry_id = None
        self.starting = self.armed = False
        self.original_start = runner.start_prepared
        self.original_button = runner.button
        self.original_click = runner.f.task.click
        runner.start_prepared = self.start
        runner.button = self.button
        runner.f.task.click = self.click

    def start(self):
        require_p0(self.p0_path)
        self.ledger.allowed_profile(self.profile)
        if self.entry_id or self.starting or self.runner.target != 1 or self.runner.policy.name != self.profile:
            raise AcceptanceStop('本次入口只允许对应模组启动一次实验。')
        self.starting = True
        try:
            result = self.original_start()
            if not self.entry_id:
                raise AcceptanceStop('实验启动未发送；页面已变化，停止并保存现场。')
            self.ledger.mark(self.entry_id, 'started', input_result='hub_observed')
            return result
        except Exception as error:
            if self.entry_id:
                entry = next(e for e in self.ledger.read()['entries'] if e['id'] == self.entry_id)
                if entry['state'] == 'pending':
                    self.ledger.mark(self.entry_id, 'unknown', input_result='unconfirmed', error=repr(error))
            raise
        finally:
            self.starting = False

    def button(self, kind, label, after, area=(1450, 960, 2015, 1100)):
        if label != '实验启动':
            return self.original_button(kind, label, after, area)
        if kind == 'hub':
            self.require_active_hub()
            return self.original_button(kind, label, after, area)
        if not self.starting or self.armed or kind != 'prepared' or self.entry_id:
            raise AcceptanceStop('实验启动必须经资源复核和预算入口，拒绝绕过或重复输入。')
        self.armed = True
        try:
            return self.original_button(kind, label, after, area)
        finally:
            self.armed = False

    def require_active_hub(self):
        """The nonspending hub node must belong to this already-started module."""
        entry = self.ledger.resume_entry(self.entry_id, self.profile)
        original_run_id = (entry.get('start_evidence') or {}).get('run_id')
        if (not original_run_id or getattr(self.runner, 'run_id', None) != original_run_id
                or getattr(getattr(self.runner, 'policy', None), 'name', None) != entry['profile']
                or getattr(self.runner, 'target', None) != 1
                or getattr(self.runner, 'module_active', False) is not True):
            raise AcceptanceStop('研发节点未关联本次已启动的活动模组，拒绝同名实验启动输入。')
        self.runner.f.refresh()
        if self.runner.screen().kind != 'hub':
            raise AcceptanceStop('新鲜页面不是活动研发 hub，禁止将同名按钮当作无消耗节点。')

    def click(self, *args, **kwargs):
        if self.starting:
            if not self.armed or self.entry_id:
                raise AcceptanceStop('实验启动重复提交已拦截。')
            require_p0(self.p0_path)
            pending = self.runner.pending_action
            if not pending or pending.get('kind') != 'experiment_start' or pending.get('phase') != 'confirming':
                raise AcceptanceStop('实验启动缺少既有的资源及持久化确认证据。')
            evidence = dict(pending, run_id=self.runner.run_id if hasattr(self.runner, 'run_id') else None,
                            window_identity=getattr(self.runner.f, 'target_identity', None))
            self.entry_id = self.ledger.reserve(self.profile, self.runner.f.logdir, evidence)
            self.runner.f.emit('season_start_reserved', attempt_id=self.entry_id, profile=self.profile,
                               ledger=str(self.ledger.path))
        return self.original_click(*args, **kwargs)

    def close(self):
        self.runner.start_prepared = self.original_start
        self.runner.button = self.original_button
        self.runner.f.task.click = self.original_click


def load_attempt_checkpoint(entry):
    """Pin history to the selected attempt, never the most recent unrelated run."""
    from src.crafting_resume import (completion_counts,selection_recovery_evidence,
                                     opened_refresh_recovery_evidence,unknown_etch_action_evidence)
    from src.workflow import SafetyStop
    origin = Path(entry['logdir']).resolve()
    if not origin.is_relative_to((ROOT / 'logs').resolve()):
        raise AcceptanceStop('原尝试记录不在本工作区日志目录，禁止关联其它账户或任务。')
    recoveries = entry.get('recoveries', [])
    folder = Path(recoveries[-1]['logdir']).resolve() if recoveries else origin
    if not folder.is_relative_to((ROOT / 'logs').resolve()):
        raise AcceptanceStop('恢复链指向当前工作区外部，禁止继续。')
    source = folder / 'resume.json'
    try:
        content = source.read_bytes()
        data = json.loads(content)
        completion = completion_counts(data, 1, entry['profile'], True)
        original_run_id = (entry.get('start_evidence') or {}).get('run_id')
        if original_run_id and completion['run_id'] != original_run_id:
            raise ValueError('crafting run identity changed')
        if (completion['module_active'] is not True
                or completion['normal'] + completion['quick'] + completion.get('unknown', 0) != 0):
            raise ValueError('not a safe active checkpoint')
        if data.get('safe') is not True:
            pending=data.get('pending_action') or {}
            if (data.get('etching_pending') is True and not data.get('refresh_state')
                    and ((data.get('observation',{}).get('kind')=='unknown' and not pending)
                         or pending.get('phase')=='confirming' and pending.get('kind')=='attribute')):
                data['_unknown_etch_recovery']=unknown_etch_action_evidence(source,data)
            elif pending.get('phase')=='confirming' and pending.get('kind')=='core_skill' and data.get('hub_step')==28:
                events=[json.loads(line) for line in (folder/'events.jsonl').read_text(encoding='utf-8').splitlines()]
                submitted=[e for e in events if e.get('event')=='craft_execution_phase' and e.get('label')=='确认'
                           and e.get('kind')=='core_skill' and e.get('receipt',{}).get('status')=='native_submitted']
                if not submitted:raise ValueError('final core confirmation was not fully submitted')
                data['_terminal_core_recovery']={'window':entry['start_evidence']['window_identity'],
                    'source':str(source),'sha256':hashlib.sha256(content).hexdigest()}
            else:
                data['_selection_recovery'] = selection_recovery_evidence(entry, source, data)
        elif (data.get('refresh_state') or {}).get('phase') == 'opened':
            data['_opened_refresh_recovery'] = opened_refresh_recovery_evidence(entry, source, data)
        data['_source_path'] = str(source)
        return source, hashlib.sha256(content).hexdigest(), data
    except (OSError, ValueError, KeyError, TypeError, SafetyStop) as error:
        raise AcceptanceStop('本次尝试缺少安全且尚未完成的关联检查点，未猜测或重置研发记录。') from error


class ResumeGuard(StartGuard):
    """Only finish the proven current module; an experiment start is never legal."""
    def __init__(self, runner, ledger, entry, source, source_hash, checkpoint):
        self.runner, self.ledger, self.profile = runner, ledger, entry['profile']
        self.entry_id = entry['id']
        self.original_start = runner.start_prepared
        self.original_button = runner.button
        self.original_click = runner.f.task.click
        self.original_restore = runner.restore
        self.source, self.source_hash, self.checkpoint = source, source_hash, checkpoint
        self.attached = False
        runner.start_prepared = self.start
        runner.button = self.button
        runner.f.task.click = self.click
        runner.restore = self.restore

    def start(self):
        raise AcceptanceStop('当前尝试恢复只允许完成原模组，禁止第二次实验启动。')

    def button(self, kind, label, after, area=(1450, 960, 2015, 1100)):
        if label == '实验启动':
            if kind == 'hub':
                if getattr(self.runner, 'resume_trace', {}).get('historical_association') != 'pass':
                    raise AcceptanceStop('原模组页面及检查点尚未关联，禁止进入同名研发节点。')
                self.require_active_hub()
                return self.original_button(kind, label, after, area)
            return self.start()
        return self.original_button(kind, label, after, area)

    def restore(self):
        from unittest.mock import patch
        from src.crafting_policy import normalize_record
        data = deepcopy(self.checkpoint)
        values = normalize_record(data['record'])
        with patch('src.crafting_resume.optional_record', return_value=(data, values)):
            result = self.original_restore()
        if getattr(self.runner, 'resume_trace', {}).get('historical_association') != 'pass':
            raise AcceptanceStop('真实当前页面无法关联本次尝试的原模组，禁止重置技能模式、等级或刷新状态。')
        self.runner.continuation_scope = 'current_module_only'
        self.runner.checkpoint_action()
        return result

    def click(self, *args, **kwargs):
        if getattr(self.runner, 'resume_trace', {}).get('historical_association') != 'pass':
            raise AcceptanceStop('本次尝试尚未完成真实页面及检查点关联，未发送恢复输入。')
        if not self.attached:
            self.ledger.attach_recovery(self.entry_id, self.runner.f.logdir, self.source, self.source_hash)
            self.attached = True
            self.runner.f.emit('season_attempt_resumed', attempt_id=self.entry_id, profile=self.profile,
                               source_checkpoint=str(self.source), new_start_budget=0)
        return self.original_click(*args, **kwargs)

    def close(self):
        super().close()
        self.runner.restore = self.original_restore


class SkillRecoveryHold:
    """Optional one-shot hold after production has confirmed and committed a skill."""
    def __init__(self, runner, enabled=False):
        self.runner, self.enabled, self.held = runner, enabled, False
        self.original_select = runner.select
        if enabled:
            runner.select = self.select

    def _local_origin(self, profile):
        from src.crafting_local_runtime import preflight_skill_node
        state, node = preflight_skill_node(self.runner)
        return {'profile': profile, 'run_id': self.runner.run_id,
                'craft_id': state.craft_id, 'node_id': node,
                'events': deepcopy(self.runner.record.local_skill_ledger['events'])}

    def _local_commit(self, origin, history):
        from src.crafting_local_runtime import state_for_runner
        from src.crafting_skill_state import confirmed_local_skill_event
        if self.runner.policy.name != origin['profile'] or self.runner.run_id != origin['run_id']:
            raise AcceptanceStop('技能暂停验收的模组或运行身份在选择期间变化。')
        state = state_for_runner(self.runner)
        if state.craft_id != origin['craft_id']:
            raise AcceptanceStop('技能暂停验收的当前件与原选择不一致。')
        event_id = history[-1].get('event_id')
        if not event_id or event_id != getattr(self.runner, 'selection_transaction_id', None):
            raise AcceptanceStop('技能暂停验收缺少本次选择对应的已提交事务。')
        # This API replays the real identity/level/proof/history chain. Source
        # strings alone, including legacy verified_confirmation, grant no hold.
        event = confirmed_local_skill_event(self.runner.record, origin['profile'], event_id,
                                           craft_id=origin['craft_id'], node_id=origin['node_id'])
        events = self.runner.record.local_skill_ledger['events']
        if (event is None or len(events) != len(origin['events']) + 1
                or events[:-1] != origin['events'] or events[-1] != event):
            raise AcceptanceStop('技能暂停验收未发现本次唯一新增的可信普通技能事件。')
        return event

    def select(self, screen):
        if not self.enabled or self.held or screen.kind != 'skill':
            return self.original_select(screen)
        prior = deepcopy(self.runner.record.history)
        profile = getattr(getattr(self.runner, 'policy', None), 'name', None)
        local = profile in ('军工', '一致')
        origin = origin_error = None
        if local:
            from src.workflow import SafetyStop
            try:
                origin = self._local_origin(profile)
            except (AcceptanceStop, SafetyStop, ValueError, TypeError, KeyError, AttributeError) as error:
                origin_error = str(error)
        result = self.original_select(screen)
        history = self.runner.record.history
        committed = (len(history) == len(prior) + 1 and history[:-1] == prior
                     and history[-1].get('kind') == 'skill')
        event = None
        if local:
            try:
                if origin is None:
                    raise AcceptanceStop(origin_error or '技能暂停验收缺少选择前的可信件与节点。')
                if not committed:
                    raise AcceptanceStop('技能暂停验收没有精确追加一条普通技能确认历史。')
                if self.runner.pending_action is not None or self.runner.uncertain is not False:
                    raise AcceptanceStop('技能暂停验收仍有未完成或未知的输入事务。')
                event = self._local_commit(origin, history)
            except (AcceptanceStop, SafetyStop, ValueError, TypeError, KeyError, AttributeError) as error:
                self.runner.f.emit('season_skill_recovery_hold_rejected', profile=profile,
                    run_id=getattr(self.runner, 'run_id', None),
                    origin_node=origin['node_id'] if origin else None,
                    reason=str(error), pause_triggered=False, history_reset=False)
                return result
        elif (not committed or history[-1].get('source') != 'verified_confirmation'
              or self.runner.pending_action is not None or self.runner.uncertain is not False):
            return result
        self.held = True
        # Save the exact production history before pausing. No selection, spend,
        # completion, or additional skill fact is manufactured by this hook.
        self.runner.checkpoint_action()
        self.runner.f.snapshot('skill_recovery_hold')
        evidence = {'reason': 'planned skill recovery/control verification',
                    'source': 'production_verified_confirmation', 'kind': 'skill',
                    'run_id': self.runner.run_id, 'history_count': len(history)}
        if event is not None:
            proof = event['proof']
            evidence.update(source='production_confirmed_local_skill_ledger', profile=profile,
                commit_source=proof['source'], event_id=event['event_id'],
                craft_id=event['craft_id'], node_id=event['node_id'],
                skill_identity=event['identity'], level_after=event['level_after'],
                actual_gain=event['actual_gain'],
                unattended_verified=proof['confirmation']['unattended_verified'],
                manual_intervention=proof['confirmation']['manual_intervention'],
                independently_observed_result_level=proof['source'] == 'ordinary_skill_result')
        self.runner.f.emit('season_skill_recovery_hold', phase='planned', **evidence)
        control = (getattr(self.runner.f.task, 'task_control', None)
                   or getattr(self.runner.f.task, 'roguelike_control', None))
        if not control:
            raise AcceptanceStop('已保存技能恢复点，但任务缺少正式暂停控制，未继续输入。')
        # DailyTask.pause also enters the framework's blocking sleep. Use its
        # shared control directly so the normal workflow checkpoint is the wait.
        control.set_paused('step', True)
        self.runner.f.task.info_set('控制状态', '技能确认后验证暂停；F7继续')
        if control.state != 'PAUSED':
            raise AcceptanceStop('已保存技能恢复点，但任务未进入正式暂停状态，未继续输入。')
        self.runner.f.emit('season_skill_recovery_hold', phase='paused', **evidence)
        self.runner.f.checkpoint()  # The normal control blocks until F7/F8.
        self.runner.f.emit('season_skill_recovery_hold', phase='resumed', **evidence)
        raise AcceptanceStop('已确认技能后的暂停/继续检查完成，保留安全技能恢复点')

    def close(self):
        if self.enabled:
            self.runner.select = self.original_select


def run_task_with_hud(app, task, invocation, enabled=False):
    """Keep the production status HUD on the main Qt thread, without new OCR."""
    if not enabled:
        return app.run_task(task, exit_after=False)
    from threading import Event, Thread, current_thread, main_thread
    from PySide6.QtCore import QSettings, QTimer, Qt
    from PySide6.QtWidgets import QApplication
    from src.module_ui import SkillRecordWindow

    if current_thread() is not main_thread():
        raise AcceptanceStop('制作记录HUD必须由主线程显示，未启动任务。')
    qt = QApplication.instance() or QApplication([])
    previous_quit_policy = qt.quitOnLastWindowClosed()
    qt.setQuitOnLastWindowClosed(False)
    settings_path = Path(invocation) / 'hud.ini'
    hud = None
    done, results, errors = Event(), [], []

    def run():
        try:
            results.append(app.run_task(task, exit_after=False))
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    worker = Thread(target=run, name='SeasonAcceptanceWait', daemon=True)
    timer = QTimer()
    timer.setInterval(100)
    timer.timeout.connect(lambda: qt.quit() if done.is_set() else None)
    try:
        hud = SkillRecordWindow(task=task, settings=QSettings(str(settings_path), QSettings.IniFormat))
        hud.show()
        atomic_json(Path(invocation) / 'hud.json', {
            'enabled': True, 'widget': 'SkillRecordWindow',
            'status_source': 'src.crafting_runner.status', 'poll_interval_ms': hud.timer.interval(),
            'settings_path': str(settings_path.resolve()),
            'accepts_focus': not bool(hud.windowFlags() & Qt.WindowDoesNotAcceptFocus),
            'transparent_for_input': bool(hud.windowFlags() & Qt.WindowTransparentForInput),
            'show_without_activating': hud.testAttribute(Qt.WA_ShowWithoutActivating),
        })
        worker.start()
        timer.start()
        qt.exec()
        if not done.is_set():
            # A closed HUD does not stop Qt. If the event loop itself ends early,
            # cancel through the normal task control before leaving the worker.
            task.terminal_stop('CANCELLED', source='HUD event loop ended')
            app.quit()
            raise AcceptanceStop('HUD事件循环提前结束，当前任务已停止。')
        if errors:
            raise errors[0]
        return results[0]
    finally:
        timer.stop()
        if hud is not None:
            hud.timer.stop()
            hud.close()
            hud.deleteLater()
        if worker.is_alive():
            task.terminal_stop('CANCELLED', source='HUD cleanup')
            app.quit()
            worker.join(timeout=12)
        qt.processEvents()
        qt.setQuitOnLastWindowClosed(previous_quit_policy)


def serialize_boxes(boxes):
    return [{'text': box.name, 'confidence': float(box.confidence),
             'box': [box.x, box.y, box.width, box.height]} for box in boxes]


def inspect_frame(flow, destination, label='inspect'):
    """Evidence from one captured frame: no focus activation or device input."""
    from src.crafting_vision import CraftScreen
    flow.refresh()
    labels = flow.task.ocr(frame=flow.task.frame, threshold=.75)
    pages = {}
    for name in ('MAIN_MENU', 'DARK_HUB', 'DARK_MAP', 'MODULE_RESEARCH',
                 'MODULE_SELECTOR', 'BLUEPRINT_SELECTOR', 'MODULE_CAPACITY',
                 'NOTICE', 'LOGIN', 'MAINTENANCE'):
        pages[name] = bool(flow.vision.page(name))
    for name in flow.vision.images:
        if name.startswith(('module_', 'seasonal_')) or name in ('back', 'home', 'main_dark'):
            flow.vision.find(name)
    screen = CraftScreen(flow.task, flow.last_frame)
    flow.snapshot(label)
    data = {'observed_at': utc_now(), 'input_sent': False,
            'client_size': flow.task.geometry.size,
            'capture': flow.task.metadata,
            'window_identity': flow.target_identity,
            'pages': pages, 'craft_kind': screen.kind,
            'ocr': serialize_boxes(labels),
            'craft_ocr': serialize_boxes(screen.boxes),
            'template_matches': list(flow.vision.results.values()),
            'logdir': str(flow.logdir)}
    atomic_json(destination, data)
    return data


def refuse_active(flow):
    from src.crafting_vision import CraftScreen
    from src.crafting_runner import CHOICES
    screen = CraftScreen(flow.task, flow.last_frame)
    if screen.kind in CHOICES | {'hub', 'result', 'refresh_confirm', 'quick_dialog', 'overclock'}:
        raise AcceptanceStop('已有模组处于研发或结算中，未替换、放弃或覆盖；请保留现场使用当前模组恢复入口。')
    return screen.kind


def navigate_profiles(flow, invocation, profiles, p0_path, *, confirm_preparation=False):
    from src.module_crafting import ModulePreparation
    from src.crafting_vision import CraftScreen
    from src.crafting_runner import CHOICES
    fingerprint = navigation_fingerprint()
    data = {'version': 1, 'status': 'incomplete', 'profiles': {},
            'navigation_fingerprint': fingerprint}
    if Path(p0_path).exists():
        previous = json.loads(Path(p0_path).read_text(encoding='utf-8'))
        if previous.get('navigation_fingerprint') == fingerprint:
            data = previous
            data['status'] = 'incomplete'
    flow.refresh()
    refuse_active(flow)
    for selector in ('MODULE_SELECTOR', 'BLUEPRINT_SELECTOR'):
        if flow.vision.page(selector):
            flow.click(selector, 'back', lambda: flow.vision.page('MODULE_RESEARCH'),
                       '无消耗关闭已有选择列表')
            break
    prep = ModulePreparation(flow)
    active = lambda: CraftScreen(flow.task, flow.last_frame).kind in CHOICES | {
        'hub', 'result', 'refresh_confirm', 'quick_dialog', 'overclock'}
    prep.navigate(active=active)
    refuse_active(flow)
    flow.require('MODULE_RESEARCH')
    for profile in profiles:
        flow.cfg['制作配置'] = profile
        prep = ModulePreparation(flow)
        for repetition in range(2):
            flow.require('MODULE_RESEARCH')
            refuse_active(flow)
            proof = prep.select_named_module(probe_only=True, verify_bottom=profile == '先攻')
            if not isinstance(proof, dict) or proof.get('identity_verified') is not True:
                raise AcceptanceStop('模组列表未提供可靠身份确认，不能写入P0通过凭证。')
            if proof.get('profile', profile) != profile:
                raise AcceptanceStop('选中的真实模组身份与本次配置不符。')
            path = invocation / f'nav-{profile}-{repetition + 1}.json'
            observed = inspect_frame(flow, path, f'p0_{profile}_{repetition + 1}')
            proof = {**observed, **proof, 'profile': profile, 'repetition': repetition + 1,
                     'input_sent': True, 'identity_verified': True, 'manual_input': False,
                     'evidence': str(path.resolve()), 'navigation_fingerprint': fingerprint}
            if confirm_preparation:
                def identity_guard():
                    if not flow.vision.find('module_name_'+profile,(122,338,788,497)):
                        raise AcceptanceStop('确认准备页前模组身份变化，未确认胚子。')
                flow.click('MODULE_SELECTOR','module_confirm',lambda:flow.vision.page('MODULE_RESEARCH'),
                           '无消耗确认'+profile+'胚子',precondition=identity_guard)
                flow.wait(lambda:flow.vision.page('MODULE_RESEARCH') and prep.module_ready(),
                          '无消耗核对'+profile+'研发准备页')
                flow.snapshot('p0_prepared_'+profile)
                proof['confirmed_to_preparation']=True
                proof['preparation_identity_verified']=True
            else:
                # Re-entry only counts after the verified return.
                flow.click('MODULE_SELECTOR', 'back', lambda: flow.vision.page('MODULE_RESEARCH'),
                           '无消耗返回研发准备页')
            proof['returned_to_research'] = True
            atomic_json(path, proof)
            observations = data['profiles'].setdefault(profile, [])
            observations.append(proof)
            data['profiles'][profile] = observations[-2:]
            data['updated_at'] = utc_now()
            atomic_json(p0_path, data)
    try:
        candidate = dict(data, status='passed')
        atomic_json(p0_path, candidate)
        require_p0(p0_path)
        data = candidate
    except AcceptanceStop:
        atomic_json(p0_path, dict(data, status='incomplete'))
        boundary_unverified=any(o.get('bottom_verified') is False
            for o in data['profiles'].get('先攻',()))
        if len(profiles) < len(PROFILES) or boundary_unverified:
            flow.emit('season_p0_incomplete', checked_profiles=list(profiles), evidence=str(p0_path))
            return dict(data, status='incomplete')
        raise
    flow.snapshot('P0_PASSED')
    flow.emit('season_p0_passed', profiles=list(PROFILES), evidence=str(p0_path))
    return data


def bound_upstream_screenshots(cache_root):
    """Bound generation at its source, including temporary OCR exception images."""
    from ok.core.screenshot import Screenshot
    root = Path(cache_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    original = Screenshot.add_task

    def bounded(instance, frame, folder, name=None, show_box=False, frame_box=None):
        if instance.task_queue is None or instance.task_queue.qsize() >= 8:
            return
        slot = getattr(instance, '_season_cache_slot', 0)
        instance._season_cache_slot = slot + 1
        # Upstream generation writes these fixed slots instead of timestamped
        # names, so neither OCR nor screenshots can accumulate indefinitely.
        instance.task_queue.put((frame, instance.ui_dict.copy(), str(root),
                                 f'cache-{slot % 16:02d}', show_box, frame_box))

    Screenshot.add_task = bounded
    return lambda: setattr(Screenshot, 'add_task', original)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--phase', choices=('inspect', 'nav', 'run'), default='inspect')
    result.add_argument('--profile', choices=PROFILES)
    result.add_argument('--confirm-preparation', action='store_true',
                        help='Nav only: confirm verified embryo and inspect preparation; never start crafting')
    result.add_argument('--navigation-resolution', choices=('auto','720p','4k'),
                        help='Nav only: temporary physical client size, without changing saved settings')
    result.add_argument('--hwnd', type=int, help='Bind the invocation to this verified game window')
    result.add_argument('--resume-attempt', help='Safely continue this existing ledger attempt; no new experiment')
    result.add_argument('--hold-after-skill', action='store_true',
                        help='Pause once after a verified skill, then stop safely after F7 continues')
    result.add_argument('--show-hud', action='store_true',
                        help='Show the production crafting status HUD without the main app window')
    result.add_argument('--additional-starts', type=int, choices=(1,),
                        help='Explicitly authorize one additional first-strike start; prior history is required')
    result.add_argument('--followup-starts', type=int, choices=(2,),
                        help='Explicitly authorize two further first-strike starts after the completed additional one')
    result.add_argument('--acceptance-starts', type=int, choices=(1,),
                        help='One separately authorized acceptance start after fourteen pinned historical starts')
    result.add_argument('--external-history', type=Path,
                        help='Immutable audit receipt for the user-ended GUI piece outside the original ten attempts')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if (args.confirm_preparation or args.navigation_resolution) and args.phase != 'nav':
        parser().error('navigation validation options require --phase nav')
    if args.resume_attempt and args.phase != 'run':
        parser().error('--resume-attempt is supported only with --phase run')
    if args.hold_after_skill and args.phase != 'run':
        parser().error('--hold-after-skill is supported only with --phase run')
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    invocation = SESSION_ROOT / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:8])
    invocation.mkdir(parents=True)
    from src.config import config as base_config
    from src.tasks import DailyTask
    from src.workflow import Workflow
    from src.settings import load_settings
    from ok import OK
    from src.diagnostics import window_identity, matches_target, require_input_privilege
    ledger = invocation_ledger(args)
    p0_path = SESSION_ROOT / 'p0.json'
    selected_profile = args.profile
    resume_entry = resume_source = resume_hash = resume_checkpoint = None
    if args.phase == 'run':
        if args.resume_attempt:
            resume_entry = ledger.resume_entry(args.resume_attempt, args.profile)
            selected_profile = resume_entry['profile']
            resume_source, resume_hash, resume_checkpoint = load_attempt_checkpoint(resume_entry)
        else:
            require_p0(p0_path)
            selected_profile = ledger.allowed_profile(args.profile)
    requested_identity = window_identity(args.hwnd) if args.hwnd else None
    if requested_identity and not matches_target(requested_identity, load_settings()):
        raise AcceptanceStop('指定的窗口不属于当前配置的目标游戏。')

    class BoundedWorkflow(Workflow):
        def __init__(self, *values, **kwargs):
            self._recent_snapshots = []
            self._key_snapshots = []
            super().__init__(*values, **kwargs)

        def snapshot(self, label):
            super().snapshot(label)
            stem = f'{self.sequence:04d}_{self.state}_{label}'
            files = [self.logdir / (stem + suffix) for suffix in ('.png', '_matches.jpg', '.json')]
            key = any(word in label.lower() for word in (
                'failed', 'timeout', 'p0_', 'inspect', 'module_ready', 'module_result', 'craft_decision',
                'craft_refresh_decision', 'skill_recovery_hold'))
            retained = self._key_snapshots if key else self._recent_snapshots
            retained.append(files)
            maximum = 128 if key else 20
            while len(retained) > maximum:
                for file in retained.pop(0):
                    # Only exact filenames created by this invocation are eligible.
                    if file.resolve().is_relative_to(self.logdir.resolve()):
                        file.unlink(missing_ok=True)

    class SeasonTask(DailyTask):
        task_label = '新赛季实机验收'

        def assert_target(self, settings):
            identity = super().assert_target(settings)
            original_identity = (resume_entry.get('start_evidence') or {}).get('window_identity') if resume_entry else None
            expected = original_identity or requested_identity or getattr(self, '_season_identity', None)
            if expected and (identity['hwnd'], identity['pid']) != (expected['hwnd'], expected['pid']):
                raise AcceptanceStop('目标游戏窗口或进程已改变，拒绝混用账户及旧坐标。')
            self._season_identity = identity
            return identity

        def _run_daily(self):
            from pynput import keyboard
            from src.run_plan import RunPlan
            cfg = load_settings()
            cfg.update(recognition_timeout=10, max_retries=0, debug_mode=False,
                       step_mode=False, 制作数量=1, 制作配置=selected_profile or '先攻')
            if args.navigation_resolution:
                width,height={'auto':(0,0),'720p':(1280,720),'4k':(3840,2160)}[args.navigation_resolution]
                cfg.update(resolution_width=width,resolution_height=height)
            kind = 'MODULE_RESUME' if args.resume_attempt else 'MODULE_CRAFT' if args.phase == 'run' else 'MODULE_PREPARE'
            self.run_plan = RunPlan(1, kind, cfg)
            control = self.task_control
            flow = BoundedWorkflow(self, cfg, control.cancel_event, clock=control.clock)
            atomic_json(invocation / 'workflow.json', {'logdir': str(flow.logdir)})
            held = set()
            def pressed(key):
                if key in held:
                    return
                held.add(key)
                if key == keyboard.Key.f7:
                    self.toggle_pause()
                elif key == keyboard.Key.f8:
                    self.terminal_stop('CANCELLED', source='F8')
                    self.executor.pause()
            listener = keyboard.Listener(on_press=pressed, on_release=lambda key: held.discard(key))
            guard = None
            skill_hold = None
            timing = None
            manual_input = None
            started = utc_now()
            outcome = None
            try:
                listener.start()
                if args.phase in ('nav', 'run'):
                    from src.crafting_timing import CraftTiming
                    timing = CraftTiming(flow)
                    timing.install_flow()
                if args.phase == 'inspect':
                    inspect_frame(flow, invocation / 'inspect.json')
                    outcome = 'inspected'
                    self.info_set('结果', '当前画面只读取证完成')
                    return
                with exclusive_file(SESSION_ROOT / 'game-input.lock'):
                    identity = self.assert_target(cfg)
                    require_input_privilege(identity['hwnd'])
                    self.prepare_input()
                    if args.phase == 'nav':
                        navigation = navigate_profiles(flow, invocation, (args.profile,) if args.profile else PROFILES,
                                                       p0_path,confirm_preparation=args.confirm_preparation)
                        outcome = 'p0_passed' if navigation['status'] == 'passed' else 'navigation_verified_p0_incomplete'
                        self.info_set('结果', '所选模组导航及身份核对通过；未启动实验' if outcome == 'p0_passed'
                                      else '所选模组重复身份核对完成；全部P0条件未齐，实验启动仍禁止')
                    else:
                        from src.crafting_runner import ModuleCrafting
                        from src.crafting_manual_input import CraftManualInputMonitor
                        manual_input = CraftManualInputMonitor(self, flow.emit).start()
                        flow.refresh()
                        if not resume_entry:
                            refuse_active(flow)
                        runner = ModuleCrafting(flow)
                        guard = (ResumeGuard(runner, ledger, resume_entry, resume_source, resume_hash, resume_checkpoint)
                                 if resume_entry else StartGuard(runner, ledger, selected_profile, p0_path))
                        skill_hold = SkillRecoveryHold(runner, args.hold_after_skill)
                        runner.run()
                        complete = runner.normal + runner.quick + runner.unknown_completion
                        if not guard.entry_id or complete != 1:
                            raise AcceptanceStop('本轮缺少实验启动关联或真实完成计数，不记为通过。')
                        ledger.mark(guard.entry_id, 'completed', completed_at=utc_now(),
                                    classification='normal' if runner.normal == 1 else 'quick' if runner.quick == 1 else 'unknown',
                                    record=str(flow.logdir / 'crafting.json'), automatically_recovered=bool(resume_entry))
                        outcome = 'completed'
                        self.info_set('结果', '本轮单件制作完成，已关联启动预算账本')
            except Exception as error:
                outcome = 'stopped'
                if guard and guard.entry_id:
                    entry = next(e for e in ledger.read()['entries'] if e['id'] == guard.entry_id)
                    if entry['state'] == 'started':
                        ledger.mark(guard.entry_id, 'failed', error=repr(error), retained_current_module=True)
                flow.snapshot('FAILED')
                failure = {'error': repr(error), 'logdir': str(flow.logdir),
                           'template_matches': list(flow.vision.results.values()),
                           'capture': flow.task.metadata, 'ocr_source': 'last_cached_frame_no_new_input'}
                if flow.last_frame is not None:
                    try:
                        failure['ocr'] = serialize_boxes(flow.task.ocr(frame=flow.last_frame, threshold=.75))
                    except Exception as ocr_error:
                        failure['ocr_error'] = repr(ocr_error)
                atomic_json(invocation / 'failure.json', failure)
                raise
            finally:
                if skill_hold:
                    skill_hold.close()
                if guard:
                    guard.close()
                if manual_input:
                    manual_input.close()
                performance_path = timing.finish() if timing else None
                atomic_json(invocation / 'summary.json', {'phase': args.phase, 'profile': selected_profile,
                    'started_at': started, 'ended_at': utc_now(), 'outcome': outcome,
                    'attempt_id': guard.entry_id if guard else None, 'logdir': str(flow.logdir),
                    'manual_input': bool(getattr(self, 'craft_manual_inputs', [])) if manual_input else None,
                    'manual_input_monitor': getattr(self, 'craft_manual_monitor', None),
                    'count': 1 if args.phase == 'run' else 0,
                    'resume_attempt': args.resume_attempt,
                    'hold_after_skill': args.hold_after_skill,
                    'skill_hold_triggered': bool(skill_hold and skill_hold.held),
                    'show_hud': args.show_hud,
                    'performance': str(performance_path) if performance_path else None,
                    'new_start_budget': int(bool(guard and guard.entry_id and not resume_entry)),
                    'additional_budget_audit': ledger.audit() if type(ledger) is AdditionalAttemptLedger else None,
                    'followup_budget_audit': ledger.audit() if type(ledger) is FollowupAttemptLedger else None,
                    'acceptance_budget_audit': ledger.audit() if isinstance(ledger, AcceptanceAttemptLedger) else None,
                    'ledger': str(ledger.path), 'p0': str(p0_path)})
                listener.stop()
                listener.join(timeout=1)
                flow.task.close()
                self.run_plan = None

    config = dict(base_config)
    config.update(gui=None, check_mutex=False, config_folder=str(invocation / 'configs'),
                  screenshots_folder=str(invocation / 'cache' / 'screenshots'),
                  click_screenshots_folder=None, debug=False)
    config.pop('my_app', None)
    config['windows'] = dict(base_config['windows'], start_exe=False)
    restore_screenshots = bound_upstream_screenshots(invocation / 'cache' / 'screenshots')
    app = OK(config)
    basic = app.global_config.get_config('Basic Options')
    basic['Auto Resize Game Window'] = False
    basic['Auto Start Game When App Starts'] = False
    if args.hwnd:
        app.device_manager.select_hwnd(requested_identity['exe'], args.hwnd)
    task = app.get_onetime_task(SeasonTask)
    task.only = 'MODULE_RESUME' if args.resume_attempt else 'MODULE_CRAFT' if args.phase == 'run' else 'MODULE_PREPARE'
    print(json.dumps({'phase': args.phase, 'evidence': str(invocation)}, ensure_ascii=False), flush=True)
    try:
        run_task_with_hud(app, task, invocation, args.show_hud)
    finally:
        atomic_json(invocation / 'task.json', {'phase': args.phase, 'profile': args.profile,
                                            'show_hud': args.show_hud,
                                            'task_info': task.info, 'ended_at': utc_now()})
        app.quit()
        restore_screenshots()
    if (invocation / 'failure.json').exists():
        return 1
    return 0 if (invocation / 'summary.json').exists() else 1


if __name__ == '__main__':
    raise SystemExit(main())
