"""Existing Qt application panel for a GUI-started native 720P run."""
import time
import json
import os
from datetime import datetime
from queue import Empty, SimpleQueue
from threading import Thread, Lock

import cv2
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QImage,QPixmap,QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QComboBox,QPushButton

from src.roguelike_control import RoguelikeControl
from src.roguelike_runtime_720p import run_native_application
from src.settings import ROOT
from src.roguelike_status import replace_status
from src.task_controls import TaskControlBar


class Runtime720pPanel(QWidget):
    def __init__(self,parent=None,*,runner=run_native_application,exclusive=None,show_hud=True,options=None):
        super().__init__(parent)
        self.runner=runner;self.exclusive=exclusive or self.framework_idle
        self.show_hud=show_hud;self.hud=None;self.hwnd=None
        from src.config_ownership import read_roguelike
        self.options=options or (lambda:read_roguelike(ROOT))
        self.target_runs=1;self.run_show_hud=show_hud;self.legacy_cards=[]
        self.control=None;self.worker=None;self.queue=SimpleQueue()
        self.frame_lock=Lock();self.latest_frame=None
        self.page='未开始';self.phase='待机';self.last_action='无';self.completed=0
        self.reason='';self.image=None;self.image_time=None;self.directory=None
        self.persisted_status=None
        self.status_attempted=None;self.status_write_error=''
        self.progress_write_error=''
        self.progress_data={};self.progress_received=None
        self.mode=QComboBox()
        self.mode.addItem('旧 4K 模式（使用原肉鸽任务卡）','legacy_4k')
        self.mode.addItem('720P 肉鸽（原生 WGC）','roguelike_720p')
        self.status_label=QLabel();self.status_label.setWordWrap(True)
        self.preview=QLabel('最新游戏画面将在启动后显示');self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(320,180);self.preview.setMaximumHeight(260)
        self.control_bar=TaskControlBar(self,start=self.start_run,toggle=self.toggle_pause,stop=self.stop_run)
        # These compatibility names refer to the same fixed primary widget.
        self.start_button=self.pause_button=self.resume_button=self.control_bar.primary_button
        self.stop_button=self.control_bar.stop_button
        self.open_button=QPushButton('查看完整截图')
        self.open_button.clicked.connect(self.open_image)
        self.mode.currentIndexChanged.connect(self.render)
        layout=QVBoxLayout(self);layout.addWidget(self.mode)
        row=QHBoxLayout()
        row.addWidget(self.control_bar);row.addWidget(self.open_button);row.addStretch(1)
        layout.addLayout(row)
        # Keep lifecycle controls above the long status and screenshot on
        # smaller 150%-scaled laptop windows.
        layout.addWidget(self.status_label);layout.addWidget(self.preview)
        self.hint=QLabel()
        self.hint.setWordWrap(True);layout.addWidget(self.hint)
        self.timer=QTimer(self);self.timer.timeout.connect(self.poll);self.timer.start(100)
        from PySide6.QtWidgets import QApplication
        QApplication.instance().aboutToQuit.connect(self.shutdown)
        self.render()

    @staticmethod
    def framework_idle():
        from ok import og
        executor=og.executor
        return not executor.current_task and not any(t.enabled for t in executor.onetime_tasks+executor.trigger_tasks)

    def sink(self,event):
        # Only one pending preview; critical error/control events are never dropped.
        if event['event']=='frame':
            with self.frame_lock:self.latest_frame=event
        elif event['event'] not in ('roguelike_wait','roguelike_safe_action_wait'):
            self.queue.put(event)

    def start_run(self):
        if self.mode.currentData()!='roguelike_720p' or (self.worker and self.worker.is_alive()):return
        if not self.exclusive():
            self.reason='请先停止其他任务；不能同时控制游戏';self.render();return
        try:
            options=self.options()
            target=options['肉鸽循环次数'];hud=options['显示肉鸽悬浮窗']
            if type(target) is not int or not 1<=target<=100 or type(hud) is not bool:
                raise ValueError('肉鸽循环次数须为1至100的整数，悬浮窗须为开关')
        except Exception as error:
            self.reason='肉鸽设置无效：'+str(error);self.render();return
        self.target_runs=target;self.run_show_hud=self.show_hud and hud
        self.control=RoguelikeControl();self.reason='';self.completed=0;self.last_action='无'
        self.page='运行前检查';self.phase='检查 HWND / DPI / WGC'
        self.progress_data={};self.progress_received=None
        self.directory=ROOT/'logs'/'roguelike-720p'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        self.image=None;self.image_time=None;self.hwnd=None
        with self.frame_lock:self.latest_frame=None
        self.preview.clear();self.preview.setText('正在获取新的可信 WGC 截图')
        while not self.queue.empty():self.queue.get_nowait()
        def work():
            try:self.runner(self.control,self.sink,self.directory,self.exclusive,target)
            except Exception as error:
                if self.control.state not in self.control.TERMINAL:self.control.finish('ERROR',source=str(error))
                self.sink({'event':'error','reason':str(error)})
            finally:self.sink({'event':'finished'})
        self.worker=Thread(target=work,name='Roguelike720p',daemon=True);self.worker.start();self.render()

    def pause_run(self):
        if self.control:self.control.set_paused('user',True)
        self.render()
    def toggle_pause(self):
        if self.control:self.control.toggle_pause('user')
        self.reason='';self.render()
    def resume_run(self):
        if not self.control or self.control.state!='PAUSED':return
        # Resume takes a new observation; never replay a previously prepared point.
        self.control.resume()
        self.reason='';self.render()
    def stop_run(self):
        if self.control:self.control.finish('STOPPED',source='GUI 停止')
        self.render()
    def shutdown(self):
        self.stop_run()
        if self.worker:self.worker.join(timeout=6)
        if self.hud:self.hud.close()

    def poll(self):
        with self.frame_lock:
            latest=self.latest_frame;self.latest_frame=None
        if latest:self.queue.put(latest)
        while True:
            try:event=self.queue.get_nowait()
            except Empty:break
            kind=event['event']
            if kind=='preflight':self.hwnd=event['hwnd'];self.phase=f"客户区 1280×720 · DPI {event['dpi']} · WGC"
            if kind=='frame':
                if not self.progress_data:self.page=event.get('page',self.page)
                self.image=event['frame'];self.image_time=event['provenance']['acquired_at']
                rgb=cv2.cvtColor(self.image,cv2.COLOR_BGR2RGB)
                image=QImage(rgb.data,rgb.shape[1],rgb.shape[0],rgb.strides[0],QImage.Format_RGB888).copy()
                self.preview.setPixmap(QPixmap.fromImage(image).scaled(640,360,Qt.KeepAspectRatio,Qt.SmoothTransformation))
            if kind=='native_progress':
                self.progress_data=event;self.progress_received=time.monotonic()
                self.progress_write_error=event.get('status_write_error','')
                self.page=event['page'];self.completed=event['completed']
                self.phase=event['wait_reason'] or '识别及状态决策'
            if kind=='native_assets_repaired':self.reason=''
            if kind=='roguelike_status':self.completed=event.get('completed_runs',self.completed)
            if kind in ('suspended','error'):
                self.reason=event['reason']
                if event.get('capture_error'):self.reason+='；新截图失败：'+event['capture_error']
            if kind=='roguelike_input_intent':self.phase=event.get('decision',self.phase)
            if event.get('last_action'):self.last_action=event['last_action']
        self.render()

    def render(self,*unused):
        if self.control is None:
            try:self.target_runs=self.options()['肉鸽循环次数']
            except (ValueError,KeyError,OSError):pass  # Start reports invalid settings before creating a worker.
        state=self.control.state if self.control else 'IDLE'
        label={'IDLE':'待机','RUNNING':'运行中','PAUSED':'已暂停','STOPPED':'已停止','ERROR':'错误停止','SUCCESS':'已完成'}.get(state,state)
        age=f'截图 {max(0,time.time()-self.image_time):.1f} 秒前' if self.image_time else '尚无可信截图'
        progress=''
        if self.progress_data:
            p=self.progress_data
            delta=max(0,time.monotonic()-self.progress_received) if state=='RUNNING' else 0
            progress=(f"\n最近确认：{p['last_confirmed']} · 当前状态 {p['state_elapsed']+delta:.1f} 秒"
                f" · 距上次推进 {p['since_advance']+delta:.1f} 秒"
                f"\n实际 dispatch：{'是' if p['dispatched'] else '否'} · 预期：{p['expected_next'] or '尚未生成动作'}"
                f"\n等待：{p['wait_reason'] or '无'} · {p['wait_elapsed']+delta:.1f} / {p['wait_limit'] or '—'} 秒")
        self.status_label.setText(f'模式：{self.mode.currentText()} · {label}\n当前页面：{self.page}\n阶段：{self.phase}\n最近动作：{self.last_action} · 完成 {self.completed}/{self.target_runs} 轮\n{age}'+progress+ ('\n原因：'+self.reason if self.reason else '')+ ('\n'+self.status_write_error if self.status_write_error else '')+ ('\n'+self.progress_write_error if self.progress_write_error else ''))
        active=self.worker is not None and self.worker.is_alive()
        native=self.mode.currentData()=='roguelike_720p'
        for card in self.legacy_cards:card.setVisible(not native)
        for widget in (self.control_bar,self.open_button,self.preview,self.status_label):
            widget.setVisible(native)
        self.hint.setText(('按设置 → Roguelike 的循环次数运行，达到目标后停止；悬浮窗开关同样生效。设置修改在下次开始生效。\n未支持页面、付费/稀缺资源确认、登录和权限异常会暂停并显示截图。F7 暂停/继续，F8 停止。'
                           if native else '旧 4K：使用下方“刷肉鸽”或“继续肉鸽”任务卡启动。两种分辨率共用设置 → Roguelike 的循环次数和悬浮窗开关；不会同时运行。'))
        # Terminal controls cannot revive the worker while it is unwinding.
        self.control_bar.render(state,start_enabled=not active and native,
                                reason=self.reason if state=='PAUSED' else '')
        self.mode.setEnabled(not active and self.exclusive());self.open_button.setEnabled(self.image is not None)
        status={'mode':self.mode.currentData(),'state':state,'page':self.page,'phase':self.phase,
                'last_action':self.last_action,'completed':self.completed,'target_runs':self.target_runs,'reason':self.reason}
        if status!=self.status_attempted:
            self.status_attempted=status
            try:
                from src.runtime_identity import get_runtime_identity
                data={**status,'updated_at':datetime.now().isoformat(),
                    'pid':os.getpid(),
                    'screenshot_acquired_at':self.image_time,'directory':str(self.directory) if self.directory else None,
                    'runtime_identity':get_runtime_identity().as_dict()}
                paths=[ROOT/'logs'/f'roguelike-720p-panel-{os.getpid()}.json']
                if self.directory:paths.append(self.directory/'gui-status.json')
                for path in paths:
                    path.parent.mkdir(parents=True,exist_ok=True)
                    temp=path.with_suffix('.tmp')
                    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
                    replace_status(temp,path)
                self.persisted_status=status
                self.status_write_error=''
            except OSError as error:
                self.status_write_error='运行状态保存失败：'+str(error)
        if self.run_show_hud and self.hwnd and active:
            try:
                if self.hud is None:
                    from src.roguelike_hud import RoguelikeHud
                    self.hud=RoguelikeHud(self)
                texts=(f'720P 肉鸽 · {label}',f'已完成 {self.completed}/{self.target_runs} 轮','页面：'+self.page,
                       '最近：'+self.last_action,(self.reason or self.progress_write_error or self.phase)+
                       (f" · 已等待 {self.progress_data['wait_elapsed']:.1f} 秒" if self.progress_data else ''),
                       'F7 暂停/继续 · F8 停止')
                for widget,text in zip(self.hud.labels,texts):widget.setText(text)
                # show_for calls render(); replace only this HUD instance's rendering.
                self.hud.render=lambda:None
                self.hud.show_for(self.hwnd)
            except Exception as error:
                self.run_show_hud=False
                self.reason+='；悬浮窗不可用：'+str(error)
        elif self.hud:self.hud.hide()

    def open_image(self):
        if self.image is None or not self.directory:return
        # The file and age refer to exactly the displayed frame, not a pretend fresh image.
        path=self.directory/'preview.png'
        if not path.parent.exists():return
        cv2.imencode('.png',self.image)[1].tofile(path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
