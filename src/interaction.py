"""Game inputs use client physical pixels and live Win32 geometry at dispatch."""
import time
import win32gui
import win32con
import win32api
from src.input_receipt import InputReceipt
from src.client_geometry import (
    ClientGeometryError, client_input_to_screen, screen_to_client,
    validate_client_geometry,
)
from ok.device.interaction_methods.post_message import PostMessageInteraction
from ok.device.interaction_methods.pynput import PynputInteraction


def _send_mouse_input(action, key='left', amount=0):
    """Use pynput's ABI/types but retain the actual Win32 SendInput count."""
    import ctypes
    from pynput import mouse
    from pynput._util import win32 as native
    if action == 'scroll':
        flags, data = native.MOUSEINPUT.WHEEL, int(amount * 120)
    else:
        button = getattr(mouse.Button, key)
        flags, data = button.value[1 if action == 'press' else 0], button.value[2]
    packet = native.INPUT(type=native.INPUT.MOUSE, value=native.INPUT_union(
        mi=native.MOUSEINPUT(dwFlags=flags, mouseData=data)))
    return native.SendInput(1, ctypes.byref(packet), ctypes.sizeof(native.INPUT))


def _dispatch_context(target, client_point=None, screen=None):
    """Read at dispatch; missing diagnostic fields are explicit, never invented."""
    import ctypes
    import win32process
    result = {'monotonic_ns': time.perf_counter_ns(), 'clock':'perf_counter_ns','target_hwnd': target,
              'target_pid': None, 'foreground_hwnd': None, 'hit_hwnd': None,
              'foreground_pid': None, 'hit_pid': None,
              'target_root': None, 'foreground_root': None, 'hit_root': None,
              'actual_cursor': None, 'screen': screen, 'client_point': client_point,
              'client_size': None, 'client_origin': None, 'dpi': None}
    errors = {}
    readers = {
        'target_pid': lambda: win32process.GetWindowThreadProcessId(target)[1],
        'foreground_hwnd': lambda: win32gui.GetForegroundWindow(),
        'actual_cursor': lambda: win32api.GetCursorPos(),
        'client_size': lambda: tuple(win32gui.GetClientRect(target)[2:]),
        'dpi': lambda: ctypes.windll.user32.GetDpiForWindow(target),
    }
    for name, read in readers.items():
        try: result[name] = read()
        except Exception as error: errors[name] = str(error)
    if screen is not None and client_point is not None:
        result['client_origin'] = tuple(a-b for a,b in zip(screen,client_point))
    try:
        if result['actual_cursor'] is not None:
            result['hit_hwnd'] = win32gui.WindowFromPoint(result['actual_cursor'])
    except Exception as error: errors['hit_hwnd'] = str(error)
    for name in ('foreground', 'hit'):
        try:
            if result[name+'_hwnd']:
                result[name+'_pid'] = win32process.GetWindowThreadProcessId(result[name+'_hwnd'])[1]
        except Exception as error: errors[name+'_pid'] = str(error)
    for name in ('target','foreground','hit'):
        try:
            if result[name+'_hwnd']:
                result[name+'_root']=win32gui.GetAncestor(result[name+'_hwnd'],win32con.GA_ROOT)
        except Exception as error: errors[name+'_root']=str(error)
    if errors: result['diagnostic_errors'] = errors
    return result


class GameForegroundInteraction(PynputInteraction):
    def _new_receipt(self, action, x, y, requested, screen=None, **parameters):
        receipt = InputReceipt(action, self.hwnd_window.hwnd, (x, y), requested)
        receipt.initial_context = _dispatch_context(receipt.target_hwnd,(x,y),screen)
        receipt.target_pid = receipt.initial_context.get('target_pid')
        receipt.parameters = parameters
        self.last_input_receipt = receipt
        return receipt

    def _native_dispatch(self, action, key='left', amount=0, context=None, cleanup=False):
        stage = (context or _dispatch_context(self.hwnd_window.hwnd)) | {
            'kind': action, 'requested': 1, 'sent': None, 'cleanup': cleanup,
            'key': key, 'amount': amount, 'transport': 'SendInput'}
        receipt = getattr(self, '_active_input_receipt', None)
        if receipt is not None:
            receipt.stages.append(stage)
        try:
            stage['sent'] = _send_mouse_input(action, key, amount)
            stage['finished_monotonic_ns'] = time.perf_counter_ns()
            if type(stage['sent']) is not int or stage['sent'] not in (0, 1):
                raise RuntimeError(f'SendInput returned an unknown count: {stage["sent"]!r}')
            return stage['sent'] == 1
        except Exception as error:
            stage['error'] = str(error)
            stage['finished_monotonic_ns'] = time.perf_counter_ns()
            if receipt is not None: receipt.finish('unknown', 'native_dispatch_unknown')
            raise

    def _mouse_dispatch(self, x, y, action='move', key='left', amount=0):
        # Never use capture.get_abs_cords(): its origin refreshes asynchronously.
        target = self.hwnd_window.hwnd
        receipt = getattr(self,'_active_input_receipt',None)
        if receipt is not None and target != receipt.target_hwnd:
            raise ClientGeometryError('Game HWND changed before native dispatch')
        screen = client_input_to_screen(target, x, y)
        if not self.clickable():
            return False
        from pynput import mouse
        controller = mouse.Controller()
        controller.position = screen
        if controller.position != screen:
            return False
        if action != 'move':
            context = _dispatch_context(target, (x,y), screen)
            # Capture diagnostics first, then re-check focus at the native boundary.
            if self.hwnd_window.hwnd != target:
                raise ClientGeometryError('Game HWND changed before native dispatch')
            target_pid=receipt.target_pid if receipt is not None else context['target_pid']
            identity_ok=(type(target_pid) is int and target_pid>0 and context['target_pid']==target_pid
                and context['foreground_pid']==target_pid and context['hit_pid']==target_pid
                and type(context['target_root']) is int and context['target_root']>0
                and context['foreground_root']==context['hit_root']==context['target_root'])
            if not self.clickable() or context['actual_cursor'] != screen or not identity_ok:
                if receipt is not None:
                    receipt.stages.append(context | {'kind':action,'requested':1,'sent':0,
                        'cleanup':False,'reason':'window_identity_focus_or_cursor_changed_before_dispatch'})
                return False
            return self._native_dispatch(action, key, amount, context)
        return True

    def move(self, x, y):
        return self._mouse_dispatch(x, y)

    def click(self, x=-1, y=-1, move_back=False, name=None, down_time=0.01, move=False, key='left'):
        # Even the old (-1,-1) "click wherever the cursor is" default is refused.
        target = self.hwnd_window.hwnd
        screen=client_input_to_screen(target, x, y)
        receipt = self._new_receipt('click', x, y, 2,screen,down_time=down_time,key=key)
        if not self.clickable(): return receipt.finish('not_sent', 'not_clickable')
        from pynput import mouse
        previous = mouse.Controller().position if move_back else None
        held=False
        self._active_input_receipt=receipt
        try:
            if not self.move(x, y): return receipt.finish('not_sent', 'move_refused')
            time.sleep(0.02)
            if self.hwnd_window.hwnd != target:
                raise ClientGeometryError('Game HWND changed before click press')
            if not self._mouse_dispatch(x, y, 'press', key):
                return receipt.finish(reason='press_not_sent')
            held=True
            time.sleep(down_time)
            if self.hwnd_window.hwnd != target:
                raise ClientGeometryError('Game HWND changed during click')
            if self._mouse_dispatch(x, y, 'release', key):held=False
            else: receipt.reason='release_not_sent'
        except BaseException as error:
            # An exception in the native press has an unknown outcome; release
            # in place without another move/press or activation.
            if any(stage['kind']=='press' and (type(stage['sent']) is not int or stage['sent'] not in (0,1))
                   for stage in receipt.stages):held=True
            receipt.finish('unknown' if receipt.status=='unknown' else ('partial' if held else 'not_sent'), str(error))
            error.input_receipt=receipt
            raise
        finally:
            try:
                if held:
                    self._native_dispatch('release', key, context=_dispatch_context(target), cleanup=True)
            except BaseException as cleanup_error:
                receipt.finish('unknown', 'owned_button_release_unknown')
                cleanup_error.input_receipt=receipt
                raise
            finally:self._active_input_receipt=None
        if previous is not None and not held and self.hwnd_window.hwnd == target and self.clickable():
            # Desktop cursor restoration is a screen-space operation, not a game point.
            mouse.Controller().position = previous
        return receipt.finish(reason=receipt.reason)

    def mouse_down(self, x=-1, y=-1, name=None, key='left'):
        return self._mouse_dispatch(x, y, 'press', key)

    def mouse_up(self, key='left'):
        # This API has no point: read the actual cursor, refuse if outside the client.
        validate_client_geometry(self.hwnd_window.hwnd)
        from pynput import mouse
        point = screen_to_client(self.hwnd_window.hwnd, *mouse.Controller().position)
        return self._mouse_dispatch(*point, 'release', key)

    def scroll(self, x, y, scroll_amount):
        screen=client_input_to_screen(self.hwnd_window.hwnd, x, y)
        receipt=self._new_receipt('scroll',x,y,abs(scroll_amount),screen,scroll_amount=scroll_amount)
        self._active_input_receipt=receipt
        sign = (scroll_amount > 0) - (scroll_amount < 0)
        try:
            for _ in range(abs(scroll_amount)):
                if self.hwnd_window.hwnd != receipt.target_hwnd:
                    raise ClientGeometryError('Game HWND changed during scroll')
                if not self._mouse_dispatch(x, y, 'scroll', amount=sign):
                    return receipt.finish(reason='wheel_not_sent')
                time.sleep(0.001)
            time.sleep(0.02)
            return receipt.finish()
        except BaseException as error:
            receipt.finish('unknown' if receipt.status=='unknown' else None, str(error))
            error.input_receipt=receipt
            raise
        finally:self._active_input_receipt=None

    def swipe(self, x1, y1, x2, y2, duration, settle_time=0):
        # Validate BOTH endpoints before the first dispatch, interpolate in client pixels.
        target = self.hwnd_window.hwnd
        def check_target():
            if self.hwnd_window.hwnd != target:
                raise ClientGeometryError('Game HWND changed during drag')
        client_input_to_screen(target, x1, y1)
        client_input_to_screen(target, x2, y2)
        check_target()
        if not self.move(x1, y1):
            return
        time.sleep(0.1)
        check_target()
        if not self._mouse_dispatch(x1, y1, 'press'):
            return
        held=True
        try:
            steps = max(1, int(duration / 100))
            for i in range(steps):
                check_target()
                if not self.move(x1 + int(i * (x2-x1) / steps), y1 + int(i * (y2-y1) / steps)):
                    return
                time.sleep(0.01)
            check_target()
            if self._mouse_dispatch(x2, y2, 'release'):held=False
        finally:
            if held:
                # Release only this gesture's accepted press. Focus/geometry
                # may now be invalid: never move or activate another window.
                from pynput import mouse
                self._native_dispatch('release','left',cleanup=True)

    def send_key_down(self, key):
        validate_client_geometry(self.hwnd_window.hwnd)
        return super().send_key_down(key)

    def send_key_up(self, key):
        validate_client_geometry(self.hwnd_window.hwnd)
        return super().send_key_up(key)

    def send_key(self, key, down_time=0.01):
        self.send_key_down(key)
        time.sleep(down_time)
        self.send_key_up(key)

    def should_capture(self):
        # WGC can capture behind other apps. Do not block the executor before run().
        return True

    def activate_for_task(self):
        """Activate at daily task start; opening retries manage their own focus lease."""
        activate_window(self.hwnd_window.hwnd)


class ForegroundUnavailable(RuntimeError):
    """Activation failed without issuing game input."""


def activate_window(target):
    import win32api
    import win32process
    if win32gui.GetForegroundWindow() == target:
        return
    errors=[]
    def attempt():
        try:
            if win32gui.IsIconic(target):
                win32gui.ShowWindow(target, win32con.SW_RESTORE)
            win32gui.BringWindowToTop(target)
            win32gui.SetForegroundWindow(target)
        except Exception as error:
            errors.append(str(error))
        return win32gui.GetForegroundWindow()==target

    # Try the normal activation path before attaching input queues. Attachment
    # can be denied even when normal activation or a manual switch works.
    if attempt():return
    attached = []
    current = win32api.GetCurrentThreadId()
    try:
        # Share input queues temporarily so the worker can activate the
        # selected game even when the Run button belongs to the GUI thread.
        foreground = win32gui.GetForegroundWindow()
        for window in (foreground, target):
            if not window:
                continue
            try:
                thread, _ = win32process.GetWindowThreadProcessId(window)
                if thread != current and thread not in attached:
                    win32process.AttachThreadInput(current, thread, True)
                    attached.append(thread)
            except Exception as error:
                errors.append(str(error))
        attempt()
    finally:
        for thread in reversed(attached):
            try:
                win32process.AttachThreadInput(current, thread, False)
            except Exception as error:
                errors.append(str(error))
    if win32gui.GetForegroundWindow() != target:
        raise ForegroundUnavailable('Windows 未允许切换前台，尚未点击；'+ '; '.join(errors))

class GameBackgroundInteraction(PostMessageInteraction):
    _new_receipt = GameForegroundInteraction._new_receipt

    def _message_submission(self, receipt, kind, message, wparam=0, lparam=0, client_point=None):
        point=client_point or receipt.client_point
        screen=client_input_to_screen(receipt.target_hwnd,*point)
        stage=_dispatch_context(receipt.target_hwnd,point,screen) | {
            'kind':kind,'requested':1,'sent':None,'cleanup':False,'transport':'PostMessage'}
        receipt.stages.append(stage)
        try:
            if stage['target_pid'] is None or stage['target_pid']!=receipt.target_pid:
                stage.update(sent=0,reason='target_pid_changed_before_message_dispatch')
                return False
            result=self.post(message,wparam,lparam,hwnd=receipt.target_hwnd,client_point=client_point)
            # pywin32 PostMessage raises on refusal and returns None on accepted queue submission.
            stage['sent']=0 if result is False else 1
            stage['finished_monotonic_ns']=time.perf_counter_ns()
            return result is not False
        except BaseException as error:
            stage['error']=str(error)
            receipt.finish('unknown',str(error))
            error.input_receipt=receipt
            raise

    @property
    def hwnd(self):
        # The existing HWND provider refreshes game discovery; never use cached child rects.
        return self.hwnd_window.hwnd

    def update_mouse_pos(self, x, y, activate=True):
        target = self.hwnd
        screen = client_input_to_screen(target, x, y)
        local = screen_to_client(target, *screen)
        self._client_mouse_point = (x, y)
        if activate:
            self.try_activate()
        return win32api.MAKELONG(*local)

    def mouse_up(self, key='left'):
        point = getattr(self, '_client_mouse_point', None)
        if point is None:
            raise ClientGeometryError('Mouse release requires a valid client point')
        position = self.update_mouse_pos(*point, activate=False)
        action = {'left': win32con.WM_LBUTTONUP, 'middle': win32con.WM_MBUTTONUP,
                  'right': win32con.WM_RBUTTONUP}[key]
        self.post(action, 0, position)

    def _release_owned_button(self, target, action, point):
        # This gesture already validated/pressed this target. Cleanup must not
        # activate it, re-read a replacement HWND, or move the desktop cursor.
        if win32gui.IsWindow(target):
            try:win32gui.PostMessage(target,action,0,win32api.MAKELONG(*point))
            except Exception as error:
                raise RuntimeError(f'后台鼠标释放失败：{error}') from error

    def click(self, x=-1, y=-1, move_back=False, name=None, down_time=0.01, move=True, key='left'):
        target=self.hwnd
        screen=client_input_to_screen(target,x,y)
        receipt=self._new_receipt('click',x,y,2,screen,down_time=down_time,key=key)
        receipt.transport='PostMessage'
        if move:
            position=self.move(x,y)
            if position is False:return receipt.finish('not_sent','move_refused')
            time.sleep(down_time)
        else:position=self.update_mouse_pos(x,y,activate=True)
        if self.hwnd != target:
            raise ClientGeometryError('Game HWND changed before click press')
        down,flag,up={
            'left':(win32con.WM_LBUTTONDOWN,win32con.MK_LBUTTON,win32con.WM_LBUTTONUP),
            'middle':(win32con.WM_MBUTTONDOWN,win32con.MK_MBUTTON,win32con.WM_MBUTTONUP),
        }.get(key,(win32con.WM_RBUTTONDOWN,win32con.MK_RBUTTON,win32con.WM_RBUTTONUP))
        if not self._message_submission(receipt,'press',down,flag,position):
            return receipt.finish(reason='press_not_sent')
        held=True
        try:
            time.sleep(down_time)
            if self.hwnd != target:
                raise ClientGeometryError('Game HWND changed during click')
            if self._message_submission(receipt,'release',up,0,position):held=False
            else:receipt.reason='release_not_sent'
        except BaseException as error:
            receipt.finish('unknown' if receipt.status=='unknown' else 'partial',str(error))
            error.input_receipt=receipt
            raise
        finally:
            if held:
                stage=_dispatch_context(target) | {'kind':'release','requested':1,'sent':None,
                    'cleanup':True,'transport':'PostMessage'}
                receipt.stages.append(stage)
                try:
                    self._release_owned_button(target,up,(x,y))
                    stage['sent']=1 if win32gui.IsWindow(target) else 0
                except BaseException as error:
                    receipt.finish('unknown','owned_button_release_unknown')
                    error.input_receipt=receipt
                    raise
        return receipt.finish(reason=receipt.reason)

    def right_click(self, x=-1, y=-1, move_back=False, name=None):
        return self.click(x, y, move_back=move_back, name=name, key='right')

    def swipe(self, x1, y1, x2, y2, duration=3, settle_time=0):
        target=self.hwnd
        def check_target():
            if self.hwnd != target:
                raise ClientGeometryError('Game HWND changed during drag')
        client_input_to_screen(target, x1, y1)
        client_input_to_screen(target, x2, y2)
        check_target()
        if self.move(x1, y1) is False:return
        time.sleep(0.1)
        check_target()
        if self.mouse_down(x1, y1) is False:return
        last_point=(x1,y1)
        try:
            steps = max(1, int(duration / 100))
            for i in range(steps):
                point=(x1 + int(i * (x2-x1) / steps), y1 + int(i * (y2-y1) / steps))
                check_target()
                if self.move(*point,down_btn=win32con.MK_LBUTTON) is False:return
                last_point=point
                time.sleep(0.01)
            check_target()
            if self.move(x2, y2, down_btn=win32con.MK_LBUTTON) is False:return
            last_point=(x2,y2)
        finally:
            # The last point was already validated at a successful dispatch.
            # Cleanup must reach the originally pressed window even if its
            # geometry/provider changes, with no activation or cursor input.
            self._release_owned_button(target,win32con.WM_LBUTTONUP,last_point)

    def scroll(self, x, y, scroll_amount):
        screen=client_input_to_screen(self.hwnd, x, y)
        receipt=self._new_receipt('scroll',x,y,1,screen,scroll_amount=scroll_amount)
        receipt.transport='PostMessage'
        self.try_activate()
        self._message_submission(receipt,'scroll',win32con.WM_MOUSEWHEEL,
                  win32api.MAKELONG(0, win32con.WHEEL_DELTA * scroll_amount),
                  client_point=(x, y))
        return receipt.finish()

    def post(self, message, wParam=0, lParam=0, hwnd=None, client_point=None):
        # Upstream swallows access-denied errors; fail immediately instead.
        target = self.hwnd if hwnd is None else hwnd
        if target != self.hwnd:
            raise ClientGeometryError('Game HWND changed before message dispatch')
        validate_client_geometry(target)
        mouse_messages = (win32con.WM_MOUSEMOVE, win32con.WM_LBUTTONDOWN,
                          win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK,
                          win32con.WM_RBUTTONDOWN, win32con.WM_RBUTTONUP,
                          win32con.WM_RBUTTONDBLCLK, win32con.WM_MBUTTONDOWN,
                          win32con.WM_MBUTTONUP, win32con.WM_MBUTTONDBLCLK)
        if message in mouse_messages or message == win32con.WM_MOUSEWHEEL:
            # LPARAM is signed 16-bit x/y by the Windows message ABI, not a handle.
            x = (lParam & 0xffff) - (0x10000 if lParam & 0x8000 else 0)
            y = ((lParam >> 16) & 0xffff) - (0x10000 if lParam & 0x80000000 else 0)
            if client_point is not None:
                x, y = client_point
            elif message == win32con.WM_MOUSEWHEEL:
                x, y = screen_to_client(target, x, y)
            screen = client_input_to_screen(target, x, y)
            point = screen if message == win32con.WM_MOUSEWHEEL else screen_to_client(target, *screen)
            lParam = win32api.MAKELONG(*point)
        try:
            win32gui.PostMessage(target, message, wParam, lParam)
        except Exception as error:
            raise RuntimeError(f'后台消息发送失败，请确认助手与游戏权限一致：{error}') from error

    def try_activate(self):
        # Send notifications only. Never SetForegroundWindow / SetFocus / move cursor.
        target = self.hwnd_window.hwnd
        self.post(win32con.WM_ACTIVATEAPP, 1, 0, hwnd=target)
        self.post(win32con.WM_NCACTIVATE, 1, 0, hwnd=target)
        self.post(win32con.WM_ACTIVATE, win32con.WA_ACTIVE, 0, hwnd=target)
        self.post(win32con.WM_SETFOCUS, 0, 0, hwnd=target)
