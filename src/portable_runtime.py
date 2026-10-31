"""Prepare portable data and Unicode file access before starting the framework."""
import os
import sys
from pathlib import Path
from src.version import APP_NAME

_instance_handles = {}


def check_single_instance():
    """Keep the upstream data-directory mutex without terminating its owner."""
    import ctypes
    import hashlib
    import logging
    from ctypes import wintypes

    # prepare() already selects DATA_ROOT as cwd. Preserve the upstream name
    # so an existing instance from an older release is recognized too.
    name = hashlib.md5(os.getcwd().encode()).hexdigest()
    if name in _instance_handles:
        return True
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = create(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    if error == 183:  # ERROR_ALREADY_EXISTS
        close(handle)
        message = '程序已在运行，请使用已有窗口。本次重复启动已取消，正在执行的任务继续运行。'
        logging.getLogger(__name__).info(message)
        if getattr(sys, 'frozen', False):
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)
        # OK ignores a False return, so stop only this duplicate before any UI
        # or executor is initialized. The first process retains its own handle.
        raise SystemExit(0)
    _instance_handles[name] = handle  # Windows closes it when this process exits.
    return True


def install_single_instance_guard():
    import ok
    from ok.util import process
    # _resolve() may already have cached the lazily imported upstream callback.
    process.check_mutex = ok.check_mutex = check_single_instance


def install_unicode_file_access():
    """Keep OpenCV codecs and flags, replacing only Windows filename I/O."""
    import cv2
    import numpy as np
    if not getattr(cv2.imread, '_leiming_unicode', False):
        original_read, original_write = cv2.imread, cv2.imwrite

        def read(filename, flags=cv2.IMREAD_COLOR):
            if os.fspath(filename).isascii():
                return original_read(filename, flags)
            try:
                encoded = np.fromfile(filename, dtype=np.uint8)
                return cv2.imdecode(encoded, flags) if encoded.size else None
            except OSError:
                return None

        def write(filename, image, params=None):
            params = [] if params is None else params
            if os.fspath(filename).isascii():
                return original_write(filename, image, params)
            encoded, data = cv2.imencode(Path(filename).suffix, image, params)
            if not encoded:
                return False
            try:
                data.tofile(filename)
                return True
            except OSError:
                return False

        read._leiming_unicode = True
        cv2.imread, cv2.imwrite = read, write
    # The pinned OpenVINO supports Unicode model/cache paths. The old upstream
    # ASCII guard predates that support and the OpenCV filename adapter above.
    from ok.util import file as ok_file
    ok_file.install_path_isascii = lambda: (True, str(Path(sys.executable).parent))


def prepare(root):
    from src.app_paths import configure_data_root
    data_root = configure_data_root(root)
    os.chdir(data_root)
    # This EXE has no external launcher. Inherited launcher metadata must not
    # override its displayed version, start another process or enable updates.
    if getattr(sys, 'frozen', False):
        for key in tuple(os.environ):
            if key.upper().startswith('PYAPPIFY_'):
                os.environ.pop(key, None)
    install_unicode_file_access()
    if getattr(sys, 'frozen', False):
        import onnxocr.utils
        onnxocr.utils.module_dir = Path(root) / '_internal' / 'onnxocr'
    return data_root


def report_startup_error(root):
    import traceback
    detail = traceback.format_exc()
    directory = root / 'logs'
    try:
        directory.mkdir(exist_ok=True)
        (directory / 'startup-error.txt').write_text(detail, encoding='utf-8')
    except OSError:
        pass  # The original error must remain visible even in a read-only folder.
    if getattr(sys, 'frozen', False) and '--release-smoke' not in sys.argv:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f'启动失败，请查看 {directory / "startup-error.txt"}。\n' + detail[-1500:],
                                        APP_NAME, 0x10)
