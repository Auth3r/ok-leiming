"""Non-blocking diagnostic retention and supported portable geometry."""
import json
import logging
import os
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_LIMIT = 512 * 1024 * 1024
MAINTENANCE_INTERVAL = 60
KEEP_RECENT_SECONDS = 24 * 60 * 60
_sample = (None, 0.0, 0)
_maintenance = (None, 0.0, None)


def _diagnostic_files(root):
    """Inventory metadata only; never follow links or read historical logs."""
    for folder in ('logs', 'screenshots'):
        base = root / folder
        if not base.exists() or base.is_symlink() or base.is_junction():
            continue
        for directory, children, names in os.walk(base, followlinks=False):
            parent = Path(directory)
            children[:] = [name for name in children
                           if not (parent / name).is_symlink()
                           and not (parent / name).is_junction()]
            for name in names:
                path = parent / name
                try:
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        continue
                    yield path, path.stat()
                except OSError:
                    continue


def diagnostic_bytes(root, *, force=False):
    global _sample
    root = Path(root).resolve()
    now = time.monotonic()
    if not force and _sample[0] == root and now - _sample[1] < 5:
        return _sample[2]
    total = 0
    for _, stat in _diagnostic_files(root):
        total += stat.st_size
    _sample = (root, now, total)
    return total


def _daily_recovery_directories(root, files):
    """Keep images associated with unfinished or unreadable daily receipts."""
    logs = root / 'logs'
    protected = set()
    for path, stat in files:
        if path.name != 'daily-checkpoint.json':
            continue
        try:
            if stat.st_size > 128 * 1024:
                raise ValueError('oversized daily checkpoint')
            with path.open(encoding='utf-8') as stream:
                record = json.load(stream)
            records = record.get('records', {})
            unresolved = (record.get('phase') != 'DONE' or record.get('pending_action')
                          or any(item.get('status') in ('FAILED', 'INTERRUPTED', 'BLOCKED')
                                 for item in records.values()))
            if not unresolved:
                continue
            if path.parent != logs:
                protected.add(path.parent)
            for value in (record.get('logdir'), record.get('source')):
                if isinstance(value, str) and value:
                    directory = Path(value)
                    directory = (directory if directory.is_absolute() else root / directory).resolve()
                    if directory != logs and directory.is_relative_to(logs):
                        protected.add(directory)
        except (OSError, ValueError, TypeError, AttributeError):
            # An unreadable local receipt cannot authorize deleting its images.
            # A corrupt global receipt has no trustworthy linked run directory.
            protected.add(path.parent)
    return protected


def maintain_diagnostics(root, *, limit=None, keep_recent_seconds=KEEP_RECENT_SECONDS,
                         protected=(), force=False):
    """Trim oldest ordinary diagnostics, preserving receipts and recovery data.

    A budget is a cleanup target, never permission to execute game actions.
    Recent files, fault evidence and recovery records stay intact. Runs
    with crafting/roguelike recovery records keep their referenced images too.
    Locked files or a budget made entirely of protected evidence are reported
    but do not prevent fresh evidence from being written.
    """
    global _sample, _maintenance
    root = Path(root).resolve()
    now = time.monotonic()
    if not force and _maintenance[0] == root and now - _maintenance[1] < MAINTENANCE_INTERVAL:
        return _maintenance[2]
    files = list(_diagnostic_files(root))
    total = sum(stat.st_size for _, stat in files)
    limit = LOG_LIMIT if limit is None else max(0, limit)
    result = {'before': total, 'after': total, 'removed': 0, 'errors': []}
    # Mark the attempt even when nothing is eligible, avoiding repeated scans
    # on every screenshot if old evidence cannot be removed.
    _maintenance = (root, now, result)
    if total > limit:
        protected = {Path(path).resolve() for path in protected}
        protected.update(path.parent for path, _ in files
                         if path.name in ('resume.json', 'crafting.json', 'progress.json'))
        protected.update(_daily_recovery_directories(root, files))
        fault_markers = ('failed', 'error', 'timeout', 'suspended', 'no_progress')
        fault_directories = {path.parent for path, _ in files
                             if any(marker in path.name.casefold() for marker in fault_markers)}
        cutoff = time.time() - keep_recent_seconds
        target = int(limit * .8)
        for path, stat in sorted(files, key=lambda item: item[1].st_mtime):
            if total <= target:
                break
            if stat.st_mtime >= cutoff or any(path.is_relative_to(p) for p in protected):
                continue
            name = path.name.casefold()
            if any(marker in name for marker in fault_markers):
                continue
            ordinary_image = (path.suffix.casefold() in ('.png', '.jpg', '.jpeg', '.bmp')
                              and (path.is_relative_to(root / 'screenshots') or re.match(r'^\d+[_-]', name)))
            rotated_text = (name.startswith('ok-script.') and name != 'ok-script.log'
                            and (name.endswith('.log') or re.search(r'\.log\.\d+$', name)
                                 or '.log.segment-' in name))
            completed_events = False
            if name == 'events.jsonl' and path.parent not in fault_directories:
                # Only a small successful summary authorizes pruning ordinary
                # events. Never parse the event stream or infer completion from
                # missing files; summaries and all ledgers remain available.
                summary = path.parent / 'result.json'
                try:
                    if summary.stat().st_size <= 128 * 1024:
                        with summary.open(encoding='utf-8') as stream:
                            record = json.load(stream)
                        completed_events = record.get('state') == 'DONE' and record.get('status') == 'SUCCESS'
                except (OSError, ValueError, AttributeError):
                    pass
            if not ordinary_image and not rotated_text and not completed_events:
                continue
            try:
                # Resolve again at deletion time; no recursive directory removal.
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    continue
                path.unlink()
            except OSError as error:
                result['errors'].append({'path': str(path), 'error': str(error)})
                continue
            total -= stat.st_size
            result['removed'] += 1
    result['after'] = total
    _sample = (root, now, total)
    if result['errors'] or total > limit:
        logging.getLogger(__name__).warning(
            '诊断清理后保留 %s 字节（目标 %s）；保留近期/恢复证据，%s 个文件未能清理，继续记录',
            total, limit, len(result['errors']))
    return result


def evidence_allowed(directory=None):
    if not getattr(sys, 'frozen', False):
        return True
    from src.app_paths import DATA_ROOT
    try:
        maintain_diagnostics(DATA_ROOT, protected=(directory,) if directory else ())
    except OSError:
        # Maintenance is best effort. Existing per-run folders continue to
        # receive text and failure evidence even if old files are locked.
        logging.getLogger(__name__).warning('旧诊断文件清理失败，继续写入本次运行记录', exc_info=True)
    return True


def check_environment(size, dpi):
    if tuple(size) not in ((1280, 720), (3840, 2160)):
        raise ValueError('当前支持1280×720或3840×2160游戏客户区；'
                         f'当前客户区{tuple(size)}，已禁止游戏输入。')
    if type(dpi) is not int or dpi <= 0:
        raise ValueError(f'无法确定游戏窗口DPI：{dpi!r}，已禁止游戏输入。')


class BoundedLogHandler(RotatingFileHandler):
    """Match the upstream factory signature while capping text logs at 20 MiB."""
    def __init__(self, filename, when=None, interval=None, backupCount=None, encoding='utf-8'):
        super().__init__(filename, maxBytes=5 * 1024 * 1024, backupCount=3, encoding=encoding, delay=True)
        self.original_filename = self.baseFilename
        self.delay = False
        try:
            self.stream = self._open()
        except OSError:
            # A previous process/viewer can also hold the initial log at startup.
            self.baseFilename = self.original_filename + f'.segment-{os.getpid()}-{time.time_ns()}'
            self.stream = self._open()

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            # A reader may hold the old log without FILE_SHARE_DELETE. Keep it
            # and write the current record to a new bounded segment instead.
            if self.stream:
                self.stream.close()
                self.stream = None
            self.baseFilename = self.original_filename + f'.segment-{os.getpid()}-{time.time_ns()}'
            if not self.delay:
                self.stream = self._open()


def install_log_limit():
    if getattr(sys, 'frozen', False):
        import ok.util.logger as logger
        logger.SafeFileHandler = BoundedLogHandler
