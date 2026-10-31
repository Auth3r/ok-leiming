"""Extend the upstream start page through its supported my_app hook."""
import time
from threading import Thread
import cv2
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMessageBox
from qfluentwidgets import PrimaryPushButton, PushButton, FluentIcon
from ok.ui.qt.widget.Card import Card
from src.settings import ROOT, load_settings
from src.diagnostics import collect_status, create_report, running_plan
from src.runtime_identity import get_runtime_identity


class DiagnosticsPanel(QWidget):
    sampled = Signal(object, object)
    exported = Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.status = {'message': '正在检测游戏连接…'}
        self.preview_bytes = None
        self.updated = 0
        self.busy = False
        self.started = 0
        identity = get_runtime_identity()
        self.version_label = QLabel(identity.display_version)
        self.version_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.version_label.setToolTip(f'{identity.git_branch or "detached / unknown"}\n{identity.git_commit or identity.git_error}\n{identity.app_root}')
        self.label = QLabel(self.status['message'])
        self.label.setWordWrap(True)
        self.preview = QLabel('等待游戏截图')
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(320, 180)
        self.preview.setMaximumHeight(260)
        self.detail = QLabel('连接游戏窗口不代表已通过主界面识别；运行任务前仍会执行安全检查。')
        self.detail.setWordWrap(True)
        self.report_button = PrimaryPushButton(FluentIcon.FEEDBACK, '生成问题报告')
        self.report_button.clicked.connect(self.export_report)
        self.folder_button = PushButton(FluentIcon.FOLDER, '报告目录')
        self.folder_button.clicked.connect(self.open_reports)
        help_text = QLabel('遇到问题：点击生成问题报告，再把生成的 ZIP 文件拖进聊天。\n包含连接诊断、最近日志和游戏截图，不会自动上传。')
        help_text.setWordWrap(True)
        left = QWidget(); left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.preview)
        right = QWidget(); right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.version_label)
        right_layout.addWidget(self.label)
        right_layout.addWidget(self.detail)
        buttons = QHBoxLayout(); buttons.addWidget(self.report_button); buttons.addWidget(self.folder_button)
        right_layout.addLayout(buttons); right_layout.addWidget(help_text)
        layout = QHBoxLayout(self); layout.addWidget(left, 1); layout.addWidget(right, 1)
        self.sampled.connect(self.receive_sample)
        self.exported.connect(self.receive_report)
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(2000)
        QTimer.singleShot(0, self.poll)

    def poll(self):
        if self.busy:
            if time.monotonic()-self.started > 8:
                self.preview.clear(); self.preview.setText('截图检测超时')
                self.preview_bytes = None
                self.status = {'message': '截图检测超时，请刷新或切换截图方式'}
                self.label.setText(self.status['message'])
            return
        self.busy = True; self.started = time.monotonic()
        Thread(target=self.sample, daemon=True).start()

    def sample(self):
        try:
            from ok import og
            status, frame = collect_status(og.device_manager, og.executor,
                                           running_plan(og.executor) or load_settings())
            # No input, task start, foreground switch, or custom capture backend.
            preview = None
            if frame is not None:
                frame = cv2.resize(frame, (640, 360))
                preview = cv2.imencode('.jpg', frame)[1].tobytes()
            self.sampled.emit(status, preview)
        except Exception as error:
            self.sampled.emit({'message': f'连接检测失败：{error}'}, None)

    def receive_sample(self, status, preview):
        self.busy = False; self.updated = time.monotonic()
        self.status = status; self.preview_bytes = preview
        self.label.setText(status['message'])
        self.detail.setText(f'{status.get("time", "")}\n截图方式：{status.get("capture_method") or "未选择"}\n'
                            '前台模式：执行任务时自动切到游戏，识别任务所需页面后点击。\n日常默认每模块临时接管并归还窗口；抢开服每轮切前台。真正后台输入暂不支持。')
        if preview:
            image = QImage.fromData(preview)
            self.preview.setPixmap(QPixmap.fromImage(image).scaled(min(460, self.preview.width()), 259, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.preview.clear(); self.preview.setText('暂无可用游戏截图')

    def open_reports(self):
        from ok.util.explorer import open_explorer_folder
        directory = ROOT/'reports'; directory.mkdir(exist_ok=True)
        open_explorer_folder(directory)

    def export_report(self):
        self.report_button.setEnabled(False); self.report_button.setText('正在生成…')
        status = dict(self.status)
        status['sample_age_seconds'] = round(time.monotonic()-self.updated, 1) if self.updated else None
        preview = self.preview_bytes if self.updated and time.monotonic()-self.updated < 8 else None
        def work():
            try:
                from ok import og
                result = create_report(ROOT, status,
                                       running_plan(og.executor) or load_settings(), preview)
                self.exported.emit(result, '')
            except Exception as error:
                self.exported.emit(None, str(error))
        Thread(target=work, daemon=True).start()

    def receive_report(self, path, error):
        self.report_button.setEnabled(True); self.report_button.setText('生成问题报告')
        if error:
            QMessageBox.warning(self, '报告生成失败', error)
            return
        from ok.util.explorer import reveal_in_explorer
        try: reveal_in_explorer(path)
        except Exception: pass
        QMessageBox.information(self, '问题报告已生成', f'请把这个 ZIP 文件拖进当前聊天：\n\n{path}\n\n无需逐个查找日志。')


class DiagnosticsApp:
    def __init__(self, exit_event):
        self.exit_event = exit_event

    def on_show_main_window(self, main_window):
        from src.update_api import install_update_api
        install_update_api(main_window)
        identity = get_runtime_identity()
        # Title stays visible on every task tab and in human acceptance screenshots.
        main_window.setWindowTitle(f'{main_window.config["gui_title"]} · {identity.display_version}')
        from ok.ui.qt.tasks.TaskCard import TaskCard
        from src.task_controls import install_controls
        for task_card in main_window.findChildren(TaskCard):
            install_controls(task_card)
        from src.roguelike_hud import RoguelikeHudManager
        if not hasattr(self,'roguelike_hud'):
            self.roguelike_hud=RoguelikeHudManager(main_window)
        from src.task_hud import TaskHudManager
        if not hasattr(self,'task_hud'):
            self.task_hud=TaskHudManager(main_window)
        main_window.task_hud_manager=self.task_hud
        for group in main_window.grouped_task_tabs:
            if group.group_name=='肉鸽' and not hasattr(group,'runtime_720p_panel'):
                from src.roguelike_runtime_ui import Runtime720pPanel
                from ok import og
                group.runtime_720p_panel=Runtime720pPanel(group,options=lambda:og.executor.global_config.get_config('Roguelike'))
                group.runtime_720p_panel.mode.setCurrentIndex(1)
                group.vBoxLayout.insertWidget(0,Card('肉鸽运行模式',group.runtime_720p_panel))
                main_window.start_tab.runtime_720p_panel=group.runtime_720p_panel
                from ok.ui.qt.tasks.TaskCard import TaskCard
                for card in group.findChildren(TaskCard):
                    card.card.titleLabel.setText(card.task.name+'（旧 4K）')
                    card.start_button.setToolTip('仅旧 4K 模式；720P 请使用上方运行面板的开始按钮')
                    group.runtime_720p_panel.legacy_cards.append(card)
                group.runtime_720p_panel.render()
                main_window.switchTo(group)
        from src.module_ui import ModuleStrategyPanel
        for group in main_window.grouped_task_tabs:
            if group.group_name=='模组制作' and not hasattr(group,'module_strategy_panel'):
                group.module_strategy_panel=ModuleStrategyPanel(next(t for t in group.tasks if t.only=='MODULE_CRAFT'),group)
                group.vBoxLayout.insertWidget(0,Card('模组制作设置',group.module_strategy_panel))
        tabs=list(main_window.grouped_task_tabs)
        if getattr(main_window,'onetime_tab',None):tabs.append(main_window.onetime_tab)
        for tab in tabs:
            if getattr(tab,'result_status_hook',False):continue
            original=tab.update_task_info
            def update_info(task,tab=tab,update=original):
                control=None
                if task is not None:
                    from src.task_controls import control_status,control_for
                    control=control_for(task)
                    if control:task.info['控制状态']=control_status(task)
                update(task)
                if task is not None and task.info.get('完整路线结果')=='INTERRUPTED':
                    tab.task_info_container.titleLabel.setText('已中断（进度已保存）'+tab.current_task_name)
                elif control and control.state=='PARTIAL':
                    tab.task_info_container.titleLabel.setText('部分完成 PARTIAL'+tab.current_task_name)
                elif control and control.state in ('PAUSED','STOPPED','CANCELLED','ERROR'):
                    tab.task_info_container.titleLabel.setText(control_status(task)+tab.current_task_name)
                elif task is not None and task.info.get('停止原因'):
                    tab.task_info_container.titleLabel.setText('失败停止'+tab.current_task_name)
                elif task is not None and not task.enabled:
                    result=str(task.info.get('结果',''))
                    if 'PARTIAL' in result or 'SKIPPED' in result:
                        tab.task_info_container.titleLabel.setText('结束（含跳过项）'+tab.current_task_name)
            tab.update_task_info=update_info
            # The framework otherwise renders info only once a second, which
            # leaves a resumed status next to the already-paused button.
            from ok.core.events import communicate
            from shiboken6 import isValid
            def refresh_control(task=None,tab=tab):
                if isValid(tab):tab.update_info_table()
            communicate.task.connect(refresh_control)
            tab.destroyed.connect(lambda *unused,callback=refresh_control:communicate.task.disconnect(callback))
            tab.timer.setInterval(100)
            tab.result_status_hook=True
        from src import opening
        class OpeningNotifications(QObject):
            success=Signal()
        self.opening_notifications=OpeningNotifications(main_window)
        self.opening_notifications.success.connect(lambda:QMessageBox.information(main_window,'抢开服成功','已成功进入游戏主界面！循环已停止。'))
        opening.notifications=self.opening_notifications
        from src.opening_focus import remember_external
        from ok import og
        if not hasattr(self,'focus_timer'):
            self.focus_timer=QTimer(main_window)
            self.focus_timer.timeout.connect(lambda:remember_external(getattr(og.device_manager.hwnd_window,'hwnd',None)))
            self.focus_timer.start(200)
        tab = main_window.start_tab
        if hasattr(tab, 'diagnostics_panel'):
            return
        tab.diagnostics_panel = DiagnosticsPanel(tab)
        card = Card('游戏连接与问题报告', tab.diagnostics_panel)
        tab.vBoxLayout.insertWidget(1, card)
        from src.resolution_ui import ResolutionPanel
        tab.resolution_panel = ResolutionPanel(tab)
        tab.vBoxLayout.insertWidget(1,Card('分辨率适配',tab.resolution_panel))
        from ok import og
        from src.sweep_settings_ui import SweepSettingsPanel
        tab.sweep_settings_panel = SweepSettingsPanel(og.executor.onetime_tasks,tab)
        tab.vBoxLayout.insertWidget(2,Card('暗涌扫荡设置',tab.sweep_settings_panel))
        tab.export_log_button.setText('导出原始日志')
        tab.start_card.refresh_button.clicked.connect(tab.diagnostics_panel.poll)
        from src.update_installer import report_update_startup
        QTimer.singleShot(1000, lambda: report_update_startup(main_window))
