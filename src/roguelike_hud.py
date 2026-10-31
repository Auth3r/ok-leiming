"""Read-only, nonactivating roguelike HUD, independent of capture and runner."""
import os
from queue import Empty
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QApplication

from src.runtime_identity import get_runtime_identity

STATE_LABELS={'ENTRY':'开始挑战','START':'挑战首页','MAP':'地图','OUTPOST':'选队员',
    'CHOICE':'选择奖励','SHOP':'商店','BATTLE_PREP':'战斗准备','BATTLE':'战斗中',
    'UNKNOWN':'加载中','TRANSIENT':'加载中','OVERLAY':'等待奖励',
    'SETTLEMENT_CONFIRM':'结算','RESULT_STATS':'结算','RESULT_TEAM':'结算','RESULT':'结算',
    'ENCOUNTER':'遭遇战','SPECIAL_OPERATION':'特殊行动','SEARCH':'搜索','EVENT':'事件'}


class HudState:
    def __init__(self,version):
        self.version=version
        self.control='RUNNING';self.waiting=False
        self.index=1;self.target=1;self.completed=0
        self.page='开始挑战';self.detail='等待识别当前页面'

    def consume(self,event,data):
        if event=='roguelike_status':
            self.index=data.get('run_index',self.index)
            self.target=data.get('target_runs',self.target)
            self.completed=data.get('completed_runs',self.completed)
            state=str(data.get('state',''))
            self.page=STATE_LABELS.get(state,state)
            self.waiting=False
        elif event=='roguelike_input_intent':
            self.waiting=False
            self.detail=f"上次动作：{data.get('decision',data.get('action',''))} attempt {data.get('attempt',1)}"
        elif event in ('roguelike_wait','roguelike_battle_context'):
            self.waiting=True
            self.page=STATE_LABELS.get(str(data.get('state')),self.page)
            self.detail=f"等待{data['reason']}：{data['elapsed']:.1f} / {data['timeout']:g}s"
        elif event=='roguelike_action_timing':
            self.waiting=False
            state=str(data.get('next_state',''))
            self.page=STATE_LABELS.get(state,state)
        elif event=='roguelike_deployment':
            self.page='部署'
            if 'seconds' in data:
                self.waiting=True
                self.detail=f"{data['seconds']:g}秒后进行第二次部署"
            else:self.detail='部署：'+{'special card located':'已定位特殊队员','double click sent':'已发送部署',
                'special card absent; waiting for battle':'等待战斗'}.get(data.get('message'),data.get('message',''))

    def lines(self):
        status='WAITING' if self.control=='RUNNING' and self.waiting else self.control
        return (f'肉鸽 · {status}',f'第 {self.index}/{self.target} 轮 · 已完成 {self.completed}/{self.target}',
                '当前：'+self.page,self.detail,'F7 暂停/继续 · F8 停止',self.version)


class RoguelikeHud(QWidget):
    def __init__(self,parent=None,version=None):
        super().__init__(parent,Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|
                         Qt.WindowDoesNotAcceptFocus|Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.setWindowOpacity(.86)
        self.setFixedSize(420,110)  # Qt logical pixels; native size follows DPI.
        self.setStyleSheet('QWidget {background:#18212d;color:#f3f5f7;} QLabel {background:transparent;font-size:11px;}')
        layout=QVBoxLayout(self);layout.setContentsMargins(10,4,10,4);layout.setSpacing(1)
        self.labels=[QLabel(self) for _ in range(6)]
        for label in self.labels:
            label.setTextFormat(Qt.PlainText);layout.addWidget(label)
        self.labels[0].setStyleSheet('font-size:14px;font-weight:bold')
        self.labels[-1].setStyleSheet('font-size:9px;color:#b3c3d4')
        self.state=HudState(version or get_runtime_identity().display_version)
        self.native_safe=False
        QApplication.instance().aboutToQuit.connect(self.close)
        self.render()

    def render(self):
        for label,text in zip(self.labels,self.state.lines()):label.setText(text)

    def prepare_native(self):
        if os.name=='nt':
            import ctypes,win32con,win32gui
            hwnd=int(self.winId())
            style=win32gui.GetWindowLong(hwnd,win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(hwnd,win32con.GWL_EXSTYLE,
                style|win32con.WS_EX_NOACTIVATE|win32con.WS_EX_TOOLWINDOW|win32con.WS_EX_TRANSPARENT)
            # Defense in depth: HWND capture already excludes this independent
            # top-level window. Do not show if native capture exclusion fails.
            api=ctypes.windll.user32.SetWindowDisplayAffinity
            api.argtypes=[ctypes.c_void_p,ctypes.c_uint];api.restype=ctypes.c_bool
            if not api(hwnd,0x11):raise RuntimeError('HUD capture exclusion unavailable')
        self.native_safe=True

    def show_for(self,game_hwnd):
        if not game_hwnd:return
        if not self.native_safe:self.prepare_native()
        self.render()
        self.dock(game_hwnd)
        self.show()  # WA_ShowWithoutActivating + WS_EX_NOACTIVATE, never raise/focus.
        self.dock(game_hwnd)

    def dock(self,game_hwnd):
        if os.name!='nt':return
        import win32gui,win32con,win32api
        if not win32gui.IsWindow(game_hwnd) or win32gui.IsIconic(game_hwnd):
            self.hide();return
        monitor=win32api.MonitorFromWindow(game_hwnd,win32con.MONITOR_DEFAULTTONEAREST)
        left,top,right,bottom=win32api.GetMonitorInfo(monitor)['Work']
        scale=self.devicePixelRatioF()
        win32gui.SetWindowPos(int(self.winId()),win32con.HWND_TOPMOST,
            min(left+round(24*scale),right-round(self.width()*scale)),
            max(top,bottom-round((self.height()+8)*scale)),0,0,
            win32con.SWP_NOACTIVATE|win32con.SWP_NOSIZE)


class RoguelikeHudManager:
    def __init__(self,main_window):
        self.window=main_window;self.hud=RoguelikeHud(main_window)
        self.control=None;self.failed=False
        self.timer=QTimer(main_window);self.timer.timeout.connect(self.poll);self.timer.start(100)
        main_window.destroyed.connect(self.timer.stop)

    def poll(self):
        from ok import og
        from src.roguelike_ui import install_controls
        active=None
        for group in self.window.grouped_task_tabs:
            if group.group_name!='肉鸽':continue
            for card in group.card_widgets:
                install_controls(card);card.update_buttons()
                task=card.task;control=getattr(task,'roguelike_control',None)
                if control and control.state not in control.TERMINAL:active=task
        if active is None:
            self.hud.hide();self.control=None;return
        control=active.roguelike_control
        if self.control is not control:
            self.control=control;self.failed=False
            self.hud.state=HudState(get_runtime_identity().display_version)
        while True:
            try:event,data=active.roguelike_events.get_nowait()
            except Empty:break
            self.hud.state.consume(event,data)
        self.hud.state.control=control.state
        self.hud.render()
        plan=getattr(active,'run_plan',None)
        if not plan or not plan.get('显示肉鸽悬浮窗',True) or self.failed:
            self.hud.hide();return
        try:
            hwnd=og.device_manager.hwnd_window.hwnd
            import win32gui
            if not hwnd or not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
                self.hud.hide();return
            self.hud.show_for(hwnd)
        except Exception as error:
            self.failed=True;self.hud.hide()
            active.log_warning('肉鸽悬浮窗未显示：'+str(error))
