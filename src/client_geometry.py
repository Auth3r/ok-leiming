"""Game client coordinates are physical pixels; Qt logical pixels stay in UI.

Points address pixels (720p: 0..1279, 0..719); bboxes have exclusive right
and bottom edges. Only Win32 converts client/screen coordinates. The existing
application must initialize per-monitor DPI awareness before using this API.
This module reads geometry only: it never initializes DPI, captures, or inputs.
Legacy business coordinates are not migrated by this foundation.
"""
import ctypes
import sys
from dataclasses import dataclass
from functools import lru_cache
from numbers import Integral

import win32gui

REFERENCE_SIZE = (1280, 720)


class ClientGeometryError(RuntimeError):
    """Invalid HWND, virtualized coordinates, or unusable client geometry."""


@lru_cache(maxsize=1)
def _dpi_api():
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.GetThreadDpiAwarenessContext.argtypes = []
    api.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    api.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    api.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
    return api


def _dpi_awareness():
    api = _dpi_api()
    # Query the calling thread, not just the process: threads can override DPI.
    return api.GetAwarenessFromDpiAwarenessContext(api.GetThreadDpiAwarenessContext())


@dataclass(frozen=True)
class ClientGeometry:
    hwnd: int
    width: int
    height: int

    @property
    def size(self):
        return self.width, self.height

    @property
    def is_standard_720p(self):
        return self.size == REFERENCE_SIZE


def validate_client_geometry(hwnd, *, expected_size=None):
    """Read usable physical geometry; optional size check never gates app startup.

    Both per-monitor v1 and v2 are accepted. System-aware/unaware calling
    threads are rejected, since their coordinates may be virtualized when the
    game moves between monitors. No DPI state or game window is changed.
    """
    if isinstance(hwnd, bool) or not isinstance(hwnd, Integral) or hwnd <= 0:
        raise ClientGeometryError(f'Invalid game HWND: {hwnd!r}')
    try:
        if not win32gui.IsWindow(hwnd):
            raise ClientGeometryError(f'Invalid game HWND: {hwnd!r}')
        awareness = _dpi_awareness()
        if awareness != 2:
            raise ClientGeometryError(
                f'Per-monitor DPI awareness required; calling thread awareness={awareness}. '
                'Initialize the existing application DPI flow before reading client pixels.')
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
    except ClientGeometryError:
        raise
    except Exception as error:
        raise ClientGeometryError(f'Cannot read client geometry for HWND {hwnd}: {error}') from error
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ClientGeometryError(f'Empty game ClientRect for HWND {hwnd}: {width}x{height}')
    geometry = ClientGeometry(int(hwnd), width, height)
    if getattr(sys, 'frozen', False):
        from src.release_limits import check_environment
        try:
            check_environment(geometry.size, ctypes.windll.user32.GetDpiForWindow(hwnd))
        except ValueError as error:
            raise ClientGeometryError(str(error)) from error
    if expected_size is not None and geometry.size != tuple(expected_size):
        raise ClientGeometryError(f'Expected client size {tuple(expected_size)}, got {geometry.size}')
    return geometry


def get_client_size(hwnd):
    return validate_client_geometry(hwnd).size


def _pixel(value):
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError('Coordinates must be integer physical pixels, not Qt logical values')
    return int(value)


def _convert(hwnd, point, method):
    try:
        return tuple(method(hwnd, point))
    except Exception as error:
        raise ClientGeometryError(f'Win32 coordinate conversion failed for HWND {hwnd}: {error}') from error


def client_to_screen(hwnd, x, y):
    """Convert via the current HWND, never a cached/hardcoded window origin.

    Boundary points (e.g. 1280,720) are allowed for bbox conversion; this
    read-only function does not claim that such points are clickable pixels.
    """
    validate_client_geometry(hwnd)
    return _convert(hwnd, (_pixel(x), _pixel(y)), win32gui.ClientToScreen)


def screen_to_client(hwnd, screen_x, screen_y):
    validate_client_geometry(hwnd)
    return _convert(hwnd, (_pixel(screen_x), _pixel(screen_y)), win32gui.ScreenToClient)


def client_input_to_screen(hwnd, x, y):
    """Fail closed for input pixels, then convert using live Win32 geometry.

    Unlike bbox conversion, the exclusive right/bottom edges are not inputs.
    No reference-size inference, clamping, or DPI multiplication is permitted.
    """
    geometry = validate_client_geometry(hwnd)
    x, y = _pixel(x), _pixel(y)
    if not (0 <= x < geometry.width and 0 <= y < geometry.height):
        raise ClientGeometryError(f'Input pixel outside client {geometry.size}: {(x, y)}')
    return _convert(hwnd, (x, y), win32gui.ClientToScreen)


def client_bbox_to_screen(hwnd, bbox):
    """Map a client-only, half-open bbox through Win32 at both corners."""
    geometry = validate_client_geometry(hwnd)
    left, top, right, bottom = map(_pixel, bbox)
    if not (0 <= left < right <= geometry.width and 0 <= top < bottom <= geometry.height):
        raise ValueError(f'Client bbox outside {geometry.size}: {bbox!r}')
    origin = _convert(hwnd, (left, top), win32gui.ClientToScreen)
    end = _convert(hwnd, (right, bottom), win32gui.ClientToScreen)
    return (*origin, *end)


def reference_to_client(hwnd, x, y):
    """1280x720 reference pixel -> current client pixel, independent of DPI/DPR.

    This is only an explicit coordinate helper. It does not resize screenshots,
    templates or ROI, and does not enable other resolutions in legacy tasks.
    """
    geometry = validate_client_geometry(hwnd)
    x, y = _pixel(x), _pixel(y)
    rw, rh = REFERENCE_SIZE
    if not (0 <= x < rw and 0 <= y < rh):
        raise ValueError(f'Reference pixel outside {REFERENCE_SIZE}: {(x, y)}')
    return (min(geometry.width - 1, round(x * geometry.width / rw)),
            min(geometry.height - 1, round(y * geometry.height / rh)))
