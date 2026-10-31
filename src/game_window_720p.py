"""Bounded relocation of the identified 720P game; never change display settings."""
import ctypes
import win32api
import win32con
import win32gui
import win32process
import psutil

from src.client_geometry import validate_client_geometry
from src.workflow import SafetyStop


def ensure_visible_game(hwnd, pid, *, allow_move=True):
    """Return relocation evidence, or None when no change is necessary.

    Ownership is checked before every window mutation and again afterwards.
    The caller must discard captured pixels/actions after a relocation.
    """
    def identity():
        if (not win32gui.IsWindow(hwnd)
                or win32process.GetWindowThreadProcessId(hwnd)[1] != pid
                or psutil.Process(pid).name().casefold() != 'warp knights.exe'):
            raise SafetyStop('游戏窗口归属变化；禁止移动或输入')
        validate_client_geometry(hwnd, expected_size=(1280, 720))

    identity()
    before = win32gui.GetWindowRect(hwnd)
    dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
    minimized = bool(win32gui.IsIconic(hwnd))
    monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    work = tuple(win32api.GetMonitorInfo(monitor)['Work'])

    def contained(rect):
        return work[0] <= rect[0] < rect[2] <= work[2] and work[1] <= rect[1] < rect[3] <= work[3]

    if not minimized and contained(before):
        return None
    if not allow_move:
        raise SafetyStop('窗口连续出界，已达到三次自动归位上限；停止输入')
    if minimized:
        identity()
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    identity()
    rect = win32gui.GetWindowRect(hwnd)
    width, height = rect[2]-rect[0], rect[3]-rect[1]
    if width > work[2]-work[0] or height > work[3]-work[1]:
        raise SafetyStop('当前显示器工作区域无法完整容纳 720P 游戏窗口；未改变分辨率或缩放，停止输入')
    if not contained(rect):
        x = min(max(rect[0], work[0]), work[2]-width)
        y = min(max(rect[1], work[1]), work[3]-height)
        identity()
        win32gui.SetWindowPos(hwnd, 0, x, y, 0, 0,
                             win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
    identity()
    after = win32gui.GetWindowRect(hwnd)
    if win32gui.IsIconic(hwnd) or not contained(after):
        raise SafetyStop('游戏窗口自动归位未成功；停止输入，保留任务进度')
    return dict(hwnd=hwnd, pid=pid, before=list(before), after=list(after),
                work_area=list(work), restored=minimized, dpi_before=dpi,
                dpi_after=ctypes.windll.user32.GetDpiForWindow(hwnd),
                client_origin=list(win32gui.ClientToScreen(hwnd, (0, 0))), client_size=[1280, 720])
