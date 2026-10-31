"""OCR observations in a fixed 2048x1152 coordinate system."""
import re
import hashlib
import json
import cv2
import numpy as np
from dataclasses import replace
from uuid import uuid4
from src.crafting_policy import Option,skill_name,skill_module,core_skill_name
from src.workflow import SafetyStop


class ChoiceUnreadable(SafetyStop):
    """A transient observation failure, safe to retry without replaying input."""


class SkillIdentityChanged(ChoiceUnreadable):
    """A complete current skill differs from the earlier choice; decide again."""


def compact(text):return re.sub(r'\s+','',text).replace('旋涡','漩涡').replace('漩涡','旋涡')


def group_scope(text):
    return tuple(g for g in ('火力','重盾','工程','医疗','突击','狙击','隐刃','指挥官') if g in text)


def group_attr(text):
    scope=group_scope(text)
    group=next((g for g in ('火力','重盾','工程','医疗','突击') if g in scope),'')
    if not group and any(g in scope for g in ('狙击','隐刃')):group='火力'
    if not group and '指挥官' in scope:group='指挥官'
    attr=next((a for a in ('最大生命','攻击','防御') if a in text),'')
    return group,attr


# The small increment icon is not business text. Accept a bounded non-Chinese,
# non-numeric decoration, rather than an ever-growing OCR character whitelist.
# Ratios, percentages, negatives, decimals and distant numbers are not increments.
ATTRIBUTE_VALUE = re.compile(r'(最大生命|攻击|防御)[^\u4e00-\u9fff\d+＋%％/／()（）\-−]{0,4}'
                             r'[+＋]([1-9]\d{0,5})(?![\d%％/／]|[.．]\d)')


def attribute_delta(text):
    matches=list(ATTRIBUTE_VALUE.finditer(compact(text)))
    return matches[0] if len(matches)==1 else None


def mission_reward(text):
    """Read a full reward inside its labelled region; special policy membership is separate."""
    if '目标达成增益' not in text:return '', '', (), '', 0
    reward=text.split('目标达成增益',1)[1]
    # This class name loses the small 刃 stroke under the selected-card glow.
    reward=reward.replace('隐刀队员','隐刃队员')
    match=attribute_delta(reward)
    if not match:return '', '', (), '', 0
    prefix=reward[:match.start()].strip('◇◆◈♦♢·•・Vv')
    label=re.fullmatch(r'([\u4e00-\u9fff/／、]{1,28}?)(?:队员)?',prefix)
    if not label:return '', '', (), '', 0
    scope=group_scope(label[1])
    group,attr=group_attr(label[1]+match[1])
    # An unfamiliar profession is readable text, without a special reward bonus.
    group=group or label[1]
    return group,attr,scope,reward[:match.end()],int(match[2])


class CraftScreen:
    def __init__(self,task,frame,page_only=False,native_frame=None):
        self.task=task
        self.native_frame=frame.copy()
        self.frame=cv2.resize(frame,(2048,1152))
        self.observation_epoch=str(uuid4())
        self.frame_id=self.observation_epoch
        self.frame_hash=hashlib.sha256(self.frame.tobytes()).hexdigest()
        self._lazy_pending=False
        self._light_kind=None
        self._light_conflict=False
        if page_only and isinstance(native_frame,np.ndarray) and native_frame.shape==(720,1280,3):
            light=self._page_kind(native_frame)
            if light:
                self.kind=light;self._light_kind=light;self._lazy_pending=True
                return
        self.ensure_full()

    def __getattribute__(self,name):
        if name in ('boxes','text','header','kind'):
            state=object.__getattribute__(self,'__dict__')
            if state.get('_light_conflict'):
                raise ChoiceUnreadable('当前局部页面与完整观察不一致，重新观察后才能操作')
            if name!='kind' and state.get('_lazy_pending'):
                object.__getattribute__(self,'ensure_full')()
        return object.__getattribute__(self,name)

    def ensure_full(self):
        if self.__dict__.get('_light_conflict'):
            raise ChoiceUnreadable('当前局部页面与完整观察不一致，重新观察后才能操作')
        if 'boxes' in self.__dict__ and not self.__dict__.get('_lazy_pending'):return
        self._lazy_pending=False
        self.boxes=self.task.ocr(frame=self.frame,threshold=.75)
        self.text=''.join(compact(b.name) for b in self.boxes)
        self.header=self.text_in((30,80,950,165))
        self.kind=self.classify()
        if self.__dict__.get('_light_kind') and self.kind!=self._light_kind:
            self._light_conflict=True
            raise ChoiceUnreadable('当前局部页面与完整观察不一致，拒绝使用局部字段')

    def _page_kind(self,native):
        # Three current-pixel strips, in one small OCR canvas. Unrecognised or
        # shaded titles always use the existing complete-frame classifier.
        regions=((18,48,600,106),(190,205,1100,330),(220,440,1050,565))
        strips=[];segments=[];offset=0
        for x,y,x2,y2 in regions:
            crop=native[y:y2,x:x2]
            strip=np.zeros((crop.shape[0]+12,1000,3),np.uint8)
            strip[:crop.shape[0],:crop.shape[1]]=crop
            strips.append(strip);segments.append((offset,offset+crop.shape[0],crop))
            offset+=strip.shape[0]
        canvas=np.concatenate(strips,axis=0)
        boxes=self.task.ocr(frame=canvas,threshold=.75)
        texts=[''.join(compact(b.name) for b in boxes
               if top<=b.y+b.height/2<bottom) for top,bottom,_ in segments]
        modal=texts[1]+texts[2]
        # A modal never borrows the underlying card title. Complete OCR remains
        # authoritative for refresh/exit dialogs, including partial dialog text.
        if any(word in modal for word in ('刷新','消耗模组','是否退出','快速智能研发')):return None
        titles=(('战术核心属性','core_attribute'),('战术核心技能','core_skill'),
            ('因子技能','factor'),('变量属性','mission'),('快速休整','rest'),
            ('属性深度刻录','attribute'),('属性刻录','attribute'),
            ('技能深度研习','skill'),('技能研习','skill'))
        for b in boxes:
            if not 0<=b.y+b.height/2<segments[0][1]:continue
            title=re.sub('[·•・]','',compact(b.name))
            found=[kind for text,kind in titles if title==text]
            if len(found)!=1 or getattr(b,'confidence',0)<.9:continue
            crop=segments[0][2]
            x,y=max(0,int(b.x)),max(0,int(b.y))
            x2,y2=min(crop.shape[1],int(b.x+b.width)),min(crop.shape[0],int(b.y+b.height))
            glyph=crop[y:y2,x:x2]
            if glyph.size and np.mean(np.min(glyph,axis=2)>135)>.025:return found[0]
        return None

    def boxes_in(self,area):
        x,y,x2,y2=area
        return [b for b in self.boxes if x<=b.x+b.width/2<=x2 and y<=b.y+b.height/2<=y2]

    def text_in(self,area):
        return ''.join(compact(b.name) for b in sorted(self.boxes_in(area),key=lambda b:(round(b.y/15),b.x)))

    def classify(self):
        t=self.text
        if '刷新所有选项' in t and '确认' in t:return 'refresh_confirm'
        if '词条效果' in t and ('同频' in t or '属性加成效果' in t):return 'result'
        # Scrolling the completed module moves "词条效果" out of view. The
        # local result's fixed identity, attribute footer and XP panel remain.
        if ('模组研发经验' in self.text_in((700,875,1400,970))
                and '属性加成效果' in self.text_in((780,735,1100,825))
                and re.fullmatch(r'(军工武装|一致战线)\d{1,3}[回口☑]?',
                                 self.text_in((780,320,1130,410)))):
            return 'result'
        if '是否退出模组研发' in t and '快速智能研发' in t:return 'quick_dialog'
        if '超频成功' in t:return 'overclock'
        for title,kind in [('战术核心·属性','core_attribute'),('战术核心·技能','core_skill'),
                           ('因子·技能','factor'),('变量·属性','mission'),('快速休整','rest'),
                           ('属性深度刻录','attribute'),('属性刻录','attribute'),
                           ('技能深度研习','skill'),('技能研习','skill')]:
            if title.replace('·','') in re.sub('[·•・]','',self.header):return kind
        if '未知区域刻蚀' in self.header:
            # The same unknown node can offer five skill OR attribute cards.
            # Neither its title nor an incomplete OCR list proves the type.
            footer=(1600,970,2020,1080)
            prompt='请选择一个选项' in self.text_in(footer) or self.button('确认',footer) is not None
            areas=self.option_areas()
            titles=[self.text_in((x+180,y+15,x2-12,y+75)) for x,y,x2,_ in areas[:5]]
            complete=all(re.fullmatch(r'[\u4e00-\u9fff·•・]+(?i:L[VY])[.·:]?\d+\+[1-9]\d*',title)
                         for title in titles)
            attributes=[self.text_in((x+180,y+15,x2-12,y+110)) for x,y,x2,_ in areas[:5]]
            complete_attribute=all(attribute_delta(text) and group_scope(text) for text in attributes)
            if prompt and (complete != complete_attribute):
                slots,evidence=self._card_slots_from_edges(self.frame)
                native=getattr(self,'native_frame',None)
                if slots is None and isinstance(native,np.ndarray):
                    if native.shape==(2160,3840,3):
                        # ReferenceTask's 3x reference is exactly reversible here;
                        # a genuine 4K image cannot borrow an approximate 720P view.
                        restored=cv2.resize(native,(1280,720))
                        native=restored if np.array_equal(cv2.resize(restored,(3840,2160)),native) else None
                    if isinstance(native,np.ndarray) and native.shape==(720,1280,3):
                        slots,evidence=self._card_slots_from_edges(native,native=True)
                if slots==tuple(range(5)):
                    if complete:
                        self.unknown_skill_classification_evidence={'producer':'unknown_area_complete_skill_canvas',
                            'titles':titles,'slots':list(slots),'geometry':evidence,'selection_prompt':True}
                        return 'skill'
                    self.unknown_attribute_classification_evidence={'producer':'unknown_area_complete_attribute_canvas',
                        'titles':attributes,'slots':list(slots),'geometry':evidence,'selection_prompt':True}
                    return 'attribute'
        if '研发队员' in t and '实验启动' in t:return 'prepared'
        if '当前进度' in t and self.button('进行封装') and re.search(r'29[/／]30',t):return 'hub'
        if '自动研发' in t and ('研发策略' in t or '当前进度' in t):return 'hub'
        if '模组研发' in t and ('研发等级' in t or '模组排名' in t):return 'map'
        return 'unknown'

    def button(self,label,area=(1450,960,2015,1100)):
        matches=[b for b in self.boxes_in(area) if label in compact(b.name)]
        if len(matches)!=1:return None
        b=matches[0]
        return (round(b.x-8),round(b.y-8),round(b.x+b.width+8),round(b.y+b.height+8))

    def factor_title_native(self,area):
        """Read a complete literal in the fixed factor-title band, never repair letters."""
        native=getattr(self,'native_frame',None)
        if self.kind!='factor' or not isinstance(native,np.ndarray) or native.shape!=(720,1280,3):return None
        key=tuple(area)
        if not hasattr(self,'factor_title_evidence'):self.factor_title_evidence={}
        if key in self.factor_title_evidence:return self.factor_title_evidence[key].get('accepted_title')
        x,_,x2,_=area
        roi=(int(np.floor((x+12)/1.6)),int(np.floor(580/1.6)),
             int(np.ceil((x2-12)/1.6)),int(np.ceil(635/1.6)))
        left,top,right,bottom=roi
        if not 0<=left<right<=1280 or not 0<=top<bottom<=720:return None
        crop=cv2.resize(native[top:bottom,left:right],None,fx=3,fy=3,interpolation=cv2.INTER_CUBIC)
        crop=cv2.copyMakeBorder(crop,24,24,24,24,cv2.BORDER_REPLICATE)
        boxes=self.task.ocr(frame=crop,threshold=.9)
        title=None
        if len(boxes)==1:
            confidence=getattr(boxes[0],'confidence',None)
            raw=compact(boxes[0].name)
            if (isinstance(confidence,(int,float,np.floating)) and not isinstance(confidence,bool)
                    and .9<=confidence<=1 and re.fullmatch(r'国?通用[·•・:：][\u4e00-\u9fff]{2,12}',raw)):
                title=raw
        evidence={'producer':'factor_title_native_fixed_band','roi':list(roi),'threshold':.9,
            'interpolation':'CUBIC','scale':3,'padding':24,'accepted_title':title,
            'raw':[{'text':b.name,'confidence':getattr(b,'confidence',None)} for b in boxes],
            'native_frame_sha256':hashlib.sha256(native.tobytes()).hexdigest(),
            'observation_epoch':getattr(self,'observation_epoch',None)}
        self.factor_title_evidence[key]=evidence
        emit=getattr(self.task,'emit',None)
        if emit:emit('craft_factor_title_native',**evidence)
        return title

    def enabled(self,area):
        x,y,x2,y2=map(int,area)
        crop=self.frame[max(0,y):min(1152,y2),max(0,x):min(2048,x2)]
        return bool(crop.size and np.mean(np.min(crop,axis=2)>135)>.025)

    def refresh_amounts(self):
        """Read the two independent fields; never concatenate ratios across OCR boxes."""
        native=getattr(self,'native_frame',None)
        capture=getattr(self,'capture_provenance',None)
        native_720=isinstance(native,np.ndarray) and native.shape==(720,1280,3)
        if isinstance(capture,dict) and capture.get('native_size')==[1280,720]:
            if (not native_720 or capture.get('source')!='ReferenceTask.native_frame'
                    or capture.get('observation_epoch')!=getattr(self,'observation_epoch',None)
                    or capture.get('native_frame_sha256')!=hashlib.sha256(native.tobytes()).hexdigest()
                    or capture.get('recognition_frame_sha256')!=hashlib.sha256(self.frame.tobytes()).hexdigest()):
                return None
        values=[]
        for area in ((750,425,1320,495),(990,695,1135,745)):
            text=self.text_in(area)
            if area==(750,425,1320,495):
                # A background boost badge can join the popup's 1/1 as
                # "1/1100%". Only a complete count field is authoritative.
                count=re.fullmatch(r'(?:※?当前研发节点剩余刷新次数)?(\d+)[/／](\d+)',compact(text))
                evidence={'producer':'refresh_remaining_field','coarse':text,'accepted':False,
                          'observation_epoch':getattr(self,'observation_epoch',None),'ratio':None}
                if count is not None and tuple(map(int,count.groups())) not in ((0,1),(1,1)):
                    evidence['coarse_ratio']=list(map(int,count.groups()))
                    count=None
                if count is None:
                    roi=(760,275,791,303) if native_720 else area
                    frame=native if native_720 else self.frame
                    x,y,x2,y2=roi
                    crop=cv2.resize(frame[y:y2,x:x2],None,fx=3,fy=3,interpolation=cv2.INTER_CUBIC)
                    crop=cv2.copyMakeBorder(crop,24,24,24,24,cv2.BORDER_REPLICATE)
                    threshold=.9 if native_720 else .55
                    boxes=self.task.ocr(frame=crop,threshold=threshold)
                    evidence.update(producer='refresh_remaining_native_roi' if native_720 else 'refresh_remaining_roi',
                                    roi=list(roi),threshold=threshold,scale=3,padding=24,
                                    frame_sha256=hashlib.sha256(frame.tobytes()).hexdigest(),
                                    raw=[{'text':b.name,'confidence':getattr(b,'confidence',None)} for b in boxes])
                    if len(boxes)==1:
                        confidence=getattr(boxes[0],'confidence',None)
                        valid=(isinstance(confidence,(int,float,np.floating)) and not isinstance(confidence,bool)
                               and threshold<=confidence<=1)
                        if valid:
                            pattern=r'(\d+)\s*[/／]\s*(\d+)' if native_720 else r'(?:※?当前研发节点剩余刷新次数)?(\d+)[/／](\d+)'
                            count=re.fullmatch(pattern,boxes[0].name.strip() if native_720 else compact(boxes[0].name))
                pair=list(map(int,count.groups())) if count else None
                accepted=pair in ([0,1],[1,1])
                evidence.update(accepted=accepted,ratio=pair if accepted else None,
                                reason='remaining_known' if accepted else
                                'remaining_unsupported' if pair is not None else 'remaining_unreadable')
                if getattr(self,'refresh_remaining_evidence',None)!=evidence:
                    self.refresh_remaining_evidence=evidence
                    emit=getattr(self.task,'emit',None)
                    if callable(emit):emit('craft_refresh_remaining_evidence',**evidence)
                if not accepted:return None
                values.extend(pair)
                continue
            if native_720 and area==(990,695,1135,745):
                # Full-client OCR can read 11350 as 11950 with high confidence.
                # Only two dedicated, same-capture field reads authorize spending.
                native_area=(618,434,710,466)
                reads=[]
                def read_field(producer,frame,roi,interpolation):
                    x,y,x2,y2=roi
                    crop=cv2.resize(frame[y:y2,x:x2],None,fx=3,fy=3,interpolation=interpolation)
                    crop=cv2.copyMakeBorder(crop,24,24,24,24,cv2.BORDER_REPLICATE)
                    boxes=self.task.ocr(frame=crop,threshold=.9)
                    row={'producer':producer,'roi':list(roi),'threshold':.9,
                         'interpolation':{cv2.INTER_LINEAR:'LINEAR',cv2.INTER_CUBIC:'CUBIC',
                                          cv2.INTER_LANCZOS4:'LANCZOS4'}[interpolation],
                         'scale':3,'padding':24,
                         'frame_sha256':hashlib.sha256(frame.tobytes()).hexdigest(),
                         'raw':[{'text':b.name,'confidence':getattr(b,'confidence',None)} for b in boxes],
                         'accepted':False,'ratio':None}
                    if len(boxes)==1:
                        match=re.fullmatch(r'(\d+)\s*[/／]\s*(\d+)',boxes[0].name.strip())
                        confidence=getattr(boxes[0],'confidence',None)
                        if (match and isinstance(confidence,(int,float,np.floating))
                                and not isinstance(confidence,bool) and .9<=confidence<=1):
                            pair=list(map(int,match.groups()))
                            if pair[0]>=0 and pair[1]>0:row.update(accepted=True,ratio=pair)
                    return row
                reads.append(read_field('refresh_amount_reference_roi',self.frame,area,cv2.INTER_CUBIC))
                reads.append(read_field('refresh_amount_native_roi',native,native_area,cv2.INTER_CUBIC))
                reference_read=reads[0]
                native_read=reads[1]
                if not native_read['accepted']:
                    # Cubic ringing merged actual thin "111" strokes into "m".
                    # One same-pixel linear variant may recover a full ratio;
                    # it cannot replace an accepted conflicting native read.
                    native_read=read_field('refresh_amount_native_roi_linear',native,native_area,cv2.INTER_LINEAR)
                    reads.append(native_read)
                if not reference_read['accepted']:
                    # Actual 5650/100 has no cubic reference detection. One
                    # same-ROI Lanczos read may agree with the independent native
                    # field; accepted reference conflicts cannot be overwritten.
                    reference_read=read_field('refresh_amount_reference_roi_lanczos',self.frame,area,cv2.INTER_LANCZOS4)
                    reads.append(reference_read)
                paired=(reference_read,native_read)
                accepted=all(row['accepted'] for row in paired) and paired[0]['ratio']==paired[1]['ratio']
                evidence={'authority':'paired_field_ocr','observation_epoch':getattr(self,'observation_epoch',None),
                          'coarse':{'text':text,'authoritative':False},'reads':reads,'accepted':accepted,
                          'reason':('field_producers_agree' if accepted else
                                    'field_producers_conflict' if all(row['accepted'] for row in paired) else
                                    'field_producer_unreadable')}
                if getattr(self,'refresh_amount_evidence',None)!=evidence:
                    self.refresh_amount_evidence=evidence
                    emit=getattr(self.task,'emit',None)
                    if callable(emit):emit('craft_refresh_amount_evidence',**evidence)
                if not accepted or len(values)!=2 or values[0]<0 or values[1]<=0 or values[0]>values[1]:return None
                values.extend(reference_read['ratio'])
                continue
            matches=re.findall(r'(\d+)\s*[/／]\s*(\d+)',text)
            if len(matches)!=1:
                x,y,x2,y2=area
                crop=cv2.resize(self.frame[y:y2,x:x2],None,fx=3,fy=3,interpolation=cv2.INTER_CUBIC)
                crop=cv2.copyMakeBorder(crop,24,24,24,24,cv2.BORDER_REPLICATE)
                text=' '.join(b.name for b in self.task.ocr(frame=crop,threshold=.55))
                matches=re.findall(r'(\d+)\s*[/／]\s*(\d+)',text)
            if len(matches)!=1:return None
            values.extend(map(int,matches[0]))
        return tuple(values)

    def progress(self):
        m=re.search(r'当前进度[:：]?(\d+)/30',self.text)
        return int(m[1]) if m else None

    def skill_opportunities(self,options):
        """Keep confirmed, possible and absent opportunities separate.

        Every future category needs present evidence before its upper bound is
        known. Missing optional labels cannot prove that an extra choice is
        absent, and progress alone never proves this is the final choice.
        """
        from src.crafting_policy import SkillOpportunities
        text=self.text
        current=max((o.gain for o in options if o.level_known),default=None)
        if current is None:return None
        confirmed=[current];possible=[];absent=[];future_known=True
        for label,gain in (('技能研习',1),('技能深度研习',2)):
            matches=re.findall('后续'+label+r'[:：]?(\d{1,2})次',text)
            if len(matches)!=1 or int(matches[0])>30:
                future_known=False
            else:confirmed.extend([gain]*int(matches[0]))
        for label,key in (('超频','overclock'),('未知区域','unknown_area')):
            matches=re.findall(label+r'技能(已确认|未确认)[:：]?(\d{1,2})次(?:[（(][+＋]([1-3])[）)])?',text)
            negative=(label+'未触发' in text or label+'失败' in text or
                      label+'不是技能' in text or label+'非技能' in text)
            if len(matches)>1 or (negative and matches and int(matches[0][1])>0):
                future_known=False
            elif negative:
                absent.append(key)
            elif not matches:
                future_known=False
            else:
                certainty,count,gain=matches[0];count=int(count)
                if count>30:
                    future_known=False
                elif certainty=='已确认':
                    if count:confirmed.extend([int(gain or 1)]*count)
                    else:absent.append(key)
                elif count:
                    possible.extend([int(gain or 1)]*count)
                else:
                    # An unconfirmed zero is not proof of an absent opportunity.
                    future_known=False
        if len(confirmed)+len(possible)>31:
            return SkillOpportunities((current,),future_known=False)
        return SkillOpportunities(tuple(confirmed),tuple(possible),tuple(absent),future_known)

    def hub_identity(self):
        """Visible module and roster identity, excluding the animated selected portrait."""
        signatures=[]
        # The module silhouette/badge and two independent roster portraits persist
        # across a node advance. Selected-portrait glow and the node track do not.
        for x,y,x2,y2 in ((245,250,480,625),(333,399,421,522),
                          (1610,205,1668,262),(1705,205,1762,262)):
            crop=self.frame[y:y2,x:x2]
            gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
            if float(gray.std())<8:raise ChoiceUnreadable('当前模组身份图像不足，不能重新建立基线')
            coefficients=cv2.dct(cv2.resize(gray,(32,32)).astype(np.float32))[:8,:8]
            median=np.median(coefficients.flat[1:])
            signatures.append(''.join('1' if value>median else '0' for value in coefficients.flat))
        return signatures

    def hub_mission_rows(self):
        """Require every visible attribute row; an OCR omission cannot imply no missions."""
        result=[]
        empty=('暂无研发目标','无研发目标','暂无属性激活条件','无属性激活条件','暂无任务')
        attribute_row=re.compile(r'[^\u4e00-\u9fff\d]{0,3}'
                                 r'(火力|狙击|隐刃|重盾|突击|工程|医疗|指挥官)'
                                 r'(最大生命|攻击|防御)[+＋]?(\d{1,7})[/／](\d{1,7})')
        def row_pixels(y,y2):
            # Independent occupancy includes unreadable rows; panel fills/dividers are not text.
            # Stop before the panel's vertical divider, including for a clipped final row.
            gray=cv2.cvtColor(self.frame[y:y2,1530:1960],cv2.COLOR_BGR2GRAY)
            strokes=cv2.morphologyEx(gray,cv2.MORPH_TOPHAT,np.ones((7,7),np.uint8))>18
            _,_,stats,_=cv2.connectedComponentsWithStats(strokes.astype(np.uint8))
            return sum(int(n) for _,_,w,h,n in stats[1:] if 3<=h<=35 and w<=70 and n>=5)

        # A labelled skill section is a verified boundary, not an unreadable attribute row.
        # Without that label, keep inspecting the entire possible attribute list.
        headers=[b for b in self.boxes_in((1480,563,1910,875)) if compact(b.name)=='技能激活条件']
        if len(headers)>1:raise ChoiceUnreadable('技能条件边界不唯一，不能核验当前属性任务')
        bottom=891
        if headers:
            header=headers[0];bottom=int(header.y)
            if (not 563<=bottom<875 or getattr(header,'confidence',.75)<.75 or
                    row_pixels(bottom,min(875,int(header.y+header.height)))<60):
                raise ChoiceUnreadable('技能条件边界缺少可信文本或像素证据')
            for i in range(7):
                y=563+i*47
                if y+46<=bottom:continue
                boxes=[b for b in self.boxes_in((1495,y,1980,y+46)) if b is not header]
                row=''.join(compact(b.name) for b in sorted(boxes,key=lambda b:(round(b.y/15),b.x)))
                if attribute_row.fullmatch(row):
                    raise ChoiceUnreadable('技能条件边界之后仍有属性任务，不能跳过当前任务行')
        observed=[]
        for i in range(7):
            y=563+i*47
            if y>=bottom:break
            y2=min(y+46,bottom)
            observed.append((self.text_in((1495,y,1980,y2)),row_pixels(y,y2)))
        for i,(text,pixels) in enumerate(observed):
            if any(marker==text for marker in empty):
                if i or result or any(t or n>=60 for t,n in observed[1:]):
                    raise ChoiceUnreadable('当前任务空状态与其它任务行冲突')
                return []
            if not text and pixels<60:continue
            match=attribute_row.fullmatch(text)
            if not match or pixels<60:raise ChoiceUnreadable('当前任务行信息不足，不能重新建立基线')
            group,attr,current,goal=match.groups()
            if int(goal)<=0:raise ChoiceUnreadable('当前任务目标数值不可信')
            if group in ('狙击','隐刃'):group='火力'
            row=(group,attr,int(current),int(goal))
            if any((r[0],r[1],r[3])==(row[0],row[1],row[3]) for r in result):
                raise ChoiceUnreadable('当前任务身份重复，不能重新建立基线')
            result.append(row)
        if not result:
            raise ChoiceUnreadable('未取得完整当前任务状态，不能把空识别当作无任务')
        return result

    def trusted_hub_progress(self):
        """Read progress from the verified hub region, without requiring accounting facts."""
        anchors=(('研发辅助',(1480,425,1980,505)),
                 ('属性激活条件',(1480,505,1910,563)),
                 ('自动研发',(1480,875,1770,960)),('研发策略',(1780,875,1990,960)),
                 ('属性加成',(50,1000,360,1080)),('词条列表',(370,1000,665,1080)))
        if self.kind!='hub' or any(label not in self.text_in(area) for label,area in anchors):
            raise ChoiceUnreadable('当前页面不足以证明模组主界面，不能重新建立基线')
        progress=re.fullmatch(r'当前进度[:：]?(\d{1,2})[/／]30',self.text_in((950,995,1260,1065)))
        if not progress or not 0<=int(progress[1])<30:
            raise ChoiceUnreadable('当前模组进度不唯一或不可信')
        return int(progress[1])

    def trusted_hub_state(self):
        """Read a new present-state baseline without guessing any historical input."""
        progress=self.trusted_hub_progress()
        balance=re.fullmatch(r'[A△▲Λ]?(\d{1,7})',self.text_in((40,800,330,900)))
        ratio=re.fullmatch(r'(\d{1,7})[/／](\d{1,7})',self.text_in((40,905,380,980)))
        if not balance or not ratio or int(balance[1])!=int(ratio[1]) or not 0<=int(ratio[1])<=int(ratio[2]) or int(ratio[2])<=0:
            raise ChoiceUnreadable('当前资源数值不足或两处读数不一致')
        node_text=self.text_in((1640,970,1985,1090))
        nodes=('战术核心','属性刻录','属性深度刻录','技能研习','技能深度研习',
               '因子','变量','快速休整','未知区域刻蚀','完成模组')
        node=next((label for label in nodes if node_text==label),None)
        area=self.button(node) if node else None
        if not area or not self.enabled(area):raise ChoiceUnreadable('当前研发节点入口不可信')
        return {'progress':progress,'resource':{'balance':int(ratio[1]),'capacity':int(ratio[2])},
                'missions':self.hub_mission_rows(),'module_identity':self.hub_identity(),'node':node}

    def visible_missions(self):
        text=self.text_in((1480,400,1995,865))
        result=[]
        for group,attr,current,goal in re.findall(r'(火力|狙击|隐刃|重盾|突击|工程|医疗)(最大生命|攻击|防御)\+?(\d+)/(\d+)',text):
            if group in ('狙击','隐刃'):group='火力'
            result.append((group,attr,int(current),int(goal)))
        return result

    def rarity(self,area):
        x,y,x2,y2=map(int,area)
        hsv=cv2.cvtColor(self.frame[max(0,y):min(1152,y2),max(0,x):min(2048,x2)],cv2.COLOR_BGR2HSV)
        h,s,v=cv2.split(hsv)
        scores=[np.mean((h>=low)&(h<=high)&(s>90)&(v>120)) for low,high in ((8,32),(120,165),(90,115))]
        return (3,2,1)[int(np.argmax(scores))] if max(scores)>.025 else 0

    def _rest_rarity_native(self,area):
        """Read the current portrait's rarity wash, excluding selected card cyan."""
        native=getattr(self,'native_frame',None)
        if native is None or native.shape!=(720,1280,3) or area not in self.option_areas():return 0
        x,y,x2,y2=area
        roi=tuple(round(v/1.6) for v in (x+15,y2-55,x+168,y2-10))
        a,b,a2,b2=roi
        if not (0<=a<a2<=1280 and 0<=b<b2<=720):return 0
        crop=native[b:b2,a:a2]
        if not crop.size:return 0
        h,s,v=cv2.split(cv2.cvtColor(crop,cv2.COLOR_BGR2HSV))
        scores=[float(np.mean((h>=low)&(h<=high)&(s>90)&(v>120)))
                for low,high in ((8,32),(120,165),(90,115))]
        rarity=(3,2,1)[int(np.argmax(scores))] if max(scores)>.025 else 0
        if not hasattr(self,'rest_rarity_evidence'):self.rest_rarity_evidence={}
        self.rest_rarity_evidence[area]={'producer':'rest_portrait_bottom_native','roi':list(roi),
                                       'color_coverage':scores,'rarity':rarity}
        return rarity

    def core_skill_visual_enabled(self,area,text):
        if '未解锁' in text:return False
        if '解锁条件' in text:
            progress=re.findall(r'(\d+)\s*[/／]\s*(\d+)',text)
            if len(progress)==1 and int(progress[0][0])<int(progress[0][1]):return False
            return None
        x,y,x2,_=map(int,area)
        title=self.frame[y+20:y+90,x+30:x2-30]
        if not title.size:return None
        return True if float(np.mean(np.min(title,axis=2)>150))>.02 else None

    def selected(self,area):
        x,y,x2,y2=map(int,area)
        native=getattr(self,'native_frame',None)
        if self.kind=='attribute' and native is not None and native.shape==(720,1280,3):
            return self._attribute_selected_native((x,y,x2,y2))
        if self.kind=='skill' and native is not None and native.shape==(720,1280,3):
            return self._skill_selected_native((x,y,x2,y2))
        if self.kind=='rest' and native is not None and native.shape==(720,1280,3):
            return self._rest_selected_native((x,y,x2,y2))
        if self.kind=='factor' and native is not None and native.shape==(720,1280,3):
            return self._factor_selected_native((x,y,x2,y2))
        if self.kind=='mission' and native is not None and native.shape==(720,1280,3):
            return self._mission_selected_native((x,y,x2,y2))
        if self.kind in ('attribute','skill','rest','core_attribute','core_skill'):
            # The portrait can have its own bright/gold border without a selected card.
            # Only the long outer top edge, away from the portrait, proves selection.
            # Tall core-attribute cards have a separate portrait. Their narrow gap
            # lets a neighbour's right border enter the old left-edge search band.
            inset=30 if self.kind in ('core_attribute','core_skill') else 180
            edge=self.frame[max(0,y-6):y+9,x+inset:x2-20]
            return bool(edge.size and np.max(np.mean(np.min(edge,axis=2)>150,axis=1))>.65)
        strip=self.frame[y+12:y2-100 if y2-y>400 else y2-15,max(0,x-25):x+26]
        # Search a narrow edge band: condition cards are located from their OCR
        # headings, whose centers can differ slightly from the card centers.
        return bool(strip.size and np.max(np.mean(np.min(strip,axis=2)>150,axis=0))>.35)

    def _attribute_selected_native(self,area):
        """A 720P selected attribute has a bright long top and both outer side edges."""
        if area not in self.option_areas():return False
        gray=cv2.cvtColor(self.native_frame,cv2.COLOR_BGR2GRAY).astype(float)
        x,y,x2,y2=area
        def crop(box):
            a,b,a2,b2=box
            return gray[round(b/1.6):round(b2/1.6),round(a/1.6):round(a2/1.6)]
        def coverage(line,before,after,axis):
            edge=crop(line)
            if axis==1:
                a,b,a2,b2=line
                top,bottom=round(b/1.6),round(b2/1.6)
                # Bind the backdrop to each searched outer-edge column. A fixed
                # inside band can instead land on the brighter gold portrait
                # frame when the actual card edge is shifted by two native pixels.
                strengths=[]
                for column in range(round(a/1.6),round(a2/1.6)):
                    backdrop=np.maximum(gray[top:bottom,column-5:column-2].mean(axis=1),
                                        gray[top:bottom,column+3:column+6].mean(axis=1))
                    mask=(gray[top:bottom,column]>150)&(gray[top:bottom,column]-backdrop>12)
                    strengths.append(float(np.mean(mask)))
                return max(strengths,default=0.)
            backdrop=np.maximum(crop(before).mean(axis=axis),crop(after).mean(axis=axis))
            if axis==1:backdrop=backdrop[:,None]
            # Native blue-white outlines can have one channel below 150. Require
            # measured luminance, local contrast and three long edges together;
            # neither color membership nor the animated bottom glow is authority.
            mask=(edge>150)&(edge-backdrop>12)
            return float(np.max(np.mean(mask,axis=1-axis)))
        strengths=[]
        for edge_y in (y,y2):
            strengths.append(coverage((x+180,edge_y-6,x2-20,edge_y+9),
                                      (x+180,edge_y-12,x2-20,edge_y-8),
                                      (x+180,edge_y+11,x2-20,edge_y+15),0))
        for edge_x in (x,x2):
            strengths.append(coverage((edge_x-6,y+20,edge_x+9,y2-20),
                                      (edge_x-12,y+20,edge_x-8,y2-20),
                                      (edge_x+11,y+20,edge_x+15,y2-20),1))
        selected=all(strengths[i]>.65 for i in (0,2,3))
        if not hasattr(self,'attribute_selected_evidence'):self.attribute_selected_evidence={}
        self.attribute_selected_evidence[area]={'producer':'attribute_selected_native_edges',
                                              'edge_coverage':strengths,'selected':selected}
        return selected

    def _skill_selected_native(self,area):
        """Bind a native skill's selected outline to its fixed, legal card slot."""
        if area not in self.option_areas():return False
        if hasattr(self,'_options') and not any(tuple(o.area)==area for o in self._options):return False
        # Skill target() verifies the current title without populating _options.
        # A fresh verified frame must therefore use geometry without another OCR.
        gray=cv2.cvtColor(self.native_frame,cv2.COLOR_BGR2GRAY).astype(float)
        x,y,x2,y2=area
        def crop(box):
            a,b,a2,b2=box
            if not (0<=a<a2<=2048 and 0<=b<b2<=1152):return np.empty((0,0))
            return gray[round(b/1.6):round(b2/1.6),round(a/1.6):round(a2/1.6)]
        edge=crop((x+180,y-6,x2-20,y+9))
        preceding=crop((x+180,y-12,x2-20,y-8))
        following=crop((x+180,y+11,x2-20,y+15))
        if not edge.size or not preceding.size or not following.size:return False
        if edge.shape[1]!=preceding.shape[1] or edge.shape[1]!=following.shape[1]:return False
        backdrop=np.maximum(preceding.mean(axis=0),following.mean(axis=0))
        strengths=[float(np.max(np.mean((edge>150)&(edge-backdrop>12),axis=1)))]
        top,bottom=round((y+20)/1.6),round((y2-20)/1.6)
        for edge_x in (x,x2):
            columns=[]
            for column in range(round((edge_x-6)/1.6),round((edge_x+9)/1.6)):
                before=gray[top:bottom,column-5:column-2]
                after=gray[top:bottom,column+3:column+6]
                line=gray[top:bottom,column]
                if not line.size or not before.size or not after.size:return False
                backdrop=np.maximum(before.mean(axis=1),after.mean(axis=1))
                columns.append(float(np.mean((line>150)&(line-backdrop>12))))
            strengths.append(max(columns,default=0.))
        selected=all(v>.65 for v in strengths)
        if not hasattr(self,'skill_selected_evidence'):self.skill_selected_evidence={}
        self.skill_selected_evidence[area]={'producer':'skill_selected_native_edges',
                                          'edge_coverage':strengths,'selected':selected}
        return selected

    def _rest_selected_native(self,area):
        """Require three long native outer edges on the current rest candidate."""
        if area not in self.option_areas():return False
        if not any(tuple(o.area)==area for o in getattr(self,'_options',())):return False
        gray=cv2.cvtColor(self.native_frame,cv2.COLOR_BGR2GRAY).astype(float)
        x,y,x2,y2=area
        def crop(box):
            a,b,a2,b2=box
            if not (0<=a<a2<=2048 and 0<=b<b2<=1152):return np.empty((0,0))
            return gray[round(b/1.6):round(b2/1.6),round(a/1.6):round(a2/1.6)]
        edge=crop((x+180,y-6,x2-20,y+9))
        preceding=crop((x+180,y-12,x2-20,y-8))
        following=crop((x+180,y+11,x2-20,y+15))
        if not edge.size or not preceding.size or not following.size:return False
        if edge.shape[1]!=preceding.shape[1] or edge.shape[1]!=following.shape[1]:return False
        backdrop=np.maximum(preceding.mean(axis=0),following.mean(axis=0))
        strengths=[float(np.max(np.mean((edge>150)&(edge-backdrop>12),axis=1)))]
        top,bottom=round((y+20)/1.6),round((y2-20)/1.6)
        for edge_x in (x,x2):
            columns=[]
            for column in range(round((edge_x-6)/1.6),round((edge_x+9)/1.6)):
                before=gray[top:bottom,column-5:column-2]
                after=gray[top:bottom,column+3:column+6]
                line=gray[top:bottom,column]
                if not line.size or not before.size or not after.size:return False
                backdrop=np.maximum(before.mean(axis=1),after.mean(axis=1))
                columns.append(float(np.mean((line>150)&(line-backdrop>12))))
            strengths.append(max(columns,default=0.))
        selected=all(v>.65 for v in strengths)
        if not hasattr(self,'rest_selected_evidence'):self.rest_selected_evidence={}
        self.rest_selected_evidence[area]={'producer':'rest_selected_native_edges',
                                         'edge_coverage':strengths,'selected':selected}
        return selected

    def _factor_selected_native(self,area):
        """Bind three bright outer edges to the OCR-located current factor card."""
        if not any(tuple(o.area)==area for o in getattr(self,'_options',())):return False
        x,y,x2,y2=area
        # Reject invalid cards and search/backdrop bands before numpy can clip
        # them into an apparently complete edge or produce an empty reduction.
        if not (0<=x<x2<=2048 and 0<=y<y2<=1152 and
                x>=32 and x2+32<=2048 and y>=12 and y+15<=y2-100):return False
        gray=cv2.cvtColor(self.native_frame,cv2.COLOR_BGR2GRAY).astype(float)
        def crop(box):
            a,b,a2,b2=box
            if not (0<=a<a2<=2048 and 0<=b<b2<=1152):return np.empty((0,0))
            return gray[round(b/1.6):round(b2/1.6),round(a/1.6):round(a2/1.6)]
        def coverage(line,before,after,axis):
            edge=crop(line)
            preceding,following=crop(before),crop(after)
            if not edge.size or not preceding.size or not following.size:return 0.
            length=1-axis
            if edge.shape[length]!=preceding.shape[length] or edge.shape[length]!=following.shape[length]:return 0.
            backdrop=np.maximum(preceding.mean(axis=axis),following.mean(axis=axis))
            if axis==1:backdrop=backdrop[:,None]
            mask=(edge>150)&(edge-backdrop>12)
            return float(np.max(np.mean(mask,axis=1-axis)))
        # Factor areas follow heading centers and can be slightly narrower than
        # the outer card. Retain the existing bounded side search, then require
        # a long top AND both long sides instead of one whiteness column.
        strengths=[coverage((x+30,y-6,x2-30,y+9),
                            (x+30,y-12,x2-30,y-8),(x+30,y+11,x2-30,y+15),0)]
        for edge_x in (x,x2):
            strengths.append(coverage((edge_x-25,y+12,edge_x+26,y2-100),
                                      (edge_x-32,y+12,edge_x-28,y2-100),
                                      (edge_x+28,y+12,edge_x+32,y2-100),1))
        selected=all(v>.65 for v in strengths)
        if not hasattr(self,'factor_selected_evidence'):self.factor_selected_evidence={}
        self.factor_selected_evidence[area]={'producer':'factor_selected_native_edges',
                                           'edge_coverage':strengths,'selected':selected}
        return selected

    def _mission_selected_native(self,area):
        """A variable card needs its own top and both sides, outside its OCR box."""
        options=getattr(self,'_options',())
        if len(options)!=3 or sorted(o.index for o in options)!=[0,1,2]:return False
        matches=[o for o in options if tuple(o.area)==area]
        if len(matches)!=1:return False
        index=matches[0].index
        x,y,x2,y2=area
        # Mission OCR areas include reward/score padding. The actual three-card
        # outline is native y190..520; its selected top can move up two pixels.
        # Bind the fixed slot to the current heading-derived legal candidate.
        if y!=280 or y2!=945 or not 600<=x2-x<=625:return False
        if not 0<=x<x2<=2048 or abs((x+x2)/2-(380+646*index))>12:return False
        left=round((65+646*index)/1.6)
        right=round((695+646*index)/1.6)
        top,bottom=190,520
        gray=cv2.cvtColor(self.native_frame,cv2.COLOR_BGR2GRAY).astype(float)
        strengths=[]
        rows=[]
        for row in range(top-4,top+5):
            line=gray[row,left+20:right-20]
            backdrop=np.maximum(gray[row-5:row-2,left+20:right-20].mean(axis=0),
                                gray[row+3:row+6,left+20:right-20].mean(axis=0))
            rows.append(float(np.mean((line>150)&(line-backdrop>12))))
        strengths.append(max(rows))
        for edge_x in (left,right):
            columns=[]
            for column in range(edge_x-6,edge_x+7):
                line=gray[top+12:bottom-12,column]
                backdrop=np.maximum(gray[top+12:bottom-12,column-5:column-2].mean(axis=1),
                                    gray[top+12:bottom-12,column+3:column+6].mean(axis=1))
                columns.append(float(np.mean((line>150)&(line-backdrop>12))))
            strengths.append(max(columns))
        selected=all(v>.65 for v in strengths)
        if not hasattr(self,'mission_selected_evidence'):self.mission_selected_evidence={}
        self.mission_selected_evidence[area]={'producer':'mission_selected_native_edges',
                                            'slot':index,'native_outline':[left,top,right,bottom],
                                            'edge_coverage':strengths,'selected':selected}
        return selected

    def card_slots(self):
        """Independent geometric occupancy: paired long card edges, never parsed names."""
        if self.kind not in ('attribute','skill','rest'):return None
        slots,evidence=self._card_slots_from_edges(self.frame)
        native=getattr(self,'native_frame',None)
        if slots is None and native is not None and native.shape==(720,1280,3):
            slots,native_evidence=self._card_slots_from_edges(native,native=True)
            for row,reference in zip(native_evidence,evidence):
                row['reference_edge_coverage']=reference['edge_coverage']
            evidence=native_evidence
        self.card_slot_evidence=evidence
        return slots

    @staticmethod
    def _card_slots_from_edges(frame,native=False):
        """Native translucent outlines need color contrast as well as inner luminance."""
        occupied=[]
        evidence=[]
        gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY).astype(float)
        color=frame.astype(float) if native else None
        scale=1/1.6 if native else 1
        def crop(image,area):
            x,y,x2,y2=area
            return image[round(y*scale):round(y2*scale),round(x*scale):round(x2*scale)]
        def strength(area,before,after,axis,inside):
            line=crop(gray,area)
            b0=crop(gray,before).mean(axis=axis)
            b1=crop(gray,after).mean(axis=axis)
            if not native:
                backdrop=np.maximum(b0,b1)
                mask=line-(backdrop if axis==0 else backdrop[:,None])>12
            else:
                # Preserve the >12 contrast threshold on the card's inner side.
                # A second >12 positive BGR vector must rise above BOTH sides,
                # so a flat lighting/shadow step cannot become a card outline.
                backdrop=b1 if inside else b0
                inner=line-(backdrop if axis==0 else backdrop[:,None])>12
                c0=crop(color,before).mean(axis=axis)
                c1=crop(color,after).mean(axis=axis)
                outer=np.maximum(c0,c1)
                if axis==1:outer=outer[:,None,:]
                delta=np.maximum(crop(color,area)-outer,0)
                mask=inner & (np.linalg.norm(delta,axis=2)>12)
            return float(np.max(np.mean(mask,axis=1-axis)))
        for i in range(9):
            x,y=65+i%3*646,190+i//3*263
            # At least three long outer edges are required, away from portraits.
            strengths=[]
            for edge_y,inside in ((y,True),(y+247,False)):
                strengths.append(strength((x+190,edge_y-4,x+605,edge_y+5),
                                          (x+190,edge_y-9,x+605,edge_y-6),
                                          (x+190,edge_y+7,x+605,edge_y+10),0,inside))
            for edge_x,inside in ((x,True),(x+625,False)):
                strengths.append(strength((edge_x-4,y+20,edge_x+5,y+227),
                                          (edge_x-9,y+20,edge_x-6,y+227),
                                          (edge_x+7,y+20,edge_x+10,y+227),1,inside))
            top_evidence={}
            if native:
                top_evidence={'original_top':strengths[0],'fallback_top':None,
                              'fallback_top_roi':None,'fallback_top_used':False}
                if strengths[0]<=.65 and all(v>.65 for v in strengths[2:]):
                    # The fixed top span is above the portrait and excludes the
                    # rightmost 135-pixel boost badge. It is longer than the
                    # original span; the same two-sided color evidence applies.
                    area=(x+20,y-4,x+490,y+5)
                    fallback=strength(area,(x+20,y-9,x+490,y-6),
                                      (x+20,y+7,x+490,y+10),0,True)
                    top_evidence.update(fallback_top=fallback,fallback_top_roi=area,
                                        fallback_top_used=fallback>.65)
                    strengths[0]=max(strengths[0],fallback)
            present=sum(v>.65 for v in strengths)>=3
            if native:present=present and all(v>.65 for v in strengths[2:])
            if present:occupied.append(i)
            evidence.append({'index':i,'producer':'paired_card_edges_native_color' if native else 'paired_card_edges',
                             'edge_coverage':strengths,'status':'occupied' if present else
                             'ambiguous' if max(strengths)>.35 else 'empty',**top_evidence})
        # Candidate cards occupy consecutive slots; gaps indicate animation/occlusion.
        complete=occupied and occupied==list(range(len(occupied))) and not any(
            row['status']=='ambiguous' for row in evidence)
        return (tuple(occupied) if complete else None),evidence

    def attribute_observation(self,*,defer_scores=False):
        from src.crafting_decision import AttributeObservation
        if defer_scores:self._defer_attribute_scores=True
        slots=self.card_slots()
        try:options=tuple(self.options())
        except ChoiceUnreadable:
            self._options=[];options=()
        area=self.button('刷新',(1250,970,1530,1090))
        return AttributeObservation(options,len(slots) if slots is not None else None,
                                    self.enabled(area) if area else None,
                                    slots is not None and set(slots)=={o.index for o in options},'paired_card_edges',
                                    tuple(self.attribute_slot_status(i) for i in slots or ()))

    def attribute_slot_status(self,index):
        area=self.option_areas()[index]
        raw=getattr(self,'_slot_retry_raw',{}).get(index,self.text_in(area))
        match=attribute_delta(raw)
        group,attr=group_attr(raw)
        missing=[]
        if not raw:missing.append('text')
        if not group:missing.append('group')
        if not attr:missing.append('attr')
        if not match:missing.append('value_parse_failed' if attr else 'value')
        option=next((o for o in getattr(self,'_options',()) if o.index==index),None)
        if option is not None:
            # Identity completeness and action-dependent fields are separate.
            # A parsed card can still need a score or boost-marker producer.
            missing=[]
            if not option.attr or not (option.group or option.groups):missing.append('attribute_scope')
            if not option.boost_known:missing.append('boost_marker')
            if not option.score_known:missing.append('score')
        elif not missing:missing.append('candidate_identity')
        return {'index':index,'present':True,'group':group,'attr':attr,
                'value':int(match[2]) if match else None,'missing':tuple(missing),'raw':raw,
                'producers':{field:self.attribute_retry_producers(field) for field in missing},
                'geometry':next((row for row in getattr(self,'card_slot_evidence',()) if row['index']==index),None)}

    @staticmethod
    def attribute_retry_producers(field):
        if field=='score':return ('score_roi_native','score_roi_2x','score_digits_roi','score_roi_gray','score_digits_binary')
        if field in ('boost_marker','attribute_scope','candidate_identity','text','group','attr','value','value_parse_failed'):
            return ('attribute_slot_native','attribute_slot_2x')
        return ()

    def retry_attribute_field(self,index,field,variant):
        """Run one producer variant on this frame; temporal recovery belongs to the runner."""
        producers=self.attribute_retry_producers(field)
        evidence={'success':False,'retry_field':field,'targeted_slot':index,'producer_variant':variant,
                  'producer_count':len(producers),'producer':producers[variant-1] if 0<variant<=len(producers) else '',
                  'raw':'','confidence':None,'accepted':False,'rejected_reason':''}
        evidence.update(self.frame_metadata())
        if not hasattr(self,'frame') or not hasattr(self,'task'):
            evidence['rejected_reason']='producer_frame_unavailable';return evidence
        if self.kind!='attribute' or index not in (self.card_slots() or ()):
            evidence['rejected_reason']='slot_not_verified';return evidence
        if not 0<variant<=len(producers):
            evidence['rejected_reason']='producer_variants_exhausted';return evidence
        area=self.option_areas()[index]
        evidence['option_area']=tuple(area)
        if field=='score':
            option=next((o for o in self.options() if o.index==index),None)
            if option is None:
                evidence['rejected_reason']='candidate_identity_missing';return evidence
            if evidence['producer'] in ('score_roi_gray','score_digits_binary') and any(
                    row['accepted'] for row in getattr(self,'_score_reads',{}).get(index,[])):
                evidence['rejected_reason']='prior_score_producer_accepted';return evidence
            read=self._read_score_producer(index,area,evidence['producer'])
            reads=getattr(self,'_score_reads',{}).get(index,[])
            # Replace this producer's observation; preserve independent producers
            # so another accepted value cannot be silently overwritten.
            reads=[row for row in reads if row['source']!=read['source']]+[read]
            self._remember_score_reads(index,reads)
            score=self._merge_score_reads(index,area,reads)
            (option.score,option.score_known,option.score_confidence,option.score_evidence_id,
             option.score_source,option.score_rejected_reason)=score
            evidence.update(score_roi=read['score_roi'],threshold=read['threshold'],parsed_value=read['value'],
                            raw=read['raw'],confidence=read['confidence'],accepted=read['accepted'],
                            success=option.score_known,rejected_reason=option.score_rejected_reason or read['rejected_reason'])
            return evidence
        option=next((o for o in self.options() if o.index==index),None)
        if field in ('boost_marker','attribute_scope') and option is not None:
            raw,confidence,value_text=self._read_attribute_slot(index,variant)
            if field=='attribute_scope':
                match=attribute_delta(value_text)
                prefix=value_text[:match.end(1)] if match else ''
                group,_=group_attr(prefix);groups=group_scope(prefix)
                success=bool((group or groups) and match and
                             (not option.attr or match[1]==option.attr) and int(match[2])==option.value)
                if success:
                    option.group,option.groups,option.attr=group,groups,match[1]
                    option.name=group+option.attr
            else:
                option.boost='激增' in raw or '暴涨' in raw
                option.boost_known=option.boost or self.attribute_boost_absent(area)
                success=option.boost_known
        else:
            success=self.retry_attribute_slot(index,variant)
            raw=getattr(self,'_slot_retry_raw',{}).get(index,'')
            confidence=getattr(self,'_slot_retry_confidence',{}).get(index)
        evidence.update(raw=raw,confidence=confidence,success=success,accepted=success,
                        rejected_reason='' if success else field+'_not_read')
        return evidence

    def retry_attribute_slot(self,index,attempt):
        """Read only one occupied card; keep its original screen coordinates."""
        if self.kind!='attribute' or index not in (self.card_slots() or ()):return False
        area=self.option_areas()[index]
        raw,_,value_text=self._read_attribute_slot(index,attempt)
        option=self.attribute_option(index,area,raw,value_text=value_text)
        if option is None:return False
        self._options=[o for o in self.options() if o.index!=index]+[option]
        self._options.sort(key=lambda o:o.index)
        return True

    def _read_attribute_slot(self,index,attempt):
        area=self.option_areas()[index]
        x,y,x2,y2=area
        scale=2 if attempt>1 else 1
        crop=self.frame[y:y2,x:x2]
        if scale>1:
            crop=cv2.resize(crop,None,fx=2,fy=2,interpolation=cv2.INTER_CUBIC)
        boxes=self.task.ocr(frame=crop,threshold=.55)
        raw=''.join(compact(b.name) for b in sorted(boxes,key=lambda b:(round(b.y/15),b.x)))
        # Use this crop's boxes, with the same fixed title/value field as the
        # full-frame producer. Initial boxes may have missed the value entirely.
        fields=[b for b in boxes if 180<= (b.x+b.width/2)/scale <=x2-x-12
                and 15<= (b.y+b.height/2)/scale <=90]
        value_text=''.join(compact(b.name) for b in sorted(fields,key=lambda b:(round(b.y/15),b.x)))
        confidences=[float(b.confidence) for b in boxes if hasattr(b,'confidence')]
        confidence=min(confidences) if confidences else None
        if not hasattr(self,'_slot_retry_raw'):self._slot_retry_raw={}
        if not hasattr(self,'_slot_retry_confidence'):self._slot_retry_confidence={}
        self._slot_retry_raw[index]=raw
        self._slot_retry_confidence[index]=confidence
        return raw,confidence,value_text

    def attribute_boost_absent(self,area):
        x,y,x2,_=map(int,area)
        badge=cv2.cvtColor(self.frame[y:y+35,x2-135:x2-5],cv2.COLOR_BGR2HSV)
        h,s,v=cv2.split(badge)
        return bool(np.mean((h>=35)&(h<90)&(s>130)&(v>160))<.20)

    @staticmethod
    def option_areas():
        return [(65+c*646,190+r*263,690+c*646,437+r*263) for r in range(3) for c in range(3)]

    def portrait_signature(self,area):
        if not hasattr(self,'frame'):return ''
        x,y,_,_=map(int,area)
        crop=self.frame[y+15:y+90,x+20:x+150]
        if not crop.size:return ''
        gray=cv2.resize(cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY),(8,8))
        if float(gray.std())<8:return ''
        return ''.join('1' if value>gray.mean() else '0' for value in gray.flat)

    def frame_metadata(self):
        if not hasattr(self,'observation_epoch'):self.observation_epoch=str(uuid4())
        if not hasattr(self,'frame_id'):self.frame_id=self.observation_epoch
        if not hasattr(self,'frame_hash'):
            self.frame_hash=hashlib.sha256(self.frame.tobytes()).hexdigest() if hasattr(self,'frame') else ''
        return {'frame_id':self.frame_id,'observation_epoch':self.observation_epoch,'frame_hash':self.frame_hash}

    @staticmethod
    def score_roi_for(area,source='current_frame'):
        """The card is the only geometry anchor, for every row and column."""
        x,y=map(int,area[:2])
        if source=='score_digits_roi':dx,dy,width,height=70,120,100,46
        elif source=='score_digits_binary':dx,dy,width,height=84,120,86,58
        elif source=='score_roi_gray':dx,dy,width,height=55,110,110,60
        else:dx,dy,width,height=10,100,158,77
        return x+dx,y+dy,x+dx+width,y+dy+height

    def _score_observation(self,boxes,source,threshold,area,text=None):
        read=self._score_read(boxes,source,threshold,text)
        read.update(self.frame_metadata(),option_area=tuple(area),score_roi=self.score_roi_for(area,source))
        return read

    def attribute_score(self,index,area):
        """All reads belong to this frame; an equal prior score is still current evidence."""
        if not hasattr(self,'observation_epoch'):self.observation_epoch=str(uuid4())
        cached=getattr(self,'_score_reads',{}).get(index,())
        if (cached and all(row.get('observation_epoch')==self.observation_epoch and
                           tuple(row.get('option_area',()))==tuple(area) for row in cached) and
                getattr(self,'_score_pixel_hashes',{}).get(index)==self._score_pixel_hash(area)):
            return self._merge_score_reads(index,area,cached)
        score_area=self.score_roi_for(area)
        primary_boxes=self.boxes_in(score_area) if hasattr(self,'boxes') else []
        reads=[self._score_observation(primary_boxes,'current_frame',.75,area,self.text_in(score_area))]
        if reads[0]['accepted']:
            self._remember_score_reads(index,reads)
            return self._merge_score_reads(index,area,reads)
        if getattr(self,'_defer_attribute_scores',False):
            self._remember_score_reads(index,reads)
            return self._merge_score_reads(index,area,reads)
        if hasattr(self,'frame') and hasattr(self,'task'):
            reads.extend(self._read_score_producer(index,area,source)
                         for source in self.attribute_retry_producers('score')[:3])
            # The gray crop is a bounded fallback for scores the existing
            # native, enlarged and digit crops could not read on this frame.
            if not any(row['accepted'] for row in reads):
                reads.append(self._read_score_producer(index,area,'score_roi_gray'))
            if not any(row['accepted'] for row in reads):
                reads.append(self._read_score_producer(index,area,'score_digits_binary'))
        self._remember_score_reads(index,reads)
        return self._merge_score_reads(index,area,reads)

    @staticmethod
    def _score_read(boxes,source,threshold,text=None):
        raw=compact(text if text is not None else ''.join(b.name for b in boxes))
        nums=re.findall(r'\d+',raw)
        confidences=[float(b.confidence) for b in boxes if hasattr(b,'confidence') and re.search(r'\d',b.name)]
        if not confidences:confidences=[float(b.confidence) for b in boxes if hasattr(b,'confidence')]
        confidence=min(confidences) if confidences else None
        reason=''
        if len(nums)>1:reason='ambiguous_score'
        elif not nums:reason='score_not_read'
        elif confidence is not None and confidence<threshold:reason='score_confidence_below_threshold'
        elif not re.fullmatch(r'[A△▲ΛXx]?\d{1,5}',raw):reason='ambiguous_score_text'
        elif source!='current_frame' and confidence is None:reason='score_confidence_missing'
        elif source=='score_roi_gray' and len(nums[0])==1:
            # A lone gray fallback can merge adjacent thin digits (real 77 ->
            # A7). Verify the digit pixels with the final bounded producer.
            reason='single_digit_gray_requires_digit_verification'
        value=int(nums[0]) if len(nums)==1 else None
        return {'source':source,'producer':source,'raw':raw,'confidence':confidence,'threshold':threshold,
                'value':value,'parsed_value':value,'accepted':not reason,'rejected_reason':reason}

    def _read_score_producer(self,index,area,source):
        x,y,x2,y2=self.score_roi_for(area,source)
        pixels=self.frame
        native=getattr(self,'native_frame',None)
        native_roi=None
        if (source!='score_roi_gray' and isinstance(native,np.ndarray) and
                native.ndim==3 and native.shape[2]==3):
            # Full-page OCR already reads client pixels through ReferenceTask.
            # Local score OCR must use that same capture, rather than a second
            # resize through 3840 -> 2048 that can merge the A icon with digits.
            sx,sy=native.shape[1]/2048,native.shape[0]/1152
            native_roi=(round(x*sx),round(y*sy),round(x2*sx),round(y2*sy))
            x,y,x2,y2=native_roi;pixels=native
        # Retain the calibrated reference-gray crop as the final bounded
        # fallback. Thin 11 strokes can disappear in a native color crop;
        # this independent producer still uses this capture's exact resize.
        crop=pixels[y:y2,x:x2]
        if source=='score_roi_2x':
            crop=cv2.resize(crop,None,fx=2,fy=2,interpolation=cv2.INTER_CUBIC)
        elif source=='score_roi_gray':
            crop=cv2.cvtColor(cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY),cv2.COLOR_GRAY2BGR)
        elif source=='score_digits_binary':
            # Isolate bright digit strokes from portrait/ribbon texture. This
            # is a pixel producer; it never replaces OCR characters/numbers.
            gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
            crop=cv2.threshold(gray,185,255,cv2.THRESH_BINARY)[1]
            crop=cv2.resize(crop,None,fx=3,fy=3,interpolation=cv2.INTER_NEAREST)
            crop=cv2.copyMakeBorder(crop,20,20,20,20,cv2.BORDER_CONSTANT,value=0)
            crop=cv2.cvtColor(crop,cv2.COLOR_GRAY2BGR)
        # Padding can merge thin digit strokes with the score icon; only the
        # enlarged variant needs it. The other crops keep native dimensions.
        if source=='score_roi_2x' and native_roi is None:
            crop=cv2.copyMakeBorder(crop,20,20,20,20,cv2.BORDER_REPLICATE)
        # Keep rejected raw OCR for diagnostics. This is a retrieval cutoff;
        # acceptance remains exactly .65 for every bounded score ROI producer.
        boxes=self.task.ocr(frame=crop,threshold=.000001)
        read=self._score_observation(boxes,source,.65,area)
        if source=='score_digits_binary':
            read.update(pixel_threshold=185,scale=3,padding=20,interpolation='NEAREST')
        if native_roi is not None:
            read.update(native_roi=native_roi,native_size=tuple(native.shape[1::-1]),
                        pixel_source='same_capture_client_pixels')
        elif source=='score_roi_gray':
            read.update(reference_roi=self.score_roi_for(area,source),
                        reference_size=tuple(self.frame.shape[1::-1]),
                        pixel_source='same_capture_reference_pixels')
        return read

    def _remember_score_reads(self,index,reads):
        if not hasattr(self,'_score_reads'):self._score_reads={}
        self._score_reads[index]=reads
        if reads:
            if not hasattr(self,'_score_pixel_hashes'):self._score_pixel_hashes={}
            self._score_pixel_hashes[index]=self._score_pixel_hash(reads[0]['option_area'])

    def _score_pixel_hash(self,area):
        if not hasattr(self,'frame'):return None
        # Union of all bounded score ROIs; changes cannot inherit old reads.
        x,y=map(int,area[:2]);x,y,x2,y2=x+10,y+100,x+170,y+178
        digest=hashlib.sha256(self.frame[y:y2,x:x2].tobytes())
        native=getattr(self,'native_frame',None)
        if isinstance(native,np.ndarray):
            sx,sy=native.shape[1]/2048,native.shape[0]/1152
            digest.update(native[round(y*sy):round(y2*sy),round(x*sx):round(x2*sx)].tobytes())
        return digest.hexdigest()

    def _merge_score_reads(self,index,area,reads):
        if not hasattr(self,'observation_epoch'):self.observation_epoch=str(uuid4())
        accepted=[row for row in reads if row['accepted']]
        values={row['value'] for row in accepted}
        if len(values)>1:
            conflict='score_producer_conflict:'+','.join(row['source']+'='+str(row['value']) for row in accepted)
            return 0,False,None,'','',conflict
        if accepted:
            # Equal trusted reads agree; choose the established source order,
            # without allowing a less confident conflicting number to win.
            order=('current_frame',)+self.attribute_retry_producers('score')
            row=min(accepted,key=lambda read:order.index(read['source']))
            fingerprint=json.dumps((self.observation_epoch,index,tuple(area),row['source'],row['raw'],row['value']),ensure_ascii=False)
            evidence_id=hashlib.sha256(fingerprint.encode('utf8')).hexdigest()
            return row['value'],True,row['confidence'],evidence_id,row['source'],''
        return 0,False,None,'','', ';'.join(row['source']+':'+row['rejected_reason'] for row in reads)

    def score_evidence(self):
        metadata=self.frame_metadata()
        return [{**metadata,'option_area':tuple(o.area),
                 'score_roi':self.score_roi_for(o.area,o.score_source) if len(o.area)>=2 else None,
                 'candidate_identity':{'index':o.index,'groups':tuple(o.groups) or (o.group,),
                                       'attribute':o.attr,'value':o.value,'boost':o.boost,
                                       'portrait':o.portrait_signature},
                 'score_value':o.score if o.score_known else None,'score_confidence':o.score_confidence,
                 'score_evidence_id':o.score_evidence_id,'score_source':o.score_source,
                 'score_raw':next((row['raw'] for row in getattr(self,'_score_reads',{}).get(o.index,[])
                                   if row['source']==o.score_source),''),
                 'producer_reads':getattr(self,'_score_reads',{}).get(o.index,[]),
                 'rejected_reason':o.score_rejected_reason}
                for o in self.options()]

    def reuse_attribute_evidence(self,previous):
        """Reuse a card only when its complete current pixels are unchanged.

        The runner calls this during observation recovery, before any input.
        Different page epochs keep distinct score IDs and the original source.
        """
        if self.kind!='attribute' or previous.kind!='attribute' or not hasattr(previous,'frame'):
            return ()
        self._defer_attribute_scores=getattr(previous,'_defer_attribute_scores',False)
        slots=self.card_slots()
        if slots is None:return ()
        reused={}
        for option in getattr(previous,'_options',()):
            if option.index not in slots or tuple(option.area)!=tuple(self.option_areas()[option.index]):continue
            x,y,x2,y2=map(int,option.area)
            if not np.array_equal(self.frame[y:y2,x:x2],previous.frame[y:y2,x:x2]):continue
            native=getattr(self,'native_frame',None);old_native=getattr(previous,'native_frame',None)
            if isinstance(native,np.ndarray) or isinstance(old_native,np.ndarray):
                if not isinstance(native,np.ndarray) or not isinstance(old_native,np.ndarray) or native.shape!=old_native.shape:
                    continue
                sx,sy=native.shape[1]/2048,native.shape[0]/1152
                nx,ny,nx2,ny2=round(x*sx),round(y*sy),round(x2*sx),round(y2*sy)
                if not np.array_equal(native[ny:ny2,nx:nx2],old_native[ny:ny2,nx:nx2]):continue
            reused[option.index]=replace(option)
            if option.score_known:
                reads=getattr(previous,'_score_reads',{}).get(option.index,())
                if not reads or not option.score_evidence_id:
                    reused.pop(option.index);continue
                reads=[dict(row,**self.frame_metadata(),reused_from_epoch=previous.observation_epoch,
                            previous_score_evidence_id=option.score_evidence_id) for row in reads]
                self._remember_score_reads(option.index,reads)
                cached=reused[option.index]
                (cached.score,cached.score_known,cached.score_confidence,cached.score_evidence_id,
                 cached.score_source,cached.score_rejected_reason)=self._merge_score_reads(option.index,option.area,reads)
        self._attribute_reuse=reused
        return tuple(sorted(reused))

    def attribute_option(self,index,area,raw,identity=None,*,value_text=None):
        cached=getattr(self,'_attribute_reuse',{}).get(index)
        if cached is not None and tuple(cached.area)==tuple(area):
            option=replace(cached)
            if not option.score_known:
                (option.score,option.score_known,option.score_confidence,option.score_evidence_id,
                 option.score_source,option.score_rejected_reason)=self.attribute_score(index,area)
            return option
        # The score belongs to the portrait on the left. When its A icon is
        # missed, whole-card concatenation turns current 攻击+43 and 67 into
        # +4367. Read the existing title/value region from this frame's boxes;
        # never split digits heuristically or borrow an older numeric value.
        if value_text is None:
            value_text=raw
            if hasattr(self,'boxes'):
                x,y,x2,_=map(int,area)
                value_text=self.text_in((x+180,y+15,x2-12,y+90))
        match=attribute_delta(value_text)
        if not match:return None
        prefix=value_text[:match.end(1)]
        group,attr=group_attr(prefix)
        if not attr:return None
        option=Option(index,area=tuple(map(int,area)))
        option.group,option.attr=group,attr
        option.groups=group_scope(prefix)
        option.value=int(match[2]);option.boost='激增' in raw or '暴涨' in raw
        if not option.boost:
            option.boost_known=self.attribute_boost_absent(area)
        option.overflow=self.attribute_overflow(area)
        option.name=option.group+option.attr
        score_area=self.score_roi_for(area)
        (option.score,option.score_known,option.score_confidence,option.score_evidence_id,
         option.score_source,option.score_rejected_reason)=self.attribute_score(index,area)
        option.rarity=self.rarity(score_area)
        option.rarity_known=option.rarity>0
        option.portrait_signature=self.portrait_signature(area)
        if not group:
            # Only explicit UI occlusion can borrow scope from a reliable snapshot.
            # Stable numeric fields AND portrait AND occupied slot must still match.
            if (identity is None or not (identity.group or identity.groups) or not identity.boost_known or
                    identity.index!=index or '属性超上限' not in raw or '折损' not in raw or
                    group_scope(prefix) or index not in (self.card_slots() or ()) or
                    tuple(area)!=tuple(identity.area) or not option.boost_known or
                    (option.attr,option.value,option.boost)!=(identity.attr,identity.value,identity.boost) or
                    not option.score_known or not identity.score_known or option.score!=identity.score or
                    not option.portrait_signature or not identity.portrait_signature or
                    len(identity.portrait_signature)!=64 or
                    sum(a!=b for a,b in zip(option.portrait_signature,identity.portrait_signature))>4):
                return None
            option=replace(option,group=identity.group,groups=identity.groups,name=identity.name)
        return option

    def recover_attribute_identities(self,snapshots):
        """Restore only occluded slots; caller must still associate the whole checkpoint."""
        if self.kind!='attribute':return
        options=list(self.options());present={o.index for o in options}
        for snapshot in snapshots:
            if snapshot.index in present or not 0<=snapshot.index<len(self.option_areas()):continue
            area=self.option_areas()[snapshot.index]
            option=self.attribute_option(snapshot.index,area,self.text_in(area),snapshot)
            if option:options.append(option)
        self._options=sorted(options,key=lambda o:o.index)

    def mission_areas(self):
        if hasattr(self,'_mission_areas'):return self._mission_areas
        anchors=[b for b in self.boxes if '系统研发目标' in compact(b.name) and 240<b.y<480]
        if len(anchors)!=3:raise ChoiceUnreadable('变量属性须读到三张任务卡标题')
        centers=sorted(b.x+b.width/2 for b in anchors)
        width=min(625,min(b-a-20 for a,b in zip(centers,centers[1:])))
        self._mission_areas=[(max(20,c-width/2),280,min(2020,c+width/2),945) for c in centers]
        return self._mission_areas

    def retry_mission_slot(self,index,attempt):
        """Replace OCR boxes only within one mission card; no input or full-page OCR."""
        area=self.mission_areas()[index];x,y,x2,y2=map(int,area)
        scale=2 if attempt>1 else 1
        crop=self.frame[y:y2,x:x2]
        if scale>1:crop=cv2.resize(crop,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
        from types import SimpleNamespace
        boxes=self.task.ocr(frame=crop,threshold=.55)
        self.boxes=[b for b in self.boxes if b not in self.boxes_in(area)]+[
            SimpleNamespace(x=x+b.x/scale,y=y+b.y/scale,width=b.width/scale,height=b.height/scale,name=b.name)
            for b in boxes]
        if hasattr(self,'_options'):del self._options

    def attribute_overflow(self,area):
        """Only saturated text strokes in the attribute line, away from score/portrait."""
        if not hasattr(self,'frame'):return None
        x,y,x2,_=map(int,area)
        crop=self.frame[y+72:y+102,x+195:min(x+545,x2-20)]
        hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV);h,s,v=cv2.split(hsv)
        # Top-hat removes broad blue card fills; connected components reject bars.
        bright=cv2.morphologyEx(v,cv2.MORPH_TOPHAT,np.ones((7,7),np.uint8))>35
        mask=((s>110)&(v>150)&bright).astype(np.uint8)
        count,labels,stats,_=cv2.connectedComponentsWithStats(mask)
        strokes=np.zeros(mask.shape,bool)
        for i in range(1,count):
            _,_,w,height,pixels=stats[i]
            if 2<=height<=30 and w<=40 and pixels>=3:strokes|=labels==i
        blue=np.count_nonzero(strokes&(h>=90)&(h<=118))
        green=np.count_nonzero(strokes&(h>=35)&(h<90))
        if blue>=70 and blue>green*3:return True
        if green>=70 and green>blue*3:return False
        return None

    def options(self):
        if hasattr(self,'_options'):return self._options
        if self.kind in ('attribute','skill','rest'):
            areas=self.option_areas()
            if self.kind=='skill':
                slots=self.card_slots()
                if slots is not None:areas=[areas[index] for index in slots]
        elif self.kind in ('core_attribute','core_skill'):
            areas=[(575+c*481,290,1035+c*481,950) for c in range(3)]
        elif self.kind=='mission':areas=self.mission_areas()
        else:
            anchors=[b for b in self.boxes if '系统研发目标' in compact(b.name) and 240<b.y<480]
            if not anchors:raise ChoiceUnreadable('没有识别到任务条件卡片')
            centers=sorted(b.x+b.width/2 for b in anchors)
            width=min(625,min((b-a-20 for a,b in zip(centers,centers[1:])),default=600))
            areas=[(max(20,c-width/2),280,min(2020,c+width/2),945) for c in centers]
        result=[]
        for i,area in enumerate(areas):
            t=self.text_in(area)
            if not t and not (self.kind=='attribute' and i in getattr(self,'_attribute_reuse',{})):
                if self.kind=='mission':result.append(Option(i,area=tuple(map(int,area)),progress_known=False,reward_known=False))
                continue
            if self.kind=='attribute':
                option=self.attribute_option(i,area,t,getattr(self,'_verified_attribute_targets',{}).get(i))
                if option:result.append(option)
                continue
            o=Option(i,area=tuple(map(int,area)))
            if self.kind=='skill':
                o=self.skill_option(i,area)
                if o is None:continue
            elif self.kind=='rest':
                m=re.search(r'(\d+)个节点',t)
                if not m:continue
                o.value=int(m[1]);o.name='休整'+str(o.value)
            elif self.kind in ('core_attribute','mission'):
                m=re.search(r'((?:(?:火力|重盾|工程|医疗|突击|狙击|隐刃|指挥官)[/／、]?)+)(?:队员)?(最大生命|攻击|防御)达到[+＋](\d+)',t)
                if not m:
                    if self.kind!='mission':continue
                    o.progress_known=False;o.reward_known=False
                    result.append(o);continue
                o.group,o.attr=group_attr(m[0]);o.goal=int(m[3]);o.name=m[0]
                o.groups=group_scope(m[1])
                progress=re.search(r'[（(](\d+)/(\d+)[）)]',t)
                o.progress_known=bool(progress and int(progress[2])==o.goal)
                if o.progress_known:o.current=int(progress[1])
                o.reward_group,o.reward_attr,o.reward_groups,o.reward_text,o.reward_value=mission_reward(t)
                o.reward_known=bool(o.reward_text)
            elif self.kind=='factor':
                title=self.factor_title_native(area) or self.text_in((area[0],580,area[2],635))
                if not title:continue
                o.name=next((n for n in ('好整以暇','声东击西') if n in title),title)
            elif self.kind=='core_skill':o.name=core_skill_name(t)
            else:o.name=t[:45]
            if self.kind in ('attribute','skill'):
                score_area=(area[0]+10,area[1]+100,area[0]+168,area[1]+177)
            else:score_area=(area[0],area[3]-95,area[2],area[3])
            nums=re.findall(r'\d+',self.text_in(score_area))
            if not nums and self.kind in ('attribute','skill'):
                x,y,x2,y2=map(int,score_area)
                crop=self.frame[y:y2,x:x2]
                crop=cv2.resize(crop,None,fx=2,fy=2,interpolation=cv2.INTER_CUBIC)
                crop=cv2.copyMakeBorder(crop,20,20,20,20,cv2.BORDER_REPLICATE)
                extra=self.task.ocr(frame=crop,threshold=.65)
                nums=re.findall(r'\d+',''.join(b.name for b in extra))
            o.score=int(nums[-1]) if nums else 0
            o.score_known=bool(nums)
            native=getattr(self,'native_frame',None)
            o.rarity=(self._rest_rarity_native(area) if self.kind=='rest' and native is not None and
                      native.shape==(720,1280,3) else self.rarity(score_area))
            o.rarity_known=o.rarity>0
            if self.kind=='core_skill':o.visual_enabled=self.core_skill_visual_enabled(area,t)
            result.append(o)
        if not result:raise ChoiceUnreadable('当前画面尚未读到可辨认选项')
        self._options=result
        return result

    def skill_option(self,index,area):
        """Parse this fixed title and rarity only; missing level remains explicit."""
        if not 0<=index<len(self.option_areas()) or tuple(area)!=self.option_areas()[index]:return None
        x,y,x2,_=area
        raw_title=self.text_in((x+180,y+15,x2-12,y+75))
        title=raw_title
        if not title:return None
        from src.crafting_policy import skill_title
        title=skill_title(title)
        lv=re.search(r'(?i)L[VY][.·:]?(\d+)\+(\d+)',title)
        prefix=title[:lv.start()] if lv else re.split(r'(?i)L[VY]',title)[0]
        # The observed blue rarity diamond is OCR'd as A before 强击.
        # Remove only that leading marker; never collapse a Chinese name suffix.
        prefix=re.sub(r'^A(?=[\u4e00-\u9fff])','',prefix)
        name=skill_name(prefix)
        if not re.fullmatch(r'[\u4e00-\u9fff]{1,16}(?:[·•・:：][\u4e00-\u9fff]{1,16})?',name):return None
        option=Option(index,name=name,area=tuple(area),module=skill_module(prefix),raw_name=raw_title,
                      level_known=bool(lv and lv.end()==len(title)))
        if lv:option.level,option.gain=map(int,lv.groups())
        option.rarity=self.rarity((x+10,y+100,x+168,y+177))
        option.rarity_known=option.rarity>0
        return option

    def skill_cards(self):
        """Only missing names obstruct absence reasoning; other fields stay local."""
        slots=self.card_slots()
        try:parsed=tuple(o.index for o in self.options() if o.name)
        except ChoiceUnreadable:parsed=()
        if slots is None:
            # A complete skill title and its two long side edges establish a
            # card even when translucent top/bottom edges blend into the HUD.
            options={o.index:o for o in getattr(self,'_options',())}
            rows=getattr(self,'card_slot_evidence',())
            present=[];unresolved=False
            for row in rows:
                option=options.get(row['index'])
                readable=(option is not None and option.name and option.level_known and option.rarity_known)
                sides=row['edge_coverage'][2:]
                corroborated=readable and len(sides)==2 and all(v>.65 for v in sides)
                if row['status']=='occupied' or corroborated:present.append(row['index'])
                elif row['status']=='ambiguous':unresolved=True
            if present and present==list(range(len(present))) and not unresolved:
                slots=tuple(present)
        return {'slots':slots,'parsed':parsed,'missing':tuple(i for i in slots if i not in parsed) if slots is not None else (),
                'geometry_known':slots is not None}

    def target(self,option):
        """Validate just this card; unrelated obscured cards do not veto it."""
        if self.kind=='skill':
            self.target_evidence='insufficient'
            complete=lambda o:(bool(o.name) and o.level_known is True and o.rarity_known is True
                and type(o.level) is int and o.level>=0 and type(o.gain) is int and o.gain>0 and o.rarity in (1,2,3))
            if not complete(option):return None
            current=self.skill_option(option.index,option.area)
            if current is None:return None
            identity=lambda o:(o.index,tuple(o.area),o.name,o.module)
            changed=(identity(current)!=identity(option) or
                current.rarity_known is True and current.rarity in (1,2,3) and current.rarity!=option.rarity or
                current.level_known is True and type(current.level) is int and current.level>=0 and
                type(current.gain) is int and current.gain>0 and
                (current.level,current.gain)!=(option.level,option.gain))
            # A known changed field cannot be erased by another unread field.
            # LocalChoiceScreen corroborates any local mismatch on the same
            # full capture; incomplete matching identities still stop below.
            if changed:
                self.target_evidence='identity_changed'
                raise SkillIdentityChanged('当前技能已知身份字段与原选择不同，未沿用旧点击目标')
            if not complete(current):return None
            self.target_evidence='verified'
            return current.area
        if self.kind=='attribute':
            self.target_evidence='insufficient'
            if not 0<=option.index<len(self.option_areas()):return None
            area=self.option_areas()[option.index]
            candidate=self.attribute_option(option.index,area,self.text_in(area),option)
            identity=lambda o:(o.index,o.group,tuple(o.groups),o.attr,o.value,o.boost)
            if candidate is None:return None
            known_identity=lambda o:(o.index,o.group,tuple(o.groups),o.attr,o.value)
            if known_identity(candidate)!=known_identity(option):
                self.target_evidence='identity_changed'
                return None
            if candidate.boost_known is not True:return None
            if identity(candidate)==identity(option):
                self.target_evidence='verified'
                if not hasattr(self,'_verified_attribute_targets'):self._verified_attribute_targets={}
                self._verified_attribute_targets[option.index]=replace(option)
                if hasattr(self,'_options'):
                    self._options=sorted([o for o in self._options if o.index!=option.index]+[candidate],key=lambda o:o.index)
                return candidate.area
            self.target_evidence='identity_changed'
            return None
        for candidate in self.options():
            identity=lambda o:(o.index,core_skill_name(o.name) if self.kind=='core_skill' else o.name,
                               o.group,tuple(o.groups),o.attr,o.value,o.boost,o.goal)
            if identity(candidate)==identity(option):
                if self.kind=='core_skill' and (candidate.visual_enabled is not True or
                        candidate.rarity!=option.rarity or
                        (candidate.rarity!=3 and candidate.score!=option.score)):
                    return None
                if self.kind=='mission' and (not candidate.reward_known or not candidate.progress_known or
                        (candidate.current,candidate.reward_text,candidate.reward_group,candidate.reward_attr)!=
                        (option.current,option.reward_text,option.reward_group,option.reward_attr)):
                    return None
                return candidate.area
        return None

    def choice_click(self,area):
        x,y,x2,y2=area
        # Use the title, away from the linked effect text and untested empty padding.
        return (x+260,y+35,x+380,y+65) if self.kind in ('skill','attribute') else area
