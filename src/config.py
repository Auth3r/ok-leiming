"""Windows-only specialization of ok-script-app src/config.py."""
from src.settings import ROOT
from src.app_paths import APP_ROOT
from src.portable_runtime import install_unicode_file_access, install_single_instance_guard
install_unicode_file_access()
install_single_instance_guard()
from src.version import APP_NAME, APP_VERSION
from src.crafting_policy import PROFILES
from src.config_migration import migrate
migrate(ROOT)
from src.capture_win32 import install_capture_bindings
install_capture_bindings()
from src.settings import load_settings, validate
from src.interaction import GameForegroundInteraction
from src.config_ownership import ADVANCED, DEVELOPER, MODULE_CRAFTING, TASKS, DEFAULTS, ROGUELIKE
from ok.util.config import ConfigOption

settings = load_settings()


def validate_owner(key, value):
    try:
        if key == '显示肉鸽悬浮窗':
            if type(value) is not bool: raise ValueError('显示肉鸽悬浮窗必须为布尔值')
        elif key in ROGUELIKE:
            if type(value) is not int or not 1 <= value <= 100: raise ValueError('肉鸽循环次数应为1至100的整数')
        elif key in MODULE_CRAFTING:
            if key == '制作配置' and value not in PROFILES: raise ValueError('请选择旧地联合、军工、一致或先攻')
            if key == '制作数量' and (type(value) is not int or not 1 <= value <= 100): raise ValueError('制作数量应为1至100的整数')
            if key == '属性刷新方式' and value not in ('自动', '优先刷新', '不刷新'): raise ValueError('请选择自动、优先刷新或不刷新')
            if key == '属性刷新人数门槛' and (type(value) is not int or not 4 <= value <= 9): raise ValueError('属性刷新人数门槛应为4至9的整数')
            if key == '优先火力攻击奖励任务' and type(value) is not bool: raise ValueError('任务优先开关必须为布尔值')
        else:
            candidate = load_settings()
            candidate[key] = value
            validate(candidate)
    except (ValueError, KeyError, TypeError) as error:
        return False, str(error)
    return True, None


config = {
    'debug': False,
    'gui': {'type': 'qt', 'window_size': {'width': 1100, 'height': 800, 'min_width': 800, 'min_height': 600}},
    'gui_title': APP_NAME,
    'gui_icon': str(APP_ROOT / 'icons/icon.png'),
    'version': APP_VERSION,
    'my_app': ['src.diagnostics_ui', 'DiagnosticsApp'],
    'config_folder': str(ROOT / 'configs'),
    'global_configs': [
        ConfigOption(name, {key: DEFAULTS[key] for key in keys}, validator=validate_owner)
        for name, keys in (('Tasks', TASKS), ('ModuleCrafting', MODULE_CRAFTING),
                           ('Advanced', ADVANCED), ('Developer', DEVELOPER))
    ] + [ConfigOption('Roguelike', {key: DEFAULTS[key] for key in ROGUELIKE},
                      description='4K / 720P 共用；下次开始生效。F7暂停/继续；F8停止。',
                      config_description={'肉鸽循环次数': '1至100；最终结算确认后才计数'},
                      validator=validate_owner)],
    'custom_tasks': False,
    'windows': {
        'start_exe': False,
        'exe': [settings['game_exe']],
        'title': settings['window_title'] or None,
        'interaction': [GameForegroundInteraction],
        'capture_method': ['WGC', 'BitBlt_RenderFull'],
        'require_bg': True,
        'check_hdr': False,
    },
    'supported_resolution': {'ratio': '16:9', 'min_size': (1280, 720)},
    'start_timeout': 20,
    'wait_until_check_delay': 0.2,
    'screenshots_folder': str(ROOT / 'screenshots'),
    'template_matching': {
        'coco_feature_json': str(APP_ROOT / 'templates/coco_annotations.json'),
        'default_threshold': settings['match_threshold'],
        'default_horizontal_variance': 0.002,
        'default_vertical_variance': 0.002,
    },
    'ocr': {'lib': 'onnxocr', 'params': {'use_openvino': True}},
    'onetime_tasks': [['src.tasks', name] for name in (
        'DailyTask', 'FundTask', 'BlackShieldTask', 'BlackDispatchTask',
        'DarkTideTask', 'GanhuoTask', 'HuntGoldTask', 'DailyRewardTask', 'InspectTask', 'ModulePrepareTask', 'ModuleCraftTask', 'ModuleResumeTask', 'RoguelikeTask', 'RoguelikeResumeTask')]+[['src.opening','OpeningTask']],
}
