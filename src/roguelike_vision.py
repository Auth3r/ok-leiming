"""Roguelike-only evidence. Coordinates come from current images, never crafting."""
from src.app_paths import APP_ROOT
from dataclasses import replace
import json
import re
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from src.roguelike_policy import Candidate, Observation, RogueAction, RogueState as S
from src.native_matching import match_native


ROOT = APP_ROOT


def compact(text):
    return re.sub(r'\s+', '', text).replace('兌', '兑')


def blank_dismiss_hint(text):
    text=compact(text).translate(str.maketrans({'點':'点','擊':'击','處':'处','關':'关','閉':'闭','繼':'继','續':'续'}))
    # Only whole instruction semantics, never reward text, quantities or names.
    text=text.replace('空自处','空白处').replace('点出','点击')
    # The recording's fading prompt merges 点击 into 陆 (13.0s).
    text=text.replace('陆空白处','点击空白处')
    return '点击' in text and '空白处' in text and ('关闭' in text or '继续' in text)


def shop_role(text):
    text=compact(text)
    for label,role in (('火力','firepower'),('隐刃','hidden_blade'),('突击','assault'),
                       ('医疗','medical'),('重盾','heavy_shield')):
        if label in text:return role
    return 'unknown'


class RogueTemplates:
    def __init__(self, root=None):
        root = Path(root) if root is not None else ROOT / 'templates/roguelike'
        self.specs = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))['templates']
        self.images = {key: cv2.imdecode(np.fromfile(root / row['file'], np.uint8), 1)
                       for key, row in self.specs.items()}
        if any(im is None for im in self.images.values()):
            raise ValueError('肉鸽模板缺失或损坏')
        self.gray = {key:cv2.cvtColor(im,cv2.COLOR_BGR2GRAY) for key,im in self.images.items()}


class RoguelikeScreen:
    def __init__(self, task, frame, templates, special_shop=False):
        self.task, self.frame, self.templates = task, frame, templates
        self.h, self.w = frame.shape[:2]
        # Uniform scale preserves source pixels' aspect ratio (2087 evidence is
        # not stretched into 2160). Workflow rejects non-4K live captures.
        self.boxes = []
        self.special_shop = special_shop
        self.matches = {}
        self.cached = {}
        self.observation = self.classify()

    def team_ocr(self):
        # Full member OCR stays inside member decisions; the generic hint uses a separate small ROI.
        if not self.boxes:
            small=cv2.resize(self.frame,None,fx=.5,fy=.5,interpolation=cv2.INTER_AREA)
            self.boxes=[SimpleNamespace(x=b.x*2,y=b.y*2,width=b.width*2,height=b.height*2,
                                      name=compact(b.name)) for b in self.task.ocr(frame=small,threshold=.7)]

    def shop_ocr(self):
        # Inventory only. The balance and refresh cost each have a separate
        # narrow native ROI; the orange currency can never enter these queries.
        x,y,x2,y2=self.roi((.14, .245, .975, .585))
        small=cv2.resize(self.frame[y:y2,x:x2],None,fx=.5,fy=.5,interpolation=cv2.INTER_AREA)
        self.boxes=[SimpleNamespace(x=b.x*2+x,y=b.y*2+y,width=b.width*2,height=b.height*2,
                                   name=compact(b.name)) for b in self.task.ocr(frame=small,threshold=.7)]

    def shop_observation(self):
        fixed=self.fixed
        if self.special_shop:return Observation(S.SHOP,actions=(fixed('back',200,120),))
        currency=self.number((.795,.025,.835,.083),local=True)
        refresh_cost=self.number((.775,.15,.98,.235),local=True)
        if refresh_cost is None and self.find('v_refresh_free'):refresh_cost=0
        self.shop_ocr()
        scale=self.w/3840
        cards=[];ready=[];actions=[fixed('back',round(200*scale),round(120*scale)),
                          fixed('refresh',round(3520*scale),round(415*scale))]
        for i in range(3):
            area=self.roi((.141+i*.280,534*scale/self.h,.411+i*.280,1049*scale/self.h))
            c=self.candidate(i,area,shop=True)
            a=None if not c.selectable else self.button('exchange_'+str(i),'兑换',(.33+i*.28,.47,.41+i*.28,.57))
            price=self.number((.148+i*.28,1020*scale/self.h,.235+i*.28,1185*scale/self.h),local=True) if a else None
            if a:
                ax,ay,ax2,ay2=map(round,a.area);label=self.frame[ay:ay2,ax:ax2]
                a=replace(a,enabled=bool(label.size and np.mean(np.min(label,axis=2)>180)>.035))
                actions.append(a)
            cards.append(replace(c,price=price,selectable=bool(c.selectable and a and a.enabled)))
            x,y,x2,y2=area
            sold='已售罄' in self.text((x/self.w,y2/self.h,x2/self.w,min(self.h,y2+(y2-y)*.32)/self.h))
            ready.append(bool(c.fingerprint and (a or sold)))
        # Button UI and affordability are separate facts; policy owns spending.
        x,y,x2,y2=self.roi((.90,.17,.97,.215))
        refresh_enabled=bool(np.mean(np.min(self.frame[y:y2,x:x2],axis=2)>160)>.025)
        if not all(ready):
            return Observation(S.TRANSIENT,detail='shop controls/cards still entering')
        actions[1]=replace(actions[1],enabled=refresh_enabled)
        return Observation(S.SHOP,actions=tuple(actions),candidates=tuple(cards),
                           currency=currency,refresh_cost=refresh_cost)

    def roi(self, area):
        x, y, x2, y2 = area
        return (round(x*self.w), round(y*self.h), round(x2*self.w), round(y2*self.h))

    def text(self, area):
        x, y, x2, y2 = self.roi(area)
        return ''.join(b.name for b in sorted(self.boxes, key=lambda b: (round(b.y/25), b.x))
                       if x <= b.x+b.width/2 <= x2 and y <= b.y+b.height/2 <= y2)

    def find(self, key, area=None):
        token = (key, area)
        if token in self.cached:
            return self.cached[token]
        spec = self.templates.specs[key]
        native=spec.get('native_client_size')
        if native is not None and native != [self.w,self.h]:
            raise ValueError('Native roguelike template client size mismatch')
        scale=1 if native is not None else self.w/3840
        if native is not None and area is None:
            from src.recognition_geometry import reference_bbox_to_client
            x,y,x2,y2=reference_bbox_to_client(spec['reference_roi'],(self.w,self.h),
                                              reference_size=spec['reference_size'])
        elif area is None:
            # Stable 4K UI: a small search margin covers the 73px difference in
            # legacy Word captures without scanning most of the screen.
            a,b,c,d=spec['crop']
            x,y,x2,y2=(max(0,round((a-40)*scale)),max(0,round((b-85)*scale)),
                       min(self.w,round((c+40)*scale)),min(self.h,round((d+85)*scale)))
        else:x,y,x2,y2=self.roi(area)
        pixel_difference=spec.get('score_mode')=='pixel_difference'
        patch=self.frame[y:y2,x:x2] if pixel_difference else cv2.cvtColor(self.frame[y:y2,x:x2],cv2.COLOR_BGR2GRAY)
        template=self.templates.images[key] if pixel_difference else self.templates.gray[key]
        # Preserve original attachment pixels. Live capture still requires 4K;
        # only the small template/search coordinates scale for offline evidence.
        if scale!=1:template=cv2.resize(template,None,fx=scale,fy=scale,interpolation=cv2.INTER_AREA)
        box, score, raw_score = None, 0.0, 0.0
        if patch.shape[0] >= template.shape[0] and patch.shape[1] >= template.shape[1]:
            score, loc, raw_score = match_native(patch,template,spec.get('threshold',.9),
                difference=pixel_difference,
                antialias=native is not None and spec.get('pixel_filter')=='antialias')
            if score >= spec.get('threshold', .9):
                a, b = loc
                box = (a+x, b+y, a+x+template.shape[1], b+y+template.shape[0])
        self.matches[key+str(area)] = {'name': 'roguelike_'+key, 'confidence': score,
                                      'raw_confidence': raw_score, 'pixel_filter': spec.get('pixel_filter'),
                                      'roi': (x, y, x2, y2), 'matched': box is not None,
                                      'box': [box[0], box[1], box[2]-box[0], box[3]-box[1]] if box else None}
        self.cached[token] = box
        return box

    def has(self, *keys):
        return all(self.find(key) for key in keys)

    def button(self, key, label, area=(.65, .82, .99, .98), irreversible=True):
        x, y, x2, y2 = self.roi(area)
        boxes = [b for b in self.boxes if label in b.name and
                 x <= b.x+b.width/2 <= x2 and y <= b.y+b.height/2 <= y2]
        if not boxes:
            # Native action ROI recovers labels missed by full-frame downsampling.
            boxes=[SimpleNamespace(x=b.x+x,y=b.y+y,width=b.width,height=b.height,name=compact(b.name))
                   for b in self.task.ocr(frame=self.frame[y:y2,x:x2],threshold=.7) if label in compact(b.name)]
        if not boxes and key.startswith('exchange_'):
            found=self.find('v_exchange_label',area)
            if found:return RogueAction(key,found,'兑换',irreversible=irreversible)
        if not boxes and key=='back' and self.find('choice_heading'):
            found=self.find('v_choice_back',area)
            if found:return RogueAction(key,found,'返回',irreversible=False)
        if len(boxes) != 1:return None
        b = boxes[0]
        return RogueAction(key, (round(b.x-6), round(b.y-6), round(b.x+b.width+6),
                                 round(b.y+b.height+6)), b.name, irreversible=irreversible)

    def number(self,area,local=False):
        if local:
            x,y,x2,y2=self.roi(area)
            patch=self.frame[y:y2,x:x2]
            # Clipboard attachments are smaller than live frames. Give digit
            # OCR the same text scale; never resize or overwrite source files.
            if self.w<3840:patch=cv2.resize(patch,None,fx=3840/self.w,fy=3840/self.w)
            text=''.join(compact(b.name) for b in self.task.ocr(frame=patch,threshold=.85))
            digits=re.findall(r'\d+',text)
            return int(digits[0]) if len(digits)==1 else None
        matches=re.findall(r'\d+',self.text(area))
        return int(matches[0]) if len(matches)==1 else None

    def enabled_confirmation(self):
        x,y,x2,y2=self.roi((.68,1878/self.h,.84,1976/self.h))
        return bool(np.mean(np.min(self.frame[y:y2,x:x2],axis=2)>160)>.65)

    def fixed(self,key,x,y,text='',enabled=True):
        return RogueAction(key,(x-20,y-20,x+20,y+20),text,enabled)

    def stable_action_visual(self):
        # Coarse whole-page pixels detect fades/animation; this is not OCR.
        return cv2.resize(self.frame, (32, 18), interpolation=cv2.INTER_AREA).astype('float32')

    def overlay_content(self):
        # Read foreground content only when dismissing an overlay. Background
        # nodes and the shared blank-click hint cannot identify a new reward.
        if 'overlay_content' not in self.cached:
            x,y,x2,y2=self.roi((.32,.12,.68,.74))
            words=self.task.ocr(frame=self.frame[y:y2,x:x2],threshold=.8)
            text=compact(''.join(b.name for b in sorted(words,key=lambda b:(round(b.y/25),b.x))))
            self.cached['overlay_content']=text
        return self.cached['overlay_content']

    def overlay_identity(self):
        text=self.overlay_content()
        return text if len(text)>=7 else ''

    def bright_action(self,key,label,area,min_width=.15,min_aspect=4):
        x,y,x2,y2=self.roi(area)
        patch=self.frame[y:y2,x:x2]
        mask=(np.min(patch,axis=2)>185).astype('uint8')*255
        candidates=[]
        for contour in cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)[0]:
            a,b,w,h=cv2.boundingRect(contour)
            if (w>self.w*min_width and min_aspect<w/max(h,1)<12 and
                    cv2.contourArea(contour)>self.w*self.h*.006):
                candidates.append((a+x,b+y,a+x+w,b+y+h))
        if len(candidates)==1:
            return RogueAction(key,candidates[0],label,irreversible=False)
        return None

    def entry_button(self):
        # The navigation page has an upper "进入副本" button; the challenge
        # variant has a lower "开始挑战" button. Both use current pixels.
        challenge=(.66,.80,.99,.98)
        dungeon=(.77,.70,.98,.80)
        return (self.bright_action('enter','开始挑战',challenge,.18) or
                self.bright_action('enter','进入副本',dungeon,.15) or
                self.button('enter','开始挑战',challenge,irreversible=False) or
                self.button('enter','进入副本',dungeon,irreversible=False))

    def battle_start_button(self):
        button=self.bright_action('battle_start','开始战斗',(.79,.85,.99,.96))
        if button:
            x,y,x2,y2=button.area
            # A fading/covered button is unavailable even if its outer border
            # survives. This evidence belongs to the page, not retry policy.
            if np.mean(np.min(self.frame[y:y2,x:x2],axis=2)>185)>.8:return button
        return None

    def battle_start_available(self):
        return self.observation.action('battle_start') is not None

    def battle_page(self):
        # Page identity is independent of whether a special card is visible.
        if self.w==3840:return bool(self.find('v_battle_view') or self.find('v_battle_auto'))
        # Offline user attachments can be downscaled; live Workflow stays 4K.
        scale=self.w/3840
        for key in ('v_battle_view','v_battle_auto'):
            a,b,c,d=self.templates.specs[key]['crop']
            x,y,x2,y2=[round(v*scale) for v in (max(0,a-40),max(0,b-85),c+40,d+85)]
            patch=cv2.cvtColor(self.frame[y:y2,x:x2],cv2.COLOR_BGR2GRAY)
            template=cv2.resize(self.templates.gray[key],None,fx=scale,fy=scale)
            if patch.shape[0]>=template.shape[0] and patch.shape[1]>=template.shape[1]:
                if cv2.minMaxLoc(cv2.matchTemplate(patch,template,cv2.TM_CCOEFF_NORMED))[1]>=.9:return True
        return False

    def find_special_deploy_card_fast(self):
        # Hand geometry only, never a fixed target index. The special device is
        # last after ordinary members (5/6/7/8 positions in the real captures).
        scale=self.w/3840
        def patch(x,y,x2,y2):
            return self.frame[round(y*scale):round(y2*scale),round(x*scale):round(x2*scale)]
        for left in reversed(range(480,2881,300)):
            body=patch(left+30,1770,left+240,1930)
            if not body.size:continue
            hue,sat,val=cv2.split(cv2.cvtColor(body,cv2.COLOR_BGR2HSV))
            blue=float(np.mean((hue>=95)&(hue<=120)&(sat>50)&(val>100)))
            # A large translucent blue body is unlike the ordinary portrait.
            # In low light use the isolated corner glyph geometry as a fallback;
            # no digit recognition, card name, portrait or exact template.
            corner=patch(left+200,1940,left+270,2005)
            white=(np.min(corner,axis=2)>170).astype('uint8')
            _,_,shapes,_=cv2.connectedComponentsWithStats(white)
            compact_corner=any(12*scale<=w<=40*scale and 35*scale<=h<=58*scale
                               and area/(w*h)>.18 for _,_,w,h,area in shapes[1:])
            texture=float(np.mean(cv2.Canny(body,100,180)>0))*scale
            if texture<.13 and (blue>.55 or (blue>.12 and compact_corner)):
                area=tuple(round(v*scale) for v in (left+25,1760,left+240,1950))
                return RogueAction('special_card',area,'rightmost_occupied_special_slot',irreversible=False)
        return None

    def choose_empty_ground(self,exclude=()):
        # Eight interior battlefield patches. No grid, direction or map template.
        pool=((.72,.42),(.70,.59),(.62,.64),(.53,.64),(.42,.64),(.30,.56),(.39,.34),(.61,.30))
        candidates=[];radius=round(80*self.w/3840)
        for nx,ny in pool:
            point=(round(nx*self.w),round(ny*self.h))
            if point in exclude:continue
            x,y=point;patch=self.frame[y-radius:y+radius,x-radius:x+radius]
            hue,sat,val=cv2.split(cv2.cvtColor(patch,cv2.COLOR_BGR2HSV))
            danger=((hue<12)|(hue>165)|((hue>35)&(hue<85)))&(sat>125)&(val>110)
            edges=float(np.mean(cv2.Canny(patch,100,180)>0))
            colour=float(np.mean(danger))
            if colour>.025 or edges>.16 or np.median(val)<45:continue
            candidates.append((edges+colour*3,point))
        if not candidates:return None
        score,point=min(candidates,key=lambda c:c[0])
        return point,score

    def special_hand_signature(self):
        x,y,x2,y2=self.roi((.12,.78,.80,.93))
        patch=cv2.resize(self.frame[y:y2,x:x2],(96,16))
        return tuple(int(v) for v in patch.ravel())

    def choices(self, members=False, ordinary=False):
        # Three ordinary choices or four special reward choices, as evidenced.
        if members:
            scale=self.w/3840
            areas = [(.033+i*.315, 820*scale/self.h, .336+i*.315, 1340*scale/self.h) for i in range(3)]
        elif ordinary:
            areas = [(.033+i*.315, 820/self.h, .336+i*.315, 1340/self.h) for i in range(3)]
        else:
            areas = [(.184, 522/self.h, .497, 1064/self.h), (.503, 522/self.h, .816, 1064/self.h),
                     (.184, 1075/self.h, .497, 1628/self.h), (.503, 1075/self.h, .816, 1628/self.h)]
        return tuple(self.candidate(i, self.roi(a), members) for i,a in enumerate(areas))

    def candidate(self, index, area, member=True, shop=False):
        x,y,x2,y2 = area
        crop = self.frame[y:y2, x:x2]
        selected = bool(crop.size and np.max(np.mean(np.min(crop[:16],axis=2)>150,axis=1))>.6)
        fingerprint = self.text((x/self.w,y/self.h,x2/self.w,(y+(y2-y)*.22)/self.h))
        quality, glow = '', False
        if member and crop.size:
            h,w = crop.shape[:2]
            # Quality is the portrait's lower colour band, not clothing or aura.
            band = crop[round(h*.79):round(h*.93), round(w*.04):round(w*.28)]
            hsv = cv2.cvtColor(band,cv2.COLOR_BGR2HSV)
            hue,sat,val = cv2.split(hsv)
            ratios = {name:float(np.mean((hue>=lo)&(hue<=hi)&(sat>75)&(val>(30 if shop else 65))))
                      for name,lo,hi in (('gold',12,38),('purple',120,165),('blue',90,119))}
            quality = max(ratios,key=ratios.get) if max(ratios.values())>.18 else ''
            # A broad luminous halo in the tactical-icon strip; no portrait/icon template.
            strip = crop[round(h*.70):round(h*.94), round(w*.48):round(w*.90)]
            hsv = cv2.cvtColor(strip,cv2.COLOR_BGR2HSV)
            hue,sat,val = cv2.split(hsv)
            glow = bool(np.mean((hue>=12)&(hue<=38)&(sat>80)&(val>110))>.025)
        occupied=bool(quality) or bool(self.text((x/self.w,y/self.h,x2/self.w,y2/self.h)))
        identity='vera' if any(re.sub(r'[^\u4e00-\u9fff]','',b.name)=='薇拉' and x<=b.x+b.width/2<=x2 and
                              y<=b.y+b.height/2<=y+(y2-y)*.3 for b in self.boxes) else ''
        role='unknown'
        if shop:
            h=y2-y
            role=shop_role(self.text((x/self.w,(y+h*.50)/self.h,x2/self.w,(y+h*.80)/self.h)))
            sold='已售罄' in self.text((x/self.w,y2/self.h,x2/self.w,min(self.h,y2+h*.32)/self.h))
            occupied=occupied and not sold
        return Candidate(index,area,quality,glow,occupied,selected,fingerprint,identity=identity,role=role)

    def map_nodes(self):
        x,y,x2,y2=self.roi((.20,.20,.98,.80))
        patch=cv2.resize(self.frame[y:y2,x:x2],None,fx=.25,fy=.25)
        hue,sat,val=cv2.split(cv2.cvtColor(patch,cv2.COLOR_BGR2HSV))
        cyan=((hue>=78)&(hue<=100)&(sat>100)&(val>180)).astype('float32')
        mass=cv2.boxFilter(cyan,-1,(21,21),normalize=True)
        nodes=[]
        for _ in range(3):
            _,strength,_,loc=cv2.minMaxLoc(mass)
            if strength<=.28:break
            a,b=loc;cx,cy=x+a*4,y+b*4
            # The glowing diamond identifies the available route, but is not
            # the event's hit target. Locate its actual icon above it afresh.
            scale=self.w/3840
            left=max(0,round(cx-115*scale));top=max(0,round(cy-350*scale))
            right=min(self.w,round(cx+115*scale));bottom=max(top,round(cy-110*scale))
            icon=self.frame[top:bottom,left:right]
            if icon.size:
                lo=icon.min(axis=2);hi=icon.max(axis=2).astype('int16')
                mask=((lo>100)&(hi-lo<100)).astype('uint8')*255
                # Weapon/person glyphs interrupt the surrounding ring.
                kernel=max(1,round(7*scale))
                mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((kernel,kernel),np.uint8))
                contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
                boxes=[cv2.boundingRect(c) for c in contours]
                boxes=[r for r in boxes if 75*scale<r[2]<210*scale and
                       75*scale<r[3]<210*scale and .65<r[2]/r[3]<1.5]
                if boxes:
                    ix,iy,iw,ih=max(boxes,key=lambda r:r[2]*r[3])
                    nodes.append((left+ix,top+iy,left+ix+iw,top+iy+ih))
            mass[max(0,b-40):b+40,max(0,a-50):a+50]=0
        nodes.sort(key=lambda a:(round(a[0]/150),a[1]))
        return tuple(RogueAction('node_'+str(i),area,irreversible=False) for i,area in enumerate(nodes))

    def member_choice_source(self):
        if self.find('choice_order'):return 'order'
        if self.find('choice_outpost'):return 'outpost'
        return None
    def classify(self):
        # Foreground instructions own the page, even when an event anchor is
        # still visible through the translucent layer. Only this small ROI is OCR'd.
        if self.detect_blank_dismiss_overlay():
            x,y=round(self.w/2),round(1940*self.w/3840)
            return Observation(S.OVERLAY,actions=(self.fixed('dismiss',x,y),))
        if self.w!=3840 and self.battle_page():return Observation(S.BATTLE,event='battle')
        def obs(state,event='',actions=(),candidates=(),evidence=(),**kw):
            return Observation(state,event,tuple(a for a in actions if a),candidates,evidence=evidence,**kw)
        fixed=self.fixed
        if self.find('reward_heading') or self.find('intro_heading') or self.find('v_intro_2') or self.find('reward_currency'):
            return obs(S.TRANSIENT)
        if self.find('v_settlement_question'):
            return obs(S.SETTLEMENT_CONFIRM,actions=(fixed('confirm',3280,1340),))
        if self.find('v_battle_result_title'):
            if not self.result_animation_finished():return obs(S.TRANSIENT)
            return obs(S.BATTLE_RESULT,actions=(fixed('confirm',3540,1940),))
        if self.find('result_title'):
            if not self.result_animation_finished():return obs(S.TRANSIENT)
            if self.find('result_map'):return obs(S.RESULT,actions=(fixed('confirm',3540,1940),))
            if self.find('result_final'):return obs(S.RESULT_TEAM,actions=(fixed('next',3540,1940),))
            if self.find('result_battles'):return obs(S.RESULT_STATS,actions=(fixed('next',3540,1940),))
        if self.find('choice_heading'):
            member_source=self.member_choice_source()
            members=bool(member_source)
            ordinary=bool(self.find('choice_event'))
            if members:
                cards=self.choices(True,ordinary)
                # Names cannot affect gold/glowing/purple priority. OCR is only
                # needed when Vera is the remaining identity-based fallback.
                if not any(c.selectable and (c.quality in ('gold','purple') or c.glowing) for c in cards):
                    self.team_ocr()
                    cards=self.choices(True,ordinary)
                # Actual supplied buttons are at y~.90; the suggested .64-.86
                # window excludes them. Locate the label in the lower-right
                # region, independent of portrait quality or a previous frame.
                confirm=self.button('confirm','确认',(.64,.64,.99,.96)) if any(c.selected for c in cards) else None
                return obs(S.OUTPOST,event=member_source,
                    actions=(confirm,fixed('back',220,120)),candidates=cards)
            # Policy still picks the first option. Observe every selection border
            # so a different selected option also revokes the old click's retry.
            cards=tuple(replace(c,selectable=True) for c in self.choices(ordinary=ordinary))
            confirm=self.bright_action('confirm','确认',(.64,.82,.99,.98))
            return obs(S.CHOICE,event='choice' if ordinary else 'reward_choice',
                actions=(confirm,),candidates=cards)
        if self.find('search_title'):
            return obs(S.SEARCH,actions=(self.button('risk','勇于冒险',(.66,.45,.99,.65)),
                self.button('trap','小心陷阱',(.66,.64,.99,.79),irreversible=False)))
        if self.find('bounty_title'):
            return obs(S.BOUNTY,actions=(self.button('treasure','死域寻宝',(.66,.34,.99,.51)),
                self.button('training','实训计划',(.66,.50,.99,.65)),
                self.button('leave','小心为上',(.66,.64,.99,.79))))
        if self.find('shop_sell'):return self.shop_observation()
        if self.battle_page() or (self.find('battle_view') and self.find('deploy_prompt')):
            return obs(S.BATTLE,event='battle')
        if self.find('battle_prep') or self.find('v_battle_prep'):
            return obs(S.BATTLE_PREP,actions=(self.battle_start_button(),))
        for title,state,event in (('v_event_accident',S.ACCIDENT,'accident'),('v_event_search',S.SEARCH,'search'),
                ('v_event_special',S.SPECIAL_OPERATION,'special'),('event_encounter',S.ENCOUNTER,'encounter'),
                ('event_outpost',S.EVENT,'outpost'),('event_choice',S.EVENT,'choice'),('event_order',S.EVENT,'order')):
            if self.find(title):
                if event=='order' and self.find('fire_order'):
                    return obs(S.ORDER,actions=(fixed('fire_order',3000,1540,'火种调令'),fixed('leave',3000,1840,'离开')))
                if state==S.ACCIDENT:
                    return obs(state,event,actions=(self.button('action_0','点击继续',irreversible=False),))
                return obs(state,event,actions=(fixed('action_0',3540,1940,'进攻' if state in (S.ENCOUNTER,S.SPECIAL_OPERATION) else '继续'),))
        if self.find('map_floor'):
            return obs(S.MAP,event='bounty_complete' if self.find('v_bounty_complete') else '',actions=self.map_nodes()+(fixed('back',220,120),),floor=2 if self.find('v_fast_floor2') else 1)
        if self.find('start_title'):
            ongoing=bool(self.find('v_fast_terminate'))
            return obs(S.START,event='ongoing' if ongoing else 'idle',actions=(
                self.bright_action('continue_run' if ongoing else 'start',
                                   '继续挑战' if ongoing else '开始挑战',(.66,.80,.99,.98),.12,3),
                fixed('terminate',2900,1840) if ongoing else None))
        if self.find('entry_title') or self.find('entry_current'):
            return obs(S.ENTRY,actions=(self.entry_button(),))
        return obs(S.UNKNOWN,detail='page anchor not detected')

    def detect_blank_dismiss_overlay(self):
        # Existing hint crops remain a cheap path, not an exhaustive catalogue
        # of reward screens. No new screenshot-specific template is required.
        if self.w==3840 and any(self.find(key) for key in (
                'blank_continue','v_fast_blank_continue','v_fast_reward_continue',
                'blank_close','v_blank_close','v_fast_blank_close')):
            return True
        scale=self.w/3840
        x,y,x2,y2=[round(v*scale) for v in (1640,1875,2200,1990)]
        patch=self.frame[y:y2,x:x2]
        if not patch.size:return False
        words=self.task.ocr(frame=patch,threshold=.6)
        hint=''.join(b.name for b in sorted(words,key=lambda b:b.x))
        self.matches['blank_dismiss_hint']={'name':'blank_dismiss_hint','roi':(x,y,x2,y2),'text':hint,
                                          'matched':blank_dismiss_hint(hint),'box':None,'confidence':0.}
        return blank_dismiss_hint(hint)

    def result_animation_finished(self):
        # Result headings arrive before their white continue strip animates in
        # (122s and 425s in the recording). Those frames are still LONG_WAIT.
        patch=self.frame[1920:1960,3320:3600]
        return bool(patch.size and np.mean(np.min(patch,axis=2)>180)>.35)
