"""Real Windows GUI release acceptance. No task or native game input is allowed."""
import ctypes
import json
import os
import sys
import traceback
from pathlib import Path


def dispatch_replay(engine, folder):
    """Use optional external real frames, never bundle private test screenshots."""
    import cv2
    import numpy as np
    from types import SimpleNamespace
    from ok import Box
    from ok.feature.FeatureSet import FeatureSet
    from src.app_paths import APP_ROOT
    from src.resolution import ReferenceTask
    from src.vision import Vision
    from src.dispatch_screen import DispatchScreen
    features = FeatureSet(False, str(APP_ROOT/'templates/coco_annotations.json'), .002, .002, .9)
    class FrameTask:
        def next_frame(self): return self.frame
        def find_one(self, name, **kwargs):
            found = features.find_feature(kwargs.pop('frame'), name, limit=1, **kwargs)
            return found[0] if found else None
        def ocr(self, *, frame, x=0, y=0, to_x=1, to_y=1, threshold=.9, **kwargs):
            h,w=frame.shape[:2];l,t,r,b=map(round,(x*w,y*h,to_x*w,to_y*h))
            boxes=[]
            for points,(text,confidence) in engine.ocr(frame[t:b,l:r])[0]:
                if confidence < threshold: continue
                points=np.array(points);a,c=points.min(axis=0);d,e=points.max(axis=0)
                boxes.append(Box(a+l,c+t,d-a,e-c,confidence,text))
            return boxes
    results=[]
    for number in (1,4,5):
        raw=FrameTask();raw.frame=cv2.imdecode(np.fromfile(folder/f'image{number}.png',np.uint8),1)
        task=ReferenceTask(raw,{'resolution_width':0,'resolution_height':0});task.next_frame()
        screen=DispatchScreen(SimpleNamespace(task=task,vision=Vision(task,.9)))
        feedback=screen.feedback(False)
        assert screen.requirements.same_tactic == (4 if number==1 else 0)
        assert screen.requirements.profession_types == (0 if number==1 else 2)
        assert feedback['allowed'] == (number==5)
        results.append({'image':number,'mission':screen.mission_identity(),
                        'selected':screen.selected,'feedback':feedback})
    return results


def check_update_controls(main, directory):
    """Click real compiled widgets; record real network separately from HTTP fixtures."""
    import io
    import time
    import urllib.error
    from packaging.version import Version
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from src.version import APP_VERSION
    from src.update_client import RELEASES_URL
    directory.mkdir(parents=True, exist_ok=True)
    main.switchTo(main.about_tab)
    card = main.about_tab.update_card
    assert card is not None
    current = Version(APP_VERSION)
    channel = {'a': 'alpha', 'b': 'beta', 'rc': 'rc'}
    next_version = (f'v{".".join(map(str, current.release))}-{channel[current.pre[0]]}.{current.pre[1]+1}'
                    if current.pre else f'v{current.major}.{current.minor}.{current.micro+1}')
    def release(version):
        return {'tag_name': version, 'draft': False, 'prerelease': Version(version).is_prerelease,
                'published_at': '2026-10-28T00:00:00Z', 'body': 'SIMULATED HTTP acceptance data', 'assets': []}
    scenarios = [('real-github', None, 'record'),
                 ('simulated-new-beta', [release(next_version)], 'new'),
                 ('simulated-current', [release(APP_VERSION)], 'same'),
                 ('simulated-older', [release('v0.9.0')], 'older'),
                 ('simulated-empty', [], 'empty'),
                 ('simulated-invalid', [{'tag_name': 'not-a-version'}], 'error'),
                 ('simulated-network-error', urllib.error.URLError('SIMULATED offline'), 'error')]
    results = []
    backend = card.pyappify_module.backend
    original_opener = backend._opener
    class SimulatedHTTPS:
        def __init__(self, response):
            self.response = response
        def open(self, request, timeout):
            if isinstance(self.response, Exception):
                raise self.response
            return io.BytesIO(json.dumps(self.response).encode())
    try:
        for name, response, expected in scenarios:
            backend._opener = original_opener if name == 'real-github' else SimulatedHTTPS(response)
            QApplication.processEvents()
            assert main.grab().save(str(directory / (name + '-before.png')))
            QTest.mouseClick(card.check_button, Qt.MouseButton.LeftButton)
            deadline = time.monotonic() + (40 if name == 'real-github' else 5)
            while card._busy and time.monotonic() < deadline:
                QTest.qWait(10)
            QTest.qWait(20)
            assert not card._busy and card.check_button.isEnabled()
            if expected == 'new':
                assert card.update_button.isEnabled()
                assert card.update_button.text() == card.tr('Update')
            elif expected != 'record':
                assert not card.update_button.isEnabled()
            if expected == 'error':
                assert card.status_label.styleSheet()
            if expected == 'same':
                assert card.update_button.text() == '当前版本'
            if expected == 'older':
                assert '不降级' in card.update_button.text()
            assert main.grab().save(str(directory / (name + '-after.png')))
            results.append({'case': name, 'status': card.status_label.text(),
                            'versions': [item['version'] for item in card.versions],
                            'source': RELEASES_URL if name == 'real-github' else 'simulated HTTPS bodies',
                            'network_succeeded': not bool(card.status_label.styleSheet()) if name == 'real-github' else None,
                            'update_enabled': card.update_button.isEnabled(), 'passed': True})
    finally:
        backend._opener = original_opener
        backend._releases = {}  # Never leave acceptance fixtures installable after the check.
    return results


def run(config):
    from src.app_paths import APP_ROOT, DATA_ROOT
    from src.interaction import GameForegroundInteraction
    from src.runtime_identity import get_runtime_identity, log_runtime_start
    from src.version import APP_VERSION
    from PySide6.QtCore import QTimer, QSettings
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QPixmap
    from ok import OK
    import src.module_ui as module_ui

    def forbidden(*args, **kwargs):
        raise AssertionError('Release smoke must never issue game input')
    for method in ('click', 'move', 'swipe', 'scroll', 'mouse_down', 'mouse_up',
                   'send_key', 'send_key_down', 'send_key_up', 'on_run', 'activate_for_task'):
        setattr(GameForegroundInteraction, method, forbidden)
    directory = DATA_ROOT / 'logs' / 'release-smoke'
    directory.mkdir(parents=True, exist_ok=True)
    # HUD preferences are isolated from the tester's registry preferences.
    original_hud = module_ui.SkillRecordWindow
    class IsolatedHUD(original_hud):
        def __init__(self, parent=None, task=None, settings=None):
            super().__init__(parent, task, QSettings(str(directory/'hud.ini'), QSettings.IniFormat))
    module_ui.SkillRecordWindow = IsolatedHUD
    config['check_mutex'] = False
    app = OK(config)
    log_runtime_start()
    app.app.show_main_window()
    result = {'status': 'running', 'runtime_identity': get_runtime_identity().as_dict(),
              'platform': QApplication.platformName(), 'native_input_forbidden': True}
    errors = []

    def finish():
        try:
            main = app.app.main_window
            assert not app.init_error
            assert main.isVisible() and QApplication.platformName() == 'windows'
            assert APP_VERSION in main.windowTitle()
            assert len(app.task_executor.onetime_tasks) == 15
            assert not any(task.enabled for task in app.task_executor.onetime_tasks)
            assert app.task_executor.current_task is None
            assert app.config['windows']['start_exe'] is False
            from src.settings import load_settings
            result['effective_config_before'] = load_settings()
            daily_defaults={key:value for key,value in result['effective_config_before'].items()
                            if key.startswith('execute_')}
            assert len(daily_defaults)==7 and all(value is True for value in daily_defaults.values())
            result['daily_defaults_enabled']=daily_defaults
            assert not QPixmap(str(APP_ROOT/'icons/icon.png')).isNull()
            awareness = ctypes.c_int(-1)
            assert ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness)) == 0
            assert awareness.value == 2
            main.switchTo(main.start_tab)
            QApplication.processEvents()
            main.grab().save(str(directory/'start.png'))
            native = main.start_tab.runtime_720p_panel
            assert native.control is None and native.worker is None
            assert native.start_button.text() == '开始'
            assert native.stop_button.text() == '停止'
            assert not native.stop_button.isEnabled()
            module = next(tab for tab in main.grouped_task_tabs if tab.group_name == '模组制作')
            panel = module.module_strategy_panel
            profiles = [panel.profile.itemText(i) for i in range(panel.profile.count())]
            assert profiles == ['旧地联合', '军工', '一致', '先攻']
            original = panel.profile.currentText()
            main.switchTo(module)
            for profile in profiles:
                panel.profile.setCurrentText(profile)
                QApplication.processEvents()
                assert profile in panel.strategy_note.text()
            panel.profile.setCurrentText(original)
            panel.record.show()
            QApplication.processEvents()
            assert panel.record.isVisible()
            assert len([w for w in QApplication.topLevelWidgets() if isinstance(w, original_hud)]) == 1
            panel.record.grab().save(str(directory/'module-hud.png'))
            panel.record.hide()
            main.grab().save(str(directory/'module.png'))
            rogue = next(tab for tab in main.grouped_task_tabs if tab.group_name == '肉鸽')
            assert [task.only for task in rogue.tasks] == ['ROGUELIKE', 'ROGUELIKE_RESUME']
            main.switchTo(rogue)
            QApplication.processEvents()
            from ok.ui.qt.tasks.TaskCard import TaskCard
            card = next(card for card in main.findChildren(TaskCard) if card.task is rogue.tasks[0])
            for state, label in (('RUNNING', '暂停'), ('PAUSED', '继续'), ('IDLE', '开始')):
                card.control_bar.render(state, reason='发布包控件状态检查')
                assert card.pause_button.text() == label
                assert card.stop_button.text() == '停止'
            card.update_buttons()
            main.grab().save(str(directory/'roguelike.png'))
            from src.roguelike_hud import RoguelikeHud
            hud = RoguelikeHud()
            hud.prepare_native()
            assert hud.native_safe
            hud.show()
            QApplication.processEvents()
            assert hud.isVisible()
            hud.grab().save(str(directory/'roguelike-hud.png'))
            hud.close()
            main.switchTo(main.onetime_tab)
            QApplication.processEvents()
            main.grab().save(str(directory/'daily.png'))
            # UI edits really persist. A second process checks the saved value.
            owner = app.global_config.get_config('ModuleCrafting')
            previous = owner['制作数量']
            if '--smoke-restart' in sys.argv:
                assert previous == 7, previous
                panel.count.setValue(1)
            else:
                panel.count.setValue(7)
            main.switchTo(main.start_tab)
            # Actual bundled OCR models and native DLLs, on synthetic pixels only.
            from PIL import Image, ImageDraw, ImageFont
            import numpy as np
            import cv2
            image = Image.new('RGB', (640, 120), 'white')
            fonts = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Fonts'
            font_path = next((fonts / name for name in ('msyh.ttc', 'arial.ttf')
                              if (fonts / name).is_file()), None)
            font = ImageFont.truetype(str(font_path), 48) if font_path else ImageFont.load_default(size=48)
            ImageDraw.Draw(image).text((24, 20), '镭明闪击 123456', font=font, fill='black')
            ocr = app.task_executor.ocr_lib()
            recognized = ocr.ocr(cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR))[0]
            text = ''.join(row[1][0] for row in recognized)
            assert '123456' in text, text
            resources=list((APP_ROOT/'templates').rglob('*.png'))
            assert resources and all(cv2.imdecode(np.fromfile(p,np.uint8),1) is not None for p in resources)
            result['template_images_loaded']=len(resources)
            if '--smoke-fixtures' in sys.argv:
                folder=Path(sys.argv[sys.argv.index('--smoke-fixtures')+1])
                result['real_dispatch_replay']=dispatch_replay(ocr,folder)
            if '--smoke-game' in sys.argv:
                from src.diagnostics import collect_status, create_report
                from src.client_geometry import client_input_to_screen
                status,frame=collect_status(app.task_executor.device_manager,app.task_executor,load_settings())
                assert frame is not None, status
                assert tuple(frame.shape[1::-1])==(1280,720), status
                hwnd=app.task_executor.device_manager.hwnd_window.hwnd
                dpi=int(ctypes.windll.user32.GetDpiForWindow(hwnd))
                assert dpi>0, 'Game window DPI is unavailable'
                result['game_readonly']={'status':status,'center_screen':client_input_to_screen(hwnd,640,360),
                                         'dpi':dpi,'size':[1280,720],'input_count':0}
                result['problem_report']=str(create_report(DATA_ROOT,status,load_settings()))
            from src.vision import Vision
            from types import SimpleNamespace
            vision = Vision(SimpleNamespace(), .9)
            assert vision.images and all(value is not None for value in vision.images.values())
            from src.navigation_720p import Navigation720p
            from src.roguelike_vision import RogueTemplates
            Navigation720p()
            RogueTemplates()
            assert not any(task.enabled for task in app.task_executor.onetime_tasks)
            result['update_controls'] = check_update_controls(
                main, directory / ('update-restart' if '--smoke-restart' in sys.argv else 'update-first'))
            result.update(status='passed', window_title=main.windowTitle(), profiles=profiles,
                          dpi_awareness=awareness.value, ocr_text=text,
                          template_count=len(vision.images), config_before=previous,
                          restart='--smoke-restart' in sys.argv, no_tasks_started=True,
                          controls='real_widgets_with_simulated_states', native_hud_safe=True)
        except Exception:
            errors.append(traceback.format_exc())
            result.update(status='failed', error=errors[-1])
        finally:
            (directory/('restart.json' if '--smoke-restart' in sys.argv else 'first.json')).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            app.quit()
    QTimer.singleShot(5000, finish)
    QTimer.singleShot(90000, app.quit)
    app.app.exec()
    if errors or result['status'] != 'passed':
        raise RuntimeError(errors[-1] if errors else 'Release smoke timed out')
