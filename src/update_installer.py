"""Portable Windows update transaction, executed outside the running install."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

EXE = 'Leiming-Automation.exe'
MUTABLE = {'settings.json', 'config_version.json', 'configs', 'config-backups',
           'logs', 'cache', 'output', 'reports', 'screenshots', 'portable-data-origin.json'}


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Never hide the write/replace error; the valid report is intact.


def _assert_tree(directory):
    # Junctions may leave the install even when Path.is_symlink() is false.
    for path in (directory, *directory.rglob('*')):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('更新目录包含链接或目录联接，原程序已保留：' + str(path))


def _validate_paths(job, job_path):
    target = Path(job['target']).resolve()
    stage = Path(job['stage']).resolve()
    helper = Path(job_path).resolve().parent
    backup = Path(job['backup']).resolve()
    if (target == target.parent or helper.parent != target.parent
            or not helper.name.startswith('.leiming-update-')
            or backup.parent != target.parent
            or backup.name != target.name + '.backup-' + job['id']
            or stage == target or target in stage.parents or stage in target.parents
            or target.parent not in stage.parents or helper == stage or stage in helper.parents):
        raise ValueError('更新安装目录关系无效，未替换原程序')
    if target.drive.lower() != stage.drive.lower():
        raise ValueError('更新暂存目录必须与安装目录位于同一磁盘')
    return target, stage, helper, backup


def _package(root, version):
    from src.update_client import validate_package
    return validate_package(root, version)


def _copy_user_data(old, stage):
    """Preserve mutable and unmanaged files, including unknown personal folders."""
    manifest = json.loads((old / 'release-manifest.json').read_text(encoding='utf-8'))
    managed = {name.replace('\\', '/').casefold()
               for name in manifest['runtime_files_sha256']}
    managed.add('release-manifest.json')
    legacy_licenses = ('schema_version' not in manifest
                       and not any(name.startswith('licenses/') for name in managed))
    _assert_tree(old)
    count = 0
    for source in old.rglob('*'):
        relative = source.relative_to(old)
        preserve = relative.parts[0].casefold() in MUTABLE or relative.as_posix().casefold() not in managed
        if source.is_file() and preserve:
            destination = stage / relative
            # Beta6's inventory was taken before bundling license notices.
            # Replace matching distribution notices; retain unknown files and
            # the untouched complete old installation as the transaction backup.
            if legacy_licenses and relative.parts[0] == 'licenses' and destination.is_file():
                continue
            if relative.parts[0].casefold() not in MUTABLE and destination.exists():
                # A formerly personal path becoming a managed application file
                # must not silently overwrite either side.
                raise ValueError('新程序与个人文件路径冲突，原程序已保留：' + str(relative))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            count += 1
        elif source.is_dir() and preserve:
            (stage / relative).mkdir(parents=True, exist_ok=True)
    return count


def _external_data_roots(target):
    # configure_data_root has exactly one external candidate per installation.
    # Include an absent candidate too: startup may create it if permissions change.
    from src.app_paths import _fallback_root
    return [_fallback_root(target)]


def _assert_data_path(path):
    # Inspect before resolve(), including ancestors, so a junction cannot turn
    # an application-specific data path into a different user's directory.
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('外置数据路径包含链接或重解析点：' + str(part))


def _data_inventory(root):
    """Content, metadata and Windows DACL; never traverse a reparse point."""
    _assert_data_path(root)
    records = {}

    def visit(path):
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('外置数据包含链接或重解析点：' + str(path))
        entry = {'mode': stat.S_IMODE(info.st_mode), 'mtime_ns': info.st_mtime_ns,
                 'dacl': _read_data_dacl(path)}
        if stat.S_ISDIR(info.st_mode):
            entry['type'] = 'directory'
            records[path.relative_to(root).as_posix()] = entry
            for child in path.iterdir():
                visit(child)
        elif stat.S_ISREG(info.st_mode):
            with path.open('rb') as source:
                entry.update(type='file', size=info.st_size,
                             sha256=hashlib.file_digest(source, 'sha256').hexdigest())
            records[path.relative_to(root).as_posix()] = entry
        else:
            raise ValueError('外置数据包含不支持的文件类型：' + str(path))

    visit(root)
    return records


def _save_data_backups(helper, records, report_errors=None):
    path = helper / 'data-backups.json'
    try:
        _write_json(path, {'external_data': records})
    except Exception as error:
        if report_errors is not None:
            report_errors.append({'path': str(path), 'error': str(error)})
        raise


def _record_data_backups(helper, records, report_errors):
    before = len(report_errors)
    try:
        _save_data_backups(helper, records, report_errors)
    except Exception as error:
        if len(report_errors) == before:
            report_errors.append({'path': str(helper / 'data-backups.json'), 'error': str(error)})
        # The error is retained independently; continue restoring other paths.
        return False
    return True


def _recovery_diagnostic(result):
    message = '更新恢复报告写入失败，请保留所有备份：' + json.dumps(result, ensure_ascii=False)
    try:
        if sys.stderr is not None:
            print(message, file=sys.stderr, flush=True)
            return
    except Exception:
        pass
    # PyInstaller's windowed helper has no stderr. Keep an independent Windows
    # debugger diagnostic when neither filesystem report can be written.
    try:
        ctypes.windll.kernel32.OutputDebugStringW(message)
    except Exception:
        pass


def _write_result(path, result, records):
    """Publish a complete report, or fail with a separate recovery report/output."""
    errors = result.setdefault('report_errors', [])
    for primary in (True, False):
        candidate = path
        try:
            if not primary:
                candidate = Path(tempfile.gettempdir()) / ('leiming-update-result-' + uuid4().hex + '.json')
            result['report_path'] = str(candidate)
            result['report_status'] = 'primary' if primary else 'fallback'
            if errors:
                result['recovery_inventory'] = records
            _write_json(candidate, result)
            if not primary:
                _recovery_diagnostic(result)
            return candidate
        except Exception as error:
            errors.append({'path': str(candidate), 'error': str(error)})
            result.setdefault('installation_status', result['status'])
            result['status'] = 'failed'
            result['rolled_back'] = False
            if result.get('rollback_status') == 'complete':
                result['rollback_status'] = 'partial'
    result['report_status'] = 'failed'
    result['recovery_inventory'] = records
    _recovery_diagnostic(result)
    return None


def _read_data_dacl(path):
    import win32security
    # Self-relative descriptor includes the ACL bytes, ACE order/flags, NULL
    # DACL distinction and protection/auto-inheritance control state.
    return bytes(win32security.GetFileSecurity(
        str(path), win32security.DACL_SECURITY_INFORMATION)).hex()


def _restore_data_dacl(path, descriptor):
    import win32security as security
    sd = security.SECURITY_DESCRIPTOR(bytes.fromhex(descriptor))
    control = sd.GetSecurityDescriptorControl()[0]
    # The low-level API does not propagate/recalculate child ACEs from the
    # backup directory's parent. AR + AI preserves the stored AI flag.
    auto_inherit_req = 0x0100  # SE_DACL_AUTO_INHERIT_REQ (not exported by pywin32).
    sd.SetSecurityDescriptorControl(auto_inherit_req,
        auto_inherit_req if control & security.SE_DACL_AUTO_INHERITED else 0)
    protection = (security.PROTECTED_DACL_SECURITY_INFORMATION
                  if control & security.SE_DACL_PROTECTED else security.UNPROTECTED_DACL_SECURITY_INFORMATION)
    security.SetFileSecurity(str(path), security.DACL_SECURITY_INFORMATION | protection, sd)
    if _read_data_dacl(path) != descriptor:
        raise OSError('外置数据 DACL 恢复校验失败：' + str(path))


def _copy_data_tree(source, destination):
    # Check each entry before descending. copytree's default can follow links,
    # and a Windows junction must not become a recursive copy of another tree.
    _assert_data_path(source)
    info = source.lstat()
    descriptor = _read_data_dacl(source)
    if stat.S_ISDIR(info.st_mode):
        destination.mkdir()
        for child in source.iterdir():
            _copy_data_tree(child, destination / child.name)
        shutil.copystat(source, destination, follow_symlinks=False)
    elif stat.S_ISREG(info.st_mode):
        shutil.copy2(source, destination, follow_symlinks=False)
    else:
        raise ValueError('外置数据包含不支持的文件类型：' + str(source))
    # Apply restrictive directory permissions only after its children exist.
    _restore_data_dacl(destination, descriptor)


def _backup_external_data(roots, helper, records, program_paths, report_errors):
    roots = [Path(root).absolute() for root in roots]
    for index, root in enumerate(roots):
        _assert_data_path(root)
        root = roots[index] = root.resolve()
        for other in [*program_paths, *roots[:index]]:
            if root == other or root in other.parents or other in root.parents:
                raise ValueError('外置数据与其他事务目录重叠：' + str(root))
    for root in roots:
        row = {'path': str(root), 'existed': root.exists(), 'backup': None,
               'backup_status': 'pending', 'restore_status': 'not_needed'}
        records.append(row)
        _save_data_backups(helper, records, report_errors)
        try:
            if row['existed']:
                if not root.is_dir():
                    raise ValueError('外置数据路径不是目录：' + str(root))
                row['inventory'] = _data_inventory(root)
                # Beside the data, not beside the program: no cross-volume rename.
                container = Path(tempfile.mkdtemp(prefix='.' + root.name + '.update-backup-', dir=root.parent))
                row['backup'] = str(container / 'original')
                _save_data_backups(helper, records, report_errors)
                _copy_data_tree(root, Path(row['backup']))
                if (_data_inventory(Path(row['backup'])) != row['inventory']
                        or _data_inventory(root) != row['inventory']):
                    raise ValueError('外置数据备份校验失败或备份期间发生变更：' + str(root))
            row['backup_status'] = 'complete'
        except Exception as error:
            row.update(backup_status='failed', error=str(error))
            _record_data_backups(helper, records, report_errors)
            raise RuntimeError('外置数据备份失败，未替换程序：' + str(root) + '；' + str(error)) from error
        _save_data_backups(helper, records, report_errors)


def _restore_external_data(helper, records, report_errors):
    """Restore exact trees, retain snapshots and quarantine new-version data."""
    for row in records:
        root = Path(row['path'])
        row['restore_status'] = 'pending'
        try:
            _record_data_backups(helper, records, report_errors)
            _assert_data_path(root)
            if root.exists():
                _data_inventory(root)  # Reject links before moving any data.
            if row['existed']:
                snapshot = Path(row['backup'])
                if _data_inventory(snapshot) != row['inventory']:
                    raise ValueError('外置数据恢复备份校验失败：' + str(snapshot))
                container = snapshot.parent
                restored = container / 'restored'
                # Keep the verified original snapshot, even after successful recovery.
                _copy_data_tree(snapshot, restored)
                if _data_inventory(restored) != row['inventory']:
                    raise ValueError('外置数据恢复副本校验失败：' + str(restored))
            elif root.exists():
                container = Path(tempfile.mkdtemp(prefix='.' + root.name + '.update-failed-', dir=root.parent))
            else:
                row['restore_status'] = 'restored'
                _record_data_backups(helper, records, report_errors)
                continue
            failed = container / 'failed-data'
            row['failed_data'] = str(failed)
            _record_data_backups(helper, records, report_errors)
            if root.exists():
                _rename(root, failed)
            if row['existed']:
                try:
                    _rename(restored, root)
                except Exception:
                    # Keep a usable path if possible, but never label modified
                    # data as restored. The original snapshot is still intact.
                    if failed.exists() and not root.exists():
                        _rename(failed, root)
                    raise
                if _data_inventory(root) != row['inventory']:
                    raise ValueError('外置数据恢复后校验失败：' + str(root))
            row['restore_status'] = 'restored'
        except Exception as error:
            row.update(restore_status='failed', error=str(root) + '；' + str(error))
        _record_data_backups(helper, records, report_errors)
    return all(row['restore_status'] == 'restored' for row in records)


def _data_results(records):
    # Full inventories remain in data-backups.json for manual recovery.
    return [{key: value for key, value in row.items() if key != 'inventory'} for row in records]


def _wait_for_parent(pid, timeout=180, cancel_path=None):
    if os.name != 'nt':
        raise RuntimeError('便携更新安装仅支持 Windows')
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, never terminate the parent.
    if not handle:
        if ctypes.get_last_error() == 87:  # Already exited.
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_path is not None and cancel_path.exists():
                raise InterruptedError('更新已取消，原程序未被替换')
            state = kernel.WaitForSingleObject(handle, min(500, max(1, int((deadline - time.monotonic()) * 1000))))
            if state == 0:
                return
            if state != 258:
                raise ctypes.WinError(ctypes.get_last_error())
        raise TimeoutError('等待原程序退出超时，原程序未被替换；请关闭程序后重新检查更新')
    finally:
        kernel.CloseHandle(handle)


def _rename(source, destination, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        try:
            source.rename(destination)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.25)


def _launch(target, *args):
    # PyInstaller's inherited loader state must not identify this new process
    # as a child of the copied helper's bundle.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('_PYI_')}
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return subprocess.Popen([str(target / EXE), *map(str, args)], cwd=target,
                            env=environment, close_fds=True)


def _wait_for_receipt(process, receipt, version, commit, target, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('新版程序启动后提前退出')
        if receipt.is_file():
            data = json.loads(receipt.read_text(encoding='utf-8'))
            if (data.get('app_version') != version or data.get('git_commit') != commit
                    or Path(data.get('app_root', '')).resolve() != target
                    or data.get('pid') != process.pid or not data.get('gui_ready')):
                raise ValueError('新版启动回执的版本、提交或进程不匹配')
            return data
        time.sleep(.2)
    raise TimeoutError('新版程序未在限定时间内完成启动，正在恢复原版本')


def apply_update(job_path):
    """Wait for old EXE, preserve data, swap, restart, and roll back on failure."""
    job_path = Path(job_path).resolve()
    result_path = job_path.parent / 'result.json'
    phase, process, swapped, target, backup = 'validating', None, False, None, None
    lock = None
    parent_exited = False
    program_moved = False
    startup_attempted = False
    external_data = []
    report_errors = []
    result = {'status': 'failed', 'phase': phase}
    try:
        job = json.loads(job_path.read_text(encoding='utf-8'))
        target, stage, helper, backup = _validate_paths(job, job_path)
        import msvcrt
        key = hashlib.sha256(os.path.normcase(str(target)).encode()).hexdigest()[:16]
        lock = (target.parent / ('.leiming-update-lock-' + key)).open('a+b')
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('已有更新安装事务正在进行，请等待其完成') from None
        _assert_tree(stage)
        manifest = _package(stage, job['version'])
        if backup.exists():
            raise FileExistsError('更新备份目录已存在，拒绝覆盖')
        _write_json(helper / 'ready.json', {'pid': os.getpid(), 'job': job['id']})
        phase = 'waiting_for_exit'
        _wait_for_parent(job['parent_pid'], cancel_path=helper / 'cancel.json')
        parent_exited = True
        if (helper / 'cancel.json').exists():
            raise InterruptedError('更新已取消，原程序未被替换')
        _assert_tree(stage)
        _package(stage, job['version'])
        phase = 'preserving_data'
        count = _copy_user_data(target, stage)
        phase = 'backing_up_data'
        _backup_external_data(_external_data_roots(target), helper, external_data,
                              [target, stage, helper, backup], report_errors)
        phase = 'replacing'
        _rename(target, backup)
        program_moved = True
        _rename(stage, target)
        swapped = True
        phase = 'restarting'
        receipt = helper / 'startup.json'
        startup_attempted = True
        process = _launch(target, '--update-receipt', receipt)
        data = _wait_for_receipt(process, receipt, job['version'], manifest['git_commit'], target)
        result = {'status': 'installed', 'phase': 'complete', 'version': job['version'],
                  'git_commit': manifest['git_commit'], 'backup': str(backup),
                  'preserved_files': count, 'startup': data, 'startup_confirmed': True,
                  'startup_attempted': True, 'program_replaced': True,
                  'external_data': _data_results(external_data)}
    except Exception as error:
        result = {'status': 'failed', 'phase': phase, 'error': str(error), 'rolled_back': False,
                  'program_replaced': swapped, 'program_restored': False,
                  'startup_attempted': startup_attempted,
                  'startup_confirmed': False, 'rollback_status': 'not_needed'}
        recovery_errors = []
        stopped = True
        if program_moved:
            try:
                if process is not None and process.poll() is None:
                    process.terminate()  # Only the new process started by this transaction.
                    process.wait(timeout=15)
            except Exception as stop_error:
                stopped = False
                result['stop_error'] = str(stop_error)
                recovery_errors.append('无法停止新版程序，未恢复仍在使用的数据：' + str(stop_error))
            result['original_backup'] = str(backup)
            if stopped:
                try:
                    if swapped:
                        _rename(target, job_path.parent / 'failed-install')
                    _rename(backup, target)
                    result['program_restored'] = True
                except Exception as rollback_error:
                    result['program_restore_error'] = str(rollback_error)
                    recovery_errors.append(str(rollback_error))
                data_restored = not startup_attempted or _restore_external_data(helper, external_data, report_errors)
                result['rolled_back'] = result['program_restored'] and data_restored and not report_errors
                if not data_restored:
                    result['data_restore_error'] = '外置数据未完整恢复，请保留备份：' + '；'.join(
                        row['error'] for row in external_data if row['restore_status'] == 'failed')
                    recovery_errors.append(result['data_restore_error'])
            else:
                for row in external_data:
                    row['restore_status'] = 'not_attempted'
                _record_data_backups(helper, external_data, report_errors)
            result['rollback_status'] = ('complete' if result['rolled_back'] else
                                         'partial' if result['program_restored'] or any(
                                             row['restore_status'] == 'restored' for row in external_data) else 'failed')
        if recovery_errors:
            result['rollback_error'] = '；'.join(recovery_errors)
        result['report_errors'] = report_errors
        result['external_data'] = _data_results(external_data)
        result['original_preserved'] = bool(target is not None and not program_moved and (target / EXE).is_file())
        written_report = _write_result(result_path, result, external_data)
        # Failed before replacement: leave a still-running old instance alone.
        if written_report is not None and not report_errors and (
                result.get('rolled_back') or (parent_exited and result['original_preserved'])):
            try:
                _launch(target, '--update-failed', written_report)
            except Exception as restart_error:
                result['restart_error'] = str(restart_error)
                _write_result(result_path, result, external_data)
    else:
        result['report_errors'] = report_errors
        _write_result(result_path, result, external_data)
    finally:
        if lock is not None:
            lock.close()
    return 0 if result['status'] == 'installed' else 1


def cancel_update_install(job_path):
    job_path = Path(job_path).resolve()
    job = json.loads(job_path.read_text(encoding='utf-8'))
    _validate_paths(job, job_path)
    _write_json(job_path.parent / 'cancel.json', {'cancelled': True})


def start_update_install(stage, target, version, exit_event=None):
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        raise RuntimeError('自动安装请使用 Windows 便携 EXE；源码运行只能检查更新')
    if exit_event is not None and exit_event.is_set():
        raise RuntimeError('程序正在退出，更新准备已取消')
    target, stage = Path(target).resolve(), Path(stage).resolve()
    if Path(sys.executable).resolve() != target / EXE:
        raise ValueError('当前运行程序与更新目标不一致')
    _package(stage, version)
    transaction = uuid4().hex
    helper = target.parent / ('.leiming-update-' + transaction)
    helper.mkdir()
    job_path = helper / 'job.json'
    job = {'id': transaction, 'target': str(target), 'stage': str(stage),
           'version': version, 'parent_pid': os.getpid(),
           'backup': str(target.parent / (target.name + '.backup-' + transaction))}
    _validate_paths(job, job_path)
    # Onedir EXE needs its own DLLs outside the directory about to be renamed.
    try:
        shutil.copy2(target / EXE, helper / EXE)
        shutil.copytree(target / '_internal', helper / '_internal')
    except (OSError, shutil.Error) as error:
        # Keep path-heavy copy errors in the chained log, not the narrow GUI status.
        raise RuntimeError('无法复制安装器文件，请检查文件占用、空间或权限；'
                           '当前程序未被替换。详情见日志。') from error
    _write_json(job_path, job)
    process = _launch(helper, '--apply-update', job_path)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            result_path = helper / 'result.json'
            detail = json.loads(result_path.read_text(encoding='utf-8')).get('error', '') if result_path.exists() else ''
            raise RuntimeError('更新安装器启动失败，当前程序未被替换。' + detail)
        if (helper / 'ready.json').is_file():
            return {'prepared': True, 'version': version, 'job': str(job_path)}
        time.sleep(.1)
    process.terminate()
    process.wait(timeout=15)
    raise TimeoutError('更新安装器未就绪，当前程序未被替换')


def report_update_startup(main_window):
    """Called only after the actual application window and panels initialized."""
    from src.runtime_identity import get_runtime_identity
    if '--update-receipt' in sys.argv:
        receipt = Path(sys.argv[sys.argv.index('--update-receipt') + 1]).resolve()
        identity = get_runtime_identity()
        target = Path(identity.app_root).resolve()
        job_path = receipt.parent / 'job.json'
        job = json.loads(job_path.read_text(encoding='utf-8'))
        _validate_paths(job, job_path)
        if (receipt.name != 'startup.json' or Path(job['target']).resolve() != target
                or job['version'] != identity.app_version or not main_window.isVisible()):
            raise ValueError('更新启动回执与当前安装不匹配')
        _write_json(receipt, {'app_version': identity.app_version, 'git_commit': identity.git_commit,
                             'app_root': identity.app_root, 'pid': os.getpid(), 'gui_ready': True})
    if '--update-failed' in sys.argv:
        from PySide6.QtWidgets import QMessageBox
        path = Path(sys.argv[sys.argv.index('--update-failed') + 1])
        result = json.loads(path.read_text(encoding='utf-8'))
        QMessageBox.warning(main_window, '更新未完成', '更新失败，当前运行原版本。\n' + result.get('error', '') + '\n详情：' + str(path))
