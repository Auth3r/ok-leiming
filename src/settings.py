import json
from pathlib import Path
from src.config_ownership import read_effective

from src.app_paths import DATA_ROOT as ROOT


def load_settings():
    data = read_effective(ROOT)
    validate(data)
    return data


def validate(data):
    if not isinstance(data['window_title'], str):
        raise ValueError('window_title 必须是字符串；留空表示仅按进程名识别')
    exe = data.get('game_exe', '')
    if not isinstance(exe, str) or not exe.lower().endswith('.exe') or any(c in exe for c in '/\\'):
        raise ValueError('game_exe 必须是程序文件名，例如 Warp Knights.exe')
    from src.resolution import selected_size
    selected_size(data)
    bounds = {'match_threshold': (0.8, 0.99), 'click_min_wait': (0.1, 3),
              'recognition_timeout': (1, 20), 'max_retries': (0, 3),
              'dark_tide_sweep_count': (0, 50), 'cooperative_interval': (1, 120)}
    for key, (low, high) in bounds.items():
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f'{key} 必须在 {low}–{high} 之间')
    for key in ('max_retries', 'dark_tide_sweep_count'):
        if type(data[key]) is not int:
            raise ValueError(f'{key} 必须是整数')
    for key, value in data.items():
        if key.startswith('execute_') or key in ('debug_mode', 'step_mode','cooperative_mode'):
            if type(value) is not bool:
                raise ValueError(f'{key} 必须是布尔值')
