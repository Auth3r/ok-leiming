"""Read-only window diagnostics and bounded, local support bundles."""
import json
import platform
import zipfile
from datetime import datetime
from pathlib import Path
from src.run_plan import RunPlan
from src.runtime_identity import get_runtime_identity


def require_input_privilege(hwnd):
    """Fail before a blocked SendInput is mistaken for a game transition failure."""
    import win32api
    import win32con
    import win32process
    import win32security
    from src.workflow import SafetyStop
    def elevated(process):
        token=win32security.OpenProcessToken(process,win32con.TOKEN_QUERY)
        try:return bool(win32security.GetTokenInformation(token,win32security.TokenElevation))
        finally:token.Close()
    if elevated(win32api.GetCurrentProcess()):return
    process=win32api.OpenProcess(0x1000,False,win32process.GetWindowThreadProcessId(hwnd)[1])
    try:blocked=elevated(process)
    finally:process.Close()
    if blocked:
        raise SafetyStop('输入权限不匹配：游戏以管理员权限运行，助手为普通权限；未发送点击。请以管理员身份启动当前助手后重试。')


def running_plan(executor):
    """Return the active run's snapshot, if one has been built."""
    return getattr(getattr(executor, 'current_task', None), 'run_plan', None)


def window_identity(hwnd):
    import psutil
    import win32gui
    import win32process
    if not hwnd or not win32gui.IsWindow(hwnd):
        return {'connected': False, 'reason': '未连接到游戏窗口'}
    try:
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        process = psutil.Process(pid)
        rect = win32gui.GetClientRect(hwnd)
        return {'connected': True, 'hwnd': hwnd, 'pid': pid,
                'exe': process.name(), 'title': win32gui.GetWindowText(hwnd),
                'resolution': [rect[2]-rect[0], rect[3]-rect[1]],
                'client_origin': list(win32gui.ClientToScreen(hwnd,(0,0))),
                'dpi': __import__('ctypes').windll.user32.GetDpiForWindow(hwnd),
                'minimized': bool(win32gui.IsIconic(hwnd)),
                'visible': bool(win32gui.IsWindowVisible(hwnd))}
    except Exception as error:
        return {'connected': False, 'reason': f'读取窗口失败：{error}'}


def matches_target(identity, settings):
    return (identity.get('connected', False)
            and identity.get('exe', '').casefold() == settings['game_exe'].casefold()
            and (not settings['window_title'] or identity.get('title') == settings['window_title']))


def capture_720p_frame(identity):
    """Read-only WGC preview; never reuse the task's unlabelled frame cache."""
    from src.capture_720p import Capture720p
    with Capture720p(identity['hwnd'], identity['pid'], game_exe=identity['exe']) as capture:
        return capture.get_frame()


def collect_status(manager, executor, settings):
    import ctypes
    import psutil
    status = {'time': datetime.now().isoformat(), 'target_exe': settings['game_exe'],
              'target_title': settings['window_title'], 'processes': [],
              'administrator': bool(ctypes.windll.shell32.IsUserAnAdmin())}
    status['runtime_identity'] = get_runtime_identity().as_dict()
    status['config_source'] = 'run_plan' if isinstance(settings, RunPlan) else 'persisted'
    if isinstance(settings, RunPlan):
        status['run_plan'] = settings.safe_summary()
    try:
        for process in psutil.process_iter(['pid', 'name']):
            if (process.info['name'] or '').casefold() == settings['game_exe'].casefold():
                status['processes'].append(process.info)
    except Exception as error:
        status['process_error'] = str(error)
    window = getattr(manager, 'hwnd_window', None)
    identity = window_identity(getattr(window, 'hwnd', 0))
    status['window'] = identity
    status['capture_method'] = type(manager.capture_method).__name__ if manager.capture_method else None
    task = getattr(executor, 'current_task', None)
    status['task'] = getattr(task, 'name', None)
    status['paused'] = executor.paused
    frame = None
    if not matches_target(identity, settings):
        status['message'] = ('发现游戏进程，但窗口尚未连接：请保持游戏窗口打开并点击刷新'
                             if status['processes'] else f'未发现 {settings["game_exe"]}，请先手动打开游戏')
        if identity.get('connected'):
            status['message'] = '选中窗口不属于目标游戏，已拒绝预览'
    elif identity.get('minimized'):
        status['message'] = '窗口已连接，但已最小化；请还原游戏窗口'
    else:
        try:
            if identity.get('resolution') == [1280, 720]:
                # 720P diagnostics are separate from guarded 4K task execution.
                status['capture_method'] = 'WGC'
                status['frame_source'] = '720P WGC 实时截图'
                packet = capture_720p_frame(identity)
                frame = packet.image
                status['frame_provenance'] = packet.metadata
                try:
                    from src.navigation_720p import Navigation720p
                    status['navigation_720p'] = Navigation720p().observe(
                        packet, hwnd=identity['hwnd'], pid=identity['pid'])
                    if status['navigation_720p']['state'] == 'UNKNOWN':
                        from src.roguelike_preparation_720p import RoguelikePreparation720p
                        status['roguelike_preparation_720p'] = RoguelikePreparation720p().observe(
                            packet, hwnd=identity['hwnd'], pid=identity['pid'])
                except Exception as error:
                    status['navigation_720p'] = {'state': 'UNKNOWN', 'error': str(error)}
            else:
                # Active 4K tasks own capture. Preserve their existing path.
                active = task is not None and not executor.paused
                status['frame_source'] = '任务缓存' if active else '实时截图'
                frame = executor.nullable_frame() if active else (
                    manager.capture_method.get_frame() if manager.capture_method else None)
            if frame is None:
                status['message'] = '窗口已连接，但截图尚不可用；可刷新或切换截图方式'
            else:
                frame = frame.copy()
                size = list(frame.shape[1::-1])
                status['frame_resolution'] = size
                from src.resolution import Resolution, selected_size
                Resolution(tuple(size),selected_size(settings))
                if size != identity['resolution']: raise ValueError('截图与客户区尺寸不一致')
                status['message'] = f'已锚定 {identity["exe"]} · {size[0]}×{size[1]} · {status["frame_source"]}'
        except Exception as error:
            status['capture_error'] = str(error)
            status['message'] = f'窗口已连接，但截图失败：{error}'
    return status, frame


def create_report(root, status, settings, preview=None, max_bytes=24*1024*1024):
    """Whitelist our task configs only; exclude upstream notification credentials."""
    root = Path(root)
    status = dict(status)
    if isinstance(settings, RunPlan):
        status['config_source'] = 'run_plan'
        status['run_plan'] = settings.safe_summary()
    directory = root/'reports'
    directory.mkdir(exist_ok=True)
    target = directory/f'镭明问题报告-{datetime.now():%Y%m%d-%H%M%S-%f}.zip'
    manifest = {'included': [], 'skipped': [], 'errors': []}
    identity = get_runtime_identity()
    metadata = {'version': identity.app_version, 'python': identity.python_version,
                'runtime_identity': identity.as_dict(),
                'windows': platform.platform(), 'connection': status,
                'config_source': 'run_plan' if isinstance(settings, RunPlan) else 'persisted',
                'settings': {key: value for key, value in settings.items()
                             if key not in ('game_exe', 'window_title')}}
    if isinstance(settings, RunPlan):
        metadata['run_plan'] = settings.safe_summary()
    total = 0
    candidates = []
    for folder in ('logs', 'screenshots'):
        for path in (root/folder).rglob('*'):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in ('.log', '.txt', '.jsonl', '.json', '.png', '.jpg'):
                if 'smoke-configs' not in path.parts:
                    candidates.append(path)
    allowed = ('InspectTask', 'DailyTask', 'BlackShieldTask', 'BlackDispatchTask', 'FundTask', 'DarkTideTask',
               'GanhuoTask', 'HuntGoldTask', 'DailyRewardTask', 'OpeningTask', 'ModulePrepareTask')
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('diagnostics.json', json.dumps(metadata, ensure_ascii=False, indent=2, default=str))
        archive.writestr('说明.txt', '将本 ZIP 文件拖进当前聊天即可。内含游戏截图、连接诊断和最近日志；不会自动上传。\n日志按时间从新到旧收集，上限 24 MB（压缩前）。遗漏见 manifest.json。')
        if preview:
            archive.writestr('preview.jpg', preview)
        for name in (() if isinstance(settings, RunPlan) else allowed):
            path = root/'configs'/f'{name}.json'
            if path.exists():
                try:
                    values = json.loads(path.read_text(encoding='utf-8-sig'))
                    archive.writestr(f'task-settings/{name}.json', json.dumps(
                        {k: v for k, v in values.items() if k in settings or k=='循环间隔（秒）'}, ensure_ascii=False, indent=2))
                except Exception as error:
                    manifest['errors'].append(f'{name}: {error}')
        def mtime(path):
            try: return path.stat().st_mtime
            except OSError: return 0
        for path in sorted(candidates, key=mtime, reverse=True):
            relative = path.relative_to(root).as_posix()
            try:
                if not path.resolve().is_relative_to(root.resolve()):
                    continue
                size = path.stat().st_size
                if total+size > max_bytes:
                    manifest['skipped'].append(relative)
                    continue
                # Bound reading even when an active log grows during export.
                with path.open('rb') as stream:
                    data = stream.read(min(size, max_bytes-total))
                archive.writestr(relative, data)
                total += len(data)
                manifest['included'].append(relative)
            except OSError as error:
                manifest['errors'].append(f'{relative}: {error}')
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    return target
