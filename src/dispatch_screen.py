"""Current dispatch page evidence in the shared 4K reference coordinates."""
import re
import cv2
import numpy as np
import json
from ok import Box
from functools import lru_cache
from src.app_paths import APP_ROOT as ROOT
from src.dispatch_policy import parse_requirements,DispatchUnreadable,PROFESSIONS,Requirements,TACTIC

CONDITIONS=(2250,440,2790,1680)
FILTER=(3420,320,3740,450)
ROSTER=(2800,470,3780,1500)
SLOTS=(1980,1680,2830,2050)


def tactic_label(text):
    text=text.replace(' ','')
    return '一致战线' if text in ('致战线','-致战线','—致战线') else text


def read_dispatch_counter(flow,page='BLACK_SHIELD'):
    """One current isolated quota reading; callers require two fresh receipts."""
    regions={'BLACK_SHIELD':(.325,.84,.55,.96),'DISPATCH':(.745,.855,.88,.91)}
    if page not in regions:raise DispatchUnreadable('派遣次数页不支持')
    roi=regions[page];labels=[]
    if flow.vision.page(page):
        ocr=getattr(flow.task,'ocr',None)
        if callable(ocr):
            labels=ocr(x=roi[0],y=roi[1],to_x=roi[2],to_y=roi[3],
                       frame=flow.task.frame,threshold=.95)
    values=[]
    for label in labels:
        if label.confidence<.95:continue
        text=label.name.replace(' ','')
        match=re.fullmatch(r'今日剩余(?:待办)?[:：]([0-5])[/／]5',text)
        # The dispatch footer caption and ratio can be separate OCR boxes.
        # Its isolated, page-verified ROI contains no other /5 counter.
        if match is None and page=='DISPATCH':match=re.fullmatch(r'([0-5])[/／]5',text)
        if match:values.append(int(match.group(1)))
    return {'page':page,'remaining':values[0] if len(values)==1 else None,'total':5,
            'unique':len(values)==1,'roi':list(roi),
            'texts':[{'text':label.name,'confidence':float(label.confidence)} for label in labels],
            'sequence':(getattr(flow.task,'metadata',None) or {}).get('sequence')}


def lines(boxes):
    groups=[]
    for b in sorted(boxes,key=lambda b:(b.y+b.height/2,b.x)):
        cy=b.y+b.height/2
        group=next((g for g in groups if abs(g[0]-cy)<32),None)
        if group is None:groups.append([cy,[b]])
        else:group[1].append(b)
    return [(y,''.join(re.sub(r'\s+','',b.name) for b in sorted(bs,key=lambda b:b.x)),bs) for y,bs in groups]


def icon_mask(image):
    if image is None or not image.size:return None
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
    foreground=((hsv[:,:,0]>=70)&(hsv[:,:,0]<=112)&(hsv[:,:,1]<145))|(hsv[:,:,1]<40)
    values=hsv[:,:,2][foreground]
    if not values.size:return None
    threshold=max(65,np.percentile(values,99)*.62)
    return (foreground&(hsv[:,:,2]>threshold)).astype(np.uint8)*255


def silhouette(image):
    mask=icon_mask(image)
    if mask is None:return None
    count,labels,stats,_=cv2.connectedComponentsWithStats(mask)
    candidates=[s for s in stats[1:] if s[4]>=3 and .15<s[2]/s[3]<5]
    if not candidates:return None
    x=min(s[0] for s in candidates);y=min(s[1] for s in candidates)
    w=max(s[0]+s[2] for s in candidates)-x;h=max(s[1]+s[3] for s in candidates)-y
    if sum(s[4] for s in candidates)>image.shape[0]*image.shape[1]*.75:return None
    cut=mask[y:y+h,x:x+w]
    scale=22/max(w,h);width=max(1,round(w*scale));height=max(1,round(h*scale))
    result=np.zeros((24,24),np.uint8);left=(24-width)//2;top=(24-height)//2
    result[top:top+height,left:left+width]=cv2.resize(cut,(width,height),interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(result,(3,3),.8)


@lru_cache(maxsize=1)
def catalog():
    directory=ROOT/'templates/dispatch_icons'
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    return {section:{key:cv2.imdecode(np.fromfile(directory/spec['file'],np.uint8),1)
                     for key,spec in items.items()} for section,items in manifest.items()}


def symbol(image,icons,scale=1,minimum=.80,margin=.12,variants=None):
    mask=icon_mask(image)
    if mask is None or not np.any(mask):return None
    mask=cv2.GaussianBlur(mask,(3,3),.8);scores=[]
    for name,template in icons.items():
        values=[]
        appearances=[template]
        if variants and name in variants:appearances.append(variants[name])
        for appearance in appearances:
            # Native UI glyphs can be an odd number of pixels wide (e.g. 15).
            # A two-pixel search skips their actual scale entirely.
            for size in range(12,28,1 if scale==1 else 2):
                size=round(size*scale)
                if size>min(mask.shape):continue
                resized=cv2.resize(appearance,(size,size))
                values.append(float(cv2.minMaxLoc(cv2.matchTemplate(mask,resized,cv2.TM_CCOEFF_NORMED))[1]))
        if values:scores.append((max(values),name))
    scores.sort(reverse=True)
    if not scores or scores[0][0]<minimum or (len(scores)>1 and scores[0][0]-scores[1][0]<margin):return None
    return scores[0][1]


class DispatchScreen:
    def __init__(self,flow):
        self.f=flow;self.v=flow.vision;self.frame=flow.task.frame
        raw_conditions=self.ocr(CONDITIONS,.01)  # Detection proposals, not accepted evidence.
        self.condition_boxes=[b for b in raw_conditions if b.confidence>=.88]
        # A satisfied-row check can itself OCR as a character. Only remove a
        # single non-numeric proposal inside the status column when the actual
        # check template independently agrees at that proposal's position.
        self.condition_boxes=[b for b in self.condition_boxes if not (
            len(b.name.strip())==1 and not b.name.strip().isdigit() and b.x>=2660 and
            self.v.find('dispatch_green',(round(b.x-9),round(b.y-9),
                round(b.x+b.width+9),round(b.y+b.height+9))))]
        for b in raw_conditions:
            if b.confidence>=.88 or not b.name.replace(' ','').endswith('职业人数'):continue
            # Low-confidence pane text is only a location proposal. A dimmed
            # satisfied row must be independently read at .93, then its glyph
            # is checked below; never silently drop a mandatory condition.
            cy=b.y+b.height/2
            labels=self.ocr((2450,round(cy-40),2770,round(cy+40)),.93)
            valid=[v for v in labels if v.name.replace(' ','') in {p+'职业人数' for p in PROFESSIONS}]
            if len(valid)!=1:raise DispatchUnreadable('变暗职业条件文字未独立确认')
            self.condition_boxes.append(Box(b.x,b.y,b.width,b.height,valid[0].confidence,valid[0].name))
        self.condition_lines=lines(self.condition_boxes)
        self.assets=catalog();self.power_rows=[];self.checked_rows=[];self.condition_glyphs={}
        self.read_slots()
        text=[t for _,t,_ in self.condition_lines]
        if any('≥' in t or '>=' in t for t in text):
            self.requirements=parse_requirements(text,slots=self.slots,selected=self.selected)
        else:self.requirements=self.visual_requirements()

    @classmethod
    def selection(cls,flow):
        screen=cls.__new__(cls)
        screen.f=flow;screen.frame=flow.task.frame;screen.assets=catalog()
        return screen.read_slots()

    def read_slots(self):
        self.slot_proposals=self.ocr(SLOTS,.01)
        self.slot_boxes=[b for b in self.slot_proposals if b.confidence>=.85]
        counts=[]
        for b in self.slot_boxes:
            match=re.search(r'(?<!\d)([0-4])[/／]([1-4])(?!\d)',b.name.replace(' ',''))
            if match:counts.append((int(match[1]),int(match[2])))
        if counts:
            if len(set(counts))!=1:raise DispatchUnreadable('选中人数/槽位OCR矛盾')
            self.selected,self.slots=counts[0]
        else:self.selected,self.slots=self.visual_slots()
        if self.selected>self.slots:raise DispatchUnreadable('实际选择人数超过槽位')
        return self.selected,self.slots

    @classmethod
    def selection_evidence(cls,flow):
        screen=cls.__new__(cls)
        screen.f=flow;screen.frame=flow.task.frame;screen.assets=catalog()
        try:selected,slots=screen.read_slots()
        except DispatchUnreadable:return None  # Optional animation corroboration; fail closed.
        evidence=getattr(screen,'slot_evidence',None)
        return (selected,slots,evidence) if evidence is not None else None

    def patch(self,area):
        image=getattr(self.f.task,'native_frame',None)
        if not isinstance(image,np.ndarray):image=self.frame
        scale=image.shape[1]/3840
        l,t,r,b=(round(v*scale) for v in area)
        return image[max(0,t):b,max(0,l):r],image.shape[1]/1280

    def visual_slots(self):
        image,scale=self.patch(SLOTS)
        edges=cv2.Canny(cv2.cvtColor(image,cv2.COLOR_BGR2GRAY),15,45)
        contours,_=cv2.findContours(edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
        rectangles=[]
        for c in contours:
            l,t,w,h=cv2.boundingRect(c)
            if not 40*scale<w<75*scale or not 70*scale<h<110*scale:continue
            if any(abs(l-old[0])<4*scale and abs(t-old[1])<4*scale for old in rectangles):continue
            rectangles.append((l,t,w,h))
        # Portraits may erase an entire contour. A current, isolated power label
        # beneath a varied portrait supplies the missing card in the same row.
        # Anchor its dimensions to another observed slot, never a fixed count.
        if rectangles:
            width=float(np.median([v[2] for v in rectangles]));top=min(v[1] for v in rectangles)
            native_ratio=scale/3
            for label in self.slot_proposals:
                if not re.fullmatch(r'[xX×]?\d{3,6}',label.name.strip()):continue
                cy=label.y+label.height/2
                if not SLOTS[1]+210<=cy<=SLOTS[3]-15:continue
                center=(label.x+label.width/2-SLOTS[0])*native_ratio
                if any(l<=center<=l+w for l,t,w,h in rectangles):continue
                if label.confidence<.85:
                    if not re.fullmatch(r'[xX×]\d{3,6}',label.name.strip()):continue
                    # Crossed-blades art can lower the whole label's OCR score.
                    # Use it only to locate an independent, high-confidence
                    # numeric crop; the portrait and row geometry still apply.
                    area=(round(label.x+label.width*.22),round(label.y-label.height*.75),
                          round(label.x+label.width*1.15),round(label.y+label.height*1.75))
                    numbers=[b for b in self.ocr(area,.9)
                             if b.confidence>=.9 and re.fullmatch(r'\d{3,6}',b.name.strip())]
                    if len(numbers)!=1:continue
                left=round(center-width/2);height=round((label.y+label.height-SLOTS[1])*native_ratio-top)
                if not 0<=left<left+width<=image.shape[1] or not 70*scale<height<110*scale:continue
                portrait=image[top+3:top+height-round(22*scale),left+3:left+round(width)-3]
                if portrait.size and portrait.std()>=25:
                    rectangles.append((left,top,round(width),height))
        rectangles.sort()
        # Filled portraits obscure sections of both borders. Verify the common
        # row and horizontal card spacing, tolerating only calibrated occlusion.
        bottoms=[t+h for l,t,w,h in rectangles]
        if (not 1<=len(rectangles)<=4 or max(bottoms)-min(bottoms)>22*scale or
                max(v[1] for v in rectangles)-min(v[1] for v in rectangles)>24*scale or
                any(not .8*left[2]<right[0]-left[0]<1.5*left[2]
                    for left,right in zip(rectangles,rectangles[1:]))):
            raise DispatchUnreadable('槽位边框未唯一确认，禁止假定固定三人')
        selected=0;empty=self.assets['slots']['empty'];self.slot_evidence=[]
        empty=cv2.resize(empty,(round(empty.shape[1]*scale),round(empty.shape[0]*scale)))
        for l,t,w,h in rectangles:
            cut=image[t+3:t+h-3,l+3:l+w-3]
            score=cv2.minMaxLoc(cv2.matchTemplate(cut,empty,cv2.TM_CCOEFF_NORMED))[1]
            if score>=.94:
                self.slot_evidence.append((False,cut.copy()));continue
            if cut.std()<25:raise DispatchUnreadable('槽位既非已验证空槽，也无已选头像证据')
            self.slot_evidence.append((True,cut.copy()))
            selected+=1
        return selected,len(rectangles)

    def counter(self,cy):
        # The full current pane can read a checked/tinted digit even when a
        # tiny second OCR crop loses it. Reuse that same-frame evidence first.
        read=[int(text) for y,text,boxes in self.condition_lines
              if 25<y-cy<130 and re.fullmatch('[1-4]',text)]
        if len(read)==1:return read[0]
        if len(read)>1:raise DispatchUnreadable('同帧人数条件不唯一')
        area=(2505,round(cy+25),2670,round(cy+130))
        numbers=[int(b.name.strip()) for b in self.ocr(area,.88) if re.fullmatch('[1-4]',b.name.strip())]
        if len(set(numbers))==1:return numbers[0]
        if numbers:raise DispatchUnreadable('人数OCR矛盾')
        image,scale=self.patch(area);scores=[]
        from src.native_matching import match_native
        for key,template in self.assets['digits'].items():
            template=cv2.resize(template,(round(template.shape[1]*scale),round(template.shape[0]*scale)))
            # Tiny native digits can be omitted by OCR. Normalize both edges
            # on the same pixel grid, retaining the .96 score and .10 margin.
            score,_,_=match_native(image,template,.96,antialias=scale==1)
            scores.append((score,int(key)))
        scores.sort(reverse=True)
        if not scores or scores[0][0]<.96 or scores[0][0]-scores[1][0]<.1:
            raise DispatchUnreadable('人数OCR与已校准数字模板均不可靠')
        return scores[0][1]

    def visual_requirements(self):
        jobs={};tactics={};same=0;diversity=0
        bank={('profession',p):silhouette(im) for p,im in self.assets['professions'].items()}
        bank.update({('tactic',p):silhouette(im) for p,im in self.assets['tactics'].items()})
        # A stable initial reading supplies this task's own glyph appearance.
        # Checked rows can change tint; never carry these symbols to a new task.
        # A current unchecked glyph must not replace the calibrated variant:
        # selecting a member dims satisfied rows. Compare identities using
        # their best appearance, retaining the competing-identity margin.
        learned=getattr(self.f,'dispatch_condition_glyphs',{})
        for cy,text,boxes in self.condition_lines:
            ratio=re.fullmatch(r'(\d{1,6})[/／](\d{3,6})',text)
            if ratio:
                if not any('队员战力之和' in other and 20<cy-y<150 for y,other,_ in self.condition_lines):
                    raise DispatchUnreadable('战力数值缺少对应条件标签')
                self.power_rows.append((cy,int(ratio[1]),int(ratio[2])));continue
            # OCR may include the medical cross; its separate icon must still agree.
            # Actual 720P attack chevron is OCR'd as 个 immediately before 突击.
            # This exact alias still requires the independent profession glyph
            # below to agree; text alone cannot authorize team selection.
            label='突击职业人数' if text=='个突击职业人数' else text
            labelled_job=next((p for p in PROFESSIONS if label in (p+'职业人数','+'+p+'职业人数')),None)
            if labelled_job is None and text.endswith('职业人数'):
                # The pane OCR can merge a profession glyph into its text.
                # Re-read the text column without that glyph; never strip an
                # arbitrary prefix or guess a profession from the suffix.
                labels=lines(self.ocr((2450,round(cy-40),2770,round(cy+40)),.93))
                matches=[p for _,value,_ in labels for p in PROFESSIONS if value==p+'职业人数']
                if len(matches)==1:labelled_job=matches[0]
            if text=='职业种类数' or '相同战术' in text:
                count=self.counter(cy)
                if text=='职业种类数':diversity=count
                else:same=count
                self.checked_rows.append(cy+75)
            elif text in ('战术人数','职业人数') or labelled_job:
                count=self.counter(cy)
                if '相同战术' in text:kind,p='tactic',TACTIC
                else:
                    left,right=(2370,2450) if labelled_job else (2420,2505)
                    image,scale=self.patch((left,round(cy-45),right,round(cy+50)))
                    group='profession' if text=='职业人数' or labelled_job else 'tactic'
                    result=symbol(image,{key:im for key,im in bank.items() if key[0]==group},scale,margin=.10,variants=learned)
                    if result is None:raise DispatchUnreadable('条件职业/战术图标未唯一识别')
                    kind,p=result
                    if labelled_job and p!=labelled_job:
                        raise DispatchUnreadable('职业文字与实际条件图标矛盾')
                    self.condition_glyphs[result]=silhouette(image)
                destination=jobs if kind=='profession' else tactics
                if p in destination:raise DispatchUnreadable('人数条件重复')
                destination[p]=count;self.checked_rows.append(cy+75)
            elif text not in ('队员战力之和','额外要求','派遣条件') and not re.fullmatch('[1-4]',text):
                raise DispatchUnreadable('未知派遣条件：'+text)
        self.power_rows.sort()
        if len(self.power_rows) not in (1,2) or len({v[1] for v in self.power_rows})!=1:
            raise DispatchUnreadable('基础/额外战力数据不完整或实际总战力矛盾')
        bonus=None
        if len(self.power_rows)==2:
            if not any('额外要求' in t for _,t,_ in self.condition_lines):raise DispatchUnreadable('额外战力缺少额外要求标签')
            bonus=self.power_rows[1][2]
        return Requirements(tuple(sorted(jobs.items())),same,self.power_rows[0][2],bonus,self.slots,self.selected,
                            tuple(sorted(tactics.items())),diversity)

    def mission_identity(self):
        # Title and commission source stay fixed while cards and filters change.
        identity=tuple(text for _,text,_ in lines(self.ocr((585,385,2300,595),.85)))
        if not identity:raise DispatchUnreadable('当前委托标题未读取，禁止沿用上一任务')
        return identity

    def ocr(self,roi,threshold=.9):
        l,t,r,b=roi
        return self.f.task.ocr(x=l/3840,y=t/2160,to_x=r/3840,to_y=b/2160,
                               frame=self.frame,threshold=threshold)

    @staticmethod
    def filter_box(flow):
        l,t,r,b=FILTER
        result=flow.task.ocr(x=l/3840,y=t/2160,to_x=r/3840,to_y=b/2160,
                             frame=flow.task.frame,threshold=.93)
        known={'全战术',*catalog()['tactics']}
        matches=[]
        for v in result:
            text=tactic_label(v.name)
            # Actual 720P header OCR drops the isolated horizontal 一 while
            # reading 致战线 at .9989. This exact suffix names only one faction.
            if text in known:matches.append(Box(v.x,v.y,v.width,v.height,v.confidence,text))
        if len(matches)!=1:raise DispatchUnreadable('当前战术筛选标签不唯一或无法识别')
        return matches[0]

    def profession_icons(self):
        # Learn symbols from their explicitly labelled requirement rows, not a
        # character's name. Catalog assets may supply other observed professions.
        found={p:silhouette(im) for p,im in self.assets['professions'].items()}
        for y,text,boxes in self.condition_lines:
            profession=next((p for p,_ in self.requirements.professions if text.startswith(p)),None)
            if profession:
                x=min(b.x for b in boxes)
                l,t,r,b=round(x-100),round(y-48),round(x-14),round(y+48)
                icon=silhouette(self.frame[max(0,t):b,max(0,l):r])
                if icon is not None:found[profession]=icon
        found.update({p:im for (kind,p),im in self.condition_glyphs.items() if kind=='profession'})
        return found

    def profession(self,area,icons):
        l,t,r,b=area
        image,scale=self.patch((l,t,l+81,t+99))
        if scale==1:
            # The profession lives in the card's top-left corner. Portrait
            # hair/shoulder highlights below it are not part of this glyph.
            image=image[:27].copy()
            hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
            cyan=(hsv[:,:,0]>=70)&(hsv[:,:,0]<=112)&(hsv[:,:,1]<145)
            if not np.any(cyan):return None
            bright=max(65,float(np.percentile(hsv[:,:,2][cyan],99))*.8)
            image[~(cyan&(hsv[:,:,2]>=bright))]=0
            bank={p:silhouette(im) for p,im in self.assets['professions'].items()}
            return symbol(image,bank,scale,variants=icons)
        return symbol(image,icons,scale)

    def tactics(self,area):
        l,t,r,b=area;result=[]
        bank={p:silhouette(im) for p,im in self.assets['tactics'].items()}
        for offset in (12,90,171):
            image,scale=self.patch((l+offset,b-148,min(r,l+offset+75),b-72))
            found=symbol(image,bank,scale,minimum=.76,margin=.10)
            if found:result.append(found)
        return tuple(sorted(set(result)))

    def roster_cards(self):
        """Current full card borders, without reading their power or classes."""
        from src.dispatch import CARDS,rect
        image,scale=self.patch(ROSTER)
        if not image.size:return []
        edges=cv2.Canny(cv2.cvtColor(image,cv2.COLOR_BGR2GRAY),15,45)
        # Native 720P animation leaves tiny gaps in the vertical card borders.
        # Close observed edges only; retain the existing native 4K detector.
        if scale==1:
            edges=cv2.morphologyEx(edges,cv2.MORPH_CLOSE,np.ones((3,1),np.uint8))
        contours,_=cv2.findContours(edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
        boxes=[cv2.boundingRect(contour) for contour in contours]
        boxes=[box for box in boxes if 70*scale<=box[2]<=100*scale and
               100*scale<=box[3]<=145*scale]
        ratio=scale/3
        columns=[rect(card) for card in CARDS]
        found=[]
        for l,t,w,h in sorted(boxes,key=lambda box:box[2]*box[3],reverse=True):
            center=ROSTER[0]+(l+w/2)/ratio
            col=min(range(3),key=lambda i:abs(center-(columns[i][0]+columns[i][2])/2))
            left,_,right,_=columns[col]
            if abs(center-(left+right)/2)>80:continue
            top=round(ROSTER[1]+t/ratio);bottom=round(ROSTER[1]+(t+h)/ratio)
            if not ROSTER[1]<=top<bottom<=ROSTER[3]:continue
            if any(left==old[0] and (abs(top-old[1])<24 or scale==1 and old[1]<=top and bottom<=old[3])
                   for old in found):continue
            found.append((left,top,right,bottom))
        rows=[]
        for area in sorted(found,key=lambda box:(box[1],box[0])):
            row=next((row for row in rows if abs(area[1]-row[0][1])<30),None)
            if row is None:rows.append([area])
            else:row.append(area)
        return [area for row in rows for area in sorted(row,key=lambda box:box[0])]

    def selectable(self,area):
        # Ordinary missions need a current, varied portrait/card and no
        # explicit disabled label. Unknown single-card power is not rejection;
        # the actual selected slots/condition power must still pass later.
        labels=self.ocr(area,.85)
        if any(any(word in box.name for word in ('已派遣','派遣中','已入队','不可','锁定')) for box in labels):return False
        l,t,r,b=area
        portrait,_=self.patch((l+35,t+25,r-25,min(b,t+220)))
        return bool(portrait.size and portrait.std()>=25)

    def available(self,area,power):
        # A readable bright power label plus the current portrait is positive
        # card evidence. Disabled labels override it; unknown cards are omitted.
        l,t,r,b=area
        labels=self.ocr(area,.85)
        if any(any(word in v.name for word in ('已派遣','派遣中','已入队','不可','锁定')) for v in labels):return False
        numbers=[v for v in self.ocr((l+100,b-90,r,b+18),.9) if v.name.strip()==str(power)]
        if len(numbers)!=1:return False
        number=numbers[0]
        cut,_=self.patch((max(l,number.x),max(t,number.y),min(r,number.x+number.width),min(b+18,number.y+number.height)))
        return bool(cut.size and np.percentile(cv2.cvtColor(cut,cv2.COLOR_BGR2GRAY),95)>=130)

    def feedback(self,bonus_required):
        if self.power_rows:
            total=self.power_rows[0][1];r=self.requirements
            base_rows=self.checked_rows+[self.power_rows[0][0]]
            base=total>=r.base_power and all(self.v.find('dispatch_green',(2660,round(y-65),2800,round(y+65))) for y in base_rows)
            bonus=r.bonus_power is None or bool(total>=r.bonus_power and self.v.find('dispatch_orange',
                (2660,round(self.power_rows[1][0]+10),2800,round(self.power_rows[1][0]+140))))
            return {'base':bool(base),'bonus':bonus,'power':total,'selected':self.selected,
                'allowed':bool(base and (not bonus_required or bonus) and self.selected>0 and self.v.find('dispatch_ready'))}
        base_checks=[];bonus_check=self.requirements.bonus_power is None
        for y,text,_ in self.condition_lines:
            if '≥' not in text and '>=' not in text:continue
            roi=(2660,max(400,round(y-65)),2800,min(1770,round(y+65)))
            is_bonus='额外' in text or (self.requirements.bonus_power is not None and str(self.requirements.bonus_power) in text)
            if is_bonus:bonus_check=bool(self.v.find('dispatch_orange',roi))
            else:base_checks.append(bool(self.v.find('dispatch_green',roi)))
        totals=[]
        for label in self.slot_boxes:
            text=label.name.replace(' ','')
            match=re.search(r'(\d{1,6})[/／]'+str(self.requirements.base_power)+r'(?!\d)',text)
            explicit=re.search(r'(?:当前|总|队伍)战力[:：]?(\d{1,6})(?!\d)',text)
            if match:totals.append(int(match[1]))
            elif explicit:totals.append(int(explicit[1]))
        total=totals[0] if len(set(totals))==1 else None
        minimum=(1+len(self.requirements.professions)+bool(self.requirements.same_tactic)
                 +bool(self.requirements.profession_types)+len(self.requirements.tactic_counts))
        base=total is not None and total>=self.requirements.base_power and len(base_checks)>=minimum and all(base_checks)
        extra=not bonus_required or (bonus_check and total is not None and
                                      (self.requirements.bonus_power is None or total>=self.requirements.bonus_power))
        return {'base':base,'bonus':bool(bonus_check),'power':total,'selected':self.selected,
                'allowed':bool(base and extra and self.selected>0 and self.v.find('dispatch_ready'))}
