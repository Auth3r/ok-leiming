"""Event evidence from existing observations; never captures, OCRs or authorizes input."""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class CraftAcceptance:
    def __init__(self,runner):
        self.runner=runner;self.segment_id=str(uuid4());self.module_index=None
        self.sequence=0;self.last={};self.images={};self.save_error=None;self.previous_opportunities={}

    def context(self):
        r=self.runner
        completed=r.normal+r.quick+getattr(r,'unknown_completion',0)
        index=self.module_index or (completed+1 if r.module_active else max(1,completed))
        return {'run_id':r.run_id,'module_index':index,'module_key':f'{r.run_id}:module:{index}',
                'segment_id':self.segment_id,'profile':r.policy.name}

    def start_module(self):
        r=self.runner
        self.module_index=r.normal+r.quick+getattr(r,'unknown_completion',0)+1
        self.last.clear();self.previous_opportunities={}

    def restore(self,metadata,linked):
        r=self.runner;completed=r.normal+r.quick+getattr(r,'unknown_completion',0)
        self.module_index=completed+1 if r.module_active else max(1,completed)
        self.last.clear()
        # This is audit linkage only. It never changes the already-validated ledger.
        old=metadata if isinstance(metadata,dict) else {}
        current=self.context()
        valid=bool(linked and old.get('run_id')==current['run_id'] and
                   old.get('module_key')==current['module_key'])
        self.event('restore_link',snapshot=False,linked=linked,
                   parent_evidence_valid=valid,metadata_conflict=bool(old and linked and not valid),
                   parent_segment_id=old.get('segment_id') if valid else None,
                   parent_module_key=old.get('module_key') if valid else None,
                   association=getattr(r,'resume_trace',{}).get('historical_association'))

    @staticmethod
    def _json(value):
        return json.dumps(value,ensure_ascii=False,sort_keys=True,default=str)

    @staticmethod
    def _semantic(value):
        if isinstance(value,dict):
            return {k:CraftAcceptance._semantic(v) for k,v in value.items()
                    if k not in ('source','origin_source')}
        if isinstance(value,list):return [CraftAcceptance._semantic(v) for v in value]
        return value

    def _opportunity_changes(self,state):
        now={row['id']:(bucket,row.get('consumed',False))
             for bucket in ('confirmed','possible','absent') for row in state.get(bucket,()) if 'id' in row}
        changes=[]
        for bucket in ('confirmed','possible','absent'):
            for row in state.get(bucket,()):
                key=row.get('id');current=now.get(key)
                if current!=self.previous_opportunities.get(key):
                    changes.append({'id':key,'from':self.previous_opportunities.get(key),
                        'to':current,'origin_id':row.get('origin_id'),'gain':row.get('gain'),
                        'actual_gain':row.get('actual_gain'),'reason':row.get('reason') or
                            ('verified_confirmation_consumed' if row.get('consumed') else
                             'observed_skill_successor' if row.get('outcome')=='skill' else row.get('phase'))})
        return now,changes

    def event(self,phase,screen=None,**facts):
        try:return self._event(phase,screen,**facts)
        except Exception as error:
            # A failing evidence sink must not mask a safety stop or authorize retries.
            self.save_error=repr(error)
            try:self.runner.f.emit('craft_acceptance_save_failed',phase=phase,error=self.save_error)
            except Exception:pass
            return None

    def _event(self,phase,screen=None,*,options=None,cards=None,snapshot=True,**facts):
        r=self.runner;folder=getattr(r.f,'logdir',None)
        if not isinstance(folder,(str,Path)):return None
        started=time.perf_counter_ns();wall=datetime.now(timezone.utc).isoformat()
        # Read instance fields only: a lazy CraftScreen must not be promoted for logging.
        state=getattr(screen,'__dict__',{}) if screen is not None else {}
        kind=state.get('kind');capture=state.get('capture_provenance') or {}
        current=[]
        for o in options or ():
            current.append({k:getattr(o,k,None) for k in ('index','name','raw_name','module',
                'level','gain','rarity','level_known','rarity_known','area')})
        opportunities=getattr(r.record,'skill_opportunity_state',{})
        opportunity_now,changes=self._opportunity_changes(opportunities)
        visible_gold=re.findall(r'已选橙色词条数量[:：]?(\d{1,2})(?!\d)',
                                re.sub(r'\s+','',state.get('text') or ''))
        visible_gold_count=int(visible_gold[0]) if len(visible_gold)==1 else None
        semantic={'phase':phase,'kind':kind,'hub_step':r.hub_step,'header':state.get('header'),
                  'action_id':getattr(r,'acceptance_action_id',None),
                  'candidates':current,'cards':cards,'skills':dict(r.record.skills),
                  'gold_picks':r.record.gold_picks,'opportunities':self._semantic(opportunities),
                  'perfect_mode':r.record.perfect_mode,'perfect_reason':r.record.perfect_reason,
                  'history_count':len(r.record.history),'pending':r.pending_action,
                  'refresh':r.refresh_state,'uncertain':r.uncertain,
                  'visible_gold_count':visible_gold_count,
                  'facts':self._semantic(facts)}
        # Ignore pixel/epoch animation when deciding whether a new audit event exists.
        # Sources inside opportunity rows do change on genuinely new observed transitions.
        key=self._json(semantic)
        if self.last.get(phase)==key:return None
        row={'version':1,**self.context(),'event_id':str(uuid4()),'phase':phase,
             'action_id':getattr(r,'acceptance_action_id',None),'time_utc':wall,
             'monotonic_ns':started,'hub_step':r.hub_step,
             'page':{'kind':kind,'identity':hashlib.sha256(self._json(
                 (kind,r.hub_step,current or state.get('header'))).encode('utf-8')).hexdigest(),
                 'observation_epoch':state.get('observation_epoch'),
                 'full':not state.get('_lazy_pending',False) and not state.get('_light_conflict',False)
                        and not state.get('_local_only',False),
                 'local_only':state.get('_local_only',False),
                 'local_pixel_witness':state.get('local_pixel_witness'),
                 'raw_text':state.get('text'),'ocr_boxes':[
                     {'text':getattr(b,'name',None),'area':[getattr(b,k,None) for k in ('x','y','width','height')],
                      'confidence':getattr(b,'confidence',None)} for b in state.get('boxes',())],
                 'capture':capture,'metadata_complete':all(type((capture.get('metadata') or {}).get(k)) is int
                    and (capture.get('metadata') or {})[k]>0 for k in ('hwnd','pid'))},
             'candidates':current,'card_slots':cards,
             'record':{'skills':dict(r.record.skills),'gold_picks':r.record.gold_picks,
                       'history_count':len(r.record.history),'history_complete':r.record.complete,
                       'perfect_mode':r.record.perfect_mode,'perfect_reason':r.record.perfect_reason},
             'visible_gold_count':{'value':visible_gold_count,'known':visible_gold_count is not None,
                 'source':'existing_full_frame_ocr_literal_only','matches':visible_gold,
                 'used_for_decision':False},
             'opportunities':opportunities,'opportunity_changes':changes,
             'opportunity_counts':{'used_choices':len(opportunities.get('consumed_ids',())),
                 'current_unconsumed':sum(row.get('kind')=='skill_choice' and not row.get('consumed')
                     for row in opportunities.get('confirmed',())),
                 'future_skill_count':None if not opportunities.get('future_known') else 0,
                 'future_known_reason':'verified_terminal' if opportunities.get('future_known') else
                     'no_verified_complete_indexed_schedule_or_terminal'},
             'blueprint':{'partial':opportunities.get('partial',[]),'complete_schedule_verified':False},
             'pending_phase':(r.pending_action or {}).get('phase'),
             'refresh_phase':(r.refresh_state or {}).get('phase'),'input_outcome_unknown':r.uncertain,
             'facts':facts}
        try:
            folder=Path(folder);image=state.get('native_frame')
            if snapshot and image is not None:
                import cv2
                raw_hash=hashlib.sha256(image.tobytes()).hexdigest()
                # A filename refers to this segment's exact pixels, not a fresh screenshot.
                if raw_hash not in self.images:
                    target=folder/'acceptance-frames';target.mkdir(parents=True,exist_ok=True)
                    relative=Path('acceptance-frames')/(self.segment_id[:8]+'-'+raw_hash[:20]+'.png')
                    ok,encoded=cv2.imencode('.png',image)
                    if not ok:raise ValueError('acceptance image encoding failed')
                    encoded.tofile(str(folder/relative));self.images[raw_hash]={
                        'path':relative.as_posix(),'file_sha256':hashlib.sha256(encoded.tobytes()).hexdigest()}
                row['image']={**self.images[raw_hash],'pixels_sha256':raw_hash,
                              'matches_capture':raw_hash==capture.get('native_frame_sha256')}
            self.sequence+=1;row['sequence']=self.sequence
            row['evidence_prepare_ms']=(time.perf_counter_ns()-started)/1_000_000
            with (folder/'acceptance.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(self._json(row)+'\n')
            self.last[phase]=key
            self.previous_opportunities=opportunity_now
            return row['event_id']
        except (OSError,ValueError,TypeError) as error:
            # Evidence failure is visible, but cannot authorize retries or change a decision.
            self.save_error=repr(error)
            try:r.f.emit('craft_acceptance_save_failed',phase=phase,error=self.save_error)
            except Exception:pass
            return None
