"""Resolve a WGC window image against measured physical window rectangles."""
import ctypes
from ctypes import wintypes

from src.client_geometry import ClientGeometryError, validate_client_geometry, _dpi_awareness


def read_capture_geometry(hwnd):
    import win32gui
    geometry = validate_client_geometry(hwnd)
    rect = wintypes.RECT()
    api = ctypes.WinDLL('dwmapi')
    api.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD]
    api.DwmGetWindowAttribute.restype = ctypes.c_long
    result = api.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
    if result < 0:
        raise ClientGeometryError(f'Cannot establish WGC frame bounds: HRESULT {result:#x}')
    origin = win32gui.ClientToScreen(hwnd, (0, 0))
    return dict(client_size=list(geometry.size), client_origin=list(origin),
                window_rect=list(win32gui.GetWindowRect(hwnd)),
                dwm_frame_rect=[rect.left, rect.top, rect.right, rect.bottom],
                dpi=int(ctypes.windll.user32.GetDpiForWindow(hwnd)),
                dpi_awareness=_dpi_awareness())


def resolve_client_crop(raw_size, geometry):
    """Accept only an unscaled measured source; never infer symmetric borders."""
    if geometry['dpi_awareness'] != 2 or geometry['dpi'] <= 0:
        raise ClientGeometryError('Untrusted WGC DPI geometry')
    width, height = raw_size
    cw, ch = geometry['client_size']
    if min(width, height, cw, ch) <= 0:
        raise ClientGeometryError('Empty WGC/client geometry')
    candidates = {}
    for name in ('dwm_frame_rect', 'window_rect'):
        left, top, right, bottom = geometry[name]
        if (right-left, bottom-top) == (width, height):
            candidates.setdefault(tuple(geometry[name]), []).append(name)
    if len(candidates) != 1:
        raise ClientGeometryError(
            f'Untrusted WGC pixel mapping: raw={raw_size}, measured={geometry}')
    rect, sources = next(iter(candidates.items()))
    x, y = (geometry['client_origin'][0]-rect[0], geometry['client_origin'][1]-rect[1])
    if not (0 <= x < x+cw <= width and 0 <= y < y+ch <= height):
        raise ClientGeometryError('WGC source does not contain the complete game client')
    return dict(geometry, raw_size=[width, height], source_rect=list(rect),
                source_bounds=sources, client_crop=[x, y, x+cw, y+ch],
                image_size=[cw, ch], scale=[1, 1], coordinate_space='physical_client_pixels')
