"""Finite, page-verified tactic selection with mandatory default-state cleanup."""
from src.dispatch_screen import DispatchScreen,catalog,tactic_label
from ok import Box
from src.dispatch_policy import DispatchUnreadable,PROFESSIONS
from src.workflow import SafetyStop


class TacticFilter:
    default='全战术'
    @property
    def known(self):return {self.default,*catalog()['tactics']}

    def __init__(self,flow):self.f=flow;self.changed=False;self.verified=None

    def label(self):return DispatchScreen.filter_box(self.f).name.replace(' ','')

    def active(self,label):
        try:return self.label()==label
        except DispatchUnreadable:return False

    def matches(self,label):
        try:return not self.menu() and self.label()==label
        except DispatchUnreadable:return False

    def target(self,name,box):
        f=self.f;l,t,r,b=round(box.x),round(box.y),round(box.x+box.width),round(box.y+box.height)
        if not 0<=l<r<=3840 or not 0<=t<b<=2160:raise DispatchUnreadable('筛选目标坐标越界')
        image=f.task.frame[t:b,l:r].copy()
        f.vision.images[name]=image
        f.vision.manifest['templates'][name]={'roi':(max(0,l-15),max(0,t-15),min(3840,r+15),min(2160,b+15))}
        f.vision.begin_frame()

    def menu(self):
        f=self.f
        # Read the text column only. Faction/profession icons otherwise become
        # prefix characters (e.g. 🍱全战术) and hide the actual menu options.
        labels=f.task.ocr(x=1082/1280,y=151/720,to_x=1240/1280,to_y=550/720,
                          frame=f.task.frame,threshold=.85)
        options={}
        for b in labels:
            text=tactic_label(b.name)
            if text in self.known:options.setdefault(text,[]).append(b)
        # Dropdown: at least both options; choose the lower occurrence for the
        # current header duplicated in its menu. No approximate text targets.
        if self.default not in options or len(options)<2:return {}
        return {key:max(values,key=lambda b:b.y) for key,values in options.items()}

    def option(self,label,options):
        if label in options:return options[label]
        if not options:return None
        f=self.f
        # A low-confidence box can locate a reread, never authorize a click.
        boxes=f.task.ocr(x=1082/1280,y=151/720,to_x=1240/1280,to_y=550/720,
                         frame=f.task.frame,threshold=0)
        found=[b for b in boxes if tactic_label(b.name)==label]
        if len(found)!=1:return None
        b=found[0]
        reread=f.task.ocr(x=max(3246,b.x-54)/3840,y=max(453,b.y-24)/2160,
                         to_x=min(3720,b.x+b.width+45)/3840,
                         to_y=min(1650,b.y+b.height+24)/2160,frame=f.task.frame,threshold=.93)
        found=[b for b in reread if tactic_label(b.name)==label]
        if len(found)!=1:return None
        b=found[0]
        return Box(b.x,b.y,b.width,b.height,b.confidence,label)

    def set(self,label):
        if label not in self.known:raise DispatchUnreadable('未支持的筛选')
        f=self.f;f.require('DISPATCH')
        current=self.header()
        options=self.menu()
        if current.name.replace(' ','')==label and not options:
            f.wait(lambda:f.vision.page('DISPATCH') and self.matches(label),'复核筛选 '+label)
            self.verified=label
            return
        if not options:
            self.target('dispatch_filter_header',current)
            f.click('DISPATCH','dispatch_filter_header',lambda:bool(self.menu()),'打开战术下拉菜单')
        options=self.menu()
        option=self.option(label,options)
        if option is None:raise DispatchUnreadable('下拉菜单没有实际目标战术')
        self.target('dispatch_filter_option',option)
        f.click('DISPATCH','dispatch_filter_option',lambda:self.active(label),'选择战术 '+label)
        if self.menu():
            f.click('DISPATCH','dispatch_title',lambda:self.matches(label),'收起已确认的战术菜单')
        f.wait(lambda:self.matches(label),'复核实际筛选 '+label)
        self.changed=label!=self.default
        self.verified=label
        f.emit('dispatch_filter_verified',filter=label)

    def header(self):return DispatchScreen.filter_box(self.f)


    def restore_professions(self):
        ProfessionFilter(self.f).set('全职业')

    def cleanup(self):
        f=self.f
        try:
            f.checkpoint();f.refresh()
            if not f.vision.page('DISPATCH'):raise DispatchUnreadable('已离开可确认的筛选页面')
            self.set('全战术')
            f.emit('dispatch_filter_restored',filter='全战术',pending=False)
            return True
        except Exception as error:
            if (isinstance(error,DispatchUnreadable) and self.verified=='全战术' and
                    f.vision.page('DISPATCH') and
                    getattr(f.vision,'has',lambda *a:False)('dispatch_recall','dispatch_assigned')):
                f.emit('dispatch_filter_restored',filter='全战术',pending=False,verified_before_submit=True)
                return True
            # Cancellation/unknown page cannot authorize cleanup inputs. The next
            # task must independently set ALL before reading any candidate.
            f.emit('dispatch_filter_restore_pending',reason=str(error),next_task_requires='全战术')
            f.snapshot('dispatch_filter_restore_pending')
            return False


class ProfessionFilter(TacticFilter):
    default='全职业'
    @property
    def known(self):return {self.default,*PROFESSIONS}

    def header(self):
        f=self.f
        labels=f.task.ocr(x=.809,y=.15,to_x=.891,to_y=.21,frame=f.task.frame,threshold=.93)
        found=[b for b in labels if b.name.replace(' ','') in self.known]
        if len(found)!=1:raise DispatchUnreadable('当前职业筛选标签不唯一')
        return found[0]

    def label(self):return self.header().name.replace(' ','')
