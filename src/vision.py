import json
import re
import cv2
import numpy as np
from ok import Box
from src.app_paths import APP_ROOT as ROOT


class Vision:
    """Thin adapter over ok-script matching, sharing exactly the same API in replay."""
    def __init__(self, task, threshold):
        self.task = task
        self.threshold = threshold
        self.manifest = json.loads((ROOT/'templates/manifest.json').read_text(encoding='utf-8'))
        self.images = {name: cv2.imdecode(np.fromfile(ROOT/'templates'/spec['file'],dtype=np.uint8),cv2.IMREAD_COLOR)
                       for name,spec in self.manifest['templates'].items()}
        if any(im is None for im in self.images.values()):
            raise ValueError('模板图片缺失或损坏')
        self.cache = {}
        self.results = {}
        self.native_specs = {}
        native=ROOT/'templates/daily_native/manifest.json'
        if native.exists():
            for name,spec in json.loads(native.read_text(encoding='utf-8'))['templates'].items():
                self.native_specs[name] = spec
                # Existing 4K assets remain the reference for 4K. ReferenceTask
                # independently selects native alternatives on a 720P client.
                if name in self.images:continue
                self.manifest['templates'][name]=spec
                im=cv2.imdecode(np.fromfile(native.parent/spec['file'],dtype=np.uint8),1)
                if im is None: raise ValueError('日常原生模板缺失：'+name)
                self.images[name]=cv2.resize(im,(im.shape[1]*3,im.shape[0]*3))
        seasonal=ROOT/'templates/module_season/manifest.json'
        if seasonal.exists():
            for name,spec in json.loads(seasonal.read_text(encoding='utf-8'))['templates'].items():
                im=cv2.imdecode(np.fromfile(seasonal.parent/spec['file'],dtype=np.uint8),1)
                if im is None:raise ValueError('新赛季模组图标缺失：'+name)
                target=spec.get('target',name)
                if target not in self.images:
                    self.manifest['templates'][target]=spec
                    self.images[target]=cv2.resize(im,(im.shape[1]*3,im.shape[0]*3),interpolation=cv2.INTER_NEAREST)
                self.native_specs.setdefault(target,spec)
                if hasattr(self.task,'native_templates'):
                    self.task.native_templates.setdefault(target,[]).append((im,spec['threshold']))

    def begin_frame(self):
        self.cache.clear()
        self.results.clear()

    def find(self, name, roi=None):
        if name=='daily_claim' and not self.page('DAILY_REWARD'):
            return None
        if name.startswith('module_name_'):
            matches=self.module_name_matches(name.removeprefix('module_name_'),roi)
            return matches[0] if len(matches)==1 else None
        spec = self.manifest['templates'][name]
        native=getattr(self.task,'native_frame',None)
        if isinstance(native,np.ndarray) and tuple(native.shape[1::-1])==(1280,720):
            spec=self.native_specs.get(name,spec)
        region = tuple(roi or spec['roi'])
        key = (name, region)
        if key not in self.cache:
            x,y,x2,y2 = region
            # Obtain best score even below decision threshold for useful failure diagnostics.
            b = self.task.find_one(name, template=self.images[name], box=Box(x,y,x2-x,y2-y),
                                   threshold=0.000001, frame=self.task.frame)
            score = float(b.confidence) if b else 0.0
            minimum=max(self.threshold,spec.get('threshold',0),.94 if name in ('free_one','free_zero','free_dialog_one','free_dialog_one_gan') else 0)
            native=getattr(self.task,'native_frame',None)
            if isinstance(native,np.ndarray) and tuple(native.shape[1::-1])==(1280,720):
                minimum=max(minimum,self.task.matched_native_thresholds.get(name,0))
            self.results[name+str(region)] = {'name':name,'confidence':score,'roi':region,
                'box': [b.x,b.y,b.width,b.height] if b else None, 'threshold':minimum, 'matched':score>=minimum}
            self.cache[key] = b if b and score>=minimum else None
            if name in ('main_black','main_daily') and self.cache[key] is None:
                self.cache[key]=self.home_entry_text(name,region)
            if name=='main_dark' and self.cache[key] is None and 'seasonal_activity_anchor' in self.images:
                from src.daily_navigation import seasonal_entry
                self.cache[key]=seasonal_entry(self,region)
            if name in ('dark_hub_research','dark_hub_trial') and self.cache[key] is None and 'seasonal_research_anchor' in self.images:
                from src.daily_navigation import seasonal_hub_control
                self.cache[key]=seasonal_hub_control(self,name)
            if name=='dark_map' and self.cache[key] is None:
                labels=self.task.ocr(x=x/3840,y=y/2160,to_x=x2/3840,to_y=y2/2160,
                                     frame=self.task.frame,threshold=.9)
                matches=[v for v in labels if re.search(r'(?<!\d)0?4[-－—]3(?!\d)',v.name.replace(' ',''))]
                if len(matches)==1:
                    self.cache[key]=matches[0]
                    self.results[name+str(region)]['ocr_fallback']=matches[0].name
        return self.cache[key]

    def home_entry_text(self, name, region):
        """Transparent home labels vary with the character and background."""
        if not self.find('main_nav'):return None
        l,t,r,b=region
        minimum=max(.9,self.threshold)
        labels=self.task.ocr(x=l/3840,y=t/2160,to_x=r/3840,to_y=b/2160,
                             frame=self.task.frame,threshold=minimum)
        expected={'main_black':'黑盾委托','main_daily':'待办'}[name]
        matches=[label for label in labels if label.confidence>=minimum
                 and re.sub(r'\s+','',label.name)==expected
                 and l<=label.x<label.x+label.width<=r
                 and t<=label.y<label.y+label.height<=b]
        target=matches[0] if len(matches)==1 else None
        self.results[name+'_text'+str(region)]={'name':name+'_text','roi':region,
            'box':[target.x,target.y,target.width,target.height] if target else None,
            'confidence':float(target.confidence) if target else 0.,
            'threshold':minimum,'matched':target is not None,'parent_anchor':'main_nav'}
        return target

    def module_name_matches(self,profile,region):
        """Text recognition for seasonal modules; no new image-template set."""
        if profile not in ('军工','一致','先攻') or region is None:return []
        if not (self.page('MODULE_SELECTOR') or self.page('MODULE_RESEARCH')):return []
        key=('module_names_'+profile,tuple(region))
        if key in self.cache:return self.cache[key]
        l,t,r,b=region
        labels=self.task.ocr(x=l/3840,y=t/2160,to_x=r/3840,to_y=b/2160,
                             frame=self.task.frame,threshold=.9)
        matches=[]
        aliases={'军工':('军工','军工联合','军工武装'),'一致':('一致','步调一致','一致战线'),'先攻':('先攻','先攻小组')}[profile]
        for label in labels:
            text=re.sub(r'\s+','',label.name).strip('◇◆◈♦♢·•・')
            if profile=='一致':text=re.sub(r'^[-—－]致战线','一致战线',text)
            x,y=label.x+label.width/2,label.y+label.height/2
            if (l<=x<r and t<=y<b and getattr(label,'confidence',0)>=.9 and
                    any(re.fullmatch(re.escape(alias)+r'\d{0,2}',text) for alias in aliases)):
                matches.append(label)
        icon='module_icon_'+profile
        if not matches and region[0]>=1300 and icon in getattr(self,'images',{}):
            matches=self.find_all(icon,region)
        matches=sorted(matches,key=lambda box:(round(box.y/40),box.x))
        if hasattr(self,'results'):
            self.results[str(key)]={'name':'module_name_'+profile,'roi':region,
                'box':[matches[0].x,matches[0].y,matches[0].width,matches[0].height] if matches else None,
                'confidence':float(matches[0].confidence) if matches else 0.,'threshold':.9,
                'matched':bool(matches),'labels':[{'text':b.name,'confidence':float(b.confidence),
                'box':[b.x,b.y,b.width,b.height]} for b in labels],'aliases':aliases}
        self.cache[key]=matches
        return matches

    def has(self, *names):
        return all(self.find(name) is not None for name in names)

    def find_all(self, name, roi=None):
        spec=self.manifest['templates'][name]
        native=getattr(self.task,'native_frame',None)
        if isinstance(native,np.ndarray) and tuple(native.shape[1::-1])==(1280,720):
            spec=self.native_specs.get(name,spec)
        x,y,r,b=roi or spec['roi']
        minimum=max(self.threshold,spec.get('threshold',0))
        return self.task.find_feature(name,template=self.images[name],box=Box(x,y,r-x,b-y),
            threshold=minimum,frame=self.task.frame)

    def free_dialog(self):
        return bool(self.find('free_dialog') and
                    (self.find('free_dialog_one') or self.find('free_dialog_one_gan')))

    def free_count(self):
        one,zero=self.find('free_one'),self.find('free_zero')
        if one and not zero:return 1
        if zero and not one:return 0
        labels=self.task.ocr(x=.71,y=.79,to_x=.85,to_y=.855,frame=self.task.frame,threshold=.95)
        values=[int(m.group(1)) for b in labels if
            (m:=re.fullmatch(r'今日免费次数[:：]([01])[/／]1',b.name.replace(' ','')))]
        return values[0] if len(values)==1 else None

    def reward_title(self):
        title=self.find('reward_title') or self.find('reward_title_black')
        if title:return title
        native=getattr(self.task,'native_frame',None)
        if (not isinstance(native,np.ndarray) or tuple(native.shape[1::-1])!=(1280,720)
                or not self.find('reward_prompt')):
            return None
        # The sweep heading overlays animated blue light. Require its exact
        # current text AND the independent calibrated dismissal instruction.
        region=tuple(self.manifest['templates']['reward_title']['roi'])
        key=('reward_title_ocr',region)
        if key not in self.cache:
            l,t,r,b=region
            labels=self.task.ocr(x=l/3840,y=t/2160,to_x=r/3840,to_y=b/2160,
                                 frame=self.task.frame,threshold=.98)
            matches=[v for v in labels if v.confidence>=.98
                     and re.sub(r'\s+','',v.name)=='物资入库审批'
                     and l<=v.x<v.x+v.width<=r and t<=v.y<v.y+v.height<=b]
            title=matches[0] if len(matches)==1 else None
            self.cache[key]=title
            self.results['reward_title_ocr']={'name':'reward_title_ocr','roi':region,
                'box':[title.x,title.y,title.width,title.height] if title else None,
                'confidence':float(title.confidence) if title else 0.,
                'threshold':.98,'matched':title is not None}
        return self.cache[key]

    def dispatch_leave_dialog(self):
        return self.has('dispatch_leave_prompt','dispatch_leave_confirm')

    def page(self, name):
        rules = {
            'MODULE_RESEARCH': ('module_team','module_start_label'),
            'MODULE_SELECTOR': ('module_filter','home'),
            'BLUEPRINT_SELECTOR': ('module_blue_filter','home'),
            # The opaque navigation control identifies the home route. Optional
            # transparent labels must not block unrelated navigation or recovery.
            'MAIN_MENU': ('main_nav',),
            'BLACK_SHIELD': ('black_title','black_credit'),
            'DISPATCH': ('dispatch_title','dispatch_reward','back'),
            'FUND': ('fund_title','home'),
            'NAV': ('nav_title','nav_recommend'),
            'RESOURCE_HUB': ('home','resource_title','resource_aux'),
            'LOGIN': ('login_enter','login_settings'),
            'MAINTENANCE': ('maintenance_text','maintenance_confirm'),
            'DARK_MAP': ('back','dark_map_landmark'),
            'DARK_HUB': ('dark_hub_research','dark_hub_trial','home'),
            'DARK_STAGE': ('dark_stage','dark_stage_aux','dark_sweep'),
            'DARK_DIALOG': ('dark_dialog','dark_start'),
            'GANHUO': ('gan_stage','gan_sweep','home'),
            'HUNT_GOLD': ('hunt_stage','hunt_sweep','home'),
            'FREE_DIALOG': ('free_dialog','free_dialog_one'),
            'REWARD': ('reward_title','reward_prompt'),
            'DAILY_REWARD': ('daily_title','daily_aux'),
            'DAILY_PLAN': ('home','daily_tab','daily_plan_active','daily_weekly_tab'),
        }
        # A known overlay always takes priority over the visible underlying scene.
        if name=='LOGIN' and self.find('maintenance_text'):return False
        if name=='DISPATCH_LEAVE':return self.dispatch_leave_dialog()
        if self.dispatch_leave_dialog():return False
        capacity='module_capacity_prompt' in getattr(self,'images',{}) and self.has(
            'module_capacity_prompt','module_capacity_cancel','module_capacity_title')
        if name=='MODULE_CAPACITY':return bool(capacity)
        if capacity:return False
        notice='notice_title' in getattr(self,'images',{}) and self.has('notice_title','notice_prompt')
        if name=='NOTICE':return bool(notice)
        if notice:return False
        if name not in ('REWARD','DARK_DIALOG','FREE_DIALOG'):
            if self.reward_title() or self.find('dark_dialog') or self.find('free_dialog'):
                return False
        if name=='REWARD': return bool(self.reward_title() and self.find('reward_prompt'))
        if name=='FREE_DIALOG': return self.free_dialog()
        if name=='BLACK_SHIELD':
            if self.has('black_title','black_credit'):return True
            if not self.has('home','black_credit'):return False
            labels=self.task.ocr(x=.325,y=.84,to_x=.55,to_y=.96,
                                 frame=self.task.frame,threshold=.95)
            # The reset counter belongs to the established credit/list layout.
            # A changed season title alone never identifies this page.
            return len([b for b in labels if re.fullmatch(r'今日剩余待办[:：][0-5][/／]5',
                       b.name.replace(' ',''))])==1
        if name=='FUND':
            return bool(self.find('home') and (self.find('fund_title') or
                         self.find('fund_claim') or self.find('fund_done')))
        if name=='BLUEPRINT_SELECTOR':
            native=getattr(self.task,'native_frame',None)
            if isinstance(native,np.ndarray) and tuple(native.shape[1::-1])==(1280,720):
                return ('module_blue_grade' in getattr(self,'images',{}) and
                        self.has('module_blue_filter','home','module_blue_grade'))
        if name=='MODULE_SELECTOR':
            if self.has(*rules[name]):return True
            if not self.has('home','back','module_confirm'):return False
            labels=self.task.ocr(x=.34,y=.14,to_x=.98,to_y=.22,frame=self.task.frame,threshold=.93)
            texts=[re.sub(r'[^\u4e00-\u9fff]','',b.name) for b in labels]
            return all(any(label==text for text in texts) for label in ('品阶','效能','当前版本'))
        return self.has(*rules[name])
