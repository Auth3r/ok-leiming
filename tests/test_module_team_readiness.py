"""Real Beta 7 incident pixels; blueprint UI transitions are explicitly simulated.

Level matching, OCR geometry, team verification and preparation orchestration are
production code. Recorded OCR is produced by the installed engine on the fixture
pixels, including the original unreadable LV60. No game input is sent here.
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest
from ok import Box
from ok.feature.FeatureSet import FeatureSet
from ok.feature.Box import relative_box

from src import module_crafting as module
from src.resolution import ReferenceTask
from src.vision import Vision
from src.workflow import SafetyStop, Workflow

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / 'fixtures/module-team-20261031'


class Incident:
    def __init__(self, name='prepare', record=False):
        self.name, self.record, self.engine = name, record, None
        self.frame = cv2.imdecode(np.fromfile(FIXTURE/(name+'.png'), np.uint8), 1)
        self.records = json.loads((FIXTURE/'ocr.json').read_text(encoding='utf8')) if (FIXTURE/'ocr.json').exists() else {}
        self.features = FeatureSet(False, str(ROOT/'templates/coco_annotations.json'), .002, .002, .9)
        self.task = ReferenceTask(self, {'resolution_width':0, 'resolution_height':0})
        self.task.next_frame()
        self.vision = Vision(self.task, .9)

    def next_frame(self): return self.frame

    def find_one(self, name, **kwargs):
        frame = kwargs.pop('frame')
        found = self.features.find_feature(frame, name, limit=1, **kwargs)
        return found[0] if found else None

    def ocr(self, *, frame, x=0, y=0, to_x=1, to_y=1, threshold=.8, **kwargs):
        height, width = frame.shape[:2]
        area=relative_box(width,height,x,y,to_x,to_y)
        l,t,r,b=area.x,area.y,area.x+area.width,area.y+area.height
        crop = frame[t:b,l:r]
        key = hashlib.sha256(crop.tobytes()).hexdigest()
        if key not in self.records:
            assert self.record, ('Unrecorded actual OCR crop', self.name, key, crop.shape)
            if self.engine is None:
                from onnxocr.onnx_paddleocr import ONNXPaddleOcr
                self.engine = ONNXPaddleOcr(use_angle_cls=False,use_openvino=True)
            values = []
            for points,(text,confidence) in self.engine.ocr(crop)[0] or []:
                points=np.array(points);left,top=points.min(axis=0);right,bottom=points.max(axis=0)
                values.append({'text':text,'confidence':float(confidence),
                               'box':[float(left),float(top),float(right-left),float(bottom-top)]})
            self.records[key] = {'shape':list(crop.shape), 'values':values}
        return [Box(v['box'][0]+l,v['box'][1]+t,*v['box'][2:],v['confidence'],v['text'])
                for v in self.records[key]['values'] if v['confidence']>=threshold]

    def save(self):
        (FIXTURE/'ocr.json').write_text(json.dumps(self.records,ensure_ascii=False,indent=2)+'\n',encoding='utf8')


@pytest.mark.parametrize('name', ['prepare', 'craft'])
def test_report_lv60_matches_at_point_eight_despite_old_template_minimum(name):
    scene = Incident(name)
    # An old caller/template minimum must not silently reintroduce 0.97.
    scene.task.native_templates['module_team_level_60'] = [
        (image,.97) for image,_ in scene.task.native_templates['module_team_level_60']]
    assert module.occupied_slot(scene.task,0,2), 'The real 0.8231 LV60 must pass the common 0.8 gate'
    evidence = scene.task.module_slot_evidence['1-3']
    assert evidence['producer']=='level_glyph_60'
    assert evidence['score']==pytest.approx(.8230770826339722,abs=1e-6)


@pytest.mark.parametrize('name', ['prepare', 'craft'])
def test_report_all_required_members_are_recognized(name):
    scene=Incident(name)
    module.check_team(scene.vision)
    # Nine visible selected portraits are the reported state; an unreadable
    # level on one of those portraits must not become a second selection gate.
    assert scene.vision.page('MODULE_RESEARCH')


def add_empty_slot(scene, name='module_plus'):
    """Explicitly synthetic negative: paste an existing real plus template."""
    supplied=scene.vision.images[name]
    plus=cv2.resize(supplied,(round(supplied.shape[1]/3),round(supplied.shape[0]/3)),interpolation=cv2.INTER_AREA)
    height,width=plus.shape[:2]
    scene.frame[192:192+height,970:970+width]=plus
    scene.task.next_frame();scene.vision.begin_frame()
    assert scene.vision.find(name,module.TEAM_ROI), 'The real matcher must see the synthetic empty marker'


@pytest.mark.parametrize('name',['module_plus','module_plus_partial'])
def test_positive_empty_slot_stops_team_completion(name):
    scene=Incident();add_empty_slot(scene,name)
    with pytest.raises(SafetyStop,match='加号空位'):module.check_team(scene.vision)


class BlueprintFlow:
    """External game transition simulator with production initial-team recognition."""
    def __init__(self, tmp_path, *, changed=False, incomplete=False, already_selected=False):
        self.scene=Incident()
        self.task=self.scene.task
        self.vision=NS(task=self.task, page=self.page, find=self.find,
                       manifest=self.scene.vision.manifest,images=self.scene.vision.images,
                       has=lambda *names: all(self.find(name) for name in names))
        self.cfg={'制作配置':'先攻','match_threshold':.9}
        self.stage='selected' if already_selected else 'research'
        self.changed,self.incomplete=changed,incomplete
        self.roster_observed=False;self.roster_markers=set()
        self.inputs=[];self.events=[];self.results={};self.logdir=tmp_path
        self.state='MODULE_PREPARE';self.stop=NS(is_set=lambda:False)
        self.task.original.info_set=lambda *a:None
        self.task.original.log_info=lambda *a,**kw:None
        self.task.original.log_error=lambda *a,**kw:None
        self.task.original.terminal_stop=lambda *a:None
        self.task.original.find_feature=lambda *a,**kw:[Box(110,550,200,220,.99,'module_blue_card')]
        if incomplete:
            add_empty_slot(self.scene)

    def page(self, name):
        if name=='MODULE_RESEARCH':return self.stage in ('research','selected')
        if name=='BLUEPRINT_SELECTOR':return self.stage in ('selector','card')
        return False

    def find(self,name,roi=None):
        if name in ('module_plus','module_plus_partial'):
            if self.roster_observed:
                raise AssertionError('A completed team must not require another right-roster observation')
            value=self.scene.vision.find(name,roi)
            if not value:
                self.roster_markers.add(name)
                self.roster_observed=self.roster_markers=={'module_plus','module_plus_partial'}
            return value
        if name=='module_blue_choose':return self.stage=='research'
        if name=='module_blue_selected':return self.stage=='selected'
        if name=='module_confirm':return self.stage=='card'
        return self.scene.vision.find(name,roi)

    def wait(self,predicate,label):
        # Change the right portraits only after team completion was confirmed.
        if self.changed and self.roster_observed:
            self.scene.frame[176:433,956:1218]=0
            self.task.next_frame();self.scene.vision.begin_frame()
        result=predicate()
        if not result:raise SafetyStop(label)
        return result

    def require(self,page):
        if not self.page(page):raise SafetyStop(page)

    def click(self,page,target,after,label,**kwargs):
        self.require(page)
        if kwargs.get('precondition'):kwargs['precondition']()
        self.inputs.append(target)
        self.stage={'module_blue_choose':'selector','module_blue_card':'card','module_confirm':'selected'}[target]
        if not after():raise SafetyStop('Simulated page transition rejected: '+label)

    def emit(self,event,**data):self.events.append((event,data))
    def snapshot(self,*args):pass


@pytest.mark.parametrize('changed',[False,True])
def test_selected_team_enters_and_confirms_blueprint_without_roster_recheck(tmp_path,changed):
    flow=BlueprintFlow(tmp_path,changed=changed)
    module.ModulePreparation(flow).run()
    assert flow.inputs==['module_blue_choose','module_blue_card','module_confirm']
    assert flow.stage=='selected'


def test_incomplete_team_never_clicks_blueprint(tmp_path):
    flow=BlueprintFlow(tmp_path,incomplete=True)
    with pytest.raises(SafetyStop):module.ModulePreparation(flow).run()
    assert flow.inputs==[]


def test_already_selected_blueprint_is_preserved(tmp_path):
    flow=BlueprintFlow(tmp_path,already_selected=True)
    module.ModulePreparation(flow).run()
    assert flow.inputs==[]


@pytest.mark.parametrize('entry',['MODULE_PREPARE','MODULE_CRAFT'])
def test_both_entries_use_real_common_preparation(tmp_path,monkeypatch,entry):
    from src import crafting_runner
    flow=BlueprintFlow(tmp_path,changed=True)
    class UntilBlueprint(crafting_runner.ModuleCrafting):
        """Stop at the requested boundary; spending and later nodes are out of scope."""
        def __init__(self,flow):
            self.f=flow;self.epoch_baseline=None;self.policy=NS(name='先攻')
            self.acceptance_evidence=NS(start_module=lambda:None)
        def run(self):self.one()
        def publish(self,*args):pass
        def wait(self,predicate,label):
            assert flow.stage=='selected'
            assert predicate(NS(kind='prepared'))
        def start_prepared(self):pass
        def advance(self):pass
    monkeypatch.setattr(crafting_runner,'ModuleCrafting',UntilBlueprint)
    Workflow.run(flow,only=entry)
    assert flow.inputs==['module_blue_choose','module_blue_card','module_confirm']
    assert flow.stage=='selected'


@pytest.mark.parametrize('profile',['旧地联合','先攻','军工','一致'])
def test_all_profiles_use_same_selected_team_rule_without_new_ocr_gate(profile):
    task=NS(ocr=lambda *a,**kw:pytest.fail('No extra level OCR gate is permitted'))
    flow=NS(cfg={'制作配置':profile},vision=NS(task=task,page=lambda p:p=='MODULE_RESEARCH',find=lambda *a:False))
    module.ModulePreparation(flow).verify_preparation_team()


def test_wrong_page_cannot_confirm_team():
    flow=NS(cfg={'制作配置':'先攻'},vision=NS(page=lambda p:False,find=lambda *a:False))
    with pytest.raises(SafetyStop,match='页面变化'):
        module.ModulePreparation(flow).verify_preparation_team()


def test_all_level_template_variants_use_point_eight():
    manifest=json.loads((ROOT/'templates/module_season/manifest.json').read_text(encoding='utf8'))
    levels=[spec for name,spec in manifest['templates'].items()
            if spec.get('target',name).startswith('module_team_level_')]
    assert levels and all(spec['threshold']==.8 for spec in levels), 'Every team-level variant must use 0.8'


def test_new_preparation_cannot_reuse_previous_team_receipt(tmp_path):
    flow=BlueprintFlow(tmp_path)
    module.ModulePreparation(flow).run()
    assert flow.stage=='selected'
    # The same flow begins a second round with an independently observed empty
    # slot. A previous round's completed selection cannot authorize this one.
    flow.stage='research';flow.inputs=[]
    flow.roster_observed=False;flow.roster_markers=set()
    add_empty_slot(flow.scene)
    with pytest.raises(SafetyStop):module.ModulePreparation(flow).run()
    assert not flow.inputs


def test_resume_without_preparation_still_stops_for_empty_team():
    from src.crafting_runner import ModuleCrafting
    flow=NS(cfg={'制作配置':'军工'},vision=NS(
        page=lambda name:name=='MODULE_RESEARCH',
        find=lambda name,*a:True if name in ('module_name_军工','module_blue_selected','module_plus') else False),
        wait=lambda predicate,label:predicate())
    runner=ModuleCrafting.__new__(ModuleCrafting)
    runner.f=flow;runner.pending_action=None;runner.policy=NS(name='军工')
    runner.screen=lambda:pytest.fail('An empty team must stop before resource/start handling')
    with pytest.raises(SafetyStop,match='加号空位'):
        runner.start_prepared()


@pytest.mark.parametrize('balance,cost',[(100,0),(99,100)])
def test_existing_resource_protection_still_stops_start(balance,cost):
    from src.crafting_runner import ModuleCrafting
    runner=ModuleCrafting.__new__(ModuleCrafting)
    runner.pending_action=None;runner.policy=NS(name='先攻')
    runner.screen=lambda:NS(text_in=lambda roi:str(balance if roi[0]==1740 else cost))
    runner.observe_skill_opportunities=lambda *a,**kw:None
    runner.button=lambda *a,**kw:pytest.fail('Invalid existing resource precondition must not click')
    with pytest.raises(SafetyStop,match='资源不足或消耗无效'):
        runner.start_prepared()
