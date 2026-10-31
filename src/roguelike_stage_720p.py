"""Native evidence adapter for the existing RogueScreen/Runner, not a policy.

Only explicitly recorded native pages are admitted. Missing assets/actions never
fall through to legacy templates or raw 4K fixed points.
"""
from src.app_paths import APP_ROOT
from dataclasses import replace
import json
from pathlib import Path
import cv2,numpy as np
from src.recognition_geometry import reference_bbox_to_client
from src.roguelike_vision import RogueTemplates,RoguelikeScreen,compact
from src.roguelike_policy import Observation,RogueAction,RogueState as S
from src.native_matching import match_native, panel_evidence

ROOT=APP_ROOT/'templates/roguelike_stage_720p'


class NativeStageAssets:
    def __init__(self,root=ROOT):
        self.root=Path(root)
        self.revision=(self.root/'manifest.json').stat().st_mtime_ns
        self.templates=RogueTemplates(self.root)
        self.pages=json.loads((self.root/'manifest.json').read_text(encoding='utf-8'))['pages']
        for key,spec in self.templates.specs.items():
            if spec.get('native_client_size')!=[1280,720]:raise ValueError('Stage assets must be native 720P')
            roi=reference_bbox_to_client(spec['reference_roi'],(1280,720),reference_size=spec['reference_size'])
            image=self.templates.images[key]
            if image.std()<1 or image.shape[0]>roi[3]-roi[1] or image.shape[1]>roi[2]-roi[0]:
                raise ValueError('Invalid native stage template/ROI: '+key)
        for page in self.pages.values():
            S(page['state'])
            geometry=page.get('shop_geometry',{})
            if 'name_text_fraction' in geometry:
                box=geometry['name_text_fraction']
                if (len(box)!=4 or any(type(v) not in (int,float) for v in box)
                        or not 0<=box[0]<box[2]<=1 or not 0<=box[1]<box[3]<=1):
                    raise ValueError('Native shop name ROI must be inside its card')
            if 'refresh_cost_roi' in geometry:
                reference_bbox_to_client(geometry['refresh_cost_roi'],(1280,720),
                    reference_size=geometry['refresh_cost_reference_size'])
            if 'currency_digit_roi' in geometry:
                reference_bbox_to_client(geometry['currency_digit_roi'],(1280,720),
                    reference_size=geometry['currency_digit_reference_size'])
            icon_kernel=page.get('map_geometry',{}).get('icon_kernel')
            if icon_kernel is not None and (type(icon_kernel) is not int or not 1<=icon_kernel<=15 or icon_kernel%2==0):
                raise ValueError('Native map icon kernel must be a small positive odd integer')
            if not page['anchors'] or any(key not in self.templates.specs for key in page['anchors']):
                raise ValueError('Stage identity requires recorded anchors')
            if 'identity_anchors_any' in page:
                groups=page['identity_anchors_any']
                if not groups or any(not group or any(k not in self.templates.specs for k in group) for group in groups):
                    raise ValueError('Alternative page identities require complete recorded anchors')
            for panel in page.get('panel_geometry',[]):
                x,y,r,b=reference_bbox_to_client(panel['bounds'],(1280,720),reference_size=(1280,720))
                a,c,d,e=reference_bbox_to_client(panel['interior'],(1280,720),reference_size=(1280,720))
                if not (4<=x<r<=1276 and 4<=y<b<=716 and r-x>30 and b-y>30 and x<a<d<r and y<c<e<b):
                    raise ValueError('Panel identity requires complete client bounds and an inner region')
            if any(key not in self.templates.specs for key in page.get('action_refs',{}).values()):
                raise ValueError('Stage actions require recorded native targets')
            parent=self.pages.get(page.get('supersedes'))
            if page.get('supersedes') and (not parent or parent['state']!=page['state'] or
                    not set(parent['anchors'])<set(page['anchors'])):
                raise ValueError('Native page specialization requires stronger same-state anchors')


class NativeStageScreen(RoguelikeScreen):
    def __init__(self,task,frame,assets,*,special_shop=False):
        self.assets=assets;self.page=None
        super().__init__(task,frame,assets.templates,special_shop)
    def find(self,key,area=None):
        if key not in self.templates.specs:return None
        spec=self.templates.specs[key]
        if spec.get('score_mode')=='panel_boundary':
            token=(key,area)
            if token in self.cached:return self.cached[token]
            if [self.w,self.h]!=spec['native_client_size'] or area is not None:
                raise ValueError('Native panel evidence requires its recorded client ROI')
            bounds=tuple(spec['panel_bbox'])
            interior=reference_bbox_to_client(spec['reference_roi'],(self.w,self.h),reference_size=spec['reference_size'])
            evidence=panel_evidence(self.frame,bounds,interior)
            box=bounds if evidence['matched'] else None
            self.matches[key+str(area)]={'name':'roguelike_'+key,'matched':box is not None,
                'roi':bounds,'score_mode':'panel_boundary','geometry_evidence':evidence}
            self.cached[token]=box
            return box
        if spec.get('score_mode') in ('foreground_glyph','foreground_luma'):
            token=(key,area)
            if token in self.cached:return self.cached[token]
            if [self.w,self.h]!=spec['native_client_size'] or area is not None:
                raise ValueError('Native glyph matcher requires its recorded client ROI')
            x,y,r,b=reference_bbox_to_client(spec['reference_roi'],(self.w,self.h),reference_size=spec['reference_size'])
            # Match complete foreground lettering, excluding changing victory
            # scenery. Both strokes and their intervening gaps must agree.
            if spec['score_mode']=='foreground_glyph':
                threshold=spec['foreground_min']
                template=(np.min(self.templates.images[key],axis=2)>=threshold).astype('uint8')*255
                patch=(np.min(self.frame[y:b,x:r],axis=2)>=threshold).astype('uint8')*255
            else:
                template=self.templates.gray[key]
                patch=cv2.cvtColor(self.frame[y:b,x:r],cv2.COLOR_BGR2GRAY)
            score=raw_score=0.;box=None
            if template.std()<1:raise ValueError('Native foreground glyph is empty or constant')
            if patch.shape[0]>=template.shape[0] and patch.shape[1]>=template.shape[1]:
                score,loc,raw_score=match_native(patch,template,spec['threshold'],
                    antialias=spec.get('pixel_filter')=='antialias')
                if score>=spec['threshold']:
                    a,c=loc;box=(x+a,y+c,x+a+template.shape[1],y+c+template.shape[0])
                    if spec['score_mode']=='foreground_luma':
                        glyph=template>=.8*float(template.max())
                        actual=patch[c:c+template.shape[0],a:a+template.shape[1]]
                        if np.mean(actual[glyph])<.85*np.mean(template[glyph]):box=None
            self.matches[key+str(area)]={'name':'roguelike_'+key,'confidence':score,
                'raw_confidence':raw_score,'pixel_filter':spec.get('pixel_filter'),
                'roi':(x,y,r,b),'matched':box is not None,'score_mode':spec['score_mode'],
                'box':[box[0],box[1],box[2]-box[0],box[3]-box[1]] if box else None}
            self.cached[token]=box
            return box
        return super().find(key,area)
    def fixed(self,key,x,y,text='',enabled=True):
        # Ignore historical fixed points; require a current native target.
        target=(self.page or {}).get('action_refs',{}).get(key)
        bbox=self.find(target) if target else None
        return RogueAction(key,bbox,text or self.templates.specs[target].get('label',''),enabled) if bbox else None
    def detect_blank_dismiss_overlay(self):
        return bool(self.page and self.page['state']=='OVERLAY' and
                    all(self.find(k) for k in self.page['anchors']))
    def overlay_identity(self):
        identity=(self.page or {}).get('identity','')
        if identity in ('reward:display','display:blank_instruction'):
            # The heading/hint establish a display, never the item name or its
            # activation status. Foreground content only distinguishes successive
            # displays for the existing bounded dismissal confirmation.
            return identity+':'+(super().overlay_identity() or 'unreadable_content')
        return identity
    def battle_page(self):return bool(self.page and self.page['state']=='BATTLE')
    def device_icon(self,area):
        x,y,r,b=area
        roi=((x-20)/self.w,(y-48)/self.h,(x+25)/self.w,(y+2)/self.h)
        return any(self.find(key,roi) for key in
                   ('device_icon_idle','device_icon_idle_variant','device_icon_selected','device_icon_selected_shifted'))
    def find_special_deploy_card_fast(self):
        card=super().find_special_deploy_card_fast()
        # Blue robot portraits satisfy the historical colour heuristic. A
        # current device must also carry its own glyph in its own card corner.
        return card if card and self.device_icon(card.area) else None
    def special_device_selected(self):
        card=self.find_special_deploy_card_fast() if self.battle_page() else None
        if card is None:return False
        x,y,r,b=card.area
        fractions=[]
        for left,right in ((x-26,x-6),(r-5,r+15)):
            patch=self.frame[max(0,y-10):b-10,max(0,left):min(self.w,right)]
            if not patch.size:return False
            fractions.append(float(np.max(np.mean(np.min(patch,axis=2)>180,axis=0))))
        # Measured selected native device: both vertical-border fractions >.50;
        # unselected device/selected ordinary member <.12. Inspect this device,
        # never infer selection from a generic team-details label.
        if min(fractions)>.40:return True
        if max(fractions)<=.40:return False
        # A selected translucent card can have a dark border against the world.
        # Require both a bright card edge and the selected-size device glyph in
        # its raised position. The ordinary flat glyph is below this ROI; world
        # texture or a selected ordinary member cannot supply device identity.
        roi=((x-22)/self.w,(y-45)/self.h,(x+20)/self.w,(y-12)/self.h)
        return any(self.find(key,roi) for key in ('device_icon_selected','device_icon_selected_shifted'))
    def special_device_remaining(self):
        card=self.find_special_deploy_card_fast() if self.battle_page() else None
        if card is None:return None
        _,_,r,b=card.area
        area=(max(0,r-18)/self.w,max(0,b-3)/self.h,min(self.w,r+10)/self.w,min(self.h,b+24)/self.h)
        count=self.number(area,local=True)
        if count is not None:return count if 0<=count<=9 else None
        # The actual one-device glyph touches the detector crop's background;
        # the same OCR engine returns no boxes without padding, but 1 at .9992
        # with a 12-pixel black margin. One read-only fallback, same source ROI,
        # same .85 threshold; no inferred count or input retry.
        x,y,right,bottom=self.roi(area)
        patch=cv2.resize(self.frame[y:bottom,x:right],None,fx=3,fy=3)
        patch=cv2.copyMakeBorder(patch,12,12,12,12,cv2.BORDER_CONSTANT,value=(0,0,0))
        text=[compact(v.name) for v in self.task.ocr(frame=patch,threshold=.85)]
        if not text:
            # A selected last-device border can still defeat the wider padded
            # crop. Read only its recorded digit interior, not the bright edge.
            # One bounded fallback with the same engine, padding and .85 gate.
            patch=self.frame[b-4:b+19,r-11:r+1]
            patch=cv2.copyMakeBorder(cv2.resize(patch,None,fx=3,fy=3),12,12,12,12,
                                    cv2.BORDER_CONSTANT,value=(0,0,0))
            text=[compact(v.name) for v in self.task.ocr(frame=patch,threshold=.85)]
        if not text:
            # Actual native2 can extend to r+15, outside both older crops.
            # Read its complete right-hand glyph at the unchanged .85 gate;
            # a wider crop never overrides accepted or ambiguous prior text.
            if not (0<=r-4<r+18<=self.w and 0<=b-4<b+21<=self.h):return None
            patch=self.frame[b-4:b+21,r-4:r+18]
            patch=cv2.copyMakeBorder(cv2.resize(patch,None,fx=3,fy=3),12,12,12,12,
                                    cv2.BORDER_CONSTANT,value=(0,0,0))
            text=[compact(v.name) for v in self.task.ocr(frame=patch,threshold=.85)]
        return int(text[0]) if len(text)==1 and len(text[0])==1 and text[0] in '0123456789' else None
    def shop_ocr(self):
        # The native 720P font is already small. The legacy half-size inventory
        # query misreads actual 已售罄 as 已售器 and never releases TRANSIENT.
        # Keep source pixels and the same OCR engine/threshold/policy.
        x,y,r,b=self.roi((.14,.245,.975,.585))
        from types import SimpleNamespace
        self.boxes=[SimpleNamespace(x=box.x+x,y=box.y+y,width=box.width,height=box.height,
                    name=compact(box.name))
                    for box in self.task.ocr(frame=self.frame[y:b,x:r],threshold=.7)]
        geometry=(self.page or {}).get('shop_geometry',{})
        if 'name_text_fraction' not in geometry:return
        # A single-character name can merge with the role icon in the inventory
        # query. Read only the recorded native name text, at the same .7 gate.
        scale=self.w/3840
        for i in range(3):
            area=self.roi((.141+i*.280,534*scale/self.h,.411+i*.280,1049*scale/self.h))
            card=self.candidate(i,area,shop=True)
            if not (card.selectable and card.quality and not card.fingerprint):continue
            x,y,r,b=area;left,top,right,bottom=geometry['name_text_fraction']
            x,y,r,b=(round(x+(r-x)*left),round(y+(b-y)*top),
                     round(x+(r-x)*right),round(y+(b-y)*bottom))
            patch=self.frame[y:b,x:r]
            if not patch.size:continue
            words=self.task.ocr(frame=cv2.resize(patch,None,fx=3,fy=3),threshold=.7)
            if len(words)!=1 or not compact(words[0].name) or not all(
                    '\u4e00'<=c<='\u9fff' for c in compact(words[0].name)):continue
            box=words[0]
            self.boxes.append(SimpleNamespace(x=x+box.x/3,y=y+box.y/3,
                width=box.width/3,height=box.height/3,name=compact(box.name)))
    def number(self,area,local=False):
        value=super().number(area,local)
        geometry=(self.page or {}).get('shop_geometry',{})
        if (value is None and local and tuple(area)==(.795,.025,.835,.083)
                and (self.page or {}).get('state')=='SHOP'):
            # Real balance10 reads .802 without a margin and .996 with one.
            # Keep the same currency ROI, engine and .85 confidence gate;
            # missing/ambiguous digits still cannot authorize spending.
            x,y,r,b=self.roi(area)
            patch=cv2.resize(self.frame[y:b,x:r],None,fx=3,fy=3)
            patch=cv2.copyMakeBorder(patch,12,12,12,12,cv2.BORDER_CONSTANT,value=(0,0,0))
            text=[compact(box.name) for box in self.task.ocr(frame=patch,threshold=.85)]
            if not text and 'currency_digit_roi' in geometry:
                # Real native8 is .717 in the wide padded HUD crop, .995 in
                # this recorded digit-only ROI. Do not override ambiguous text
                # or infer a balance from previous purchases.
                x,y,r,b=reference_bbox_to_client(geometry['currency_digit_roi'],(self.w,self.h),
                    reference_size=geometry['currency_digit_reference_size'])
                patch=cv2.copyMakeBorder(cv2.resize(self.frame[y:b,x:r],None,fx=3,fy=3),
                    12,12,12,12,cv2.BORDER_CONSTANT,value=(0,0,0))
                text=[compact(box.name) for box in self.task.ocr(frame=patch,threshold=.85)]
            return int(text[0]) if len(text)==1 and text[0] and all(c in '0123456789' for c in text[0]) else None
        if (value is not None or not local or tuple(area)!=(.775,.15,.98,.235)
                or (self.page or {}).get('state')!='SHOP' or 'refresh_cost_roi' not in geometry):
            return value
        # The actual native refresh price1 is missed beside its icon/刷新 label.
        # Re-read only the recorded digit ROI with the same engine and .85
        # threshold; never infer a free refresh or change spending policy.
        x,y,r,b=reference_bbox_to_client(geometry['refresh_cost_roi'],(self.w,self.h),
            reference_size=geometry['refresh_cost_reference_size'])
        patch=cv2.resize(self.frame[y:b,x:r],None,fx=3,fy=3)
        text=[compact(box.name) for box in self.task.ocr(frame=patch,threshold=.85)]
        return int(text[0]) if len(text)==1 and text[0] and all(c in '0123456789' for c in text[0]) else None
    def special_device_removed(self,previous_area):
        # Last-device consumption removes its card instead of showing digit0.
        # Never infer exhaustion from a missing detector alone: the known battle
        # HUD, visible portrait and empty former device body must all remain.
        if not self.battle_page() or self.find_special_deploy_card_fast() is not None:return False
        if len(previous_area)!=4:return False
        x,y,r,b=previous_area
        if not (0<=x+6<r-6<=self.w and 0<=y+6<b-4<=self.h):return False
        patch=self.frame[y+6:b-4,x+6:r-6]
        value=np.max(patch,axis=2)
        portrait=self.frame[555:640,50:135]
        edges=cv2.Canny(patch,100,180)
        # A native battlefield grid line can cross the now-empty hand slot.
        # Exclude only a full-height, <=2px vertical component; other texture
        # and card edges retain the original emptiness gate.
        labels_count,labels,stats,_=cv2.connectedComponentsWithStats(edges)
        for label in range(1,labels_count):
            _,_,width,height,_=stats[label]
            if width<=2 and height>=patch.shape[0]*.9:
                edges[labels==label]=0
        if portrait.std()<=20:return False
        if np.median(value)<110 and value.std()<20 and np.mean(edges>0)<.01:return True
        # The consumed card can expose textured battlefield objects. Verify
        # loss of the blue device body AND its neutral card frame, rather than
        # requiring the world behind the hand to become a uniform black patch.
        hue,sat,val=cv2.split(cv2.cvtColor(patch,cv2.COLOR_BGR2HSV))
        blue=np.mean((hue>=90)&(hue<=125)&(sat>70)&(val>90))
        neutral=(np.min(patch,axis=2)>150)&(value.astype('int16')-np.min(patch,axis=2)<50)
        border=self.frame[max(0,y-18):min(self.h,b+18),r+1:min(self.w,r+10)]
        if not border.size:return False
        border_ink=(np.min(border,axis=2)>120)&(np.max(border,axis=2).astype('int16')-np.min(border,axis=2)<50)
        return bool((blue<.12 or not self.device_icon(previous_area))
                    and np.mean(neutral)<.005 and np.max(np.mean(border_ink,axis=0))<.3
                    and np.mean(np.min(portrait,axis=2)>180)>.01)
    def shop_observation(self):
        if self.special_shop or (self.page or {}).get('shop_kind')=='SPECIAL_OPERATION_SHOP':
            back=self.fixed('back',0,0)
            if back is None:return Observation(S.TRANSIENT,detail='Native special-shop back target unavailable')
            return Observation(S.SHOP,actions=(back,))
        observed=super().shop_observation()
        geometry=(self.page or {}).get('shop_geometry')
        if not geometry or observed.state!=S.SHOP or self.special_shop:return observed
        # Same stock/OCR/price policy; native anti-aliased labels need measured
        # photometry rather than the 4K padded-label white-pixel fraction.
        actions=[];enabled={}
        for action in observed.actions:
            if action is not None and action.key.startswith('exchange_'):
                x,y,r,b=action.area;patch=self.frame[y:b,x:r]
                valid=bool(patch.size and np.mean(np.min(patch,axis=2)>geometry['label_pixel_floor'])>
                           geometry['label_min_fraction'])
                action=replace(action,enabled=valid);enabled[action.key]=valid
            actions.append(action)
        cards=[]
        for card in observed.candidates:
            stock=self.candidate(card.index,card.area,shop=True)
            cards.append(replace(card,selectable=bool(stock.selectable and enabled.get('exchange_'+str(card.index),False))))
        return replace(observed,actions=tuple(actions),candidates=tuple(cards))
    def member_choice_source(self):
        # The foreground proves member selection even when the modal blurs
        # its parent title. Do not invent whether it came from an order/outpost.
        source=super().member_choice_source()
        if source:return source
        if (self.page or {}).get('identity')=='members:foreground':return 'members'
        return None
    def choices(self,members=False,ordinary=False):
        # The actual page must supply calibrated card regions. Historical y=820
        # is not a native coordinate, even though the policy is unchanged.
        geometry=(self.page or {}).get('card_geometry')
        if not geometry:return ()
        areas=[reference_bbox_to_client(b,(self.w,self.h),reference_size=geometry['reference_size'])
               for b in geometry['reference_bboxes']]
        return tuple(self.candidate(i,area,members) for i,area in enumerate(areas))
    def enabled_confirmation(self):return bool(self.fixed('confirm',0,0))
    def result_animation_finished(self):
        target=(self.page or {}).get('action_refs',{}).get('confirm') or (self.page or {}).get('action_refs',{}).get('next')
        area=self.find(target) if target else None
        if area is None:return False
        x,y,r,b=area
        return bool(np.mean(np.min(self.frame[y:b,x:r],axis=2)>180)>.35)
    def map_nodes(self):
        geometry=(self.page or {}).get('map_geometry')
        if not geometry:return ()
        x,y,r,b=reference_bbox_to_client(geometry['roi'],(self.w,self.h),reference_size=geometry['reference_size'])
        patch=cv2.resize(self.frame[y:b,x:r],None,fx=.25,fy=.25)
        hue,sat,val=cv2.split(cv2.cvtColor(patch,cv2.COLOR_BGR2HSV))
        mass=cv2.boxFilter(((hue>=78)&(hue<=100)&(sat>100)&(val>180)).astype('float32'),-1,
                          (geometry['kernel'],geometry['kernel']),normalize=True)
        nodes=[];scale=self.w/3840
        for _ in range(3):
            _,strength,_,loc=cv2.minMaxLoc(mass)
            if strength<=.28:break
            a,b=loc;cx,cy=x+a*4,y+b*4
            left=max(0,round(cx-115*scale));top=max(0,round(cy-350*scale))
            right=min(self.w,round(cx+115*scale));bottom=min(self.h,max(top,round(cy-110*scale)))
            icon=self.frame[top:bottom,left:right]
            if icon.size:
                lo=icon.min(axis=2);hi=icon.max(axis=2).astype('int16')
                mask=((lo>100)&(hi-lo<100)).astype('uint8')*255
                # Native weapon rings need a measured odd closing kernel;
                # retain the historical default for profiles without it.
                k=geometry.get('icon_kernel',max(1,round(7*scale)))
                mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((k,k),np.uint8))
                boxes=[cv2.boundingRect(c) for c in cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)[0]]
                boxes=[v for v in boxes if 75*scale<v[2]<210*scale and 75*scale<v[3]<210*scale and .65<v[2]/v[3]<1.5]
                if boxes:
                    ix,iy,iw,ih=max(boxes,key=lambda v:v[2]*v[3]);nodes.append((left+ix,top+iy,left+ix+iw,top+iy+ih))
            dx=max(1,round(50*scale));dy=max(1,round(40*scale))
            mass[max(0,b-dy):b+dy,max(0,a-dx):a+dx]=0
        nodes.sort(key=lambda area:(round(area[0]/(150*scale)),area[1]))
        return tuple(RogueAction('node_'+str(i),area,irreversible=False) for i,area in enumerate(nodes))
    def classify(self):
        candidates=[p for p in self.assets.pages.values() if all(self.find(k) for k in p['anchors'])]
        verified=[]
        for page in candidates:
            alternatives=page.get('identity_anchors_any')
            if alternatives and not any(all(self.find(k) for k in group) for group in alternatives):
                continue
            panels=page.get('panel_geometry',[])
            evidence=[panel_evidence(self.frame,v['bounds'],v['interior']) for v in panels]
            if panels:
                self.matches[page['identity']+'_panels']={'matched':all(v['matched'] for v in evidence),
                    'score_mode':'panel_boundary','geometry_evidence':evidence}
            if all(v['matched'] for v in evidence):verified.append(page)
        candidates=verified
        if any(p['state']=='OUTPOST' and p['identity']!='members:foreground' for p in candidates):
            candidates=[p for p in candidates if p['identity']!='members:foreground']
        superseded={p['supersedes'] for p in candidates if p.get('supersedes')}
        candidates=[p for p in candidates if not any(p is self.assets.pages[key] for key in superseded)]
        # A display heading AND its native blank-continue instruction own the
        # foreground. No UNKNOWN click, card activation or choice confirmation.
        heading=self.find('reward_display_heading')
        readable=False
        if heading:
            x,y,r,b=heading
            # Observed native headings: .149-.153 pixels above 180; another
            # modal/fade dims that fraction. Correlation alone ignores dimming.
            readable=np.mean(np.min(self.frame[y:b,x:r],axis=2)>180)>.12
        display=(readable and self.find('reward_blank') and not self.find('choice_heading'))
        overlays=[p for p in candidates if p['state']=='OVERLAY']
        if display:
            candidates=overlays or [dict(state='OVERLAY',
                anchors=['reward_display_heading','reward_blank'],identity='reward:display',
                action_refs={'dismiss':'reward_blank'})]
        if not candidates or all(page['state']=='MAP' for page in candidates):
            # The explicit, visible native instruction is enough to close a
            # display. Its item/floor title need not have a separate template.
            # These full instruction crops retain their calibrated thresholds;
            # neither UNKNOWN pixels nor a choice card authorize a blank click.
            # An active foreground instruction also owns a visible map behind it.
            for key in ('blank_close','reward_blank','member_reward_close',
                        'currency_blank_close','search_empty_close'):
                hint=self.find(key)
                if hint:
                    x,y,r,b=hint
                    reference=self.templates.gray[key]
                    current=cv2.cvtColor(self.frame[y:b,x:r],cv2.COLOR_BGR2GRAY)
                    glyph=reference>=.8*float(reference.max())
                    # A dimmed instruction behind another modal is not active.
                    # Match shape plus foreground glyph luminance, not the dark
                    # background that dominates mean pixel-difference scores.
                    if np.mean(current[glyph])<.85*np.mean(reference[glyph]):continue
                    candidates=[dict(state='OVERLAY',anchors=[key],
                                     identity='display:blank_instruction',
                                     action_refs={'dismiss':key})]
                    break
        if len(candidates)!=1:return Observation(detail='720P 内部页面没有唯一的原生证据')
        self.page=candidates[0]
        state=S(self.page['state'])
        if state==S.OVERLAY:
            action=self.fixed('dismiss',0,0)
            return Observation(state,event=self.overlay_identity(),actions=(action,) if action else (),
                               evidence=tuple(self.page['anchors']))
        if state==S.START:
            actions=tuple(a for key in self.page.get('action_refs',{}) if (a:=self.fixed(key,0,0)))
            return Observation(state,event=self.page.get('phase','unknown'),actions=actions)
        if state==S.MAP:
            back=self.fixed('back',0,0)
            return Observation(state,event='bounty_complete' if self.find('v_bounty_complete') else '',
                actions=self.map_nodes()+((back,) if back else ()),floor=self.page['floor'])
        observed=super().classify()
        if observed.state not in (state,S.TRANSIENT):return Observation(detail='Native page and existing Vision disagree')
        if observed.state==S.CHOICE and self.page.get('identity')=='choice:three_options':
            observed=replace(observed,event='choice')
        return observed
