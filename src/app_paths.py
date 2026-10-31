"""Separate immutable installation resources from writable per-user state."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

APP_ROOT = (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
            else Path(__file__).resolve().parents[1])
DATA_ROOT = APP_ROOT


def _writable(directory):
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Windows os.access can report writable despite an ACL denial. tempfile
        # retries that denial as a name collision, so probe exactly once.
        probe = directory / ('.leiming-write-probe-' + uuid4().hex)
        with probe.open('xb'):
            pass
        probe.unlink()
        return True
    except OSError:
        return False


def _fallback_root(root):
    # Include the installation path so two portable copies never merge ledgers.
    local = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    key = hashlib.sha256(os.path.normcase(str(root)).encode('utf-8')).hexdigest()[:16]
    return local / 'Leiming-Automation' / ('portable-' + key)


def _copy_missing(source, destination):
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for path in source.iterdir():
            _copy_missing(path, destination / path.name)
    elif source.is_file() and not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='portable-copy-', delete=False) as file:
            temporary = Path(file.name)
        try:
            shutil.copyfile(source, temporary)
            with temporary.open('rb+') as file:
                os.fsync(file.fileno())
            if not destination.exists():
                temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def _preserve_native_history(logs, destination):
    """Copy the latest native run and only its explicit earned-round chain."""
    source = logs / 'roguelike-720p'
    if not source.is_dir():
        return
    runs = sorted((p for p in source.iterdir() if p.is_dir()), key=lambda p: p.name)
    pending = runs[-1:]
    copied = set()
    while pending and len(copied) < 100:
        directory = pending.pop()
        if directory.name in copied:
            continue
        _copy_missing(directory, destination / 'logs' / 'roguelike-720p' / directory.name)
        copied.add(directory.name)
        try:
            for line in (directory / 'events.jsonl').read_text(encoding='utf-8').splitlines():
                event = json.loads(line)
                if event.get('event') != 'native_run_progress_restored':
                    continue
                previous = source / event.get('previous_run', '')
                if (previous.parent.resolve() == source.resolve() and previous.name < directory.name
                        and previous.is_dir()):
                    pending.append(previous)
        except (OSError, ValueError, TypeError, AttributeError):
            # Preserve corrupt evidence too. The existing runtime validation
            # will report it; migration must not manufacture a clean history.
            continue


def resolve_history_file(path, directory):
    """Resolve one copied evidence file using the exact recorded original root."""
    path, directory = Path(path), Path(directory)
    if path.parent.resolve() == directory.resolve():
        return path
    try:
        relative = directory.resolve().relative_to(DATA_ROOT.resolve())
        origin = json.loads((DATA_ROOT / 'portable-data-origin.json').read_text(encoding='utf-8'))
        expected = Path(origin['original_data_root']).resolve() / relative
        copied = directory / path.name
        if path.parent.resolve() == expected and copied.is_file():
            return copied
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return path  # Existing source-containment checks reject unrelated paths.


def _preserve_state(root, destination):
    """Seed a read-only install's state once; leave all original evidence intact."""
    marker = destination / 'portable-data-origin.json'
    if marker.exists():
        return
    for name in ('settings.json', 'config_version.json', 'configs'):
        _copy_missing(root / name, destination / name)
    logs = root / 'logs'
    if logs.is_dir():
        for name in ('daily-checkpoint.json', 'daily-dark-progress.json', 'daily-accounts'):
            _copy_missing(logs / name, destination / 'logs' / name)
        # The current crafting ledger and its evidence live together. Inspect
        # directory metadata only, then copy its latest checkpoint run once.
        runs = sorted((path for path in logs.iterdir() if path.is_dir() and
                       ((path / 'resume.json').is_file() or (path / 'crafting.json').is_file())),
                      key=lambda path: path.name, reverse=True)
        if runs:
            _copy_missing(runs[0], destination / 'logs' / runs[0].name)
        _preserve_native_history(logs, destination)
    marker.write_text(json.dumps({'original_data_root': str(root)}, ensure_ascii=False), encoding='utf-8')


def configure_data_root(root):
    """Use the portable directory when writable, otherwise local user storage."""
    global DATA_ROOT
    root = Path(root).resolve()
    fallback = _fallback_root(root)
    # Once selected, retain that location after permissions change or restart.
    if (fallback / 'portable-data-origin.json').is_file():
        candidate = fallback
    elif all(_writable(root / name) for name in ('', 'configs', 'logs', 'cache')):
        candidate = root
    else:
        candidate = fallback
    if candidate != root:
        if not all(_writable(candidate / name) for name in ('', 'configs', 'logs', 'cache')):
            raise OSError(f'无法写入用户数据目录：{candidate}')
        _preserve_state(root, candidate)
    DATA_ROOT = candidate
    return candidate
