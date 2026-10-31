"""One immutable set of execution parameters for a single task run."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from src.config_ownership import (ADVANCED, DEVELOPER, MODULE_CRAFTING, ROGUELIKE,
                                  RUNTIME, TASKS, current_version, read_roguelike)
from src import settings
from src.crafting_policy import PROFILES


PREPARE_KEYS = ('第一优先策略', '第二优先策略', '第三优先策略', '优先加成')
OPENING_KEY = '循环间隔（秒）'
TASK_KINDS = ('DAILY', 'INSPECT', 'BLACK_SHIELD', 'BLACK_DISPATCH', 'FUND',
              'DARK_TIDE', 'GANHUO', 'HUNT_GOLD', 'DAILY_REWARD',
              'MODULE_PREPARE', 'MODULE_CRAFT', 'MODULE_RESUME', 'OPENING', 'ROGUELIKE', 'ROGUELIKE_RESUME')


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError(f'不支持的执行参数类型: {type(value).__name__}')
    return value


@dataclass(frozen=True)
class RunPlan(Mapping):
    config_version: int
    task_kind: str
    values: Mapping = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, 'values', _freeze(self.values))

    def __getitem__(self, key):
        return self.values[key]

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)

    def safe_summary(self):
        """Only diagnostic execution values; exclude window identity and local paths."""
        return {'config_version': self.config_version, 'task_kind': self.task_kind,
                'parameters': {key: value for key, value in self.values.items()
                               if key not in ('game_exe', 'window_title')}}


def build_run_plan(task, task_kind=None):
    """Read owners once at the GUI/engine boundary, then validate before input."""
    version = current_version(settings.ROOT)
    source = settings.load_settings()
    kind = task_kind or getattr(task, 'only', None) or 'DAILY'
    if kind not in TASK_KINDS:
        raise ValueError(f'未知任务类型: {kind!r}')
    local = tuple(getattr(task, 'default_config', {}))
    if version and kind in ('MODULE_CRAFT', 'MODULE_RESUME'):
        local = ()  # The shared owner, never the old per-task copy, owns these fields.
    if kind == 'OPENING':
        local = (OPENING_KEY,)
    if kind in ('ROGUELIKE', 'ROGUELIKE_RESUME'):
        local = ()
    task_config = getattr(task, 'config', {}) or {}
    merged = dict(source)
    if version == 0:
        # Preserve stage-0 effective per-task overrides for legacy characterization.
        merged.update({key: task_config.get(key) for key in local})
    else:
        merged.update({key: task_config.get(key) for key in local if key in task_config})
    settings.validate(merged)
    if type(version) is not int or version not in (0, 1):
        raise ValueError('config_version 必须是已支持的整数版本')
    if type(merged['resolution_width']) is not int or type(merged['resolution_height']) is not int:
        raise ValueError('运行尺寸必须是 3840×2160 的整数像素')
    keys = RUNTIME + ADVANCED + DEVELOPER
    if kind == 'DAILY':
        keys += TASKS
    elif kind == 'DARK_TIDE':
        keys += ('dark_tide_sweep_count',)
    if kind in ('ROGUELIKE', 'ROGUELIKE_RESUME'):
        merged.update(read_roguelike(settings.ROOT))
        keys += ROGUELIKE
    if kind in ('MODULE_CRAFT', 'MODULE_RESUME'):
        keys += MODULE_CRAFTING
        if merged['制作配置'] not in PROFILES:
            raise ValueError('制作配置：请选择旧地联合、军工、一致或先攻')
        if type(merged['制作数量']) is not int or not 1 <= merged['制作数量'] <= 100:
            raise ValueError('制作数量：必须为 1 至 100 的整数')
        if merged['属性刷新方式'] not in ('自动', '优先刷新', '不刷新'):
            raise ValueError('属性刷新方式：未知值')
        if merged['属性刷新方式']=='优先刷新':merged['属性刷新方式']='自动'
        if type(merged['属性刷新人数门槛']) is not int or not 4 <= merged['属性刷新人数门槛'] <= 9:
            raise ValueError('属性刷新人数门槛：必须为 4 至 9 的整数')
        if type(merged['优先火力攻击奖励任务']) is not bool:
            raise ValueError('优先火力攻击奖励任务：必须为布尔值')
    if kind == 'MODULE_PREPARE':
        keys += PREPARE_KEYS + ('制作配置',)
        merged.setdefault('制作配置','旧地联合')
        if merged['制作配置'] not in PROFILES:
            raise ValueError('制作配置：请选择旧地联合、军工、一致或先攻')
    if kind == 'OPENING':
        keys += (OPENING_KEY,)
        interval = merged[OPENING_KEY]
        if type(interval) is not int or not 5 <= interval <= 3600:
            raise ValueError('循环间隔（秒）：必须为 5 至 3600 的整数')
    values = {key: merged[key] for key in keys}
    manager = getattr(getattr(task, 'executor', None), 'device_manager', None)
    for key, device in (('input_method', getattr(manager, 'interaction', None)),
                        ('capture_method', getattr(manager, 'capture_method', None))):
        if device is not None:
            values[key] = type(device).__name__
    return RunPlan(version, kind, values)
