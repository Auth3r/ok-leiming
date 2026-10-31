"""Read stage labels in the current reward strip, including its auto-scroll."""
import re
from statistics import median

from src.workflow import RecognitionTimeout

NODES=(200,400,600,800,1000)


class StageOffscreen(RecognitionTimeout):
    def __init__(self,node,anchors=None):
        self.node=node
        self.anchors=anchors or {}
        super().__init__(f'已解锁阶段{node}在可视范围外，需要重新观察奖励条')


def stages(flow, activity):
    task=flow.task
    labels=task.ocr(x=.371,y=.763,to_x=.97,to_y=.979,frame=task.frame,threshold=.95)
    anchors={}
    for box in labels:
        text=box.name.replace(' ','')
        if text in map(str,NODES) and box.y>=2160*.91:
            value=int(text)
            if value in anchors:raise RecognitionTimeout('每日阶段标签重复，停止领取')
            anchors[value]=box.x+box.width/2
    if len(anchors)<3:raise RecognitionTimeout('每日阶段标签不足三个，不能定位奖励条')
    pairs=sorted(anchors.items())
    spacing=median((x2-x1)/((n2-n1)/200) for (n1,x1),(n2,x2) in zip(pairs,pairs[1:]))
    if not 380<=spacing<=620:raise RecognitionTimeout('每日奖励条间距异常')
    origin=median(x-spacing*(n/200) for n,x in pairs)
    if any(abs(x-(origin+spacing*(n/200)))>25 for n,x in pairs):
        raise RecognitionTimeout('每日奖励条标签不能对应，停止领取')
    result=[]
    for node in NODES:
        center=round(origin+spacing*(node/200))
        roi=[max(1425,center-160),1620,min(3735,center+160),1950]
        visible=roi[2]-roi[0]>=40
        if not visible:
            if node<=activity:raise StageOffscreen(node,anchors)
            result.append(dict(node=node,status='LOCKED_OFFSCREEN',roi=None));continue
        gift=flow.vision.find('gift',roi)
        obtained=flow.vision.find('daily_obtained',roi)
        # The 200 node can sit partly beneath the fixed activity badge after
        # bulk claiming. Only that measured clipped region uses its alternative.
        if not obtained and node==200 and center<1600:
            obtained=flow.vision.find('daily_obtained_partial',roi)
        if gift and obtained:raise RecognitionTimeout('阶段奖励状态冲突')
        if obtained:status='CLAIMED'
        elif gift and node<=activity:status='CLAIMABLE'
        elif node>activity and node in anchors and not gift:status='LOCKED'
        else:raise RecognitionTimeout(f'阶段{node}缺少已获得或锁定证据')
        result.append(dict(node=node,status=status,roi=roi,label_observed=node in anchors,
                           label_x=center,confidence=float((gift or obtained).confidence) if (gift or obtained) else None))
    flow.emit('daily_stage_feedback',activity=activity,anchors=anchors,stages=result)
    return result
