"""The persisted owners of settings used by the existing execution API."""
import json
from pathlib import Path


VERSION = 1
VERSION_FILE = 'config_version.json'
RUNTIME = ('game_exe', 'window_title', 'resolution_width', 'resolution_height',
           'cooperative_mode', 'cooperative_interval')
TASKS = ('execute_black_shield', 'execute_black_dispatch', 'execute_fund',
         'execute_dark_tide', 'execute_ganhuo', 'execute_hunt_gold',
         'execute_daily_reward', 'dark_tide_sweep_count')
MODULE_CRAFTING = ('制作配置', '制作数量', '属性刷新方式',
                   '属性刷新人数门槛', '优先火力攻击奖励任务')
ROGUELIKE = ('肉鸽循环次数', '显示肉鸽悬浮窗')
ADVANCED = ('match_threshold', 'click_min_wait', 'recognition_timeout', 'max_retries')
DEVELOPER = ('debug_mode', 'step_mode')
OWNERS = {'Tasks': TASKS, 'ModuleCrafting': MODULE_CRAFTING,
          'Roguelike': ROGUELIKE,
          'Advanced': ADVANCED, 'Developer': DEVELOPER}
DEFAULTS = {
    'game_exe': 'Warp Knights.exe',
    'window_title': '', 'resolution_width': 0, 'resolution_height': 0,
    'cooperative_mode': True, 'cooperative_interval': 5,
    'match_threshold': .9, 'click_min_wait': .4, 'recognition_timeout': 15.0,
    'max_retries': 2, 'dark_tide_sweep_count': 5,
    'debug_mode': True, 'step_mode': False,
    **{key: True for key in TASKS if key.startswith('execute_')},
    '制作配置': '旧地联合', '制作数量': 1, '属性刷新方式': '自动',
    '属性刷新人数门槛': 6, '优先火力攻击奖励任务': True,
    '肉鸽循环次数': 1, '显示肉鸽悬浮窗': True,
}


def current_version(root: Path):
    path = root / VERSION_FILE
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    version = data.get('config_version')
    if version != VERSION:
        raise ValueError(f'不支持的 config_version: {version!r}')
    return version


def read_effective(root: Path):
    """Return the familiar flat dict consumed by Workflow and diagnostics."""
    settings = json.loads((root / 'settings.json').read_text(encoding='utf-8-sig'))
    if current_version(root) == 0:
        return settings
    for owner, keys in OWNERS.items():
        path = root / 'configs' / f'{owner}.json'
        # Existing v1 installations acquire the new owner through OK Config.
        # Read-only diagnostics must not create files or rewrite old owners.
        data = (read_roguelike(root) if owner == 'Roguelike' else
                json.loads(path.read_text(encoding='utf-8-sig')))
        if set(data) != set(keys):
            raise ValueError(f'{owner}.json 字段不完整或含非 owner 字段')
        settings.update(data)
    if set(settings) != set(RUNTIME + TASKS + MODULE_CRAFTING + ROGUELIKE + ADVANCED + DEVELOPER):
        raise ValueError('settings.json 必须只含 Runtime 字段')
    return settings


def read_roguelike(root: Path):
    path = root / 'configs/Roguelike.json'
    data = (json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else
            {key: DEFAULTS[key] for key in ROGUELIKE})
    data.setdefault('显示肉鸽悬浮窗', True)  # Upgrade existing one-field owner in memory.
    if type(data['显示肉鸽悬浮窗']) is not bool:
        raise ValueError('显示肉鸽悬浮窗必须为布尔值')
    if set(data) != set(ROGUELIKE):
        raise ValueError('Roguelike.json 字段不完整或含非 owner 字段')
    count = data['肉鸽循环次数']
    if type(count) is not int or not 1 <= count <= 100:
        raise ValueError('肉鸽循环次数：必须为 1 至 100 的整数')
    return data
