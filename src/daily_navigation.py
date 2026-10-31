"""Season labels are evidence, while verified parent/controls identify a route."""
import json
import re
from src.app_paths import APP_ROOT as ROOT


def seasonal_entry(vision, region):
    # A title alone, even an exact alias, cannot authorize a navigation click.
    if not vision.page('MAIN_MENU'):
        return None
    anchor=vision.find('seasonal_activity_anchor')
    if not anchor: return None
    l,t,r,b=region
    labels=vision.task.ocr(x=l/3840,y=t/2160,to_x=r/3840,to_y=b/2160,
                           frame=vision.task.frame,threshold=.93)
    aliases=json.loads((ROOT/'templates/daily_aliases.json').read_text(encoding='utf-8'))['seasonal_activity']
    candidates=[]
    for label in labels:
        text=re.sub(r'\s+','',label.name)
        x,y=label.x+label.width/2,label.y+label.height/2
        if (not l<=x<r or not t<=y<b or label.confidence<.93): continue
        # Header sits immediately above the calibrated profession/control strip.
        if not abs(x-(anchor.x+anchor.width/2))<330 or not 0<anchor.y-y<180: continue
        if text in aliases or (re.fullmatch(r'[\u4e00-\u9fff]{2,12}',text) and
                               text not in ('导航','理财基金','黑盾委托','待办')):
            candidates.append((label,text in aliases))
    if len(candidates)!=1:return None
    label,known=candidates[0]
    vision.results['seasonal_entry_evidence']={'name':'seasonal_entry_evidence','roi':region,
        'box':[label.x,label.y,label.width,label.height],'confidence':float(label.confidence),
        'threshold':.93,'matched':True,'alias':label.name,'known_alias':known,
        'anchor':'seasonal_activity_anchor','parent':'MAIN_MENU','requires_destination':'DARK_HUB'}
    return label


def seasonal_hub_control(vision,target):
    """Current icons and progress counters prove the established feature route."""
    if not vision.has('home','seasonal_research_anchor','seasonal_trial_anchor'):return None
    name,maximum=('seasonal_research_anchor',10) if target=='dark_hub_research' else ('seasonal_trial_anchor',60)
    anchor=vision.find(name)
    l,t=anchor.x+anchor.width, max(0,anchor.y-30)
    r,b=min(3840,l+900),min(2160,anchor.y+anchor.height+130)
    labels=vision.task.ocr(x=l/3840,y=t/2160,to_x=r/3840,to_y=b/2160,
                           frame=vision.task.frame,threshold=.93)
    counters=[]
    for label in labels:
        match=re.fullmatch(r'进程(\d{1,2})[/／](\d{2})',re.sub(r'\s+','',label.name))
        cx,cy=label.x+label.width/2,label.y+label.height/2
        if match and label.confidence>=.93 and l<=cx<r and t<=cy<b:
            current,limit=map(int,match.groups())
            if limit==maximum and 0<=current<=limit:counters.append((label,current))
    if len(counters)!=1:return None
    vision.results['seasonal_hub_'+target]={'name':target,'matched':True,'threshold':.93,
        'confidence':min(float(anchor.confidence),float(counters[0][0].confidence)),
        'anchor':name,'box':[anchor.x,anchor.y,anchor.width,anchor.height],
        'counter':counters[0][1],'maximum':maximum,'parent':'DARK_HUB',
        'requires_destination':'DARK_MAP' if target=='dark_hub_research' else 'DARK_STAGE'}
    return anchor
