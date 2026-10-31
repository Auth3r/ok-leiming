"""Daily module boundaries and durable progress; no device input lives here."""
import json
import hashlib
from collections import Counter
from datetime import date, datetime

NAMES = dict(FUND='理财基金', BLACK_SHIELD='黑盾领取', BLACK_DISPATCH='黑盾派遣',
             DARK_TIDE='暗涌', GANHUO='干货练习', HUNT_GOLD='猎金行动', DAILY_REWARD='每日奖励')
# Rewards inspect current eligibility; dispatch independently reads its remaining
# quota. Neither requires its predecessor to succeed. Add only hard dependencies.
DEPENDENCIES = {name: () for name in NAMES}
NAVIGATION = frozenset(('main_nav', 'main_fund', 'main_black', 'main_daily', 'main_dark',
    'nav_fund', 'nav_gan', 'nav_hunt', 'nav_resources', 'resource_gan', 'resource_hunt',
    'dark_hub_research', 'dark_map', 'home', 'back', 'daily_tab'))


class SweepHistoryUnavailable(RuntimeError):
    """Only this spending history is unresolved; independent modules can run."""


def outcome(value):
    if value is None: return 'SUCCESS', '已完成并验证'
    text = str(value)
    prefix = text.split('：', 1)[0]
    status = {'SUCCESS': 'SUCCESS', 'DONE_CHECK': 'ALREADY_DONE', 'ALREADY_DONE': 'ALREADY_DONE',
              'SKIPPED': 'SKIPPED', 'BLOCKED': 'BLOCKED', 'PARTIAL': 'BLOCKED',
              'FAILED': 'FAILED', 'INTERRUPTED': 'INTERRUPTED'}.get(prefix, 'FAILED')
    return status, text


class DailySchedule:
    def __init__(self, flow, only=None):
        self.f = flow
        self.only = only
        self.order = [only] if only else list(NAMES)
        self.records = {}
        self.current = None
        self.pending_action = None
        self.mutated = False
        self.started = datetime.now().isoformat()
        self.path = flow.logdir / 'daily-checkpoint.json'
        self.latest = flow.logdir.parent / 'daily-checkpoint.json'
        self.parameters = {k: v for k, v in flow.cfg.items()
                           if k.startswith('execute_') or k == 'dark_tide_sweep_count'}
        self.identity = getattr(flow, 'target_identity', None)
        if getattr(flow,'daily_account',None):
            self.identity=dict(self.identity or {},account_name=flow.daily_account)
        self.phase = 'RUNNING'
        self.source = None
        self.publish_latest = False
        self.dark_path = flow.logdir.parent / 'daily-dark-progress.json'
        self.dashboard_latest = self.latest
        if (self.identity or {}).get('account_name'):
            # A verified switch starts that account's own ledger. Preserve
            # prior owners before updating the global diagnostic checkpoint.
            for attribute in ('latest', 'dark_path'):
                legacy = getattr(self, attribute)
                account_path = self.account_path(self.identity['account_name'], legacy.name)
                if legacy.exists():
                    try:
                        data = json.loads(legacy.read_text(encoding='utf-8'))
                        owner = (data.get('identity') or {}).get('account_name')
                        if owner:
                            previous = self.account_path(owner, legacy.name)
                            if not previous.exists():
                                previous.write_bytes(legacy.read_bytes())
                        elif attribute == 'dark_path':
                            continue  # Resolve unknown spending only inside DARK_TIDE.
                        else:
                            self.dashboard_latest=self.path
                    except (OSError, ValueError, TypeError, AttributeError) as error:
                        self.f.emit('daily_history_unreadable', path=str(legacy), error=str(error))
                        if attribute == 'dark_path':
                            continue
                        self.dashboard_latest=self.path
                setattr(self, attribute, account_path)
        if getattr(flow,'daily_fresh_route',False):
            # Recheck current UI on every new route. Never inherit completed
            # modules from a possibly different account in the same window.
            self.latest=self.dashboard_latest=self.path

    def bind_dark_account(self, account):
        if not account:
            # A missing nickname is harmless on first use. If spending history
            # exists, its owner cannot be inferred from a reused game window.
            paths = [self.f.logdir.parent/'daily-dark-progress.json']
            paths.extend((self.f.logdir.parent/'daily-accounts').glob('*/daily-dark-progress.json'))
            for path in paths:
                if not path.exists():continue
                data = self.read_dark_record(path)
                if data['date'] == self.started[:10] and (data['completed'] or data['uncertain']):
                    raise SweepHistoryUnavailable('已有本日扫荡消耗记录，但无法关联当前画面；仅保留本项，其他日常继续')
            return
        self.identity=dict(self.identity or {},account_name=account)
        legacy=self.f.logdir.parent/'daily-dark-progress.json'
        destination=self.account_path(account,legacy.name)
        if legacy.exists():
            data=self.read_dark_record(legacy)
            owner=(data.get('identity') or {}).get('account_name')
            if not owner:
                if data['date']==self.started[:10] and (data['completed'] or data['uncertain']):
                    raise SweepHistoryUnavailable('本日旧暗涌消耗记录归属不明；保留记录，仅暂停本项消耗')
            else:
                previous=self.account_path(owner,legacy.name)
                if not previous.exists():previous.write_bytes(legacy.read_bytes())
        self.dark_path=destination

    def account_path(self, account, filename):
        key = hashlib.sha256(account.encode('utf-8')).hexdigest()
        directory = self.f.logdir.parent / 'daily-accounts' / key
        directory.mkdir(parents=True, exist_ok=True)
        return directory / filename

    def save(self):
        data = dict(version=1, date=self.started[:10], started=self.started,
                    phase=self.phase, current=self.current, order=self.order,
                    records=self.records, pending_action=self.pending_action,
                    mutated=self.mutated, parameters=self.parameters, identity=self.identity,
                    remaining=[n for n in self.order if n not in self.records],
                    logdir=str(self.f.logdir), source=self.source)
        paths = [self.path] if self.only or not self.publish_latest else [
            self.path, self.latest, self.dashboard_latest]
        for path in dict.fromkeys(paths):
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(path)

    def restore(self):
        if self.only or not self.latest.exists(): return
        try:
            data = json.loads(self.latest.read_text(encoding='utf-8'))
            if data.get('phase') not in ('RUNNING', 'INTERRUPTED'): return
            if (data.get('version') != 1 or data.get('date') != self.started[:10]
                    or data.get('order') != self.order or data.get('parameters') != self.parameters):
                raise ValueError('检查点日期或计划不同')
            old, new = data.get('identity') or {}, self.identity or {}
            if not new.get('account_name') or old.get('account_name')!=new['account_name']:
                raise ValueError('检查点未关联当前角色，不能继承完成状态')
            if old.get('pid') != new.get('pid') or old.get('hwnd') != new.get('hwnd'):
                raise ValueError('游戏会话已变化，重新核对当前页面')
            records=data['records']
            if (not isinstance(records,dict) or not isinstance(data['logdir'],str)
                    or data.get('current') not in (None,*self.order)
                    or any(name not in self.order or not isinstance(record,dict)
                           or record.get('status') not in ('SUCCESS','ALREADY_DONE','SKIPPED','BLOCKED','FAILED','INTERRUPTED')
                           or not isinstance(record.get('reason'),str) for name,record in records.items())):
                raise ValueError('检查点记录格式不完整')
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            # Keep the original byte-for-byte. Each module will observe its
            # own current page; uncertain spending has a separate local ledger.
            source=str(self.latest)
            self.latest=self.dashboard_latest=self.path
            self.f.emit('daily_checkpoint_ignored', source=source, reason=str(error),
                        recovery='current_game_state')
            return
        self.records = data['records']
        self.source = data['logdir']
        current = data.get('current')
        if current and (current not in self.records or self.records[current]['status'] == 'INTERRUPTED'):
            # A crash can happen after input and before its receipt is persisted.
            # Preserve uncertain work, resume only the independent pending modules.
            self.record(current, 'BLOCKED', '前次执行中断；结果待核实，未重放可能已生效的操作')
        self.f.results.update({n: r['status']+'：'+r['reason'] for n, r in self.records.items()})
        self.f.emit('daily_resumed',source=self.source,remaining=[n for n in self.order if n not in self.records])

    def record(self, name, status, reason, **extra):
        previous = self.records.get(name, {})
        self.records[name] = dict(module=name, name=NAMES[name], status=status, reason=reason,
            started_at=previous.get('started_at', self.started), ended_at=datetime.now().isoformat(),
            logdir=str(self.f.logdir), **extra)
        self.f.results[name] = status+'：'+reason
        self.f.task.info_set('模块结果', '；'.join(NAMES[n]+': '+r['status']+' '+r['reason']
            for n,r in self.records.items()))
        self.f.task.info_set('失败与未完成', '；'.join(NAMES[n]+': '+r['reason']
            for n,r in self.records.items() if r['status'] in ('FAILED','BLOCKED','INTERRUPTED')) or '无')

    def action(self, target, label, confirmed=False):
        if target=='dark_start':
            progress=self.dark_progress()
            progress['uncertain']=not confirmed
            if confirmed:progress['completed']+=1
            temp=self.dark_path.with_suffix('.tmp')
            temp.write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8')
            temp.replace(self.dark_path)
        if confirmed:
            self.pending_action = None
        else:
            self.pending_action = dict(target=target, step=label, time=datetime.now().isoformat())
            if target not in NAVIGATION: self.mutated = True
        self.save()  # Write-ahead: failure prevents the actual input.

    def dark_progress(self):
        current=dict(date=self.started[:10],identity=self.identity,completed=0,uncertain=False,
                     run_started=self.started)
        if not self.dark_path.exists():return current
        data=self.read_dark_record(self.dark_path)
        if data['date']!=current['date']:return current
        old,new=data.get('identity') or {},self.identity or {}
        if new.get('account_name') and old.get('account_name')!=new['account_name']:
            raise SweepHistoryUnavailable('暗涌记录角色不同或旧记录未绑定角色，禁止沿用次数或重复消耗')
        if not new.get('account_name') and (old.get('account_name') or data.get('run_started')!=self.started):
            if data['completed'] or data['uncertain']:
                raise SweepHistoryUnavailable('本日扫荡记录未关联当前运行；仅保留本项，不能沿用其他账号次数')
            return current
        return data

    def read_dark_record(self, path):
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(data,dict) or not isinstance(data.get('date'),str)
                    or type(data.get('completed')) is not int or data['completed']<0
                    or type(data.get('uncertain')) is not bool
                    or not isinstance(data.get('identity') or {},dict)):
                raise ValueError('invalid sweep ledger')
            if date.fromisoformat(data['date']).isoformat()!=data['date'] or data['date']>self.started[:10]:
                raise ValueError('invalid or future sweep date')
            return data
        except (OSError,ValueError,TypeError) as error:
            raise SweepHistoryUnavailable('暗涌消耗记录无法读取；保留原文件，仅暂停本项消耗') from error

    def summary(self):
        counts = Counter(r['status'] for r in self.records.values())
        incomplete = counts['FAILED']+counts['BLOCKED']+counts['INTERRUPTED']
        status = 'INTERRUPTED' if self.phase == 'INTERRUPTED' else 'PARTIAL' if incomplete else 'SUCCESS'
        self.f.route_status = status
        self.f.module_results = self.records
        summary = dict(status=status, counts=dict(counts), remaining=[n for n in self.order if n not in self.records])
        self.f.task.info_set('完整路线结果', status)
        self.f.task.info_set('路线汇总', f"成功{counts['SUCCESS']} / 已完成{counts['ALREADY_DONE']} / 业务跳过{counts['SKIPPED']} / 失败{counts['FAILED']} / 未完成{counts['BLOCKED']+counts['INTERRUPTED']+len(summary['remaining'])}")
        self.f.task.info_set('失败与未完成', '；'.join(NAMES[n]+': '+r['reason'] for n,r in self.records.items()
            if r['status'] in ('FAILED','BLOCKED','INTERRUPTED')) or '无')
        self.f.task.info_set('日志位置', str(self.f.logdir))
        self.f.emit('daily_summary', **summary, modules=self.records)

    def run(self):
        f = self.f
        f.daily_schedule = self
        try:
            self.restore()
            self.publish_latest = True
            self.save()
            f.task.info_set('日志位置', str(f.logdir))
            for index, name in enumerate(self.order):
                if name in self.records: continue
                self.current = name
                self.mutated = False
                self.pending_action = None
                f.notices = 0
                f.state = name
                self.records.pop(name, None)
                self.save()
                f.task.info_set('当前模块', NAMES[name])
                f.task.info_set('当前状态', '正在执行'+NAMES[name])
                f.task.info_set('路线进度', f'{len(self.records)}/{len(self.order)}')
                f.task.info_set('下一模块', NAMES[self.order[index+1]] if index+1<len(self.order) else '汇总')
                if not self.only and not f.cfg['execute_'+name.lower()]:
                    self.record(name, 'SKIPPED', '用户关闭此模块'); self.save(); continue
                unmet = [n for n in DEPENDENCIES[name] if self.records.get(n,{}).get('status') not in ('SUCCESS','ALREADY_DONE')]
                if unmet:
                    self.record(name, 'BLOCKED', '前置任务未完成：'+','.join(unmet)); self.save(); continue
                started = datetime.now().isoformat()
                f.emit('module_start',module=name)
                before = getattr(f.task,'before_module',None)
                if before: before(f)
                for attempt in range(2):
                    recovering=False
                    try:
                        # A fresh classification at EVERY boundary, never inherit
                        # the previous module's Vision cache or navigation state.
                        if attempt == 0 and self.only in ('BLACK_DISPATCH','DARK_TIDE','GANHUO','HUNT_GOLD','DAILY_REWARD'):
                            # Standalone tasks can safely resolve an already-open
                            # dialog using their existing page-specific guards.
                            # Returning home first could reopen that same dialog.
                            f.refresh()
                            if f.vision.page('NAV'): f.home('NAV')
                            if f.vision.page('RESOURCE_HUB') and self.only not in ('GANHUO','HUNT_GOLD'):
                                f.home('RESOURCE_HUB')
                        else:
                            recovering=True
                            f.recover_for_next_task()
                            recovering=False
                        f.snapshot('module_start')
                        value = getattr(f,name.lower())()
                        f.require('MAIN_MENU')
                        status, reason = outcome(value)
                        break
                    except Exception as error:
                        f.emit('module_error', module=name, error=str(error),error_type=type(error).__name__,attempt=attempt+1)
                        f.snapshot('module_error')
                        f.checkpoint()  # User stop must NEVER initiate recovery.
                        if recovering:
                            raise  # This bounded recovery already failed; do not repeat it.
                        f.task.info_set('当前状态', NAMES[name]+'执行失败，正在恢复首页')
                        f.recover_for_next_task()
                        f.snapshot('recovered_home')
                        f.emit('module_recovered',module=name,home_verified=True)
                        if attempt == 0 and not self.mutated:
                            f.emit('module_retry',module=name,reason='尚未执行业务操作，首页已验证')
                            continue
                        from src.workflow import InsufficientResource
                        status = 'BLOCKED' if isinstance(error,InsufficientResource) else 'FAILED'
                        reason = str(error)
                        break
                self.record(name,status,reason,attempts=attempt+1,home_verified=True)
                self.records[name]['started_at'] = started
                self.current = None
                self.save()
                f.task.info_set('路线进度', f'{len(self.records)}/{len(self.order)}')
                f.emit('module_complete',**self.records[name])
                after = getattr(f.task,'after_module',None)
                if after: after()
            self.phase = 'DONE'
            f.state = 'DONE'
        except Exception as error:
            self.phase = 'INTERRUPTED'
            if self.current:
                self.record(self.current,'INTERRUPTED',str(error),home_verified=False)
            raise
        finally:
            self.save()
            self.summary()
