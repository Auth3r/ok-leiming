"""Bounded daily state machine. All device operations stay in ok-script BaseTask."""
import json
import re
import time
from enum import StrEnum
from datetime import datetime
from threading import Lock
import cv2
from src.settings import ROOT, validate
from src.vision import Vision
from src.run_plan import RunPlan
from src.runtime_identity import get_runtime_identity


class State(StrEnum):
    MAIN_MENU='MAIN_MENU'
    FUND='FUND'
    BLACK_SHIELD='BLACK_SHIELD'
    BLACK_DISPATCH='BLACK_DISPATCH'
    DARK_TIDE='DARK_TIDE'
    GANHUO='GANHUO'
    HUNT_GOLD='HUNT_GOLD'
    DAILY_REWARD='DAILY_REWARD'
    DONE='DONE'


class SafetyStop(RuntimeError):
    pass


class RecognitionTimeout(SafetyStop):
    pass


class InsufficientResource(SafetyStop):
    """A unique, high-confidence current balance is below the known cost."""


class PageChanged(SafetyStop):
    """No input was sent; discard the target and observe again."""


# The old main_dark crop was centred here; the business entry remained in this
# 4K area after its artwork changed. Keep the click inside the verified crop.
DARK_ENTRY_AREA = (2756, 812, 3060, 896)
DARK_ENTRY_POINT = (2908, 854)

# Windows append handles can overwrite each other across task/mouse threads.
_EVENT_LOG_LOCK = Lock()


class Workflow:
    def __init__(self, task, settings, stop_event, clock=time.monotonic):
        if not isinstance(settings, RunPlan): validate(settings)
        from src.resolution import ReferenceTask
        self.task=ReferenceTask(task,settings); self.cfg=settings; self.stop=stop_event; self.clock=clock
        self.vision=Vision(self.task,settings['match_threshold'])
        self.state=State.MAIN_MENU
        self.last_frame=None
        self.results={}; self.sequence=0
        self.logdir=ROOT/'logs'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        self.logdir.mkdir(parents=True,exist_ok=True)
        self.task.emit=self.emit
        self.resolution_logged=False
        self.emit('start',runtime_identity=get_runtime_identity().as_dict(),run_plan=settings.safe_summary() if isinstance(settings, RunPlan)
                  else {key: value for key, value in settings.items()
                        if key not in ('game_exe', 'window_title')})

    def emit(self, event, **data):
        from src.release_limits import evidence_allowed
        evidence_allowed(self.logdir)
        record={'time':datetime.now().isoformat(),'state':str(self.state),'event':event,**data}
        with _EVENT_LOG_LOCK, (self.logdir/'events.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(record,ensure_ascii=False,default=str)+'\n')
        if event in ('verified','click','scroll') and data.get('step'):
            self.task.info_set('当前步骤',str(data['step']))
        sink=getattr(self.task,'roguelike_event',None)
        if sink:sink(event,data)
        if event not in ('poll','roguelike_wait'): self.task.log_info(f'{self.state} | {event} | {data}')

    def checkpoint(self):
        control=getattr(self.task,'roguelike_checkpoint',None)
        if control:control()
        if self.stop.is_set(): raise SafetyStop('F8 紧急停止：本次任务已取消')

    def refresh(self):
        self.checkpoint()
        frame=self.task.next_frame()
        self.checkpoint()
        if frame is not None: self.last_frame=frame.copy()
        if frame is None:
            raise SafetyStop('游戏截图为空')
        self.target_identity=self.task.assert_target(self.cfg)
        if self.target_identity and tuple(self.target_identity.get('resolution',self.task.geometry.size))!=self.task.geometry.size:
            raise SafetyStop('截图与实际游戏客户区尺寸不一致，拒绝使用旧坐标')
        if not self.resolution_logged:
            self.emit('resolution',selected=self.task.selected,client_size=self.task.geometry.size,
                      identity=self.target_identity,reference_size=(3840,2160))
            self.resolution_logged=True
        self.vision.begin_frame()

    def delay(self, duration):
        end=self.clock()+duration
        while self.clock()<end:
            self.checkpoint(); self.task.sleep(min(0.1,end-self.clock()))
        self.checkpoint()

    def snapshot(self, label):
        from src.release_limits import evidence_allowed
        if not evidence_allowed(self.logdir): return
        self.sequence+=1
        stem=f'{self.sequence:04d}_{self.state}_{label}'
        # Never ask a paused executor for a fresh frame while handling an emergency stop.
        frame=self.last_frame
        if self.task.native_frame is not None: frame=self.task.native_frame
        if frame is not None:
            cv2.imencode('.png',frame)[1].tofile(self.logdir/(stem+'.png'))
            annotated=frame.copy()
            for result in self.vision.results.values():
                if result['box']:
                    x,y,w,h=result['box'];color=(0,220,0) if result['matched'] else (0,0,255)
                    if tuple(frame.shape[1::-1])!=(3840,2160):
                        x,y,x2,y2=self.task.geometry.roi((x,y,x+w,y+h));w,h=x2-x,y2-y
                    cv2.rectangle(annotated,(x,y),(x+w,y+h),color,3)
                    cv2.putText(annotated,f"{result['name']} {result['confidence']:.3f}",(x,max(25,y-8)),cv2.FONT_HERSHEY_SIMPLEX,.7,color,2)
            cv2.imencode('.jpg',annotated)[1].tofile(self.logdir/(stem+'_matches.jpg'))
        (self.logdir/(stem+'.json')).write_text(json.dumps(list(self.vision.results.values()),ensure_ascii=False,indent=2),encoding='utf-8')
        metadata=getattr(self.task,'metadata',None)
        if metadata:
            (self.logdir/(stem+'_capture.json')).write_text(json.dumps(metadata,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

    def wait(self, predicate, label, *, timeout=None, retries=None):
        """Retry recognition only. Never replay an uncertain resource-spending click."""
        from src.capture_720p import CaptureError
        capture_restarts=0
        timeout=self.cfg['recognition_timeout'] if timeout is None else timeout
        retries=self.cfg['max_retries'] if retries is None else retries
        for attempt in range(retries+1):
            deadline=self.clock()+timeout; consecutive=0
            diagnostic_at=self.clock()+3
            while self.clock()<deadline:
                try:self.refresh()
                except CaptureError as error:
                    # A failed WGC owner remains faulted. Discard its pixels
                    # and recreate capture inside this observation, not by
                    # replaying the operation whose result we are awaiting.
                    self.checkpoint()
                    self.last_frame=None
                    self.task.native_frame=self.task.reference_frame=None
                    self.task.metadata=None
                    self.vision.begin_frame()
                    self.task.assert_target(self.cfg)
                    if capture_restarts>=2:raise
                    self.task.close()
                    capture_restarts+=1
                    consecutive=0
                    self.emit('capture_observation_restarted',step=label,
                              reason=str(error),restart=capture_restarts)
                    self.delay(.2)
                    continue
                if (label!='关闭已验证羁绊提示 点击前' and label!='关闭已验证羁绊提示 结果' and
                        'notice_title' in getattr(self.vision,'images',{}) and self.vision.page('NOTICE')):
                    notices=getattr(self,'notices',0)
                    if notices>=3:raise SafetyStop('已验证提示反复出现，停止输入')
                    self.notices=notices+1
                    self.snapshot('known_notice')
                    self.click('NOTICE','notice_prompt',lambda:not self.vision.page('NOTICE'),'关闭已验证羁绊提示')
                    self.refresh()
                value=predicate()
                consecutive=consecutive+1 if value else 0
                if consecutive>=2:
                    self.emit('verified',step=label,attempt=attempt+1)
                    if self.cfg['debug_mode']: self.snapshot('verified')
                    return value
                if self.clock()>=diagnostic_at:
                    self.snapshot('no_progress_3s')
                    self.emit('no_progress_diagnostic',step=label,attempt=attempt+1,
                              capture=getattr(self.task,'metadata',None))
                    diagnostic_at=self.clock()+3
                self.delay(0.2)
            self.snapshot('timeout')
            self.emit('recognition_retry',step=label,attempt=attempt+1)
        raise RecognitionTimeout(f'{label}：识别超时')

    def optional_entry(self, page, target):
        """Only infer a missing entry while its parent page remains identified."""
        self.require(page)
        deadline=self.clock()+min(2.0,self.cfg['recognition_timeout'])
        while self.clock()<deadline:
            self.refresh()
            if not self.vision.page(page):
                raise SafetyStop('检查任务入口时页面变化，停止操作')
            if self.vision.find(target):return True
            self.delay(.2)
        self.snapshot('entry_missing')
        self.emit('entry_missing',page=page,target=target)
        return False

    def recover_for_next_task(self):
        # Never dismiss an unrecognized overlay or replay a spending click.
        pages=('MAIN_MENU','NAV','RESOURCE_HUB','BLACK_SHIELD','FUND','DARK_HUB','DARK_MAP',
               'DARK_STAGE','GANHUO','HUNT_GOLD','DAILY_REWARD','DAILY_PLAN')
        # Ordinary page classification gets one short observation window. A
        # missing page label is not a capture fault; wait already recreates WGC
        # on CaptureError without replaying input or waiting another 3 x 15s.
        try:
            self.wait(lambda:any(self.vision.page(p) for p in pages+('REWARD','DISPATCH','DISPATCH_LEAVE',
                'DARK_DIALOG','FREE_DIALOG','MODULE_CAPACITY','NOTICE')),'恢复：识别当前页面',
                timeout=min(5.,self.cfg['recognition_timeout']),retries=0)
        except RecognitionTimeout as error:
            self.checkpoint()
            raise SafetyStop('无法确认当前页面，不能安全返回并跳过任务') from error
        if self.vision.page('NOTICE'):
            self.click('NOTICE','notice_prompt',lambda:not self.vision.page('NOTICE'),'关闭已验证羁绊提示')
        if self.vision.page('REWARD'):
            # On restart self.state names the NEXT module, not the producer
            # of this reward. Dismiss the verified overlay once, then classify
            # its actual destination without claiming that old work succeeded.
            self.click('REWARD','reward_prompt',lambda:any(self.vision.page(p) for p in pages),
                       '恢复：关闭已验证奖励并核对当前页面',reward_blank=True)
        if self.vision.page('DISPATCH'):
            self.click('DISPATCH','back',lambda:self.vision.page('BLACK_SHIELD') or
                       self.vision.page('DISPATCH_LEAVE'),'恢复：退出派遣详情')
        if self.vision.page('DISPATCH_LEAVE'):
            self.click('DISPATCH_LEAVE','dispatch_leave_confirm',lambda:self.vision.page('BLACK_SHIELD') or
                       self.vision.page('MAIN_MENU'),'恢复：放弃未提交的队员选择')
        if self.vision.page('MODULE_CAPACITY'):
            self.click('MODULE_CAPACITY','module_capacity_cancel',lambda:self.vision.page('DARK_STAGE'),'恢复：取消库存提示')
        if self.vision.page('DARK_DIALOG'):
            self.click('DARK_DIALOG','dark_dialog',lambda:self.vision.page('DARK_STAGE'),'恢复：取消未提交扫荡',reward_blank=True)
        if self.vision.page('FREE_DIALOG'):
            # No calibrated non-spending cancel control exists for this dialog.
            raise SafetyStop('免费扫荡确认框缺少已验证取消入口，保留检查点')
        current=next((p for p in pages if self.vision.page(p)),None)
        if current is None:raise SafetyStop('无法确认当前页面，不能安全返回并跳过任务')
        self.require(current)
        if current!='MAIN_MENU':self.home(current)
        self.require('MAIN_MENU')

    def require(self, page):
        return self.wait(lambda:self.vision.page(page),page)

    def dark_entry_fallback(self):
        frame=self.last_frame
        x,y=DARK_ENTRY_POINT
        x1,y1,x2,y2=DARK_ENTRY_AREA
        if (frame is None or tuple(frame.shape[1::-1])!=(3840,2160) or
                self.task.geometry.size!=(3840,2160) or
                not x1<=x<x2 or not y1<=y<y2 or not self.vision.page('MAIN_MENU')):
            return None
        return DARK_ENTRY_POINT

    def click_dark_entry(self, next_check, label):
        self.refresh()
        if not self.vision.page('MAIN_MENU'):
            self.emit('dark_entry',status='homepage_not_verified_click_refused')
            raise SafetyStop('暗涌入口：首页未可靠确认，拒绝固定区域点击')
        self.click('MAIN_MENU','main_dark',next_check,label,fallback=self.dark_entry_fallback)

    def click(self, page, target, next_check, label, roi=None, reward_blank=False, precondition=None,allow_already_done=False,reobserve=False,fallback=None):
        from src.roguelike_control import Reobserve
        for attempt in range(max(1,self.cfg['max_retries'])+1):
            try:
                return self._click(page,target,next_check,label,roi,reward_blank,precondition,
                    allow_already_done,reobserve,fallback)
            except (Reobserve,PageChanged):
                self.emit('resume_discarded_target',step=label,attempt=attempt+1)
                self.checkpoint()
                if attempt>=max(1,self.cfg['max_retries']):raise SafetyStop('恢复期间画面连续变化，停止输入')
                self.refresh()

    def _click(self, page, target, next_check, label, roi=None, reward_blank=False, precondition=None,allow_already_done=False,reobserve=False,fallback=None):
        def locate():
            box=self.vision.find(target,roi)
            if box:return ('template',(round(box.x+box.width/2),round(box.y+box.height/2)),float(box.confidence))
            point=fallback() if fallback else None
            return ('verified_home_fallback',point,None) if point else None
        self.wait(lambda:(allow_already_done and next_check()) or (self.vision.page(page) and locate()),label+' 点击前')
        if self.cfg['step_mode']:
            self.emit('step_pause',step=label)
            self.task.log_info(f'单步暂停：{label}；在 GUI 继续后切回游戏',notify=True)
            self.task.pause()
            self.delay(3)
            # Re-observe everything after an arbitrarily long manual pause.
            self.wait(lambda:self.vision.page(page) and locate(),label+' 恢复复核')
        before_click=getattr(self.task,'before_click',None)
        if before_click:before_click()
        self.refresh()
        if allow_already_done and next_check():
            self.emit('already_completed',step=label)
            return
        if not self.vision.page(page):
            self.snapshot('preclick_page_changed')
            raise PageChanged('点击前页面变化，拒绝点击')
        located=locate()
        if located is None and reobserve:
            self.wait(lambda:self.vision.page(page) and locate(),label+' 重新定位')
            located=locate()
        if located is None: raise SafetyStop('点击前按钮消失，拒绝点击')
        method,(x,y),confidence=located
        if reward_blank:
            # User-approved blank confirmation zone observed below items in both reward screenshots.
            x,y=1920,1836
        if precondition: precondition()
        self.snapshot('before_click')
        self.task.assert_foreground()
        self.checkpoint()
        self.emit('click',step=label,target=target,x=x,y=y,confidence=confidence)
        self.checkpoint()
        validate_click=getattr(self.task,'validate_click_context',None)
        if validate_click:validate_click()
        schedule=getattr(self,'daily_schedule',None)
        if schedule:schedule.action(target,label)
        self.task.click(x,y,after_sleep=0,down_time=0.15)
        if fallback:self.emit('dark_entry',status='template_matched' if method=='template' else 'used_verified_home_fallback',
                              point=[x,y],area=list(DARK_ENTRY_AREA),homepage='main_black+main_nav+main_daily')
        after_input=getattr(self.task,'after_input',None)
        if after_input:after_input()
        self.delay(self.cfg['click_min_wait'])
        self.wait(next_check,label+' 结果')
        if schedule:schedule.action(target,label,confirmed=True)
        self.snapshot('after_click')

    def reward(self, destination):
        self.click('REWARD','reward_prompt',lambda:self.vision.page(destination),'确认物资入库',reward_blank=True)

    def read_daily_account(self, quick=False):
        """Bind daily progress to two fresh home-name readings, not just a PID."""
        if quick:
            if not self.vision.page('MAIN_MENU'):return None
        else:
            self.require('MAIN_MENU')
        publish=getattr(self.task,'info_set',None)
        if publish:publish('当前步骤','核对暗涌历史记录')
        names=[]
        def read():
            if not self.vision.page('MAIN_MENU'):return False
            boxes=self.task.ocr(x=80/1280,y=0,to_x=350/1280,to_y=90/720,
                                frame=self.task.frame,threshold=.98)
            values=[b.name.strip() for b in boxes if b.confidence>=.98 and b.name.strip()
                    and 90<=b.y+b.height/2<=180]
            # Thin 720P name glyphs can be missed altogether beside the NEW
            # badge. Retry the same name field once on an explicit 2x OCR
            # canvas. This is preprocessing, not a lower confidence threshold.
            native=getattr(self.task,'native_frame',None)
            if not values and native is not None and getattr(native,'shape',None):
                height,width=native.shape[:2]
                l,t,r,b=(round(85*width/1280),round(20*height/720),
                         round(350*width/1280),round(70*height/720))
                field=cv2.resize(native[t:b,l:r],(530,100),interpolation=cv2.INTER_CUBIC)
                reread=self.task.ocr(frame=field,threshold=.98)
                values=[box.name.strip() for box in reread
                        if box.confidence>=.98 and box.name.strip()
                        and 20<=box.y+box.height/2<=80]
                self.emit('daily_account_field_reread',roi=[l,t,r,b],ocr_size=[530,100],
                          readings=[dict(text=box.name,confidence=float(box.confidence))
                                    for box in reread],unique=len(values)==1)
            if len(values)!=1:
                names.clear();return False
            names.append(values[0])
            return len(names)>=2 and names[-1]==names[-2]
        if quick:
            for _ in range(2):
                self.refresh()
                if read():break
            else:
                self.emit('daily_account_unavailable',scope='DARK_TIDE',observations=2)
                return None
        else:
            self.wait(read,'核对当前角色')
        self.emit('daily_account_verified',account_name=names[-1])
        return names[-1]

    def home(self, page):
        self.require(page)
        # All these observed screens have a direct 指挥室 button; map/stage use 返回.
        if page in ('DARK_MAP','DARK_STAGE'):
            destination='DARK_MAP' if page=='DARK_STAGE' else 'DARK_HUB'
            self.click(page,'back',lambda:self.vision.page(destination),'返回 '+destination)
            self.home(destination)
        elif page=='BLACK_SHIELD' and self.vision.find('home') is None:
            # The home icon can change after dispatch. The verified list's
            # existing 返回 control leads to the same parent; never use this
            # alternate path on a task detail page or an unidentified overlay.
            def verify_back():
                if (not self.vision.page('BLACK_SHIELD') or
                        self.vision.find('black_title') is None or
                        self.vision.find('black_credit') is None):
                    raise SafetyStop('黑盾返回前页面结构变化，拒绝点击')
                box=self.vision.find('back')
                if box is None:raise SafetyStop('黑盾返回按钮消失，拒绝点击')
                point=(round(box.x+box.width/2),round(box.y+box.height/2))
                self.emit('black_home_back_verified',confidence=float(box.confidence),
                          reference=list(point),client=list(self.task.geometry.point(*point)),
                          page_anchors=['black_title','black_credit'])
            self.click(page,'back',lambda:self.vision.page('MAIN_MENU') or
                       self.vision.page('DISPATCH_LEAVE'),'返回指挥室',
                       precondition=verify_back)
            # The list can retain an unsubmitted team after leaving details.
            # Its back button then opens this verified discard dialog.
            if self.vision.page('DISPATCH_LEAVE'):
                self.click('DISPATCH_LEAVE','dispatch_leave_confirm',
                           lambda:self.vision.page('MAIN_MENU'),
                           '返回前放弃未提交的队员选择')
        else:
            self.click(page,'home',lambda:self.vision.page('MAIN_MENU') or
                       (page=='BLACK_SHIELD' and self.vision.page('DISPATCH_LEAVE')),'返回指挥室')
            if page=='BLACK_SHIELD' and self.vision.page('DISPATCH_LEAVE'):
                self.click('DISPATCH_LEAVE','dispatch_leave_confirm',
                           lambda:self.vision.page('MAIN_MENU'),
                           '返回前放弃未提交的队员选择')

    def run(self, only=None, grace=0, prepare=None):
        try:
            if prepare: prepare()
            if grace: self.delay(grace)
            if only in ('ROGUELIKE','ROGUELIKE_RESUME'):
                from src.roguelike_runner import RoguelikeRunner
                self.state='ROGUELIKE'
                RoguelikeRunner(self,resume=only=='ROGUELIKE_RESUME').run()
                self.emit('done',results=self.results)
                return
            if only in ('MODULE_CRAFT','MODULE_RESUME'):
                from src.crafting_runner import ModuleCrafting
                self.state='MODULE_CRAFT'
                ModuleCrafting(self).run()
                self.emit('done',results=self.results)
                return
            if only=='MODULE_PREPARE':
                from src.module_crafting import ModulePreparation
                self.state='MODULE_PREPARE'
                self.task.info_set('当前状态','模组准备')
                ModulePreparation(self).run()
                self.results['MODULE_PREPARE']='READY：准备完成，尚未启动实验'
                self.emit('done',results=self.results)
                self.task.info_set('结果',self.results['MODULE_PREPARE'])
                self.task.log_info(self.cfg.get('制作配置','旧地联合')+'模组、蓝图及队员已准备完成；尚未启动实验。',notify=True)
                return
            if only=='INSPECT':
                self.refresh()
                self.require('MAIN_MENU')
                self.snapshot('inspect');self.emit('inspection_complete');return
            from src.daily_scheduler import DailySchedule
            self.refresh()
            # Current game state decides daily completion. A nickname must
            # never gate route startup or restore another account's modules.
            self.daily_fresh_route=True
            DailySchedule(self,only).run()
            self.task.info_set('当前状态','本次路线结束：'+self.route_status)
            self.emit('done',results=self.results,status=self.route_status)
            self.task.info_set('结果',str(self.results))
        except Exception as error:
            # Stop roguelike (legacy tasks pause) before diagnostic writes can fail.
            terminal=getattr(self.task,'terminal_stop',None)
            if terminal:terminal('STOPPED' if isinstance(error,SafetyStop) else 'ERROR')
            else:self.task.executor.pause()
            if self.stop.is_set() and not terminal: error=SafetyStop('F8 紧急停止：本次任务已取消')
            reason=str(error) or ('任务已被停止或取消' if type(error).__name__=='TaskDisabledException' else type(error).__name__)
            self.snapshot('FAILED')
            self.emit('stopped',error=reason,error_type=type(error).__name__,completed=self.results)
            self.task.info_set('停止原因',reason)
            prefix='已中断（进度已保存，再次启动完整日常可续跑）' if getattr(self,'daily_schedule',None) else '已停止'
            self.task.info_set('当前状态',prefix+'：'+reason)
            self.task.log_error(f'{prefix}：{reason}\n现场：{self.logdir}',notify=True)
            # Global pause stops subsequent queued tasks too. No auto-resume of failed step.
            raise error
        finally:
            try:
                (self.logdir/'result.json').write_text(json.dumps({'state':str(self.state),'results':self.results,
                    'runtime_identity':get_runtime_identity().as_dict(),
                    'status':getattr(self,'route_status',None),
                    'modules':getattr(self,'module_results',{})},ensure_ascii=False,indent=2),encoding='utf-8')
            finally:
                close=getattr(self.task,'close',None)
                if close:close()

    def black_shield(self):
        self.click('MAIN_MENU','main_black',lambda:self.vision.page('BLACK_SHIELD'),'进入黑盾委托')
        self.scroll_black(20,'领取前回到列表顶部')
        claimed=0;pages=0
        for _ in range(40):
            self.require('BLACK_SHIELD')
            if self.vision.find('black_claim'):
                before=len(self.vision.find_all('black_claim'))
                self.click('BLACK_SHIELD','black_claim',lambda:self.vision.page('REWARD') or
                    (self.vision.page('BLACK_SHIELD') and len(self.vision.find_all('black_claim'))<before),'领取委托')
                if self.vision.page('REWARD'):self.reward('BLACK_SHIELD')
                claimed+=1
                continue  # Re-read the reordered list after each confirmed claim.
            before=self.black_list_signature()
            self.scroll_black(-4,'检查后续可领取委托');pages+=1
            after=self.black_list_signature()
            self.emit('black_claim_scan',page=pages,before=before,after=after,claimed=claimed)
            if before==after:
                self.refresh()
                if self.black_list_signature()==after and not self.vision.find('black_claim'):break
            if pages>=12:raise RecognitionTimeout('黑盾列表超过12屏，无法证明领取完整')
        else:raise RecognitionTimeout('黑盾领取达到40次扫描上限')
        self.snapshot('black_claim_list_complete')
        self.emit('black_claim_complete',claimed=claimed,pages=pages,end_verified=True)
        self.home('BLACK_SHIELD')
        return f'SUCCESS：已领取{claimed}条，滚动检查{pages}屏并确认列表末尾无遗漏' if claimed else 'DONE_CHECK：列表已滚动至末尾，无可领取委托'

    def black_list_signature(self):
        self.require('BLACK_SHIELD')
        labels=self.task.ocr(x=.33,y=.16,to_x=.94,to_y=.85,frame=self.task.frame,threshold=.93)
        # Repeated fixed labels (e.g. 今日) retain the same coordinates even
        # when unreadable task rows scroll past. Only paired actual row names
        # may establish that the list contents stopped changing.
        labels=[b for b in labels if b.confidence>=.93]
        values=[]
        todays=sorted((b for b in labels if b.name.replace(' ','')=='今日'),key=lambda b:b.y)
        for today in todays:
            center=today.y+today.height/2
            names=[b for b in labels if 2<=len(b.name.replace(' ',''))<=24
                and b.name.replace(' ','')!='今日' and re.search('[\u4e00-\u9fff]',b.name)
                and today.x+today.width<=b.x<=today.x+today.width+500
                and abs(b.y+b.height/2-center)<=50]
            if len(names)!=1:
                raise RecognitionTimeout('委托列表任务行名称无法唯一识别，不能断言已全部领取')
            values.append((names[0].name.replace(' ',''),round(center/30)))
        values=tuple(values)
        if not values:raise RecognitionTimeout('委托列表文本无法可靠识别，不能断言已全部领取')
        return values

    def scroll_black(self,count,label):
        self.scroll_verified('BLACK_SHIELD',3000,1300,count,label)

    def scroll_verified(self,page,x,y,count,label):
        from src.cooperative import FocusChanged
        from src.roguelike_control import Reobserve
        for attempt in range(2):
            try:
                before=getattr(self.task,'before_click',None)
                if before:before()
                self.require(page);self.task.assert_foreground();self.checkpoint()
                self.snapshot('before_scroll')
                self.emit('scroll',step=label,x=x,y=y,count=count)
                self.task.scroll(x,y,count)
                break
            except (FocusChanged,Reobserve):
                if attempt:raise
                self.emit('scroll_reobserve',step=label)
                self.checkpoint()
        after=getattr(self.task,'after_input',None)
        if after:after()
        self.delay(self.cfg['click_min_wait']);self.require(page)

    def black_dispatch(self):
        from src.dispatch import Dispatch
        from src.dispatch_screen import read_dispatch_counter
        # An empty matcher result cannot establish that today's work is done.
        # Inspect the existing list HUD independently of team-selection policy.
        if not self.vision.page('DISPATCH'):
            if not self.vision.page('BLACK_SHIELD'):
                self.click('MAIN_MENU','main_black',lambda:self.vision.page('BLACK_SHIELD'),'进入黑盾派遣列表')
            self.require('BLACK_SHIELD')
            readings=[]
            for observation in range(2):
                self.refresh()
                if not self.vision.page('BLACK_SHIELD'):
                    raise SafetyStop('读取派遣剩余次数时页面变化，停止输入')
                reading=read_dispatch_counter(self,'BLACK_SHIELD')
                remaining=reading['remaining']
                readings.append(remaining)
                self.emit('dispatch_remaining_counter',observation=observation+1,**reading)
                if observation==0:self.delay(.2)
            self.dispatch_initial_quota=readings[0] if readings[0] is not None and readings[0]==readings[1] else None
            if readings==[0,0]:
                self.snapshot('dispatch_count_exhausted')
                self.emit('dispatch_count_exhausted',remaining=0,total=5,observations=2)
                self.home('BLACK_SHIELD')
                return 'DONE_CHECK：已两次确认今日剩余待办0/5，未重复派遣；已返回指挥室'
            if readings==[None,None] and self.check_assigned_dispatches():
                self.home('BLACK_SHIELD')
                return 'DONE_CHECK：完整列表五个唯一今日任务均有已派队员与倒计时，未重复派遣'
        result=Dispatch(self).run()
        if isinstance(result,str) and result.startswith('SUCCESS：检查0条，派遣0条'):
            return 'PARTIAL'+result.removeprefix('SUCCESS')+'；剩余次数或入口未核实，未证明已完成'
        return result

    def check_assigned_dispatches(self):
        from src.daily_dispatch_status import read_assigned_today
        if not read_assigned_today(self.task,self.vision):return False
        self.scroll_black(20,'核对已派遣状态，从列表顶部开始')
        records={}
        for screen in range(12):
            self.require('BLACK_SHIELD')
            rows=read_assigned_today(self.task,self.vision)
            for row in rows:records[row['name']]=row
            self.emit('dispatch_assigned_rows',screen=screen+1,rows=rows,unique=len(records))
            self.snapshot('dispatch_assigned_rows')
            if screen==0 and not rows:return False
            before=self.black_list_signature()
            self.scroll_black(-4,'核对已派遣列表后续')
            after=self.black_list_signature()
            if before==after:
                self.refresh()
                if self.black_list_signature()!=after:continue
                repeated=read_assigned_today(self.task,self.vision)
                for row in repeated:records[row['name']]=row
                if not rows or not repeated:return False
                if {row['name'] for row in repeated}!={row['name'] for row in rows}:return False
                self.emit('dispatch_assigned_complete',end_verified=True,completed=len(records)==5,
                          count=len(records),records=records)
                self.snapshot('dispatch_assigned_complete')
                return len(records)==5
        self.emit('dispatch_assigned_unproven',reason='超过十二屏或列表变化')
        return False

    def fund(self):
        self.refresh()
        if self.vision.find('main_fund'):
            self.click('MAIN_MENU','main_fund',lambda:self.vision.page('FUND'),'进入理财基金')
        else:
            self.click('MAIN_MENU','main_nav',lambda:self.vision.page('NAV'),'进入导航核对基金')
            self.click('NAV','nav_fund',lambda:self.vision.page('FUND'),'进入负荷推演')
        self.wait(lambda:self.vision.page('FUND') and (self.vision.find('fund_claim') or self.vision.find('fund_done')),'基金领取状态')
        if self.vision.find('fund_done'):
            self.home('FUND');return 'DONE_CHECK：今日分红已领取'
        self.click('FUND','fund_claim',lambda:self.vision.page('REWARD'),'领取今日分红')
        self.reward('FUND')
        self.wait(lambda:self.vision.page('FUND') and self.vision.find('fund_done'),'核对基金已领取')
        self.home('FUND')

    def bind_daily_progress(self):
        """Associate optional spending history; non-spending probes override it."""
        schedule=getattr(self,'daily_schedule',None)
        if schedule:
            if getattr(self,'daily_fresh_route',False):
                # Optional association of existing spending history. A
                # first run without a readable nickname remains runnable.
                account=self.read_daily_account(quick=True)
                schedule.bind_dark_account(account)
            return schedule.dark_progress()

    def dark_tide(self):
        from src.daily_scheduler import SweepHistoryUnavailable
        if self.cfg['dark_tide_sweep_count']==0: return 'SKIPPED：扫荡次数为 0'
        if self.vision.page('MODULE_CAPACITY'):return self.skip_dark_capacity()
        completed=0
        try:
            progress=self.bind_daily_progress()
        except SweepHistoryUnavailable as error:
            self.emit('dark_history_unresolved',reason=str(error))
            return 'BLOCKED：'+str(error)
        if progress is not None:
            if progress['uncertain']:return 'BLOCKED：此前扫荡提交结果不明，禁止重复消耗；请核对日志'
            completed=progress['completed']
            if completed>=self.cfg['dark_tide_sweep_count']:
                return f'DONE_CHECK：本日同一游戏会话已验证{completed}次扫荡，达到计划次数'
        self.task.info_set('暗涌计划次数',self.cfg['dark_tide_sweep_count'])
        if not self.vision.page('DARK_STAGE'):
            if not self.vision.page('DARK_MAP'):
                if not self.vision.page('DARK_HUB'):
                    self.click_dark_entry(lambda:self.vision.page('DARK_HUB'),'进入暗涌协议总览')
                self.click('DARK_HUB','dark_hub_research',lambda:self.vision.page('DARK_MAP'),'进入同频研发')
            self.task.info_set('当前状态','正在定位 04-3')
            for attempt in range(2):
                try:
                    self.click('DARK_MAP','dark_map',lambda:self.vision.page('DARK_STAGE'),'进入 04-3',reobserve=True)
                    break
                except RecognitionTimeout:
                    self.refresh()
                    if self.vision.page('DARK_STAGE'):break
                    if attempt or not self.vision.page('DARK_MAP'):raise
                    self.emit('navigation_retry',target='04-3')
            self.require('DARK_STAGE')
        for i in range(completed,self.cfg['dark_tide_sweep_count']):
            self.click('DARK_STAGE','dark_sweep',lambda:self.vision.page('DARK_DIALOG') or
                       self.vision.page('MODULE_CAPACITY'),'打开扫荡确认')
            if self.vision.page('MODULE_CAPACITY'):return self.skip_dark_capacity()
            try:
                self.click('DARK_DIALOG','dark_start',lambda:self.vision.page('REWARD'),f'开始扫荡 {i+1}',
                           precondition=self.check_dark_resource)
            except InsufficientResource as error:
                self.snapshot('dark_resource_insufficient')
                # Verified dialog blank zone below its bottom edge, outside all
                # stage buttons. This dismisses; it never refills or submits.
                self.click('DARK_DIALOG','dark_dialog',lambda:self.vision.page('DARK_STAGE'),
                    '取消资源不足的扫荡',reward_blank=True)
                self.home('DARK_STAGE')
                return f'BLOCKED：{error}；已取消并返回指挥室'
            self.reward('DARK_STAGE')
            self.task.info_set('暗涌完成次数',i+1)
            self.task.info_set('暗涌进度',f"{i+1}/{self.cfg['dark_tide_sweep_count']}")
            self.results[State.DARK_TIDE]=f'IN_PROGRESS：已确认 {i+1} 次扫荡'
        self.home('DARK_STAGE')

    def skip_dark_capacity(self):
        self.snapshot('module_capacity_full')
        self.click('MODULE_CAPACITY','module_capacity_cancel',lambda:self.vision.page('DARK_STAGE'),
                   '取消模组库存已满提示')
        self.home('DARK_STAGE')
        return 'BLOCKED：战术模组库存空间不足；请手动清理，未扫荡，已取消并返回指挥室'

    def check_dark_resource(self):
        # Executed on the final frame AFTER any manual step pause, before spending.
        texts=self.task.ocr(x=.491,y=.615,to_x=.56,to_y=.655,frame=self.task.frame,threshold=.85)
        pairs=[re.fullmatch(r'(\d+)\s*/\s*(\d+)',b.name.replace(' ','')) for b in texts]
        pairs=[m for m in pairs if m]
        self.emit('resource_ocr',texts=[{'text':b.name,'confidence':float(b.confidence)} for b in texts])
        if len(pairs)!=1 or int(pairs[0][2])!=60:
            raise SafetyStop('04-3 体力或消耗数值不明，停止输入')
        if int(pairs[0][1])<60:
            raise InsufficientResource(f'04-3 体力{int(pairs[0][1])}不足60')
        self.emit('resource_check',available=int(pairs[0][1]),cost=60)

    def free_sweep(self,page,entry,button):
        stage='gan_stage' if page=='GANHUO' else 'hunt_stage'
        resumed=self.vision.page('FREE_DIALOG') and self.vision.find(stage)
        if not resumed:
            if not self.vision.page(page):
                if not self.vision.page('RESOURCE_HUB'):
                    self.click('MAIN_MENU','main_nav',lambda:self.vision.page('NAV'),'进入导航')
                    if self.optional_entry('NAV',entry):
                        self.click('NAV',entry,lambda:self.vision.page(page),'进入 '+page)
                    else:
                        self.click('NAV','nav_resources',lambda:self.vision.page('RESOURCE_HUB'),
                                   '待办入口消失后核对抢滩行动')
                if self.vision.page('RESOURCE_HUB'):
                    target='resource_gan' if page=='GANHUO' else 'resource_hunt'
                    self.click('RESOURCE_HUB',target,lambda:self.vision.page(page),'进入 '+page)
            self.wait(lambda:self.vision.page(page) and self.vision.free_count() is not None,'检查免费次数')
            if self.vision.free_count()==0:
                self.snapshot('free_count_exhausted')
                self.home(page);return 'DONE_CHECK：今日免费次数0/1已用完'
            self.click(page,button,lambda:self.vision.page('FREE_DIALOG'),'打开免费扫荡')
        # FREE_DIALOG requires the observed 免费次数 1/1 label; never spend a ticket.
        self.require('FREE_DIALOG')
        start='free_start' if page!='GANHUO' else (
            'free_start_gan_current' if self.vision.find('free_start_gan_current') else 'free_start_gan')
        self.click('FREE_DIALOG',start,lambda:self.vision.page('REWARD'),'开始一次免费扫荡')
        self.reward(page)
        self.wait(lambda:self.vision.page(page) and self.vision.free_count()==0,'验证免费次数已扣除')
        self.home(page)

    def ganhuo(self):
        return self.free_sweep('GANHUO','nav_gan','gan_sweep')

    def hunt_gold(self):
        return self.free_sweep('HUNT_GOLD','nav_hunt','hunt_sweep')

    def daily_reward(self):
        if not self.vision.page('DAILY_REWARD') and not self.vision.page('DAILY_PLAN'):
            self.click('MAIN_MENU','main_daily',lambda:self.vision.page('DAILY_REWARD') or
                       self.vision.page('DAILY_PLAN'),'进入每日调度')
        if self.vision.page('DAILY_PLAN'):
            self.click('DAILY_PLAN','daily_tab',lambda:self.vision.page('DAILY_REWARD'),'切换每日调度页签')
        self.require('DAILY_REWARD')
        # A glowing claim button can miss a single template frame. Do not
        # commit to the already-claimed branch until fresh observations prove
        # either an actionable button or independently readable activity.
        self.wait(lambda:self.vision.page('DAILY_REWARD') and
                  (self.vision.find('daily_claim') or self.daily_activity() is not None),
                  '核对每日领取按钮或活跃值')
        global_claimed=False
        if self.vision.find('daily_claim'):
            self.click('DAILY_REWARD','daily_claim',lambda:self.vision.page('REWARD') or
                (self.vision.page('DAILY_REWARD') and not self.vision.find('daily_claim')),'一键领取每日奖励')
            if self.vision.page('REWARD'): self.reward('DAILY_REWARD')
            global_claimed=True
        # One current button collects every eligible daily list reward globally.
        # Incomplete quest ratios are not prerequisites for stage rewards.
        self.wait(lambda:self.vision.page('DAILY_REWARD') and
                  self.vision.find('daily_activity_icon') and not self.vision.find('daily_claim'),
                  '确认每日全局领取按钮已消失')
        self.snapshot('daily_global_claim_checked')
        self.emit('daily_global_claim_verified',claimed=global_claimed,button_visible=False,
                  page='DAILY_REWARD',anchor='daily_activity_icon')
        self.wait(lambda:self.vision.page('DAILY_REWARD') and self.daily_activity() is not None,
                  '读取每日活跃值')
        claimed=[]
        for _ in range(5):
            self.require('DAILY_REWARD')
            activity=self.daily_activity()
            if activity is None:raise RecognitionTimeout('阶段领取前活跃值不明')
            feedback=self.read_daily_stages(activity)
            target=next((row for row in feedback if row['status']=='CLAIMABLE'),None)
            if target is None:break
            node,roi=target['node'],target['roi']
            self.click('DAILY_REWARD','gift',lambda:self.vision.page('REWARD') or
                (self.vision.page('DAILY_REWARD') and not self.vision.find('gift',roi)),f'领取阶段 {node}',roi=roi,
                precondition=lambda:self.verify_daily_stage(node,roi))
            if self.vision.page('REWARD'): self.reward('DAILY_REWARD')
            claimed.append(node)
            # Claiming may collect several nodes and move the strip. Locate all
            # labels again, never reuse the pre-click positions for another node.
        self.require('DAILY_REWARD')
        activity=self.daily_activity()
        if activity is None:raise RecognitionTimeout('每日最终活跃值不明')
        final=self.read_daily_stages(activity)
        if any(row['node']<=activity and row['status']!='CLAIMED' for row in final):
            raise RecognitionTimeout('已解锁阶段尚未证明已领取')
        self.snapshot('daily_stages_after_claim')
        self.home('DAILY_REWARD')
        return f"{'SUCCESS' if global_claimed or claimed else 'DONE_CHECK'}：全局领取按钮已消失；活跃值{activity}，已解锁阶段均显示已获得"

    def verify_daily_stage(self,node,roi):
        from src.daily_reward_screen import stages
        activity=self.daily_activity()
        if activity is None:raise RecognitionTimeout('阶段点击前活跃值不明，拒绝领取')
        current=next((row for row in stages(self,activity) if row['node']==node),None)
        if current is None or current['status']!='CLAIMABLE':
            raise SafetyStop(f'阶段{node}当前不可领取，拒绝旧目标')
        if len(roi)!=4 or any(abs(old-new)>6 for old,new in zip(roi,current['roi'])):
            raise SafetyStop(f'阶段{node}奖励条位置变化，拒绝旧区域')
        self.emit('daily_stage_input_verified',node=node,activity=activity,
                  expected_roi=list(roi),current_roi=current['roi'],confidence=current['confidence'])
        return True

    def read_daily_stages(self,activity):
        from src.daily_reward_screen import stages,StageOffscreen
        for attempt in range(3):
            try:return stages(self,activity)
            except StageOffscreen as error:
                if attempt==2:raise
                # Drag in the observed gap between reward cards. Wheel scrolling
                # controls the quest list, not this horizontal reward strip.
                before=getattr(self.task,'before_click',None)
                if before:before()
                self.require('DAILY_REWARD');self.task.assert_foreground();self.checkpoint()
                try:return stages(self,activity)
                except StageOffscreen as current:error=current
                xs=sorted(x for x in error.anchors.values() if 1585<=x<=3575)
                if len(xs)<2:raise RecognitionTimeout('奖励条缺少可验证拖动锚点')
                start=round((xs[-1]+xs[-2])/2)
                shift=round((xs[-1]-xs[-2])*.8)*(1 if error.node<600 else -1)
                self.snapshot('before_stage_drag')
                self.emit('daily_stage_drag',node=error.node,attempt=attempt+1,
                          start=[start,1940],end=[start+shift,1940],anchors=error.anchors)
                from src.roguelike_control import Reobserve
                from src.cooperative import FocusChanged
                try:self.task.swipe(start,1940,start+shift,1940,duration=300)
                except (Reobserve,FocusChanged):
                    self.checkpoint();continue
                after=getattr(self.task,'after_input',None)
                if after:after()
                self.delay(self.cfg['click_min_wait']);self.require('DAILY_REWARD')

    def check_daily_list(self):
        """Prove the remaining list is incomplete, rather than trusting absence."""
        self.scroll_daily(20,'每日领取核对从顶部开始')
        for screen in range(10):
            self.require('DAILY_REWARD')
            if self.vision.find('daily_claim'):
                self.click('DAILY_REWARD','daily_claim',lambda:self.vision.page('REWARD') or
                    (self.vision.page('DAILY_REWARD') and not self.vision.find('daily_claim')),'补领每日列表奖励')
                if self.vision.page('REWARD'):self.reward('DAILY_REWARD')
                continue
            before=self.daily_list_signature()
            self.scroll_daily(-4,'检查每日列表后续领取项')
            after=self.daily_list_signature()
            self.emit('daily_list_scan',screen=screen+1,before=before,after=after)
            if before==after:
                self.refresh()
                if self.daily_list_signature()==after and not self.vision.find('daily_claim'):
                    self.snapshot('daily_list_complete');return
        raise RecognitionTimeout('每日列表超过十屏，未证明领取完整')

    def daily_list_signature(self):
        self.require('DAILY_REWARD')
        labels=self.task.ocr(x=.33,y=.29,to_x=.93,to_y=.72,frame=self.task.frame,threshold=.93)
        ratios=[(b,m) for b in labels if (m:=re.fullmatch(r'(\d+)\s*/\s*(\d+)',b.name))]
        ratios=[(box,match) for box,match in ratios if 735<=round(box.y+box.height/2)<=1485]
        if not ratios:
            if not self.vision.find('daily_all'):raise RecognitionTimeout('每日列表状态不可读')
        for box,match in ratios:
            center=round(box.y+box.height/2)
            if int(match[2])<=0:raise RecognitionTimeout('每日列表进度分母异常')
            if int(match[1])>=int(match[2]):
                roi=[3360,max(645,center-90),3720,min(1560,center+90)]
                if not self.vision.find('daily_row_claimed',roi):
                    raise RecognitionTimeout('每日列表有达成项，但领取状态未证明')
        return tuple((b.name.replace(' ',''),round(b.y/30)) for b in labels
                     if re.search('[\u4e00-\u9fff]|/',b.name))

    def scroll_daily(self,count,label):
        self.scroll_verified('DAILY_REWARD',3000,1100,count,label)

    def daily_activity(self):
        if not self.vision.page('DAILY_REWARD') or not self.vision.find('daily_activity_icon'):
            return None
        bottom=self.task.ocr(x=.285,y=.86,to_x=.36,to_y=.915,frame=self.task.frame,threshold=.95)
        totals=[int(b.name) for b in bottom if b.confidence>=.95 and re.fullmatch(r'\d{1,4}',b.name)]
        # The top x/1000 is level experience and resets on level-up. Daily
        # activity is the independently anchored badge below the reward strip.
        if len(totals)==1:
            self.emit('daily_activity',value=totals[0],source='activity_badge',
                      bottom=[{'text':b.name,'confidence':float(b.confidence)} for b in bottom])
            return totals[0]
        return None
