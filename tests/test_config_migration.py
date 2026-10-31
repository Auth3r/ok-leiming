"""Stage 1 ownership and migration checks use only disposable configuration copies."""
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from ok.util.config import Config
from src.config_migration import MigrationConflict, migrate
from src.config_ownership import DEFAULTS, MODULE_CRAFTING, OWNERS, TASKS, read_effective
from src.settings import load_settings
from src import tasks
from src.tasks import DailyTask, DarkTideTask, ModuleCraftTask, ModuleResumeTask


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures' / 'config'


def fixture(name):
    return json.loads((FIXTURES / f'{name}.json').read_text(encoding='utf-8'))


def write_fixture(root, name):
    data = fixture(name)
    settings = fixture('legacy_normal')['settings.json'] | data.get('settings.json', {})
    (root / 'settings.json').write_text(json.dumps(settings, ensure_ascii=False), encoding='utf-8')
    for name, values in data.items():
        if name == 'settings.json':
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_patch = patch('src.settings.ROOT', self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def saved(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob('*.json')
                if 'config-backups' not in path.parts}

    def task(self, cls):
        executor = SimpleNamespace(scene=None, device_manager=SimpleNamespace(interaction=object()))
        return cls(executor, SimpleNamespace())

    def test_first_start_enables_all_daily_modules_in_the_execution_plan(self):
        self.assertTrue(migrate(self.root))
        values = load_settings()
        self.assertEqual(values['game_exe'], 'Warp Knights.exe')
        self.assertEqual(values['window_title'], '')
        self.assertEqual((values['resolution_width'], values['resolution_height']), (0, 0))
        self.assertFalse(values['debug_mode'])
        self.assertTrue(all(values[key] for key in TASKS if key.startswith('execute_')))
        from src.run_plan import build_run_plan
        plan = build_run_plan(self.task(DailyTask))
        self.assertEqual({key: plan[key] for key in TASKS}, {key: values[key] for key in TASKS})
        for owner, keys in OWNERS.items():
            data = json.loads((self.root / 'configs' / f'{owner}.json').read_text(encoding='utf-8'))
            self.assertEqual(set(data), set(keys))
        self.assertFalse((self.root / 'config-backups').exists())

    def test_existing_disabled_daily_choices_are_not_reset_on_restart(self):
        migrate(self.root)
        path = self.root / 'configs/Tasks.json'
        values = json.loads(path.read_text(encoding='utf-8'))
        for all_disabled in (True, False):
            with self.subTest(all_disabled=all_disabled):
                values.update({key: not all_disabled and key != 'execute_fund'
                               for key in TASKS if key.startswith('execute_')})
                values['dark_tide_sweep_count'] = 0
                path.write_text(json.dumps(values), encoding='utf-8')
                original = self.saved()
                self.assertFalse(migrate(self.root))
                self.assertEqual(self.saved(), original)
                owner = Config('Tasks', {key: DEFAULTS[key] for key in TASKS}, folder=str(path.parent))
                self.assertEqual(dict(owner), values)
                from src.run_plan import build_run_plan
                plan = build_run_plan(self.task(DailyTask))
                self.assertEqual({key: plan[key] for key in TASKS}, values)

    def test_first_start_retains_existing_ui_preferences(self):
        path = self.root / 'configs/ui_config.json'
        path.parent.mkdir()
        original = b'{"MainWindow": {"Theme": "Dark"}}'
        path.write_bytes(original)
        self.assertTrue(migrate(self.root))
        self.assertEqual(path.read_bytes(), original)

    def test_fresh_defaults_and_saved_preferences_survive_second_start(self):
        migrate(self.root)
        path = self.root / 'configs/ModuleCrafting.json'
        values = json.loads(path.read_text(encoding='utf-8'))
        values.update({'制作配置': '先攻', '制作数量': 7, '属性刷新方式': '不刷新'})
        path.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')
        original = self.saved()
        self.assertFalse(migrate(self.root))
        self.assertEqual(self.saved(), original)
        self.assertEqual(load_settings()['制作数量'], 7)

    def test_missing_runtime_does_not_overwrite_existing_user_configs(self):
        path = self.root / 'configs/ModuleCrafting.json'
        path.parent.mkdir()
        path.write_text('{"制作数量": 9}', encoding='utf-8')
        original = self.saved()
        with self.assertRaisesRegex(MigrationConflict, '缺少 settings.json'):
            migrate(self.root)
        self.assertEqual(self.saved(), original)

    def test_missing_runtime_does_not_reinitialize_versioned_install(self):
        migrate(self.root)
        (self.root / 'settings.json').unlink()
        original = self.saved()
        with self.assertRaisesRegex(MigrationConflict, '缺少 settings.json'):
            migrate(self.root)
        self.assertEqual(self.saved(), original)

    def test_first_start_write_failure_rolls_back_created_files(self):
        from src import config_migration
        real_atomic = config_migration._atomic

        def fail_advanced(path, data):
            if path.name == 'Advanced.json':
                raise OSError('injected initial write failure')
            return real_atomic(path, data)

        with patch('src.config_migration._atomic', side_effect=fail_advanced):
            with self.assertRaisesRegex(OSError, 'injected'):
                migrate(self.root)
        self.assertEqual(self.saved(), {})
        self.assertTrue(migrate(self.root))

    def test_versioned_install_without_marker_preserves_user_files_byte_for_byte(self):
        migrate(self.root)
        path = self.root / 'configs/ModuleCrafting.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data.update({'制作配置': '先攻', '制作数量': 9, '属性刷新方式': '不刷新'})
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        (self.root / 'config_version.json').unlink()
        original = self.saved()
        self.assertTrue(migrate(self.root))
        saved = self.saved()
        self.assertEqual({key: value for key, value in saved.items()
                          if key != 'config_version.json'}, original)
        self.assertEqual(read_effective(self.root)['制作数量'], 9)
        self.assertFalse(migrate(self.root))
        self.assertFalse((self.root / 'config-backups').exists())

    def test_partial_new_owner_without_marker_is_not_treated_as_legacy(self):
        write_fixture(self.root, 'legacy_normal')
        for owner in ('Tasks', 'ModuleCrafting', 'Advanced', 'Developer'):
            with self.subTest(owner=owner):
                path = self.root / 'configs' / f'{owner}.json'
                path.write_text('{"user_value": 9}', encoding='utf-8')
                original = self.saved()
                with self.assertRaisesRegex(MigrationConflict, owner + '.json'):
                    migrate(self.root)
                self.assertEqual(self.saved(), original)
                self.assertFalse((self.root / 'config-backups').exists())
                path.unlink()

    def test_lost_marker_with_old_migration_backup_preserves_later_user_preferences(self):
        write_fixture(self.root, 'legacy_normal')
        migrate(self.root)
        path = self.root / 'configs/ModuleCrafting.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data.update({'制作配置': '先攻', '制作数量': 9, '属性刷新方式': '不刷新'})
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        (self.root / 'config_version.json').unlink()
        originals = self.saved()
        backup = self.root / 'config-backups/stage1-v0'
        backup_bytes = {path.relative_to(backup): path.read_bytes()
                        for path in backup.rglob('*') if path.is_file()}
        self.assertTrue(migrate(self.root))
        self.assertEqual({key: value for key, value in self.saved().items()
                          if key != 'config_version.json'}, originals)
        self.assertEqual(read_effective(self.root)['制作数量'], 9)
        self.assertEqual({path.relative_to(backup): path.read_bytes()
                          for path in backup.rglob('*') if path.is_file()}, backup_bytes)

    def test_unmarked_mixed_legacy_runtime_and_modern_owners_is_not_silently_blessed(self):
        migrate(self.root)
        path = self.root / 'settings.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['dark_tide_sweep_count'] = 42  # Conflicts with Tasks.json's value 5.
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        (self.root / 'config_version.json').unlink()
        originals = self.saved()
        with self.assertRaisesRegex(MigrationConflict, '缺少 config_version'):
            migrate(self.root)
        self.assertEqual(self.saved(), originals)

    def test_legacy_roguelike_owner_alone_does_not_require_version_marker(self):
        write_fixture(self.root, 'legacy_normal')
        path = self.root / 'configs/Roguelike.json'
        path.write_text('{"肉鸽循环次数": 7}', encoding='utf-8')
        self.assertTrue(migrate(self.root))
        self.assertEqual(read_effective(self.root)['肉鸽循环次数'], 7)
        self.assertTrue(read_effective(self.root)['显示肉鸽悬浮窗'])

    def test_completed_owner_writes_without_marker_recover_from_valid_migration_backup(self):
        write_fixture(self.root, 'legacy_normal')
        originals = self.saved()
        migrate(self.root)
        expected = read_effective(self.root)
        # Simulate interruption after every owner write, before the completion marker.
        (self.root / 'config_version.json').unlink()
        self.assertTrue(migrate(self.root))
        self.assertEqual(read_effective(self.root), expected)
        backup = self.root / 'config-backups/stage1-v0'
        for name, data in originals.items():
            self.assertEqual((backup / name).read_bytes(), data)

    def test_normal_values_owners_backup_and_second_start(self):
        write_fixture(self.root, 'legacy_normal')
        original = self.saved()
        self.assertTrue(migrate(self.root))
        values = load_settings()
        self.assertEqual(values['dark_tide_sweep_count'], 4)
        self.assertEqual(values['制作数量'], 1)
        self.assertEqual(values['属性刷新人数门槛'], 4)
        self.assertEqual({key: values[key] for key in TASKS if key.startswith('execute_')},
                         {key: True for key in TASKS if key.startswith('execute_')})
        self.assertEqual(json.loads((self.root / 'config_version.json').read_text())['config_version'], 1)
        backup = self.root / 'config-backups/stage1-v0'
        for name, data in original.items():
            self.assertEqual((backup / name).read_bytes(), data)
        first = self.saved()
        self.assertFalse(migrate(self.root))
        self.assertEqual(self.saved(), first)
        for owner, keys in OWNERS.items():
            actual = json.loads((self.root / 'configs' / f'{owner}.json').read_text(encoding='utf-8'))
            self.assertEqual(set(actual), set(keys))

    def test_missing_fields_are_filled_from_known_defaults(self):
        write_fixture(self.root, 'legacy_missing_fields')
        daily = self.root / 'configs/DailyTask.json'
        data = json.loads(daily.read_text(encoding='utf-8'))
        data.pop('recognition_timeout')
        daily.write_text(json.dumps(data), encoding='utf-8')
        self.assertTrue(migrate(self.root))
        values = read_effective(self.root)
        self.assertEqual(values['recognition_timeout'], 15.0)
        self.assertEqual(values['制作数量'], 2)
        self.assertEqual(values['属性刷新人数门槛'], 6)

    def test_missing_field_with_different_effective_value_conflicts(self):
        write_fixture(self.root, 'legacy_missing_fields')
        original = self.saved()
        with self.assertRaisesRegex(MigrationConflict, 'recognition_timeout'):
            migrate(self.root)
        self.assertEqual(self.saved(), original)

    def test_unknown_fields_are_read_and_backed_up_before_ok_cleanup(self):
        write_fixture(self.root, 'legacy_unknown_fields')
        craft = self.root / 'configs/ModuleCraftTask.json'
        raw = json.loads(craft.read_text(encoding='utf-8'))
        raw.pop('refresh_mode')  # The conflicting alias is tested separately.
        craft.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
        self.assertTrue(migrate(self.root))
        backup = self.root / 'config-backups/stage1-v0/configs/DailyTask.json'
        self.assertEqual(json.loads(backup.read_text(encoding='utf-8'))['retired_runtime_option'],
                         'retain before migration')

    def test_alias_conflict_preserves_raw_files(self):
        write_fixture(self.root, 'legacy_unknown_fields')
        original = self.saved()
        with self.assertRaisesRegex(MigrationConflict, 'refresh_mode'):
            migrate(self.root)
        self.assertEqual(self.saved(), original)
        self.assertFalse((self.root / 'config_version.json').exists())

    def test_proven_aliases_are_read_once_and_not_persisted_as_parallel_fields(self):
        write_fixture(self.root, 'legacy_normal')
        craft = self.root / 'configs/ModuleCraftTask.json'
        raw = json.loads(craft.read_text(encoding='utf-8'))
        raw.update(refresh_mode='自动', refresh_min_people=4, prioritize_fire_reward=True)
        craft.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
        migrate(self.root)
        owner = json.loads((self.root / 'configs/ModuleCrafting.json').read_text(encoding='utf-8'))
        self.assertEqual(set(owner), set(MODULE_CRAFTING))
        self.assertEqual(json.loads(craft.read_text(encoding='utf-8')), {})
        self.assertIn('refresh_mode', json.loads((self.root / 'config-backups/stage1-v0/configs/ModuleCraftTask.json').read_text(encoding='utf-8')))

    def test_craft_resume_conflict_has_field_sources_and_no_writes(self):
        write_fixture(self.root, 'legacy_craft_resume_conflict')
        original = self.saved()
        with self.assertRaises(MigrationConflict) as caught:
            migrate(self.root)
        message = str(caught.exception)
        for part in ('属性刷新方式', 'ModuleCraftTask.json', 'ModuleResumeTask.json'):
            self.assertIn(part, message)
        self.assertEqual(self.saved(), original)
        self.assertFalse((self.root / 'config_version.json').exists())

    def test_daily_and_dark_count_conflict_is_not_guessed(self):
        write_fixture(self.root, 'legacy_daily_count_conflict')
        dark = self.root / 'configs/DarkTideTask.json'
        dark.write_text(json.dumps({'dark_tide_sweep_count': 6}), encoding='utf-8')
        original = self.saved()
        with self.assertRaisesRegex(MigrationConflict, 'dark_tide_sweep_count'):
            migrate(self.root)
        self.assertEqual(self.saved(), original)

    def test_execute_switches_take_daily_canonical_value(self):
        write_fixture(self.root, 'legacy_normal')
        daily = self.root / 'configs/DailyTask.json'
        data = json.loads(daily.read_text(encoding='utf-8'))
        data['execute_fund'] = False
        daily.write_text(json.dumps(data), encoding='utf-8')
        self.assertTrue(migrate(self.root))
        self.assertFalse(read_effective(self.root)['execute_fund'])

    def test_matching_global_copies_promote_once(self):
        write_fixture(self.root, 'legacy_normal')
        for name in ('DailyTask', 'DarkTideTask', 'ModuleCraftTask', 'ModuleResumeTask'):
            path = self.root / 'configs' / f'{name}.json'
            data = json.loads(path.read_text(encoding='utf-8'))
            data.update(recognition_timeout=8.0, debug_mode=False)
            path.write_text(json.dumps(data), encoding='utf-8')
        self.assertTrue(migrate(self.root))
        self.assertEqual(read_effective(self.root)['recognition_timeout'], 8.0)
        self.assertFalse(read_effective(self.root)['debug_mode'])

    def test_global_conflict_reports_sources_and_preserves_legacy(self):
        write_fixture(self.root, 'legacy_runtime_conflict')
        original = self.saved()
        with self.assertRaises(MigrationConflict) as caught:
            migrate(self.root)
        for part in ('resolution_width', 'recognition_timeout', 'DailyTask.json', 'DarkTideTask.json'):
            self.assertIn(part, str(caught.exception))
        self.assertEqual(self.saved(), original)
        self.assertFalse((self.root / 'config_version.json').exists())

    def test_mid_write_failure_rolls_back_and_version_stays_old(self):
        write_fixture(self.root, 'legacy_normal')
        original = self.saved()
        from src import config_migration
        real_atomic = config_migration._atomic

        def fail_advanced(path, data):
            if path.name == 'Advanced.json':
                raise OSError('injected write failure')
            return real_atomic(path, data)

        with patch('src.config_migration._atomic', side_effect=fail_advanced):
            with self.assertRaisesRegex(OSError, 'injected'):
                migrate(self.root)
        self.assertEqual(self.saved(), original)
        self.assertFalse((self.root / 'config_version.json').exists())
        self.assertTrue((self.root / 'config-backups/stage1-v0/settings.json').exists())
        self.assertTrue(migrate(self.root))

    def test_successful_schema_is_narrow_and_runner_reads_same_owner(self):
        write_fixture(self.root, 'legacy_normal')
        migrate(self.root)
        for name in ('DailyTask', 'BlackShieldTask', 'BlackDispatchTask', 'FundTask',
                     'DarkTideTask', 'GanhuoTask', 'HuntGoldTask', 'DailyRewardTask',
                     'InspectTask', 'ModuleCraftTask', 'ModuleResumeTask'):
            self.assertEqual(self.task(getattr(tasks, name)).default_config, {}, name)
        self.assertEqual(set(self.task(tasks.ModulePrepareTask).default_config),
                         {'第一优先策略', '第二优先策略', '第三优先策略', '优先加成'})
        self.assertEqual(json.loads((self.root / 'configs/ModuleCraftTask.json').read_text()), {})
        self.assertEqual(json.loads((self.root / 'configs/ModuleResumeTask.json').read_text()), {})
        values = load_settings()
        self.assertEqual({key: values[key] for key in MODULE_CRAFTING},
                         json.loads((self.root / 'configs/ModuleCrafting.json').read_text(encoding='utf-8')))

    def test_installed_ok_config_loads_only_reduced_schema_after_migration(self):
        write_fixture(self.root, 'legacy_normal')
        migrate(self.root)
        with patch.object(Config, 'config_folder', str(self.root / 'configs')):
            for cls in (DailyTask, DarkTideTask, ModuleCraftTask, ModuleResumeTask):
                task = self.task(cls)
                task.load_config()
                self.assertEqual(dict(task.config), {})
            crafting = Config('ModuleCrafting', {key: DEFAULTS[key] for key in MODULE_CRAFTING})
            crafting['制作数量'] = 3
        self.assertEqual(read_effective(self.root)['制作数量'], 3)
        self.assertEqual(json.loads((self.root / 'configs/ModuleCraftTask.json').read_text()), {})
        self.assertEqual(json.loads((self.root / 'configs/ModuleResumeTask.json').read_text()), {})

    def test_startup_calls_migration_before_ok_construction(self):
        events = []
        fake_ok = ModuleType('ok')

        class FakeOK:
            def __init__(self, config):
                events.append('OK Config initialization')
            def start(self):
                events.append('start')

        fake_ok.OK = FakeOK
        fake_config = ModuleType('src.config')
        fake_config.config = {}
        with patch.dict('sys.modules', {'ok': fake_ok, 'src.config': fake_config}), \
             patch('src.config_migration.migrate', side_effect=lambda root: events.append('migration')):
            runpy.run_path(str(ROOT / 'main.py'), run_name='__main__')
        self.assertEqual(events, ['migration', 'OK Config initialization', 'start'])


if __name__ == '__main__':
    unittest.main()
