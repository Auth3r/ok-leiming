"""Portable releases locate writable files beside the EXE and fail closed on provenance."""
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from src import app_paths, portable_runtime, runtime_identity
from src.version import APP_VERSION


ROOT = Path(__file__).resolve().parents[1]


class PortablePathTests(unittest.TestCase):
    def test_development_root_uses_loaded_source_instead_of_cwd(self):
        with patch.object(sys, 'frozen', False, create=True), \
             patch.object(sys, 'executable', str(ROOT / 'elsewhere' / 'python.exe')):
            values = runpy.run_path(str(ROOT / 'src/app_paths.py'))
        self.assertEqual(values['APP_ROOT'], ROOT)

    def test_frozen_root_uses_exe_directory_instead_of_internal_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve() / '独立便携目录'
            executable = folder / 'Leiming-Automation.exe'
            with patch.object(sys, 'frozen', True, create=True), \
                 patch.object(sys, 'executable', str(executable)), \
                 patch.object(sys, '_MEIPASS', str(folder / '_internal'), create=True):
                values = runpy.run_path(str(ROOT / 'src/app_paths.py'))
            self.assertEqual(values['APP_ROOT'], folder)

    def test_development_prepare_selects_data_cwd_and_installs_unicode_support(self):
        with patch.object(sys, 'frozen', False, create=True), \
             patch.object(portable_runtime.os, 'chdir') as change, \
             patch.object(app_paths, 'configure_data_root', return_value=ROOT), \
             patch.object(portable_runtime, 'install_unicode_file_access') as install:
            portable_runtime.prepare(ROOT)
        change.assert_called_once_with(ROOT)
        install.assert_called_once()

    def test_frozen_ascii_prepare_uses_bundled_models_without_subst(self):
        parent = ModuleType('onnxocr')
        utils = ModuleType('onnxocr.utils')
        parent.utils = utils
        folder = Path('D:/Portable/Leiming')
        with patch.dict(sys.modules, {'onnxocr': parent, 'onnxocr.utils': utils}), \
             patch.object(sys, 'frozen', True, create=True), \
             patch.object(portable_runtime.os, 'chdir') as change, \
             patch.object(app_paths, 'configure_data_root', return_value=folder), \
             patch.object(portable_runtime, 'install_unicode_file_access'):
            portable_runtime.prepare(folder)
        change.assert_called_once_with(folder)
        self.assertEqual(utils.module_dir, folder / '_internal/onnxocr')

    def test_unicode_prepare_uses_original_resources_and_never_requires_drive_letters(self):
        parent = ModuleType('onnxocr')
        utils = ModuleType('onnxocr.utils')
        parent.utils = utils
        folder = Path('D:/中文便携目录')
        with patch.dict(sys.modules, {'onnxocr': parent, 'onnxocr.utils': utils}), \
             patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'executable', str(folder / 'Leiming-Automation.exe')), \
             patch.object(portable_runtime.os, 'chdir') as change, \
             patch.object(app_paths, 'configure_data_root', return_value=folder), \
             patch.object(portable_runtime, 'install_unicode_file_access'), \
             patch.object(subprocess, 'run', side_effect=AssertionError('No SUBST or shell dependency')):
            portable_runtime.prepare(folder)
            self.assertEqual(sys.executable, str(folder / 'Leiming-Automation.exe'))
        change.assert_called_once_with(folder)
        self.assertEqual(utils.module_dir, folder / '_internal/onnxocr')

    def test_readonly_install_preserves_state_in_per_user_directory_and_retains_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, local = base / 'readonly', base / 'user'
            (root / 'configs').mkdir(parents=True)
            (root / 'settings.json').write_text('{"setting":7}')
            (root / 'config_version.json').write_text('{"config_version":1}')
            (root / 'configs/Tasks.json').write_text('{"count":5}')
            (root / 'logs').mkdir()
            (root / 'logs/daily-dark-progress.json').write_text('{"completed":5}')
            writable = app_paths._writable
            with patch.dict(app_paths.os.environ, {'LOCALAPPDATA': str(local)}), \
                 patch.object(app_paths, 'DATA_ROOT', root), \
                 patch.object(app_paths, '_writable', side_effect=lambda p: False if p.is_relative_to(root) else writable(p)):
                destination = app_paths.configure_data_root(root)
                self.assertTrue(destination.is_relative_to(local))
                self.assertEqual((destination / 'configs/Tasks.json').read_text(), '{"count":5}')
                self.assertEqual((destination / 'logs/daily-dark-progress.json').read_text(), '{"completed":5}')
                (destination / 'configs/Tasks.json').write_text('{"count":3}')
                with patch.object(app_paths, '_writable', return_value=True), \
                     patch.object(app_paths, '_copy_missing', side_effect=AssertionError('Do not rescan history')):
                    self.assertEqual(app_paths.configure_data_root(root), destination)
                self.assertEqual((destination / 'configs/Tasks.json').read_text(), '{"count":3}')
                self.assertEqual((root / 'configs/Tasks.json').read_text(), '{"count":5}')

    def test_writable_portable_install_keeps_existing_local_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(app_paths, 'DATA_ROOT', root):
                self.assertEqual(app_paths.configure_data_root(root), root.resolve())
            self.assertTrue((root / 'configs').is_dir())

    def test_acl_denial_is_bounded_even_when_windows_access_reports_writable(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(app_paths.os, 'access', return_value=True), \
             patch.object(tempfile, 'TMP_MAX', 5), \
             patch.object(app_paths.os, 'open', side_effect=PermissionError('ACL denied')) as raw_open, \
             patch.object(Path, 'open', side_effect=PermissionError('ACL denied')) as path_open:
            self.assertFalse(app_paths._writable(Path(temporary)))
            self.assertEqual(raw_open.call_count + path_open.call_count, 1)

    def test_writable_probe_leaves_no_temporary_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            self.assertTrue(app_paths._writable(directory))
            self.assertEqual(list(directory.iterdir()), [])

    def test_interrupted_state_copy_never_publishes_a_partial_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, destination = base / 'original.json', base / 'data/copied.json'
            source.write_bytes(b'complete original ledger')
            def interrupted_copy(original, partial):
                Path(partial).write_bytes(b'truncated')
                raise OSError('interrupted copy')
            with patch.object(app_paths.shutil, 'copyfile', side_effect=interrupted_copy):
                with self.assertRaisesRegex(OSError, 'interrupted copy'):
                    app_paths._copy_missing(source, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.iterdir()), [])
            app_paths._copy_missing(source, destination)
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_native_history_copies_latest_and_its_explicit_chain_without_rewriting_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, destination = Path(temporary) / 'old', Path(temporary) / 'data'
            native = root / 'logs/roguelike-720p'
            for name in ('000', '001', '002'):
                directory = native / name
                directory.mkdir(parents=True)
                records = ([{'event': 'native_run_progress_restored', 'previous_run': '001'}]
                           if name == '002' else [{'event': 'runtime_started'}])
                (directory / 'events.jsonl').write_text('\n'.join(json.dumps(row) for row in records))
                (directory / 'suspended.png').write_bytes(b'evidence pixels')
            original = (native / '002/events.jsonl').read_bytes()
            destination.mkdir()
            app_paths._preserve_state(root, destination)
            copied = destination / 'logs/roguelike-720p'
            self.assertEqual({p.name for p in copied.iterdir()}, {'001', '002'})
            self.assertEqual((copied / '002/events.jsonl').read_bytes(), original)
            self.assertEqual((native / '002/events.jsonl').read_bytes(), original)
            with patch.object(app_paths, 'DATA_ROOT', destination):
                self.assertEqual(app_paths.resolve_history_file(native / '002/suspended.png', copied / '002'),
                                 copied / '002/suspended.png')
                unrelated = Path(temporary) / 'unrelated/suspended.png'
                self.assertEqual(app_paths.resolve_history_file(unrelated, copied / '002'), unrelated)

    def test_unicode_opencv_roundtrip_preserves_flags_pixels_and_ascii_behavior(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(cv2, 'imread', cv2.imread), patch.object(cv2, 'imwrite', cv2.imwrite):
            portable_runtime.install_unicode_file_access()
            frame = np.zeros((8, 9, 3), dtype=np.uint8)
            frame[:, :, 1] = 173
            for name in ('ascii.png', '中文图像.png'):
                path = Path(temporary) / name
                self.assertTrue(cv2.imwrite(str(path), frame, [cv2.IMWRITE_PNG_COMPRESSION, 1]))
                np.testing.assert_array_equal(cv2.imread(str(path), cv2.IMREAD_COLOR), frame)
                self.assertEqual(cv2.imread(str(path), cv2.IMREAD_GRAYSCALE).shape, (8, 9))
            self.assertIsNone(cv2.imread(str(Path(temporary) / '缺失.png')))


class PortableProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.manifest = {'app_version': APP_VERSION,
                         'git_branch': 'codex/unified-resolution-dispatch',
                         'git_commit': '0123456789abcdef0123456789abcdef01234567',
                         'git_dirty': False}

    def write_manifest(self, **updates):
        data = self.manifest | updates
        (self.folder / 'release-manifest.json').write_text(json.dumps(data), encoding='utf-8')

    def capture(self):
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'executable', str(self.folder / 'Leiming-Automation.exe')), \
             patch.object(runtime_identity.subprocess, 'run',
                          side_effect=AssertionError('Frozen release must not invoke Git')):
            return runtime_identity.capture_identity(self.folder, APP_VERSION)

    def test_frozen_manifest_identifies_exact_clean_sha_without_git(self):
        self.write_manifest()
        identity = self.capture()
        self.assertEqual(identity.app_version, APP_VERSION)
        self.assertEqual(identity.git_commit, self.manifest['git_commit'])
        self.assertEqual(identity.git_branch, self.manifest['git_branch'])
        self.assertIs(identity.git_dirty, False)
        self.assertIsNone(identity.git_error)
        self.assertEqual(identity.app_root, str(self.folder))
        self.assertEqual(identity.python_executable, str(self.folder / 'Leiming-Automation.exe'))

    def test_frozen_manifest_rejects_wrong_version(self):
        self.write_manifest(app_version='v0.1.19-beta.3')
        with self.assertRaisesRegex(ValueError, 'provenance'):
            self.capture()

    def test_frozen_manifest_rejects_noncanonical_or_wrong_length_sha(self):
        for sha in ('0123456', 'a' * 41, 'g' * 40, 'A' * 40, '0' * 39 + ' '):
            with self.subTest(sha=sha):
                self.write_manifest(git_commit=sha)
                with self.assertRaisesRegex(ValueError, 'provenance'):
                    self.capture()

    def test_trial_manifest_shows_dirty_code_in_runtime_identity(self):
        self.write_manifest(git_dirty=True)
        identity = self.capture()
        self.assertIs(identity.git_dirty, True)
        self.assertIn('dirty', identity.display_version)

    def test_frozen_manifest_rejects_unknown_or_nonboolean_clean_state(self):
        for dirty in (None, 0, 'false'):
            with self.subTest(dirty=dirty):
                self.write_manifest(git_dirty=dirty)
                with self.assertRaisesRegex(ValueError, 'provenance'):
                    self.capture()

    def test_frozen_missing_or_corrupt_manifest_cannot_borrow_worktree_identity(self):
        with self.assertRaises(FileNotFoundError):
            self.capture()
        (self.folder / 'release-manifest.json').write_text('{incomplete', encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            self.capture()

    def test_startup_error_is_written_to_portable_root(self):
        with patch.object(sys, 'frozen', False, create=True):
            try:
                raise ValueError('Invalid portable release provenance')
            except ValueError:
                portable_runtime.report_startup_error(self.folder)
        text = (self.folder / 'logs/startup-error.txt').read_text(encoding='utf-8')
        self.assertIn('ValueError: Invalid portable release provenance', text)

    def test_entry_reports_preparation_provenance_and_migration_errors(self):
        fake_ok = ModuleType('ok')
        fake_ok.OK = Mock()
        fake_config = ModuleType('src.config')
        fake_config.config = {}
        for stage in ('prepare', 'identity', 'migration'):
            with self.subTest(stage=stage), \
                 patch.dict(sys.modules, {'ok': fake_ok, 'src.config': fake_config}), \
                 patch('src.app_paths.APP_ROOT', self.folder), \
                 patch('src.app_paths.DATA_ROOT', self.folder), \
                 patch('src.portable_runtime.prepare') as prepare, \
                 patch('src.runtime_identity.get_runtime_identity') as identity, \
                 patch('src.config_migration.migrate') as migration, \
                 patch('src.portable_runtime.report_startup_error') as report:
                {'prepare': prepare, 'identity': identity, 'migration': migration}[stage].side_effect = \
                    RuntimeError(stage + ' failed')
                with self.assertRaisesRegex(RuntimeError, stage + ' failed'):
                    runpy.run_path(str(ROOT / 'main.py'), run_name='__main__')
                report.assert_called_once_with(self.folder)
        fake_ok.OK.assert_not_called()

    def test_navigation_entry_preserves_exit_status_and_does_not_start_normal_tasks(self):
        fake_ok = ModuleType('ok'); fake_ok.OK = Mock()
        fake_config = ModuleType('src.config'); fake_config.config = {}
        navigation = ModuleType('tools.validate_module_navigation')
        navigation.main = Mock(return_value=1)
        with patch.dict(sys.modules, {'ok': fake_ok, 'src.config': fake_config,
                                     'tools.validate_module_navigation': navigation}), \
             patch.object(sys, 'argv', ['Leiming-Automation.exe', '--validate-navigation',
                                       '--resolution', '4k', '--profile', '先攻']), \
             patch('src.app_paths.APP_ROOT', self.folder), \
             patch('src.portable_runtime.prepare'), \
             patch('src.runtime_identity.get_runtime_identity'), \
             patch('src.config_migration.migrate'):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(ROOT / 'main.py'), run_name='__main__')
        self.assertEqual(result.exception.code, 1)
        navigation.main.assert_called_once_with(['--resolution', '4k', '--profile', '先攻'])
        fake_ok.OK.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows startup message')
    def test_unwritable_startup_log_still_displays_original_error(self):
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(Path, 'mkdir', side_effect=PermissionError('read-only')), \
             patch('ctypes.windll.user32.MessageBoxW') as message:
            try:
                raise ValueError('original startup failure')
            except ValueError:
                portable_runtime.report_startup_error(self.folder)
        message.assert_called_once()
        self.assertIn('original startup failure', message.call_args.args[1])

    def test_explicit_roguelike_qa_entry_does_not_start_a_second_gui(self):
        fake_ok=ModuleType('ok');fake_ok.OK=Mock()
        fake_config=ModuleType('src.config');fake_config.config={}
        qa=ModuleType('tools.roguelike_gui_acceptance')
        with patch.dict(sys.modules,{'ok':fake_ok,'src.config':fake_config,
                                     'tools.roguelike_gui_acceptance':qa}), \
             patch.object(sys,'argv',['Leiming-Automation.exe','--validate-roguelike','--live','--runs','2']), \
             patch('src.app_paths.APP_ROOT',self.folder), \
             patch('src.portable_runtime.prepare'), \
             patch('src.runtime_identity.get_runtime_identity'), \
             patch('src.config_migration.migrate'):
            runpy.run_path(str(ROOT/'main.py'),run_name='__main__')
            self.assertEqual(sys.argv,['Leiming-Automation.exe','--live','--runs','2'])
        fake_ok.OK.assert_not_called()


if __name__ == '__main__':
    unittest.main()
