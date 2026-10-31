# PyInstaller onedir, Windows x64. All first-party resources are staged by the
# build script from a positive allowlist, never from configs/logs/cache.
from pathlib import Path
import json
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs, copy_metadata

root = Path(SPECPATH).parent
build_work = Path(os.environ.get('LEIMING_BUILD_WORK_DIR', str(root/'output'/'release-build')))
datas = []
binaries = []
source_files = json.loads(os.environ['LEIMING_TRACKED_SOURCE_FILES'])
hidden = ['src.' + Path(name).stem for name in source_files
          if name.endswith('.py') and Path(name).stem != '__init__']
hidden += ['tools.validate_module_navigation', 'tools.season_live_test', 'tools.roguelike_gui_acceptance']
hidden += ['tools.daily_gui_acceptance']
for package in ('ok', 'qfluentwidgets', 'qframelesswindow', 'onnxocr', 'opencc', 'pyappify'):
    hidden += collect_submodules(package, on_error='warn once')
    datas += collect_data_files(package, excludes=['fonts/**'] if package == 'onnxocr' else [])
    binaries += collect_dynamic_libs(package)
datas += collect_data_files('openvino', includes=['libs/**'])
binaries += collect_dynamic_libs('openvino')
hidden += collect_submodules('openvino')
for distribution in ('ok-script', 'onnxocr-ppocrv5', 'pyappify', 'PySide6-Essentials',
                     'PySide6-Fluent-Widgets', 'PySideSix-Frameless-Window', 'openvino', 'opencc'):
    datas += copy_metadata(distribution)
a = Analysis([str(root/'main.py')], pathex=[str(root)], binaries=binaries, datas=datas,
             hiddenimports=hidden,
             excludes=['pytest', 'pip', 'setuptools', 'tkinter', 'matplotlib', 'onnxruntime',
                       'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets'],
             noarchive=False)
pyz = PYZ(a.pure)
# Qt uses Windows' system ICU ABI. A similarly named developer Poppler ICU
# exposes version-suffixed functions and must never shadow that system library.
a.binaries = [item for item in a.binaries if item[0].lower() not in ('icuuc.dll', 'icudt78.dll')]
for _, source, _ in a.binaries:
    path = str(source).lower().replace('\\', '/')
    if '/.cache/codex-runtimes/' in path or '/.codex/' in path:
        raise RuntimeError('Developer tooling DLL entered the release: ' + source)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Leiming-Automation',
          debug=False, strip=False, upx=False, console=False,
          icon=str(build_work/'icon.ico'),
          version=str(build_work/'version-info.txt'),
          manifest=str(root/'tools'/'windows.manifest'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Leiming-Automation')
