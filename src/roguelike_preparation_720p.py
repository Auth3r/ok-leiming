"""Read-only START preparation adapter. No runner, OCR, policy or input dispatch."""
from src.app_paths import APP_ROOT
from pathlib import Path

from src.navigation_720p import Navigation720p
from src.recognition_geometry import reference_bbox_to_client
from src.roguelike_policy import Observation, RogueState as S
from src.roguelike_vision import RogueTemplates, RoguelikeScreen


class PreparationScreen(RoguelikeScreen):
    def classify(self):
        # Reuse the real template matcher and current-pixel button locator only.
        for key in self.templates.specs:
            self.find(key)
        if not self.has('start_title','prep_difficulty_label'):
            return Observation()
        label=self.find('prep_start_label')
        button=self.bright_action('start','开始挑战',(.66,.80,.99,.98),.12,3) if label else None
        if button and not (button.area[0] <= (label[0]+label[2])/2 <= button.area[2]
                           and button.area[1] <= (label[1]+label[3])/2 <= button.area[3]):
            button=None
        return Observation(S.START,event='idle' if button else 'unknown',
                           actions=(button,) if button else ())


class RoguelikePreparation720p:
    def __init__(self, root=None, navigation=None):
        root=Path(root) if root is not None else APP_ROOT/'templates/roguelike_720p'
        self.root=root
        self.revision=(root/'manifest.json').stat().st_mtime_ns
        self.navigation=navigation if navigation is not None else Navigation720p()
        self.templates=RogueTemplates(root)
        required={'start_title','prep_difficulty_label','prep_mode','prep_start_label','prep_change_difficulty'}
        if not required.issubset(self.templates.specs):
            raise ValueError('Missing preparation identity/control templates')
        for key,spec in self.templates.specs.items():
            if spec.get('native_client_size') != [1280,720]:
                raise ValueError('Preparation requires native 1280x720 assets')
            roi=reference_bbox_to_client(spec['reference_roi'],(1280,720),reference_size=spec['reference_size'])
            image=self.templates.images[key]
            if image.std()<1 or image.shape[1]>roi[2]-roi[0] or image.shape[0]>roi[3]-roi[1]:
                raise ValueError('Preparation template outside ROI or invalid')
            if not 0<spec['threshold']<=1:
                raise ValueError('Invalid preparation threshold')

    def observe(self, packet, *, hwnd, pid):
        result=self.navigation.observe(packet,hwnd=hwnd,pid=pid)
        screen=PreparationScreen(None,packet.image,self.templates)
        details={}
        for name,spec in self.templates.specs.items():
            match=screen.matches[name+'None']
            details[name]={'target':name,'score':float(match['confidence']),
                'threshold':spec['threshold'],'roi':list(match['roi']),
                'bbox':list(screen.find(name)) if screen.find(name) else None,
                'matched':bool(match['matched']),'label':spec.get('label')}
        result['preparation_matches']=details
        result['preparation']={'rogue_state':str(screen.observation.state),
            'phase':'unknown','challenge_in_progress':None,'next_action_starts_challenge':None,
            'buttons':{},'current_mode':None,'input_allowed':False,
            'overlay':'known_overlay' if result['state']=='BLOCKED_BY_OVERLAY' else 'not_detected',
            'coverage':'Recorded idle START only; ongoing challenges and unknown popups are not inferred'}
        if result['state']=='BLOCKED_BY_OVERLAY':
            result['preparation']['rogue_state']=str(S.UNKNOWN)
            return result
        if screen.observation.state==S.START:
            if result['state']!='UNKNOWN':
                result['state']='UNKNOWN'  # Conflicting page evidence is never actionable.
                result['preparation']['rogue_state']=str(S.UNKNOWN)
                return result
            result['state']='ROGUE_PREPARATION_CONFIRMED'
            result['entry_bbox']=None
            info=result['preparation']
            info['phase']=screen.observation.event
            action=screen.observation.action('start')
            info['challenge_in_progress']=False if action else None
            info['next_action_starts_challenge']=True if details['prep_start_label']['matched'] else None
            info['buttons']['start']={'label':'开始挑战','bbox':list(action.area) if action else None,
                                      'available':action is not None,'input_allowed':False}
            change=details['prep_change_difficulty']
            info['buttons']['change_difficulty']={'label':change['label'],'bbox':change['bbox'],
                                                  'available':change['matched'],'input_allowed':False}
            if details['prep_mode']['matched']:
                info['current_mode']=details['prep_mode']['label']
            elif details.get('prep_normal_mode',{}).get('matched'):
                info['current_mode']=details['prep_normal_mode']['label']
        return result
