"""Keep the existing BitBlt_RenderFull backend's Win32 handles pointer-safe.

ok-script keeps a GetWindowDC across captures, while the diagnostics panel
samples on a new thread each time. A window DC must be released by the thread
that acquired it, so give each capture one local DC lifetime. No backend,
crop, template, or input behavior is changed.
"""
import ctypes
from ctypes import wintypes


def bind_print_window(function=None):
    """Declare HWND and HDC as pointer-sized Win32 arguments."""
    function = function if function is not None else ctypes.windll.user32.PrintWindow
    function.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    function.restype = wintypes.BOOL
    return function


def install_capture_bindings():
    """Install before OK starts its capture threads; safe to call repeatedly."""
    bind_print_window()
    from ok.device.capture_methods import bitblt, bitblt_utils

    if getattr(bitblt.capture_by_bitblt, '_client_dc_lifetime', False):
        return
    original_capture = bitblt_utils.capture_by_bitblt

    def capture_with_local_dc(context, hwnd, width, height, x, y, render_full_content):
        local = bitblt_utils.BitBltCtxDummy()
        try:
            frame = original_capture(local, hwnd, width, height, x, y, render_full_content)
            # The upstream ndarray is backed by bitmap bits; copy before releasing it.
            return None if frame is None else frame.copy()
        finally:
            bitblt_utils.clean_up_bitblt(local)

    capture_with_local_dc._client_dc_lifetime = True
    bitblt.capture_by_bitblt = capture_with_local_dc
    bitblt_utils.capture_by_bitblt = capture_with_local_dc
