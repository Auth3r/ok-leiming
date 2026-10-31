"""Authorized real acceptance using the existing Qt panel and production runner."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.app_paths import APP_ROOT as ROOT
from src.app_paths import DATA_ROOT
if not getattr(sys,'frozen',False):os.chdir(ROOT)
parser=argparse.ArgumentParser()
parser.add_argument('--live',action='store_true',required=True)
parser.add_argument('--runs',type=int,default=2)
parser.add_argument('--resume-current',action='store_true',help='Diagnostic takeover only; not a from-home acceptance run')
parser.add_argument('--inspect-mode',action='store_true',help='Evidence only: open the verified idle mode selector, never start a challenge')
parser.add_argument('--return-home',action='store_true',help='Diagnostic navigation only: verified idle START to home using the existing native home asset')
parser.add_argument('--set-normal',action='store_true',help='QA setup only: choose normal mode and return to idle; never start a challenge')
args=parser.parse_args();sys.argv=sys.argv[:1]
if sum((args.inspect_mode,args.return_home,args.set_normal))>1:parser.error('Choose only one diagnostic navigation action')
from src.portable_runtime import prepare
if not getattr(sys,'frozen',False):prepare(ROOT)  # Frozen main already prepared the portable alias.
from src.runtime_identity import get_runtime_identity,log_runtime_start
from src.config import config
from ok import OK
from PySide6.QtCore import QTimer

if args.inspect_mode:
    from src.roguelike_runtime_720p import NativeRunner, UnsupportedNativePage
    from src.roguelike_policy import RogueAction, RogueState as S
    def inspect_mode(self,o):
        if getattr(self,'mode_inspection_sent',False):
            raise UnsupportedNativePage('模式选择实机取证完成；未开始挑战')
        if o.state!=S.START or o.event not in ('idle','mode_required') or self.active:
            raise UnsupportedNativePage('模式取证只允许已确认的空闲挑战准备页')
        def locate(current):
            match=self.screen().result['preparation_matches']['prep_change_difficulty']
            if current.state!=S.START or not match['matched']:return None
            return RogueAction('change_difficulty',tuple(match['bbox']),'切换难度',irreversible=False)
        self.mode_inspection_sent=True
        self.send_retryable_stable(S.START,locate,
            lambda current:current.state==S.START and current.event=='mode_select','模式选择只读取证')
    NativeRunner._step=inspect_mode

if args.set_normal:
    from src.roguelike_runtime_720p import NativeRunner, UnsupportedNativePage
    from src.roguelike_policy import RogueAction, RogueState as S
    def set_normal(self,o):
        if self.active:raise UnsupportedNativePage('已有挑战时不允许设置验收模式')
        if o.state==S.START and o.event=='idle' and o.action('start'):
            def open_picker(current):
                if current.event!='idle':return None
                match=self.screen().result['preparation_matches']['prep_change_difficulty']
                return RogueAction('change_difficulty',tuple(match['bbox']),'切换难度',irreversible=False) if match['matched'] else None
            self.send_retryable_stable(S.START,open_picker,
                lambda current:current.state==S.START and current.event=='mode_selected','验收打开当前模式')
        elif o.state==S.START and o.event=='mode_selected':
            def locate(current):
                if current.event!='mode_selected':return None
                box=self.screen().native.find('mode_normal')
                return RogueAction('select_normal',box,'普通模式验收准备',irreversible=False) if box else None
            self.send_retryable_stable(S.START,locate,
                lambda current:current.state==S.START and current.event=='mode_normal_pending','验收预选普通模式')
        elif o.state==S.START and o.event=='mode_normal_pending':
            def apply(current):
                if current.event!='mode_normal_pending':return None
                box=self.screen().native.find('mode_apply')
                return RogueAction('apply_normal',box,'确认普通模式',irreversible=False) if box else None
            self.send_retryable_stable(S.START,apply,
                lambda current:current.state==S.START and current.event=='mode_select','验收确认普通模式')
        elif o.state==S.START and o.event=='mode_select':
            def back(current):
                if current.event!='mode_select':return None
                box=self.screen().native.find('mode_back')
                return RogueAction('back',box,'返回',irreversible=False) if box else None
            self.send_retryable_stable(S.START,back,
                lambda current:current.state==S.START and current.event=='mode_required','验证普通模式准备页')
            self.f.control.finish('SUCCESS');self.terminal=True
        else:raise UnsupportedNativePage('模式验收准备只接受已确认的模式选择页')
    NativeRunner._step=set_normal

if args.return_home:
    import cv2
    import numpy as np
    from src.roguelike_runtime_720p import NativeRunner, UnsupportedNativePage
    from src.roguelike_policy import RogueAction, RogueState as S
    def return_home(self,o):
        if (o.state!=S.START or o.event not in ('idle','mode_required') or self.active
                or not (o.action('start') or o.action('change_difficulty'))):
            raise UnsupportedNativePage('回首页验收只允许已确认的空闲挑战准备页')
        # Reuse the recorded daily native home asset and its existing threshold;
        # all dispatch/freshness/ownership gates remain the production runner's.
        folder=ROOT/'templates/daily_native'
        spec=json.loads((folder/'manifest.json').read_text(encoding='utf8'))['templates']['home']
        templates=self.f.recognizer.templates
        templates.specs['qa_home']=dict(file=spec['file'],native_client_size=[1280,720],
            reference_size=[3840,2160],reference_roi=spec['roi'],threshold=spec['threshold'],label='指挥室')
        templates.images['qa_home']=cv2.imdecode(np.fromfile(folder/spec['file'],np.uint8),1)
        templates.gray['qa_home']=cv2.cvtColor(templates.images['qa_home'],cv2.COLOR_BGR2GRAY)
        def locate(current):
            if current.state!=S.START or current.event not in ('idle','mode_required'):return None
            box=self.screen().native.find('qa_home')
            return RogueAction('home',box,'指挥室',irreversible=False) if box else None
        self.send_retryable_stable(S.START,locate,lambda current:current.state==S.MAIN_MENU,'验收返回首页')
        self.f.control.finish('SUCCESS');self.terminal=True
    NativeRunner._step=return_home

out=DATA_ROOT/'logs'/('roguelike-acceptance-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
out.mkdir()
report=dict(started=datetime.now().isoformat(),pid=os.getpid(),runs=args.runs,start_mode='takeover' if args.resume_current else 'home',
            mode_inspection=args.inspect_mode,
            return_home_only=args.return_home,
            harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest() if Path(__file__).is_file() else None,
            frozen=bool(getattr(sys,'frozen',False)),
            executable_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest() if getattr(sys,'frozen',False) else None,
            set_normal_only=args.set_normal,
            runtime_identity=get_runtime_identity().as_dict(),
            source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in (ROOT/'src').glob('*.py')},
            template_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                             for folder in ('navigation_720p','roguelike_720p','roguelike_stage_720p')
                             for p in (ROOT/'templates'/folder).rglob('*') if p.is_file()},status='INITIALIZING')
def save():
    (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf8')
save()
app=OK(config)
if app.init_error:raise RuntimeError(app.init_error)
log_runtime_start();app.app.show_main_window()
window=app.app.main_window
panel=window.start_tab.runtime_720p_panel
panel.mode.setCurrentIndex(1)
panel.options=lambda:{'肉鸽循环次数':args.runs,'显示肉鸽悬浮窗':True}
state={'started':False,'deadline':time.monotonic()+600,'probe':0,'command':None}
def tick():
    command=out/'command.json'
    if command.exists():
        request=json.loads(command.read_text(encoding='utf8'))
        if request['id']!=state['command']:
            state['command']=request['id']
            if request['action']=='resume' and panel.control and panel.control.state=='PAUSED':
                panel.resume_button.click()
            elif request['action']=='stop':panel.stop_button.click()
            elif request['action']=='close':
                timer.stop();panel.stop_button.click();app.app.quit();return
            report.setdefault('commands',[]).append(request);save()
    if not state['started']:
        if time.monotonic()>state['deadline']:
            report.update(status='START_BLOCKED',reason='十分钟内未确认游戏首页；未启动输入');save();timer.stop();return
        if not app.device_manager.hwnd_window.hwnd:return
        if time.monotonic()<state['probe']:return
        state['probe']=time.monotonic()+2
        from src.roguelike_runtime_720p import discover_target
        from src.capture_720p import Capture720p
        from src.navigation_720p import Navigation720p
        hwnd,pid=discover_target()
        with Capture720p(hwnd,pid) as capture:
            packet=capture.get_frame()
            result=Navigation720p().observe(packet,hwnd=hwnd,pid=pid)
        if result['state']!='HOME_CONFIRMED' and not args.resume_current:
            report.update(status='WAITING_FOR_HOME',page=result['state']);save();return
        window.switchTo(next(g for g in window.grouped_task_tabs if g.group_name=='肉鸽'))
        panel.start_button.click()
        if panel.worker:
            state['started']=True;report['status']='RUNNING';report['directory']=str(panel.directory);save()
        elif time.monotonic()>state['deadline']:
            report['status']='START_BLOCKED';report['reason']=panel.reason;save();timer.stop()
    else:
        status=panel.control.state
        if (report.get('control'),report.get('reason'),report.get('completed'))!=(status,panel.reason,panel.completed):
            report.update(control=status,reason=panel.reason,completed=panel.completed,
                          updated=datetime.now().isoformat());save()
        if status in panel.control.TERMINAL and not panel.worker.is_alive():
            report.update(status='FINISHED',control=status,completed=panel.completed,reason=panel.reason);save();timer.stop()
def guarded_tick():
    try:tick()
    except Exception as error:
        timer.stop();panel.stop_button.click()
        report.update(status='BLOCKED',reason=str(error));save()
timer=QTimer();timer.timeout.connect(guarded_tick);timer.start(500)
# show_main_window above already creates the real main window; do not create a
# second, invisible panel with another set of hotkeys by calling OK.start again.
app.app.exec()
