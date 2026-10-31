"""Preparation only. No experiment-start, dismantle, or team-edit action exists here."""
import re
import hashlib
import time
from uuid import uuid4
from copy import deepcopy
import cv2
import numpy as np
from ok import Box
from src.workflow import SafetyStop


def rect(values):
    return tuple(round(v*1.875) for v in values)


TEAM_ROI=rect((1520,275,1950,695))
MODULE_LIST_ROI=(1369,506,3656,1725)
MODULE_SCROLL_POINTS=((3000,1300),(1650,570))


def list_motion(before,after,count):
    """Require coherent vertical motion in native card pixels, not average change."""
    if before.shape!=after.shape:raise SafetyStop('模组列表捕获尺寸变化，停止滚动')
    delta=cv2.absdiff(before,after)
    result={'difference':round(float(delta.mean()),3),
            'changed_pixels':int(np.count_nonzero(delta)),
            'displacement_native_y':None,'motion_support':[],'outcome':'list_temporarily_stationary'}
    if not np.any(delta):return result
    result['outcome']='frame_changed_without_scroll'
    height,width=before.shape
    # Thin edge patches retain overlap even when four wheel ticks move almost
    # one viewport; keep native rows so a one/two-pixel movement survives.
    ph,pw=min(72,height//12),min(160,width//7)
    if min(ph,pw)<8:return result
    votes=[]
    for row,cy in enumerate((ph//2+2,height//3,2*height//3,height-ph//2-2)):
        for col,cx in enumerate((width//6,width//2,5*width//6)):
            x,y=cx-pw//2,cy-ph//2
            patch=before[y:y+ph,x:x+pw]
            if float(patch.std())<6:continue
            top,bottom=0,height
            left,right=max(0,x-3),min(width,x+pw+3)
            scores=cv2.matchTemplate(after[top:bottom,left:right],patch,cv2.TM_CCOEFF_NORMED)
            _,score,_,point=cv2.minMaxLoc(scores)
            dx,dy=point[0]+left-x,point[1]+top-y
            zero=float(scores[y-top,x-left])
            if score>=.90 and score-zero>=.02 and abs(dx)<=3 and dy*count>0:
                votes.append({'row':row,'column':col,'dy':dy,'dx':dx,'score':round(score,4)})
    # An animated icon/one changed card cannot authorize another wheel input.
    for vote in votes:
        group=[v for v in votes if abs(v['dy']-vote['dy'])<=1]
        if len(group)>=3 and len({v['column'] for v in group})>=2:
            result.update(outcome='list_displaced',
                displacement_native_y=float(np.median([v['dy'] for v in group])),motion_support=group)
            break
    return result


def has_level(texts):
    return any(match and int(match[1])>0 and getattr(b,'confidence',1)>=.8 for b in texts
               if (match:=re.fullmatch(r'(?i)L?[VY][.·:]?(\d{1,3})',b.name.replace(' ',''))))


def occupied_slot(task,row,col):
    # Different portraits confuse OCR detection. Retry the isolated level strip,
    # then its enlarged grayscale version; never infer occupancy from no plus alone.
    x,y,x2,y2=rect((1523+142*col,278+142*row,1662+142*col,405+142*row))
    native=getattr(task,'native_frame',None)
    templates=getattr(task,'native_templates',{})
    level_templates={name:len(values) for name,values in templates.items()
                     if re.fullmatch(r'module_team_level_\d{1,3}',name)}
    evidence={'row':row,'col':col,'occupied':False,'producer':None,'ocr_attempts':[],
              'glyph_attempts':[],'level_templates':level_templates,
              'native_shape':list(native.shape) if isinstance(native,np.ndarray) else None,
              'geometry':list(getattr(getattr(task,'geometry',None),'size',()) or ()),
              'capture_metadata':deepcopy(getattr(task,'metadata',None))}
    def failed(reason):
        evidence['failure_reason']=reason
        evidence['native_frame_sha256']=hashlib.sha256(native.tobytes()).hexdigest() if isinstance(native,np.ndarray) else None
        reference=task.frame
        evidence['reference_frame_sha256']=hashlib.sha256(reference.tobytes()).hexdigest() if isinstance(reference,np.ndarray) else None
        evidence['reference_matches_native']=(bool(np.array_equal(reference,cv2.resize(native,(3840,2160))))
            if isinstance(native,np.ndarray) and tuple(native.shape[1::-1])==(1280,720)
            and isinstance(reference,np.ndarray) and tuple(reference.shape[1::-1])==(3840,2160) else None)
        evidence['source_packet_unchanged']=(getattr(task,'native_frame',None) is native
            and getattr(task,'metadata',None)==evidence['capture_metadata'])
        evidence['glyph_best_score']=max((a['score'] for a in evidence['glyph_attempts']),default=None)
        task.module_slot_failure=evidence
        return False
    def observed(producer,texts):
        evidence['ocr_attempts'].append({'producer':producer,'threshold':.8,
            'texts':[{'text':b.name,'confidence':getattr(b,'confidence',None)} for b in texts]})
        return texts
    if isinstance(native,np.ndarray) and tuple(native.shape[1::-1])==(1280,720):
        left,top,right,bottom=task.geometry.roi((x,y,x2,y2))
        portrait=native[top+26:bottom-12,left+6:right-6]
        gray=cv2.cvtColor(portrait,cv2.COLOR_BGR2GRAY) if portrait.size else None
        evidence['portrait_std']=float(gray.std()) if gray is not None else 0.
        if evidence['portrait_std']<12:return failed('portrait_texture_below_12')
    def accepted(producer,texts=None,score=None):
        evidence.update(occupied=True,producer=producer,score=score,
                        texts=[{'text':b.name,'confidence':getattr(b,'confidence',None)} for b in texts or []])
        if not hasattr(task,'module_slot_evidence'):task.module_slot_evidence={}
        task.module_slot_evidence[f'{row+1}-{col+1}']=evidence
        return True
    texts=task.ocr(x=x/3840,y=y/2160,to_x=x2/3840,to_y=y2/2160,
                   frame=task.frame,threshold=.8)
    observed('slot_ocr',texts)
    if has_level(texts):return accepted('slot_ocr',texts)
    x,y,x2,y2=rect((1570+142*col,281+142*row,1656+142*col,320+142*row))
    texts=task.ocr(x=x/3840,y=y/2160,to_x=x2/3840,to_y=y2/2160,
                   frame=task.frame,threshold=.8)
    observed('level_strip_ocr',texts)
    if has_level(texts):return accepted('level_strip_ocr',texts)
    if isinstance(native,np.ndarray) and tuple(native.shape[1::-1])==(1280,720):
        left,top,right,bottom=task.geometry.roi((x,y,x2,y2))
        source=native[top:bottom,left:right]
        for scale,grayscale in ((2,False),(2,True),(3,False)):
            crop=source
            if grayscale:crop=cv2.cvtColor(cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY),cv2.COLOR_GRAY2BGR)
            crop=cv2.resize(crop,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
            crop=cv2.copyMakeBorder(crop,20,20,20,20,cv2.BORDER_REPLICATE)
            texts=task.ocr(frame=crop,threshold=.8)
            observed(f'native_{"gray" if grayscale else "color"}_{scale}x',texts)
            if has_level(texts):return accepted(f'native_{"gray" if grayscale else "color"}_{scale}x',texts)
        # Only a verified positive level glyph may bypass unread OCR, within
        # this slot's own strip. No portrait or missing-plus match substitutes.
        for name,templates in getattr(task,'native_templates',{}).items():
            match=re.fullmatch(r'module_team_level_(\d{1,3})',name)
            if not match or int(match[1])<=0:continue
            for template,minimum in templates:
                if source.shape[0]<template.shape[0] or source.shape[1]<template.shape[1]:continue
                score=float(cv2.matchTemplate(source,template,cv2.TM_CCOEFF_NORMED).max())
                evidence['glyph_attempts'].append({'template':name,'threshold':max(.97,minimum),
                    'score':score,'template_shape':list(template.shape),'roi_native':[left,top,right,bottom]})
                if score>=max(.97,minimum):return accepted('level_glyph_'+match[1],score=score)
        # This support strip's current white text is read as LW60 by CUBIC.
        # One bounded same-packet retry may supply a complete positive literal;
        # existing successful producers and the strict has_level rule stay first.
        if (row,col)==(2,2):
            crop=cv2.resize(source,None,fx=3,fy=3,interpolation=cv2.INTER_LINEAR)
            crop=cv2.copyMakeBorder(crop,24,24,24,24,cv2.BORDER_REPLICATE)
            texts=task.ocr(frame=crop,threshold=.8)
            observed('native_color_linear_3x',texts)
            if has_level(texts):return accepted('native_color_linear_3x',texts)
        return failed('no_positive_level_evidence')
    crop=task.frame[y:y2,x:x2]
    gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
    gray=cv2.resize(gray,None,fx=2,fy=2,interpolation=cv2.INTER_CUBIC)
    gray=cv2.copyMakeBorder(gray,20,20,20,20,cv2.BORDER_REPLICATE)
    texts=task.ocr(frame=cv2.cvtColor(gray,cv2.COLOR_GRAY2BGR),threshold=.8)
    observed('reference_gray_2x',texts)
    return accepted('reference_gray_2x',texts) if has_level(texts) else failed('no_positive_level_evidence')


def check_team(vision):
    # User explicitly requires every slot occupied, including the support slot.
    if vision.find('module_plus',TEAM_ROI) or vision.find('module_plus_partial',TEAM_ROI):
        raise SafetyStop('研发队员存在加号空位：请手动补齐全部队员后重新运行。')
    for row in range(3):
        for col in range(3):
            if not occupied_slot(vision.task,row,col):
                raise SafetyStop(f'第{row+1}排第{col+1}格队员识别失败（不是检测到加号）。请重新运行或生成问题报告。')


class ModulePreparation:
    def __init__(self,flow):
        self.f=flow
        self.v=flow.vision
        self.profile=getattr(flow,'cfg',{}).get('制作配置','旧地联合')

    def module_ready(self):
        if self.profile=='旧地联合':return self.v.find('module_old_ready')
        title=self.v.find('module_name_'+self.profile,(100,320,1030,1050))
        if title or self.profile not in ('先攻','军工','一致'):return title
        # These captured 720P preparation pages omit the name. Each specific
        # brand was bound to its own selected-detail and confirmed embryo.
        brand='module_ready_'+self.profile+'_brand'
        native=getattr(getattr(self.v,'task',None),'native_frame',None)
        if (not isinstance(native,np.ndarray) or tuple(native.shape[1::-1])!=(1280,720)
                or brand not in getattr(self.v,'images',{})
                or not self.v.page('MODULE_RESEARCH')):return None
        return self.v.find(brand,(540,630,960,1110))

    def verify_team(self):
        diagnostic_epochs=set()
        def ready():
            if not self.v.page('MODULE_RESEARCH'):
                raise SafetyStop('队员复核时页面变化，停止操作')
            try:
                check_team(self.v)
                return True
            except SafetyStop as error:
                # The core portrait has a moving gold shine over its level label.
                # Retry fresh frames only; an actual empty slot still stops immediately.
                if '识别失败' in str(error):
                    evidence=getattr(getattr(self.v,'task',None),'module_slot_failure',None)
                    if isinstance(evidence,dict):
                        metadata=evidence.get('capture_metadata') or {}
                        epoch=(metadata.get('session_id'),metadata.get('sequence')) if metadata.get('session_id') is not None and metadata.get('sequence') is not None else ('pixels',evidence.get('native_frame_sha256'))
                        if epoch not in diagnostic_epochs and len(diagnostic_epochs)<3:
                            diagnostic_epochs.add(epoch)
                            self.f.emit('module_team_slot_unreadable',failure=str(error),
                                diagnostic_attempt=len(diagnostic_epochs),maximum_attempts=3,**deepcopy(evidence))
                    return False
                raise
        self.f.wait(ready,'等待队员扫光结束并复核全部槽位')

    def candidates(self,name):
        x,y,x2,y2=self.v.manifest['templates'][name]['roi']
        matches=self.f.task.find_feature(name,template=self.v.images[name],
            box=Box(x,y,x2-x,y2-y),threshold=self.f.cfg['match_threshold'],frame=self.f.task.frame)
        return sorted(matches,key=lambda b:(round(b.y/40),b.x))

    def pin(self,name,box):
        # Preserve the selected candidate's location across click-time re-observation.
        return (round(box.x-4),round(box.y-4),round(box.x+box.width+4),round(box.y+box.height+4))

    def _scroll_modules_once(self,count,point):
        from src.cooperative import FocusChanged
        from src.roguelike_control import Reobserve
        from src.input_receipt import receipt_status
        f=self.f
        action_id=str(uuid4())
        for attempt in range(2):
            receipt=None
            try:
                f.task.before_click()
                f.refresh()
                if not self.v.page('MODULE_SELECTOR'):raise SafetyStop('模组列表页面变化，停止滚动')
                before=self.list_pixels()
                before_capture=deepcopy(getattr(f.task,'metadata',None))
                before_hash=hashlib.sha256(before.tobytes()).hexdigest()
                if hasattr(f,'snapshot'):f.snapshot('module_scroll_'+action_id+'_before')
                f.task.assert_foreground();f.checkpoint();f.task.validate_click_context()
                started=time.perf_counter_ns()
                receipt=f.task.scroll(*point,count)
                status=receipt_status(receipt)
                f.emit('module_list_input',action_id=action_id,
                    send_status=status,monotonic_ns=started,completed_monotonic_ns=time.perf_counter_ns(),clock='perf_counter_ns',
                    point=list(point),client_point=list(f.task.geometry.point(*point)),count=count,
                    target_identity=deepcopy(getattr(f,'target_identity',None)),
                    before_capture=before_capture,before_list_sha256=before_hash,
                    receipt=receipt.as_dict() if hasattr(receipt,'as_dict') else None)
                if status!='native_submitted':
                    if hasattr(f,'snapshot'):f.snapshot('module_scroll_'+action_id+'_'+status)
                    raise SafetyStop('模组列表滚轮'+{'not_sent':'明确未发送','partial':'部分发送','unknown':'发送结果未知'}[status]+'；停止，不自动重放')
                break
            except (FocusChanged,Reobserve) as error:
                # A post-dispatch cache/focus exception carries its native
                # receipt. Re-observation never turns a partial/unknown/already
                # submitted wheel sequence into permission to send it again.
                interrupted=getattr(error,'input_receipt',None)
                if interrupted is None:interrupted=receipt
                if interrupted is not None:
                    status=receipt_status(interrupted)
                    f.emit('module_list_input',action_id=action_id,send_status=status,
                        monotonic_ns=time.perf_counter_ns(),clock='perf_counter_ns',point=list(point),count=count,
                        receipt=interrupted.as_dict() if hasattr(interrupted,'as_dict') else None,
                        interruption=type(error).__name__,input_replayed=False)
                    if status!='not_sent':
                        if hasattr(f,'snapshot'):f.snapshot('module_scroll_'+action_id+'_interrupted_'+status)
                        raise SafetyStop('模组列表滚轮提交后观察中断，发送状态为'+status+'；停止，不自动重放') from error
                if attempt:raise
                f.emit('scroll_reobserve',step='模组胚子列表')
                f.checkpoint()
        f.task.after_input()
        # Request a new frame after dispatch and invalidate Vision's frame cache.
        # A slow/no-op first wheel event is not evidence that the list is bottom.
        # Local list pixels are enough here; do not OCR every module on each poll.
        for observation in range(10):
            f.refresh()
            if not self.v.page('MODULE_SELECTOR'):raise SafetyStop('模组列表页面变化，停止滚动')
            after=self.list_pixels()
            motion=list_motion(before,after,count)
            if motion['outcome']=='list_displaced':break
            if observation<9:f.delay(.08)
        changed=motion['outcome']=='list_displaced'
        if hasattr(f,'snapshot'):f.snapshot('module_scroll_'+action_id+'_after')
        f.emit('module_list_scroll',direction='up' if count>0 else 'down',changed=changed,
               action_id=action_id,send_status=status,receipt=receipt.as_dict() if hasattr(receipt,'as_dict') else None,
               point=list(point),observations=observation+1,**motion,
               before_capture=before_capture,after_capture=deepcopy(getattr(f.task,'metadata',None)),
               before_list_sha256=before_hash,after_list_sha256=hashlib.sha256(after.tobytes()).hexdigest(),
               bottom_verified=False)
        self.last_scroll_evidence=motion|{'bottom_verified':False,'send_status':status}
        return changed

    def scroll_modules(self,count=-4):
        changed=self._scroll_modules_once(count,MODULE_SCROLL_POINTS[0])
        if changed:return True
        # Reversible input only: after bounded fresh observations with no displacement,
        # retry once at a second interior point. Dispatch errors/pause never
        # enter this recovery, and the next card search reads the final frame.
        self.f.emit('module_list_scroll_recovery',reason=self.last_scroll_evidence['outcome'],count=count)
        self.f.delay(.25)
        changed=self._scroll_modules_once(count,MODULE_SCROLL_POINTS[1])
        if not changed:self.f.emit('module_list_no_progress',direction='up' if count>0 else 'down',
            bottom_verified=False,outcome=self.last_scroll_evidence['outcome'],
            evidence='two_native_submissions+bounded_fresh_observations_without_coherent_displacement')
        return changed

    def list_pixels(self):
        # Compare the actual list, excluding the animated detail portrait. The
        # existing ReferenceTask converts this reference region to native pixels.
        task=self.f.task
        frame=getattr(task,'native_frame',None)
        if frame is None:frame=task.frame
        l,t,r,b=task.geometry.roi(MODULE_LIST_ROI)
        crop=frame[t:b,l:r]
        if not crop.size:raise SafetyStop('模组列表截图为空，停止滚动')
        return cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)

    def select_module(self):
        f=self.f
        if self.profile!='旧地联合':return self.select_named_module()
        target='module_choose' if self.v.find('module_choose') else 'module_replace'
        f.click('MODULE_RESEARCH',target,lambda:self.v.page('MODULE_SELECTOR'),'打开模组胚子列表')
        self.scroll_modules(40)
        for _ in range(8):
            f.require('MODULE_SELECTOR')
            matches=self.candidates('module_eagle')
            if matches:
                area=self.pin('module_eagle',matches[0])
                f.click('MODULE_SELECTOR','module_eagle',
                    lambda:self.v.page('MODULE_SELECTOR') and self.v.find('module_old_title'),
                    '选择首个旧地胚子并核对旧地联合详情',roi=area)
                def guard():
                    if not self.v.find('module_old_title'):raise SafetyStop('无法确认选中的是旧地胚子')
                f.click('MODULE_SELECTOR','module_confirm',lambda:self.v.page('MODULE_RESEARCH'),
                        '确认旧地胚子',precondition=guard)
                return
            self.scroll_modules()
        raise SafetyStop('未找到旧地胚子（已检查最多8屏），请检查模组库存及筛选条件。')

    def _choose_named_module(self,matches,screen,scroll_count,search_stalled,list_changed,probe_only):
        f=self.f;name='module_name_'+self.profile
        title=lambda:self.v.find(name,(122,338,788,497))
        for match in matches:
            area=self.pin(name,match)
            f.click('MODULE_SELECTOR',name,lambda:self.v.page('MODULE_SELECTOR') and title(),
                    '选择'+self.profile+'胚子并核对名称',roi=area)
            if probe_only or self.empty_selected_embryo():break
            f.emit('module_existing_effects_skipped',profile=self.profile,
                   reason='未取得无词条效果的明确证据；不以已制成模组作为新胚子')
        else:return False
        return self._confirm_named_module(screen,scroll_count,search_stalled,list_changed,probe_only)

    def _confirm_named_module(self,screen,scroll_count,search_stalled,list_changed,probe_only,
                              identity_source='list_name+selected_detail_name'):
        f=self.f;name='module_name_'+self.profile
        title=lambda:self.v.find(name,(122,338,788,497))
        def guard():
            if not title():raise SafetyStop('无法确认选中的是'+self.profile+'胚子')
            if not probe_only and not self.empty_selected_embryo():
                raise SafetyStop('选中胚子未明确显示无词条效果，拒绝覆盖已有模组。')
        guard()
        f.emit('module_identity_verified',profile=self.profile,screen=screen+1,
               evidence=identity_source,probe_only=probe_only)
        f.snapshot('module_identity_'+self.profile)
        if probe_only:return {'profile':self.profile,'screens':screen+1,
            'identity_verified':True,'scroll_count':scroll_count,
            'reached_bottom':False,'search_stalled':search_stalled,'list_changed':list_changed,
            # No endpoint marker has been calibrated. Native submission plus
            # unchanged pixels cannot prove the list's true bottom.
            'bottom_verified':False,'boundary_evidence':'unverified_no_displacement' if search_stalled else None}
        f.click('MODULE_SELECTOR','module_confirm',lambda:self.v.page('MODULE_RESEARCH'),
                '确认'+self.profile+'胚子',precondition=guard)
        return True

    def empty_selected_embryo(self):
        task=getattr(self.v,'task',None)
        native=getattr(task,'native_frame',None)
        if self.profile not in ('军工','一致') or not isinstance(native,np.ndarray) or native.shape!=(720,1280,3):
            return True  # Preserve other profiles and non-device policy adapters.
        if not self.v.page('MODULE_SELECTOR'):return False
        labels=task.ocr(x=.025,y=.73,to_x=.32,to_y=.855,frame=task.frame,threshold=.93)
        return any(re.sub(r'\s+','',b.name)=='无词条效果' and getattr(b,'confidence',0)>=.93 for b in labels)

    def select_named_module(self,probe_only=False,verify_bottom=False):
        f=self.f
        target='module_choose' if self.v.find('module_choose') else 'module_replace'
        f.click('MODULE_RESEARCH',target,lambda:self.v.page('MODULE_SELECTOR'),'打开模组胚子列表')
        # Search the current page first. Only the explicit boundary probe resets
        # to the top; normal target lookup advances downward without previewing
        # cards whose name or own icon does not match the requested profile.
        if verify_bottom:self.scroll_modules(40)
        scroll_count=0;list_changed=False
        for screen in range(65):
            f.require('MODULE_SELECTOR')
            matches=self.v.module_name_matches(self.profile,MODULE_LIST_ROI)
            # The acceptance probe can require a real trip to the bottom even
            # if its target becomes visible one screen earlier.
            if matches and not verify_bottom:
                selected=self._choose_named_module(matches,screen,scroll_count,False,list_changed,probe_only)
                if selected is not False:return selected
            if screen==64:break
            changed=self.scroll_modules();scroll_count+=1
            list_changed=list_changed or changed is True
            if changed is False:
                # The last scroll may have rendered its target after our
                # pre-scroll search, so always re-read the final current frame.
                bottom_matches=self.v.module_name_matches(self.profile,MODULE_LIST_ROI)
                if bottom_matches:
                    selected=self._choose_named_module(bottom_matches,screen+1,scroll_count,True,list_changed,probe_only)
                    if selected is not False:return selected
                f.snapshot('module_list_boundary_unverified_'+self.profile)
                f.emit('module_list_target_not_found',profile=self.profile,scroll_count=scroll_count,
                    bottom_verified=False,reason='bounded_search_stalled_endpoint_unverified')
                raise SafetyStop('模组列表无位移，底部未验证，仍未读到'+self.profile+'；已保留截图与识别证据，未确认胚子。')
        f.snapshot('module_list_search_exhausted_'+self.profile)
        f.emit('module_list_target_not_found',profile=self.profile,scroll_count=scroll_count,
            bottom_verified=False,reason='bounded_search_limit')
        raise SafetyStop('未识别到'+self.profile+'胚子（已检查最多64次滚动），未确认胚子。')

    def ready(self):
        if self.profile=='旧地联合':
            return self.v.page('MODULE_RESEARCH') and self.v.has('module_old_ready','module_blue_selected')
        return self.v.page('MODULE_RESEARCH') and self.module_ready() and self.blueprint_ready()

    def blueprint_ready(self):
        """A missed route template is unknown, not an unselected blueprint."""
        marker=self.v.find('module_blue_selected')
        if marker:return marker
        if self.profile not in ('军工','一致') or not self.v.page('MODULE_RESEARCH'):return None
        task=getattr(self.v,'task',None)
        native=getattr(task,'native_frame',None)
        if not isinstance(native,np.ndarray) or native.shape!=(720,1280,3):return None
        # Independent positive evidence from the selected route: its own replace
        # control and labelled nodes. Never use the absence of the choose button.
        if not self.v.find('module_replace',(1290,300,1700,510)):return None
        labels=task.ocr(x=.43,y=.14,to_x=.62,to_y=.86,frame=task.frame,threshold=.93)
        texts={re.sub(r'\s+','',b.name) for b in labels if getattr(b,'confidence',0)>=.93}
        stages={'属性刻录','属性深度刻录','技能研习','技能深度研习','战术核心·属性','战术核心·技能'}
        return '开始刻录' in texts and bool(texts & stages)

    def verify_preparation_team(self):
        if self.profile not in ('军工','一致'):return self.verify_team()
        # These teams are user-managed. A positive empty-slot marker still
        # stops, but unreadable portrait levels cannot invalidate their team.
        if not self.v.page('MODULE_RESEARCH'):raise SafetyStop('队员复核时页面变化，停止操作')
        if self.v.find('module_plus',TEAM_ROI) or self.v.find('module_plus_partial',TEAM_ROI):
            raise SafetyStop('研发队员存在加号空位：请手动配置队员；程序不会修改配队。')

    def navigate(self,active=lambda:False):
        """Reach research without replacing a module already in progress."""
        f=self.f
        pages=('MAIN_MENU','DARK_HUB','DARK_MAP','MODULE_RESEARCH')
        current=f.wait(lambda:'ACTIVE' if active() else next((p for p in pages if self.v.page(p)),None),'识别模组准备入口')
        if current=='ACTIVE':return
        if current=='MAIN_MENU':
            f.click_dark_entry(lambda:self.v.page('DARK_HUB'),'进入暗涌协议')
            current='DARK_HUB'
        if current=='DARK_HUB':
            f.click('DARK_HUB','dark_hub_research',lambda:self.v.page('DARK_MAP'),'进入同频研发')
            current='DARK_MAP'
        if current=='DARK_MAP':
            f.click('DARK_MAP','module_entry',lambda:active() or self.v.page('MODULE_RESEARCH'),'进入模组研发')

    def run(self):
        f=self.f
        self.navigate()
        if not self.module_ready():self.select_module()
        f.require('MODULE_RESEARCH')
        self.verify_preparation_team()
        state=f.wait(lambda:'selected' if self.blueprint_ready() else
                     'unselected' if self.v.find('module_blue_choose') else None,
                     '核对蓝图已选路径或选择入口（模板未命中不代表未选择）')
        if state=='unselected':
            f.click('MODULE_RESEARCH','module_blue_choose',lambda:self.v.page('BLUEPRINT_SELECTOR') or self.ready(),
                    '打开研发蓝图',precondition=self.verify_preparation_team,allow_already_done=True)
        if state=='unselected' and not self.ready():
            f.require('BLUEPRINT_SELECTOR')
            matches=self.candidates('module_blue_card')
            if not matches:raise SafetyStop('没有识别到可用的配对蓝图，请检查蓝图库存。')
            f.click('BLUEPRINT_SELECTOR','module_blue_card',
                    lambda:self.ready() or (self.v.page('BLUEPRINT_SELECTOR') and self.v.find('module_confirm')),
                    '选择首个可用蓝图',roi=self.pin('module_blue_card',matches[0]))
            f.click('BLUEPRINT_SELECTOR','module_confirm',lambda:self.ready(),'确认研发蓝图',allow_already_done=True)
        f.wait(self.ready,'复核'+self.profile+'胚子和蓝图')
        self.verify_preparation_team()
        f.snapshot('module_ready')
