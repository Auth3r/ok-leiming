"""One-time, fail-closed move from copied task settings to their owners."""
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from src.crafting_policy import PROFILES

from src.config_ownership import (ADVANCED, DEFAULTS, DEVELOPER, MODULE_CRAFTING, ROGUELIKE,
                                  OWNERS, RUNTIME, TASKS, VERSION, VERSION_FILE,
                                  current_version)


class MigrationConflict(RuntimeError):
    pass


ALIASES = {'refresh_mode': '属性刷新方式',
           'refresh_min_people': '属性刷新人数门槛',
           'prioritize_fire_reward': '优先火力攻击奖励任务'}
BACKUP = Path('config-backups/stage1-v0')


def _json(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.migration-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.suffix == '.json':
            json.loads(Path(name).read_text(encoding='utf-8'))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _agree(key, sources, conflicts):
    if not sources:
        return None
    first = sources[0][1]
    if any(type(value) is not type(first) or value != first for _, value in sources[1:]):
        conflicts.append(f'{key}: ' + ', '.join(f'{name}={value!r}' for name, value in sources))
    return first


def _plan(raw):
    root = dict(raw['settings.json'])
    names = sorted(name for name in raw if name.startswith('configs/') and name.endswith('Task.json'))
    conflicts = []
    owners = {}
    runtime = {}
    for key in RUNTIME + ADVANCED + DEVELOPER:
        # Missing old task fields were filled from settings.json by OK Config.
        sources = ([(name, raw[name].get(key, root.get(key, DEFAULTS.get(key)))) for name in names]
                   if key not in ('game_exe', 'window_title') else [])
        value = _agree(key, sources, conflicts)
        if value is None:
            value = root.get(key, DEFAULTS.get(key))
        if value is None:
            conflicts.append(f'{key}: settings.json 中缺失，无法补齐')
        if key in RUNTIME:
            runtime[key] = value
        else:
            owner = 'Advanced' if key in ADVANCED else 'Developer'
            owners.setdefault(owner, {})[key] = value

    daily = 'configs/DailyTask.json'
    dark = 'configs/DarkTideTask.json'
    tasks = {}
    for key in TASKS:
        if key == 'dark_tide_sweep_count':
            sources = [(name, raw[name].get(key, root.get(key, DEFAULTS[key])))
                       for name in (daily, dark) if name in raw]
            value = _agree(key, sources, conflicts)
        else:
            value = raw.get(daily, {}).get(key)
        tasks[key] = root.get(key, DEFAULTS[key]) if value is None else value
    owners['Tasks'] = tasks

    craft = 'configs/ModuleCraftTask.json'
    resume = 'configs/ModuleResumeTask.json'
    crafting = {}
    for key in MODULE_CRAFTING:
        sources = [(name, raw[name].get(key, DEFAULTS[key])) for name in (craft, resume)
                   if name in raw and key in raw[name]]
        sources += [(name + ':' + alias, raw[name][alias])
                    for alias, canonical in ALIASES.items() if canonical == key
                    for name in (craft, resume) if alias in raw.get(name, {})]
        for name in (craft, resume):
            if name in raw and key not in raw[name] and not any(alias in raw[name] for alias, canonical in ALIASES.items() if canonical == key):
                sources.append((name + ':旧默认值', DEFAULTS[key]))
        value = _agree(key, sources, conflicts)
        crafting[key] = DEFAULTS[key] if value is None else value
    owners['ModuleCrafting'] = crafting
    owners['Roguelike'] = raw.get('configs/Roguelike.json', {key: DEFAULTS[key] for key in ROGUELIKE})
    owners['Roguelike'].setdefault('显示肉鸽悬浮窗', True)
    if (type(owners['Roguelike']['显示肉鸽悬浮窗']) is not bool or set(owners['Roguelike']) != set(ROGUELIKE) or
            type(owners['Roguelike'].get('肉鸽循环次数')) is not int or
            not 1 <= owners['Roguelike']['肉鸽循环次数'] <= 100):
        conflicts.append('Roguelike.json：循环次数必须为1至100的整数，悬浮窗开关必须为布尔值')

    if conflicts:
        raise MigrationConflict('配置迁移冲突；原文件未修改：\n' + '\n'.join(conflicts))
    from src.settings import validate
    validate(runtime | tasks | crafting | owners['Advanced'] | owners['Developer'])
    if crafting['制作配置'] not in PROFILES:
        raise MigrationConflict('制作配置：请选择旧地联合、军工、一致或先攻')
    if type(crafting['制作数量']) is not int or not 1 <= crafting['制作数量'] <= 100:
        raise MigrationConflict('制作数量：必须为 1 至 100 的整数')
    if crafting['属性刷新方式'] not in ('自动', '优先刷新', '不刷新'):
        raise MigrationConflict(f"属性刷新方式：未知值 {crafting['属性刷新方式']!r}")
    if type(crafting['属性刷新人数门槛']) is not int or not 4 <= crafting['属性刷新人数门槛'] <= 9:
        raise MigrationConflict('属性刷新人数门槛：必须为 4 至 9 的整数')
    if type(crafting['优先火力攻击奖励任务']) is not bool:
        raise MigrationConflict('优先火力攻击奖励任务：必须为布尔值')

    outputs = {'settings.json': _json(runtime)}
    outputs.update({f'configs/{name}.json': _json(owners[name]) for name in OWNERS})
    shared = set(RUNTIME + TASKS + MODULE_CRAFTING + ADVANCED + DEVELOPER) | set(ALIASES)
    for name in names:
        cleaned = {key: value for key, value in raw[name].items() if key not in shared}
        if cleaned != raw[name]:
            outputs[name] = _json(cleaned)
    return outputs


def _restore_if_interrupted(root, backup):
    manifest = backup / 'plan.json'
    if not manifest.exists():
        original_files = _read(backup / 'manifest.json')['files']
        if any(not (root / name).exists() or (root / name).read_bytes() != (backup / name).read_bytes()
               for name in original_files):
            raise MigrationConflict(f'发现不完整备份 {backup} 且原文件已变化；请检查后恢复')
        return
    plan = _read(manifest)
    for name, hashes in plan.items():
        path = root / name
        current = _hash(path.read_bytes()) if path.exists() else None
        if current not in hashes:
            raise MigrationConflict(f'{name} 在迁移中断后被修改；请从 {backup} 手工核对')
    for name, hashes in plan.items():
        original = backup / name
        path = root / name
        if original.exists():
            _atomic(path, original.read_bytes())
        elif path.exists():
            path.unlink()


def _initialize(root):
    """Create a clean install without accepting a partial old config as new."""
    existing = sorted(path for path in (root / 'configs').glob('*.json')
                      if path.name != 'ui_config.json')
    if (root / VERSION_FILE).exists() or existing or (root / BACKUP).exists():
        raise MigrationConflict('缺少 settings.json，但发现已有配置；原文件未修改。'
                                '请从旧程序目录同时复制 settings.json、configs 和 '
                                'config_version.json（如存在），或使用完整备份恢复。')
    # New users start with all seven daily modules selected. Existing saved
    # choices take the migration/versioned paths and are never reset here.
    values = DEFAULTS | {'debug_mode': False}
    from src.settings import validate
    validate(values)
    outputs = {'settings.json': _json({key: values[key] for key in RUNTIME})}
    outputs.update({f'configs/{owner}.json': _json({key: values[key] for key in keys})
                    for owner, keys in OWNERS.items()})
    outputs[VERSION_FILE] = _json({'config_version': VERSION})
    created = []
    try:
        for name, data in outputs.items():
            _atomic(root / name, data)
            created.append(root / name)
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return True


def migrate(root: Path):
    """Run before importing OK or constructing any task Config."""
    root = Path(root)
    if not (root / 'settings.json').exists():
        return _initialize(root)
    if current_version(root) == VERSION:
        from src.config_ownership import read_effective
        from src.settings import validate
        validate(read_effective(root))
        return False
    backup = root / BACKUP
    unmarked_owners = [f'{owner}.json' for owner in OWNERS if owner != 'Roguelike'
                       and (root / 'configs' / f'{owner}.json').exists()]
    if current_version(root) == 0 and unmarked_owners:
        # A completed modern configuration identifies its schema by exact owner
        # fields. Losing only the marker must not require manual file surgery.
        # Validate a disposable view first; never rewrite the user's settings.
        try:
            if set(_read(root / 'settings.json')) != set(RUNTIME):
                raise ValueError('Runtime fields do not identify the current schema')
            with tempfile.TemporaryDirectory(prefix='leiming-config-check-') as directory:
                candidate = Path(directory)
                shutil.copy2(root / 'settings.json', candidate / 'settings.json')
                (candidate / 'configs').mkdir()
                for owner in OWNERS:
                    source = root / 'configs' / f'{owner}.json'
                    if source.exists():
                        shutil.copy2(source, candidate / 'configs' / source.name)
                (candidate / VERSION_FILE).write_bytes(_json({'config_version': VERSION}))
                from src.config_ownership import read_effective
                from src.settings import validate
                validate(read_effective(candidate))
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Incomplete or conflicting legacy data still needs diagnosis.
        else:
            _atomic(root / VERSION_FILE, _json({'config_version': VERSION}))
            return True
    # A valid current schema wins over a stale migration backup. Only an
    # incomplete migration should be restored to its old configuration.
    if backup.exists():
        _restore_if_interrupted(root, backup)
        unmarked_owners = [f'{owner}.json' for owner in OWNERS if owner != 'Roguelike'
                           and (root / 'configs' / f'{owner}.json').exists()]
    if current_version(root) == 0 and unmarked_owners:
        raise MigrationConflict('缺少 config_version.json，但发现新版领域配置：'
                                + '、'.join(unmarked_owners)
                                + '。不会覆盖现有配置；请从旧程序目录复制 '
                                  'config_version.json，或恢复完整备份后重试。')
    paths = [root / 'settings.json', *sorted((root / 'configs').glob('*.json'))]
    originals = {path.relative_to(root).as_posix(): path.read_bytes() for path in paths}
    raw = {name: json.loads(data.decode('utf-8-sig')) for name, data in originals.items()}
    outputs = _plan(raw)  # Detect all conflicts before backup or writes.
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix='stage1-v0-', dir=backup.parent))
        try:
            for name, data in originals.items():
                target = temp / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            (temp / 'manifest.json').write_bytes(_json({'files': sorted(originals)}))
            os.replace(temp, backup)
        finally:
            if temp.exists() and temp.resolve().is_relative_to(root.resolve()):
                shutil.rmtree(temp)
    plan = {name: [hash_value for hash_value in
                   (_hash(originals[name]) if name in originals else None, _hash(data))]
            for name, data in outputs.items()}
    _atomic(backup / 'plan.json', _json(plan))
    try:
        for name, data in outputs.items():
            _atomic(root / name, data)
        from src.config_ownership import read_effective
        # Validate the staged owners without declaring the migration complete.
        staged = json.loads(outputs['settings.json'])
        for owner in OWNERS:
            staged.update(_read(root / 'configs' / f'{owner}.json'))
        from src.settings import validate
        validate(staged)
        if set(staged) != set(RUNTIME + TASKS + MODULE_CRAFTING + ROGUELIKE + ADVANCED + DEVELOPER):
            raise ValueError('领域配置字段验证失败')
        _atomic(root / VERSION_FILE, _json({'config_version': VERSION}))
        validate(read_effective(root))
    except Exception:
        for name in outputs:
            path = root / name
            if name in originals:
                _atomic(path, originals[name])
            elif path.exists():
                path.unlink()
        (root / VERSION_FILE).unlink(missing_ok=True)
        raise
    return True
