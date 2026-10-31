"""Explicit live acceptance through the production Qt Start buttons.

No synthetic faults, replacement workflows or direct game input. Each run has
its own continuous production log. Stops the suite on any unsafe interruption.
"""
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
parser.add_argument('--round',choices=('single','full','repeat'),required=True)
parser.add_argument('--exit-after',action='store_true')
args=parser.parse_args()
sys.argv=sys.argv[:1]  # Framework arguments are independent of this test driver.

from src.portable_runtime import prepare
if not getattr(sys,'frozen',False):prepare(ROOT)
from src.runtime_identity import get_runtime_identity,log_runtime_start
from src.config import config
from src.daily_scheduler import NAMES
from src.diagnostics import require_input_privilege
from ok import OK
from PySide6.QtCore import QTimer
from ok.ui.qt.tasks.TaskCard import TaskCard

out=DATA_ROOT/'logs'/('daily-acceptance-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
out.mkdir()
identity=get_runtime_identity().as_dict()
evidence=dict(round=args.round,runtime_identity=identity,started=datetime.now().isoformat(),runs=[],
              source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in (ROOT/'src').glob('*.py')})
from types import CodeType
from src.workflow import Workflow
from src.daily_scheduler import DailySchedule
from src.dispatch import Dispatch
def code_signature(code):
    return (code.co_code.hex(),code.co_names,tuple(code_signature(value) if isinstance(value,CodeType)
                                                else repr(value) for value in code.co_consts))
evidence['loaded_code_sha256']={
    name:hashlib.sha256(repr(code_signature(function.__code__)).encode('utf-8')).hexdigest()
    for name,function in (
        ('Workflow.read_daily_account',Workflow.read_daily_account),
        ('DailySchedule.__init__',DailySchedule.__init__),
        ('DailySchedule.dark_progress',DailySchedule.dark_progress),
        ('Dispatch.special_plan',Dispatch.special_plan),
        ('Dispatch.pick_candidate',Dispatch.pick_candidate),
        ('Dispatch.locate_special_member',Dispatch.locate_special_member))}
def save():
    (out/'acceptance.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
save()
app=OK(config)
if app.init_error:raise RuntimeError(app.init_error)
log_runtime_start()
app.app.show_main_window()
window=app.app.main_window
window.switchTo(window.onetime_tab)
queue=list(NAMES) if args.round=='single' else [None]
state=dict(index=0,task=None,started=None,previous=None,deadline=time.monotonic()+60)

def tick():
    try:
        task=state['task']
        if task is None:
            if time.monotonic()>state['deadline']:raise RuntimeError('GUI 初始化或窗口连接超时')
            hwnd=app.device_manager.hwnd_window.hwnd
            if not hwnd:return
            require_input_privilege(hwnd)
            task=next(t for t in app.task_executor.onetime_tasks if t.only==queue[state['index']])
            card=next(c for c in window.onetime_tab.findChildren(TaskCard) if c.task is task)
            card.update_buttons(task)
            if not card.start_button.isEnabled():return
            window.onetime_tab.ensureWidgetVisible(card.control_bar,0,32)
            window.grab().save(str(out/f"{state['index']:02d}-before-start.png"))
            state.update(task=task,started=datetime.now().isoformat(),previous=task.task_control,
                         deadline=time.monotonic()+900)
            evidence['runs'].append(dict(module=task.only or 'DAILY',started=state['started'],
                                         trigger='production Qt TaskCard Start button'))
            save()
            card.start_button.click()
            return
        control=task.task_control
        if time.monotonic()>state['deadline']:
            task.terminal_stop('CANCELLED',source='acceptance_timeout')
            raise RuntimeError('单项验收达到15分钟上限，保留检查点')
        if control is None or control is state['previous'] or control.state not in control.TERMINAL:return
        if task.running:return
        row=evidence['runs'][-1]
        row.update(ended=datetime.now().isoformat(),control=control.state,info=dict(task.info))
        logdir=task.info.get('日志位置')
        if logdir and (Path(logdir)/'result.json').exists():
            row['production_result']=json.loads((Path(logdir)/'result.json').read_text(encoding='utf-8'))
        window.grab().save(str(out/f"{state['index']:02d}-result.png"))
        save()
        if control.state not in ('SUCCESS','PARTIAL') or task.info.get('完整路线结果')=='INTERRUPTED':
            raise RuntimeError('真实任务中断，验收停止，未自动进入后续任务')
        state['index']+=1
        if state['index']==len(queue):
            evidence['ended']=datetime.now().isoformat();evidence['status']='FINISHED'
            save();timer.stop()
            if args.exit_after:app.app.quit()
            # Keep the actual GUI visible for inspection; no automatic close.
            return
        state.update(task=None,deadline=time.monotonic()+60)
    except Exception as error:
        evidence.update(status='BLOCKED',error=str(error),ended=datetime.now().isoformat())
        save();timer.stop()

timer=QTimer(window);timer.timeout.connect(tick);timer.start(500)
# The main window is already created above. OK.start() creates another one,
# leaving the test attached to a hidden panel and installing duplicate hotkeys.
app.app.exec()
