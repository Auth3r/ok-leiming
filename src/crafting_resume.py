"""Recovery records are evidence, never permission to repeat an uncertain confirmation."""
import json
import hashlib
from copy import deepcopy
from dataclasses import asdict
from uuid import UUID,uuid4,uuid5,NAMESPACE_URL
from src.crafting_policy import normalize_record
from src.workflow import SafetyStop
from src.app_paths import resolve_history_file


def optional_record(root,current):
    """History is optional. Never decode old screenshots to authorize current input."""
    folders=sorted((p for p in root.iterdir() if p.is_dir() and p!=current and
                    ((p/'resume.json').exists() or (p/'crafting.json').exists())),reverse=True)
    if not folders:return None
    folder=folders[0]
    source=folder/'resume.json'
    if not source.exists():source=folder/'crafting.json'
    try:
        data=json.loads(source.read_text(encoding='utf-8'))
        try:adjusted=read_adjustment(source)
        except (OSError,ValueError,TypeError,KeyError):adjusted=None
        values=adjusted or data.get('record',{})
        if 'completion_ledger' not in data:
            data['completion_ledger']=legacy_completion_ledger(folder,data)
        data['_source_path']=str(source)
        return data,normalize_record(values)
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        return None


def legacy_completion_ledger(folder,data):
    """A legacy status alone is not a source: audit verified starts and completions.

    Old MODULE_RESUME counters could already have been reset. They cannot prove a
    batch total, so upgrading them must stop instead of blessing a false zero.
    """
    try:
        events=[json.loads(line) for line in (folder/'events.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        start=events[0].get('run_plan',{})
        if events[0].get('event')!='start' or start.get('task_kind')!='MODULE_CRAFT':return None
        target=start.get('parameters',{}).get('制作数量')
        if target!=data.get('status',{}).get('目标'):return None
        if not any(e.get('event')=='verified' and e.get('step')=='实验启动 结果' for e in events):return None
        normal=quick=0
        for e in events:
            if e.get('event')!='crafting':continue
            message=e.get('当前状态','')
            if message=='本个模组正常完成':normal+=1
            elif message=='本个模组快速结束':quick+=1
            if (e.get('正常完成'),e.get('快速结束'))!=(normal,quick):return None
        status=data.get('status',{})
        if (status.get('正常完成'),status.get('快速结束'))!=(normal,quick):return None
        return {'version':1,'run_id':str(uuid5(NAMESPACE_URL,hashlib.sha256((folder/'events.jsonl').read_bytes()).hexdigest())),
                'profile':start.get('parameters',{}).get('制作配置'),'target':target,
                'normal':normal,'quick':quick,'source':'verified_legacy_events',
                'module_active':(data.get('observation') or {}).get('kind')!='map'}
    except (OSError,ValueError,TypeError,KeyError,IndexError,AttributeError):return None


def completion_counts(data,target,profile,linked):
    """Restore either a proven batch total or a current-state session increment.

    A v2 zero is relative to its observed baseline; it never states that the old
    batch completed zero modules. Its scope only authorizes the current module.
    """
    if not linked:raise SafetyStop('Resume记录关联失败，无法证明同一制作轮次；禁止默认完成数为0')
    ledger=data.get('completion_ledger')
    if not isinstance(ledger,dict):raise SafetyStop('Resume完成数量缺少可信来源；禁止猜测或默认0')
    try:
        UUID(ledger['run_id'])
        version=ledger['version']
        if type(version) is not int or version not in (1,2,3):raise ValueError('version')
        if version==1 and ledger['source'] not in ('verified_run_ledger','verified_legacy_events'):
            raise ValueError('source')
        if version==2 and ledger['source']!='trusted_current_state':raise ValueError('source')
        if version==3 and ledger['source']!='page_driven_current':raise ValueError('source')
        if ledger['profile']!=profile or type(ledger['target']) is not int or ledger['target']!=target:raise ValueError('batch target/profile')
        normal,quick=ledger['normal'],ledger['quick']
        unknown=ledger.get('unknown',0)
        if any(type(n) is not int or n<0 for n in (normal,quick,unknown)) or normal+quick+unknown>target:
            raise ValueError('counts')
        if type(ledger['module_active']) is not bool:raise ValueError('module_active')
        if version==1 and unknown:
            if (unknown!=1 or ledger.get('continuation_scope')!='current_module_only' or
                    type(ledger.get('session_delta_completed')) is not int or ledger['session_delta_completed']!=1 or
                    ledger['module_active'] or type(ledger.get('historical_completed')) is not int or
                    ledger['historical_completed']!=normal+quick):raise ValueError('unclassified current completion')
        if version in (2,3):
            if ledger['historical_completed'] is not None:raise ValueError('historical_completed')
            if ledger['continuation_scope']!='current_module_only':raise ValueError('continuation_scope')
            delta=ledger['session_delta_completed']
            if version==2 and unknown:raise ValueError('unknown v2')
            if type(delta) is not int or delta!=normal+quick+unknown or not 0<=delta<=1:
                raise ValueError('session_delta_completed')
            if ledger['module_active']!=(delta==0):raise ValueError('session module_active')
            baseline=ledger.get('baseline')
            if version==2 or baseline is not None:
                _rebaseline_hub(baseline)
                if type(ledger['baseline_progress']) is not int or ledger['baseline_progress']!=baseline['progress']:
                    raise ValueError('baseline_progress')
            elif ledger.get('baseline_progress') is not None:raise ValueError('baseline_progress')
        status=data['status']
        if any(type(status[k]) is not int for k in ('目标','正常完成','快速结束')):raise ValueError('status type')
        if (status['目标'],status['正常完成'],status['快速结束'])!=(target,normal,quick):raise ValueError('status mismatch')
        for key in ('分类未知完成','类型未知完成'):
            if key in status and (type(status[key]) is not int or status[key]!=unknown):
                raise ValueError('unknown status mismatch')
        return ledger
    except (ValueError,TypeError,KeyError,AttributeError) as error:
        raise SafetyStop('Resume账本损坏、数量来源不明确或制作目标已改变；禁止继续消耗') from error


def _rebaseline_hub(facts):
    """Validate complete, current hub facts supplied by strict visual observation."""
    if not isinstance(facts,dict):raise ValueError('缺少可信模组主界面')
    identity=facts.get('module_identity')
    if not isinstance(identity,(list,tuple)) or len(identity)<2 or any(
            not isinstance(part,str) or len(part)!=64 or set(part)-{'0','1'} for part in identity):
        raise ValueError('模组身份证据不足')
    progress=facts.get('progress')
    if type(progress) is not int or not 0<=progress<30:raise ValueError('当前进度不可信')
    resource=facts.get('resource')
    if not isinstance(resource,dict):raise ValueError('当前资源不可信')
    balance,capacity=resource.get('balance'),resource.get('capacity')
    if any(type(value) is not int for value in (balance,capacity)) or not 0<=balance<=capacity or capacity<=0:
        raise ValueError('当前资源不可信')
    if facts.get('node') not in ('战术核心','属性刻录','属性深度刻录','技能研习','技能深度研习',
                             '因子','变量','快速休整','未知区域刻蚀','完成模组'):
        raise ValueError('当前节点入口不可信')
    rows=facts.get('missions')
    if not isinstance(rows,(list,tuple)):raise ValueError('任务状态不完整')
    missions={}
    for row in rows:
        if not isinstance(row,(list,tuple)) or len(row)!=4:raise ValueError('任务状态不完整')
        group,attr,current,goal=row
        if group not in ('火力','狙击','隐刃','重盾','突击','工程','医疗','指挥官') or attr not in ('攻击','防御','最大生命'):
            raise ValueError('任务身份不可信')
        if type(current) is not int or type(goal) is not int or current<0 or goal<=0:
            raise ValueError('任务数值不可信')
        key=(group,attr,goal)
        if key in missions:raise ValueError('任务身份重复')
        missions[key]=current
    return identity,missions


def validate_current_state(current):
    """Validate present facts without requiring or attributing an old checkpoint.

    The caller supplies facts from a trusted hub observation and separately
    verifies capture stability and the current game/task. No historical action,
    progress, identity, reward or completion count participates in this proof.
    """
    try:
        _rebaseline_hub(current)
        return {'current':deepcopy(current),'current_progress':current['progress']}
    except (ValueError,TypeError,KeyError,AttributeError) as error:
        raise SafetyStop('无法重新建立基线：'+str(error)) from error


def read_adjustment(source):
    path=source.parent/'manual-adjustment.json'
    if not path.exists():return None
    data=json.loads(path.read_text(encoding='utf-8'))
    if data.get('source_hash')!=hashlib.sha256(source.read_bytes()).hexdigest():return None
    return normalize_record(data['record'])


def reconcile_attribute(record,options,visible,old_step,new_step):
    """Infer exactly one manual attribute choice, only with a one-node advance and matching goals."""
    if old_step is None or new_step!=old_step+1 or not visible:return False
    candidates=[]
    for o in options:
        changed=False;matches=True
        for group,attr,current,goal in visible:
            prior=[m for m in record.missions if (m.group,m.attr,m.goal)==(group,attr,goal)]
            if len(prior)!=1:matches=False;break
            delta=o.value if o.applies(group) and o.attr==attr else 0
            if prior[0].current+delta!=current:matches=False;break
            changed=changed or delta>0
        if matches and changed:candidates.append(o)
    unique={(tuple(o.groups or (o.group,)),o.attr,o.value,o.boost):o for o in candidates}
    if len(unique)!=1:return False
    record.commit('attribute',next(iter(unique.values())))
    return True


def load_resume(root,current):
    folders=sorted((p for p in root.iterdir() if p.is_dir() and p!=current and
                    ((p/'crafting.json').exists() or (p/'resume.json').exists())),reverse=True)
    if not folders:raise SafetyStop('没有可恢复的模组制作记录')
    folder=folders[0]
    path=folder/'resume.json'
    if path.exists():
        data=json.loads(path.read_text(encoding='utf-8'))
        if not data.get('safe') and (data.get('refresh_state') or {}).get('phase')!='confirming':
            raise SafetyStop('上次点击结果未确认，不能自动继续；请先核对游戏进度')
        picture=folder/'resume.png'
    else:
        data=json.loads((folder/'crafting.json').read_text(encoding='utf-8'))
        # Compatibility for v0.1.14: only a stopped run with no unrecorded choice confirmation.
        events=[json.loads(line) for line in (folder/'events.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        if not events or events[-1].get('event')!='stopped':raise SafetyStop('最近一次制作没有明确中断记录')
        last_commit=max((i for i,e in enumerate(events) if e.get('event')=='crafting' and
                         e.get('当前状态','').startswith('已确认：')),default=-1)
        tail=events[last_commit+1:]
        if last_commit<0 or any(e.get('event')=='click' and e.get('step') in ('确认','快速智能研发') for e in tail):
            raise SafetyStop('旧记录包含尚未核对的确认操作，无法自动恢复')
        clicks=[e for e in tail if e.get('event')=='click']
        if clicks and not any(e.get('event')=='verified' and e.get('step')==clicks[-1]['step']+' 结果' for e in tail):
            raise SafetyStop('旧记录最后一次点击结果未确认')
        pictures=sorted(folder.glob('*FAILED.png'))
        if not pictures:raise SafetyStop('缺少中断时的页面截图')
        picture=pictures[-1]
    d=data['status']
    if d['正常完成']+d['快速结束']>=d['目标']:raise SafetyStop('最近一轮制作已完成，请使用制作新模组')
    if not picture.exists():raise SafetyStop('缺少中断时的页面截图')
    return data,picture


def _semantic_skill_identity(identity):
    from src.crafting_policy import skill_title,skill_name,skill_module,core_skill_name
    try:
        signature=json.loads(identity)
        if isinstance(signature,list) and len(signature)==2 and signature[0] in ('skill','core_skill'):
            for row in signature[1]:
                if signature[0]=='core_skill' and isinstance(row,list) and len(row)==11:
                    row[0]=core_skill_name(row[0]);continue
                if isinstance(row,list) and len(row)==11 and row[9]=='':
                    title=skill_title(row[0])
                    if title!=row[0]:row[0],row[9]=skill_name(title),skill_module(title)
            return json.dumps(signature,ensure_ascii=False)
    except (ValueError,TypeError):pass
    return identity


def unknown_etch_action_evidence(source,data):
    """Prove the sole unresolved free entry or etching attribute confirmation."""
    from pathlib import Path
    source=Path(source);raw=source.read_bytes();saved=json.loads(raw)
    keys=('record','observation','completion_ledger','safe','pending_action','refresh_state','hub_step',
          'etching_pending','normal_origin_known','fast')
    pending=data.get('pending_action') or {}
    confirming=pending.get('phase')=='confirming' and pending.get('kind')=='attribute'
    mode='attribute_confirm' if confirming else 'entry'
    if (any(saved.get(k)!=data.get(k) for k in keys) or data.get('safe') is not False
            or (pending and not confirming) or data.get('refresh_state') or data.get('hub_step')!=27
            or data.get('etching_pending') is not True or data.get('normal_origin_known') is not True
            or data.get('fast') is not False
            or data.get('observation',{}).get('kind')!=('attribute' if confirming else 'unknown')):
        raise SafetyStop('未知区域恢复不是唯一未确认的免费入口')
    ledger=data['completion_ledger'];profile=ledger['profile']
    completion_counts(data,ledger['target'],profile,True)
    if profile not in ('军工','一致') or ledger['module_active'] is not True:
        raise SafetyStop('未知区域恢复缺少同件本地模组历史')
    from src.crafting_policy import CraftRecord
    from src.crafting_skill_state import build_local_skill_state
    state=build_local_skill_state(CraftRecord(**normalize_record(deepcopy(data['record']))),profile)
    if not state.trusted or state.craft_id!=ledger['run_id']+':module:'+str(ledger['normal']+ledger['quick']+1):
        raise SafetyStop('未知区域恢复的技能历史不可信')
    capture=data['observation']['capture'];window=capture['metadata']
    events_raw=(source.parent/'acceptance.jsonl').read_bytes()
    events=[json.loads(line) for line in events_raw.decode('utf-8').splitlines() if line.strip()]
    last=events[-1];action=last['action_id']
    tail=[row for row in events if row.get('action_id')==action]
    if (events[-4:]!=tail
            or [row['phase'] for row in tail]!=['action_started','input_ready','input_submission','action_unresolved']
            or any(row.get('module_key')!=ledger['run_id']+':module:'+str(
                ledger['normal']+ledger['quick']+1) or row.get('hub_step')!=27
                or row.get('facts',{}).get('kind')!=('attribute' if confirming else 'hub')
                or row.get('facts',{}).get('label')!=('确认' if confirming else '未知区域刻蚀') for row in tail)
            or last['facts'].get('error_type')!='RecognitionTimeout'
            or last['page'].get('kind')!=data['observation']['kind'] or last['page'].get('capture')!=capture
            or last['page'].get('raw_text')!=data['observation']['text']
            or last.get('image',{}).get('matches_capture') is not True
            or last['image'].get('pixels_sha256')!=capture.get('native_frame_sha256')
            or any(row.get('phase')=='input_submission' for row in events[events.index(tail[2])+1:])):
        raise SafetyStop('未知区域原始动作链不完整，不能解除未知输入')
    receipt=tail[2]['facts'].get('receipt') or {}
    context=receipt.get('initial_context') or {}
    if (tail[2]['facts'].get('send_status')!='native_submitted' or receipt.get('status')!='native_submitted'
            or any(receipt.get('target_'+k)!=window.get(k) for k in ('hwnd','pid'))
            or context.get('foreground_hwnd')!=window.get('hwnd')
            or context.get('foreground_pid')!=window.get('pid')
            or len(receipt.get('stages',[]))!=2 or any(s.get('sent')!=1 for s in receipt.get('stages',[]))
            or any(row.get('facts',{}).get('manual_intervention') is True for row in tail)):
        raise SafetyStop('未知区域入口原生回执或窗口不一致')
    picture=source.parent/last['image']['path']
    if not picture.resolve().is_relative_to(source.parent.resolve()):raise SafetyStop('未知区域截图越界')
    proof={'source_checkpoint':str(source),'source_sha256':hashlib.sha256(raw).hexdigest(),
            'events_sha256':hashlib.sha256(events_raw).hexdigest(),'window':{k:window[k] for k in ('hwnd','pid')},
            'picture':str(picture),'picture_sha256':last['image']['file_sha256'],'capture':capture,
            'mode':mode,'action_id':action}
    if confirming:
        selected=tail[1];path=source.parent/selected['image']['path']
        if (not path.resolve().is_relative_to(source.parent.resolve())
                or selected['page'].get('kind')!='attribute'
                or selected['image'].get('matches_capture') is not True):raise SafetyStop('缺少原属性选中凭证')
        proof.update(selected_picture=str(path),selected_picture_sha256=selected['image']['file_sha256'],
                     selected_capture=selected['page']['capture'])
    return proof


def _revalidate_unknown_etch_action(runner,data,current,observed):
    proof=data.get('_unknown_etch_recovery')
    if not proof and (data.get('safe') is not False or not data.get('etching_pending')
                     or (data.get('observation') or {}).get('kind') not in ('unknown','attribute')):
        return False,current,observed
    from pathlib import Path
    import cv2
    import numpy as np
    from src.crafting_vision import CraftScreen
    from src.resolution import ReferenceTask
    from types import SimpleNamespace
    try:
        actual=unknown_etch_action_evidence(data['_source_path'],data)
        if proof and actual!=proof:raise ValueError('未知区域来源变化')
        proof=actual;encoded=Path(proof['picture']).read_bytes()
        native=cv2.imdecode(np.frombuffer(encoded,np.uint8),1)
        if (hashlib.sha256(encoded).hexdigest()!=proof['picture_sha256'] or native is None
                or native.shape!=(720,1280,3)
                or hashlib.sha256(native.tobytes()).hexdigest()!=proof['capture']['native_frame_sha256']):
            raise ValueError('未知区域原始截图不匹配')
        def archived_screen(pixels,capture):
            # A live ReferenceTask intentionally substitutes its CURRENT native
            # capture for full-page OCR. Pin a read-only task to source pixels.
            reader=current.task.original.ocr if isinstance(current.task,ReferenceTask) else current.task.ocr
            task=ReferenceTask(SimpleNamespace(frame=pixels,ocr=reader),
                               {'resolution_width':1280,'resolution_height':720})
            task.native_frame=pixels;task.reference_frame=cv2.resize(pixels,(3840,2160))
            screen=CraftScreen(task,task.reference_frame);screen.native_frame=pixels
            screen.capture_provenance=deepcopy(capture)
            screen.capture_provenance['observation_epoch']=screen.observation_epoch
            return screen
        archived=archived_screen(native,proof['capture'])
        def valid(screen):
            return (screen.kind=='attribute' and hasattr(screen,'unknown_attribute_classification_evidence')
                and _selection_native_capture_matches(screen,proof['window'])
                and len(screen.options())==5 and screen.card_slots()==tuple(range(5))
                and '请选择一个选项' in screen.text_in((1600,970,2020,1080))
                and not any(screen.selected(o.area) for o in screen.options()))
        if not valid(archived):raise ValueError('未知区域原始五卡属性页不完整或已选中')
        if proof['mode']=='attribute_confirm':
            from src.crafting_policy import CraftRecord,Option
            encoded_selected=Path(proof['selected_picture']).read_bytes()
            selected_native=cv2.imdecode(np.frombuffer(encoded_selected,np.uint8),1)
            if (hashlib.sha256(encoded_selected).hexdigest()!=proof['selected_picture_sha256']
                    or selected_native is None or selected_native.shape!=(720,1280,3)):
                raise ValueError('原属性选中截图不匹配')
            selected=archived_screen(selected_native,proof['selected_capture'])
            option=Option(**data['pending_action']['option'])
            if (not runner._next_unknown_attribute(selected,archived) or not selected.target(option)
                    or [o.index for o in selected.options() if selected.selected(o.area)]!=[option.index]
                    or not selected.button('确认')):raise ValueError('原属性确认未证明进入不同的下一组卡片')
        signature=runner.signature(archived)
        for attempt in range(2):
            if not valid(current) or runner.signature(current)!=signature:
                raise ValueError('未知区域当前属性候选或窗口不同')
            if not attempt:
                epoch=current.observation_epoch
                runner.f.refresh();current=runner.screen();observed=runner.observation_data(current)
                if current.observation_epoch==epoch:raise ValueError('未知区域需要两帧当前观察')
        if unknown_etch_action_evidence(data['_source_path'],data)!=proof:raise ValueError('未知区域来源变化')
        if proof['mode']=='attribute_confirm':
            values=normalize_record(deepcopy(data['record']))
            record=CraftRecord(**{**values,'missions':[Option(**row) for row in values.get('missions',[])]})
            if any(row.get('event_id')==proof['action_id'] for row in record.history):raise ValueError('属性确认已记账')
            record.commit('attribute',option)
            record.history[-1].update(event_id=proof['action_id'],source='verified_unknown_attribute_transition')
            data['record']=asdict(record);data['pending_action']=None
        runner.resume_trace['unknown_etch_action_revalidated']={k:v for k,v in proof.items() if k!='capture'}
        runner.f.emit('craft_unknown_etch_action_recovered',**runner.resume_trace['unknown_etch_action_revalidated'],
                      input_replayed=False,source_history_modified=False,
                      attribute_committed=proof['mode']=='attribute_confirm')
        return True,current,observed
    except (OSError,ValueError,TypeError,KeyError,AttributeError,IndexError,SafetyStop,cv2.error) as error:
        runner.f.emit('craft_unknown_etch_action_rejected',reason=str(error),input_dispatched=False)
        return False,current,observed


def _same_observation(prior,current):
    if prior.get('kind') in ('result','unknown') and current.get('kind')=='result':
        import re
        def result_key(observation):
            text=observation.get('text','')
            title=re.match(r'^(军工武装|一致战线)(\d{1,3})',text)
            experience=re.search(r'模组研发经验(\d+)$',text)
            capture=observation.get('capture') or {};metadata=capture.get('metadata') or {}
            if (title and experience and '激活所需'+title[1]+'队员数量' in text
                    and '属性加成效果' in text and capture.get('source')=='ReferenceTask.native_frame'
                    and capture.get('native_size')==[1280,720]
                    and all(type(metadata.get(key)) is int and metadata[key]>0 for key in ('hwnd','pid'))):
                return title.groups(),int(experience[1]),metadata['hwnd'],metadata['pid']
        old,new=result_key(prior),result_key(current)
        if old is not None and new is not None:return old==new
    if prior.get('kind')!=current.get('kind'):return False
    if prior.get('identity') and current.get('identity'):
        return _semantic_skill_identity(prior['identity'])==_semantic_skill_identity(current['identity'])
    return bool(prior.get('text')) and prior['text']==current.get('text')


def _opened_refresh_skill_geometry(folder,rows,data,window):
    """A variable candidate count needs its original native occupancy witness."""
    from pathlib import Path
    import cv2
    import numpy as np
    from src.crafting_vision import CraftScreen
    paths=sorted(Path(folder).glob('*craft_refresh_decision.png'))
    if not paths:raise ValueError('variable skill count has no original decision picture')
    picture=paths[-1];pixels=cv2.imdecode(np.frombuffer(picture.read_bytes(),np.uint8),1)
    if pixels is None or pixels.shape!=(720,1280,3):raise ValueError('skill decision picture is not native720')
    slots,_=CraftScreen._card_slots_from_edges(pixels,native=True)
    expected=list(range(len(rows)))
    if slots is None or list(slots)!=expected:raise ValueError('skill decision occupancy differs')
    raw=(Path(folder)/'acceptance.jsonl').read_bytes()
    if not raw.endswith(b'\n'):raise ValueError('skill candidate event stream is incomplete')
    fields=('index','name','raw_name','module','level','gain','rarity','level_known','rarity_known','area')
    wanted=[{k:r[k] for k in fields} for r in rows]
    image_hash=hashlib.sha256(pixels.tobytes()).hexdigest()
    matches=[]
    for line in raw.decode('utf-8').splitlines():
        event=json.loads(line);page=event.get('page') or {};capture=page.get('capture') or {};cards=event.get('card_slots') or {}
        if (event.get('phase')=='skill_candidates' and event.get('hub_step')==data.get('hub_step')
                and page.get('kind')=='skill' and page.get('full') is True
                and capture.get('native_frame_sha256')==image_hash
                and all((capture.get('metadata') or {}).get(k)==window[k] for k in ('hwnd','pid'))
                and cards.get('geometry_known') is True and not cards.get('missing')
                and list(cards.get('slots') or ())==expected and list(cards.get('parsed') or ())==expected
                and event.get('candidates')==wanted):matches.append(event)
    if not matches:raise ValueError('skill count lacks same-frame complete candidate evidence')
    return {'picture':str(picture),'picture_sha256':hashlib.sha256(picture.read_bytes()).hexdigest(),
            'count':len(rows),'native_frame_sha256':image_hash,'acceptance_sha256':hashlib.sha256(raw).hexdigest()}


def opened_refresh_recovery_evidence(entry,source,data):
    """Bind an unconfirmed opened choice dialog to its original active attempt."""
    from pathlib import Path
    import cv2
    import numpy as np
    from src.crafting_policy import Option
    from src.crafting_vision import CraftScreen
    try:
        source=Path(source).resolve();raw=source.read_bytes()
        if json.loads(raw)!={k:v for k,v in data.items() if not k.startswith('_')}:
            raise ValueError('checkpoint content changed')
        ledger=completion_counts(data,1,entry['profile'],True)
        accounting=data.get('refresh_accounting') or {};window=(entry.get('start_evidence') or {}).get('window_identity') or {}
        kind=(data.get('refresh_state') or {}).get('kind')
        if (entry['state'] not in ('started','failed') or data.get('safe') is not True
                or data.get('pending_action') is not None or kind not in ('skill','attribute')
                or data.get('refresh_state')!={'kind':kind,'phase':'opened'}
                or ledger['run_id']!=(entry.get('start_evidence') or {}).get('run_id')
                or ledger['source']!='verified_run_ledger' or ledger['module_active'] is not True
                or ledger['normal']+ledger['quick']+ledger.get('unknown',0)!=0
                or type(accounting.get('version')) is not int or accounting['version']!=1
                or type(accounting.get('reserved_total')) is not int or accounting['reserved_total']<0
                or any(type(window.get(k)) is not int or window[k]<=0 for k in ('hwnd','pid'))):
            raise ValueError('not a safe opened dialog from this active attempt')
        observation=data.get('observation') or {};capture=observation.get('capture') or {}
        if (observation.get('kind')!='refresh_confirm' or capture.get('source')!='ReferenceTask.native_frame'
                or capture.get('native_size')!=[1280,720] or capture.get('reference_size')!=[3840,2160]
                or not capture.get('observation_epoch')
                or any((capture.get('metadata') or {}).get(k)!=window[k] for k in ('hwnd','pid'))):
            raise ValueError('opened source capture is not bound to this window')
        events_raw=(source.parent/'events.jsonl').read_bytes()
        if not events_raw.endswith(b'\n'):raise ValueError('incomplete event stream')
        events=[json.loads(line) for line in events_raw.decode('utf8').splitlines() if line.strip()]
        if not events or any(not isinstance(e,dict) or not isinstance(e.get('event'),str) for e in events):
            raise ValueError('invalid event stream')
        plan=events[0].get('run_plan') or {}
        if events[0]['event']!='start' or plan.get('制作配置')!=entry['profile'] or plan.get('制作数量')!=1:
            raise ValueError('source task identity differs')
        windows=[e.get('identity') or {} for e in events if e['event']=='resolution']
        if not windows or any(windows[-1].get(k)!=window[k] for k in ('hwnd','pid')):
            raise ValueError('source event window differs')
        origin=Path(entry['logdir']).resolve();recoveries=entry.get('recoveries',[])
        if resolve_history_file(origin/source.name,source.parent).resolve()==source:
            associations=[e for e in events if e['event']=='season_start_reserved']
        else:
            if not recoveries or resolve_history_file(Path(recoveries[-1]['logdir'])/source.name,source.parent).resolve()!=source:
                raise ValueError('not the latest attached recovery')
            associations=[e for e in events if e['event']=='season_attempt_resumed' and e.get('new_start_budget')==0
                and Path(e.get('source_checkpoint','')).resolve()==Path(recoveries[-1]['source_checkpoint']).resolve()]
        if len(associations)!=1 or associations[0].get('attempt_id')!=entry['id'] or associations[0].get('profile')!=entry['profile']:
            raise ValueError('source attempt association differs')
        decisions=[(i,e) for i,e in enumerate(events) if e['event']=='craft_refresh_decision']
        if not decisions:raise ValueError('missing original refresh decision')
        decision_index,decision=decisions[-1];rows=decision.get('options')
        if (decision.get('kind')!=kind or decision.get('record')!=data.get('record')
                or not isinstance(rows,list) or not rows or [r.get('index') for r in rows]!=list(range(len(rows)))
                or (kind=='skill' and not 1<=len(rows)<=9)):
            raise ValueError('source complete choice decision/history differs')
        if kind=='skill' and len(rows)!=5:
            _opened_refresh_skill_geometry(source.parent,rows,data,window)
        if kind=='attribute':
            decisions=[e for e in events[:decision_index] if e['event']=='craft_decision']
            observed=(decisions[-1].get('observation') or {}) if decisions else {}
            slots=observed.get('slots')
            if (not decisions or decisions[-1].get('record')!=data.get('record')
                    or (decisions[-1].get('decision') or {}).get('action')!='refresh'
                    or observed.get('options')!=rows or observed.get('cards_complete') is not True
                    or type(observed.get('remaining')) is not int or not 1<=observed['remaining']<=9
                    or observed['remaining']!=len(rows) or observed.get('count_source')!='paired_card_edges'
                    or not isinstance(slots,list) or len(slots)!=len(rows)):
                raise ValueError('source attribute independent occupancy/business is incomplete')
            for index,slot in enumerate(slots):
                geometry=slot.get('geometry') or {};edges=geometry.get('edge_coverage')
                native=geometry.get('producer')=='paired_card_edges_native_color'
                if (slot.get('index')!=index or slot.get('present') is not True or slot.get('missing')
                        or geometry.get('index')!=index or geometry.get('status')!='occupied'
                        or geometry.get('producer') not in ('paired_card_edges','paired_card_edges_native_color')
                        or not isinstance(edges,list) or len(edges)!=4
                        or any(not isinstance(v,(int,float)) or isinstance(v,bool) or not 0<=v<=1 for v in edges)
                        or sum(v>.65 for v in edges)<3 or native and not all(v>.65 for v in edges[2:])):
                    raise ValueError('source attribute card geometry is not independently complete')
        for row in rows:
            option=Option(**row)
            if kind=='skill' and (tuple(option.area)!=CraftScreen.option_areas()[option.index] or not option.name or not option.raw_name
                    or option.level_known is not True or option.rarity_known is not True or option.score_known is not True
                    or type(option.level) is not int or option.level<0 or type(option.gain) is not int or option.gain<=0):
                raise ValueError('source skill candidate is incomplete')
            if kind=='attribute' and (tuple(option.area)!=CraftScreen.option_areas()[option.index] or not option.name
                    or not option.group or not option.groups or option.attr not in ('攻击','防御','最大生命')
                    or type(option.value) is not int or option.value<=0 or type(option.score) is not int or option.score<0
                    or option.score_known is not True or option.boost_known is not True or option.rarity_known is not True
                    or type(option.boost) is not bool or type(option.overflow) is not bool
                    or not isinstance(option.portrait_signature,str) or len(option.portrait_signature)!=64
                    or set(option.portrait_signature)-{'0','1'}):
                raise ValueError('source attribute candidate business is incomplete')
        tail=events[decision_index+1:];clicks=[(i,e) for i,e in enumerate(tail) if e['event']=='click']
        if len(clicks)!=1 or clicks[0][1].get('step')!='刷新':
            raise ValueError('refresh was not the sole reversible input after its decision')
        if not any(e['event']=='verified' and e.get('step')=='刷新 结果' for e in tail[clicks[0][0]+1:]):
            raise ValueError('opened refresh result was not verified')
        picture=source.parent/'resume.png';encoded=picture.read_bytes()
        reference=cv2.imdecode(np.frombuffer(encoded,np.uint8),1)
        sha=lambda frame:hashlib.sha256(frame.tobytes()).hexdigest()
        if reference is None or reference.shape!=(2160,3840,3) or sha(reference)!=capture.get('reference_frame_sha256'):
            raise ValueError('source reference image differs from checkpoint capture')
        native=None;native_path=None
        for path in sorted(source.parent.glob('*FAILED.png'))+sorted(source.parent.glob('*timeout.png')):
            pixels=cv2.imdecode(np.frombuffer(path.read_bytes(),np.uint8),1)
            if pixels is not None and pixels.shape==(720,1280,3) and sha(pixels)==capture.get('native_frame_sha256'):
                native=pixels;native_path=path;break
        if (native is None or sha(cv2.resize(native,(3840,2160)))!=capture.get('reference_frame_sha256')
                or sha(cv2.resize(reference,(2048,1152)))!=capture.get('recognition_frame_sha256')):
            raise ValueError('source native/reference/recognition packet differs')
        return {'entry':deepcopy(entry),'source_checkpoint':str(source),'source_sha256':hashlib.sha256(raw).hexdigest(),
            'events_sha256':hashlib.sha256(events_raw).hexdigest(),'run_id':ledger['run_id'],'window_identity':deepcopy(window),
            'candidates':deepcopy(rows),'reserved_total':accounting['reserved_total'],'capture':deepcopy(capture),
            'reference_image':str(picture),'reference_image_sha256':hashlib.sha256(encoded).hexdigest(),
            'native_image':str(native_path),'native_image_sha256':hashlib.sha256(native_path.read_bytes()).hexdigest()}
    except (OSError,ValueError,TypeError,KeyError,AttributeError,IndexError,cv2.error) as error:
        raise SafetyStop('未证明本件刷新弹窗仅已打开且从未确认，未继续输入：'+str(error)) from error


def _opened_refresh_pixels(source,current,kind='skill',*,semantic_witness=False,candidate_count=5):
    """Compare fixed, individual skill text/level fields without searching alignment."""
    import numpy as np
    if source.shape!=(720,1280,3) or current.shape!=(720,1280,3):raise ValueError('native dialog geometry differs')
    fields={'header':(30,50,1240,102),'efficacy':(32,512,198,560),'missions':(922,270,1256,478),
            'dialog':(318,216,959,301),'confirm':(417,482,865,526),'amount':(618,434,710,466)}
    for index in range(candidate_count):
        x=65+(index%3)*646;y=190+(index//3)*263
        fields['portrait_'+str(index)]=tuple(round(v/1.6) for v in (x+20,y+15,x+150,y+90))
    measurements={}
    def compare(label,roi,tolerance):
        x,y,x2,y2=roi;a=source[y:y2,x:x2];b=current[y:y2,x:x2]
        if not a.size or float(a.std())<2 or float(b.std())<2:raise ValueError(label+' has no independent pixels')
        maximum=int(np.abs(a.astype(np.int16)-b.astype(np.int16)).max())
        if semantic_witness:tolerance=max(tolerance,3)
        correlation=None
        if maximum>tolerance and semantic_witness:
            # Only exact popup text + independently equal amounts may enable
            # this fixed-position shape comparison. Animated illumination must
            # not replace or align a candidate title, level or icon.
            import cv2
            high_a=a.astype(float)-cv2.GaussianBlur(a.astype(float),(0,0),3)
            high_b=b.astype(float)-cv2.GaussianBlur(b.astype(float),(0,0),3)
            va=high_a.ravel()-high_a.mean();vb=high_b.ravel()-high_b.mean()
            norm=float(np.linalg.norm(va)*np.linalg.norm(vb))
            correlation=float(va@vb/norm) if norm>0 else 0.
            minimum=.97 if label=='header' or label.startswith('portrait_') else .995
            if not np.isfinite(correlation) or correlation<minimum:
                raise ValueError(label+' shape differs from original dialog')
        elif maximum>tolerance:raise ValueError(label+' differs from original dialog')
        measurements[label]={'roi':list(roi),'maximum_channel_delta':maximum,'allowed_delta':tolerance}
        if correlation is not None:measurements[label]['same_position_highpass_ncc']=correlation
    if kind=='attribute':
        # Keep the entire card canvas exact, including the source popup overlay.
        # No hidden field is reconstructed, aligned or borrowed from history.
        compare('attribute_canvas',(28,108,1258,612),0)
        for label,roi in fields.items():compare(label,roi,0)
        return measurements
    for label,roi in fields.items():compare(label,roi,0)
    name_columns=((158,340),(566,748),(973,1156))
    level_columns=((340,415),(748,823),(1156,1231))
    names=[(name_columns[i%3][0],136+166*(i//3),name_columns[i%3][1],166+166*(i//3)) for i in range(candidate_count)]
    levels=[(level_columns[i%3][0],139+166*(i//3),level_columns[i%3][1],165+166*(i//3)) for i in range(candidate_count)]
    for index,(name,level) in enumerate(zip(names,levels)):
        compare('name_'+str(index),name,3);compare('level_'+str(index),level,3)
    return measurements


def _revalidate_opened_refresh(runner,data,current,observed):
    """Only two fresh source-bound observations restore opened; this sends no input."""
    from pathlib import Path
    import cv2
    import numpy as np
    from src.crafting_vision import CraftScreen
    proof=data.get('_opened_refresh_recovery')
    if not isinstance(proof,dict):return False,current,observed
    try:
        if opened_refresh_recovery_evidence(proof['entry'],proof['source_checkpoint'],data)!=proof:
            raise ValueError('original source proof changed')
        native=cv2.imdecode(np.frombuffer(Path(proof['native_image']).read_bytes(),np.uint8),1)
        reference=cv2.imdecode(np.frombuffer(Path(proof['reference_image']).read_bytes(),np.uint8),1)
        archived=CraftScreen(current.task,reference);archived.native_frame=native
        archived.capture_provenance=deepcopy(proof['capture'])
        archived.capture_provenance['observation_epoch']=archived.observation_epoch
        amounts=archived.refresh_amounts()
        kind=(data.get('refresh_state') or {}).get('kind')
        if (archived.kind!='refresh_confirm' or not amounts or amounts[:2]!=(1,1)
                or not archived.button('确认',(630,750,1410,850))):
            raise ValueError('original popup fields are unreadable')
        epochs=[];packets=[];checks=[]
        for index in range(2):
            game=getattr(runner.f,'target_identity',None) or {};window=proof['window_identity']
            if (current.kind!='refresh_confirm' or runner.policy.name!=proof['entry']['profile'] or runner.target!=1
                    or any(game.get(k)!=window[k] for k in ('hwnd','pid'))
                    or not _selection_native_capture_matches(current,window)
                    or not current.button('确认',(630,750,1410,850))):
                raise ValueError('current task/window/dialog capture differs')
            metadata=current.capture_provenance.get('metadata') or {}
            packet=(metadata.get('session_id'),metadata.get('sequence'))
            if (not isinstance(packet[0],str) or not packet[0] or type(packet[1]) is not int or packet[1]<0
                    or packet in packets or current.observation_epoch in epochs):
                raise ValueError('two distinct fresh capture packets are required')
            fresh_amounts=current.refresh_amounts()
            if fresh_amounts!=amounts:raise ValueError('current paired amounts/remaining differ from original')
            if kind=='attribute' and current.text!=(data.get('observation') or {}).get('text'):
                raise ValueError('current attribute popup raw business text differs from original')
            question=archived.text_in((510,340,1540,500))
            semantic=(kind=='skill' and question==current.text_in((510,340,1540,500))
                      and '消耗模组数据单元' in question and '当前研发节点剩余刷新次数' in question)
            fields=_opened_refresh_pixels(native,current.native_frame,kind,
                semantic_witness=semantic,candidate_count=len(proof['candidates']))
            epochs.append(current.observation_epoch);packets.append(packet)
            checks.append({'observation_epoch':current.observation_epoch,'packet':list(packet),'amounts':list(fresh_amounts),
                'native_frame_sha256':current.capture_provenance['native_frame_sha256'],'fields':fields})
            if index==0:
                current=runner.fresh_evidence(current,list(epochs),{'missing_field':'opened_refresh_identity',
                    'producer_plan':('fresh_frame_capture',),'action_dependency':'恢复原已打开刷新需两帧独立来源绑定'},
                    'craft_opened_refresh_retry')
                observed=runner.observation_data(current)
        if opened_refresh_recovery_evidence(proof['entry'],proof['source_checkpoint'],data)!=proof:
            raise ValueError('original source changed during fresh validation')
        runner.resume_trace['opened_refresh_revalidated']={'source_checkpoint':proof['source_checkpoint'],
            'source_sha256':proof['source_sha256'],'events_sha256':proof['events_sha256'],
            'run_id':proof['run_id'],'reserved_total':proof['reserved_total'],'kind':kind,'phase':'opened',
            'confirmation_sent':False,'captures':checks}
        return True,current,observed
    except (SafetyStop,OSError,ValueError,TypeError,KeyError,AttributeError,cv2.error) as error:
        raise SafetyStop('两次新帧未证明原已打开刷新弹窗，未继续输入：'+str(error)) from error


def selection_recovery_evidence(entry,source,data):
    """Read proof of one reversible selection, never resolve an old confirmation."""
    from pathlib import Path
    from src.crafting_policy import Option
    choices={'attribute','skill','mission','factor','rest','core_attribute','core_skill'}
    try:
        source=Path(source).resolve();raw=source.read_bytes()
        if json.loads(raw)!= {k:v for k,v in data.items() if not k.startswith('_')}:
            raise ValueError('checkpoint content changed')
        ledger=completion_counts(data,1,entry['profile'],True)
        if (entry['state'] not in ('started','failed') or data.get('safe') is not False
                or ledger['run_id']!=(entry.get('start_evidence') or {}).get('run_id')
                or ledger['module_active'] is not True or ledger['normal']+ledger['quick']+ledger.get('unknown',0)!=0):
            raise ValueError('not this active started attempt')
        pending=data.get('pending_action') or {}
        if pending and pending.get('phase')!='selecting':raise ValueError('old action is not a reversible selection')
        if (data.get('refresh_state') or {}).get('phase') in ('confirming','uncertain'):
            raise ValueError('unresolved refresh remains')
        observation=data.get('observation') or {};kind=observation.get('kind')
        if kind not in choices or not observation.get('identity'):raise ValueError('missing exact candidate identity')
        window=(entry.get('start_evidence') or {}).get('window_identity') or {}
        if any(type(window.get(key)) is not int or window[key]<=0 for key in ('hwnd','pid')):
            raise ValueError('original game window is not bound')
        events_raw=(source.parent/'events.jsonl').read_bytes()
        if not events_raw.endswith(b'\n'):raise ValueError('incomplete event stream')
        events=[json.loads(line) for line in events_raw.decode('utf8').splitlines() if line.strip()]
        if not events or any(not isinstance(row,dict) or not isinstance(row.get('event'),str) for row in events):
            raise ValueError('invalid event stream')
        plan=events[0].get('run_plan') or {}
        if events[0]['event']!='start' or plan.get('制作配置')!=entry['profile'] or plan.get('制作数量')!=1:
            raise ValueError('event task identity differs')
        identities=[row.get('identity') or {} for row in events if row['event']=='resolution']
        if not identities or any(identities[-1].get(key)!=window[key] for key in ('hwnd','pid')):
            raise ValueError('event window identity differs')
        origin=Path(entry['logdir']).resolve()
        recoveries=entry.get('recoveries',[])
        if resolve_history_file(origin/source.name,source.parent).resolve()==source:
            associations=[row for row in events if row['event']=='season_start_reserved']
        else:
            if not recoveries or resolve_history_file(Path(recoveries[-1]['logdir'])/source.name,source.parent).resolve()!=source:
                raise ValueError('not the latest linked recovery')
            associations=[row for row in events if row['event']=='season_attempt_resumed']
            associations=[row for row in associations if row.get('new_start_budget')==0
                          and Path(row.get('source_checkpoint','')).resolve()==Path(recoveries[-1]['source_checkpoint']).resolve()]
        if len(associations)!=1 or associations[0].get('attempt_id')!=entry['id'] or associations[0].get('profile')!=entry['profile']:
            raise ValueError('events are not associated with this attempt')
        clicks=[(i,row) for i,row in enumerate(events) if row['event']=='click']
        if not clicks:raise ValueError('no recorded selection input')
        last_click=clicks[-1][0]
        candidate_rows=[(i,row) for i,row in enumerate(events[:last_click]) if row['event']=='craft_candidates']
        if not candidate_rows:raise ValueError('selection has no prior candidate decision')
        candidate_index,decision=candidate_rows[-1]
        options=decision['options'];chosen=decision['chosen_index']
        if (decision.get('kind')!=kind or type(chosen) is not int or not isinstance(options,list)
                or not options or len({row['index'] for row in options})!=len(options)
                or decision.get('record')!=data.get('record')):
            raise ValueError('selection decision/history is inconsistent')
        targets=[row for row in options if row['index']==chosen and row['name']==decision.get('chosen')]
        if len(targets)!=1:raise ValueError('intended target is not unique')
        for row in options:Option(**row)
        allowed_steps={'选择 '+decision['chosen'],'选择 '+decision['chosen']+' 补点'}
        if any(row.get('step') not in allowed_steps for i,row in clicks if i>candidate_index):
            raise ValueError('a later confirmation/refresh/other input exists')
        if pending and (pending.get('kind')!=kind or pending.get('option')!=targets[0]
                        or pending.get('candidates')!=options):
            raise ValueError('durable selection intent differs')
        return {'entry':deepcopy(entry),'source_checkpoint':str(source),'source_sha256':hashlib.sha256(raw).hexdigest(),
                'events_sha256':hashlib.sha256(events_raw).hexdigest(),'kind':kind,'option':deepcopy(targets[0]),
                'candidates':deepcopy(options),'window_identity':deepcopy(window),'run_id':ledger['run_id']}
    except (OSError,ValueError,TypeError,KeyError,AttributeError,IndexError) as error:
        raise SafetyStop('缺少同一尝试仅选卡且未确认的完整证据；不解除未知确认：'+str(error)) from error


def _selection_portrait_source(proof,data):
    """Pin a selected 720P portrait to immutable checkpoint capture pixels."""
    from pathlib import Path
    import cv2
    import numpy as np
    from src.crafting_policy import Option
    from src.crafting_vision import CraftScreen
    observation=data.get('observation') or {};capture=observation.get('capture') or {}
    metadata=capture.get('metadata') or {};window=proof['window_identity']
    if (proof['kind']!='attribute' or capture.get('source')!='ReferenceTask.native_frame'
            or capture.get('native_size')!=[1280,720] or capture.get('reference_size')!=[3840,2160]
            or any(metadata.get(key)!=window[key] for key in ('hwnd','pid'))
            or not capture.get('observation_epoch')):
        raise ValueError('selected portrait lacks an associated native capture')
    fields=set(Option.__dataclass_fields__)-{'portrait_signature','score_confidence',
        'score_evidence_id','score_source','score_rejected_reason'}
    def business(rows):
        if not isinstance(rows,list):raise ValueError('selected source candidates are missing')
        rows=_legacy_non_skill_options(rows)
        if any(not fields.issubset(row) for row in rows):
            raise ValueError('selected source candidate fields are incomplete')
        return [json.dumps({key:row[key] for key in fields},ensure_ascii=False,sort_keys=True) for row in rows]
    rows=observation.get('options')
    if business(rows)!=business(proof['candidates']):
        raise ValueError('selected source candidates differ from the original decision')
    path=Path(proof['source_checkpoint']).parent/'resume.png';encoded=path.read_bytes()
    pixels=cv2.imdecode(np.frombuffer(encoded,np.uint8),cv2.IMREAD_COLOR)
    if pixels is None:raise ValueError('selected source image cannot be decoded')
    def sha(frame):return hashlib.sha256(frame.tobytes()).hexdigest()
    if pixels.shape==(720,1280,3):
        native=pixels;reference=cv2.resize(native,(3840,2160))
    elif pixels.shape==(2160,3840,3):
        reference=pixels;native=cv2.resize(reference,(1280,720))
    else:raise ValueError('selected source image geometry differs')
    frame=cv2.resize(reference,(2048,1152))
    if (sha(native)!=capture.get('native_frame_sha256') or sha(reference)!=capture.get('reference_frame_sha256')
            or sha(frame)!=capture.get('recognition_frame_sha256')):
        raise ValueError('selected source image does not match checkpoint capture hashes')
    screen=CraftScreen.__new__(CraftScreen)
    screen.kind='attribute';screen.frame=frame;screen.native_frame=native
    screen._options=[Option(**row) for row in rows]
    chosen=next(row for row in rows if row['index']==proof['option']['index'])
    area=tuple(chosen['area']);portrait=chosen['portrait_signature']
    if (area not in CraftScreen.option_areas() or not isinstance(portrait,str) or len(portrait)!=64
            or set(portrait)-{'0','1'} or CraftScreen.portrait_signature(screen,area)!=portrait):
        raise ValueError('selected source portrait does not match its recorded pixels')
    if [o.index for o in screen.options() if screen.selected(o.area)]!=[chosen['index']]:
        raise ValueError('selected source image does not uniquely select the intended target')
    return {'source_image':str(path),'source_image_sha256':hashlib.sha256(encoded).hexdigest(),
            'source_native_frame_sha256':sha(native),'source_recognition_frame_sha256':sha(frame),
            'source_observation_epoch':capture['observation_epoch'],'area':area,
            'original_portrait_signature':proof['option']['portrait_signature'],
            'pinned_portrait_signature':portrait,'window_identity':dict(window),'_frame':frame}


def _selection_native_capture_matches(screen,window):
    """The native, reference and recognition pixels must be one 720P packet."""
    import cv2
    import numpy as np
    frame=getattr(screen,'frame',None);native=getattr(screen,'native_frame',None)
    capture=getattr(screen,'capture_provenance',None) or {};metadata=capture.get('metadata') or {}
    return not (not isinstance(frame,np.ndarray) or frame.shape!=(1152,2048,3) or frame.dtype!=np.uint8
            or not isinstance(native,np.ndarray) or native.shape!=(720,1280,3) or native.dtype!=np.uint8
            or capture.get('source')!='ReferenceTask.native_frame'
            or capture.get('native_size')!=[1280,720] or capture.get('reference_size')!=[3840,2160]
            or capture.get('observation_epoch')!=getattr(screen,'observation_epoch',None)
            or any(metadata.get(key)!=window[key] for key in ('hwnd','pid'))
            or hashlib.sha256(native.tobytes()).hexdigest()!=capture.get('native_frame_sha256')
            or hashlib.sha256(frame.tobytes()).hexdigest()!=capture.get('recognition_frame_sha256')
            or hashlib.sha256(cv2.resize(native,(3840,2160)).tobytes()).hexdigest()!=capture.get('reference_frame_sha256')
            or hashlib.sha256(cv2.resize(cv2.resize(native,(3840,2160)),(2048,1152)).tobytes()).hexdigest()
                !=capture.get('recognition_frame_sha256'))


def _selection_portrait_measure(witness,screen,row):
    """Independent shape and inner-face evidence, never a Hamming tolerance."""
    from pathlib import Path
    import cv2
    import numpy as np
    from src.crafting_vision import CraftScreen
    if hashlib.sha256(Path(witness['source_image']).read_bytes()).hexdigest()!=witness['source_image_sha256']:
        raise ValueError('selected source image changed during revalidation')
    frame=getattr(screen,'frame',None);capture=getattr(screen,'capture_provenance',None) or {}
    if (tuple(row['area'])!=witness['area'] or screen.kind!='attribute'
            or not _selection_native_capture_matches(screen,witness['window_identity'])):
        raise ValueError('current portrait lacks matching geometry/capture provenance')
    portrait=CraftScreen.portrait_signature(screen,witness['area'])
    if portrait!=row['portrait_signature']:
        raise ValueError('current portrait signature is not from its current pixels')
    x,y,_,_=witness['area']
    def crop(image,offsets):
        a,b,a2,b2=offsets
        if not (0<=x+a<x+a2<=image.shape[1] and 0<=y+b<y+b2<=image.shape[0]):
            raise ValueError('portrait ROI is outside its bound frame')
        return cv2.cvtColor(image[y+b:y+b2,x+a:x+a2],cv2.COLOR_BGR2GRAY).astype(np.float64)
    source=crop(witness['_frame'],(20,15,150,90));current=crop(frame,(20,15,150,90))
    source_face=crop(witness['_frame'],(82,47,128,80));current_face=crop(frame,(82,47,128,80))
    source_high=source-cv2.GaussianBlur(source,(0,0),3)
    current_high=current-cv2.GaussianBlur(current,(0,0),3)
    textures=[float(image.std()) for image in (source,current,source_face,current_face,source_high,current_high)]
    if any(not np.isfinite(value) or value<8 for value in textures):
        raise ValueError('portrait pixels lack sufficient independent texture')
    def correlation(a,b):
        a=a.ravel()-a.mean();b=b.ravel()-b.mean()
        value=float(a@b/(np.linalg.norm(a)*np.linalg.norm(b)))
        if not np.isfinite(value):raise ValueError('portrait correlation is not finite')
        return value
    high=correlation(source_high,current_high);face=correlation(source_face,current_face)
    if high<.98 or face<.99:raise ValueError('selected portrait shape/inner-face identity differs')
    return {'observation_epoch':screen.observation_epoch,'portrait_signature':portrait,
            'recognition_frame_sha256':capture['recognition_frame_sha256'],
            'highpass_ncc':high,'inner_face_ncc':face,'texture_std':textures}


def _factor_selection_title(proof,data,current,runner):
    """Derive one recorded title from two immutable original native images."""
    from pathlib import Path
    import cv2
    import numpy as np
    from src.crafting_vision import CraftScreen
    if proof['kind']!='factor' or len(proof['candidates'])!=1:return proof,None
    if current.options()[0].name==proof['option']['name']:return proof,None
    pending=data.get('pending_action') or {};capture=(data.get('observation') or {}).get('capture') or {}
    if (pending.get('phase')!='selecting' or pending.get('hub_step')!=data.get('hub_step')
            or type(data.get('hub_step')) is not int or current.kind!='factor'):
        raise ValueError('factor title correction lacks the exact reversible node')
    folder=Path(proof['source_checkpoint']).parent
    events=[json.loads(line) for line in (folder/'events.jsonl').read_text(encoding='utf8').splitlines() if line.strip()]
    decision=max(i for i,row in enumerate(events) if row.get('event')=='craft_candidates')
    if sum(row.get('event')=='click' for row in events[decision+1:])!=1:
        raise ValueError('factor title correction requires the sole original selection click')
    reference_path=folder/'resume.png';reference_raw=reference_path.read_bytes()
    reference=cv2.imdecode(np.frombuffer(reference_raw,np.uint8),1)
    sha=lambda image:hashlib.sha256(image.tobytes()).hexdigest()
    if reference is None or reference.shape!=(2160,3840,3) or sha(reference)!=capture.get('reference_frame_sha256'):
        raise ValueError('factor source reference capture differs')
    native_path=None;native=None;native_raw=None
    for path in sorted(folder.glob('*FAILED.png')):
        encoded=path.read_bytes();image=cv2.imdecode(np.frombuffer(encoded,np.uint8),1)
        if image is not None and image.shape==(720,1280,3) and sha(image)==capture.get('native_frame_sha256'):
            native_path,native,native_raw=path,image,encoded;break
    if native is None:raise ValueError('factor source native capture missing')
    before_paths=sorted(folder.glob('*craft_before_click.png'))
    if not before_paths:raise ValueError('factor original pre-click image missing')
    before_path=before_paths[-1];before_raw=before_path.read_bytes()
    before=cv2.imdecode(np.frombuffer(before_raw,np.uint8),1)
    if before is None or before.shape!=(720,1280,3):raise ValueError('factor pre-click is not native720')
    area=tuple(proof['option']['area']);screens=[];names=[]
    expected=deepcopy(proof['candidates'])
    for pixels,ref in ((before,cv2.resize(before,(3840,2160))),(native,reference)):
        screen=CraftScreen(current.task,ref);screen.native_frame=pixels
        if screen.kind!='factor':raise ValueError('original title image is not factor')
        name=screen.factor_title_native(area)
        if not name:raise ValueError('original independent factor title is unreadable')
        rows=[asdict(o) for o in screen.options()]
        original=deepcopy(expected);original[0]['name']=name
        if json.dumps(rows,sort_keys=True)!=json.dumps(original,sort_keys=True):
            raise ValueError('original factor business differs beyond its title')
        names.append(name);screens.append(screen)
    if names[0]!=names[1]:raise ValueError('two original independent titles disagree')
    archived=screens[-1];archived.capture_provenance=deepcopy(capture)
    archived.capture_provenance['observation_epoch']=archived.observation_epoch
    if (not _selection_native_capture_matches(archived,proof['window_identity'])
            or json.loads((data.get('observation') or {})['identity'])!=json.loads(json.dumps(runner.signature(archived)))
            or [o.index for o in archived.options() if archived.selected(o.area)]!=[proof['option']['index']]
            or not archived.button('确认')):
        raise ValueError('original corrected factor is not uniquely selected in its source packet')
    corrected=deepcopy(proof);corrected['option']['name']=names[0];corrected['candidates'][0]['name']=names[0]
    sources=[(reference_path,reference_raw),(native_path,native_raw),(before_path,before_raw)]
    evidence={'original_name':proof['option']['name'],'independent_name':names[0],
              'sources':[{'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()} for path,raw in sources]}
    return corrected,evidence


def _redecide_recovered_attribute_scores(runner,data,proof,screen,rows):
    """An unknown unchosen score becoming readable needs a fresh decision.

    This supplies no identity evidence. All other fields, selected score,
    portraits, slots and two fresh selected/confirm observations are still
    validated by the caller; old history and current options stay untouched.
    """
    from src.crafting_policy import CraftRecord,Option
    chosen=proof['option']['index']
    prior={row['index']:row for row in proof['candidates']}
    recovered=[row['index'] for row in rows if row['index'] in prior and row['index']!=chosen
               and prior[row['index']]['score_known'] is False and row['score_known'] is True]
    if not recovered:return rows
    if not _selection_native_capture_matches(screen,proof['window_identity']):
        raise ValueError('recovered score lacks current native provenance')
    observation=screen.attribute_observation()
    record=CraftRecord(**normalize_record(deepcopy(data['record'])))
    record.missions=[Option(**m) for m in record.missions]
    decision=runner.policy.decide_attribute(observation,record,runner.attribute_config)
    if decision.action!='select' or decision.selected_index!=chosen:
        raise ValueError('current attribute policy no longer selects the original target')
    runner.f.emit('craft_selection_score_redecided',slots=recovered,selected_index=chosen,
        rule=decision.rule,observation_epoch=screen.observation_epoch,history_mutated=False)
    compared=deepcopy(rows)
    for row in compared:
        if row['index'] in recovered:
            row['score']=prior[row['index']]['score']
            row['score_known']=prior[row['index']]['score_known']
    return compared


def _legacy_non_skill_options(rows):
    # These later skill-only fields have neutral defaults on attribute/factor
    # cards. Preserve every saved value; never default missing business evidence.
    return [dict(skill_kind='skill',skill_usable=None,usability_evidence_id='') | row for row in rows]


def _revalidate_selection(runner,data,current,observed):
    """Two fresh observations can prove selection, but cannot prove confirmation."""
    from src.crafting_policy import Option
    proof=data.get('_selection_recovery')
    if not isinstance(proof,dict):return False,current,observed
    verified=selection_recovery_evidence(proof['entry'],proof['source_checkpoint'],data)
    if verified!=proof:raise SafetyStop('可逆选卡证据在恢复前发生变化，未继续输入')
    original_proof=proof
    if proof['kind'] in ('attribute','factor'):
        proof=deepcopy(proof)
        proof['candidates']=_legacy_non_skill_options(proof['candidates'])
    try:proof,factor_title=_factor_selection_title(proof,data,current,runner)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,IndexError) as error:
        raise SafetyStop('原因子标题没有独立同源证据，未继续输入：'+str(error)) from error
    ignored={'area','score_confidence','score_evidence_id','score_source','score_rejected_reason'}
    if proof['kind']=='skill':ignored.add('raw_name')
    fields=set(Option.__dataclass_fields__)-ignored
    attribute=proof['kind']=='attribute'
    chosen_index=proof['option']['index']
    portrait_observations=[]
    portrait_witness=None;portrait_checks=[];portrait_measurements=[]
    factor_packets=set()
    def fingerprint(rows,pixels_verified=False):
        if proof['kind']=='core_skill':
            from src.crafting_policy import core_skill_name
            rows=[dict(row,name=core_skill_name(row['name'])) for row in rows]
        if proof['kind']=='skill':
            from src.crafting_policy import skill_name
            rows=[dict(row,name=skill_name(row['name'])) for row in rows]
        if any(not fields.issubset(row) for row in rows):raise ValueError('candidate fields are incomplete')
        result=[]
        for row in rows:
            compared=fields
            if proof['kind']=='skill' and row['index']!=chosen_index:
                # Score decoration on an unchosen skill is not card identity.
                # Restore only control flow; the runner redecides on current
                # scores before any selection/confirmation after recovery.
                compared=compared-{'score','score_known'}
            if attribute:
                # Unchosen portraits can animate. Their complete policy inputs and
                # slot geometry remain exact; no obscured scope is borrowed here.
                compared=fields|{'area'}
                if row['index']!=chosen_index:compared=compared-{'portrait_signature'}
                else:
                    portrait=row['portrait_signature']
                    if (not isinstance(portrait,str) or len(portrait)!=64
                            or set(portrait)-{'0','1'}):
                        raise ValueError('chosen portrait is not a valid 64-bit identity')
                    if pixels_verified:compared=compared-{'portrait_signature'}
            result.append(json.dumps({key:row[key] for key in compared},ensure_ascii=False,sort_keys=True))
        return result
    def compare(screen,options,stage):
        nonlocal portrait_witness
        rows=[asdict(o) for o in options]
        if attribute:rows=_redecide_recovered_attribute_scores(runner,data,proof,screen,rows)
        actual=fingerprint(rows)
        if not attribute:
            if actual!=expected:raise ValueError('candidate list differs')
            return
        portrait_checks.append((screen,rows,stage))
        if actual==expected and portrait_witness is None:return
        if fingerprint(rows,True)!=fingerprint(proof['candidates'],True):
            raise ValueError('candidate business/slot/known fields differ')
        if portrait_witness is None:portrait_witness=_selection_portrait_source(proof,data)
        # If the first frame was exact and a later frame animates, independently
        # verify the earlier frame as well; both fresh observations stay required.
        for prior,prior_rows,prior_stage in portrait_checks[len(portrait_measurements):]:
            chosen=next(row for row in prior_rows if row['index']==chosen_index)
            result=_selection_portrait_measure(portrait_witness,prior,chosen)
            portrait_measurements.append(dict(result,stage=prior_stage))
    def page_identity(screen,observation):
        game=getattr(runner.f,'target_identity',None) or {}
        window=proof['window_identity']
        # The full candidate comparison below is the identity check. The old
        # failure-frame OCR string is neither stronger nor a second identity.
        if (screen.kind!=proof['kind'] or not observation.get('identity')
                or any(game.get(key)!=window[key] for key in ('hwnd','pid'))
                or runner.policy.name!=proof['entry']['profile'] or runner.target!=1):
            raise ValueError('page/task/window identity differs')
        if factor_title:
            if not _selection_native_capture_matches(screen,window):raise ValueError('current factor packet differs')
            metadata=screen.capture_provenance['metadata'];packet=(metadata.get('session_id'),metadata.get('sequence'))
            if (not isinstance(packet[0],str) or not packet[0] or type(packet[1]) is not int or packet in factor_packets):
                raise ValueError('two distinct factor packets are required')
            factor_packets.add(packet)
    def score_obscured(screen,observation):
        """Only the chosen score may wait; missing pixels never become proof."""
        if not attribute:return False
        page_identity(screen,observation)
        rows=[asdict(o) for o in screen.options()]
        fingerprint(rows,True)  # Complete fields and a valid chosen portrait are mandatory.
        unknown=[row for row in rows if row['score_known'] is False]
        if not unknown:return False
        if len(unknown)!=1 or unknown[0]['index']!=chosen_index:
            raise ValueError('only an obscured chosen score can be reobserved')
        business=(fields|{'area'})-{'portrait_signature'}
        if len(rows)!=len(proof['candidates']):raise ValueError('candidate count differs')
        for row,prior in zip(rows,proof['candidates']):
            compared=business
            if row['index']==chosen_index:
                if prior['score_known'] is not True:raise ValueError('original chosen score was not known')
                compared=business-{'score','score_known'}
            if any(json.dumps(row[key],sort_keys=True)!=json.dumps(prior[key],sort_keys=True) for key in compared):
                raise ValueError('candidate business/slot/known fields differ while score is missing')
        if not _selection_native_capture_matches(screen,proof['window_identity']):
            raise ValueError('missing score capture lacks matching native provenance')
        if ([o.index for o in screen.options() if screen.selected(o.area)]!=[chosen_index]
                or not screen.button('确认')):
            raise ValueError('missing score target is not uniquely selected with a confirm button')
        return True
    def validate(screen,observation):
        page_identity(screen,observation)
        options=screen.options()
        compare(screen,options,'before_target')
        selected=[o for o in options if screen.target(o) and screen.selected(o.area)]
        from src.crafting_policy import skill_name
        expected_name=skill_name(proof['option']['name']) if proof['kind']=='skill' else proof['option']['name']
        if proof['kind']=='core_skill':
            from src.crafting_policy import core_skill_name
            expected_name=core_skill_name(expected_name)
        if (len(selected)!=1 or selected[0].index!=proof['option']['index']
                or selected[0].name!=expected_name or not screen.button('确认')):
            raise ValueError('intended target is not uniquely selected with a confirm button')
        # target() can reparse a card and replace cached options on this frame.
        # Authorize only the final cache, including the exact selected portrait.
        options=screen.options()
        compare(screen,options,'after_target')
        if attribute:
            portrait_observations.append({'observation_epoch':getattr(screen,'observation_epoch',None),
                                          'portraits':{o.index:o.portrait_signature for o in options}})
    try:
        expected=fingerprint(proof['candidates'])
        epoch=getattr(current,'observation_epoch',None)
        if not epoch:raise ValueError('first capture epoch missing')
        epochs=[epoch];accepted=[];obscured=[]
        while True:
            if score_obscured(current,observed):
                obscured.append(current.observation_epoch)
                runner.f.emit('craft_selection_score_obscured',target_slot=chosen_index,
                    observation_epoch=current.observation_epoch,attempt=len(epochs),max_attempts=3,
                    history_mutated=False,successful_proof=False)
            else:
                validate(current,observed);accepted.append(current.observation_epoch)
                if len(accepted)==2:break
            if len(epochs)>=3:raise ValueError('three captures did not provide two complete score observations')
            current=runner.fresh_evidence(current,epochs,{'missing_field':'chosen_score' if obscured else 'selection',
                'target_slot':chosen_index,'producer_plan':('fresh_frame_capture',),
                'action_dependency':'恢复必须有两张完整候选且原目标唯一选中的新鲜画面'},'craft_selection_retry')
            observed=runner.observation_data(current)
    except (SafetyStop,ValueError,TypeError,KeyError,AttributeError,OSError) as error:
        raise SafetyStop('两次新鲜观察未证明原目标已选中且尚未确认，未继续输入：'+str(error)) from error
    runner.resume_trace['selection_revalidated']={'kind':proof['kind'],'index':proof['option']['index'],
        'source_checkpoint':proof['source_checkpoint'],'source_sha256':proof['source_sha256'],
        'events_sha256':proof['events_sha256'],'source_safe':False,'history_committed':False}
    if factor_title:
        from pathlib import Path
        if (selection_recovery_evidence(original_proof['entry'],original_proof['source_checkpoint'],data)!=original_proof
                or any(hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()!=row['sha256'] for row in factor_title['sources'])):
            raise SafetyStop('因子独立标题来源在恢复期间变化，未继续输入')
        runner.resume_trace['selection_revalidated']['factor_title_evidence']=factor_title
    if obscured:runner.resume_trace['selection_revalidated']['score_reobserved']={
        'missing_epochs':obscured,'accepted_epochs':accepted,'capture_count':len(epochs),'maximum_captures':3}
    if attribute:
        runner.resume_trace['selection_revalidated']['ignored_nonchosen_portraits']=[
            {'index':row['index'],'source_portrait_signature':row['portrait_signature'],
             'observations':[{'observation_epoch':capture['observation_epoch'],
                              'portrait_signature':capture['portraits'][row['index']]}
                             for capture in portrait_observations]}
            for row in proof['candidates'] if row['index']!=chosen_index and
            any(capture['portraits'][row['index']]!=row['portrait_signature'] for capture in portrait_observations)]
        if portrait_witness is not None:
            runner.resume_trace['selection_revalidated']['chosen_portrait_witness']={
                **{key:value for key,value in portrait_witness.items() if not key.startswith('_')},
                'producer':'capture_bound_portrait_shape_and_inner_face',
                'thresholds':{'highpass_ncc':.98,'inner_face_ncc':.99,'minimum_texture_std':8},
                'captures':portrait_measurements}
    return True,current,observed


def _revalidate_rest_hub_selection(runner,data,current,observed):
    """Abandon only a proven reversible rest selection on its unchanged hub."""
    proof=data['_selection_recovery'];pending=data.get('pending_action') or {}
    if selection_recovery_evidence(proof['entry'],proof['source_checkpoint'],data)!=proof:
        raise SafetyStop('休整选卡来源证据变化，未继续输入')
    prior=data.get('hub_baseline');accounting=data.get('refresh_accounting') or {}
    if (proof['kind']!='rest' or pending.get('phase')!='selecting' or pending.get('kind')!='rest'
            or type(accounting.get('version')) is not int or accounting['version']!=1
            or type(accounting.get('reserved_total')) is not int or accounting['reserved_total']<0):
        raise SafetyStop('同节点恢复缺少明确休整选择或已知刷新预算')
    validate_current_state(prior)
    if (prior['node']!='快速休整' or len(prior['module_identity'])!=4
            or type(pending.get('hub_step')) is not int or pending['hub_step']!=prior['progress']
            or type(data.get('hub_step')) is not int or data['hub_step']!=prior['progress']):
        raise SafetyStop('原休整选择与完整主界面基线不一致')
    epochs=[];captures=[];first=None
    def inspect(screen):
        window=proof['window_identity'];game=getattr(runner.f,'target_identity',None) or {}
        epoch=getattr(screen,'observation_epoch',None)
        if (screen.kind!='hub' or not epoch or epoch in epochs
                or runner.policy.name!=proof['entry']['profile'] or runner.target!=1
                or any(game.get(key)!=window[key] for key in ('hwnd','pid'))
                or not _selection_native_capture_matches(screen,window)):
            raise SafetyStop('同休整主界面缺少新鲜同窗口捕获来源')
        facts=screen.trusted_hub_state();validate_current_state(facts)
        if (len(facts['module_identity'])!=4 or
                any(json.dumps(facts[key],sort_keys=True)!=json.dumps(prior[key],sort_keys=True)
                    for key in ('progress','resource','missions','node'))):
            raise SafetyStop('休整主界面业务状态与原节点不同')
        diff=[sum(a!=b for a,b in zip(old,new)) for old,new in zip(prior['module_identity'],facts['module_identity'])]
        adjacent=([sum(a!=b for a,b in zip(old,new)) for old,new in
                   zip(first['module_identity'],facts['module_identity'])] if first else None)
        # Reuse the existing hub identity limit; chosen card portraits retain
        # their independent exact/capture-bound rules.
        if any(value>4 for value in diff) or adjacent and any(value>4 for value in adjacent):
            raise SafetyStop('休整主界面模组身份证据变化')
        epochs.append(epoch)
        captures.append({'observation_epoch':epoch,'capture':deepcopy(screen.capture_provenance),
                         'module_identity_diff_bits':diff,'previous_diff_bits':adjacent})
        return facts
    first=inspect(current);previous=current
    runner.f.refresh();current=runner.screen();observed=runner.observation_data(current)
    if current is previous:raise SafetyStop('第二次休整主界面观察不是新鲜画面')
    inspect(current)
    if selection_recovery_evidence(proof['entry'],proof['source_checkpoint'],data)!=proof:
        raise SafetyStop('休整选卡来源证据在观察期间变化')
    runner.resume_trace['rest_hub_selection_revalidated']={
        'source_checkpoint':proof['source_checkpoint'],'source_sha256':proof['source_sha256'],
        'events_sha256':proof['events_sha256'],'hub_baseline':deepcopy(prior),'captures':captures,
        'icon_maximum_diff_bits':4,'reversible_pending_released':True,'history_committed':False,
        'rest_confirmation_inferred':False}
    return True,current,observed


def _pending_blocks(data,current,observed,selection_revalidated=False):
    """Refuse only a confirmation which may repeat the old irreversible action."""
    pending=data.get('pending_action') or {}
    refresh=data.get('refresh_state') or {}
    if current.kind=='refresh_confirm' and refresh.get('phase') in ('confirming','uncertain'):
        return 'refresh_confirmation'
    if selection_revalidated and (not pending or pending.get('phase')=='selecting'):
        return None
    if refresh.get('phase') in ('confirming','uncertain') and not pending:
        return None  # The choice page may proceed; its refresh remains unavailable.
    uncertain=pending.get('phase')=='confirming' or data.get('safe') is False
    if not uncertain:return None
    prior=data.get('observation') or {}
    kind=pending.get('kind') or prior.get('kind')
    action_page='quick_dialog' if kind=='quick_finish' else 'prepared' if kind=='experiment_start' else kind
    if kind in ('attribute','skill','mission','factor','rest','core_attribute','core_skill') and current.kind=='hub':
        old_step=pending.get('hub_step',data.get('hub_step'))
        try:
            new_step=current.trusted_hub_progress() if hasattr(current,'trusted_hub_progress') else None
        except (SafetyStop,ValueError,TypeError,KeyError,AttributeError):new_step=None
        # Returning to the same hub step can lead straight back to the old choice.
        return None if (type(old_step) is int and type(new_step) is int and
                        0<=old_step<new_step<30) else kind
    if current.kind!=action_page:return None
    if current.kind=='hub':
        old=data.get('hub_step');new=current.progress() if hasattr(current,'progress') else None
        if type(old) is int and type(new) is int and old!=new:return None
    # OCR identity drift is not proof of a node transition: a missing card or
    # unread boost marker must never authorize the same confirmation again.
    return kind or 'unresolved_input'


def _archive_previous(runner,data):
    """Copy evidence without changing or resolving any previous action."""
    from pathlib import Path
    source=Path(data['_source_path']) if data.get('_source_path') else None
    if source is None:
        folders=sorted((p for p in runner.f.logdir.parent.iterdir() if p.is_dir() and p!=runner.f.logdir
                        and ((p/'resume.json').exists() or (p/'crafting.json').exists())),reverse=True)
        if folders:
            source=folders[0]/'resume.json'
            if not source.exists():source=folders[0]/'crafting.json'
    content=source.read_bytes() if source else None
    archive=runner.f.logdir/('previous-checkpoint-'+runner.checkpoint_session_id+'.json')
    archive_error=None
    if content is not None:
        try:
            temporary=archive.with_suffix('.tmp');temporary.write_bytes(content);temporary.replace(archive)
        except OSError as error:
            archive_error=str(error)
            runner.f.emit('craft_resume_archive',resume_mode='page_driven',failed_check=archive_error,
                          source_checkpoint=str(source),resolution='unknown')
    return {'session_id':runner.checkpoint_session_id,'parent_session_id':data.get('checkpoint_session_id'),
            'source_checkpoint':str(source) if source else None,
            'source_sha256':hashlib.sha256(content).hexdigest() if content is not None else None,
            'archive_checkpoint':archive.name if content is not None and archive_error is None else None,
            'archive_failed_check':archive_error,
            'source_ledger':deepcopy(data.get('completion_ledger')),
            'unresolved_previous_action':{'resolution':'unknown','action':deepcopy(data.get('pending_action'))},
            'unresolved_previous_refresh':{'resolution':'unknown','state':deepcopy(data.get('refresh_state'))}}


def _optional_hub_checkpoint(runner,current):
    """Current hub evidence improves accounting, never grants control-flow permission."""
    if current.kind!='hub':return
    import cv2
    trace=runner.resume_trace
    trace['rebaseline_attempted']=True
    try:
        if not hasattr(current,'trusted_hub_state'):raise ValueError('主界面当前事实读取不可用')
        facts=current.trusted_hub_state();validate_current_state(facts)
        game=deepcopy(getattr(runner.f,'target_identity',None))
        runner.f.refresh();latest=runner.screen()
        if latest.kind!='hub':raise ValueError('两次观察的主界面状态变化')
        fresh=latest.trusted_hub_state();validate_current_state(fresh)
        if game!=getattr(runner.f,'target_identity',None):raise ValueError('两次观察的游戏实例变化')
        identities=facts['module_identity'];new=fresh['module_identity']
        if ({k:v for k,v in facts.items() if k!='module_identity'}!=
                {k:v for k,v in fresh.items() if k!='module_identity'} or len(identities)!=len(new) or any(
                    sum(a!=b for a,b in zip(old,value))>4 for old,value in zip(identities,new))):
            raise ValueError('两次观察的主界面状态变化')
        frame=runner.f.last_frame
        if frame is None:raise ValueError('缺少当前截图')
        ok,png=cv2.imencode('.png',frame)
        if not ok:raise ValueError('当前截图编码失败')
        evidence=runner.f.logdir/('rebaseline-'+runner.checkpoint_session_id+'.png')
        content=png.tobytes();temporary=evidence.with_suffix('.tmp')
        temporary.write_bytes(content);temporary.replace(evidence)
        runner.epoch_baseline=deepcopy(fresh);runner.hub_baseline=deepcopy(fresh)
        runner.hub_step=fresh['progress']
        runner.rebaseline.update(current=fresh,evidence_id=evidence.name,
                                 evidence_sha256=hashlib.sha256(content).hexdigest(),game_identity=game)
        from src.crafting_policy import Option
        runner.record.missions=[Option(i,group=g,groups=(g,),attr=a,current=value,goal=goal,
                                       progress_known=True,reward_known=False)
                                for i,(g,a,value,goal) in enumerate(fresh['missions'])]
        runner.record.missions_complete=True
        trace.update(module_identity=fresh['module_identity'],progress=fresh['progress'],
                     resource=fresh['resource'],optional_rebaseline='pass')
        runner.f.emit('craft_resume_checkpoint',resume_mode='optional_rebaseline',current_page='hub',
                      continuation_scope=runner.continuation_scope,**runner.rebaseline)
    except (SafetyStop,ValueError,TypeError,KeyError,AttributeError,OSError,cv2.error) as error:
        trace['optional_rebaseline']='unavailable';trace['optional_failed_check']=str(error)
        runner.f.emit('craft_resume_checkpoint',resume_mode='optional_rebaseline',current_page='hub',
                      failed_check=str(error),continuation_scope=runner.continuation_scope,action='continue_current_page')


def page_driven_restore(runner):
    """Restore present control flow, then separately associate any trustworthy history."""
    from src.crafting_policy import CraftRecord,Option
    choices={'attribute','skill','mission','factor','rest','core_attribute','core_skill'}
    supported=choices|{'hub','result','refresh_confirm','prepared','overclock','map','quick_dialog'}
    runner.resume_trace={'resume_mode':'page_driven','historical_association':'fail',
                         'completion_history_known':False,'continuation_scope':'current_module_only',
                         'rebaseline_attempted':False,'module_identity':None,'progress':None,'resource':None,
                         'task_identity':{'profile':runner.policy.name,'target':runner.target,'task_kind':'MODULE_RESUME'},
                         'page_identity':None,'current_page':None,'pending_action':None,
                         'blocked_action':None,'failed_check':None}
    trace=runner.resume_trace
    try:
        runner.accounting_trusted=False
        saved=optional_record(runner.f.logdir.parent,runner.f.logdir)
        data,values=saved if saved else ({},{})
        accounting=data.get('refresh_accounting') or {}
        total=accounting.get('reserved_total') if isinstance(accounting,dict) else None
        runner.refresh_spent=(total if isinstance(accounting,dict) and type(accounting.get('version')) is int and
                              accounting.get('version')==1 and
                              type(total) is int and total>=0 else None)
        runner.f.refresh();current=runner.screen()
        game=getattr(runner.f,'target_identity',None)
        if isinstance(game,dict) and all(key in runner.f.cfg for key in ('game_exe','window_title','resolution_width','resolution_height')):
            from src.diagnostics import matches_target
            from src.resolution import Resolution,selected_size
            if not matches_target(game,runner.f.cfg) or game.get('minimized'):
                raise SafetyStop('当前游戏实例与目标任务不兼容，未继续输入')
            try:Resolution(tuple(game.get('resolution',())),selected_size(runner.f.cfg))
            except ValueError as error:
                raise SafetyStop('当前游戏实例与目标任务不兼容，未继续输入：'+str(error)) from error
        trace.update(current_page=current.kind,page_identity=current.kind,pending_action=data.get('pending_action'))
        if current.kind not in supported:raise SafetyStop('当前制作页面未识别；无法安全接管')
        if current.kind=='attribute' and data.get('safe') is True and hasattr(current,'recover_attribute_identities'):
            pending=data.get('pending_action') or {}
            snapshots=(data.get('observation') or {}).get('options',[])+pending.get('candidates',[])
            if pending.get('option'):snapshots.append(pending['option'])
            try:current.recover_attribute_identities([Option(**row) for row in snapshots])
            except (SafetyStop,TypeError,ValueError,KeyError,AttributeError) as error:
                trace['identity_recovery_failed_check']=str(error)
        try:observed=runner.observation_data(current)
        except (SafetyStop,TypeError,ValueError):observed={'kind':current.kind,'identity':None,'text':current.text}
        etch_entry_revalidated,current,observed=_revalidate_unknown_etch_action(runner,data,current,observed)
        if etch_entry_revalidated:values=normalize_record(deepcopy(data['record']))
        from src.crafting_local_runtime import guard_unresolved_resume, recover_verified_overclock
        overclock_revalidated,current,observed=recover_verified_overclock(runner,data,current,observed)
        if overclock_revalidated:
            values=normalize_record(deepcopy(data['record']))
            trace['local_overclock_revalidated']=True
        guard_unresolved_resume(runner,data,values)
        rest_hub_revalidated=False
        if current.kind=='hub' and (data.get('_selection_recovery') or {}).get('kind')=='rest':
            rest_hub_revalidated,current,observed=_revalidate_rest_hub_selection(runner,data,current,observed)
            selection_revalidated=False
        else:selection_revalidated,current,observed=_revalidate_selection(runner,data,current,observed)
        opened_refresh_revalidated,current,observed=_revalidate_opened_refresh(runner,data,current,observed)
        terminal_core_revalidated=False
        terminal=data.get('_terminal_core_recovery')
        if terminal:
            from pathlib import Path
            if hashlib.sha256(Path(terminal['source']).read_bytes()).hexdigest()!=terminal['sha256']:
                raise SafetyStop('最终核心恢复来源变化')
            first_epoch=current.observation_epoch
            for attempt in range(2):
                if (current.kind!='hub' or current.progress()!=29 or not current.button('进行封装') or
                        not _selection_native_capture_matches(current,terminal['window'])):
                    raise SafetyStop('最终核心确认未观察到29/30封装节点，未重放确认')
                if not attempt:
                    runner.f.refresh();current=runner.screen();observed=runner.observation_data(current)
                    if current.observation_epoch==first_epoch:raise SafetyStop('封装节点缺少新帧')
            terminal_core_revalidated=True
            trace['terminal_core_revalidated']={'from_step':28,'to_step':29,'confirmation_replayed':False}
        # The terminal hub has no automatic-development button. Its native,
        # twice-observed 28 -> 29 progress proves the old core confirmation done.
        from src.crafting_local_runtime import resume_committed
        local_skill_revalidated=resume_committed(runner,data,current,observed)
        if local_skill_revalidated:trace['local_skill_committed_revalidated']=True
        blocked=None if terminal_core_revalidated or etch_entry_revalidated else _pending_blocks(
            data,current,observed,selection_revalidated or rest_hub_revalidated)
        if blocked:
            trace['blocked_action']=blocked
            if blocked in choices:
                pending=data.get('pending_action') or {}
                old_step=pending.get('hub_step',data.get('hub_step'))
                option=(pending.get('option') or {}).get('name') or '原选择'
                trace['manual_check']={'old_option':option,'old_step':old_step,
                                       'source_checkpoint':data.get('_source_path'),'resolution':'unknown'}
                location='请只手动返回研发进度页' if current.kind!='hub' else '请在当前研发进度页'
                raise SafetyStop('旧不可逆动作结果不确定，禁止重放：'+blocked+'；需人工核对“'+option+'”的确认结果。'+
                                 location+'核对进度是否超过旧进度'+str(old_step if type(old_step) is int else '未知')+
                                 '（勿确认或刷新）；若已前进，停在该页再继续。若未前进，只有明确旧选择未生效才人工处理该节点，'
                                 '待进度前进后再继续；无法判断则保留现场。人工处理不计自动通过。')
            raise SafetyStop('旧不可逆动作结果不确定，禁止重放：'+blocked)
        ledger=None
        try:ledger=completion_counts(data,runner.target,runner.policy.name,True)
        except SafetyStop as error:trace['historical_failure']=str(error)
        # Complete selection revalidation already proves this page, candidates,
        # target and two fresh captures. Do not veto it with the old OCR spelling.
        same=selection_revalidated or etch_entry_revalidated or _same_observation(data.get('observation') or {},observed)
        inactive=bool(ledger and not ledger['module_active'] and current.kind in ('map','prepared'))
        observed_etch_map=bool(ledger and ledger['module_active'] and current.kind=='map' and
                              data.get('etching_pending') is True and data.get('etch_result_seen') is True and
                              (data.get('observation') or {}).get('kind') in ('result','map'))
        identity_conflict=False
        if ledger and data.get('safe') is True and current.kind=='hub' and data.get('hub_baseline'):
            try:
                prior=data['hub_baseline']['module_identity'];fresh=current.trusted_hub_state()['module_identity']
                identity_conflict=not (len(prior)==len(fresh) and all(len(old)==len(new) and sum(a!=b for a,b in zip(old,new))<=4
                                                                 for old,new in zip(prior,fresh)))
            except (SafetyStop,ValueError,TypeError,KeyError,AttributeError):identity_conflict=True
            if identity_conflict:same=False
        reconciled=False
        pending=data.get('pending_action') or {}
        old_step=data.get('hub_step')
        new_step=current.progress() if current.kind=='hub' and hasattr(current,'progress') else None
        if (ledger and not same and not identity_conflict and data.get('safe') is True and
                pending.get('phase')=='selected_verified' and pending.get('kind')=='attribute'):
            try:
                trial=CraftRecord(**{**values,'missions':[Option(**row) for row in values.get('missions',[])]})
                candidates=[Option(**row) for row in pending.get('candidates',[])]
                reconciled=reconcile_attribute(trial,candidates,current.visible_missions(),old_step,new_step)
                if reconciled:values=asdict(trial);trace['manual_attribute_reconciled']=True
            except (SafetyStop,ValueError,TypeError,KeyError,AttributeError):pass
        linked=bool(ledger and ((data.get('safe') is True and (same or inactive or reconciled or observed_etch_map))
                               or (selection_revalidated and same) or rest_hub_revalidated or opened_refresh_revalidated
                               or terminal_core_revalidated or etch_entry_revalidated))
        if linked:
            try:record=CraftRecord(**{**values,'missions':[Option(**row) for row in values.get('missions',[])]})
            except (TypeError,ValueError,KeyError,AttributeError):
                linked=False;trace['historical_failure']='历史记录结构不可用'
        if linked:
            trace.update(resume_mode='historical_restore',historical_association='pass')
            runner.normal=ledger['normal'];runner.quick=ledger['quick']
            runner.unknown_completion=ledger.get('unknown',0)
            runner.run_id=ledger['run_id'];runner.completion_source=ledger['source']
            runner.continuation_scope=ledger.get('continuation_scope','batch')
            runner.historical_completed=None if ledger['version'] in (2,3) else runner.normal+runner.quick
            runner.session_delta_completed=ledger.get('session_delta_completed',0)
            runner.epoch_baseline=deepcopy(ledger.get('baseline'))
            runner.checkpoint_session_id=data.get('checkpoint_session_id') or runner.checkpoint_session_id
            runner.hub_baseline=data.get('hub_baseline');runner.rebaseline=data.get('rebaseline')
            runner.module_active=ledger['module_active']
            runner.record=record
            if terminal_core_revalidated:runner.record.commit('core_skill',Option(**data['pending_action']['option']))
            runner.fast=data.get('fast') is True;runner.refresh_state=data.get('refresh_state')
            origin=data.get('normal_origin_known')
            runner.normal_origin_known=(origin if type(origin) is bool else
                'normal_origin_known' not in data and ledger['version']==1 and data.get('safe') is True
                and runner.fast is False)
            runner.pending_action=data.get('pending_action');runner.completion_kind=data.get('completion_kind')
            if selection_revalidated or rest_hub_revalidated or opened_refresh_revalidated or terminal_core_revalidated or etch_entry_revalidated:
                runner.uncertain=False;runner.pending_action=None
            if reconciled:runner.pending_action=None;runner.refresh_state=None
            runner.etching_pending=data.get('etching_pending',False);runner.etch_result_seen=data.get('etch_result_seen',False)
            runner.hub_step=current.progress() if current.kind=='hub' and hasattr(current,'progress') else data.get('hub_step')
            runner.acceptance_evidence.restore(data.get('acceptance_context'),linked=True)
        else:
            from src.crafting_local_runtime import guard_unlinked_history
            guard_unlinked_history(runner,data,values,current)
            # A zero delta is local to this Continue, never a fabricated old completion total.
            runner.continuation_scope='current_module_only';runner.historical_completed=None
            runner.session_delta_completed=0;runner.normal=runner.quick=runner.unknown_completion=0
            runner.run_id=str(uuid4());runner.checkpoint_session_id=str(uuid4())
            runner.completion_source='page_driven_current';runner.module_active=True
            runner.record=CraftRecord(complete=False,boosts_complete=False,missions_complete=False,source='page_driven_current')
            runner.pending_action=None;runner.refresh_state=None;runner.fast=False
            runner.normal_origin_known=False
            runner.completion_kind=None;runner.etching_pending=False;runner.etch_result_seen=False
            runner.hub_baseline=None;runner.epoch_baseline=None
            runner.rebaseline=_archive_previous(runner,data)
            refresh=data.get('refresh_state') or {}
            if refresh.get('phase') in ('confirming','uncertain') and current.kind==refresh.get('kind'):
                runner.refresh_state={'kind':current.kind,'phase':'uncertain','resolution':'unknown'}
            _optional_hub_checkpoint(runner,current)
            runner.acceptance_evidence.restore(None,linked=False)
        if current.kind=='map' and not linked:
            trace['blocked_action']='infer_completion_from_map'
            raise SafetyStop('当前是研发地图，缺少本件结算来源；不能猜完成或启动新实验')
        if current.kind=='quick_dialog' and not linked:
            trace['blocked_action']='quick_finish'
            raise SafetyStop('快速结束弹窗缺少当前决策来源，不能自动确认')
        if current.kind=='refresh_confirm' and opened_refresh_revalidated:
            trace['refresh_recovery']='source_bound_opened_ready_for_original_confirm_refresh'
        elif current.kind=='refresh_confirm':
            if data.get('safe') is True and (data.get('refresh_state') or {}).get('phase')=='opened':
                trace['blocked_action']='unproven_opened_refresh'
                raise SafetyStop('已打开刷新弹窗的来源证据不完整，未取消、确认或再次点击底层刷新')
            # Cancelling a clear dialog is reversible. Never resume an unknown resource spend.
            runner.accounting_trusted=True
            runner.checkpoint_action()
            runner.button('refresh_confirm','取消',lambda s:s.kind in choices,(630,750,1410,850))
            runner.refresh_state={'kind':runner.screen().kind,'phase':'uncertain'}
            trace['refresh_recovery']='cancelled_without_replaying_confirmation'
        runner.accounting_trusted=True
        from src.crafting_local_runtime import restore_guard
        restore_guard(runner)
        if runner.module_active and runner.continuation_scope=='batch' and runner.normal+runner.quick>=runner.target:
            raise SafetyStop('账本已达目标但仍标记活动模组，禁止额外制作')
        trace.update(completion_history_known=runner.historical_completed is not None,
                     continuation_scope=runner.continuation_scope,pending_action=runner.pending_action)
        runner.f.emit('craft_resume_validation',**trace)
        runner.publish('已接管当前制作页面；历史未知字段仅在影响具体动作时停止')
        runner.checkpoint_action()
    except Exception as error:
        trace['failed_check']=str(error);runner.f.emit('craft_resume_validation',**trace)
        raise
