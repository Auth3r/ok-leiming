"""One fixed control bar for framework task cards and the native run panel."""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget, QHBoxLayout, QSizePolicy
from qfluentwidgets import FluentIcon, PrimaryPushButton, PushButton
from ok.core.events import communicate
from ok.ui.qt.common.OKIcon import OKIcon
from shiboken6 import isValid


def control_for(task):
    return getattr(task, 'task_control', None) or getattr(task, 'roguelike_control', None)


def task_state(task):
    control = control_for(task)
    if control:return control.state
    if getattr(task, 'enabled', False):
        return 'PAUSED' if task.paused else ('RUNNING' if task.running else 'QUEUED')
    return 'IDLE'


def control_status(task):
    control=control_for(task)
    state=task_state(task)
    if state=='PAUSED':
        labels={'user':'用户暂停','task':'用户暂停','step':'单步调试暂停',
                'safety':'安全暂停','executor':'全局暂停'}
        return '、'.join(labels.get(source,source) for source in sorted(control.pauses)) if control else '已暂停'
    return {'RUNNING':'运行中','QUEUED':'等待运行','IDLE':'待机','SUCCESS':'已完成',
            'PARTIAL':'部分完成 PARTIAL','STOPPED':'已停止','CANCELLED':'已停止','ERROR':'错误停止'}.get(state,state)


class TaskControlBar(QWidget):
    """Pause/continue share the left widget; stop never moves or disappears."""
    def __init__(self, parent=None, *, start, toggle, stop):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.primary_button = PrimaryPushButton(FluentIcon.PLAY, '开始', self)
        self.stop_button = PushButton(OKIcon.STOP, '停止', self)
        for button in (self.primary_button, self.stop_button):
            button.setFixedSize(92, 32)
            font = button.font(); font.setPointSize(10); button.setFont(font)
        self.stop_button.setToolTip('停止当前任务（F8）；停止后须从任务入口重新启动')
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(8)
        self.row.addWidget(self.primary_button)
        self.row.addWidget(self.stop_button)
        self.setFixedSize(192, 32)
        self.state = 'IDLE'
        self._start, self._toggle, self._stop = start, toggle, stop
        self.primary_button.clicked.connect(self.activate)
        self.stop_button.clicked.connect(self.stop)
        self.render('IDLE')

    def activate(self):
        if self.state in ('RUNNING', 'PAUSED'):
            self._toggle()
        elif self.state != 'QUEUED':
            self._start()

    def stop(self):
        if self.state in ('RUNNING', 'PAUSED', 'QUEUED'):
            self._stop()

    def render(self, state, *, start_enabled=True, reason=''):
        self.state = state
        active = state in ('RUNNING', 'PAUSED')
        self.primary_button.setText('继续' if state == 'PAUSED' else ('暂停' if active else ('等待' if state == 'QUEUED' else '开始')))
        self.primary_button.setIcon(FluentIcon.PAUSE if state == 'RUNNING' else FluentIcon.PLAY)
        self.primary_button.setEnabled(active or (start_enabled and state != 'QUEUED'))
        self.stop_button.setEnabled(active or state == 'QUEUED')
        self.primary_button.setToolTip(reason or ('暂停/继续当前任务（F7）' if active else '从入口开始新任务'))


def toggle_task(task):
    toggle = getattr(task, 'toggle_pause', None)
    if toggle:
        toggle()
    elif task.enabled:
        task.unpause() if task.paused else task.pause()


def install_controls(card):
    """Adapt the supported task card once, without changing framework files."""
    if not getattr(card, 'onetime', True) or getattr(card, 'unified_controls', False):
        return
    card.unified_controls = True
    communicate.task.disconnect(card.update_buttons)
    for name in ('pause_button', 'stop_button', 'start_button'):
        button = getattr(card, name, None)
        if button is not None:
            button.hide()
            try: button.clicked.disconnect()
            except RuntimeError: pass
    starter = getattr(card, 'start_clicked', lambda: None)
    stopper = getattr(card.task, 'terminal_stop', None)
    bar = TaskControlBar(getattr(card, 'button_container', None), start=starter,
                         toggle=lambda: toggle_task(card.task),
                         stop=(lambda: stopper('STOPPED', source='GUI 停止')) if stopper else getattr(card.task, 'disable', lambda: None))
    card.control_bar = bar
    card.pause_button = bar.primary_button
    card.stop_button = bar.stop_button
    card.start_button = bar.primary_button
    while card.button_layout.count(): card.button_layout.takeAt(0)
    card.button_layout.addWidget(bar)
    for name in ('waiting_label', 'instructions_button'):
        widget = getattr(card, name, None)
        if widget is not None: widget.hide()
    # Text may shrink, the controls cannot. Tooltips retain full descriptions.
    header = getattr(card, 'card', None)
    if header is not None:
        # ConfigCard appends its settings arrow after the controls; cards with
        # and without settings then have different right edges. Move that
        # existing, connected arrow into the leading side of the fixed row,
        # and give every compact header the same trailing inset.
        arrow=getattr(header,'expandButton',None)
        has_settings=arrow is not None and not arrow.isHidden()
        while header.hBoxLayout.count():header.hBoxLayout.takeAt(0)
        header.hBoxLayout.setContentsMargins(16,0,16,0)
        header.hBoxLayout.setSpacing(8)
        header.hBoxLayout.addLayout(header.vBoxLayout,1)
        header.hBoxLayout.addWidget(card.button_container,0)
        if has_settings:
            arrow.setToolTip('展开/收起任务设置')
            card.button_layout.insertWidget(0,arrow)
        card.settings_button=arrow
        for name in ('titleLabel', 'contentLabel'):
            label = getattr(header, name, None)
            if label is not None:
                label.setMinimumWidth(0)
                label.setSizePolicy(QSizePolicy.Preferred if name=='titleLabel' else QSizePolicy.Ignored, QSizePolicy.Fixed)
                if name=='titleLabel':label.setMaximumWidth(240)
                if name=='contentLabel':label.setVisible(bool(card.task.description))
                label.setToolTip(card.task.name if name == 'titleLabel' else card.task.description)

    def update(task=None):
        if not isValid(bar):
            communicate.task.disconnect(update)
            return
        control = control_for(card.task)
        pauses = sorted(control.pauses) if control else []
        labels = {'user': '用户暂停', 'task': '用户暂停', 'step': '单步调试暂停',
                  'safety': '安全暂停', 'executor': '全局暂停'}
        bar.render(task_state(card.task), start_enabled=not getattr(card.task,'running',False),
                   reason='、'.join(labels.get(source, source) for source in pauses))
    card.update_buttons = update
    communicate.task.connect(update)
    timer = QTimer(bar); timer.timeout.connect(update); timer.start(100)
    card.control_timer = timer
    bar.destroyed.connect(lambda: communicate.task.disconnect(update))
    update()
