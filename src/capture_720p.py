"""Read-only, fail-closed native-client WGC capture (720P default, optional 4K).

The backend is the installed WGC implementation. A private MTA owns its entire
session lifetime; callers receive copied pixels and explicit source evidence.
There is no desktop/BitBlt fallback and no application-wide COM/DPI override.
"""
import ctypes
import queue
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from numbers import Integral
from types import SimpleNamespace

import numpy as np

CLIENT_SIZE = (1280, 720)
CLIENT_SIZES = (CLIENT_SIZE, (3840, 2160))
GAME_EXE = 'Warp Knights.exe'


class CaptureError(RuntimeError):
    """Capture failed; no pixels from this request may reach recognition."""


@dataclass(frozen=True)
class TargetIdentity:
    hwnd: int
    pid: int
    exe: str
    client_size: tuple[int, int]


@dataclass(frozen=True)
class BackendFrame:
    image: np.ndarray
    system_relative_time: int
    callback_thread_id: int
    callback_apartment: int
    hwnd: int
    pid: int
    mapping: dict | None = None


@dataclass(frozen=True)
class FrameEnvelope:
    image: np.ndarray
    hwnd: int
    pid: int
    backend: str
    pixel_format: str
    sequence: int
    session_id: str
    request_qpc: int
    system_relative_time: int
    acquired_at: float
    owner_thread_id: int
    owner_apartment: int
    callback_thread_id: int
    callback_apartment: int
    mapping: dict | None = None

    @property
    def metadata(self):
        return {key: value for key, value in vars(self).items() if key != 'image'}


def _apartment_type():
    api = ctypes.WinDLL('ole32')
    api.CoGetApartmentType.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
    api.CoGetApartmentType.restype = ctypes.c_long
    apartment, qualifier = ctypes.c_int(), ctypes.c_int()
    result = api.CoGetApartmentType(ctypes.byref(apartment), ctypes.byref(qualifier))
    if result < 0:
        raise CaptureError(f'Cannot read COM apartment: HRESULT 0x{result & 0xffffffff:08X}')
    return apartment.value


@contextmanager
def _mta_apartment():
    api = ctypes.WinDLL('combase')
    api.RoInitialize.argtypes = [ctypes.c_uint]
    api.RoInitialize.restype = ctypes.c_long
    api.RoUninitialize.argtypes = []
    api.RoUninitialize.restype = None
    result = api.RoInitialize(1)  # RO_INIT_MULTITHREADED, only this new worker.
    if result < 0:
        raise CaptureError(f'WGC MTA initialization failed: HRESULT 0x{result & 0xffffffff:08X}')
    try:
        yield _apartment_type()
    finally:
        api.RoUninitialize()


def _qpc_ticks():
    """WinRT SystemRelativeTime uses QPC in 100 ns units, not wall-clock time."""
    api = ctypes.WinDLL('kernel32')
    for name in ('QueryPerformanceCounter', 'QueryPerformanceFrequency'):
        function = getattr(api, name)
        function.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
        function.restype = ctypes.c_int
    counter, frequency = ctypes.c_longlong(), ctypes.c_longlong()
    if not api.QueryPerformanceCounter(ctypes.byref(counter)) or not api.QueryPerformanceFrequency(ctypes.byref(frequency)):
        raise CaptureError('Cannot read QPC clock')
    return counter.value * 10_000_000 // frequency.value


def _read_identity(hwnd, expected_size=CLIENT_SIZE):
    import psutil
    import win32gui
    import win32process
    from src.client_geometry import validate_client_geometry

    geometry = validate_client_geometry(hwnd, expected_size=expected_size)
    if win32gui.IsIconic(hwnd):
        raise CaptureError('Game window is minimized')
    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    return TargetIdentity(int(hwnd), pid, psutil.Process(pid).name(), geometry.size)


def _preload_bindings():
    # rotypes initializes STA once, at import. Import it before the new MTA;
    # never change its global initializer or an existing thread's apartment.
    import ok.rotypes.Windows.Graphics.Capture  # noqa: F401


def _guard_agile_factories():
    # Called only after the owner initialized MTA, including when a GUI sample
    # originates on a fresh thread that has never initialized COM itself.
    from ok.rotypes import IUnknown
    from ok.rotypes.roapi import GetActivationFactory
    from ok.rotypes.types import GUID
    from ok.rotypes.Windows.Graphics.Capture import IDirect3D11CaptureFramePoolStatics2

    agile = GUID('94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90')
    factories = (
        GetActivationFactory('Windows.Graphics.Capture.GraphicsCaptureItem'),
        GetActivationFactory('Windows.Graphics.Capture.Direct3D11CaptureFramePool',
                             IDirect3D11CaptureFramePoolStatics2),
    )
    for factory in factories:
        reference = IUnknown()
        try:
            factory.QueryInterface(ctypes.byref(agile), ctypes.byref(reference))
            if not reference.value:
                raise CaptureError('WinRT activation factory is not agile')
        except Exception as error:
            raise CaptureError(f'Cannot safely share WinRT activation factory: {error}') from error
        finally:
            reference.Release()


def _make_wgc_backend(target):
    import win32gui
    from ok.device.capture_methods.windows_graphics import WindowsGraphicsCaptureMethod

    _guard_agile_factories()

    class OwnedWGC(WindowsGraphicsCaptureMethod):
        def __init__(self, state):
            self.owner_thread_id = threading.get_ident()
            self.closed_requested = threading.Event()
            self._initial_attempt_completed = False
            self._start_depth = 0
            self._deferred_close = False
            self._request_qpc = None
            self._last_timestamp = None
            self._evidence = None
            self._callback_error = None
            super().__init__(state)
            if not self.connected():
                raise CaptureError('WGC initialization failed; no fallback is permitted')

        def start_or_stop(self, *args, **kwargs):
            if self.closed_requested.is_set():
                raise CaptureError('WGC window/session has closed')
            if self._initial_attempt_completed and self.frame_pool is None:
                raise CaptureError('WGC session ended; create a new capture explicitly')
            self._start_depth += 1
            try:
                result = super().start_or_stop(*args, **kwargs)
            finally:
                self._start_depth -= 1
                if self._start_depth == 0 and self._deferred_close:
                    # Upstream calls close() while its start_or_stop RLock is
                    # held, including initialization failures. Wait until that
                    # enclosing lock has been released before native shutdown.
                    self._deferred_close = False
                    self.close()
            self._initial_attempt_completed = True
            return result

        def close(self, *args):
            if threading.get_ident() != self.owner_thread_id:
                # GraphicsCaptureItem.Closed comes from a WinRT callback. Its
                # only job is to notify the owner, which releases resources.
                self.closed_requested.set()
                self.frame_event.set()
                return
            self.closed_requested.set()
            if self._start_depth:
                self._deferred_close = True
                self.frame_requested.clear()
                self.frame_event.set()
                return

            # An in-flight D3D copy completes under this lock before its
            # resources are detached. Subsequent callbacks see no frame pool.
            # Native Close may wait for those callbacks, so it must run after
            # releasing the lock they acquire, on the same MTA owner.
            with self.lock:
                self.frame_requested.clear()
                self.frame_event.set()
                self.last_frame = None
                self._evidence = None
                self._request_qpc = None
                resources = []
                for name, method in (('frame_pool', 'Close'), ('session', 'Close'),
                                     ('rtdevice', 'Release'), ('dxdevice', 'Release'),
                                     ('immediatedc', 'Release'), ('cputex', 'Release')):
                    resources.append((name, getattr(self, name), method))
                    setattr(self, name, None)
                item, self.item = self.item, None
                self.capture_hwnd = 0
                self.capture_target_signature = None
            errors = []
            for name, resource, method in resources:
                if resource is not None and (method == 'Close' or resource):
                    try:
                        getattr(resource, method)()
                    except Exception as error:
                        errors.append(f'{name}.{method}: {error}')
            # Retain the item until native Close has returned, then drop its
            # Python COM reference outside the capture lock as well.
            del item
            if errors:
                raise CaptureError('WGC resource release failed: ' + '; '.join(errors))

        def convert_dx_frame(self, frame):
            try:
                apartment = _apartment_type()
                if apartment != 1:
                    raise CaptureError(f'WGC callback is outside MTA: apartment={apartment}')
                timestamp = frame.SystemRelativeTime
                if (self._request_qpc is None or timestamp < self._request_qpc or
                        (self._last_timestamp is not None and timestamp <= self._last_timestamp)):
                    # Discard buffered frames while the same bounded request
                    # waits for a genuinely new WinRT frame. No cached pixels.
                    return None
                image = super().convert_dx_frame(frame)
                if image is not None:
                    self._last_timestamp = timestamp
                    self._evidence = (timestamp, threading.get_ident(), apartment)
                return image
            except Exception as error:
                self._callback_error = error
                self.frame_event.set()
                raise

        def read(self, request_qpc, timeout):
            from src.capture_geometry import read_capture_geometry
            if self.closed_requested.is_set() or self._callback_error is not None:
                raise CaptureError(f'WGC callback/session failed: {self._callback_error}')
            self._request_geometry = read_capture_geometry(target.hwnd)
            self._frame_mapping = None
            with self.lock:
                self._request_qpc = request_qpc
                self._evidence = None
                self._request_timeout = timeout
            image = self.get_frame()
            if read_capture_geometry(target.hwnd) != self._request_geometry:
                raise CaptureError('Game geometry changed during WGC acquisition; discard frame')
            if self._callback_error is not None:
                raise CaptureError(f'WGC callback failed: {self._callback_error}')
            if self.closed_requested.is_set():
                raise CaptureError('Game capture session closed during acquisition')
            if image is None or self._evidence is None:
                raise CaptureError('WGC capture timed out without a fresh frame')
            if self.capture_hwnd != target.hwnd:
                raise CaptureError('WGC capture HWND changed')
            timestamp, callback_thread, apartment = self._evidence
            return BackendFrame(image, timestamp, callback_thread, apartment,
                                self.capture_hwnd, target.pid, self._frame_mapping)

        def crop_image(self, frame):
            from src.capture_geometry import resolve_client_crop
            self._frame_mapping = resolve_client_crop(
                (frame.shape[1], frame.shape[0]), self._request_geometry)
            x, y, right, bottom = self._frame_mapping['client_crop']
            return frame[y:bottom, x:right]

        def _do_get_frame(self):
            # Reuse WGC's pool, callbacks, D3D copy and client crop. Narrow the
            # request boundary: no automatic restart and no BitBlt composition.
            if not self.start_or_stop():
                raise CaptureError('WGC session is unavailable')
            with self.lock:
                self.last_frame = None
                self.frame_event.clear()
                self.frame_requested.set()
            deadline = time.monotonic() + self._request_timeout
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CaptureError('WGC capture timed out without a fresh frame')
                    self.frame_event.wait(remaining)
                    with self.lock:
                        if self._callback_error is not None:
                            raise CaptureError(f'WGC callback failed: {self._callback_error}')
                        if self.closed_requested.is_set() or self.frame_pool is None:
                            raise CaptureError('WGC capture session closed during acquisition')
                        image, self.last_frame = self.last_frame, None
                        self.frame_event.clear()
                        if image is not None:
                            return self.crop_image(image)
            finally:
                with self.lock:
                    self.frame_requested.clear()
                    self.last_frame = None
                    self.frame_event.clear()

    left, top, right, bottom = win32gui.GetWindowRect(target.hwnd)
    x, y = win32gui.ClientToScreen(target.hwnd, (0, 0))
    width,height=target.client_size
    state = SimpleNamespace(hwnd=target.hwnd, x=x, y=y,
                            window_width=right-left, window_height=bottom-top,
                            client_width=width, client_height=height, width=width, height=height,
                            exists=True, hwnds=[], app_exit_event=threading.Event(),
                            capture_target_signature=(target.hwnd, width, height))
    return OwnedWGC(state)


class ClientCapture720p:
    """One WGC session, owned by one MTA worker; failures invalidate the session.

    Injectable factories are for offline tests. Production always uses WGC;
    initialization/read errors never switch backend or return earlier pixels.
    Rebuilding means creating a new instance after close(), with a new epoch.
    """

    def __init__(self, hwnd, pid, *, game_exe=GAME_EXE, backend_factory=None,
                 identity_reader=None, apartment_factory=None, qpc_clock=None, timeout=4.0,
                 client_size=CLIENT_SIZE):
        if (isinstance(hwnd, bool) or not isinstance(hwnd, Integral) or hwnd <= 0 or
                isinstance(pid, bool) or not isinstance(pid, Integral) or pid <= 0):
            raise CaptureError('Positive integer game HWND and PID are required')
        if not 0 < timeout <= 4.0:
            raise ValueError('Capture timeout must be positive and at most four seconds')
        if tuple(client_size) not in CLIENT_SIZES:
            raise CaptureError(f'Unsupported capture client size: {client_size}')
        self.client_size=tuple(client_size)
        self.hwnd, self.pid = int(hwnd), int(pid)
        self.game_exe = game_exe
        self.session_id = str(uuid.uuid4())
        self._factory = backend_factory or _make_wgc_backend
        self._identity_reader = identity_reader or (lambda hwnd:_read_identity(hwnd,self.client_size))
        self._apartment_factory = apartment_factory or _mta_apartment
        self._clock = qpc_clock or _qpc_ticks
        self._timeout = timeout
        self._commands = queue.Queue()
        self._lock = threading.Lock()
        self._fault = None
        self._cleanup_error = None
        self._closed = False
        self._ready = threading.Event()
        if backend_factory is None:
            try:
                _preload_bindings()
            except Exception as error:
                raise CaptureError(f'WGC bindings unavailable: {error}') from error
        self._thread = threading.Thread(target=self._run, name=f'WGC720p-{self.session_id[:8]}', daemon=True)
        self._thread.start()
        if not self._ready.wait(self._timeout + 2.0):
            self._closed = True
            self._commands.put(None)
            raise CaptureError('WGC owner thread initialization timed out')
        if self._fault is not None:
            self.close()
            raise CaptureError(str(self._fault)) from self._fault

    def _identity(self):
        identity = self._identity_reader(self.hwnd)
        if (identity.hwnd != self.hwnd or identity.pid != self.pid or
                identity.exe.casefold() != self.game_exe.casefold()):
            raise CaptureError(f'Game target identity mismatch: {identity}')
        if tuple(identity.client_size) != self.client_size:
            raise CaptureError(f'Expected game client {self.client_size}, got {identity.client_size}')
        return identity

    def _run(self):
        backend = None
        command = None
        try:
            with self._apartment_factory() as apartment:
                try:
                    if apartment != 1:
                        raise CaptureError(f'WGC owner must use MTA, got apartment={apartment}')
                    target = self._identity()
                    backend = self._factory(target)
                    self._ready.set()
                    sequence, last_timestamp = 0, None
                    while True:
                        try:
                            command = self._commands.get(timeout=0.1)
                        except queue.Empty:
                            closed = getattr(backend, 'closed_requested', None)
                            if closed is not None and closed.is_set():
                                raise CaptureError('Game capture session closed')
                            continue
                        if command is None:
                            break
                        self._identity()
                        request_qpc = self._clock()
                        if isinstance(request_qpc, bool) or not isinstance(request_qpc, Integral) or request_qpc <= 0:
                            raise CaptureError('Capture QPC timestamp must be a positive integer')
                        request_qpc = int(request_qpc)
                        frame = backend.read(request_qpc, self._timeout)
                        self._identity()
                        if frame.hwnd != self.hwnd or frame.pid != self.pid:
                            raise CaptureError('WGC returned a frame from another target')
                        if frame.callback_apartment != 1:
                            raise CaptureError('WGC callback apartment must be MTA')
                        if (isinstance(frame.system_relative_time, bool) or
                                not isinstance(frame.system_relative_time, Integral) or frame.system_relative_time <= 0):
                            raise CaptureError('WGC source timestamp must be a positive integer')
                        if (frame.system_relative_time < request_qpc or
                                (last_timestamp is not None and frame.system_relative_time <= last_timestamp)):
                            raise CaptureError('WGC returned an expired or repeated frame timestamp')
                        image = frame.image
                        if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or
                                image.shape != (self.client_size[1],self.client_size[0],3)):
                            raise CaptureError(f'Invalid WGC BGR client image: {getattr(image, "shape", None)}')
                        if not image.any():
                            raise CaptureError('WGC returned a black frame')
                        image = image.copy()
                        image.flags.writeable = False
                        sequence += 1
                        last_timestamp = int(frame.system_relative_time)
                        command.put(FrameEnvelope(image, self.hwnd, self.pid, 'WGC', 'BGR', sequence,
                                                  self.session_id, request_qpc, last_timestamp, time.time(),
                                                  threading.get_ident(), apartment, frame.callback_thread_id,
                                                  frame.callback_apartment, frame.mapping))
                        command = None
                finally:
                    if backend is not None:
                        previous_error = sys.exception()
                        try:
                            backend.close()
                        except Exception as error:
                            self._cleanup_error = CaptureError(f'WGC resource release failed: {error}')
                            detail = (f'{previous_error}; ' if previous_error is not None else '')
                            raise CaptureError(detail + str(self._cleanup_error)) from error
        except Exception as error:
            self._fault = error if isinstance(error, CaptureError) else CaptureError(f'WGC capture failed: {error}')
            if command is not None:
                command.put(self._fault)
        finally:
            self._ready.set()

    def get_frame(self):
        with self._lock:
            if self._closed:
                raise CaptureError('WGC capture is closed')
            if self._fault is not None:
                raise CaptureError(str(self._fault)) from self._fault
            response = queue.Queue(maxsize=1)
            self._commands.put(response)
            try:
                frame = response.get(timeout=self._timeout + 2.0)
            except queue.Empty as error:
                self._fault = CaptureError('WGC owner failed to return a bounded capture response')
                self._commands.put(None)
                raise self._fault from error
            if isinstance(frame, Exception):
                self._thread.join(timeout=self._timeout + 2.0)
                raise CaptureError(str(frame)) from frame
            return frame

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._commands.put(None)
        self._thread.join(timeout=self._timeout + 2.0)
        if self._thread.is_alive():
            raise CaptureError('WGC owner did not release resources within the bounded close deadline')
        if self._cleanup_error is not None:
            raise self._cleanup_error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


Capture720p = ClientCapture720p
