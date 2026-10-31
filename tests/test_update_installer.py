"""Real file/process installer transactions; Python launch harness is not EXE QA."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import update_installer as installer


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows portable installer')


@pytest.fixture(autouse=True)
def isolated_user_storage(tmp_path, monkeypatch):
    # Installer tests must never inspect the real user's fallback directory or
    # attempt to execute the byte-only package fixtures.
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'localappdata'))

    def no_executable(*args, **kwargs):
        raise OSError('Synthetic package: executable launch disabled in this test')

    monkeypatch.setattr(installer, '_launch', no_executable)


def test_locked_helper_file_reports_readable_failure_and_preserves_install(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    target, stage = tmp_path / 'installed', tmp_path / 'stage'
    package(target, 'v1.0.0-beta.7')
    package(stage, 'v1.0.0-beta.8')
    (target / 'settings.json').write_bytes(b'{"qa": "keep"}')
    before = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob('*') if p.is_file()}
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(target / installer.EXE))
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(target / '_internal/runtime.dll'), 0x80000000, 0, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    try:
        with pytest.raises(RuntimeError, match='无法复制安装器文件') as failure:
            installer.start_update_install(stage, target, 'v1.0.0-beta.8')
        message = str(failure.value)
        assert '当前程序未被替换' in message
        assert '日志' in message and len(message) < 120
        assert failure.value.__cause__ is not None  # Full paths stay in the exception log.
    finally:
        kernel.CloseHandle(handle)
    assert before == {str(p.relative_to(target)): p.read_bytes() for p in target.rglob('*') if p.is_file()}
    assert not list(tmp_path.glob('installed.backup-*'))
    assert not list(tmp_path.glob('.leiming-update-*/ready.json'))


def package(root, version, *, legacy=False):
    root.mkdir(parents=True)
    files = {installer.EXE: version.encode(), '_internal/runtime.dll': b'runtime '+version.encode(),
             'licenses/notice.txt': b'license '+version.encode()}
    for name, data in files.items():
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()
              if not (legacy and name.startswith('licenses/'))}
    manifest = {'schema_version': 1, 'repository': 'Auth3r/ok-leiming', 'platform': 'win-x64',
                'app_version': version, 'release_tag': version, 'git_commit': '7'*40,
                'git_dirty': False, 'trial': False, 'runtime_files_sha256': hashes}
    if legacy:
        for name in ('schema_version', 'repository', 'platform', 'release_tag', 'trial'):
            manifest.pop(name)
    (root/'release-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


def transaction(tmp_path):
    target = tmp_path/'installed'
    stage = tmp_path/'staging/unpacked/Leiming-Automation'
    helper = tmp_path/'.leiming-update-test'
    helper.mkdir()
    package(target, 'v1.0.0-beta.6')
    package(stage, 'v1.0.0-beta.7')
    parent = subprocess.Popen([sys.executable, '-c', 'pass'])
    parent.wait(timeout=10)
    job = {'id': 'test', 'target': str(target), 'stage': str(stage),
           'backup': str(tmp_path/'installed.backup-test'), 'version': 'v1.0.0-beta.7',
           'parent_pid': parent.pid}
    job_path = helper/'job.json'
    job_path.write_text(json.dumps(job), encoding='utf-8')
    return target, stage, helper, job_path


@pytest.fixture
def process_launcher(tmp_path, monkeypatch):
    """Only launch boundary is replaced; receipt, PID and waiting are real."""
    script = tmp_path/'startup_harness.py'
    script.write_text('''import json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1]).resolve()
if '--update-receipt' in sys.argv:
    receipt = Path(sys.argv[sys.argv.index('--update-receipt')+1])
    manifest = json.loads((root/'release-manifest.json').read_text(encoding='utf-8'))
    data = {'app_root': str(root), 'app_version': manifest['app_version'],
            'git_commit': manifest['git_commit'], 'pid': os.getpid(), 'gui_ready': True}
    temporary = receipt.with_suffix('.tmp')
    temporary.write_text(json.dumps(data), encoding='utf-8')
    temporary.replace(receipt)
time.sleep(30)
''', encoding='utf-8')
    processes = []

    def launch(target, *args):
        # Windows venv python.exe is a redirector with a different PID from
        # the interpreter. Use the base executable to test a direct child.
        process = subprocess.Popen([sys._base_executable, str(script), str(target), *map(str, args)], cwd=target)
        processes.append(process)
        return process

    monkeypatch.setattr(installer, '_launch', launch)
    yield processes
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


def test_installs_verified_files_preserves_data_and_checks_real_process_receipt(tmp_path, process_launcher):
    target, stage, helper, job_path = transaction(tmp_path)
    private_files = {'settings.json': b'{"custom":true}', 'config_version.json': b'{"version":6}',
                     'configs/user.json': b'personal', 'logs/original.log': b'original failure',
                     'config-backups/old.json': b'backup', 'my-notes/personal.txt': b'unknown user data'}
    for name, data in private_files.items():
        path = target/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert installer.apply_update(job_path) == 0
    result = json.loads((helper/'result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'installed'
    assert result['startup']['pid'] == process_launcher[0].pid
    assert result['startup']['app_version'] == 'v1.0.0-beta.7'
    assert (target/installer.EXE).read_bytes() == b'v1.0.0-beta.7'
    backup = Path(result['backup'])
    assert (backup/installer.EXE).read_bytes() == b'v1.0.0-beta.6'
    for name, data in private_files.items():
        assert (target/name).read_bytes() == data
        assert (backup/name).read_bytes() == data


def test_user_file_conflict_leaves_original_install_untouched(tmp_path):
    target, stage, helper, job_path = transaction(tmp_path)
    (target/'licenses/added-note.txt').write_bytes(b'personal note')
    (stage/'licenses/added-note.txt').write_bytes(b'new application notice')
    manifest_path = stage/'release-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['runtime_files_sha256']['licenses/added-note.txt'] = hashlib.sha256(b'new application notice').hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    assert installer.apply_update(job_path) == 1
    result = json.loads((helper/'result.json').read_text(encoding='utf-8'))
    assert result['phase'] == 'preserving_data'
    assert '冲突' in result['error']
    assert (target/'licenses/added-note.txt').read_bytes() == b'personal note'
    assert (target/installer.EXE).read_bytes() == b'v1.0.0-beta.6'
    assert not (tmp_path/'installed.backup-test').exists()


def test_corrupt_staged_executable_never_replaces_original(tmp_path):
    target, stage, helper, job_path = transaction(tmp_path)
    (stage/installer.EXE).write_bytes(b'corrupt')
    assert installer.apply_update(job_path) == 1
    result = json.loads((helper/'result.json').read_text(encoding='utf-8'))
    assert result['phase'] == 'validating'
    assert (target/installer.EXE).read_bytes() == b'v1.0.0-beta.6'
    assert not (helper/'ready.json').exists()


def test_waiting_on_real_windows_parent_does_not_terminate_it():
    process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'], stdin=subprocess.PIPE)
    try:
        with pytest.raises(TimeoutError, match='退出超时'):
            installer._wait_for_parent(process.pid, timeout=.05)
        assert process.poll() is None
        process.stdin.close()
        installer._wait_for_parent(process.pid, timeout=5)
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_stage_rename_failure_restores_original_directory(tmp_path, monkeypatch):
    target, stage, helper, job_path = transaction(tmp_path)
    (target/'settings.json').write_bytes(b'personal')
    original = installer._rename

    def fail_install_move(source, destination, **kwargs):
        if source == stage:
            raise PermissionError('simulated Windows lock on new directory')
        return original(source, destination, **kwargs)

    monkeypatch.setattr(installer, '_rename', fail_install_move)
    assert installer.apply_update(job_path) == 1
    assert (target/installer.EXE).read_bytes() == b'v1.0.0-beta.6'
    assert (target/'settings.json').read_bytes() == b'personal'
    assert not (tmp_path/'installed.backup-test').exists()


def test_bad_restart_receipt_rolls_back_and_keeps_failed_payload(tmp_path, monkeypatch, process_launcher):
    target, stage, helper, job_path = transaction(tmp_path)
    (target/'settings.json').write_bytes(b'personal')
    wait = installer._wait_for_receipt

    def reject_wrong_commit(process, receipt, version, commit, root, **kwargs):
        return wait(process, receipt, version, '8'*40, root, timeout=5)

    monkeypatch.setattr(installer, '_wait_for_receipt', reject_wrong_commit)
    assert installer.apply_update(job_path) == 1
    result = json.loads((helper/'result.json').read_text(encoding='utf-8'))
    assert result['rolled_back'] is True
    assert result['phase'] == 'restarting'
    assert (target/installer.EXE).read_bytes() == b'v1.0.0-beta.6'
    assert (target/'settings.json').read_bytes() == b'personal'
    assert (helper/'failed-install'/installer.EXE).read_bytes() == b'v1.0.0-beta.7'
    assert process_launcher[0].poll() is not None
    assert len(process_launcher) == 2  # original installation was restarted


def test_legacy_beta6_licenses_are_application_content_not_personal_config(tmp_path):
    old = tmp_path/'old'
    stage = tmp_path/'new'
    package(old, 'v1.0.0-beta.6', legacy=True)
    package(stage, 'v1.0.0-beta.7')
    (old/'settings.json').write_bytes(b'personal')
    (old/'licenses/personal-note.txt').write_bytes(b'unknown personal note')
    installer._copy_user_data(old, stage)
    assert (stage/'licenses/notice.txt').read_bytes() == b'license v1.0.0-beta.7'
    assert (stage/'settings.json').read_bytes() == b'personal'
    assert (stage/'licenses/personal-note.txt').read_bytes() == b'unknown personal note'


def test_stage_inside_current_install_is_rejected_before_touching_files(tmp_path):
    target, stage, helper, job_path = transaction(tmp_path)
    job = json.loads(job_path.read_text(encoding='utf-8'))
    job['stage'] = str(target/'download')
    job_path.write_text(json.dumps(job), encoding='utf-8')
    assert installer.apply_update(job_path) == 1
    result = json.loads((helper/'result.json').read_text(encoding='utf-8'))
    assert result['phase'] == 'validating'
    assert '目录关系无效' in result['error']
    assert (target/installer.EXE).read_bytes() == b'v1.0.0-beta.6'


@pytest.mark.parametrize('cancel', [False, True])
def test_waiting_transaction_rechecks_payload_or_respects_cancellation(tmp_path, process_launcher, cancel):
    target, stage, helper, job_path = transaction(tmp_path)
    parent = subprocess.Popen([sys._base_executable, '-c', 'import sys; sys.stdin.buffer.read(1)'],
                              stdin=subprocess.PIPE)
    job = json.loads(job_path.read_text(encoding='utf-8'))
    job['parent_pid'] = parent.pid
    job_path.write_text(json.dumps(job), encoding='utf-8')
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(installer.apply_update, job_path)
            deadline = time.monotonic()+5
            while not (helper/'ready.json').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert (helper/'ready.json').exists()
            if cancel:
                installer.cancel_update_install(job_path)
            else:
                (stage/installer.EXE).write_bytes(b'changed while waiting for exit')
                parent.stdin.close()
            assert future.result(timeout=10) == 1
            result = json.loads((helper/'result.json').read_text(encoding='utf-8'))
            assert result['phase'] == 'waiting_for_exit'
            assert (target/installer.EXE).read_bytes() == b'v1.0.0-beta.6'
            if cancel:
                assert parent.poll() is None
                assert process_launcher == []
                assert '取消' in result['error']
            else:
                assert '校验失败' in result['error']
    finally:
        if parent.poll() is None:
            parent.terminate()
        parent.wait(timeout=5)


def test_second_transaction_cannot_acquire_existing_windows_install_lock(tmp_path):
    target, stage, helper, job_path = transaction(tmp_path)
    key = hashlib.sha256(os.path.normcase(str(target.resolve())).encode()).hexdigest()[:16]
    lock_path = target.parent/('.leiming-update-lock-'+key)
    script = ('import msvcrt,sys; f=open(sys.argv[1],"a+b"); f.write(b"0"); f.flush();'
              'f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1);'
              'print("locked",flush=True); sys.stdin.buffer.read(1)')
    holder = subprocess.Popen([sys._base_executable, '-c', script, str(lock_path)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=False)
    try:
        assert holder.stdout.readline().strip() == b'locked'
        assert installer.apply_update(job_path) == 1
        result = json.loads((helper/'result.json').read_text(encoding='utf-8'))
        assert '已有更新安装事务' in result['error']
        assert (target/installer.EXE).read_bytes() == b'v1.0.0-beta.6'
        assert not (helper/'ready.json').exists()
    finally:
        holder.stdin.close()
        holder.wait(timeout=5)


def data_snapshot(root):
    """Independent expected bytes, directory structure, mode and modification time."""
    import stat
    if not root.exists():
        return None
    return {p.relative_to(root).as_posix(): {
        'bytes': p.read_bytes() if p.is_file() else None,
        'mode': stat.S_IMODE(p.stat().st_mode), 'mtime_ns': p.stat().st_mtime_ns,
    } for p in [root, *sorted(root.rglob('*'))]}


def external_data(target):
    from src import app_paths
    root = app_paths._fallback_root(target.resolve())
    seed_data(root)
    (root / 'portable-data-origin.json').write_text(
        json.dumps({'original_data_root': str(target)}), encoding='utf-8')
    return root


def seed_data(root):
    (root / 'configs/nested/empty').mkdir(parents=True)
    (root / 'configs/settings.json').write_bytes(b'{"count":7,"theme":"Light"}')
    (root / 'configs/nested/deleted.bin').write_bytes(bytes(range(256)))
    # Old, non-default mtimes make a lost copy2/copystat observable.
    for path in reversed([root, *root.rglob('*')]):
        os.utime(path, ns=(1600000000000000000, 1600000000000000000))


def change_data(root):
    (root / 'configs/settings.json').write_bytes(b'{"count":0,"theme":"Dark"}')
    (root / 'configs/nested/deleted.bin').unlink()
    (root / 'configs/nested/empty').rmdir()
    (root / 'new/subdirectory').mkdir(parents=True)
    (root / 'new/subdirectory/new.bin').write_bytes(b'new-version-data')


@pytest.fixture
def synthetic_startup(monkeypatch):
    """Only process boundary is synthetic; real receipt validation and files run."""
    launches = []
    wait = installer._wait_for_receipt
    monkeypatch.setattr(installer, '_wait_for_receipt',
                        lambda *args: wait(*args, timeout=.02))

    class Process:
        pid = 7654321
        stopped = False
        def poll(self):
            return 1 if self.stopped else None
        def terminate(self):
            self.stopped = True
        def wait(self, timeout):
            return 1

    def configure(mutate, *, receipt=False):
        def launch(target, *args):
            launches.append(args[0])
            process = Process()
            if args[0] == '--update-receipt':
                mutate()
                if receipt:
                    manifest = json.loads((target / 'release-manifest.json').read_text())
                    Path(args[1]).write_text(json.dumps({
                        'pid': process.pid, 'gui_ready': True, 'app_root': str(target),
                        'app_version': manifest['app_version'], 'git_commit': manifest['git_commit'],
                    }))
            return process
        monkeypatch.setattr(installer, '_launch', launch)
        return launches
    return configure


def test_external_data_timeout_restores_bytes_structure_and_metadata(tmp_path, monkeypatch, synthetic_startup):
    from src import app_paths
    target, stage, helper, job_path = transaction(tmp_path)
    root = external_data(target)
    monkeypatch.setattr(app_paths, 'DATA_ROOT', target)
    assert app_paths.configure_data_root(target) == root  # Actual production path selection.
    before = data_snapshot(root)
    launches = synthetic_startup(lambda: change_data(root))
    assert installer.apply_update(job_path) == 1
    assert data_snapshot(root) == before
    result = json.loads((helper / 'result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'failed' and result['rolled_back'] is True
    assert result['rollback_status'] == 'complete'
    assert result['external_data'][0]['restore_status'] == 'restored'
    assert (target / installer.EXE).read_bytes() == b'v1.0.0-beta.6'
    assert not (helper / 'startup.json').exists()
    assert launches == ['--update-receipt', '--update-failed']


def two_data_roots(tmp_path, target, monkeypatch):
    roots = [external_data(target), tmp_path / 'second-volume-layout/data']
    seed_data(roots[1])
    # Current application resolves one fallback root. Inject two affected roots
    # to test the transaction's collection handling, not a new storage setting.
    monkeypatch.setattr(installer, '_external_data_roots', lambda target: roots, raising=False)
    return roots


def test_multiple_external_data_roots_all_restore(tmp_path, monkeypatch, synthetic_startup):
    target, stage, helper, job_path = transaction(tmp_path)
    roots = two_data_roots(tmp_path, target, monkeypatch)
    before = [data_snapshot(root) for root in roots]
    synthetic_startup(lambda: [change_data(root) for root in roots])
    assert installer.apply_update(job_path) == 1
    assert [data_snapshot(root) for root in roots] == before
    result = json.loads((helper / 'result.json').read_text(encoding='utf-8'))
    assert result['rolled_back'] is True
    assert len(result['external_data']) == 2
    assert all(row['restore_status'] == 'restored' for row in result['external_data'])


def test_external_backup_failure_stops_before_program_replacement(tmp_path, monkeypatch, synthetic_startup):
    target, stage, helper, job_path = transaction(tmp_path)
    roots = two_data_roots(tmp_path, target, monkeypatch)
    before = [data_snapshot(target), *map(data_snapshot, roots)]
    copy = installer.shutil.copy2
    def fail_second_backup(source, destination, **kwargs):
        if Path(source) == roots[1] / 'configs/settings.json':
            raise OSError('simulated backup disk full: ' + str(source))
        return copy(source, destination, **kwargs)
    monkeypatch.setattr(installer.shutil, 'copy2', fail_second_backup)
    launches = synthetic_startup(lambda: [change_data(root) for root in roots], receipt=True)
    assert installer.apply_update(job_path) == 1
    assert [data_snapshot(target), *map(data_snapshot, roots)] == before
    assert '--update-receipt' not in launches
    assert not (tmp_path / 'installed.backup-test').exists()
    result = json.loads((helper / 'result.json').read_text(encoding='utf-8'))
    assert result['phase'] == 'backing_up_data'
    assert 'simulated backup disk full' in result['error']
    assert str(roots[1]) in result['error']
    assert result['rolled_back'] is False


def test_external_restore_failure_is_partial_and_keeps_recovery_copy(tmp_path, monkeypatch, synthetic_startup):
    target, stage, helper, job_path = transaction(tmp_path)
    roots = two_data_roots(tmp_path, target, monkeypatch)
    before = [data_snapshot(root) for root in roots]
    rename = installer._rename
    def fail_second_restore(source, destination, **kwargs):
        if destination == roots[1] and source.name == 'restored':
            raise PermissionError('simulated restore lock: ' + str(destination))
        return rename(source, destination, **kwargs)
    monkeypatch.setattr(installer, '_rename', fail_second_restore)
    launches = synthetic_startup(lambda: [change_data(root) for root in roots])
    assert installer.apply_update(job_path) == 1
    result = json.loads((helper / 'result.json').read_text(encoding='utf-8'))
    assert result['rolled_back'] is False
    assert result['rollback_status'] == 'partial'
    assert result['program_restored'] is True
    assert data_snapshot(roots[0]) == before[0]
    rows = {row['path']: row for row in result['external_data']}
    failed = rows[str(roots[1])]
    assert failed['restore_status'] == 'failed'
    assert str(roots[1]) in failed['error']
    assert data_snapshot(Path(failed['backup'])) == before[1]
    assert launches == ['--update-receipt']  # No old program writing partially restored data.
    assert not (helper / 'startup.json').exists()


def test_successful_update_keeps_new_external_data_and_old_backup(tmp_path, synthetic_startup):
    target, stage, helper, job_path = transaction(tmp_path)
    root = external_data(target)
    before = data_snapshot(root)
    changed = []
    def mutate():
        change_data(root)
        changed.append(data_snapshot(root))
    launches = synthetic_startup(mutate, receipt=True)
    assert installer.apply_update(job_path) == 0
    assert data_snapshot(root) == changed[0] and changed[0] != before
    result = json.loads((helper / 'result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'installed' and result['phase'] == 'complete'
    assert result['startup_confirmed'] is True
    assert data_snapshot(Path(result['external_data'][0]['backup'])) == before
    assert launches == ['--update-receipt']


def test_external_root_created_by_failed_startup_returns_to_absent(tmp_path, synthetic_startup):
    from src.app_paths import _fallback_root
    target, stage, helper, job_path = transaction(tmp_path)
    root = _fallback_root(target.resolve())
    assert not root.exists()
    synthetic_startup(lambda: seed_data(root))
    assert installer.apply_update(job_path) == 1
    assert not root.exists()
    result = json.loads((helper / 'result.json').read_text(encoding='utf-8'))
    row = result['external_data'][0]
    assert result['rolled_back'] is True
    assert (Path(row['failed_data']) / 'configs/settings.json').read_bytes() == b'{"count":7,"theme":"Light"}'


@pytest.mark.parametrize('when', ['backup', 'restore'])
def test_external_junction_is_rejected_without_touching_its_destination(tmp_path, monkeypatch, synthetic_startup, when):
    target, stage, helper, job_path = transaction(tmp_path)
    root = external_data(target)
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    (unrelated / 'keep.bin').write_bytes(b'unrelated user data')
    before = data_snapshot(unrelated)
    link = root / 'linked'
    def link_directory():
        # Windows junction creation does not require symlink privileges.
        import _winapi
        _winapi.CreateJunction(str(unrelated), str(link))
    if when == 'backup':
        link_directory()
    launches = synthetic_startup(link_directory if when == 'restore' else lambda: None, receipt=when == 'backup')
    try:
        assert installer.apply_update(job_path) == 1
        result = json.loads((helper / 'result.json').read_text(encoding='utf-8'))
        assert result['rolled_back'] is False
        assert data_snapshot(unrelated) == before
        if when == 'backup':
            assert '--update-receipt' not in launches
            assert not (tmp_path / 'installed.backup-test').exists()
        else:
            assert result['rollback_status'] == 'partial'
            assert Path(result['external_data'][0]['backup']).is_dir()
            assert launches == ['--update-receipt']
    finally:
        # Remove just the junction, never recursively touch its target.
        if link.is_junction():
            link.rmdir()


@pytest.mark.parametrize('failure', ['copy', 'corrupt_backup', 'stop_process', 'journal'])
def test_external_recovery_errors_keep_backup_and_never_claim_complete(tmp_path, monkeypatch, synthetic_startup, failure):
    target, stage, helper, job_path = transaction(tmp_path)
    root = external_data(target)
    before = data_snapshot(root)
    def mutate():
        change_data(root)
        if failure == 'corrupt_backup':
            records = json.loads((helper / 'data-backups.json').read_text(encoding='utf-8'))
            snapshot = Path(records['external_data'][0]['backup'])
            (snapshot / 'configs/settings.json').write_bytes(b'damaged snapshot')
        elif failure == 'copy':
            original = installer.shutil.copy2
            def fail_copy(source, destination, **kwargs):
                if Path(source).name == 'settings.json':
                    raise OSError('simulated recovery copy disk full')
                return original(source, destination, **kwargs)
            monkeypatch.setattr(installer.shutil, 'copy2', fail_copy)
        elif failure == 'journal':
            original = installer._write_json
            def fail_journal(path, value):
                if Path(path).name == 'data-backups.json':
                    raise OSError('simulated recovery journal disk full')
                return original(path, value)
            monkeypatch.setattr(installer, '_write_json', fail_journal)
    launches = synthetic_startup(mutate)
    if failure == 'stop_process':
        original = installer._launch
        def launch(*args):
            process = original(*args)
            def cannot_stop():
                raise PermissionError('simulated process termination denied')
            process.terminate = cannot_stop
            return process
        monkeypatch.setattr(installer, '_launch', launch)
    assert installer.apply_update(job_path) == 1
    result = json.loads((helper / 'result.json').read_text(encoding='utf-8'))
    assert result['rolled_back'] is False
    assert result['status'] == 'failed'
    assert result['rollback_status'] in ('partial', 'failed')
    assert launches == ['--update-receipt']
    snapshot = Path(result['external_data'][0]['backup'])
    assert snapshot.is_dir()
    if failure != 'corrupt_backup':
        assert data_snapshot(snapshot) == before
    else:
        assert (snapshot / 'configs/nested/deleted.bin').read_bytes() == bytes(range(256))


def test_external_data_on_another_volume_restores_without_cross_volume_rename(tmp_path, monkeypatch, synthetic_startup):
    import tempfile
    volume = os.environ.get('LEIMING_TEST_OTHER_VOLUME')
    if not volume:
        pytest.skip('Set LEIMING_TEST_OTHER_VOLUME to an available secondary test volume')
    volume = Path(volume).resolve()
    assert volume == Path(volume.anchor) and volume.drive != tmp_path.drive
    target, stage, helper, job_path = transaction(tmp_path)
    # Small synthetic data only; the unique directory is verified before cleanup.
    with tempfile.TemporaryDirectory(prefix='leiming-update-test-', dir=volume) as directory:
        parent = Path(directory).resolve()
        assert parent.parent == volume and parent.name.startswith('leiming-update-test-')
        root = parent / 'data'
        seed_data(root)
        before = data_snapshot(root)
        monkeypatch.setattr(installer, '_external_data_roots', lambda target: [root], raising=False)
        rename = installer._rename
        def same_volume_only(source, destination, **kwargs):
            assert source.drive == destination.drive
            return rename(source, destination, **kwargs)
        monkeypatch.setattr(installer, '_rename', same_volume_only)
        synthetic_startup(lambda: change_data(root))
        assert installer.apply_update(job_path) == 1
        assert data_snapshot(root) == before
        result = json.loads((helper / 'result.json').read_text(encoding='utf-8'))
        assert result['rolled_back'] is True
        assert Path(result['external_data'][0]['backup']).drive == volume.drive


def test_external_readonly_file_metadata_is_restored(tmp_path, synthetic_startup):
    import stat
    target, stage, helper, job_path = transaction(tmp_path)
    root = external_data(target)
    config = root / 'configs/settings.json'
    config.chmod(stat.S_IREAD)
    before = data_snapshot(root)
    def mutate():
        config.chmod(stat.S_IREAD | stat.S_IWRITE)
        change_data(root)
    synthetic_startup(mutate)
    try:
        assert installer.apply_update(job_path) == 1
        assert data_snapshot(root) == before
        assert not config.stat().st_mode & stat.S_IWRITE
    finally:
        # Only test-owned temporary files; allow eventual pytest cleanup.
        for path in tmp_path.rglob('settings.json'):
            path.chmod(stat.S_IREAD | stat.S_IWRITE)


def acl_snapshot(root):
    """Read actual Windows descriptors independently of the installer inventory."""
    import win32security as security
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        sd = security.GetFileSecurity(str(path), 7)  # Owner, group and DACL.
        result[path.relative_to(root).as_posix()] = {
            'sddl': security.ConvertSecurityDescriptorToStringSecurityDescriptor(sd, 1, 7),
            'control': sd.GetSecurityDescriptorControl()[0] & 0x150c,
        }
    return result


def set_test_acl(path, *, deny=True, protected=True):
    import win32security as security
    # Everyone includes this test's token, even when elevated. Deny only data
    # writes; READ_CONTROL / WRITE_DAC / DELETE stay usable for backup/cleanup.
    flags = 'OICI' if path.is_dir() else ''
    sddl = ('D:' + ('P' if protected else '')
            + (f'(D;{flags};0x2;;;WD)' if deny else '') + f'(A;{flags};FA;;;WD)')
    sd = security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1)
    security.SetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
        security.DACL_SECURITY_INFORMATION | (security.PROTECTED_DACL_SECURITY_INFORMATION
        if protected else security.UNPROTECTED_DACL_SECURITY_INFORMATION),
        None, None, sd.GetSecurityDescriptorDacl(), None)


def windows_write_allowed(path):
    import win32file
    import pywintypes
    try:
        handle = win32file.CreateFile(str(path), 2, 7, None, 3, 0x02000000, None)
    except pywintypes.error as error:
        assert error.winerror == 5, error
        return False
    handle.Close()
    return True


@pytest.mark.parametrize('receipt', [False, True])
@pytest.mark.parametrize('protected_file', [False, True])
def test_windows_dacl_transaction(tmp_path, synthetic_startup, receipt, protected_file):
    target, stage, helper, job_path = transaction(tmp_path)
    root = external_data(target)
    config = root / 'configs/settings.json'
    set_test_acl(root)
    set_test_acl(root / 'configs/nested')
    set_test_acl(config, protected=protected_file)
    before = acl_snapshot(root)
    content_before = data_snapshot(root)
    assert all(not windows_write_allowed(p) for p in (root, root / 'configs/nested', config))
    changed = {}
    def mutate():
        # Synthetic new version changes permissions as well as contents.
        for path in [root, *root.rglob('*')]:
            set_test_acl(path, deny=False)
        change_data(root)
        changed['acl'] = acl_snapshot(root)
        changed['data'] = data_snapshot(root)
    synthetic_startup(mutate, receipt=receipt)
    assert installer.apply_update(job_path) == (0 if receipt else 1)
    result = json.loads((helper / 'result.json').read_text(encoding='utf-8'))
    after = acl_snapshot(root)
    write_allowed = windows_write_allowed(config)
    # Preserve raw descriptors and effective access results even on old-code failure.
    (tmp_path / 'acl-evidence.json').write_text(json.dumps({
        'before': before, 'after': after, 'write_allowed_before': False,
        'write_allowed_after': write_allowed, 'result': result,
    }, indent=2), encoding='utf-8')
    assert after == (changed['acl'] if receipt else before)
    assert data_snapshot(root) == (changed['data'] if receipt else content_before)
    assert write_allowed is receipt
    assert acl_snapshot(Path(result['external_data'][0]['backup'])) == before
    if not receipt:
        assert result['rolled_back'] is True and result['rollback_status'] == 'complete'
        assert all(not windows_write_allowed(p) for p in (root, root / 'configs/nested', config))


@pytest.mark.parametrize('report_failure', ['journal', 'primary', 'both'])
def test_combined_stop_and_report_failure(tmp_path, monkeypatch, synthetic_startup, capsys, report_failure):
    target, stage, helper, job_path = transaction(tmp_path)
    root = external_data(target)
    before = data_snapshot(root)
    monkeypatch.setattr(installer.tempfile, 'gettempdir', lambda: str(tmp_path))
    attempts = []
    write = installer._write_json
    def fail_reports(path, value):
        path = Path(path)
        if (path.name == 'data-backups.json'
                or report_failure in ('primary', 'both') and path.name == 'result.json'
                or report_failure == 'both' and path.name.startswith('leiming-update-result-')):
            attempts.append(path.name)
            raise OSError('injected report failure: ' + str(path))
        return write(path, value)
    def mutate():
        change_data(root)
        monkeypatch.setattr(installer, '_write_json', fail_reports)
    launches = synthetic_startup(mutate)
    launch = installer._launch
    stopped = []
    def launch_unstoppable(*args):
        process = launch(*args)
        def cannot_stop():
            stopped.append(True)
            raise PermissionError('injected stop failure')
        process.terminate = cannot_stop
        return process
    monkeypatch.setattr(installer, '_launch', launch_unstoppable)
    try:
        code = installer.apply_update(job_path)
    finally:
        (tmp_path / 'fault-attempts.json').write_text(json.dumps({
            'stop': stopped, 'report': attempts, 'primary_exists': (helper / 'result.json').exists(),
        }), encoding='utf-8')
    assert code != 0 and stopped and 'data-backups.json' in attempts
    if report_failure == 'both':
        assert 'result.json' in attempts and any(n.startswith('leiming-update-result-') for n in attempts)
        assert not (helper / 'result.json').exists()
        assert not list(tmp_path.glob('leiming-update-result-*.json'))
        diagnostic = capsys.readouterr().err
        assert 'injected report failure' in diagnostic and 'injected stop failure' in diagnostic
        assert '未在限定时间' in diagnostic and 'original_backup' in diagnostic
        assert 'program_restored' in diagnostic and 'not_attempted' in diagnostic
        (tmp_path / 'stderr-evidence.txt').write_text(diagnostic, encoding='utf-8')
    else:
        report = helper / 'result.json' if report_failure == 'journal' else next(tmp_path.glob('leiming-update-result-*.json'))
        result = json.loads(report.read_text(encoding='utf-8'))
        assert '未在限定时间' in result['error']
        assert 'injected stop failure' in result['stop_error']
        assert any('injected report failure' in row['error'] for row in result['report_errors'])
        assert result['status'] == 'failed' and result['rolled_back'] is False
        assert result['rollback_status'] == 'failed' and result['program_restored'] is False
        assert result['external_data'][0]['restore_status'] == 'not_attempted'
        assert Path(result['original_backup']).is_dir()
    assert (target / installer.EXE).read_bytes() == b'v1.0.0-beta.7'
    assert (tmp_path / 'installed.backup-test' / installer.EXE).read_bytes() == b'v1.0.0-beta.6'
    journal = json.loads((helper / 'data-backups.json').read_text(encoding='utf-8'))
    assert data_snapshot(Path(journal['external_data'][0]['backup'])) == before
    assert data_snapshot(root) != before and launches == ['--update-receipt']


@pytest.mark.parametrize('failure', ['read', 'backup', 'restore', 'verify'])
def test_windows_dacl_failure_keeps_original_and_never_claims_complete(
        tmp_path, monkeypatch, synthetic_startup, failure):
    import win32security as security
    target, stage, helper, job_path = transaction(tmp_path)
    root = external_data(target)
    config = root / 'configs/settings.json'
    set_test_acl(root)
    before = acl_snapshot(root)
    content_before = data_snapshot(root)
    get_security, set_security = security.GetFileSecurity, security.SetFileSecurity
    injected = []
    def fail_read(path, flags):
        if Path(path) == config:
            injected.append(str(path))
            raise PermissionError('injected ACL read failure')
        return get_security(path, flags)
    def fail_set(path, flags, descriptor):
        candidate = Path(path)
        part = 'original' if failure == 'backup' else 'restored'
        if candidate.name == 'settings.json' and part in candidate.parts:
            injected.append(str(path))
            if failure == 'verify':
                # A real descriptor remains wrong on disk; verification must detect it.
                set_test_acl(candidate, deny=False)
                return
            raise PermissionError('injected ACL write failure')
        return set_security(path, flags, descriptor)
    if failure == 'read':
        monkeypatch.setattr(security, 'GetFileSecurity', fail_read)
    elif failure == 'backup':
        monkeypatch.setattr(security, 'SetFileSecurity', fail_set)
    def mutate():
        for path in [root, *root.rglob('*')]:
            set_test_acl(path, deny=False)
        change_data(root)
        monkeypatch.setattr(security, 'SetFileSecurity', fail_set)
    launches = synthetic_startup(mutate)
    assert installer.apply_update(job_path) == 1 and injected
    monkeypatch.setattr(security, 'GetFileSecurity', get_security)
    result = json.loads((helper / 'result.json').read_text(encoding='utf-8'))
    assert result['rolled_back'] is False and result['rollback_status'] != 'complete'
    row = result['external_data'][0]
    if failure in ('read', 'backup'):
        assert result['phase'] == 'backing_up_data' and not result['program_replaced']
        assert acl_snapshot(root) == before and data_snapshot(root) == content_before
        assert '--update-receipt' not in launches and not windows_write_allowed(config)
    else:
        assert result['program_restored'] and row['restore_status'] == 'failed'
        assert result['rollback_status'] == 'partial'
        assert acl_snapshot(Path(row['backup'])) == before
        assert data_snapshot(Path(row['backup'])) == content_before
        assert launches == ['--update-receipt']
        if failure == 'verify':
            assert 'DACL' in row['error'] and '校验失败' in row['error']


def test_restore_continues_when_journal_fails_and_reports_actual_states(tmp_path, monkeypatch, synthetic_startup):
    target, stage, helper, job_path = transaction(tmp_path)
    roots = two_data_roots(tmp_path, target, monkeypatch)
    before = [data_snapshot(root) for root in roots]
    write, rename = installer._write_json, installer._rename
    failures = []
    def fail_journal(path, value):
        if Path(path).name == 'data-backups.json':
            failures.append('journal')
            raise OSError('injected journal failure')
        return write(path, value)
    def fail_restore(source, destination, **kwargs):
        if source.name == 'installed.backup-test':
            failures.append('program')
            raise OSError('injected program restore failure')
        if destination == roots[1] and source.name == 'restored':
            failures.append('data')
            raise OSError('injected data restore failure')
        return rename(source, destination, **kwargs)
    def mutate():
        for root in roots:
            change_data(root)
        monkeypatch.setattr(installer, '_write_json', fail_journal)
        monkeypatch.setattr(installer, '_rename', fail_restore)
    launches = synthetic_startup(mutate)
    assert installer.apply_update(job_path) == 1
    result = json.loads((helper / 'result.json').read_text(encoding='utf-8'))
    assert set(failures) == {'journal', 'program', 'data'}
    assert '未在限定时间' in result['error']
    assert 'injected program' in result['program_restore_error']
    assert 'injected data' in result['data_restore_error']
    assert any('injected journal' in e['error'] for e in result['report_errors'])
    assert not result['program_restored'] and not result['rolled_back']
    assert result['rollback_status'] == 'partial'
    assert [r['restore_status'] for r in result['external_data']] == ['restored', 'failed']
    assert data_snapshot(roots[0]) == before[0] and data_snapshot(roots[1]) != before[1]
    assert [data_snapshot(Path(r['backup'])) for r in result['external_data']] == before
    assert Path(result['original_backup']).is_dir() and launches == ['--update-receipt']


@pytest.mark.parametrize('failure', ['serialize', 'replace'])
def test_atomic_report_failure_preserves_previous_valid_json(tmp_path, monkeypatch, failure):
    path = tmp_path / 'result.json'
    original = {'status': 'failed', 'error': 'original failure', 'backup': 'retained'}
    installer._write_json(path, original)
    if failure == 'replace':
        def deny_replace(*args):
            raise PermissionError('injected atomic replacement failure')
        monkeypatch.setattr(Path, 'replace', deny_replace)
        value = {'error': 'later error'}
    else:
        value = {'error': 'later error', 'invalid': object()}
    with pytest.raises((TypeError, PermissionError)):
        installer._write_json(path, value)
    assert json.loads(path.read_text(encoding='utf-8')) == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('receipt,both', [(False, False), (False, True), (True, False)])
def test_final_report_failure_preserves_data_and_returns_nonzero(
        tmp_path, monkeypatch, synthetic_startup, capsys, receipt, both):
    target, stage, helper, job_path = transaction(tmp_path)
    root = external_data(target)
    before = data_snapshot(root)
    monkeypatch.setattr(installer.tempfile, 'gettempdir', lambda: str(tmp_path))
    write = installer._write_json
    attempted = []
    def fail_result(path, value):
        path = Path(path)
        if path.name == 'result.json' or both and path.name.startswith('leiming-update-result-'):
            attempted.append(path.name)
            raise OSError('injected final report failure')
        return write(path, value)
    def mutate():
        change_data(root)
        monkeypatch.setattr(installer, '_write_json', fail_result)
    launches = synthetic_startup(mutate, receipt=receipt)
    assert installer.apply_update(job_path) == 1
    assert 'result.json' in attempted
    if both:
        diagnostic = capsys.readouterr().err
        assert 'injected final report failure' in diagnostic and '"report_status": "failed"' in diagnostic
        assert any(name.startswith('leiming-update-result-') for name in attempted)
    else:
        report = next(tmp_path.glob('leiming-update-result-*.json'))
        result = json.loads(report.read_text(encoding='utf-8'))
        assert result['status'] == 'failed' and not result['rolled_back']
        assert result['rollback_status'] != 'complete' if not receipt else result['startup_confirmed']
        if receipt:
            assert result['installation_status'] == 'installed'
        else:
            assert result['program_restored'] and '未在限定时间' in result['error']
            assert result['external_data'][0]['restore_status'] == 'restored'
    assert (data_snapshot(root) == before) is (not receipt)
    journal = json.loads((helper / 'data-backups.json').read_text(encoding='utf-8'))
    assert data_snapshot(Path(journal['external_data'][0]['backup'])) == before
    assert launches == ['--update-receipt']
