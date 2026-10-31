"""No-consumption live navigation acceptance on 720P or 4K physical client pixels.

Refuses an active crafting screen. Opens the list, scrolls to first-strike bottom,
checks selected identity, confirms the embryo to preparation and repeats twice.
This command exposes no crafting start, refresh, dismantle or abandonment option.
With --preview-only it instead repeats discovery/preview/return three times and
rejects all module confirmations, including replacement of an existing module.
Run through the existing ASCII-path launcher/alias when required by OK.
"""
import argparse
import sys
from pathlib import Path
from contextlib import contextmanager

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.app_paths import APP_ROOT as ROOT


@contextmanager
def permitted_navigation(flow, *, preview_only=False):
    """Fail at the production target boundary before an unexpected input."""
    from tools.season_live_test import AcceptanceStop
    original=flow.click
    allowed={('MAIN_MENU','main_dark'),('DARK_HUB','dark_hub_research'),
             ('DARK_MAP','module_entry'),('MODULE_RESEARCH','module_choose'),
             ('MODULE_RESEARCH','module_replace'),('MODULE_SELECTOR','module_confirm'),
             ('MODULE_SELECTOR','back'),('BLUEPRINT_SELECTOR','back'),
             ('NOTICE','notice_prompt')}
    allowed.update(('MODULE_SELECTOR','module_name_'+name) for name in ('先攻','军工','一致'))
    if preview_only:
        allowed.remove(('MODULE_SELECTOR','module_confirm'))
    def click(page,target,*args,**kwargs):
        if (page,target) not in allowed:
            raise AcceptanceStop('无消耗验收拒绝操作：'+str(page)+' / '+str(target))
        if target=='module_confirm' and not callable(kwargs.get('precondition')):
            raise AcceptanceStop('确认胚子缺少即时身份复核，未发送输入。')
        if preview_only and target.startswith('module_name_'):
            flow.snapshot('p0_preview_before_selection')
        return original(page,target,*args,**kwargs)
    flow.click=click
    try:yield
    finally:flow.click=original


def preview_navigation(flow, invocation, profiles, *, repetitions=3):
    """Production discovery/preview/detail verification; confirmation is forbidden."""
    from src.module_crafting import ModulePreparation
    from tools.season_live_test import AcceptanceStop, atomic_json, refuse_active
    flow.refresh()
    refuse_active(flow)
    for selector in ('MODULE_SELECTOR', 'BLUEPRINT_SELECTOR'):
        if flow.vision.page(selector):
            flow.click(selector, 'back', lambda: flow.vision.page('MODULE_RESEARCH'),
                       '预览验收关闭已有列表')
            break
    ModulePreparation(flow).navigate()
    refuse_active(flow)
    rows = []
    for profile in profiles:
        flow.cfg['制作配置'] = profile
        prep = ModulePreparation(flow)
        for repetition in range(1, repetitions + 1):
            flow.require('MODULE_RESEARCH')
            refuse_active(flow)
            proof = prep.select_named_module(probe_only=True)
            if not proof or proof.get('identity_verified') is not True:
                raise AcceptanceStop('预览未核对详情身份，验收失败')
            flow.snapshot('p0_preview_identity_'+profile+'_'+str(repetition))
            row = dict(proof, repetition=repetition, confirmation_sent=False,
                       logdir=str(flow.logdir), returned_to_research=False)
            rows.append(row)
            atomic_json(invocation / 'preview-navigation.json', {'status':'incomplete','rows':rows})
            flow.click('MODULE_SELECTOR', 'back', lambda: flow.vision.page('MODULE_RESEARCH'),
                       '预览验收返回研发准备页')
            row['returned_to_research'] = True
            flow.snapshot('p0_preview_returned_'+profile+'_'+str(repetition))
    result = {'status':'preview_verified', 'rows':rows, 'confirmation_sent':False,
              'scope':'real navigation and selected-detail identity; no embryo confirmation'}
    atomic_json(invocation / 'preview-navigation.json', result)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resolution',choices=('auto','720p','4k'),default='auto')
    parser.add_argument('--profile',choices=('先攻','军工','一致'),default='先攻')
    parser.add_argument('--hwnd',type=int)
    parser.add_argument('--preview-only',action='store_true',
                        help='三次重新打开并预览；禁止确认、替换及消耗')
    args=parser.parse_args(argv)
    from tools import season_live_test as live
    original=live.navigate_profiles
    original_root,original_session=live.ROOT,live.SESSION_ROOT
    live.ROOT=ROOT
    live.SESSION_ROOT=ROOT/'logs'/'season-live'
    def guarded(flow,*values,**kwargs):
        with permitted_navigation(flow, preview_only=args.preview_only):
            if args.preview_only:
                invocation, profiles, _ = values
                return preview_navigation(flow, invocation, profiles)
            return original(flow,*values,**kwargs)
    live.navigate_profiles=guarded
    try:
        values=['--phase','nav','--profile',args.profile,
                '--navigation-resolution',args.resolution]
        if not args.preview_only:values.append('--confirm-preparation')
        if args.hwnd:values.extend(('--hwnd',str(args.hwnd)))
        return live.main(values)
    finally:
        live.navigate_profiles=original
        live.ROOT,live.SESSION_ROOT=original_root,original_session


if __name__=='__main__':raise SystemExit(main())
