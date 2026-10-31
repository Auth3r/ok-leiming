"""Current-pixel checks for an already decided attribute or skill card."""
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import cv2
import numpy as np

from src.crafting_vision import (CraftScreen,ChoiceUnreadable,SkillIdentityChanged,
    compact,attribute_delta,group_attr,group_scope)


class LocalChoiceScreen(CraftScreen):
    """A local observation cannot supply new candidates or a new decision.

    Header/modal strips, the chosen identity and the footer are freshly OCR'd.
    Production selection geometry and skill identity checks are retained.
    Explicit full observation discards the local boxes and runs normal OCR.
    """
    def __init__(self,task,frame,option,kind,native_frame,previous=None):
        self.task=task;self.native_frame=native_frame.copy()
        self.frame=cv2.resize(frame,(2048,1152))
        self.observation_epoch=str(uuid4());self.frame_id=self.observation_epoch
        self.frame_hash=hashlib.sha256(self.frame.tobytes()).hexdigest()
        self._lazy_pending=False;self._light_conflict=False;self._light_kind=None
        self._local_only=True;self._known_option=option;self._expected_kind=kind
        # An exact current native image supplies the same identity/modal pixels
        # as its trusted observation. The new packet/provenance still belongs to
        # this capture; only unchanged OCR boxes are reused, never a score guess.
        if (isinstance(previous,CraftScreen) and previous.kind==kind and
                not previous.__dict__.get('_light_conflict') and
                not previous.__dict__.get('_lazy_pending') and
                np.array_equal(native_frame,previous.__dict__.get('native_frame'))):
            self.kind=kind;self.boxes=list(previous.boxes)
            self.text=previous.text;self.header=previous.header;self.local_exit_reason=None
            self.local_pixel_witness={'exact_native_pixels':True,'previous_epoch':previous.observation_epoch}
            return
        native=cv2.resize(native_frame,(1280,720)) if native_frame.shape[:2]!=(720,1280) else native_frame
        x,y,x2,y2=map(int,option.area)
        regions=((18,48,600,106),(190,205,1100,330),(220,440,1050,565),
            (x+175,y+8,x2-4,y+96),(x2-140,y,x2-4,y+38),(1450,960,2015,1100))
        strips=[];segments=[];offset=0
        for index,(left,top,right,bottom) in enumerate(regions):
            if not 0<=left<right<=2048 or not 0<=top<bottom<=1152:
                raise ChoiceUnreadable('快速检查卡片区域超出当前截图')
            crop=(native if index<3 else self.frame)[top:bottom,left:right]
            strip=np.zeros((crop.shape[0]+16,1000,3),np.uint8)
            strip[:crop.shape[0],:crop.shape[1]]=crop
            strips.append(strip);segments.append((offset,offset+crop.shape[0],left,top))
            offset+=strip.shape[0]
        boxes=task.ocr(frame=np.concatenate(strips,axis=0),threshold=.9)
        texts=[''.join(compact(b.name) for b in boxes if start<=b.y+b.height/2<end)
            for start,end,_,_ in segments[:3]]
        modal=texts[1]+texts[2]
        titles={'attribute':('属性深度刻录','属性刻录'),'skill':('技能深度研习','技能研习')}
        header_boxes=[b for b in boxes if segments[0][0]<=b.y+b.height/2<segments[0][1]]
        bright=False
        for b in header_boxes:
            if re_title(b.name) not in titles.get(kind,()):continue
            left,top,right,bottom=regions[0]
            glyph=native[top+max(0,int(b.y)):top+min(bottom-top,int(b.y+b.height)),
                left+max(0,int(b.x)):left+min(right-left,int(b.x+b.width))]
            if glyph.size and np.mean(np.min(glyph,axis=2)>135)>.025:bright=True
        if not bright or any(word in modal for word in ('刷新','消耗模组','是否退出','快速智能研发')):
            self.local_exit_reason='page_or_modal_changed';self.ensure_full();return
        self.kind=kind
        self.boxes=[]
        for b in boxes:
            center=b.y+b.height/2
            for start,end,left,top in segments[3:]:
                if start<=center<end:
                    self.boxes.append(SimpleNamespace(x=left+b.x,y=top+b.y-start,
                        width=b.width,height=b.height,name=b.name,confidence=getattr(b,'confidence',None)))
                    break
        self.text=''.join(compact(b.name) for b in self.boxes)
        self.header='已决策候选局部复核'
        self.local_exit_reason=None

    def ensure_full(self):
        if self.__dict__.get('_local_only'):
            self._local_only=False
            self.__dict__.pop('boxes',None)
        return super().ensure_full()

    def options(self):
        if self.__dict__.get('_local_only'):
            raise ChoiceUnreadable('局部候选检查不能代替完整策略观察')
        return super().options()

    def button(self,label,area=(1450,960,2015,1100)):
        result=super().button(label,area)
        if result is None and label=='确认' and self.__dict__.get('_local_only'):
            # The stitched footer can miss a small current confirmation glyph.
            # Re-read this exact packet with the existing full-page producer;
            # a selected outline alone never authorizes a confirmation click.
            self.local_exit_reason='confirmation_footer_insufficient'
            self.ensure_full()
            if self.kind!=self._expected_kind:
                self.target_evidence='page_changed'
                return None
            return super().button(label,area)
        return result

    def target(self,option):
        if self.kind!=self._expected_kind:
            self.target_evidence='page_changed'
            return None
        if not self.__dict__.get('_local_only'):return super().target(option)
        if option is not self._known_option or self.kind!=self._expected_kind:return None
        if self.kind=='skill':
            try:result=super().target(option)
            except SkillIdentityChanged:
                # A complete local mismatch is corroborated on these same
                # pixels before revoking the old choice and deciding again.
                result=None
            if result:return result
            if self.__dict__.get('target_evidence') not in ('insufficient','identity_changed'):return None
            self.local_exit_reason=('skill_identity_insufficient' if self.target_evidence=='insufficient'
                                    else 'skill_identity_changed')
            self.local_initial_text=self.text
            self.ensure_full()
            if self.kind!=self._expected_kind:
                self.target_evidence='page_changed'
                return None
            return super().target(option)
        if self.kind!='attribute' or not 0<=option.index<len(self.option_areas()):return None
        area=self.option_areas()[option.index]
        if tuple(area)!=tuple(option.area):return None
        x,y,x2,_=area
        title=self.text_in((x+180,y+15,x2-12,y+90))
        match=attribute_delta(title)
        if not match:
            # Incomplete local text is not evidence that this candidate changed.
            # Reuse this capture, with the complete production page/identity gates.
            self.local_exit_reason='attribute_identity_insufficient'
            self.local_initial_text=self.text
            self.ensure_full()
            result=super().target(option) if self.kind==self._expected_kind else None
            if self.kind!=self._expected_kind:self.target_evidence='page_changed'
            return result
        prefix=title[:match.end(1)];group,attr=group_attr(prefix)
        # Occluded scope needs the existing full portrait/score corroboration.
        if not group:
            self.local_exit_reason='attribute_scope_occluded'
            self.ensure_full();return super().target(option)
        raw=self.text_in((x,y,x2,y+100))
        boost='激增' in raw or '暴涨' in raw
        if not boost and not self.attribute_boost_absent(area):
            self.local_exit_reason='attribute_boost_insufficient'
            self.ensure_full()
            result=super().target(option) if self.kind==self._expected_kind else None
            if self.kind!=self._expected_kind:self.target_evidence='page_changed'
            return result
        identity=(option.group,tuple(option.groups),option.attr,option.value,option.boost)
        result=area if (group,group_scope(prefix),attr,int(match[2]),boost)==identity else None
        self.target_evidence='verified' if result else 'identity_changed'
        return result


SAFE_HUB_FOLLOWUPS=('战术核心','属性刻录','属性深度刻录','技能研习','技能深度研习','因子','变量','快速休整')


def re_title(text):
    return compact(text).replace('·','').replace('•','').replace('・','')


class FastPathExit(ChoiceUnreadable):
    """A followup has not been sent; normal observation may resume."""


class LocalHubScreen(CraftScreen):
    """The safe hub operation must retain every pixel of the verified hub."""
    def __init__(self,task,frame,native_frame,previous):
        if (previous.kind=='hub' and np.array_equal(native_frame,previous.native_frame)
                and not previous.__dict__.get('_lazy_pending') and
                not previous.__dict__.get('_light_conflict')):
            self.__dict__.update(previous.__dict__)
            self.native_frame=native_frame.copy();self.task=task
            self.observation_epoch=str(uuid4());self.frame_id=self.observation_epoch
            self._local_only=True;self.local_exit_reason=None
            self.local_pixel_witness={'exact_native_pixels':True,'previous_epoch':previous.observation_epoch}
        else:
            super().__init__(task,frame)
            self.native_frame=native_frame.copy();self._local_only=False
            self.local_exit_reason='safe_hub_pixels_changed'

    def ensure_full(self):
        # The copied boxes were full-page OCR and remain exact current pixels.
        result=super().ensure_full()
        self._local_only=False
        return result
